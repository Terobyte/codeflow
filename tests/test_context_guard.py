import asyncio

from pipecat.frames.frames import LLMContextFrame, TextFrame
from pipecat.processors.frame_processor import FrameDirection

from synapse.pipeline.context_guard import (
    GenerationGuard,
    GenerationStartHook,
    make_guarded_assistant_aggregator,
)


class FakeContext:
    def __init__(self, messages=None):
        self.messages = messages if messages is not None else []


def test_abort_before_commit_leaves_nothing_to_scrub():
    """Race order (a): abort marked before the aggregator ever attempts to commit -- the
    guarded aggregator's own pre-commit check (`guard.is_aborted(gen)`) means
    record_committed() is never even called, so context.messages is untouched."""
    guard = GenerationGuard()
    ctx = FakeContext(["system"])
    gen = guard.start_generation(ctx)
    guard.mark_aborted(gen)
    assert guard.is_aborted(gen) is True
    assert ctx.messages == ["system"]


def test_commit_before_abort_is_scrubbed_retroactively():
    """Race order (b): the commit lands before the abort is marked -- mark_aborted() must
    scrub the tail immediately when it finally runs."""
    guard = GenerationGuard()
    ctx = FakeContext(["system"])
    gen = guard.start_generation(ctx)

    ctx.messages.append({"role": "assistant", "content": "partial garbage"})
    guard.record_committed(gen, ctx)
    assert ctx.messages == ["system", {"role": "assistant", "content": "partial garbage"}]

    guard.mark_aborted(gen)  # arrives late
    assert ctx.messages == ["system"]


def test_record_committed_after_abort_is_symmetric():
    """Defense-in-depth: if mark_aborted() somehow ran first and record_committed() still
    got called for that generation, the commit must be scrubbed immediately too."""
    guard = GenerationGuard()
    ctx = FakeContext(["system"])
    gen = guard.start_generation(ctx)
    guard.mark_aborted(gen)

    ctx.messages.append({"role": "assistant", "content": "should not survive"})
    guard.record_committed(gen, ctx)
    assert ctx.messages == ["system"]


def test_unrelated_generation_is_not_touched():
    guard = GenerationGuard()
    ctx1 = FakeContext(["a"])
    ctx2 = FakeContext(["b"])
    gen1 = guard.start_generation(ctx1)
    ctx1.messages.append("gen1 text")
    guard.record_committed(gen1, ctx1)

    gen2 = guard.start_generation(ctx2)
    ctx2.messages.append("gen2 text")
    guard.record_committed(gen2, ctx2)

    guard.mark_aborted(gen2)
    assert ctx1.messages == ["a", "gen1 text"]
    assert ctx2.messages == ["b"]


def test_successful_generation_is_never_scrubbed():
    guard = GenerationGuard()
    ctx = FakeContext(["system"])
    gen = guard.start_generation(ctx)
    ctx.messages.append("normal reply")
    guard.record_committed(gen, ctx)
    assert ctx.messages == ["system", "normal reply"]


def test_start_generation_forgets_prior_generation_bookkeeping():
    """Plan v1 item 2: a new start_generation() must forget every earlier generation's
    records (dict-growth fix), while the new generation is fully live and an unrelated
    forgotten generation stays inert (is_aborted() still reports False for it, same as any
    generation nobody has marked -- the pre-cleanup default, not silently changed)."""
    guard = GenerationGuard()
    ctx1 = FakeContext(["a"])
    gen1 = guard.start_generation(ctx1)
    ctx1.messages.append("gen1 text")
    guard.record_committed(gen1, ctx1)

    ctx2 = FakeContext(["b"])
    gen2 = guard.start_generation(ctx2)

    # gen1's bookkeeping is gone from all three internal collections...
    assert gen1 not in guard._snapshot_len
    assert gen1 not in guard._committed_contexts
    # ...but a late mark_aborted(gen1) is still a harmless no-op, not a KeyError or a scrub
    # of the wrong context -- is_aborted() for a forgotten generation reads exactly like a
    # generation that was never seen.
    guard.mark_aborted(gen1)
    assert guard.is_aborted(gen1) is True  # membership-tracked, but...
    assert ctx1.messages == ["a", "gen1 text"]  # ...nothing left to scrub against, so no-op

    # gen2 (the current generation) is fully live and unaffected.
    ctx2.messages.append("gen2 text")
    guard.record_committed(gen2, ctx2)
    assert ctx2.messages == ["b", "gen2 text"]


