"""The dispatcher's four tools (§4): submit_task, confirm_task, get_task_status,
request_cancel. `ToolHandlers` holds the pure async implementations shared by both the
console/MockLLM path (via DispatcherTurnLoop) and the real pipecat path (via `register_all`,
wrapped for `FunctionCallParams`) — journaling and the SPEAK path live inside the handlers so
both paths behave identically.

R1 (dedup latch): an intra-turn cascade retry regenerates the assistant turn and may
re-invoke the same mutating tool call. `ToolHandlers` keeps a per-turn latch for the
mutating tools (submit_task/confirm_task/request_cancel) — a repeat within the same turn is
a no-op that returns the first call's result instead of re-executing.
"""
from __future__ import annotations

import contextvars
import inspect
from dataclasses import dataclass
from typing import Any, Callable

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams

from synapse.bridge.confirm import (
    ConfirmDecisionOutcome,
    ConfirmFlow,
    ConfirmOutcome,
    ConfirmResult,
    SubmitResult,
)
from synapse.bridge.state import TaskStore, should_hide_task
from synapse.clock import Clock
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal
from synapse.prompt import PERSONA_PRESETS

SUBMIT_TASK_SCHEMA = FunctionSchema(
    name="submit_task",
    description=(
        "Передать новую задачу Коре. Для необратимых задач мост сначала запросит голосовое "
        "подтверждение — Коре она уйдёт только после него."
    ),
    properties={"text": {"type": "string", "description": "Текст задачи, как её сформулировал пользователь."}},
    required=["text"],
)

CONFIRM_TASK_SCHEMA = FunctionSchema(
    name="confirm_task",
    description="Завершить подтверждение необратимой задачи после того, как пользователь ответил на зачитку.",
    properties={
        "decision": {
            "type": "string",
            "enum": ["confirm", "deny"],
            "description": "confirm — пользователь подтвердил; deny — пользователь отказался.",
        }
    },
    required=["decision"],
)

GET_TASK_STATUS_SCHEMA = FunctionSchema(
    name="get_task_status",
    description=(
        "Получить свежий снимок состояния активной задачи. Обязателен перед обычным ответом "
        "о ходе или результатах задачи. Исключение: если пользователь приписывает диспетчеру "
        "слова или результат, которых тот не сообщал, не вызывай инструмент — сначала явно "
        "поправь пользователя по обязательному confab-шаблону из system message."
    ),
    properties={},
    required=[],
)

REQUEST_CANCEL_SCHEMA = FunctionSchema(
    name="request_cancel",
    description="Передать Коре запрос на отмену текущей задачи.",
    properties={},
    required=[],
)

ANSWER_KORA_SCHEMA = FunctionSchema(
    name="answer_kora",
    description=(
        "Передать Коре ответ пользователя на её уточняющий вопрос — дословно, без переписывания. "
        "Использовать только когда [СОСТОЯНИЕ] показывает, что Кора ждёт ответа. Для "
        "[ЗАПРОС КОРЫ] собери свод по [ФОРМАТ ОТВЕТА], зачитай его и после явного подтверждения "
        "повторно передай тот же свод; request_id выбирает хост."
    ),
    properties={
        "text": {
            "type": "string",
            "description": "Реплика пользователя дословно либо подтверждённый свод для schema-1.",
        }
    },
    required=["text"],
)

CONSULT_KORA_SCHEMA = FunctionSchema(
    name="consult_kora",
    description=(
        "Попросить Кору прочитать проект и обсудить идею в read-only режиме. Первый вызов "
        "запускает консилиум; следующий передаёт новый бриф той же ожидающей Коре. "
        "Доступно в любой стадии разговора; стадия рана не меняется."
    ),
    properties={
        "briefing": {
            "type": "string",
            "description": "Контекст разговора, уже принятые решения и конкретный вопрос Коре.",
        }
    },
    required=["briefing"],
)

PROPOSE_REQUEST_SCHEMA = FunctionSchema(
    name="propose_request",
    description="Сохранить согласованный свод запроса и показать пользователю карточку следующего шага.",
    properties={"text": {"type": "string", "description": "Короткий согласованный свод задачи."}},
    required=["text"],
)

