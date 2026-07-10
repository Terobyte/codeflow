from synapse.pipeline.context_guard import GenerationGuard


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