def test_late_record_committed_for_forgotten_generation_is_swept_by_next_start():
    """Plan v2.1 Д-1: push_aggregation() captures its gen at entry and commits that captured
    number after its await -- so a late record_committed(N) can land AFTER N was already
    forgotten by a newer start_generation(), recreating an entry that lives only in
    `_committed_contexts` (and, symmetrically, a late mark_aborted recreates one in
    `_aborted`). A snapshot-keys-only sweep would never evict those; the union sweep must
    clear them from ALL three structures on the next start."""
    guard = GenerationGuard()
    ctx1 = FakeContext(["a"])
    gen1 = guard.start_generation(ctx1)

    ctx2 = FakeContext(["b"])
    gen2 = guard.start_generation(ctx2)  # forgets gen1 entirely

    # The late commit (and a late abort) for the forgotten gen1 arrive now -- harmless in
    # the moment (its snapshot is gone, so any scrub is a no-op)...
    guard.record_committed(gen1, ctx1)
    guard.mark_aborted(gen1)
    assert gen1 in guard._committed_contexts  # recreated, invisible to a snapshot-only sweep
    assert gen1 in guard._aborted
    assert ctx1.messages == ["a"]  # no scrub happened -- snapshot was already gone

    # ...and the NEXT start_generation must sweep the recreated entries from all three
    # structures, not leak them forever.
    ctx3 = FakeContext(["c"])
    gen3 = guard.start_generation(ctx3)
    assert gen1 not in guard._snapshot_len
    assert gen1 not in guard._committed_contexts
    assert gen1 not in guard._aborted
    assert gen2 not in guard._committed_contexts

    # The new generation itself is fully live.
    assert set(guard._snapshot_len) == {gen3}
    assert guard.current_generation == gen3


class _PushSpy:
    """Stand-in for FrameProcessor.push_frame -- records calls without pipecat's
    StartFrame/task machinery (push_frame() is normally a no-op until a StartFrame has been
    processed; these tests are about GenerationStartHook's own logic, not pipecat's
    plumbing)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def __call__(self, frame, direction) -> None:
        self.calls.append((frame, direction))


def test_generation_start_hook_matching_direction_starts_generation_and_forwards():
    """GenerationStartHook: an LLMContextFrame in the hook's own direction triggers
    start_generation() AND is still forwarded (§3 item 1)."""
    guard = GenerationGuard()
    hook = GenerationStartHook(guard, FrameDirection.DOWNSTREAM)
    hook.push_frame = _PushSpy()

    ctx = FakeContext(["system"])
    frame = LLMContextFrame(context=ctx)
    asyncio.run(hook.process_frame(frame, FrameDirection.DOWNSTREAM))

    assert guard.current_generation == 1
    assert hook.push_frame.calls == [(frame, FrameDirection.DOWNSTREAM)]


def test_generation_start_hook_mismatched_direction_forwards_without_starting():
    guard = GenerationGuard()
    hook = GenerationStartHook(guard, FrameDirection.UPSTREAM)
    hook.push_frame = _PushSpy()

    ctx = FakeContext(["system"])
    frame = LLMContextFrame(context=ctx)
    asyncio.run(hook.process_frame(frame, FrameDirection.DOWNSTREAM))

    assert guard.current_generation == 0  # untouched -- no start_generation call
    assert hook.push_frame.calls == [(frame, FrameDirection.DOWNSTREAM)]


def test_generation_start_hook_ignores_non_context_frames():
    guard = GenerationGuard()
    hook = GenerationStartHook(guard, FrameDirection.DOWNSTREAM)
    hook.push_frame = _PushSpy()

    frame = TextFrame(text="hello")
    asyncio.run(hook.process_frame(frame, FrameDirection.DOWNSTREAM))

    assert guard.current_generation == 0
    assert hook.push_frame.calls == [(frame, FrameDirection.DOWNSTREAM)]


class _FakeBaseAssistantAggregator:
    """Minimal stand-in for pipecat's LLMAssistantAggregator: just enough surface
    (`_context`, `push_aggregation`, `reset`) for make_guarded_assistant_aggregator's
    wrapper to drive, without any real pipecat aggregation logic."""

    def __init__(self, context, _paired_user_aggregator=None) -> None:
        self._context = context
        self.reset_calls = 0

    async def push_aggregation(self) -> str:
        return ""  # empty aggregation -- e.g. a pure tool-call turn with no assistant text

    async def reset(self) -> None:
        self.reset_calls += 1


class _RecordingGuard(GenerationGuard):
    """GenerationGuard with call-order logging, for the MAJOR-1 ordering test below."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def start_generation(self, context) -> int:
        gen = super().start_generation(context)
        self.calls.append(f"start:{gen}")
        return gen

    def record_committed(self, generation: int, context) -> None:
        self.calls.append(f"commit:{generation}")
        super().record_committed(generation, context)


def test_empty_aggregation_commit_still_registers_before_next_generation_starts():
    """Plan v2 §5 (critique MAJOR-1): LLMFullResponseEndFrame -- which triggers
    push_aggregation() -- is pushed unconditionally in the LLM service's `finally`, even for
    a pure tool-call turn with no assistant text. GuardedLLMAssistantAggregator.
    push_aggregation() must call guard.record_committed() unconditionally too, so commit(N)
    is always registered strictly before start_generation(N+1) -- otherwise a tool-loop
    turn's snapshot_len would go stale before it's ever committed."""
    guard = _RecordingGuard()
    ctx = FakeContext(["system"])
    Guarded = make_guarded_assistant_aggregator(_FakeBaseAssistantAggregator, guard)
    aggregator = Guarded(ctx)

    gen1 = guard.start_generation(ctx)
    result = asyncio.run(aggregator.push_aggregation())
    assert result == ""  # empty aggregation, as in a pure tool-call turn

    gen2 = guard.start_generation(ctx)

    assert guard.calls == [f"start:{gen1}", f"commit:{gen1}", f"start:{gen2}"]
