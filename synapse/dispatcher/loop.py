"""DispatcherTurnLoop — the chat loop: builds messages, calls the LLM, dispatches tool
calls through ToolHandlers, and returns the final text for the caller (console.py / a future
pipecat adapter) to route into ArbiterPolicy. Journaling and the grounding check happen
here; `end_turn()` is left to the caller so `tts_texts` can be filled in AFTER the caller
drains the arbiter (R2/R6 evidence-ordering: the alert-durability guarantee is about
`alert()`, not the turn-close line, so it's fine for end_turn() to happen after TTS output
is known).
"""
from __future__ import annotations

import json
from typing import Any, Protocol

from synapse.bridge.confirm import ConfirmFlow
from synapse.bridge.state import TaskStore
from synapse.clock import Clock
from synapse.config import SynapseConfig
from synapse.dispatcher.tools import ALL_SCHEMAS, ToolCall, ToolHandlers
from synapse.journal import TurnJournal, TurnRecord
from synapse.prompt import build_system_prompt


class LLMClient(Protocol):
    async def complete(self, messages: list[dict[str, Any]], tools: list[Any]) -> tuple[str, list[ToolCall]]:
        ...


class DispatcherTurnLoop:
    def __init__(
        self,
        llm: LLMClient,
        handlers: ToolHandlers,
        confirm_flow: ConfirmFlow,
        store: TaskStore,
        journal: TurnJournal,
        clock: Clock,
        cfg: SynapseConfig,
        task_dictionary: dict[str, str] | None = None,
    ) -> None:
        self._llm = llm
        self._handlers = handlers
        self._confirm_flow = confirm_flow
        self._store = store
        self._journal = journal
        self._clock = clock
        self._cfg = cfg
        self._task_dictionary = task_dictionary or {}
        self._history: list[dict[str, Any]] = []

    async def ingest_user_turn(self, transcript: str) -> tuple[TurnRecord, str]:
        now = self._clock.now()
        record = self._journal.begin_turn(transcript)
        self._handlers.begin_turn(record.turn_id)

        # R3: MUST run before the LLM call — half (a) of Р-16's double-key confirm check.
        self._confirm_flow.note_user_turn(transcript, now)

        had_active_task = self._store.has_active_task()
        self._history.append({"role": "user", "content": transcript})

        text, tool_calls = await self._complete()
        record.llm_output = text

        if tool_calls:
            for call in tool_calls:
                await self._dispatch_tool(call)
            # Р-2: status-turn (and any tool turn) is two passes — one more call with the
            # tool results in context, to get the text the dispatcher actually says.
            text, _ = await self._complete()
            if text:
                record.llm_output = text

        if text:
            self._history.append({"role": "assistant", "content": text})

        record.latency_ms = (self._clock.now() - now) * 1000.0
        self._journal.check_grounding(record, had_active_task)
        return record, text

    async def _complete(self) -> tuple[str, list[ToolCall]]:
        state_block = self._render_state(self._clock.now())
        system_prompt = build_system_prompt(self._cfg, self._task_dictionary)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt + "\n\n" + state_block},
            *self._history,
        ]
        return await self._llm.complete(messages, ALL_SCHEMAS)

    async def _dispatch_tool(self, call: ToolCall) -> Any:
        handler = getattr(self._handlers, call.name, None)
        if handler is None:
            result: Any = {"error": f"unknown tool {call.name}"}
        else:
            result = await handler(**call.arguments)
        self._history.append(
            {"role": "tool", "name": call.name, "content": json.dumps(result, ensure_ascii=False)}
        )
        return result

    def _render_state(self, now: float) -> str:
        return self._store.render_state(now, self._cfg.stale_after_s, self._cfg.unreachable_after_s)
