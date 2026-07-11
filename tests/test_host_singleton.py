"""M1 host-singleton (2026-07-11 run): SynapseHost holds the long-lived logical state
(store/speak_ledger/confirm_flow/arbiter_policy/breaker/cost_cap) across WebRTC reconnects;
build_session_pipeline(host) rebuilds only the per-connection processors on every connection,
never the host state itself."""


def _cfg(tmp_path):
    from synapse.config import SynapseConfig

    return SynapseConfig(
        google_api_key="fake-google-key",
        openrouter_api_key="fake-openrouter-key",
        anthropic_api_key="fake-anthropic-key",
        deepgram_api_key="fake-deepgram-key",
        fish_audio_api_key="fake-fish-key",
        fish_reference_id="fake-fish-ref",
        journal_dir=str(tmp_path),
    )


def test_state_survives_reconnect(tmp_path):
    from synapse.bridge.state import TaskStatus
    from synapse.pipeline.app import build_host, build_session_pipeline
    from synapse.pipeline.arbiter import TTSArbiterProcessor

    host = build_host(_cfg(tmp_path))
    host.store.start_task("t1", "do the thing", TaskStatus.RUNNING, now=0.0)

    session1 = build_session_pipeline(host)
    assert host.store.task is not None and host.store.task.id == "t1"

    # session1 "drops" (browser disconnect) -- nothing about a session ever owns host state, so
    # losing the reference to it changes nothing about the task.
    del session1

    session2 = build_session_pipeline(host)
    # DoD-1: the task submitted before the first connection is still there after a reconnect.
    assert host.store.task is not None
    assert host.store.task.id == "t1"

    # Both sessions were wired to the SAME long-lived collaborators, not copies -- the arbiter
    # processor built into session2's pipeline holds a reference to host.arbiter_policy
    # specifically (identity, not just equal contents).
    arbiter = next(p for p in session2.pipeline.processors if isinstance(p, TTSArbiterProcessor))
    assert arbiter._policy is host.arbiter_policy


def test_session_processors_are_fresh(tmp_path):
    from synapse.pipeline.app import build_host, build_session_pipeline

    host = build_host(_cfg(tmp_path))
    session1 = build_session_pipeline(host)
    session2 = build_session_pipeline(host)

    # Falsify-check (research §2): a pipecat FrameProcessor instance's singly-linked
    # _prev/_next belongs to exactly one PipelineRunner run -- reusing one across connections
    # would corrupt that topology. Every build_session_pipeline() call must therefore hand back
    # entirely fresh processor instances, never the same objects twice.
    procs1, procs2 = session1.pipeline.processors, session2.pipeline.processors
    assert len(procs1) == len(procs2)
    for p1, p2 in zip(procs1, procs2):
        assert p1 is not p2
        # Compare by name, not `type(p1) is type(p2)`: the guarded assistant aggregator's class
        # is itself built fresh per call (make_guarded_assistant_aggregator), by design -- so
        # its two class *objects* legitimately differ even though both instances are correct.
        assert type(p1).__name__ == type(p2).__name__

    assert session1.llm_switcher is not session2.llm_switcher
    assert session1.generation_guard is not session2.generation_guard


def test_breaker_cost_cap_on_host(tmp_path):
    from synapse.pipeline.app import build_host, build_session_pipeline

    host = build_host(_cfg(tmp_path))
    host.cost_cap.record_paid_attempt()  # a paid call happened before any session existed

    session1 = build_session_pipeline(host)
    session2 = build_session_pipeline(host)

    # Alt-Q3/Risk-M6: breaker/cost_cap live on the host, not rebuilt per connection -- a
    # reconnect must not reset an already-tripped cap or un-mute a breaker-muted tier.
    assert session1.llm_switcher.strategy._breaker is host.breaker
    assert session2.llm_switcher.strategy._breaker is host.breaker
    assert session1.llm_switcher.strategy._cost_cap is host.cost_cap
    assert session2.llm_switcher.strategy._cost_cap is host.cost_cap
    assert host.cost_cap.count == 1  # unaffected by building two sessions