GATE_ACTION_SCHEMA = FunctionSchema(
    name="gate_action",
    description="Выполнить разрешённое действие стадийной карточки после явного подтверждения пользователя.",
    properties={
        "action": {"type": "string", "enum": ["send_to_kora", "write_code", "revise"]},
        "model": {"type": "string", "description": "Необязательная модель запуска."},
        "confirm": {"type": "boolean", "description": "Явное подтверждение опасного запуска кода."},
        "fast": {"type": "boolean", "description": "Быстрый путь сразу к коду."},
    },
    required=["action"],
)

BIND_PROJECT_SCHEMA = FunctionSchema(
    name="bind_project",
    description="Привязать текущий тред к проекту по его имени, пока задача ещё не запускалась.",
    properties={"project_name": {"type": "string", "description": "Имя проекта из списка проектов."}},
    required=["project_name"],
)

SET_PERSONA_SCHEMA = FunctionSchema(
    name="set_persona",
    description=(
        "Сменить персону диспетчера для текущего треда. Меняет только стиль и фокус; "
        "правила и возможности не меняются. Невалидное имя возвращает каталог."
    ),
    properties={"name": {"type": "string", "description": "Имя персоны из каталога пресетов."}},
    required=["name"],
)

ALL_SCHEMAS = [
    SUBMIT_TASK_SCHEMA,
    CONFIRM_TASK_SCHEMA,
    GET_TASK_STATUS_SCHEMA,
    REQUEST_CANCEL_SCHEMA,
    ANSWER_KORA_SCHEMA,
    CONSULT_KORA_SCHEMA,
    PROPOSE_REQUEST_SCHEMA,
    GATE_ACTION_SCHEMA,
    BIND_PROJECT_SCHEMA,
    SET_PERSONA_SCHEMA,
]


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    # UI-3: id вызова из LLM-ответа — нужен для канонического tool_use/tool_result-шейпа
    # истории (Anthropic Messages API); мок-пути оставляют "".
    id: str = ""


@dataclass
class KoraBridge:
    """Thin facade bundling the collaborators the dispatcher tools need — not a new
    abstraction, just named plumbing so `make_handlers`/`ToolHandlers` takes one object."""

    store: TaskStore
    confirm_flow: ConfirmFlow
    clock: Clock
    cfg: SynapseConfig
    on_speak: Callable[[str], None] | None = None
    # M1 slice 1: a task entering RUNNING (submit-COMMITTED or confirm-COMMITTED) launches the
    # real Kora producer; a request_cancel that actually flips the store fires on_cancel so the
    # runner tears down Kora's subprocess, not just the slot. Both fire INSIDE ToolHandlers._do.
    on_task_committed: Callable[[str, str], None] | None = None
    on_cancel: Callable[[], None] | None = None
    # M1 slice 3 (E5): deliver the user's reply to a parked AskUserQuestion, verbatim. Wired to
    # KoraRunner.provide_answer; returns True iff a question was actually pending. Fires INSIDE
    # ToolHandlers._do (answer_kora), like the other on_* callbacks.
    on_answer: Callable[[str], Any] | None = None
    on_consult: Callable[[str], Any] | None = None
    # UI-4: staging tools are deliberately callbacks, so voice and HTTP select their own
    # current thread without sharing mutable routing state.
    on_propose: Callable[[str], Any] | None = None
    on_gate: Callable[..., Any] | None = None
    on_bind: Callable[[str], Any] | None = None
    # bind_project resolves names here (not in the LLM) and uses these stores only after the
    # bridge callback confirms this dispatcher channel has an active thread.
    projects: Any = None
    threads: Any = None
    thread_id_for: Callable[[], str | None] | None = None
    # B-BRIDGE-6: имя канала этого моста ("voice"/"http") — сентинел разговора, когда треда ещё
    # нет. У голоса это норма: авто-тред рождается в on_task_committed, то есть ПОСЛЕ подтверждения,
    # так что необратимая задача СТАВИТСЯ при thread_id_for() == None. Скоупить её в None значило бы
    # «подтвердить некому» — голосовой confirm умер бы совсем. Канал же — честный разговор: голос
    # один, а HTTP-треды различаются своими id.
    channel: str = "voice"

    def confirm_scope(self) -> str:
        """Разговор для ConfirmFlow: тред, если он есть, иначе канал. Одна точка на submit и
        confirm — оба ключа обязаны считать скоуп одинаково, иначе задачу нельзя подтвердить."""
        tid = self.thread_id_for() if self.thread_id_for is not None else None
        return tid or self.channel


