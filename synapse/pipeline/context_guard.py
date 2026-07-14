"""GenerationGuard — S1: an intra-turn cascade retry must not let an aborted generation's
partial text land in the LLM context. `ErrorFrame` (upstream, from the failover strategy)
and `LLMFullResponseEndFrame` (downstream, from the assistant context aggregator) race on
separate asyncio tasks with no ordering guarantee (asyncio critique S1). Both orders are
handled:

  (a) abort marked BEFORE the aggregator's commit -> the commit is skipped outright.
  (b) the aggregator's commit lands BEFORE the abort is marked -> the tail it appended is
      scrubbed retroactively, the moment the abort is marked.

`GenerationGuard` itself has no pipecat dependency in its own logic — a `context` here is
duck-typed as "anything with a `.messages` list", so it is unit-testable with a bare fake
object. The rest of this module is pipecat-specific integration glue on top:
`make_guarded_assistant_aggregator` wraps the assistant aggregator that commits generations;
`GenerationStartHook` is the frame-flow glue that actually calls `start_generation` (wired
into the live pipeline in app.py). Two `GenerationStartHook`s, not one, because tool-loop
re-inference re-enters the LLM UPSTREAM of the switcher, past a pre-switcher-only hook.
"""
from __future__ import annotations

import itertools
from typing import Any, Protocol

from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


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
        the pre-generation message count so a later scrub knows where to cut.

        Also forgets every earlier generation's bookkeeping (an unbounded cascade-retry
        session would otherwise grow these dicts forever). Stale generations are collected
        from the UNION of all three structures, not just `_snapshot_len`: the guarded
        aggregator captures its gen at push_aggregation() entry and commits that captured
        number after its await — it does NOT re-read `current_generation` — so a late
        `record_committed(N)` for an already-forgotten N recreates an entry that lives only
        in `_committed_contexts`, which a snapshot-keys-only sweep would never evict. Such a
        recreated record is HARMLESS while it sits there (`_scrub` of a forgotten generation
        is a no-op — its snapshot is gone, see `_scrub`'s None check); the union sweep here
        evicts it on the next start. `is_aborted()` on a forgotten generation still returns
        False, same as it always has for any generation nobody has marked — the pre-cleanup
        default is preserved, not silently changed."""
        gen = next(self._counter)
        stale = (set(self._snapshot_len) | set(self._committed_contexts) | self._aborted) - {gen}
        for old_gen in stale:
            self.forget(old_gen)
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


def make_guarded_assistant_aggregator(
    base_cls: type, guard: GenerationGuard, on_commit: Any = None
) -> type:
    """Returns a subclass of pipecat's LLMAssistantAggregator (`base_cls`) that consults
    `guard` before committing a generation's text to the LLM context (race order a), and
    registers the commit with `guard` right after (so race order b can scrub it later).

    `on_commit` (optional, no-arg callable): fired synchronously right after a generation's
    text is committed to the live context — B25's hook so the dispatcher's just-spoken answer
    reaches the thread feed AT COMMIT time (≤ next pollFeed), instead of one turn late (the
    next `_on_end_of_turn`'s context-diff flush) or only on disconnect. The wired callback is
    `_flush_voice_context` in app.py; its cursor makes a commit-time flush idempotent with the
    later turn/disconnect flushes, so no feed entry is duplicated.

    An instance of the returned class is wired into the live pipeline in app.py (built via
    LLMContextAggregatorPair's own __init__ recipe, since the pair offers no subclass hook),
    together with the two GenerationStartHooks around the switcher — see
    test_pipeline_smoke.py for the wiring coverage.
    """

    class GuardedLLMAssistantAggregator(base_cls):  # type: ignore[misc]
        async def push_aggregation(self) -> str:
            gen = guard.current_generation
            if guard.is_aborted(gen):
                await self.reset()
                return ""
            result = await super().push_aggregation()
            # Unconditional on purpose, even when `result` is empty (a pure tool-call turn
            # with no assistant text): LLMFullResponseEndFrame -- which triggers this
            # push_aggregation -- is pushed unconditionally in the LLM service's `finally`,
            # so this call always runs too. That is what keeps commit(N) ordered strictly
            # before start_generation(N+1) in the tool-loop case (critique MAJOR-1).
            guard.record_committed(gen, self._context)
            # B25: flush the just-committed answer to the thread feed now. Guarded on
            # is_aborted(gen): if a failover marked this generation aborted around the commit
            # (its text is/was scrubbed from the context), don't leak the retracted text to the
            # feed. Race order (b) — abort STRICTLY after this point — is the residual rare
            # window a later real answer's flush supersedes. Empty content is skipped inside the
            # flusher, so a pure tool-call turn writes nothing.
            if on_commit is not None and not guard.is_aborted(gen):
                on_commit()
            return result

    return GuardedLLMAssistantAggregator


class GenerationStartHook(FrameProcessor):
    """Calls `guard.start_generation(frame.context)` when an `LLMContextFrame` passes
    through in `direction` — the frame-flow trigger `GenerationGuard` itself has no way to
    see on its own (it is pure core, no pipecat imports).

    Two of these are wired around the cascade's LLMSwitcher, not one: the user turn's
    LLMContextFrame travels DOWNSTREAM out of the user aggregator, but a tool-call's
    re-inference travels UPSTREAM out of the assistant aggregator instead (pipecat's LLM
    services terminate LLMContextFrame — they never re-push it) — a DOWNSTREAM-only hook
    would miss every tool-loop turn, leaving `current_generation` stale for it."""

    def __init__(self, guard: GenerationGuard, direction: FrameDirection) -> None:
        super().__init__()
        self._guard = guard
        self._direction = direction

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame) and direction == self._direction:
            self._guard.start_generation(frame.context)
        await self.push_frame(frame, direction)
