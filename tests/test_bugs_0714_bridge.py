"""Regression tests for the 2026-07-14 live-test findings B23 and B24 (bugs.md § «сбор проблем»).

Two RED repro tests (xfail strict) assert the DESIRED post-fix behaviour that is currently
violated, plus two GREEN invariant tests that the future fix must NOT break.

- B23 (`synapse/bridge/state.py:263-279` `TaskStore.liveness`): idle/terminal Кора reported
  UNREACHABLE because age is measured unconditionally. Desired: no active task ⇒ OK.
  Invariant kept: a RUNNING task with a stale signal is a dead Кора mid-task ⇒ UNREACHABLE (R6).
- B24 (`synapse/bridge/kora.py` `_gate_decision`, outside_workspace-deny :727): owner order
  «везде она может писать» — Write/Edit/NotebookEdit allowed everywhere EXCEPT secret paths.
  Desired: a non-secret path outside the workspace ⇒ allowed. Invariant kept: a secret path
  stays denied (secret_path).
"""
from __future__ import annotations

import pytest

from synapse.bridge.kora import KoraRunner
from synapse.bridge.runspec import RunSpec
from synapse.bridge.state import (
    EventClass,
    KoraEvent,
    Liveness,
    SpeakLedger,
    TaskStatus,
    TaskStore,
)
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal


# =========================================================================================
# B23 — liveness must not report an idle/terminal Кора as UNREACHABLE
# =========================================================================================


def _terminal_store(event_type: str) -> TaskStore:
    """Store whose single task was driven to a terminal status by a Kora event far in the past
    (last_event_ts=0.0). Mirrors test_state.py: start_task RUNNING → apply_event(task_completed
    /task_failed) flips the status via _EVENT_STATUS and stamps last_event_ts."""
    store = TaskStore(FakeClock(0.0))
    store.start_task("t1", "з", TaskStatus.RUNNING, now=0.0)
    store.apply_event(
        KoraEvent(
            id="e1", type=event_type, cls=EventClass.CRITICAL,
            payload={}, speak_text=None, ts=0.0,
        )
    )
    return store


@pytest.mark.xfail(reason="B23: terminal (COMPLETED) task ⇒ idle Кора ⇒ liveness OK, not UNREACHABLE", strict=True)
def test_B23_completed_task_idle_is_ok_not_unreachable():
    store = _terminal_store("task_completed")
    assert store._task.status == TaskStatus.COMPLETED  # setup sanity: task really terminal
    # now = old(0.0) + 400 > unreachable_after_s=300 → today returns UNREACHABLE.
    live = store.liveness(now=400.0, stale_after_s=120, unreachable_after_s=300)
    assert live == Liveness.OK


@pytest.mark.xfail(reason="B23: terminal (FAILED) task ⇒ idle Кора ⇒ liveness OK, not UNREACHABLE", strict=True)
def test_B23_failed_task_idle_is_ok_not_unreachable():
    store = _terminal_store("task_failed")
    assert store._task.status == TaskStatus.FAILED  # setup sanity: task really terminal
    live = store.liveness(now=400.0, stale_after_s=120, unreachable_after_s=300)
    assert live == Liveness.OK


def test_B23_running_task_stale_signal_stays_unreachable():
    # R6 invariant (must survive the B23 fix): a RUNNING task whose last signal is ancient is a
    # dead Кора MID-TASK — the fix must NOT silence this. Only no-task/terminal becomes OK.
    store = TaskStore(FakeClock(0.0))
    store.start_task("t1", "з", TaskStatus.RUNNING, now=0.0)
    store.heartbeat(0.0)  # last_event_ts = 0.0, status still RUNNING
    live = store.liveness(now=400.0, stale_after_s=120, unreachable_after_s=300)
    assert live == Liveness.UNREACHABLE


# =========================================================================================
# B24 — write tools allowed everywhere except secret paths
# =========================================================================================


def _runner(tmp_path):
    clock = FakeClock(0.0)
    ws = tmp_path / "ws"
    cfg = SynapseConfig(kora_workspace_dir=str(ws), kora_deadline_s=900.0)
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "journal"), clock, session_id="s")
    runner = KoraRunner(cfg, store, SpeakLedger(), clock, journal, None)
    return runner, store, ws


async def _run_gate(tmp_path, gate_mode, probes):
    """Gate decisions taken WHILE a run is live (gate_mode snapshot set) — pattern from
    tests/test_gate_v2.py::_run_gate. Returns the list of (allowed, detail, category) tuples."""
    captured = {"results": []}
    runner, store, _ws = _runner(tmp_path)

    class FakeClient:
        def __init__(self, opts): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def query(self, text): pass
        async def receive_response(self):
            for tool, inp in probes:
                captured["results"].append(runner._gate_decision(tool, inp))
            if False:
                yield None

    runner._client_factory = lambda opts: FakeClient(opts)
    store.start_task("t1", "з", TaskStatus.RUNNING, 0.0)
    await runner._run("t1", "з", RunSpec(thread_id="th1", gate_mode=gate_mode))
    return captured["results"]


@pytest.mark.xfail(reason="B24: Write to a non-secret path outside the workspace must be allowed", strict=True)
async def test_B24_write_outside_workspace_nonsecret_is_allowed(tmp_path):
    # tmp_path.parent/helloworld.txt is outside ws (=tmp_path/ws) and NOT secret — the owner's
    # «везде она может писать». Today this hits the outside_workspace deny (kora.py:727).
    target = str(tmp_path.parent / "helloworld.txt")
    [res] = await _run_gate(tmp_path, "full", [("Write", {"file_path": target})])
    allowed, _detail, _category = res
    assert allowed is True


async def test_B24_write_to_secret_path_stays_denied(tmp_path):
    # secret write must stay denied after B24 fix (security invariant): secrets.yaml ∈
    # _SECRET_FILE_NAMES, so the pre-workspace secret check denies it regardless of location.
    target = str(tmp_path / "secrets.yaml")
    [res] = await _run_gate(tmp_path, "full", [("Write", {"file_path": target})])
    allowed, _detail, category = res
    assert allowed is False and category == "secret_path"
