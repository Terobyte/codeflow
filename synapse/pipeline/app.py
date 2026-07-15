"""Voice pipeline assembly (Р-6, item 20; split host/session — M1 host-singleton).
`build_host` constructs the long-lived logical state (task store, speak ledger, confirm
flow, arbiter policy, breaker, cost cap, ...) ONCE, so it survives a WebRTC reconnect.
`build_session_pipeline` constructs the transport-agnostic per-connection processing chain --
Flux STT -> context aggregator -> cascade LLMSwitcher (Р-14 failover) -> TTS arbiter (Р-5) ->
Fish TTS -- referencing the host's long-lived objects by reference; a fresh one is built on
every connection because a pipecat FrameProcessor instance is only good for one PipelineRunner
run. `run()` — the `python -m synapse.pipeline.app` entrypoint — lazily imports LocalAudioTransport
(S4: it requires pyaudio/portaudio at *module import time*, which would otherwise make this whole
module unimportable without the optional `voice` extra, breaking every environment that never
runs live voice, including test_pipeline_smoke).
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import itertools
import logging
import os
from collections import deque
from typing import Any

from pipecat.frames.frames import LLMFullResponseEndFrame, TTSSpeakFrame
from pipecat.pipeline.llm_switcher import LLMSwitcher
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMAssistantAggregator, LLMUserAggregator
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.services.fish.tts import FishAudioTTSService

from pathlib import Path

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.approvals import ApprovalService, gate_digest
from synapse.bridge.kora import KoraRunner
from synapse.bridge.runspec import RunSpec
from synapse.bridge.state import Liveness, SpeakLedger, TaskStore
from synapse.cascade.breaker import CircuitBreaker
from synapse.cascade.services import CostCap, build_tier_services
from synapse.cascade.strategy import build_strategy_type
from synapse.clock import Clock, SystemClock
from synapse.config import SynapseConfig
from synapse.dispatcher.tools import ALL_SCHEMAS, KoraBridge, ToolHandlers, register_all
from synapse.journal import AlertKind, TurnJournal
from synapse.pipeline.arbiter import ArbiterPolicy, TTSArbiterProcessor
from synapse.pipeline.context_guard import GenerationGuard, GenerationStartHook, make_guarded_assistant_aggregator
from synapse.pipeline.tts_cache import TTSCache, TTSCacheObserver
from synapse.prompt import STAGE_RULES_COLLECT, STAGE_RULES_PROPOSE
from synapse.threads import ThreadStore

logger = logging.getLogger(__name__)

# UI-4 (S34): серверный модельный allowlist для гейт-запусков. kora_model из конфига — дефолт,
# он в allowlist не обязан входить исторически; валидируем только то, что пришло из UI/инструмента.
_KORA_MODELS = frozenset({"claude-opus-4-8", "claude-sonnet-5", "claude-fable-5"})


class TaskLocalThreadDict(dict):
    """Task-local thread dictionary to prevent race conditions on concurrent HTTP turns (B-PIPE-5 residual/R1 dedup)."""
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._var = contextvars.ContextVar("current_http_thread_id", default=None)

    def __getitem__(self, key: Any) -> Any:
        if key == "id":
            return self._var.get()
        return super().__getitem__(key)

    def __setitem__(self, key: Any, value: Any) -> None:
        if key == "id":
            self._var.set(value)
        else:
            super().__setitem__(key, value)

    def __contains__(self, key: Any) -> bool:
        if key == "id":
            return True
        return super().__contains__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        if key == "id":
            return self._var.get()
        return super().get(key, default)


class SynapseHost:
    """Long-lived logical state, built ONCE by `build_host()` and shared by reference across
    every WebRTC reconnect (M1 host-singleton): task store, speak ledger, confirm flow, arbiter
    policy, breaker, cost cap. None of these are pipecat FrameProcessors, so nothing here is
    tied to a single PipelineRunner run -- `SynapseSession` (per-connection) is.

    EXCEPTION (M1 slice 2): `_output_task` is the ONE field that is NOT long-lived -- it is a
    per-connection bind-slot for the currently live PipelineTask, set by `bind_output()` on
    connect and cleared by `unbind_output()` on disconnect/preempt. It exists so `speak()` can
    inject Kora's proactive SPEAK straight into the running output task (out-of-band, no input
    frame needed). Typed `Any` to avoid importing the pipecat worker type into this module --
    the only surface used is `has_finished()`/`queue_frame()` (duck-typed)."""

    def __init__(
        self,
        clock: Clock,
        cfg: SynapseConfig,
        journal: TurnJournal,
        store: TaskStore,
        speak_ledger: SpeakLedger,
        classifier: KeywordClassifier,
        confirm_flow: ConfirmFlow,
        arbiter_policy: ArbiterPolicy,
        bridge: KoraBridge,
        handlers: ToolHandlers,
        breaker: CircuitBreaker,
        cost_cap: CostCap,
        approvals: ApprovalService | None = None,
        kora_runner: KoraRunner | None = None,
        kora_log: deque | None = None,
        threads: Any = None,
        voice_thread: dict | None = None,
        voice_project: dict | None = None,
        projects: Any = None,
        text_loop: Any = None,
        turn_lock: asyncio.Lock | None = None,
        current_http_thread: dict | None = None,
    ) -> None:
        self.clock = clock
        self.cfg = cfg
        self.journal = journal
        self.store = store
        self.speak_ledger = speak_ledger
        self.classifier = classifier
        self.confirm_flow = confirm_flow
        self.arbiter_policy = arbiter_policy
        self.bridge = bridge
        self.handlers = handlers
        self.breaker = breaker
        self.cost_cap = cost_cap
        # С3: двухключевой контракт gate_action. None в стабах (тесты, не-гейт-сценарии).
        self.approvals = approvals
        # The real Kora producer (M1 slice 1), held on the host so its single in-flight task
        # survives WebRTC reconnects like the rest of the long-lived state. None when disabled.
        self.kora_runner = kora_runner
        # Display-only ring buffer behind GET /client/kora-log — kora status UI (tero run
        # 2026-07-12). Stored RAW like kora_runner (None when unwired, e.g. host stubs in
        # tests); the single reader (the route) guards for None at the call site.
        self.kora_log = kora_log
        # UI v2 слайс UI-2: треды (ThreadStore) + текущий голосовой тред (mut dict — меняют роуты).
        self.threads = threads
        self.voice_thread = voice_thread if voice_thread is not None else {"id": None}
        # UI v3 иерархия: активный проект дома (фидбек Теро «проекты → треды») — голосовой
        # авто-тред рождается В проекте, а не сиротой. Mut dict, меняет /api/active-thread.
        self.voice_project = voice_project if voice_project is not None else {"id": None}
        # UI v2 слайс UI-3: проекты, текстовый ход, очередь ходов, текущий HTTP-тред.
        self.projects = projects
        self.text_loop = text_loop
        self.turn_lock = turn_lock or asyncio.Lock()
        # CR-5: busy-check (store.has_active_task) и launch (_launch_run) атомарны на ХОСТЕ.
        # Пер-thread гейт-локи сериализуют треды, но стор — глобальный синглтон «одна активная
        # задача». Раньше между has_active_task и _launch_run не было ни одного await, и
        # корректность держалась на этой конвенции; появись await — два треда запустили бы
        # две Коры. Лок превращает конвенцию в структурный инвариант. Внутри сегодня ни одного
        # await (_launch_run синхронный), лок дёшев; обратного порядка захвата (launch→thread)
        # нет — deadlock невозможен.
        self._launch_lock = asyncio.Lock()
        self.current_http_thread = current_http_thread if current_http_thread is not None else {"id": None}
        # M1 slice 2 (the one NON-long-lived field, see class docstring): the currently live
        # per-connection PipelineTask, or None when no client is connected.
        self._output_task: Any = None
        # UI-4: per-thread гейт-локи — single-flight двух конкурентных гейт-вызовов на один тред.
        self._gate_locks: dict[str, asyncio.Lock] = {}

    @property
    def current_http_thread(self) -> dict:
        return self._current_http_thread

    @current_http_thread.setter
    def current_http_thread(self, value: dict | None) -> None:
        # CR-1: setter НЕ инициализирует _output_task/_gate_locks — иначе любое повторное
        # `host.current_http_thread = …` молча отвязывает живой PipelineTask (тихий дроп SPEAK,
        # класс B17) и сбрасывает все per-thread гейт-локи. Их инициализация живёт в __init__.
        if isinstance(value, TaskLocalThreadDict):
            self._current_http_thread = value
            return
        self._current_http_thread = TaskLocalThreadDict()
        if isinstance(value, dict) and "id" in value:
            self._current_http_thread["id"] = value["id"]

    def bind_output(self, task: Any) -> None:
        """Point the SPEAK injector at this connection's live output task. SYNCHRONOUS and
        await-free: webrtc calls it while already holding its own lock, so the host must never
        acquire a lock here (S6-style)."""
        self._output_task = task

    def unbind_output(self, task: Any) -> None:
        """Clear the bind-slot IFF it still points at `task` -- a preempting connection's new
        bind supersedes this one, so a late unbind of the superseded task is a harmless no-op
        (the `is` check fails). SYNCHRONOUS and await-free (called under webrtc's lock)."""
        if self._output_task is task:
            self._output_task = None

    def voice_session_live(self) -> bool:
        """B43: «идёт ли живой звонок» — та же правда, которой пользуются push_speak_frame/
        speak (бинд M1-слайса-2 + has_finished()), инкапсулированная для роутов: никто вне
        хоста не должен щупать _output_task напрямую. Возвращает НАСТОЯЩИЙ bool — роут
        active-thread гейтится строгим `is True`, чтобы stub-хосты тестов (MagicMock)
        никогда не считались «живым звонком»."""
        t = self._output_task
        return bool(t is not None and not t.has_finished())

    async def push_speak_frame(self, text: str) -> bool:
        """Inject Kora's SPEAK straight into the running output task, out-of-band (no input
        frame needed). Re-checks liveness: the task may have finished between `speak()`
        scheduling this and it running. `queue_frame` on a finished task is a SILENT DROP
        (worker.py: an unbounded put never raises/blocks, the drain task is gone), so the
        `has_finished()` guard is what actually prevents a lost-in-the-void SPEAK.

        Returns True iff the frame was actually queued. B17: the finished/unbound guard
        returns normally WITHOUT raising, so the raise-only revert (B01) never fired for this
        clean-return silent drop — the ledger stayed spoken=True and the Р-15г watchdog was
        disarmed. Signalling non-delivery lets `_on_speak_frame_done` revert here too."""
        t = self._output_task
        if t is not None and not t.has_finished():
            await t.queue_frame(TTSSpeakFrame(text=text, append_to_context=False))
            return True
        return False

    def speak(self, text: str) -> None:
        """SPEAK entry point (called by on_speak). Registers the ledger ALWAYS and synchronously
        (Р-15г: a critical that DID get its SPEAK stops counting as an unpaired-critical alert; an
        in-flight SPEAK must not false-alarm the watchdog either), then:
        - live output task + running loop -> schedule an out-of-band TTSSpeakFrame injection.
          The done-callback REVERTS the optimistic registration IFF the injection raised (B01) —
          a dropped critical must re-arm the watchdog, not stay recorded as delivered. Does NOT
          also call arbiter_policy.push_speak (the injected frame travels the arbiter downstream).
        - live output task but NO running loop (e.g. a sync test path) -> arbiter fallback.
        - no live output task -> arbiter fallback (frame-driven, drained when a frame flows)."""
        marked = self.speak_ledger.register_speak_text(text, self.clock.now())
        t = self._output_task
        if t is not None and not t.has_finished():
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                self.arbiter_policy.push_speak(text)
                return
            fut = asyncio.ensure_future(self.push_speak_frame(text))
            # B01/B9: the ledger was marked spoken up front (above). If push_speak_frame raises
            # (queue_frame on a task torn down mid-emit), that optimistic mark is a LIE — the
            # critical produced no audio. Revert it in the callback so the Р-15г
            # CRITICAL_WITHOUT_SPEAK watchdog fires instead of the drop vanishing silently.
            fut.add_done_callback(functools.partial(self._on_speak_frame_done, marked=marked))
        else:
            self.arbiter_policy.push_speak(text)

    def _on_speak_frame_done(self, fut: "asyncio.Future[Any]", *, marked: list[str]) -> None:
        if fut.cancelled():
            self.speak_ledger.revert_speak(marked)  # cancelled → never delivered → re-arm watchdog
            return
        exc = fut.exception()
        if exc is not None:
            # NOT delivered → revert the optimistic mark so the Р-15г watchdog alerts.
            logger.warning("push_speak_frame injection failed; SPEAK dropped: %r", exc)
            self.speak_ledger.revert_speak(marked)
            return
        if fut.result() is False:
            # B17: the output task finished/was unbound in the scheduling window, so
            # push_speak_frame returned a clean False — a SILENT drop, no audio, and NO arbiter
            # fallback was taken (speak() saw the task live). Revert exactly as the cancel/raise
            # paths do so the dropped critical re-arms the Р-15г watchdog instead of vanishing.
            logger.warning("push_speak_frame silently dropped SPEAK (output task finished); reverting")
            self.speak_ledger.revert_speak(marked)

    async def monitor_forever(self) -> None:
        """R8: periodically drives speak_ledger.check()/store.liveness() so the Р-15г/Р-11
        invariants fire even between turns, not only incidentally when a turn happens to run."""
        last_live = Liveness.OK
        while True:
            await asyncio.sleep(self.cfg.heartbeat_interval_s)
            # B2: one transient failure in the loop body (e.g. journal.alert's os.fsync raising)
            # must NOT permanently kill this task — it is the SOLE voice-path driver of the Р-15г
            # CRITICAL_WITHOUT_SPEAK check. Log and keep looping; only CancelledError stops it.
            try:
                now = self.clock.now()
                for kind, detail in self.speak_ledger.check(now, self.cfg.critical_speak_window_s):
                    self.journal.alert(AlertKind(kind), detail)
                live = self.store.liveness(now, self.cfg.stale_after_s, self.cfg.unreachable_after_s)
                # B12 (Р-11): liveness's result was previously discarded and no stale/unreachable
                # alert existed — a Kora dying between turns was invisible. Surface it ONCE on the
                # OK→degraded transition (not every tick).
                if live != last_live and live != Liveness.OK:
                    self.journal.alert(AlertKind.KORA_UNREACHABLE, {"liveness": live.value})
                last_live = live
                # B30: drive the cost cap's daily recovery even when idle (no failover turns).
                self.cost_cap.maybe_reset(now)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("monitor_forever iteration failed; continuing")

    # --- UI-4: стадийный гейт -----------------------------------------------------------

    def _resolve_root_for(self, th) -> str:
        """Корень запуска Коры для треда: папка проекта, либо дефолт-воркспейс конфига.
        Зеркало build_host._resolve_project_root — без него gate_action не знает, где план-файл."""
        if th.project_id and self.projects is not None:
            proj = self.projects.get(th.project_id)
            if proj:
                return proj["path"]
        return self.cfg.kora_workspace_dir

    def resolve_thread_root(self, th) -> str:
        """Публичная поверхность корня треда для роутов (/api/threads/{id}/diff). Делегат к
        _resolve_root_for — роут не лезет через приватную границу хоста (pattern-fidelity)."""
        return self._resolve_root_for(th)

    def _run_finished(self, thread_id: str, outcome: str, gate_mode: str | None = None) -> None:
        """Обёртка on_run_finished. `gate_mode` — вид завершившегося рана (несёт RunSpec):
        docs_only/full = гейт-ран стадии, None = прямая диспетчеризация (submit/confirm).

        Гейт-ран: пишет исход (freshness-сигнал write_code) и, если ран был на стадии code и
        завершился completed, переводит тред в done. set_outcome про стадии не знает (факт 10)
        — переход code→done живёт здесь.

        Прямая задача (B46, реопен B07 закрыт): в треде С гейт-флоу (request_text есть) она
        НЕВИДИМА для гейт-стейта — не трогает ни last_outcome (иначе завершение несвязанной
        мелочи воскрешает stale-plan-гвард и Кора кодит по чужому плану), ни стадию (юзер
        всё ещё собирает/правит запрос). В ЧИСТОМ direct-dispatch треде (гейт-флоу не было)
        исход пишется для UI, а completed двигает collect→done (B47: бейдж «СБОР» на
        выполненной задаче)."""
        if self.threads is None:
            return
        th = self.threads.get(thread_id)
        if th is None:
            return
        if gate_mode is None and th.request_text is not None:
            return  # B46: несвязанная прямая задача не касается гейт-стейта треда
        self.threads.set_outcome(thread_id, outcome)
        if outcome != "completed":
            return
        target = "done" if gate_mode is not None and th.stage == "code" else None
        if gate_mode is None and th.stage == "collect":
            target = "done"  # B47: чистый direct-dispatch тред покидает «СБОР»
        if target is not None:
            try:
                self.threads.set_stage(thread_id, target)
            except ValueError:
                pass  # гонка: стадия уже сменилась — молчаливый no-op

    async def gate_action(self, thread_id: str, action: str,
                          model: str | None = None, confirm: bool = False,
                          fast: bool = False, user_initiated: bool = True) -> dict:
        """Единая хост-функция гейта (UI-4): её зовёт и POST /api/threads/{id}/gate, и голосовой
        инструмент диспетчера gate_action. СТРОГИЙ порядок: тред → per-thread lock → валидация
        модели → busy-чек ДО set_stage → ветвление. Возвращает dict с ok/error.

        С3 (Ф0.3): user_initiated различает каналы. HTTP-клик (user_initiated=True) несёт
        подтверждение живого пользователя — ApprovalService не требуется. Голосовой tool-путь
        (user_initiated=False): confirm=true из tool call больше не власть — запуск требует
        двухключевого approval (readback → user turn → affirm + совпадение digest со стадией)."""
        th = self.threads.get(thread_id) if self.threads is not None else None
        if th is None:
            return {"error": "unknown_thread"}
        lock = self._gate_locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            # B48: архивный тред — read-only. Ни один гейт-экшен (запуск Коры, revise) не
            # должен оживлять «убранный» тред; чтение под локом — конкурентный архив виден.
            if th.archived:
                return {"error": "archived"}
            # валидация модели — только когда она передана (revise её не несёт)
            if model is not None and model not in _KORA_MODELS:
                return {"error": "invalid_model"}
            if action == "revise":
                # revise не запускает ран — launch_lock не нужен.
                try:
                    self.threads.set_stage(thread_id, "collect")
                except ValueError:
                    return {"error": "illegal_stage"}
                # B07: regressing to collect invalidates any prior run's outcome. write_code's only
                # staleness signals are last_outcome=="completed" + plan-file existence, neither tied
                # to the current request_text — so a completed spec_plan from request A would keep
                # write_code satisfied, and the NEXT propose (request B) would launch A's on-disk
                # plan. Reset the outcome so write_code refuses (stale_plan) until a fresh spec_plan
                # completes for the new request.
                self.threads.set_outcome(thread_id, None)
                # С3: revise меняет стадию на collect — pending approval (если был staged) инвалидируется.
                if self.approvals is not None:
                    self.approvals.invalidate(thread_id)
                self.threads.append_feed(thread_id,
                                         {"ts": self.clock.now(), "kind": "event", "text": "правки → сбор"})
                return {"ok": True, "stage": "collect"}
            # CR-5: launch-действия (send_to_kora/write_code) под ХОСТОВЫМ launch_lock — busy-check
            # и _launch_run атомарны на синглтоне-сторе. per-thread lock выше сериализует тред, но
            # два РАЗНЫХ треда проходят разные per-thread локи; без launch_lock между
            # has_active_task() и _launch_run мог бы вклиниться await и запустить вторую Кору.
            if action in ("send_to_kora", "write_code"):
                async with self._launch_lock:
                    # busy-чек ДО set_stage: запуск-действия не должны двигать стадию на занятом синглтоне
                    if self.kora_runner is not None and self.store.has_active_task():
                        return {"error": "busy"}
                    # С3 (Ф0.3): голосовой путь (user_initiated=False) — двухключевой approval.
                    # confirm=true из tool call не читается как власть; HTTP-клик (user_initiated=True)
                    # несёт подтверждение сам, ApprovalService в этом случае выключен. digest несёт И
                    # стадию: любое движение стадии между stage() и consume() инвалидирует pending.
                    if not user_initiated and self.approvals is not None:
                        digest = gate_digest(th.request_text, action, model, fast, th.stage)
                        approval = self.approvals.consume(thread_id, action, digest, self.clock.now())
                        if approval is None:
                            readback = self.approvals.stage(thread_id, action, digest, self.clock.now())
                            return {"error": "confirm_required", "readback": readback}
                    if action == "send_to_kora":
                        # user_initiated-путь: confirm=true из клика всё ещё нужен (belt+suspenders).
                        if user_initiated and not confirm:
                            return {"error": "confirm_required"}
                        request_text = th.request_text
                        if not request_text:
                            return {"error": "no_request"}
                        if fast:
                            target_stage, gate_mode, text = "code", "full", request_text
                        else:
                            target_stage = "spec_plan"
                            gate_mode = "docs_only"
                            text = (f"Подготовь спеку и план по запросу ниже. План запиши в файл "
                                    f"docs/plans/{thread_id}.md (создай директории). Запрос: {request_text}")
                        # B06: _launch_run's first act is set_stage, which raises ValueError on an illegal
                        # transition (e.g. send_to_kora straight from `collect`). Guard it like `revise`
                        # does — an unhandled ValueError escapes gate_action as a 500 / voice-path crash.
                        # set_stage runs BEFORE any store mutation, so a caught raise leaves no partial state.
                        try:
                            self._launch_run(th, target_stage, gate_mode, text, model)
                        except ValueError:
                            return {"error": "illegal_stage"}
                        return {"ok": True, "stage": target_stage}
                    if action == "write_code":
                        if user_initiated and not confirm:
                            return {"error": "confirm_required"}
                        # план-файл должен существовать И последняя SPEC_PLAN — completed (иначе stale_plan:
                        # устаревший файл от провалившейся попытки не должен пускать CODE)
                        # B18: root может быть None (projectless тред + kora_workspace_dir не задан) — это
                        # ЛЕГАЛЬНЫЙ сигнал «дефолт-воркспейс» (RunSpec его так и трактует). Раньше `Path(None)`
                        # тут кидал TypeError МИМО ValueError-гарда ниже → неперехваченный 500 / краш хода.
                        # Резолвим план-путь против того же дефолт-воркспейса, что и рантайм Коры
                        # (зеркало kora.py:485 `kora_workspace_dir or ~/synapse-kora-workspace`).
                        root = self._resolve_root_for(th) or os.path.expanduser("~/synapse-kora-workspace")
                        plan_path = Path(root) / "docs" / "plans" / f"{thread_id}.md"
                        if not plan_path.exists():
                            return {"error": "no_plan_file"}
                        if th.last_outcome != "completed":
                            return {"error": "stale_plan"}
                        text = (f"Реализуй по плану docs/plans/{thread_id}.md. "
                                f"Исходный запрос: {th.request_text}")
                        try:  # B06: same illegal-transition guard as send_to_kora above.
                            self._launch_run(th, "code", "full", text, model)
                        except ValueError:
                            return {"error": "illegal_stage"}
                        return {"ok": True, "stage": "code"}
            return {"error": "unknown_action"}

    def _launch_run(self, th, stage: str, gate_mode: str, text: str, model: str | None) -> None:
        """Общий хвост запуска стадийного рана: set_stage → task_id → store.start_task →
        append_task → set_last_model → kora_runner.start → gate_card в ленту."""
        from synapse.bridge.state import TaskStatus
        # B11: gate-minted IDs live in their OWN `gate-` namespace, DISJOINT from confirm's
        # `task-…` IDs. The two mint sites are independent itertools.count(1) generators with the
        # same `{ms}-{seq}` tail, so a shared prefix let a voice/confirm task and a UI-gate task
        # collide on the identical string at the same clock tick+seq → _task_index silently
        # overwrote → a task's live log misrouted into another (possibly cross-project) thread.
        # Distinct prefixes make cross-module collision structurally impossible.
        task_id = f"gate-{int(self.clock.now() * 1000)}-{next(_GATE_TASK_SEQ)}"
        self.threads.set_stage(th.id, stage)
        self.store.start_task(task_id, text, TaskStatus.RUNNING, self.clock.now())
        self.threads.append_task(th.id, task_id)
        if model is not None:
            self.threads.set_last_model(th.id, model)
        root = self._resolve_root_for(th)
        self.kora_runner.start(
            task_id, text,
            RunSpec(thread_id=th.id, project_root=root, gate_mode=gate_mode, model=model),
        )
        self.threads.append_feed(th.id, {
            "ts": self.clock.now(), "kind": "gate_card",
            "stage": stage, "action": "run_started", "model": model,
        })


_GATE_TASK_SEQ = itertools.count(1)


class _CostCountingLLMSwitcher(LLMSwitcher):
    """B04: counts a paid-tier attempt on a SUCCESSFUL generation, closing the R9 hole where
    `CostCap.record_paid_attempt` was reachable ONLY via the failover error path — so a healthy
    tier1 turn (the common case) made a real billed call that never counted and the daily cap was
    structurally inert. A completed generation pushes an `LLMFullResponseEndFrame` DOWNSTREAM out
    of the switcher; the ParallelPipeline filters ensure only the ACTIVE tier's frames escape, so
    one such frame == one completed paid attempt. Gated on `active_tier_index()==0`: the INITIAL
    tier is the one the failover path never pre-counts (`strategy._advance` already counts every
    tier it switches TO), so this counts exactly the attempt that was being missed without
    double-counting a failover-then-success."""

    def __init__(self, services, *, strategy_type, cost_cap: CostCap,
                 labels: list, clock: Clock) -> None:
        super().__init__(services, strategy_type=strategy_type)
        self._cost_cap = cost_cap
        self._labels = labels
        self._clock = clock

    async def push_frame(self, frame, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMFullResponseEndFrame):
            idx = self.strategy.active_tier_index()
            # B21: only count the INITIAL tier0 attempt here. If failover RETURNED to tier0
            # this generation, strategy._advance already counted it — recounting double-charges
            # the cap and trips it prematurely. `advanced_this_generation()` is the guard.
            if (idx == 0 and self._labels[idx].paid
                    and not self.strategy.advanced_this_generation()):
                self._cost_cap.record_paid_attempt(self._clock.now())
        await super().push_frame(frame, direction)


def build_host(cfg: SynapseConfig, clock: Clock | None = None) -> SynapseHost:
    """Hard-fails via cfg.validate_voice_keys() before touching the network if a required
    key is missing (R5) — never a silently half-configured host. Builds the long-lived logical
    state exactly ONCE; call `build_session_pipeline(host)` per WebRTC connection for the
    per-connection processors."""
    cfg.validate_voice_keys()
    clock = clock or SystemClock()

    journal = TurnJournal(cfg.journal_dir, clock)
    store = TaskStore(clock, journal_dir=cfg.journal_dir)
    speak_ledger = SpeakLedger()
    classifier = KeywordClassifier(cfg.destructive_keywords)
    confirm_flow = ConfirmFlow(
        store, clock, classifier, journal,
        cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s,
    )
    # С3 (Ф0.3): ApprovalService — двухключевой контракт gate_action. confirm=true из tool call
    # больше не власть: голосовой запуск требует readback → user turn → affirm + совпадение digest.
    # TTL = тот же confirm_timeout_s (единый бюджет подтверждения для обоих сервисов).
    approvals = ApprovalService(clock, cfg.confirm_timeout_s, cfg.affirm_words, cfg.deny_words)

    arbiter_policy = ArbiterPolicy()

    # Holder for the late-bound host: on_speak is handed to KoraBridge/KoraRunner BEFORE the
    # SynapseHost is constructed (below), so the closure can't capture `host` directly. It reads
    # `_h["host"]` at call time instead -- filled in just before build_host returns, always well
    # before any SPEAK actually fires at runtime. (A method on host would force splitting the
    # constructor, which touches the slice-0 host-singleton wiring -- avoided.)
    _h: dict = {}

    def on_speak(text: str) -> None:
        # SPEAK path (Р-5/Р-15/Р-15г): host.speak() registers the ledger ALWAYS, then makes the
        # text audible -- injecting a TTSSpeakFrame into the live per-connection output task when
        # one is bound (M1 slice 2 proactive push), else the frame-driven arbiter fallback.
        _h["host"].speak(text)

    # Build the real Kora producer before the bridge so submit/confirm-COMMITTED can launch it
    # and request_cancel can tear it down (M1 slice 1). Disabled → the old hollow behavior.
    # kora_log: display-only лента «размышлений Коры» (kora status UI, tero run 2026-07-12) —
    # ring buffer на хосте, кормится log_sink'ом раннера, читается роутом /client/kora-log.
    kora_log: deque = deque(maxlen=cfg.kora_log_max)
    # UI v2 слайс UI-2: треды. Автотред голосового submit: у голоса всегда есть текущий
    # тред (UI-3 даст клиенту его выбирать); нет → создаётся из текста задачи. Тред-стор
    # персистит метаданные синхронно в точках переходов (находка G).
    threads = ThreadStore(clock, Path(cfg.journal_dir) / "threads", feed_max=cfg.thread_feed_max)
    voice_thread: dict = {"id": None}
    voice_project: dict = {"id": None}  # UI v3: проект, в котором дом рожает авто-треды

    def _kora_log_sink(entry: dict) -> None:
        # Горячий кэш live-стрима (ring) + правда на диске (S3). Исключения глотает
        # вызывающая сторона (_stream) — display-путь не валит ран по конструкции.
        kora_log.append(entry)
        tid = entry.get("task_id")
        th = threads.thread_for_task(tid) if tid else None
        if th is not None:
            threads.append_feed(th.id, entry)

    def _on_run_finished(thread_id: str, outcome: str, gate_mode: str | None = None) -> None:
        # UI-4: обёртка над host._run_finished (исход + стадийные переходы; B46 — прямые
        # задачи не трогают гейт-стейт). Holder-паттерн: kora_runner строится ДО host, но
        # on_run_finished зовётся в runtime (после сборки host).
        host = _h["host"]
        if host is not None:
            host._run_finished(thread_id, outcome, gate_mode)
        else:
            threads.set_outcome(thread_id, outcome)

    kora_runner = (
        KoraRunner(cfg, store, speak_ledger, clock, journal, on_speak,
                   log_sink=_kora_log_sink, on_run_finished=_on_run_finished)
        if cfg.kora_enabled
        else None
    )

    # UI v2 слайс UI-3: текстовый канал + проекты. C-guard (спека §4, находка C):
    # answer_kora доставляет ответ ТОЛЬКО когда ход идёт в треде awaiting-запуска.
    from synapse.projects import ProjectStore
    projects = ProjectStore(Path(cfg.journal_dir) / "projects.json")
    turn_lock = asyncio.Lock()
    current_http_thread = TaskLocalThreadDict()

    def _stage_block_for(thread_id: str | None) -> str:
        th = threads.get(thread_id) if thread_id else None
        if th is None:
            return ""
        if th.stage == "collect":
            return STAGE_RULES_COLLECT
        if th.stage == "propose":
            return STAGE_RULES_PROPOSE
        return ""

    def _on_compact(thread_id: str) -> None:
        # UI-5 (S10): факт компакта истории → запись в ленту треда. Сырой feed-файл не
        # меняется компактом (он жмёт только LLM-историю в памяти), это лишь уведомление.
        threads.append_feed(thread_id, {"ts": clock.now(), "kind": "event", "text": "контекст сжат"})

    def _propose_for(thread_id: str | None, text: str, *, project_id: str | None = None) -> dict:
        """Commit a dispatcher-approved request summary to its thread.

        Voice has no thread until a meaningful summary exists; creating it here makes the
        successful tool result truthful instead of silently writing to a None id.
        """
        th = threads.get(thread_id) if thread_id else None
        if th is None:
            th = threads.create(
                title=text,
                project_id=project_id if project_id and projects.get(project_id) else None,
            )
            if thread_id is None:
                voice_thread["id"] = th.id
        if th.archived:
            return {"outcome": "thread_archived"}  # B48: свод не коммитится в убранный тред
        if th.stage != "collect":
            return {"outcome": "illegal_stage"}
        try:
            threads.set_request(th.id, text)
            threads.set_stage(th.id, "propose")
        except ValueError:
            return {"outcome": "illegal_stage"}
        # С3 (Ф0.3): смена СВОДА инвалидирует pending approval этого треда (digest несёт
        # request_text — он бы и сам не совпал, но явный invalidate чистит pending сразу, не
        # дожидаясь consume). НЕ внутри ThreadStore — обратная зависимость threads→bridge запрещена.
        approvals.invalidate(th.id)
        threads.append_feed(th.id, {
            "ts": clock.now(), "kind": "gate_card", "stage": "propose", "action": "send_to_kora",
        })
        return {"outcome": "proposed", "thread_id": th.id, "stage": "propose"}

    def _voice_propose(text: str) -> dict:
        result = _propose_for(voice_thread["id"], text, project_id=voice_project["id"])
        if result.get("thread_id"):
            voice_thread["id"] = result["thread_id"]
        return result

    def _http_propose(text: str) -> dict:
        tid = current_http_thread["id"]
        if tid is None:
            # The HTTP message route normally creates its thread first. Preserve the same
            # truthful no-thread behaviour as voice if a caller bypasses that route.
            return {"outcome": "no_active_thread"}
        return _propose_for(tid, text)

    async def _gate_for(thread_id: str | None, action: str, *, model: str | None = None,
                        confirm: bool = False, fast: bool = False,
                        user_initiated: bool = False) -> dict:
        if thread_id is None:
            return {"outcome": "no_active_thread"}
        return await _h["host"].gate_action(
            thread_id, action, model=model, confirm=confirm, fast=fast,
            user_initiated=user_initiated,
        )

    async def _voice_gate(action: str, **kwargs: Any) -> dict:
        # С3: голосовой tool-путь — user_initiated=False → двухключевой approval (confirm=true из
        # tool call больше не власть). readback возвращается в результата, Кора озвучит его.
        return await _gate_for(voice_thread["id"], action, user_initiated=False, **kwargs)

    async def _http_gate(action: str, **kwargs: Any) -> dict:
        # С3: HTTP tool-путь — user_initiated=False (это тоже LLM-канал, не клик). Реальный
        # HTTP-клик идёт через POST /api/threads/{id}/gate с user_initiated=True.
        return await _gate_for(current_http_thread["id"], action, user_initiated=False, **kwargs)

    def _project_bound(project_id: str) -> dict:
        return {"outcome": "project_bound", "project_id": project_id}

    def _awaiting_thread_id() -> str | None:
        task = store.task
        th = threads.thread_for_task(task.id) if task is not None else None
        return th.id if th is not None else None

    def _voice_answer(text: str) -> bool:
        # CR-4: асимметрия с _http_answer НАМЕРЕННАЯ. Голос — канал дома: вопрос Коры прозвучал
        # вслух, voice_thread["id"] может быть None после реконнекта (привязка ещё не воскресла),
        # и строгий гвард `awaiting is None or voice_thread["id"] != awaiting` сломал бы доставку
        # ответа — ответ Коре падал бы в no_pending_question. Здесь гвард мягче: блокируется
        # только ЯВНЫЙ чужой тред (voice_thread стоит в другом треде, awaiting — в этом); None и
        # неопределённость пропускаются. Якорь-тест: voice_thread=None + awaiting → доставляется;
        # HTTP-ответ из чужого треда → no_pending_question.
        if kora_runner is None:
            return False
        awaiting = _awaiting_thread_id()
        if awaiting is not None and voice_thread["id"] not in (None, awaiting):
            return False  # голос стоит в чужом треде — ответ Коре не отсюда
        return kora_runner.provide_answer(text)

    def _http_answer(text: str) -> bool:
        if kora_runner is None:
            return False
        awaiting = _awaiting_thread_id()
        if awaiting is None or current_http_thread["id"] != awaiting:
            return False  # реплика из треда Б не должна улетать ответом Коре в А
        return kora_runner.provide_answer(text)

    def _resolve_project_root(th) -> str | None:
        # UI v3: единый резолвер для голоса и HTTP — Кора работает в папке проекта треда.
        if th.project_id:
            proj = projects.get(th.project_id)
            return proj["path"] if proj else None
        return None

    def _on_task_committed(task_id: str, text: str) -> None:
        th = threads.get(voice_thread["id"]) if voice_thread["id"] else None
        if th is not None and th.archived:
            th = None  # B48: стейл-привязка на архивный тред деградирует в свежий тред
        if th is None:
            # Авто-тред рождается В активном проекте дома (иерархия «проекты → треды»);
            # несуществующий/удалённый проект тихо деградирует в «без проекта».
            pid = voice_project["id"]
            th = threads.create(title=text,
                                project_id=pid if pid and projects.get(pid) else None)
            voice_thread["id"] = th.id
        threads.append_task(th.id, task_id)
        # Раньше здесь было жёсткое project_root=None — голосовая задача в проектном треде
        # игнорировала папку проекта и Кора шла в дефолтный workspace.
        kora_runner.start(task_id, text,
                          RunSpec(thread_id=th.id, project_root=_resolve_project_root(th)))

    bridge = KoraBridge(
        store=store,
        confirm_flow=confirm_flow,
        clock=clock,
        on_speak=on_speak,
        cfg=cfg,
        on_task_committed=_on_task_committed if kora_runner else None,
        on_cancel=kora_runner.request_cancel if kora_runner else None,
        on_answer=_voice_answer if kora_runner else None,
        on_propose=_voice_propose,
        on_gate=_voice_gate,
        on_bind=_project_bound,
        projects=projects,
        threads=threads,
        thread_id_for=lambda: voice_thread["id"],
    )
    handlers = ToolHandlers(bridge, journal)

    # S7: HTTP-канал — СВОЙ ToolHandlers/bridge (дедуп _current_turn_id не делится с голосом),
    # но store/confirm — синглтоны.
    http_bridge = KoraBridge(
        store=store, confirm_flow=confirm_flow, clock=clock, cfg=cfg,
        on_speak=on_speak,
        on_task_committed=None,
        on_cancel=kora_runner.request_cancel if kora_runner else None,
        on_answer=_http_answer if kora_runner else None,
        on_propose=_http_propose,
        on_gate=_http_gate,
        on_bind=_project_bound,
        projects=projects,
        threads=threads,
        thread_id_for=lambda: current_http_thread["id"],
    )

    def _http_task_committed(task_id: str, text: str) -> None:
        tid = current_http_thread["id"]
        th = threads.get(tid) if tid else None
        if th is not None and th.archived:
            th = None  # B48: зеркало войс-пути — задача не аппендится в убранный тред
        if th is None:
            th = threads.create(title=text)
        threads.append_task(th.id, task_id)
        kora_runner.start(task_id, text,
                          RunSpec(thread_id=th.id, project_root=_resolve_project_root(th)))

    if kora_runner is not None:
        http_bridge.on_task_committed = _http_task_committed
    http_handlers = ToolHandlers(http_bridge, journal)

    text_loop = None
    if cfg.anthropic_api_key:
        from synapse.dispatcher.llm_client import AnthropicLLMClient
        from synapse.dispatcher.loop import DispatcherTurnLoop
        text_loop = DispatcherTurnLoop(
            AnthropicLLMClient(cfg.anthropic_api_key, cfg.tier2_model),
            http_handlers, confirm_flow, store, journal, clock, cfg,
            thread_feed_reader=threads.read_feed,
            stage_block_for=_stage_block_for,
            on_compact=_on_compact,
            owner_thread_for=lambda task_id: (
                th.id if (th := threads.thread_for_task(task_id)) else None
            ),
            on_user_turn=(
                lambda tid, tr, now: approvals.note_user_turn(tid, tr, now)
                if approvals is not None else None
            ),
        )

    # breaker needs only the tier COUNT, not the service instances themselves -- those are
    # per-connection (build_session_pipeline rebuilds them fresh every reconnect, since a
    # pipecat FrameProcessor instance belongs to exactly one PipelineRunner run). This pair is
    # discarded immediately after counting.
    _tier_probe, _ = build_tier_services(cfg)
    breaker = CircuitBreaker(len(_tier_probe), cfg.rpm_mute_s, cfg.rpd_reset_hour_utc)
    cost_cap = CostCap(cfg.max_paid_calls_per_day, cfg.rpd_reset_hour_utc)

    host = SynapseHost(
        clock=clock,
        cfg=cfg,
        journal=journal,
        store=store,
        speak_ledger=speak_ledger,
        classifier=classifier,
        confirm_flow=confirm_flow,
        arbiter_policy=arbiter_policy,
        bridge=bridge,
        handlers=handlers,
        breaker=breaker,
        cost_cap=cost_cap,
        approvals=approvals,
        kora_runner=kora_runner,
        kora_log=kora_log,
        threads=threads,
        voice_thread=voice_thread,
        voice_project=voice_project,
        projects=projects,
        text_loop=text_loop,
        turn_lock=turn_lock,
        current_http_thread=current_http_thread,
    )
    # Kept on the long-lived host so the independently constructed voice session can refresh
    # its system prompt from the same stage resolver as the HTTP DispatcherTurnLoop.
    host.stage_block_for = _stage_block_for
    # С2: HTTP-канал зовёт handlers.end_turn() рядом с journal.end_turn() — нужно держать
    # http_handlers на хосте (раньше это была только локальная переменная build_host).
    host.http_handlers = http_handlers
    # С1: голосовой путь собирает контекст хода через ту же фабрику, что HTTP DispatcherTurnLoop.
    # Резолвер owner_thread_for — тот же, что у text_loop (скоуп терминальной задачи к треду).
    # `task_dictionary` де-факто пустой (см. Parking lot в спеке) — передаём как есть, честно.
    from synapse.dispatcher.turn_context import build_turn_context
    _owner_thread_for = lambda task_id: (
        th.id if (th := threads.thread_for_task(task_id)) else None
    )
    host.turn_context_for = lambda tid: build_turn_context(
        cfg=cfg, store=store, clock=clock, thread_id=tid,
        stage_block_for=_stage_block_for, owner_thread_for=_owner_thread_for,
    )
    # Play-озвучка ленты (tero 2026-07-14): кэш реалтайм-аудио на диск, ключ по содержимому.
    # Пост-хок атрибут на долгоживущем хосте — тот же приём, что host.turn_context_for выше.
    host.tts_cache = TTSCache(
        Path(cfg.journal_dir) / "tts_cache",
        model=cfg.fish_tts_model,
        voice=cfg.fish_reference_id or "",
    )
    _h["host"] = host  # fills the on_speak holder -- must precede any runtime SPEAK
    return host


class SynapseSession:
    """Bundles the assembled per-connection pipeline with the collaborators `run()` (and tests)
    need direct handles to — the pipeline itself only sees frames, never these objects. Built
    fresh by `build_session_pipeline()` on every WebRTC connection; wired to the long-lived
    `SynapseHost` by reference."""

    def __init__(self, pipeline: Pipeline, llm_switcher: LLMSwitcher, generation_guard: GenerationGuard,
                 flush_voice_feed: Any = None, observers=None) -> None:
        self.pipeline = pipeline
        self.llm_switcher = llm_switcher
        self.generation_guard = generation_guard
        # Gate v2 D3': колбэк «дофлашить context-diff в ленту войс-треда» — webrtc_server зовёт
        # его в on_client_disconnected (последний ответ звонка). None → фича не проведена (стабы).
        self.flush_voice_feed = flush_voice_feed
        # TTS-кэш (tero 2026-07-14): обсерверы для PipelineTask — тап на выходе TTS пишет
        # реалтайм-аудио в кэш. Пусто → кэша нет (стабы/тесты без host.tts_cache).
        self.observers = list(observers or [])


def build_session_pipeline(host: SynapseHost) -> SynapseSession:
    """Builds the transport-agnostic per-connection processing chain, wired to `host`'s
    long-lived state by reference. Safe to call again on every WebRTC reconnect -- each call
    returns fresh FrameProcessor instances (pipecat constraint: an instance is good for exactly
    one PipelineRunner run, so these can never be shared across connections like the host is)."""
    services, labels = build_tier_services(host.cfg)
    generation_guard = GenerationGuard()
    strategy_type = build_strategy_type(host.breaker, labels, host.cost_cap, generation_guard, host.clock)
    # B04: cost-counting switcher — increments the daily paid-call cap on a successful generation,
    # not only on the failover error path (which never fired for a healthy tier1, the common case).
    llm_switcher = _CostCountingLLMSwitcher(
        services, strategy_type=strategy_type,
        cost_cap=host.cost_cap, labels=labels, clock=host.clock,
    )
    register_all(llm_switcher, host.handlers)

    # Cascade observability (Bug 3): the strategy exposes on_retry/on_tail_tier/on_all_failed
    # so this module owns the journal wiring without strategy.py depending on the journal.
    strategy = llm_switcher.strategy

    @strategy.event_handler("on_retry")
    async def _journal_retry(_strategy, next_idx):
        if host.journal.current is not None:
            host.journal.current.retry = True

    @strategy.event_handler("on_tail_tier")
    async def _journal_tail_tier(_strategy):
        host.journal.alert(AlertKind.TAIL_TIER_ENTRY)

    @strategy.event_handler("on_all_failed")
    async def _journal_all_failed(_strategy, reason=None):
        host.journal.alert(
            AlertKind.COST_CAP if reason == "cost_cap" else AlertKind.ALL_TIERS_FAILED,
            {"reason": reason},
        )

    # B-CORE-17: model= депрекейтнут pipecat 0.0.105 → settings=…Settings(model=…)
    stt = DeepgramFluxSTTService(
        api_key=host.cfg.deepgram_api_key or "",
        settings=DeepgramFluxSTTService.Settings(model="flux-general-multi"),
    )

    @stt.event_handler("on_end_of_turn")
    async def _on_end_of_turn(service, transcript: str) -> None:
        # B13: the voice path must OPEN a journal turn like the console path does — otherwise
        # `handlers._current_turn_id` stays None and the R1 dedup latch is DEAD in voice (an
        # intra-turn cascade retry re-executes a mutating tool, incl. a destructive confirm_task),
        # and `record_tool_call` no-ops so the tool audit is empty. (Closing the turn with
        # check_grounding/end_turn — capturing the assistant text + turn end in the frame flow —
        # is the remaining grounding-wiring work, needs live-mic verification.)
        # UI-3 (S7): очередь ходов — HTTP-ход (POST message) не начнётся посреди открытия
        # голосового. Residual (Parking lot): хвост голосового хода (tool-вызовы в
        # pipecat-потоке) живёт после отпуска лока — полная сериализация = pipecat-хирургия.
        async with host.turn_lock:
            record = host.journal.begin_turn(transcript)
            host.handlers.begin_turn(record.turn_id)
            # R3: every user turn must reach confirm_flow.note_user_turn() before the LLM runs.
            host.confirm_flow.note_user_turn(transcript, host.clock.now())
            # С3: fan-out на ApprovalService — тот же user turn кормит и confirm-flow, и
            # approval-flow (gate_action). Ключится по голосовому треду.
            if host.approvals is not None:
                host.approvals.note_user_turn(host.voice_thread["id"], transcript, host.clock.now())
            # UI-4: voice uses pipecat's own live context, not DispatcherTurnLoop. Refresh the
            # single system item in place before the user aggregator starts generation, keeping
            # its accumulated assistant/tool tail intact.
            # С1: голос собирает system_message через ту же фабрику turn_context_for, что HTTP —
            # теперь [СОСТОЯНИЕ] (с awaiting_answer, hide-скоупом) виден и голосу. Раньше голос
            # вообще не видел состояния, роутинг answer_kora держался на догадке модели.
            voice_system = host.turn_context_for(host.voice_thread["id"]).system_message
            context.set_messages([
                {"role": "system", "content": voice_system},
                *(m for m in context.get_messages() if m.get("role") != "system"),
            ])
        # Gate v2 D1'/D3': реплики звонка → лента треда (континуитет live→чат). Всё ВНЕ
        # turn_lock-секции (MINOR lock-скоуп): создание треда и append-ы не держат очередь ходов.
        if host.threads is not None:
            # D3' ПЕРЕД записью текущего transcript: диффом ловится ответ ПРЕДЫДУЩЕГО хода.
            _flush_voice_context()
            tid = host.voice_thread["id"]
            if tid is None or host.threads.get(tid) is None:
                # D1' (alt-MAJOR): EAGER-создание треда на первой голосовой реплике — буферов
                # нет, транскрипты не теряются никогда. Паттерн _on_task_committed: проект из
                # voice_project, битый/удалённый тихо деградирует в «без проекта».
                pid = host.voice_project["id"]
                th = host.threads.create(
                    title="новый тред",
                    project_id=pid if pid and host.projects is not None and host.projects.get(pid) else None,
                )
                host.voice_thread["id"] = th.id
                tid = th.id
            host.threads.maybe_autotitle(tid, transcript)
            host.threads.append_feed(tid, {"ts": host.clock.now(), "kind": "user", "text": transcript})
            if host.text_loop is not None:
                # D4' (sec-5): тёплая LLM-история треда видит войс-реплику без рестарта.
                host.text_loop.note_external_turn(tid, "user", transcript)

    context = LLMContext(tools=ALL_SCHEMAS)

    # B44: (ре)коннект В УЖЕ РАЗГОВОРЕННЫЙ тред обязан продолжать разговор, а не начинать с
    # амнезии — лента на экране показывает историю, значит и диспетчер обязан её помнить.
    # Сидим свежий per-connection контекст из ленты треда той же функцией, что HTTP-путь
    # (dispatcher/loop.py::history_from_feed): один резолвер «feed → history» на оба канала,
    # NO-EXFIL соблюдён по построению (в history идут только kind user/assistant).
    _seed_tid = host.voice_thread["id"]
    if host.threads is not None and _seed_tid is not None and host.threads.get(_seed_tid) is not None:
        from synapse.dispatcher.loop import history_from_feed
        for _m in history_from_feed(host.threads.read_feed(_seed_tid)):
            context.add_message(_m)

    # Gate v2 D3': context-diff флашер — сказанные ответы диспетчера идут в ленту войс-треда.
    # Курсор двигается по ВСЕМ сообщениям context.get_messages(); в ленту из диффа пишутся
    # ТОЛЬКО assistant со строковым контентом: user-транскрипт пишет D1' напрямую из параметра
    # (не дублируем), role tool/system отфильтрованы. Aggregator сидит downstream TTS →
    # контекст = реально СКАЗАННОЕ (интеррапты уже обработаны pipecat). Зовётся из
    # _on_end_of_turn (ответ предыдущего хода), on_client_disconnected (последний ответ) и —
    # B25 — из guarded-агрегатора СРАЗУ по коммиту ответа (on_commit), чтобы ответ появлялся в
    # ленте в тот же момент, а не ходом позже. Курсор делает все три вызова идемпотентными.
    # NO-EXFIL не задет: в ленту идут только слова диспетчера, кора-виды в контексте не живут.
    # Старт курсора = длина УЖЕ засеянного контекста (B44): регидрированные из ленты ответы
    # уже В ленте — флашить их обратно значило бы задублировать каждую реплику на реконнекте.
    _voice_cursor = {"n": len(context.get_messages())}

    def _flush_voice_context() -> None:
        msgs = context.get_messages()
        fresh = msgs[_voice_cursor["n"]:]
        _voice_cursor["n"] = len(msgs)
        tid = host.voice_thread["id"]
        committed_text = ""
        if host.threads is not None and tid is not None:
            for m in fresh:
                if m.get("role") != "assistant":
                    continue
                content = m.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue
                committed_text = content
                host.threads.append_feed(tid, {"ts": host.clock.now(), "kind": "assistant", "text": content})
                if host.text_loop is not None:
                    host.text_loop.note_external_turn(tid, "assistant", content)
        # С2 (Ф0.2): голос закрывает ход в момент коммита ответа (on_commit) — то самое
        # «remaining grounding-wiring work» из B13-коммента. Раньше голос НИКОГДА не закрывал
        # ход: journal.end_turn() на happy-path звали только консоль и exception-путь, а B08-бэкстоп
        # превращал это в слияние — ВСЕ войс/HTTP-ходы процесса писутся в одну вечно открытую
        # запись, turn_id не рос. Теперь on_commit (ответ зафиксирован) → check_grounding (голос
        # наконец получает grounding-проверку, как консольный) + end_turn. Идемпотентность: end_turn
        # no-op когда _current уже None (повторные on_commit / _flush_voice_final после закрытия).
        # committed_text — последний зафлашенный assistant-контент: это и есть llm_output хода.
        record = host.journal.current
        if record is not None:
            if committed_text:
                record.llm_output = committed_text
            host.journal.check_grounding(record, host.store.has_active_task())
            host.journal.end_turn()
            host.handlers.end_turn()  # С2: сброс _last_turn_id (anti-misattribution)
    # S1: replicate LLMContextAggregatorPair's own __init__ recipe (user first, then the
    # assistant with a back-reference to it) so the assistant half can be the guarded
    # subclass from make_guarded_assistant_aggregator -- LLMContextAggregatorPair itself
    # always builds a plain LLMAssistantAggregator internally with no hook to substitute a
    # subclass.
    user_aggregator = LLMUserAggregator(context)
    GuardedAssistantAggregator = make_guarded_assistant_aggregator(
        LLMAssistantAggregator, generation_guard, on_commit=_flush_voice_context
    )
    assistant_aggregator = GuardedAssistantAggregator(context, _paired_user_aggregator=user_aggregator)

    # B42: teardown-флаш («Завершить — в чат» / hangup). Ответ, оборванный ПОСРЕДИ речи, в
    # context.messages не попадает никогда: pipecat коммитит агрегацию только на
    # LLMFullResponseEndFrame, а CancelFrame (_handle_end_or_cancel) её молча бросает. При этом
    # всё, что лежит в _aggregation, УЖЕ прозвучало — агрегатор сидит downstream TTS. Поэтому
    # только на teardown (не на commit-флашах: там недокоммиченный хвост — это ещё живой
    # стриминг) дренируем pending-хвост в ленту, чтобы лента держала ровно то, что юзер слышал.
    def _flush_voice_final() -> None:
        _flush_voice_context()
        pending = assistant_aggregator.aggregation_string() if assistant_aggregator._aggregation else ""
        assistant_aggregator._aggregation = []  # идемпотентность повторного teardown-вызова
        tid = host.voice_thread["id"]
        if host.threads is None or tid is None or not pending.strip():
            return
        host.threads.append_feed(tid, {"ts": host.clock.now(), "kind": "assistant", "text": pending})
        if host.text_loop is not None:
            host.text_loop.note_external_turn(tid, "assistant", pending)

    # Two GenerationStartHooks, not one, around llm_switcher (research §2.2): a user turn's
    # LLMContextFrame travels DOWNSTREAM out of user_aggregator, but a tool-call's
    # re-inference travels UPSTREAM out of assistant_aggregator instead -- LLM services
    # terminate LLMContextFrame rather than re-pushing it, so a switcher itself never relays
    # one either way. A DOWNSTREAM-only hook would miss every tool-loop turn, leaving
    # `current_generation` stale for it and letting a stale scrub cut off already-committed
    # tool-messages on the next error.
    pre_hook = GenerationStartHook(generation_guard, FrameDirection.DOWNSTREAM)
    post_hook = GenerationStartHook(generation_guard, FrameDirection.UPSTREAM)

    tts = FishAudioTTSService(
        api_key=host.cfg.fish_audio_api_key or "",
        settings=FishAudioTTSService.Settings(model=host.cfg.fish_tts_model, voice=host.cfg.fish_reference_id),
    )

    # TTS-кэш (tero 2026-07-14): тап на выходе tts пишет реалтайм-аудио в кэш хоста. Нет
    # host.tts_cache (стабы) → без обсервера, пайплайн неизменен.
    cache = getattr(host, "tts_cache", None)
    observers = [TTSCacheObserver(cache, tts)] if cache is not None else []

    arbiter = TTSArbiterProcessor(host.arbiter_policy)

    # assistant_aggregator MUST sit AFTER tts, not before it: LLMAssistantAggregator.process_frame
    # terminates TextFrame/LLMTextFrame/LLMFullResponse* (it consumes them to build context and
    # does NOT push them onward), so placed upstream of TTS it swallows the LLM's spoken text
    # before arbiter/tts ever see it -> run_tts never fires -> silence. pipecat still forwards
    # what the aggregator needs from downstream of TTS: TTSTextFrame (a TextFrame subclass) via
    # push_text_frames (default True) rebuilds context from the spoken words, and
    # LLMFullResponseEndFrame (also forwarded by default) still fires the commit/tool-loop
    # re-inference trigger. This is pipecat's own canonical order (ref E6/E7 in run file).
    pipeline = Pipeline(
        [
            stt,
            user_aggregator,
            pre_hook,
            llm_switcher,
            post_hook,
            arbiter,
            tts,
            assistant_aggregator,
        ]
    )

    return SynapseSession(pipeline=pipeline, llm_switcher=llm_switcher, generation_guard=generation_guard,
                          flush_voice_feed=_flush_voice_final, observers=observers)


def run() -> None:
    """`python -m synapse.pipeline.app` — boots the WebRTC demo server. The agent is now served
    over pipecat SmallWebRTCTransport (see synapse.pipeline.webrtc_server), so the browser's own
    WebRTC stack does acoustic echo cancellation and the old LocalAudioTransport (raw PortAudio,
    no AEC, echo-loop) is gone. uvicorn/webrtc deps are lazy-imported here (S4) so importing this
    module stays free of the `voice` extra."""
    import uvicorn
    from dotenv import load_dotenv

    from synapse.pipeline.webrtc_server import build_web_app

    load_dotenv()
    cfg = SynapseConfig.from_env()
    host = build_host(cfg)
    app = build_web_app(host)
    uvicorn.run(app, host="localhost", port=7860)


if __name__ == "__main__":
    run()
