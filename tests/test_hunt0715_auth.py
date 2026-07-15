# -*- coding: utf-8-sig -*-
"""Hunt 2026-07-15 (вечер), Фаза 0: auth + money — red tests for B-BRIDGE-6/7/8/9.

Each test proves the ledger entry in bugs.md (section "Hunt 2026-07-15 (вечер) — Фаза 0:
auth + money"), written so the DOCUMENTED CORRECT behavior is the assertion — the test must
go green once the underlying bug is fixed, without editing the assertions themselves.

Tree frozen at 058faf2 per the hunt brief.
"""
from __future__ import annotations

import asyncio
import pytest

from synapse.bridge.approvals import ApprovalService, gate_digest
from synapse.bridge.confirm import ConfirmDecisionOutcome, ConfirmFlow, KeywordClassifier
from synapse.bridge.kora import KoraRunner
from synapse.bridge.runspec import RunSpec
from synapse.bridge.state import SpeakLedger, TaskStatus, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal

_AFFIRM = frozenset({"да", "подтверждаю", "делай"})
_DENY = frozenset({"нет", "отмена", "стоп"})


# =============================================================================================
# B-BRIDGE-6 (CRIT) — ConfirmFlow: double-key confirm not thread-scoped
# =============================================================================================

def _confirm_flow(tmp_path, max_rereadbacks=2, confirm_timeout_s=30.0):
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path), clock, session_id="hunt0715")
    classifier = KeywordClassifier({"удали", "снеси"})
    flow = ConfirmFlow(store, clock, classifier, journal, _AFFIRM, _DENY, max_rereadbacks, confirm_timeout_s)
    return flow, store, clock, journal


def test_b_bridge_6_confirm_flow_not_thread_scoped(tmp_path):
    """B-BRIDGE-6 (CRIT): ConfirmFlow has no `thread_id` anywhere in its API — `_staged` and
    `_last_user_turn_transcript` are single process-wide fields (confirm.py:103-106, 152-158).
    Contrast: ApprovalService, the sibling double-key mechanism guarding gate_action, keys
    everything per-thread with a monotonic watermark (approvals.py:88-94/138) — proven by
    test_phase0_approval.py::test_note_user_turn_is_thread_scoped. ConfirmFlow guards an
    equally dangerous path (destructive tasks) with none of that protection: ONE ConfirmFlow
    instance is handed to bridge/http_bridge/text_loop/host (app.py:571-574/778/797/840/867) —
    the same Python object serves every thread/channel.

    Reachability: thread A's request stages a destructive task, awaiting THAT conversation's
    own confirming turn. A completely unrelated conversation (thread B) has its own ordinary
    turn within the confirm window — ConfirmFlow cannot tell the two apart: `note_user_turn`
    clears the self-attempt guard and overwrites the transcript `confirm()` classifies, using
    thread B's words to decide thread A's pending destructive task.
    """
    flow, store, clock, journal = _confirm_flow(tmp_path)

    # Thread A: a destructive request gets staged, awaiting THIS conversation's own confirming
    # turn (Р-16 double-key: readback, then a genuine intervening user turn).
    flow.submit("удали старые бэкапы", now=0.0)
    assert store.task.status == TaskStatus.PENDING_CONFIRMATION
    assert flow.staged.awaiting_user_turn is True

    # Thread B: an unrelated conversation's ordinary turn lands in the same confirm window.
    # Nothing here ties this transcript to thread A's staged destructive task.
    flow.note_user_turn("да, давай", now=1.0)

    # Some LLM turn (voice or HTTP, thread A or thread B — ConfirmFlow structurally cannot
    # distinguish) now calls confirm_task(decision="confirm").
    result = flow.confirm("confirm", now=1.0)

    # CORRECT/documented behavior: a turn belonging to a DIFFERENT conversation must not be
    # able to supply either half of the double-key for a task it never staged. Thread A's
    # destructive task must still be sitting there, waiting for ITS OWN user's turn — exactly
    # the guarantee ApprovalService already gives its threads.
    # actual (bug): the self-attempt guard was cleared by the unrelated turn and the
    # affirm-check reads that same unrelated transcript, so the destructive task launches.
    assert result.outcome == ConfirmDecisionOutcome.REJECTED
    assert store.task is not None
    assert store.task.status == TaskStatus.PENDING_CONFIRMATION


