# -*- coding: utf-8-sig -*-
"""Red tests proving bugs B15, B17, B18, B21 from docs/bugs.md ("Hunt 2026-07-14b").

One test per bug ID. Each test is written so the CORRECT (documented) behavior is the pass
condition -- these are expected to FAIL against the current (unfixed) tree and to flip green
once the corresponding bug is fixed, with NO change to the assertions themselves.

Touches no production code, no other test files, never bugs.md. Fixtures/harness mirror
tests/test_hunt0714_a.py and tests/test_hunt0714_b.py (SynapseHost via build_host with fake
keys that are never dialed, the cost-counting switcher, SpeakLedger, gate_action, a fake
output task).
"""
from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

from synapse.bridge.state import EventClass, KoraEvent
from synapse.config import SynapseConfig


def _fake_cfg(tmp_path, **overrides) -> SynapseConfig:
    """The standard fake-key cfg pattern used throughout the suite -- build_host() never dials
    the network with it. `overrides` lets a test tweak a single frozen field (e.g. the cap)."""
    cfg = SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path), kora_workspace_dir=str(tmp_path / "ws"),
    )
    return dataclasses.replace(cfg, **overrides) if overrides else cfg


class _FakeKoraRunner:
    """Stub KoraRunner (same shape as test_stages.py's _FakeRunner): records start(...) calls,
    no SDK/network involved."""

    def __init__(self) -> None:
        self.starts: list[tuple] = []

    def start(self, task_id, text, spec) -> None:
        self.starts.append((task_id, text, spec))


# ---------------------------------------------------------------------------------------
# B15 -- cost cap trips but is never ENFORCED on the healthy primary tier0. The switcher
# calls CostCap.record_paid_attempt and DISCARDS the returned veto, so once the cap trips a
# further paid tier0 generation still proceeds/forwards (bills) unbounded.
# location: synapse/pipeline/app.py:406-411 (+ strategy.py:89-92, services.py:92-105)
# expected: once tripped, no further paid call proceeds on any tier -- the switcher honors the
#           veto and does NOT forward the paid tier0 generation.
# actual:   only the failover path honors the veto; the primary tier bills without bound.
# ---------------------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="B15 PARKED (proven, deferred — see docs/bugs.md 'Hunt 2026-07-14b'). The bug is real "
    "(tripped cap not enforced on the primary tier0), but (a) this proof asserts the wrong "
    "enforcement layer — dropping the downstream End frame can't un-bill a completed call and "
    "would hang the very turn that trips the cap; correct enforcement gates the REQUEST frame, so "
    "no correct fix flips THIS test green — and (b) that fix is request-path pipecat surgery on "
    "the live voice path plus a UX decision (what the user hears when a turn is cost-blocked) that "
    "must be Tero-reviewed and live-tested, not landed autonomously. Kept as regression armor; "
    "owed: a corrected request-layer proof test + the request-time cost gate.",
    strict=False,
)
async def test_B15_tripped_cost_cap_must_block_further_tier0_generation(tmp_path, monkeypatch):
    from pipecat.frames.frames import LLMFullResponseEndFrame
    from pipecat.processors.frame_processor import FrameDirection
    import pipecat.pipeline.service_switcher as ss_mod

    from synapse.pipeline.app import build_host, build_session_pipeline

    # Cap of 1 so the very first paid tier0 turn trips it; the SECOND is the one that must be
    # enforced (blocked).
    host = build_host(_fake_cfg(tmp_path, max_paid_calls_per_day=1))
    session = build_session_pipeline(host)
    switcher = session.llm_switcher
    tier0 = switcher.strategy.active_service  # services[0], paid=True (openrouter)
    assert switcher.strategy.active_tier_index() == 0

    # Turn 1: healthy tier0 completes a paid generation -> counts -> trips the cap (max==1).
    end1 = LLMFullResponseEndFrame()
    end1.processor = tier0
    await switcher.push_frame(end1)
    assert host.cost_cap.tripped is True, (
        f"setup: one paid tier0 turn should trip a cap of 1, tripped={host.cost_cap.tripped!r}"
    )

    # Spy on the base forward (the `await super().push_frame(...)` the cost-counting switcher
    # reaches iff it decided to forward). Installed AFTER turn 1 so only turn 2 is observed.
    forwarded: list = []
    async def _spy(self, frame, direction=FrameDirection.DOWNSTREAM):
        forwarded.append(frame)

    monkeypatch.setattr(ss_mod.ServiceSwitcher, "push_frame", _spy)

    # Turn 2: tier0 tries another paid generation while the cap is TRIPPED. record_paid_attempt
    # returns False here; enforcement means this generation must NOT be forwarded (billed).
    end2 = LLMFullResponseEndFrame()
    end2.processor = tier0
    await switcher.push_frame(end2)

    assert not any(f is end2 for f in forwarded), (
        "B15: the daily cost cap is tripped, yet the switcher still forwarded (billed) a "
        "further paid tier0 generation -- record_paid_attempt's False veto is discarded "
        "(app.py:409-410), so the primary tier bills unbounded until the daily reset."
    )


