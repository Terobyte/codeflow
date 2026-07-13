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

# B5: the authoritative set of dispatchable tool names — dispatch never resolves anything else.
_VALID_TOOL_NAMES = frozenset(s.name for s in ALL_SCHEMAS)


class LLMClient(Protocol):
    async def complete(self, messages: list[dict[str, Any]], tools: list[Any]) -> tuple[str, list[ToolCall]]:
        ...


# B10: tool passes per user turn are BOUNDED. The old shape was strictly two passes and
# silently dropped any tool_calls the second completion returned; a chaining LLM
# (get_task_status -> request_cancel) lost the follow-up. Loop until the model stops
# calling tools, capped so a pathological LLM can't spin forever (industry default 5-20).
_MAX_TOOL_PASSES = 5


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
        passes = 0
        # Р-2: a tool turn needs at least one more completion with the tool results in context —
        # that call produces the text the dispatcher actually says. B10: keep going while the
        # model keeps chaining tools, bounded by _MAX_TOOL_PASSES; on cap exhaustion the tail
        # tool_calls are dropped (same behavior the old 2-pass shape had on pass 2).
        while tool_calls and passes < _MAX_TOOL_PASSES:
            # UI-3: канонический шейп — tool-результату предшествует assistant-ход с
            # tool_use-анонсом (без него Anthropic Messages API отклоняет историю).
            self._history.append({
                "role": "assistant",
                "content": text or "",
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments} for c in tool_calls
                ],
            })
            for call in tool_calls:
                await self._dispatch_tool(call)
            text, tool_calls = await self._complete()
            if text:
                record.llm_output = text
            passes += 1

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
        # B5: dispatch ONLY the declared tools. A hallucinated/adversarial name that collides with
        # a real ToolHandlers method (e.g. `begin_turn`) must NOT be `getattr`'d and invoked —
        # validate against the ALL_SCHEMAS allowlist first, not just "is it an attribute".
        handler = getattr(self._handlers, call.name, None) if call.name in _VALID_TOOL_NAMES else None
        if handler is None:
            result: Any = {"error": f"unknown tool {call.name}"}
        else:
            result = await handler(**call.arguments)
        self._history.append(
            {"role": "tool", "tool_call_id": call.id, "name": call.name,
             "content": json.dumps(result, ensure_ascii=False)}
        )
        return result

    def _render_state(self, now: float) -> str:
        return self._store.render_state(now, self._cfg.stale_after_s, self._cfg.unreachable_after_s)