# =============================================================================================
# B-BRIDGE-7 (MAJOR) — gate_action(revise) lies about active-task state
# =============================================================================================

def _gate_host(tmp_path):
    from synapse.config import SynapseConfig as _Cfg
    from synapse.pipeline.app import build_host

    cfg = _Cfg(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path), kora_workspace_dir=str(tmp_path / "ws"),
        confirm_timeout_s=30.0,
    )
    host = build_host(cfg)

    class _FakeRunner:
        def __init__(self):
            self.starts = []

        def start(self, task_id, text, spec):
            self.starts.append((task_id, text, spec))

    host.kora_runner = _FakeRunner()
    return host


def _propose(host):
    t = host.threads.create("x")
    host.threads.set_stage(t.id, "propose")
    host.threads.set_request(t.id, "сделай штуку")
    return t


async def test_b_bridge_7_revise_stage_lies_about_active_task(tmp_path):
    """B-BRIDGE-7 (MAJOR): gate_action's `revise` branch (app.py:395-413) used to call
    `set_stage(thread_id, "collect")` unconditionally — no `store.has_active_task()` check,
    unlike the launch branches right below it (app.py:421). The registry flagged a genuine
    design-tension the OWNER had to resolve — revise could keep the run alive and only fix the
    bookkeeping (block while busy), or it could actually cancel the run outright — so this test
    does NOT pin which of the two the fix picks, and deliberately does NOT assert anything
    about what `revise` *returns*: an unconditional `result.get("ok") is True` would itself
    outlaw the "block" branch, since a legitimately blocked revise answers
    `{"error": "busy"}` and has no `ok` key at all. It asserts only the one INVARIANT that is
    unambiguously a lie no matter which fix is chosen: the thread must never simultaneously
    claim stage == "collect" (UI-4's own rule — collect means Kora is not running) while
    `store.has_active_task()` is True. Both legal fixes make this combination impossible
    (block: stage never moves off "code" while busy; cancel: the run stops being active
    before/as stage moves to "collect"); the pre-fix code violated it on a routine sequence:
    launch, then revise before completion.
    """
    host = _gate_host(tmp_path)
    t = _propose(host)

    launch = await host.gate_action(t.id, "send_to_kora", confirm=True, fast=True, user_initiated=True)
    assert launch.get("ok") is True
    assert launch.get("stage") == "code"
    assert host.store.has_active_task() is True  # sanity: the run is genuinely active

    await host.gate_action(t.id, "revise", user_initiated=True)

    th = host.threads.get(t.id)
    # The invariant, independent of which fix `revise` implements: the thread must never claim
    # stage "collect" ("сбор" — rules say Kora is not running) while the store underneath still
    # has an active task RUNNING.
    assert not (th.stage == "collect" and host.store.has_active_task())


# =============================================================================================
# B-BRIDGE-8 (MINOR) — ApprovalService: explicit deny indistinguishable from unclear
# =============================================================================================

def _approval_service(ttl=30.0):
    return ApprovalService(FakeClock(0.0), ttl, _AFFIRM, _DENY)


def _approval_digest(request_text="сделай X", action="send_to_kora", model=None, fast=False, stage="propose"):
    return gate_digest(request_text, action, model, fast, stage)


def test_b_bridge_8_deny_is_indistinguishable_from_unclear():
    """B-BRIDGE-8 (MINOR): ApprovalService.consume() folds `deny` and `unclear` into the same
    `None` (approvals.py:140-144) — there is no "rejected" transition at all. Sibling
    ConfirmFlow.confirm() (confirm.py:188-189) treats an explicit deny as a hard RESET,
    clearing the staged task outright. Here an explicit "нет" leaves the SAME pending sitting
    untouched — indistinguishable from having said nothing coherent — so a later, unrelated
    affirmative turn against that same never-cancelled pending still launches the very action
    the user already refused.
    """
    svc = _approval_service()
    svc.stage("th", "send_to_kora", _approval_digest(), now=1.0)

    # Explicit refusal.
    svc.note_user_turn("th", "нет, отмена", now=2.0)
    assert svc.consume("th", "send_to_kora", _approval_digest(), now=3.0) is None

    # CORRECT/documented behavior: the "нет" above should have extinguished the pending
    # outright, like ConfirmFlow._reset does for its sibling mechanism — no amount of later
    # affirmation should be able to resurrect THIS staged approval without a fresh
    # stage()/readback. A subsequent, unrelated "да" must find nothing left to consume.
    # actual (bug): consume() never popped the pending on deny, so the watermark/digest still
    # match and the later "да" launches it.
    svc.note_user_turn("th", "да, делай", now=4.0)
    assert svc.consume("th", "send_to_kora", _approval_digest(), now=5.0) is None