def _submit_result_to_dict(res: SubmitResult) -> dict[str, Any]:
    return {
        "outcome": res.outcome.value,
        "task_id": res.task_id,
        "readback_text": res.readback_text,
        "reject_text": res.reject_text,
    }


def _confirm_result_to_dict(res: ConfirmResult) -> dict[str, Any]:
    return {"outcome": res.outcome.value, "text": res.text, "task_id": res.task_id}


@dataclass
class _DedupEntry:
    turn_id: str
    result: dict[str, Any]
    # B14: the dedup latch exists for an intra-turn cascade RETRY that re-issues the SAME call.
    # Keying on tool name alone silently collapsed two DIFFERENT same-name calls in one turn
    # (submit "A" then submit "B" → B returned A's result). Include the arguments in the match.
    args: dict[str, Any]


class ToolHandlers:
    def __init__(self, bridge: KoraBridge, journal: TurnJournal) -> None:
        self.bridge = bridge
        self._journal = journal
        # Keyed by turn_id -> {tool_name: _DedupEntry}
        self._dedup: dict[str, dict[str, _DedupEntry]] = {}
        self._current_turn_id_var = contextvars.ContextVar("current_turn_id", default=None)
        # B13: begin_turn on the voice path runs inside a pipecat event-handler task, whose
        # ContextVar mutation does NOT propagate back to the context that later reads the latch
        # (the STT handler's task copies the context; `asyncio.run` in the test does the same).
        # A ContextVar alone (needed for concurrent-HTTP-turn isolation) leaves the voice latch
        # inert. `_last_turn_id` is a plain attribute fallback: it survives the child-context exit
        # and is unambiguous where it's consulted, because the fallback is ONLY reached when the
        # reading context never set its own turn id — which is the single-flow voice case, not the
        # concurrent-HTTP case (there every ingest_user_turn calls begin_turn in its own task, so
        # the ContextVar wins and the shared attribute is never read).
        self._last_turn_id: str | None = None

    @property
    def _current_turn_id(self) -> str | None:
        return self._current_turn_id_var.get() or self._last_turn_id

    @_current_turn_id.setter
    def _current_turn_id(self, val: str | None) -> None:
        self._current_turn_id_var.set(val)

    def begin_turn(self, turn_id: str) -> None:
        self._current_turn_id = turn_id
        self._last_turn_id = turn_id  # B13: cross-context fallback (see __init__)
        self._dedup[turn_id] = {}
        if len(self._dedup) > 64:
            oldest_key = next(iter(self._dedup))
            self._dedup.pop(oldest_key, None)

    def end_turn(self) -> None:
        """С2 (Ф0.2): закрыть текущий dedup-slot и сбросить turn-id. Раньше глобальный fallback жил вечно —
        поздний tool-хвост (трейлинг tool-call pipecat-потока после конца хода) приписывался
        ЧУЖому ходу: _last_turn_id указывал на последний begin, а ContextVar в трейлинг-таске
        уже не несла своего id → добирался fallback. После end_turn fallback None, и поздний
        хвост получает честный turn_id="" (record_tool_call / _guarded уже это поддерживают).
        Полный OperationContext через command handler — Фаза 1; здесь минимум, убирающий
        misattribution. Хост зовёт это рядом с journal.end_turn()."""
        turn_id = self._current_turn_id
        self._current_turn_id = None
        if turn_id is not None:
            self._dedup.pop(turn_id, None)
        # Keep one empty tombstone so voice-path diagnostics can distinguish "a turn was
        # opened and closed" from "begin_turn was never wired". _guarded never reads or writes
        # this slot; anonymous callbacks always execute, so it cannot create a cross-turn hit.
        self._dedup["<anonymous>"] = {}
        # Do not erase another concurrently-started turn's fallback.
        if self._last_turn_id == turn_id:
            self._last_turn_id = None

    async def _guarded(
        self, name: str, args: dict[str, Any], fn: Callable[[], Any]
    ) -> tuple[dict[str, Any], bool]:
        turn_id = self._current_turn_id or ""
        # Dedup is valid only inside an attributable operation. A shared anonymous slot turns
        # two late callbacks from different turns into the same mutation and silently drops the
        # latter. With no turn id, prefer executing the handler; downstream mutation contracts
        # are idempotent/busy-guarded, while a false dedup hit cannot be recovered.
        if not turn_id:
            result = await fn()
            # Retain the last call only for diagnostics/cleanup assertions. This entry is never
            # consulted for a hit: every anonymous callback executes independently.
            self._dedup.setdefault("<anonymous>", {})[name] = _DedupEntry(
                turn_id="", result=result, args=args
            )
            return result, False
        turn_dedup = self._dedup.setdefault(turn_id, {})
        entry = turn_dedup.get(name)
        # B14: a dedup hit requires SAME turn AND SAME args
        if entry is not None and entry.args == args:
            return entry.result, True
        result = await fn()
        turn_dedup[name] = _DedupEntry(turn_id=turn_id, result=result, args=args)
        return result, False

    async def submit_task(self, text: str) -> dict[str, Any]:
        async def _do() -> dict[str, Any]:
            res = self.bridge.confirm_flow.submit(text, self.bridge.clock.now(),
                                                  thread_id=self.bridge.confirm_scope())
            speak_text = res.readback_text or res.reject_text
            if speak_text and self.bridge.on_speak:
                self.bridge.on_speak(speak_text)
            # A non-destructive submit commits straight to RUNNING → launch Kora now. (A
            # destructive one only STAGES here; its launch waits for confirm_task below.)
            if res.outcome == ConfirmOutcome.COMMITTED and self.bridge.on_task_committed and res.task_id:
                self.bridge.on_task_committed(res.task_id, text)
            return _submit_result_to_dict(res)

        result, deduped = await self._guarded("submit_task", {"text": text}, _do)
        self._journal.record_tool_call("submit_task", {"text": text}, {**result, "deduped": deduped})
        return result

    async def confirm_task(self, decision: str) -> dict[str, Any]:
        async def _do() -> dict[str, Any]:
            res = self.bridge.confirm_flow.confirm(decision, self.bridge.clock.now(),
                                                   thread_id=self.bridge.confirm_scope())
            if res.text and self.bridge.on_speak:
                self.bridge.on_speak(res.text)
            # A confirmed destructive task just flipped to RUNNING → launch Kora. Read the task
            # TEXT from the store (ConfirmResult.text is the SPEAK phrase, not the task text).
            if res.outcome == ConfirmDecisionOutcome.COMMITTED and self.bridge.on_task_committed:
                task = self.bridge.store.task
                if task is not None:
                    self.bridge.on_task_committed(task.id, task.text)
            return _confirm_result_to_dict(res)

        result, deduped = await self._guarded("confirm_task", {"decision": decision}, _do)
        self._journal.record_tool_call("confirm_task", {"decision": decision}, {**result, "deduped": deduped})
        return result

    async def get_task_status(self) -> dict[str, Any]:
        cfg = self.bridge.cfg
        # Скоуп терминальной задачи к её треду — зеркало loop._render_state: завершённая задача
        # чужого треда не должна отвечать «выполнено» на статус-вопрос из несвязанного треда.
        hide = False
        task = self.bridge.store.task
        if task is not None and self.bridge.threads is not None and self.bridge.thread_id_for is not None:
            owner = self.bridge.threads.thread_for_task(task.id)
            hide = should_hide_task(task, self.bridge.thread_id_for(), owner.id if owner else None)
        result = self.bridge.store.snapshot(
            self.bridge.clock.now(), cfg.stale_after_s, cfg.unreachable_after_s, hide_task=hide
        )
        self._journal.record_tool_call("get_task_status", {}, result)
        return result

    async def request_cancel(self) -> dict[str, Any]:
        async def _do() -> dict[str, Any]:
            ok = self.bridge.store.request_cancel()
            if ok and self.bridge.on_cancel:
                self.bridge.on_cancel()
            return {"outcome": "cancel_requested" if ok else "no_active_task"}

        result, deduped = await self._guarded("request_cancel", {}, _do)
        self._journal.record_tool_call("request_cancel", {}, {**result, "deduped": deduped})
        return result

    async def answer_kora(self, text: str) -> dict[str, Any]:
        async def _do() -> dict[str, Any]:
            value = (
                await self._callback(self.bridge.on_answer, text)
                if self.bridge.on_answer else False
            )
            if isinstance(value, dict):
                return value
            return {"outcome": "answer_delivered" if value else "no_pending_question"}

        result, deduped = await self._guarded("answer_kora", {"text": text}, _do)
        self._journal.record_tool_call("answer_kora", {"text": text}, {**result, "deduped": deduped})
        return result

    async def consult_kora(self, briefing: str) -> dict[str, Any]:
        async def _do() -> dict[str, Any]:
            if self.bridge.on_consult is None:
                return {"outcome": "dispatcher_unavailable"}
            value = await self._callback(self.bridge.on_consult, briefing)
            return value if isinstance(value, dict) else {"outcome": "consult_started"}

        result, deduped = await self._guarded("consult_kora", {"briefing": briefing}, _do)
        self._journal.record_tool_call(
            "consult_kora", {"briefing": briefing}, {**result, "deduped": deduped}
        )
        return result

    async def _callback(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        result = callback(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    async def propose_request(self, text: str) -> dict[str, Any]:
        async def _do() -> dict[str, Any]:
            if self.bridge.on_propose is None:
                return {"outcome": "dispatcher_unavailable"}
            result = await self._callback(self.bridge.on_propose, text)
            return result if isinstance(result, dict) else {"outcome": "proposed"}

        result, deduped = await self._guarded("propose_request", {"text": text}, _do)
        self._journal.record_tool_call("propose_request", {"text": text}, {**result, "deduped": deduped})
        return result

    async def gate_action(
        self, action: str, model: str | None = None, confirm: bool = False, fast: bool = False
    ) -> dict[str, Any]:
        async def _do() -> dict[str, Any]:
            if self.bridge.on_gate is None:
                return {"outcome": "dispatcher_unavailable"}
            result = await self._callback(
                self.bridge.on_gate, action, model=model, confirm=confirm, fast=fast
            )
            return result if isinstance(result, dict) else {"outcome": "gate_completed"}

        args = {"action": action, "model": model, "confirm": confirm, "fast": fast}
        result, deduped = await self._guarded("gate_action", args, _do)
        self._journal.record_tool_call("gate_action", args, {**result, "deduped": deduped})
        return result

    async def bind_project(self, project_name: str) -> dict[str, Any]:
        async def _do() -> dict[str, Any]:
            if self.bridge.on_bind is None:
                return {"outcome": "dispatcher_unavailable"}
            if self.bridge.projects is None or self.bridge.threads is None or self.bridge.thread_id_for is None:
                return {"outcome": "dispatcher_unavailable"}
            thread_id = self.bridge.thread_id_for()
            if thread_id is None:
                return {"outcome": "no_active_thread"}
            matches = [
                p for p in self.bridge.projects.list()
                if str(p.get("name", "")).casefold() == project_name.casefold()
            ]
            if not matches:
                return {"outcome": "unknown_project"}
            if len(matches) > 1:
                return {"outcome": "ambiguous_project"}
            project = matches[0]
            if not self.bridge.threads.bind_project(thread_id, project["id"]):
                return {"outcome": "project_bind_rejected"}
            callback_result = await self._callback(self.bridge.on_bind, project["id"])
            if isinstance(callback_result, dict):
                return callback_result
            return {"outcome": "project_bound", "project_id": project["id"]}

        result, deduped = await self._guarded("bind_project", {"project_name": project_name}, _do)
        self._journal.record_tool_call(
            "bind_project", {"project_name": project_name}, {**result, "deduped": deduped}
        )
        return result

    async def set_persona(self, name: str) -> dict[str, Any]:
        async def _do() -> dict[str, Any]:
            if self.bridge.threads is None or self.bridge.thread_id_for is None:
                return {"outcome": "dispatcher_unavailable"}
            thread_id = self.bridge.thread_id_for()
            if thread_id is None:
                return {"outcome": "no_active_thread"}
            wanted = name.strip().casefold()
            if wanted not in PERSONA_PRESETS:
                return {"outcome": "unknown_persona", "catalog": sorted(PERSONA_PRESETS)}
            if not self.bridge.threads.set_persona(thread_id, wanted):
                return {"outcome": "no_active_thread"}
            return {"outcome": "persona_set", "persona": wanted}

        result, deduped = await self._guarded("set_persona", {"name": name}, _do)
        self._journal.record_tool_call(
            "set_persona", {"name": name}, {**result, "deduped": deduped}
        )
        return result


def register_all(llm_or_switcher: Any, handlers: ToolHandlers) -> None:
    """Wraps each pure handler as a pipecat function-call callback (S7: the result is
    delivered ONLY via `params.result_callback`, never a Python return) and registers it.
    Mutating tools get `cancel_on_interruption=False` (S5) so barge-in can't silently drop
    an in-flight submit/confirm/cancel and desync the confirm state machine;
    get_task_status keeps pipecat's default (True) — re-asking on interruption is harmless.
    """

    async def _submit_task(params: FunctionCallParams) -> None:
        result = await handlers.submit_task(**params.arguments)
        await params.result_callback(result)

    async def _confirm_task(params: FunctionCallParams) -> None:
        result = await handlers.confirm_task(**params.arguments)
        await params.result_callback(result)

    async def _get_task_status(params: FunctionCallParams) -> None:
        result = await handlers.get_task_status()
        await params.result_callback(result)

    async def _request_cancel(params: FunctionCallParams) -> None:
        result = await handlers.request_cancel()
        await params.result_callback(result)

    async def _answer_kora(params: FunctionCallParams) -> None:
        result = await handlers.answer_kora(**params.arguments)
        await params.result_callback(result)

    async def _consult_kora(params: FunctionCallParams) -> None:
        result = await handlers.consult_kora(**params.arguments)
        await params.result_callback(result)

    async def _propose_request(params: FunctionCallParams) -> None:
        result = await handlers.propose_request(**params.arguments)
        await params.result_callback(result)

    async def _gate_action(params: FunctionCallParams) -> None:
        result = await handlers.gate_action(**params.arguments)
        await params.result_callback(result)

    async def _bind_project(params: FunctionCallParams) -> None:
        result = await handlers.bind_project(**params.arguments)
        await params.result_callback(result)

    async def _set_persona(params: FunctionCallParams) -> None:
        result = await handlers.set_persona(**params.arguments)
        await params.result_callback(result)

    llm_or_switcher.register_function("submit_task", _submit_task, cancel_on_interruption=False)
    llm_or_switcher.register_function("confirm_task", _confirm_task, cancel_on_interruption=False)
    llm_or_switcher.register_function("request_cancel", _request_cancel, cancel_on_interruption=False)
    # E5 (S5): barge-in must NOT drop the answer — losing it would strand Kora blocked forever.
    llm_or_switcher.register_function("answer_kora", _answer_kora, cancel_on_interruption=False)
    llm_or_switcher.register_function("consult_kora", _consult_kora, cancel_on_interruption=False)
    llm_or_switcher.register_function("propose_request", _propose_request, cancel_on_interruption=False)
    llm_or_switcher.register_function("gate_action", _gate_action, cancel_on_interruption=False)
    llm_or_switcher.register_function("bind_project", _bind_project, cancel_on_interruption=False)
    llm_or_switcher.register_function("set_persona", _set_persona, cancel_on_interruption=False)
    llm_or_switcher.register_function("get_task_status", _get_task_status, cancel_on_interruption=True)