# ---------------------------------------------------------------------------------------
# B17 -- a critical SPEAK dropped on the has_finished() silent-drop path never reverts the
# ledger. speak() marks the ledger spoken optimistically then fire-and-forgets
# push_speak_frame, which RETURNS NORMALLY (no queue, no raise) when the output task finished
# between scheduling and running. _on_speak_frame_done reverts only on cancel/exception, so
# the critical stays spoken=True producing no audio -> the Р-15г watchdog is disarmed.
# location: synapse/pipeline/app.py:190-192,204-217,221-229
# expected: a critical that produced no audio is left spoken=False so the watchdog re-fires.
# actual:   the finished/unbound silent-drop leaves it spoken=True permanently.
# ---------------------------------------------------------------------------------------


async def test_B17_silent_drop_on_finished_task_must_revert_ledger(tmp_path):
    from synapse.pipeline.app import build_host

    host = build_host(_fake_cfg(tmp_path))

    class _FinishesBeforePushTask:
        """LIVE when speak() checks liveness (app.py:206), then flips finished=True BEFORE the
        scheduled push_speak_frame coroutine runs (app.py:191) -- the exact silent-drop
        clean-return interleaving, forced with an explicit sync point (not loop-and-hope)."""

        def __init__(self) -> None:
            self.finished = False
            self.queued: list = []

        def has_finished(self) -> bool:
            return self.finished

        async def queue_frame(self, frame) -> None:
            self.queued.append(frame)

    task = _FinishesBeforePushTask()
    host.bind_output(task)

    ev = KoraEvent(
        id="e1", type="task_completed", cls=EventClass.CRITICAL, payload={},
        speak_text="готово", ts=host.clock.now(),
    )
    host.speak_ledger.register_critical(ev)

    before_tasks = set(asyncio.all_tasks())
    host.speak("готово")  # line 206 sees finished=False -> schedules push_speak_frame as a task
    scheduled = set(asyncio.all_tasks()) - before_tasks
    assert scheduled, "speak() should have scheduled the push_speak_frame injection as a task"

    # Explicit sync point: the task finishes AFTER speak() scheduled the coroutine but BEFORE it
    # runs. Now drive the scheduled coroutine (and its done-callback) to completion.
    task.finished = True
    await asyncio.gather(*scheduled, return_exceptions=True)

    # The silent drop happened: push_speak_frame returned without queueing and without raising.
    assert task.queued == [], (
        f"setup: push_speak_frame must NOT queue onto a finished task, queued={task.queued!r}"
    )

    assert host.speak_ledger._pending["e1"].spoken is False, (
        "B17: a CRITICAL SPEAK silently dropped on the has_finished() clean-return path was "
        f"left spoken=True (spoken={host.speak_ledger._pending['e1'].spoken!r}) -- "
        "push_speak_frame returned normally and _on_speak_frame_done reverts ONLY on "
        "cancel/exception, permanently disarming the Р-15г watchdog for this critical."
    )
    alerts = host.speak_ledger.check(now=host.clock.now() + 9999, window_s=1.0)
    assert any(
        kind == "CRITICAL_WITHOUT_SPEAK" and detail.get("event_id") == "e1"
        for kind, detail in alerts
    ), f"B17: the Р-15г watchdog did not re-fire for the silently-dropped critical (alerts={alerts!r})"