# =============================================================================================
# B-BRIDGE-9 (MAJOR) — KoraRunner: read-side gate decision has no identity-guard
# =============================================================================================

class SystemMessage:
    """Minimal fake SDK message — duck-typed on class NAME by the mapper, matches
    tests/test_kora.py's own fixture class."""

    def __init__(self, subtype, data=None):
        self.subtype = subtype
        self.data = data or {}


def _make_runner(tmp_path, client_factory):
    clock = FakeClock(0.0)
    ws = tmp_path / "ws"
    cfg = SynapseConfig(kora_workspace_dir=str(ws), kora_deadline_s=900.0)
    store = TaskStore(clock)
    ledger = SpeakLedger()
    journal = TurnJournal(str(tmp_path / "journal"), clock, session_id="hunt0715")
    speaks: list[str] = []
    runner = KoraRunner(cfg, store, ledger, clock, journal, speaks.append, client_factory=client_factory)
    return runner, store


def _sequential_client_factory(gen_funcs):
    """Returns a different fake client per successive `_build_options`/`_stream` call — the
    Nth call to `start()` (in order) gets `gen_funcs[N-1]`. Same shape as
    tests/test_kora.py's `_client_factory`, generalized to two distinct runs sharing ONE
    KoraRunner instance (the exact scenario B-BRIDGE-9 describes: a superseding launch)."""
    calls = {"n": 0}

    class _FakeClient:
        def __init__(self, gen_func):
            self._gen_func = gen_func

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, prompt, session_id="default"):
            return None

        def receive_response(self):
            return self._gen_func()

    def factory(opts):
        idx = calls["n"]
        calls["n"] += 1
        return _FakeClient(gen_funcs[idx])

    return factory


