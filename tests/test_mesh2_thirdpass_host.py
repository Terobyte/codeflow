# -*- coding: utf-8-sig -*-
"""Red tests proving bugs B-M2-13, B-M2-14, B-M2-15, B-M2-19 from bugs.md
("🔫 Багхант МЕШ-2 (третий заход, `ca4ce0d`) — 2026-07-17").

Each test drives the REAL `SynapseHost` via `build_host()` (fake keys, never dials the
network), with `kora_runner` swapped for a small stub so the Claude Agent SDK is never touched.
No production code is modified by this file. Every assertion encodes the DOCUMENTED CORRECT
behavior from the ledger — it is the pass condition, and should flip green untouched once the
corresponding bug is fixed.

Run with `.venv/bin/python -m pytest tests/test_mesh2_thirdpass_host.py -q` from the repo root
(a bare `python3` lacks `pipecat` and gives a false ImportError at collection).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from synapse.bridge.state import AwaitingRequest, TaskStatus
from synapse.clock import FakeClock
from synapse.config import SynapseConfig


def _fake_cfg(tmp_path) -> SynapseConfig:
    """The standard fake-key cfg pattern used throughout the suite (test_bugs_0714_gatestate.py,
    test_mesh2_secondpass_failing.py) -- build_host() never dials the network with it."""
    return SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path), kora_workspace_dir=str(tmp_path / "ws"),
    )


class _FakeKoraRunner:
    """Stub KoraRunner (same shape as test_mesh2_secondpass_failing.py's): records start(...)
    calls, no SDK/network involved. Used for the gate-flow bugs (B-M2-14/15), which never park a
    consult question and so never need `.provide_answer`."""

    def __init__(self) -> None:
        self.starts: list[tuple] = []

    def start(self, task_id, text, spec) -> None:
        self.starts.append((task_id, text, spec))


class _StubRunner:
    """Minimal kora_runner for the consult-flow bugs (B-M2-13/19): `.start` records the RunSpec's
    run_kind (needed by consult_kora's busy-check `active_run_kind`), `.provide_answer` RECORDS
    every (request_id, text) delivery so a test can assert WHICH pending request an autonomous
    follow-up actually resolved (mirrors test_mesh2_secondpass_leak_failing.py's `_StubRunner`)."""

    active_run_kind = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.started: tuple | None = None

    def start(self, task_id, text, spec) -> None:
        self.active_run_kind = spec.run_kind
        self.started = (task_id, text, spec)

    def provide_answer(self, request_id, text):
        self.calls.append((request_id, text))
        return "answer_delivered"


class _StaleFollowupTextLoop:
    """Stub for `host.text_loop`. Models the ONE consequence of the real LLM follow-up turn that
    B-M2-13 is about: it eventually calls `consult_kora(thread_id, briefing_for_Q1, autonomous=
    True)`. `on_ingest` runs immediately before that call -- the exact sync point the bug report
    describes ("до завершения его LLM-хода... Кора паркует Q2") -- so the test can force the
    CURRENT pending to move to a different request (R2) at precisely the right moment, with no
    sleeping/polling/loop-and-hope."""

    def __init__(self, host, briefing, on_ingest) -> None:
        self._host = host
        self._briefing = briefing
        self._on_ingest = on_ingest
        self.results: list[dict] = []

    async def ingest_autonomous_turn(self, instruction, thread_id) -> None:
        self._on_ingest()
        result = await self._host.consult_kora(thread_id, self._briefing, autonomous=True)
        self.results.append(result)


# --------------------------------------------------------------------------------------------
# B-M2-13 -- stale autonomous consult follow-up misdelivers into a later pending request
# --------------------------------------------------------------------------------------------

async def test_b_m2_13_stale_autonomous_followup_misdelivers_to_new_pending(tmp_path):
    from synapse.pipeline.app import build_host

    host = build_host(_fake_cfg(tmp_path), FakeClock(1.0))
    runner = _StubRunner()
    host.kora_runner = runner

    thread = host.threads.create("idea")
    host.threads.set_stage(thread.id, "propose")

    # (1) Start a real consult session on thread T: task RUNNING, budget granted (cfg default 1).
    started = await host.consult_kora(thread.id, "сравни варианты кеша")
    assert started["outcome"] == "consult_started"
    task_id = host.store.task.id

    briefing_q1 = "нагрузка: 500 rps, латентность p99 200мс"
    r2_id = "r-Q2-b-m2-13"

    def _supersede_pending() -> None:
        # (3) Right before the stale follow-up's consult_kora(autonomous=True) call reaches the
        # store, the CURRENT pending moves to a DIFFERENT parked question (R2) on the SAME
        # thread/task -- e.g. the user answered Q1 directly and Kora immediately parked Q2.
        r2 = AwaitingRequest(
            1, r2_id, thread.id, task_id, "consult", "уточни Б", "формат Б", host.clock.now(),
        )
        host.store.set_awaiting(r2)

    host.text_loop = _StaleFollowupTextLoop(host, briefing_q1, on_ingest=_supersede_pending)

    # (2) Kora parks Q1 (R1) -- mirrors kora.py:987-1003 (`set_awaiting` then
    # `on_consult_parked`), which schedules the (now controlled, deterministic) follow-up turn.
    r1 = AwaitingRequest(
        1, "r-Q1-b-m2-13", thread.id, task_id, "consult", "уточни А", "формат А", host.clock.now(),
    )
    host.store.set_awaiting(r1)
    host.on_consult_parked(r1)

    pending = list(host._consult_followup_tasks)
    assert pending, "sanity: on_consult_parked scheduled the follow-up task"
    # Drive the scheduled follow-up deterministically -- this is the forced sync point, not a
    # sleep/poll: `_supersede_pending` runs INSIDE the follow-up coroutine, exactly at the moment
    # described in the ledger, right before it calls consult_kora(autonomous=True).
    await asyncio.gather(*pending)

    # (4) DOCUMENTED correct behavior (fix-note: "гвардить current.request_id == origin перед
    # provide_answer; безопасный no-op при мисматче"): an autonomous follow-up generated for R1
    # must never deliver its briefing into a request it wasn't generated for. R2 is a DIFFERENT
    # parked question (different request_id) on the same thread -- provide_answer(R2.request_id,
    # briefing_for_Q1) must not happen.
    assert (r2_id, briefing_q1) not in runner.calls, (
        "B-M2-13: the stale Q1 follow-up delivered its briefing into R2's (a later, unrelated "
        f"parked question) pending future instead of refusing/no-op. runner.calls={runner.calls!r}"
    )


# --------------------------------------------------------------------------------------------
# B-M2-14 -- propose during a LIVE run resurrects last_outcome="completed" under the new summary
# --------------------------------------------------------------------------------------------

async def test_b_m2_14_propose_during_live_run_resurrects_stale_plan(tmp_path):
    from synapse.pipeline.app import build_host

    host = build_host(_fake_cfg(tmp_path))
    host.kora_runner = _FakeKoraRunner()

    t = host.threads.create("x")
    host.voice_thread["id"] = t.id

    # (1) collect -> propose(R1).
    r1 = host.bridge.on_propose("запрос R1")
    assert r1.get("outcome") == "proposed"
    assert host.threads.get(t.id).stage == "propose"

    # (2) send_to_kora (non-fast) launches the docs/spec_plan run -- task becomes RUNNING and
    # STAYS running (no completion callback fires yet).
    res = await host.gate_action(t.id, "send_to_kora", confirm=True)
    assert res.get("ok") is True and host.threads.get(t.id).stage == "spec_plan"
    assert host.store.has_active_task() is True  # sanity: R1's run is still LIVE

    # (3) WHILE R1's run is still RUNNING, propose(R2). `_propose_for` has no busy-guard (unlike
    # `revise`), so this is accepted; it resets last_outcome (B-M2-10 fix) but does NOT move the
    # stage -- the thread stays in `spec_plan`, the same stage R1's eventual completion targets.
    r2 = host.bridge.on_propose("запрос R2")
    assert r2.get("outcome") == "proposed"
    th = host.threads.get(t.id)
    assert th.request_text == "запрос R2" and th.stage == "spec_plan"
    assert th.last_outcome is None  # sanity: the B-M2-10 reset did fire

    # (4) NOW R1's run finishes -- Kora wrote R1's plan file on disk, the store task completes,
    # and the run-finished callback fires for the docs_only run it actually was.
    root = Path(host.cfg.kora_workspace_dir)
    (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "plans" / f"{t.id}.md").write_text("план R1", encoding="utf-8")
    host.store.set_task_status(TaskStatus.COMPLETED)
    host._run_finished(t.id, "completed", "docs_only")

    # `finish_run`'s CAS guards only on STAGE. propose(R2) never moved the stage away from
    # "spec_plan", so R1's stale completion CAS-matches and resurrects last_outcome="completed"
    # under R2 -- despite step (3)'s reset.
    th = host.threads.get(t.id)

    # (5) write_code must refuse: R1's on-disk plan does not correspond to R2's request. Changing
    # the request summary while a run for the OLD summary is still in flight must not let that
    # run's completion re-arm write_code for the NEW summary.
    result = await host.gate_action(t.id, "write_code", confirm=True)

    assert result == {"error": "stale_plan"}, (
        "B-M2-14: propose_request(R2) while R1's docs run was still RUNNING left the stage "
        "unmoved (spec_plan); R1's completion then CAS-matched finish_run's stage-only guard and "
        "resurrected last_outcome='completed' under R2, so write_code launched R1's stale "
        f"on-disk plan under the new request R2 instead of refusing with stale_plan. "
        f"got {result!r} (last_outcome={th.last_outcome!r}, starts={host.kora_runner.starts!r})"
    )


# --------------------------------------------------------------------------------------------
# B-M2-15 -- propose on a terminal `done` thread wipes last_outcome with no path back
# --------------------------------------------------------------------------------------------

async def test_b_m2_15_propose_on_done_thread_bricks_it(tmp_path):
    from synapse.pipeline.app import build_host

    host = build_host(_fake_cfg(tmp_path))
    host.kora_runner = _FakeKoraRunner()

    t = host.threads.create("x")
    host.voice_thread["id"] = t.id

    # (1) collect -> propose(R1) -> send_to_kora(fast) -> code, task RUNNING.
    r1 = host.bridge.on_propose("запрос R1")
    assert r1.get("outcome") == "proposed"
    res = await host.gate_action(t.id, "send_to_kora", confirm=True, fast=True)
    assert res.get("ok") is True and host.threads.get(t.id).stage == "code"

    # (2) The run completes: code stage -> done, last_outcome="completed" (a legal, documented
    # terminal state -- `_STAGE_TRANSITIONS["code"]` allows "done" and `_run_finished` drives it).
    host.store.set_task_status(TaskStatus.COMPLETED)
    host._run_finished(t.id, "completed", "full")
    th = host.threads.get(t.id)
    assert th.stage == "done" and th.last_outcome == "completed"  # setup sanity

    # (3) The user keeps talking in the same thread; the dispatcher commits a new summary via
    # propose_request. `_propose_for` no longer gates on stage (the collect-only guard was
    # removed), so this is silently accepted even though `done` has NO outgoing transitions
    # (`_STAGE_TRANSITIONS["done"] == frozenset()`) -- there is no legal way back to `collect`.
    r2 = host.bridge.on_propose("запрос R2")
    th = host.threads.get(t.id)

    # DOCUMENTED correct behavior (ledger: "смена свода на треде... должна отклоняться... ИЛИ не
    # чистить completion, который не восстановить"): either propose is rejected on a terminal
    # stage, or its outcome-reset is skipped there -- either way `last_outcome` must NOT be wiped
    # to None on a thread with no path back to `collect` to ever recompute it. Losing it here is
    # unrecoverable: `revise` from `done` raises illegal_stage, and `send_to_kora`/`write_code`
    # CAS against a stage that can never be entered again.
    assert th.last_outcome == "completed", (
        "B-M2-15: propose_request on a terminal 'done' thread wiped last_outcome to "
        f"{th.last_outcome!r} instead of leaving it intact (or refusing the propose outright) -- "
        "'done' has no outgoing transitions, so this thread is now PERMANENTLY bricked "
        f"(no legal path back to collect can ever recompute last_outcome). propose result={r2!r}, "
        f"stage={th.stage!r}"
    )


# --------------------------------------------------------------------------------------------
# B-M2-19 -- consult supersede in the teardown window leaks session bookkeeping forever
# --------------------------------------------------------------------------------------------

async def test_b_m2_19_superseded_consult_session_bookkeeping_leaks_forever(tmp_path):
    """Forces the documented interleaving deterministically, without depending on real asyncio
    task scheduling nondeterminism or kora.py's SDK internals.

    The real race (kora.py:654-683): consult A parks past its idle timeout ->
    `store.request_cancel()` runs SYNCHRONOUSLY (kora.py:708), flipping the task's status to
    CANCEL_REQUESTED *before* A's owning `_run()` coroutine ever reaches its `finally` block
    (its first await after that point is `await stream_task`). `has_active_task()` is False the
    instant that status flip happens, so any concurrent `consult_kora`/`gate_action` (serialized
    only by `_launch_lock`, which the finally block does NOT hold) can `begin_task()` a new
    session B in that window, overwriting the store's single task slot. When A's finally block
    FINALLY runs, its identity guard (`task.id == task_id`) sees B's id, not A's, and skips
    calling `on_run_finished` for A entirely -- app.py's ONLY place that pops
    `_consult_session_threads`/`_consult_budget_remaining` (`_run_finished`, gate_mode="consult",
    app.py:486-489) then NEVER runs for A's task_id.

    This test reproduces that exact effect at the host level: it forces the synchronous half of
    the race (`request_cancel()` then a rival session B's `begin_task`) with explicit sync
    points, and asserts what the ledger says SHOULD still hold afterward -- A's bookkeeping is
    released, not left dangling forever because the callback that would have released it was
    (correctly, per the real identity guard) skipped for a superseded run.
    """
    from synapse.pipeline.app import build_host

    host = build_host(_fake_cfg(tmp_path), FakeClock(1.0))
    host.kora_runner = _StubRunner()

    thread_a = host.threads.create("idea A")
    host.threads.set_stage(thread_a.id, "propose")
    thread_b = host.threads.create("idea B")
    host.threads.set_stage(thread_b.id, "propose")

    # (1) Consult A starts: task RUNNING, session bookkeeping registered.
    started_a = await host.consult_kora(thread_a.id, "сравни варианты A")
    assert started_a["outcome"] == "consult_started"
    task_id_a = host.store.task.id
    assert host._consult_session_threads.get(task_id_a) == thread_a.id
    assert host._consult_budget_remaining.get(task_id_a, 0) > 0

    # (2) The idle-timeout watchdog's SYNCHRONOUS half (kora.py:708): request_cancel() flips
    # status before A's finally block has a chance to run its identity-guarded teardown.
    assert host.store.request_cancel() is True
    assert host.store.has_active_task() is False  # exactly the window the bug exploits

    # (3) In that window, a rival session B legally begins (has_active_task() is False) and
    # overwrites the store's single task slot -- mirrors kora.py's unguarded `begin_task`.
    started_b = await host.consult_kora(thread_b.id, "сравни варианты B")
    assert started_b["outcome"] == "consult_started"
    task_id_b = host.store.task.id
    assert task_id_b != task_id_a

    # Precondition: A's entries are STILL present -- the leak source is live (A's owning
    # `on_run_finished` never fires for a superseded run; we do not call it here either, matching
    # the real identity-guard skip).
    assert task_id_a in host._consult_session_threads
    assert task_id_a in host._consult_budget_remaining

    # (4) B's session ends normally -- the only teardown callback the real system will ever fire
    # for THIS interleaving (A's own teardown callback was skipped by the identity guard).
    host._run_finished(thread_b.id, "completed", "consult")

    # B's own bookkeeping is correctly released...
    assert task_id_b not in host._consult_session_threads
    assert task_id_b not in host._consult_budget_remaining

    # ...but DOCUMENTED correct behavior is that EVERY session's bookkeeping is eventually
    # released exactly once at its own teardown -- a superseded session must not depend on
    # thread reuse (the ledger's noted incidental self-heal path) to ever be cleaned up.
    assert task_id_a not in host._consult_session_threads, (
        "B-M2-19: consult A's session bookkeeping survived indefinitely after being superseded "
        "mid-teardown by consult B -- the finish_run(gate_mode='consult') teardown loop "
        "(app.py:486-489) only matches entries by thread ownership, and B's teardown never "
        f"touches A's entries. _consult_session_threads={host._consult_session_threads!r}"
    )
    assert task_id_a not in host._consult_budget_remaining, (
        f"B-M2-19: same permanent leak for _consult_budget_remaining={host._consult_budget_remaining!r}"
    )
