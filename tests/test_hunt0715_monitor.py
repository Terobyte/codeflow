"""Hunt 2026-07-15 -- B-PIPE-3: monitor_forever's single `try` couples independent steps by
ORDER. `store.liveness()` runs BEFORE `cost_cap.maybe_reset(now)` in the same try block
(synapse/pipeline/app.py:272-297); a persistently-raising liveness never reaches the line
after it, so the daily cost-cap recovery (B30) silently never fires -- a tripped cap stays
tripped forever, even though B2 correctly keeps the loop itself alive.

These tests do NOT ask the loop to die (that is B2, already fixed and pinned by
tests/test_bughunt_w1_app_config.py::test_b2_monitor_forever_survives_a_raising_loop_body --
a transient failure must be survived). They ask for two additive fixes instead:

  1. cost_cap.maybe_reset must run independently of whether liveness() raised.
  2. a PERSISTENT failure streak must surface as one AlertKind.MONITOR_DEGRADED alert (already
     defined in synapse/journal.py) -- not zero (today), and not one per tick (anti-spam).

Both are RED today. Mirrors the bare-fakes + SynapseHost(...) construction style of
tests/test_bughunt_w1_app_config.py and tests/test_reported_bugs_failing.py -- no pipecat
task, no live server.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from synapse.journal import AlertKind
from synapse.pipeline.app import SynapseHost


# --------------------------------------------------------------------------------------------------
# Shared minimal fakes (only the members monitor_forever actually touches)
# --------------------------------------------------------------------------------------------------

class _Clock:
    """Monotonically increasing fake clock -- avoids any dependency on wall-clock time."""

    def __init__(self) -> None:
        self._t = 0.0

    def now(self) -> float:
        self._t += 1.0
        return self._t


class _NoopLedger:
    """speak_ledger stand-in: check() never raises and never has anything to report -- the
    test isolates the failure to store.liveness() (the step that sits BEFORE cost_cap in the
    body), exactly as the bug narrative describes."""

    def check(self, now, window_s):
        return []


class _AlwaysRaisingStore:
    """store.liveness stand-in that raises on EVERY tick -- a persistent failure, in contrast
    to B2's regression test which raises exactly once (transient) and expects a survival."""

    def __init__(self) -> None:
        self.liveness_calls = 0

    def liveness(self, now, stale_after_s, unreachable_after_s):
        self.liveness_calls += 1
        raise RuntimeError("persistent liveness backend failure")


class _RecordingCostCap:
    """cost_cap stand-in: records every maybe_reset(now) call so the test can assert it ran
    even while store.liveness() (the step before it in the loop body) keeps raising."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def maybe_reset(self, now: float) -> bool:
        self.calls.append(now)
        return False


class _RecordingJournal:
    """journal stand-in: records alert() calls -- the codebase's canonical observability hook,
    and the only place a monitor_forever failure streak could become visible (logger alone is
    not §8-evidence)."""

    def __init__(self) -> None:
        self.alerts: list[tuple] = []

    def alert(self, kind, detail=None) -> None:
        self.alerts.append((kind, detail))


class _MonitorCfg:
    """Tiny real heartbeat interval (mirrors test_b_pipe_3_monitor_forever_swallows_persistent_
    exceptions in tests/test_reported_bugs_failing.py) so many ticks elapse within a short,
    deterministic real sleep -- no monkeypatching of asyncio.sleep needed."""

    heartbeat_interval_s = 0.001
    critical_speak_window_s = 5.0
    stale_after_s = 120.0
    unreachable_after_s = 300.0


def _bare_host(*, journal, store, cost_cap) -> SynapseHost:
    return SynapseHost(
        clock=_Clock(),
        cfg=_MonitorCfg(),
        journal=journal,
        store=store,
        speak_ledger=_NoopLedger(),
        classifier=None,
        confirm_flow=None,
        arbiter_policy=None,
        bridge=None,
        handlers=None,
        breaker=None,
        cost_cap=cost_cap,
    )


async def _run_monitor_for_a_while(host: SynapseHost) -> None:
    """Start monitor_forever() as a task, let several heartbeat ticks elapse (real, but tiny,
    sleep -- heartbeat_interval_s is 0.001s so this is dozens of ticks), then cancel cleanly."""
    task = asyncio.create_task(host.monitor_forever())
    try:
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# --------------------------------------------------------------------------------------------------
# 1. cost_cap.maybe_reset must run even while liveness() keeps raising every single tick
# --------------------------------------------------------------------------------------------------

async def test_b_pipe_3_cost_cap_reset_survives_a_persistently_failing_liveness():
    """POST-FIX: cost_cap.maybe_reset(now) is the SOLE driver of the daily cost-cap recovery
    (B30) -- it must be called regardless of whether store.liveness() (the step immediately
    before it in the same try block) raised. Today the single `try` means one raising step
    silently disables everything after it in the body, forever, as long as the failure
    persists -- a tripped cap that never resets -> red at the `cost_cap.calls` assert."""
    store = _AlwaysRaisingStore()
    cost_cap = _RecordingCostCap()
    journal = _RecordingJournal()
    host = _bare_host(journal=journal, store=store, cost_cap=cost_cap)

    await _run_monitor_for_a_while(host)

    # Sanity: the loop really did survive and tick many times with liveness raising every time
    # (otherwise this would just be re-testing already-fixed B2, not B-PIPE-3).
    assert store.liveness_calls >= 5, (
        f"test setup too weak to exercise a persistent failure: only {store.liveness_calls} ticks"
    )

    assert cost_cap.calls, (
        "cost_cap.maybe_reset was never called while store.liveness() kept raising -- the daily "
        "cost-cap recovery (B30) is silently disabled for as long as liveness stays broken, "
        "which can be forever"
    )


# --------------------------------------------------------------------------------------------------
# 2. a persistent loop-body failure must surface as ONE journal alert, not zero and not one/tick
# --------------------------------------------------------------------------------------------------

async def test_b_pipe_3_persistent_failure_surfaces_as_a_journal_alert():
    """POST-FIX: once the loop body fails on consecutive ticks (not just a one-off transient
    blip), that must become one AlertKind.MONITOR_DEGRADED journal alert -- the only §8-style
    durable evidence that the Р-15г/Р-11 checks stopped running. It must fire ONCE per failure
    streak, not on every tick (anti-spam), even though this test drives dozens of ticks.

    Today nothing ever calls journal.alert(AlertKind.MONITOR_DEGRADED, ...) -- the failure is
    only ever logged -- so the alert list is empty -> red at the first assert."""
    store = _AlwaysRaisingStore()
    cost_cap = _RecordingCostCap()
    journal = _RecordingJournal()
    host = _bare_host(journal=journal, store=store, cost_cap=cost_cap)

    await _run_monitor_for_a_while(host)

    assert store.liveness_calls >= 5, (
        f"test setup too weak to exercise a persistent failure: only {store.liveness_calls} ticks"
    )

    degraded_alerts = [a for a in journal.alerts if a[0] == AlertKind.MONITOR_DEGRADED]

    assert degraded_alerts, (
        "no MONITOR_DEGRADED alert was raised despite a persistent monitor_forever failure "
        "streak -- the only evidence of the outage is a logger line, which is not durable "
        "§8-style evidence"
    )
    assert len(degraded_alerts) == 1, (
        f"expected exactly ONE MONITOR_DEGRADED alert for the whole failure streak (anti-spam), "
        f"got {len(degraded_alerts)} across {store.liveness_calls} failing ticks"
    )
