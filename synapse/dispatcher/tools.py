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
from synapse.bridge.state import TaskStore
from synapse.clock import Clock
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal

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
    description="Получить свежий снимок состояния активной задачи. Обязателен перед любым ответом о ходе или результатах задачи.",
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
        "Использовать только когда [СОСТОЯНИЕ] показывает, что Кора ждёт ответа на свой вопрос."
    ),
    properties={"text": {"type": "string", "description": "Реплика пользователя дословно."}},
    required=["text"],
)

ALL_SCHEMAS = [
    SUBMIT_TASK_SCHEMA,
    CONFIRM_TASK_SCHEMA,
    GET_TASK_STATUS_SCHEMA,
    REQUEST_CANCEL_SCHEMA,
    ANSWER_KORA_SCHEMA,
]


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


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
    on_answer: Callable[[str], bool] | None = None


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
        self._dedup: dict[str, _DedupEntry] = {}
        self._current_turn_id: str | None = None

    def begin_turn(self, turn_id: str) -> None:
        self._current_turn_id = turn_id

    async def _guarded(
        self, name: str, args: dict[str, Any], fn: Callable[[], Any]
    ) -> tuple[dict[str, Any], bool]:
        entry = self._dedup.get(name)
        # B14: a dedup hit requires SAME turn AND SAME args — a retry re-issues identical args;
        # a genuinely different same-name call must execute, not return the prior result.
        if entry is not None and entry.turn_id == self._current_turn_id and entry.args == args:
            return entry.result, True
        result = await fn()
        self._dedup[name] = _DedupEntry(turn_id=self._current_turn_id or "", result=result, args=args)
        return result, False

    async def submit_task(self, text: str) -> dict[str, Any]:
        async def _do() -> dict[str, Any]:
            res = self.bridge.confirm_flow.submit(text, self.bridge.clock.now())
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
            res = self.bridge.confirm_flow.confirm(decision, self.bridge.clock.now())
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
        result = self.bridge.store.snapshot(self.bridge.clock.now(), cfg.stale_after_s, cfg.unreachable_after_s)
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
            ok = self.bridge.on_answer(text) if self.bridge.on_answer else False
            return {"outcome": "answer_delivered" if ok else "no_pending_question"}

        result, deduped = await self._guarded("answer_kora", {"text": text}, _do)
        self._journal.record_tool_call("answer_kora", {"text": text}, {**result, "deduped": deduped})
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

    llm_or_switcher.register_function("submit_task", _submit_task, cancel_on_interruption=False)
    llm_or_switcher.register_function("confirm_task", _confirm_task, cancel_on_interruption=False)
    llm_or_switcher.register_function("request_cancel", _request_cancel, cancel_on_interruption=False)
    # E5 (S5): barge-in must NOT drop the answer — losing it would strand Kora blocked forever.
    llm_or_switcher.register_function("answer_kora", _answer_kora, cancel_on_interruption=False)
    llm_or_switcher.register_function("get_task_status", _get_task_status, cancel_on_interruption=True)
