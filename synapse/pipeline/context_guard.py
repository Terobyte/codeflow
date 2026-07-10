"""GenerationGuard — S1: an intra-turn cascade retry must not let an aborted generation's
partial text land in the LLM context. `ErrorFrame` (upstream, from the failover strategy)
and `LLMFullResponseEndFrame` (downstream, from the assistant context aggregator) race on
separate asyncio tasks with no ordering guarantee (asyncio critique S1). Both orders are
handled:

  (a) abort marked BEFORE the aggregator's commit -> the commit is skipped outright.
  (b) the aggregator's commit lands BEFORE the abort is marked -> the tail it appended is
      scrubbed retroactively, the moment the abort is marked.

Pure core, no pipecat imports — a `context` here is duck-typed as "anything with a
`.messages` list", so `GenerationGuard` itself is unit-testable with a bare fake object.
`make_guarded_assistant_aggregator` is the pipecat-specific integration glue on top.
"""
from __future__ import annotations

import itertools
from typing import Any, Protocol


class _HasMessages(Protocol):
    messages: list[Any]


class GenerationGuard:
    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self._current_generation = 0
        self._aborted: set[int] = set()
        self._snapshot_len: dict[int, int] = {}
        self._committed_contexts: dict[int, _HasMessages] = {}

    @property
    def current_generation(self) -> int:
        return self._current_generation

    def start_generation(self, context: _HasMessages) -> int:
        """Called when a new turn's generation begins (LLMContextFrame pushed). Snapshots
        the pre-generation message count so a later scrub knows where to cut."""
        gen = next(self._counter)
        self._current_generation = gen
        self._snapshot_len[gen] = len(context.messages)
        return gen

    def is_aborted(self, generation: int) -> bool:
        return generation in self._aborted

    def mark_aborted(self, generation: int) -> None:
        """Called by the failover strategy before/around switching tiers. If this
        generation's text was already committed to a context (race order b), scrub it
        immediately."""
        self._aborted.add(generation)
        context = self._committed_contexts.get(generation)
        if context is not None:
            self._scrub(context, generation)

    def record_committed(self, generation: int, context: _HasMessages) -> None:
        """Called by the guarded assistant aggregator right after it commits a generation's
        text to `context`. Symmetric defense-in-depth: if the generation was already marked
        aborted by then, scrub immediately."""
        self._committed_contexts[generation] = context
        if generation in self._aborted:
            self._scrub(context, generation)

    def _scrub(self, context: _HasMessages, generation: int) -> None:
        snapshot_len = self._snapshot_len.get(generation)
        if snapshot_len is None:
            return
        if len(context.messages) > snapshot_len:
            del context.messages[snapshot_len:]

    def forget(self, generation: int) -> None:
        self._aborted.discard(generation)
        self._snapshot_len.pop(generation, None)
        self._committed_contexts.pop(generation, None)


def make_guarded_assistant_aggregator(base_cls: type, guard: GenerationGuard) -> type:
    """Returns a subclass of pipecat's LLMAssistantAggregator (`base_cls`) that consults
    `guard` before committing a generation's text to the LLM context (race order a), and
    registers the commit with `guard` right after (so race order b can scrub it later).

    NOTE: wiring an instance of the returned class into the live pipeline (in place of
    `LLMContextAggregatorPair`'s built-in assistant aggregator) is integration work beyond
    what M0's offline test suite exercises — GenerationGuard's own logic is fully unit
    tested (test_context_guard.py); this factory is the documented next step for wiring it
    into a live pipecat pipeline.
    """

    class GuardedLLMAssistantAggregator(base_cls):  # type: ignore[misc]
        async def push_aggregation(self) -> str:
            gen = guard.current_generation
            if guard.is_aborted(gen):
                await self.reset()
                return ""
            result = await super().push_aggregation()
            guard.record_committed(gen, self._context)
            return result

    return GuardedLLMAssistantAggregator