async def test_b_bridge_9_gate_decision_read_side_has_no_identity_guard(tmp_path):
    """B-BRIDGE-9 (MAJOR): `_run_root`/`_run_gate_mode` are plain KoraRunner instance fields,
    shared by every run. The WRITE side (the `finally` in `_run`, kora.py:513-518) is guarded
    by task identity (`if self._run_owner == task_id`) so a superseded run's teardown cannot
    clobber its successor's snapshot. The READ side — `_gate_decision` (kora.py:648-782),
    reached from the PreToolUse hook that is the whole containment boundary — used to have NO
    such guard at all: it took no `task_id` and simply trusted whatever the fields held at the
    moment it ran.

    Fixed shape: `_gate_decision(tool_name, tool_input, task_id=None)` now fail-closes any call
    that carries the identity of a run which no longer owns the snapshot (`task_id=None` stays
    exempt — that is a bare unit-call of the predicate, not a real tool dispatch). A REAL tool
    call always carries its own run's identity: `_build_options` binds it once per run via
    `functools.partial(self._pretool_hook, task_id=task_id)`, and `_pretool_hook` forwards it
    into `_gate_decision`. So the correct assertion here is NOT "a call with no task_id is still
    judged by task A's docs_only rules" — an identity-less call structurally cannot know whose
    call it is, and no fix could make it resolve to a particular run's rules. The correct
    assertion is: a call carrying task A's identity, made AFTER task B's launch overwrote the
    shared snapshot, must be denied outright as `superseded_run` — never silently evaluated
    under task B's `full`/`root_b` rules (that would wrongly ALLOW it), and never resurrected
    under task A's OWN stale `docs_only`/`root_a` rules either (a superseded run has no rules
    left, period — the write may or may not have been legal for the task that no longer owns
    the run, and that question no longer matters).

    Reproduced with the exact sequence the ledger names as reachable by "an ordinary pair of
    actions" (tools.py:309-318 request_cancel → kora.py:467-472 fire-and-forget cancel opens
    the window; a busy-check that already treats the old run as inactive lets a second launch
    start immediately, kora.py:497-518 unconditionally overwrites the snapshot). The
    interleaving is forced with explicit `asyncio.Event` synchronization — not a scheduling
    race — so the outcome is deterministic: task A's run is driven up to (and parked at) its
    first fully-snapshotted, in-flight point, then task B genuinely launches and reaches the
    same point, and ONLY THEN is a tool-call carrying task A's identity evaluated again.
    """
    reached_a = asyncio.Event()
    release_a = asyncio.Event()
    reached_b = asyncio.Event()
    release_b = asyncio.Event()

    async def gen_a():
        yield SystemMessage("init", {})
        reached_a.set()
        await release_a.wait()

    async def gen_b():
        yield SystemMessage("init", {})
        reached_b.set()
        await release_b.wait()

    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"

    runner, store = _make_runner(tmp_path, _sequential_client_factory([gen_a, gen_b]))

    # Task A: a docs_only run rooted at root_a.
    store.start_task("taskA", "spec+plan only", TaskStatus.RUNNING, 0.0)
    runner.start("taskA", "spec+plan only", RunSpec(thread_id="T1", project_root=str(root_a), gate_mode="docs_only"))
    first = runner._active
    await asyncio.wait_for(reached_a.wait(), 1.0)

    # Task A's `_run()` has snapshotted its own launch parameters.
    assert runner._run_root == root_a
    assert runner._current_gate_mode() == "docs_only"

    # Sanity: the decision an in-flight PreToolUse hook for task A is entitled to, RIGHT NOW,
    # carrying task A's OWN identity — a mutating write outside docs/ is denied because task A
    # is docs_only.
    allowed_before, _, category_before = runner._gate_decision(
        "Write", {"file_path": str(root_a / "src" / "main.py")}, task_id="taskA",
    )
    assert allowed_before is False
    assert category_before == "docs_only_violation"

    # Cancel + supersede: task A's run is requested-cancel'd (fire-and-forget — its `finally`
    # has NOT run yet, it is parked on release_a) and a second, unrelated run for task B is
    # launched immediately — exactly the busy-check window the ledger describes.
    runner.request_cancel()
    store.request_cancel()
    store.start_task("taskB", "full run", TaskStatus.RUNNING, 0.0)
    runner.start("taskB", "full run", RunSpec(thread_id="T2", project_root=str(root_b), gate_mode="full"))
    second = runner._active
    await asyncio.wait_for(reached_b.wait(), 1.0)

    # Task B's `_run()` has now snapshotted ITS OWN launch parameters into the SAME instance
    # fields task A's in-flight hook would read.
    assert runner._run_root == root_b
    assert runner._current_gate_mode() == "full"

    # The exact same call, still carrying task A's identity, made again NOW — standing in for
    # task A's in-flight PreToolUse hook still resolving its containment decision for a tool
    # call dispatched under task A.
    allowed_after, _, category_after = runner._gate_decision(
        "Write", {"file_path": str(root_a / "src" / "main.py")}, task_id="taskA",
    )

    # CORRECT/documented behavior: a run that lost ownership of the snapshot has no rules left
    # to be judged by — neither task B's ("full"/root_b, which would wrongly ALLOW the write)
    # nor a silent fallback to its own stale ("docs_only"/root_a) rules. It must be denied as
    # superseded, full stop. actual (bug, pre-fix): `_gate_decision` had no task_id/identity
    # check at all, so it read task B's gate_mode ("full") for a path outside task B's root and
    # ALLOWED the write outright.
    assert allowed_after is False
    assert category_after == "superseded_run"

    # The real containment boundary is the PreToolUse hook, not the bare predicate — prove the
    # SAME denial through it end-to-end, exactly as a real tool dispatch would reach it.
    hook_result = await runner._pretool_hook(
        {"tool_name": "Write", "tool_input": {"file_path": str(root_a / "src" / "main.py")}},
        None, None, task_id="taskA",
    )
    hook_out = hook_result["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    assert hook_out["permissionDecisionReason"] == "superseded_run"

    # Cleanup: release both parked runs so nothing is left dangling after the test.
    release_a.set()
    release_b.set()
    try:
        await asyncio.wait_for(first, 1.0)
    except asyncio.CancelledError:
        pass
    await asyncio.wait_for(second, 1.0)
