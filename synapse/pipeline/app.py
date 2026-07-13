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
import logging
from collections import deque
from typing import Any

from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.llm_switcher import LLMSwitcher
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMAssistantAggregator, LLMUserAggregator
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.services.fish.tts import FishAudioTTSService

from pathlib import Path

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
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
from synapse.threads import ThreadStore

logger = logging.getLogger(__name__)


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
        kora_runner: KoraRunner | None = None,
        kora_log: deque | None = None,
        threads: Any = None,
        voice_thread: dict | None = None,
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
        # M1 slice 2 (the one NON-long-lived field, see class docstring): the currently live
        # per-connection PipelineTask, or None when no client is connected.
        self._output_task: Any = None

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

    async def push_speak_frame(self, text: str) -> None:
        """Inject Kora's SPEAK straight into the running output task, out-of-band (no input
        frame needed). Re-checks liveness: the task may have finished between `speak()`
        scheduling this and it running. `queue_frame` on a finished task is a SILENT DROP
        (worker.py: an unbounded put never raises/blocks, the drain task is gone), so the
        `has_finished()` guard is what actually prevents a lost-in-the-void SPEAK."""
        t = self._output_task
        if t is not None and not t.has_finished():
            await t.queue_frame(TTSSpeakFrame(text=text, append_to_context=False))

    def speak(self, text: str) -> None:
        """SPEAK entry point (called by on_speak). Registers the ledger ALWAYS (Р-15г: a
        critical that DID get its SPEAK stops counting as an unpaired-critical alert), then:
        - live output task + running loop -> schedule an out-of-band TTSSpeakFrame injection.
          Does NOT also call arbiter_policy.push_speak (that would double the utterance -- the
          injected frame itself travels through the arbiter downstream).
        - live output task but NO running loop (e.g. a sync test path) -> arbiter fallback.
        - no live output task -> arbiter fallback (frame-driven, drained when a frame flows)."""
        self.speak_ledger.register_speak_text(text, self.clock.now())
        t = self._output_task
        if t is not None and not t.has_finished():
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                self.arbiter_policy.push_speak(text)
                return
            fut = asyncio.ensure_future(self.push_speak_frame(text))
            # B9: without a done-callback, a raise inside push_speak_frame (queue_frame on a task
            # torn down mid-emit) is never retrieved AND the ledger was already marked spoken
            # (line above) — so the dropped critical produces neither audio nor a
            # CRITICAL_WITHOUT_SPEAK alert. Surface the failure instead of swallowing it.
            fut.add_done_callback(self._on_speak_frame_done)
        else:
            self.arbiter_policy.push_speak(text)

    def _on_speak_frame_done(self, fut: "asyncio.Future[Any]") -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            logger.warning("push_speak_frame injection failed; SPEAK dropped: %r", exc)

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
    kora_runner = (
        KoraRunner(cfg, store, speak_ledger, clock, journal, on_speak,
                   log_sink=kora_log.append, on_run_finished=threads.set_outcome)
        if cfg.kora_enabled
        else None
    )

    def _on_task_committed(task_id: str, text: str) -> None:
        th = threads.get(voice_thread["id"]) if voice_thread["id"] else None
        if th is None:
            th = threads.create(title=text)
            voice_thread["id"] = th.id
        threads.append_task(th.id, task_id)
        kora_runner.start(task_id, text, RunSpec(thread_id=th.id, project_root=None))

    bridge = KoraBridge(
        store=store,
        confirm_flow=confirm_flow,
        clock=clock,
        on_speak=on_speak,
        cfg=cfg,
        on_task_committed=_on_task_committed if kora_runner else None,
        on_cancel=kora_runner.request_cancel if kora_runner else None,
        on_answer=kora_runner.provide_answer if kora_runner else None,
    )
    handlers = ToolHandlers(bridge, journal)

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
        kora_runner=kora_runner,
        kora_log=kora_log,
        threads=threads,
        voice_thread=voice_thread,
    )
    _h["host"] = host  # fills the on_speak holder -- must precede any runtime SPEAK
    return host


class SynapseSession:
    """Bundles the assembled per-connection pipeline with the collaborators `run()` (and tests)
    need direct handles to — the pipeline itself only sees frames, never these objects. Built
    fresh by `build_session_pipeline()` on every WebRTC connection; wired to the long-lived
    `SynapseHost` by reference."""

    def __init__(self, pipeline: Pipeline, llm_switcher: LLMSwitcher, generation_guard: GenerationGuard) -> None:
        self.pipeline = pipeline
        self.llm_switcher = llm_switcher
        self.generation_guard = generation_guard


def build_session_pipeline(host: SynapseHost) -> SynapseSession:
    """Builds the transport-agnostic per-connection processing chain, wired to `host`'s
    long-lived state by reference. Safe to call again on every WebRTC reconnect -- each call
    returns fresh FrameProcessor instances (pipecat constraint: an instance is good for exactly
    one PipelineRunner run, so these can never be shared across connections like the host is)."""
    services, labels = build_tier_services(host.cfg)
    generation_guard = GenerationGuard()
    strategy_type = build_strategy_type(host.breaker, labels, host.cost_cap, generation_guard, host.clock)
    llm_switcher = LLMSwitcher(services, strategy_type=strategy_type)
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

    stt = DeepgramFluxSTTService(api_key=host.cfg.deepgram_api_key or "", model="flux-general-multi")

    @stt.event_handler("on_end_of_turn")
    async def _on_end_of_turn(service, transcript: str) -> None:
        # B13: the voice path must OPEN a journal turn like the console path does — otherwise
        # `handlers._current_turn_id` stays None and the R1 dedup latch is DEAD in voice (an
        # intra-turn cascade retry re-executes a mutating tool, incl. a destructive confirm_task),
        # and `record_tool_call` no-ops so the tool audit is empty. (Closing the turn with
        # check_grounding/end_turn — capturing the assistant text + turn end in the frame flow —
        # is the remaining grounding-wiring work, needs live-mic verification.)
        record = host.journal.begin_turn(transcript)
        host.handlers.begin_turn(record.turn_id)
        # R3: every user turn must reach confirm_flow.note_user_turn() before the LLM runs.
        host.confirm_flow.note_user_turn(transcript, host.clock.now())

    context = LLMContext(tools=ALL_SCHEMAS)
    # S1: replicate LLMContextAggregatorPair's own __init__ recipe (user first, then the
    # assistant with a back-reference to it) so the assistant half can be the guarded
    # subclass from make_guarded_assistant_aggregator -- LLMContextAggregatorPair itself
    # always builds a plain LLMAssistantAggregator internally with no hook to substitute a
    # subclass.
    user_aggregator = LLMUserAggregator(context)
    GuardedAssistantAggregator = make_guarded_assistant_aggregator(LLMAssistantAggregator, generation_guard)
    assistant_aggregator = GuardedAssistantAggregator(context, _paired_user_aggregator=user_aggregator)

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

    return SynapseSession(pipeline=pipeline, llm_switcher=llm_switcher, generation_guard=generation_guard)


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