# ---------------------------------------------------------------------------------------
# B18 -- write_code gate crashes with TypeError on a projectless thread under default config
# (kora_workspace_dir=None). `root = self._resolve_root_for(th)` returns None, then
# `Path(root) / "docs" / ...` raises TypeError BEFORE the ValueError-only guard below it.
# location: synapse/pipeline/app.py:343-344 (+ config.py:77)
# expected: return a diagnosable error dict (no_plan_file), NOT raise TypeError.
# actual:   Path(None) raises TypeError -> uncaught 500 / voice-turn crash.
# ---------------------------------------------------------------------------------------


async def test_B18_write_code_projectless_none_root_returns_error_not_typeerror(tmp_path):
    from synapse.pipeline.app import build_host

    # Default config: kora_workspace_dir stays None (config.py:77). Voice keys are present so
    # build_host passes validate_voice_keys, but the workspace dir is deliberately unset.
    host = build_host(_fake_cfg(tmp_path, kora_workspace_dir=None))
    host.kora_runner = _FakeKoraRunner()

    t = host.threads.create("x")  # projectless: project_id is None
    assert not t.project_id

    try:
        res = await host.gate_action(t.id, "write_code", confirm=True)
    except TypeError as exc:
        pytest.fail(
            "B18: write_code on a projectless thread with kora_workspace_dir=None raised an "
            f"uncaught TypeError (Path(None)) instead of returning an error dict: {exc!r}"
        )

    assert isinstance(res, dict), f"B18: gate_action must return a dict, got {res!r}"
    assert res.get("error") == "no_plan_file", (
        "B18: with no plan file the projectless write_code must return the diagnosable "
        f"error dict {{'error': 'no_plan_file'}}, got {res!r}"
    )


# ---------------------------------------------------------------------------------------
# B21 -- failover BACK to tier0 double-counts the cost cap. strategy._advance calls
# record_paid_attempt for whatever tier it switches to (incl. tier0 when first_available
# returns 0), and the switcher counts tier0 AGAIN on the success end-frame -- so one
# failover-back-to-tier0 attempt increments the cap twice.
# location: synapse/pipeline/app.py:406-410 vs synapse/cascade/strategy.py:89-92
# expected: one paid tier0 attempt = one increment.
# actual:   two -> the cap trips prematurely, denying paid service before the real limit.
# ---------------------------------------------------------------------------------------


async def test_B21_failover_back_to_tier0_counts_exactly_once(tmp_path):
    from pipecat.frames.frames import LLMFullResponseEndFrame

    from synapse.pipeline.app import build_host, build_session_pipeline

    host = build_host(_fake_cfg(tmp_path))  # default cap 500 -- far above these counts
    session = build_session_pipeline(host)
    switcher = session.llm_switcher
    strategy = switcher.strategy
    services = switcher.services  # [tier0 (paid), tier1 (paid)]

    assert host.cost_cap.count == 0

    # Simulate a prior failover: we are currently on tier1 (the fallback tier).
    strategy._active_service = services[1]
    assert strategy.active_tier_index() == 1

    now = host.clock.now()
    # tier1 errors -> failover. The breaker's first_available(now) returns 0 (tier0's mute has
    # expired / it is available again), so _advance switches BACK to tier0 and records the
    # paid attempt (+1) -- the `idx==0 is only ever the initial tier` premise is false here.
    result = await strategy._advance(now)
    assert result is services[0] and strategy.active_tier_index() == 0, (
        f"setup: _advance should fail back to tier0, got result={result!r} "
        f"idx={strategy.active_tier_index()!r}"
    )
    assert host.cost_cap.count == 1, (
        f"setup: strategy._advance should have counted the tier0 attempt once, "
        f"count={host.cost_cap.count!r}"
    )

    # tier0 then SUCCEEDS: an LLMFullResponseEndFrame flows downstream with active tier == 0.
    end = LLMFullResponseEndFrame()
    end.processor = services[0]
    await switcher.push_frame(end)

    assert host.cost_cap.count == 1, (
        "B21: a single failover-back-to-tier0 attempt was counted TWICE -- once in "
        "strategy._advance (strategy.py:90) and again by the success end-frame "
        f"(app.py:409-410) -- count={host.cost_cap.count!r}; the cap trips prematurely."
    )
