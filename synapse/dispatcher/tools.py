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

from synapse.bridge.confirm import ConfirmFlow, ConfirmResult, SubmitResult
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

ALL_SCHEMAS = [SUBMIT_TASK_SCHEMA, CONFIRM_TASK_SCHEMA, GET_TASK_STATUS_SCHEMA, REQUEST_CANCEL_SCHEMA]


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


class ToolHandlers:
    def __init__(self, bridge: KoraBridge, journal: TurnJournal) -> None:
        self.bridge = bridge
        self._journal = journal
        self._dedup: dict[str, _DedupEntry] = {}
        self._current_turn_id: str | None = None

    def begin_turn(self, turn_id: str) -> None:
        self._current_turn_id = turn_id

    async def _guarded(self, name: str, fn: Callable[[], Any]) -> tuple[dict[str, Any], bool]:
        entry = self._dedup.get(name)
        if entry is not None and entry.turn_id == self._current_turn_id:
            return entry.result, True
        result = await fn()
        self._dedup[name] = _DedupEntry(turn_id=self._current_turn_id or "", result=result)
        return result, False

    async def submit_task(self, text: str) -> dict[str, Any]:
        async def _do() -> dict[str, Any]:
            res = self.bridge.confirm_flow.submit(text, self.bridge.clock.now())
            speak_text = res.readback_text or res.reject_text
            if speak_text and self.bridge.on_speak:
                self.bridge.on_speak(speak_text)
            return _submit_result_to_dict(res)

        result, deduped = await self._guarded("submit_task", _do)
        self._journal.record_tool_call("submit_task", {"text": text}, {**result, "deduped": deduped})
        return result

    async def confirm_task(self, decision: str) -> dict[str, Any]:
        async def _do() -> dict[str, Any]:
            res = self.bridge.confirm_flow.confirm(decision, self.bridge.clock.now())
            if res.text and self.bridge.on_speak:
                self.bridge.on_speak(res.text)
            return _confirm_result_to_dict(res)

        result, deduped = await self._guarded("confirm_task", _do)
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
            return {"outcome": "cancel_requested" if ok else "no_active_task"}

        result, deduped = await self._guarded("request_cancel", _do)
        self._journal.record_tool_call("request_cancel", {}, {**result, "deduped": deduped})
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

    llm_or_switcher.register_function("submit_task", _submit_task, cancel_on_interruption=False)
    llm_or_switcher.register_function("confirm_task", _confirm_task, cancel_on_interruption=False)
    llm_or_switcher.register_function("request_cancel", _request_cancel, cancel_on_interruption=False)
    llm_or_switcher.register_function("get_task_status", _get_task_status, cancel_on_interruption=True)
