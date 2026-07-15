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


@pytest.mark.xfail(strict=True, reason="B-BRIDGE-6 доказан, не починен — см. bugs.md, Hunt 2026-07-15 (вечер)")
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


@pytest.mark.xfail(strict=True, reason="B-BRIDGE-7 доказан, не починен — см. bugs.md, Hunt 2026-07-15 (вечер)")
async def test_b_bridge_7_revise_stage_lies_about_active_task(tmp_path):
    """B-BRIDGE-7 (MAJOR): gate_action's `revise` branch (app.py:395-413) calls
    `set_stage(thread_id, "collect")` unconditionally — no `store.has_active_task()` check,
    unlike the launch branches right below it (app.py:421). The registry flags this
    design-tension (maybe revise should keep the run alive and only fix the bookkeeping, maybe
    it should cancel the run outright — the owner hasn't decided), so this test does NOT
    assert revise must cancel anything. It asserts the one thing that is unambiguously a lie
    no matter which fix is chosen: the thread must never simultaneously claim
    stage == "collect" (UI-4's own rule — collect means Kora is not running) while
    `store.has_active_task()` is True. Either fix (block revise while busy, or actually cancel
    the run) makes this combination impossible; today's code produces it on a routine
    sequence: launch, then revise before completion.
    """
    host = _gate_host(tmp_path)
    t = _propose(host)

    launch = await host.gate_action(t.id, "send_to_kora", confirm=True, fast=True, user_initiated=True)
    assert launch.get("ok") is True
    assert launch.get("stage") == "code"
    assert host.store.has_active_task() is True  # sanity: the run is genuinely active

    result = await host.gate_action(t.id, "revise", user_initiated=True)
    assert result.get("ok") is True  # revise "succeeds" per the current contract

    th = host.threads.get(t.id)
    # The lie: UI now shows stage "collect" ("сбор" — rules say Kora is not running) while the
    # store underneath still has the FakeRunner's task RUNNING.
    assert not (th.stage == "collect" and host.store.has_active_task())


# =============================================================================================
# B-BRIDGE-8 (MINOR) — ApprovalService: explicit deny indistinguishable from unclear
# =============================================================================================

def _approval_service(ttl=30.0):
    return ApprovalService(FakeClock(0.0), ttl, _AFFIRM, _DENY)


def _approval_digest(request_text="сделай X", action="send_to_kora", model=None, fast=False, stage="propose"):
    return gate_digest(request_text, action, model, fast, stage)


@pytest.mark.xfail(strict=True, reason="B-BRIDGE-8 доказан, не починен — см. bugs.md, Hunt 2026-07-15 (вечер)")
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


@pytest.mark.xfail(strict=True, reason="B-BRIDGE-9 доказан, не починен — см. bugs.md, Hunt 2026-07-15 (вечер)")
async def test_b_bridge_9_gate_decision_read_side_has_no_identity_guard(tmp_path):
    """B-BRIDGE-9 (MAJOR): `_run_root`/`_run_gate_mode` are plain KoraRunner instance fields,
    shared by every run. The WRITE side (the `finally` in `_run`, kora.py:512-517) is guarded
    by task identity (`if self._run_owner == task_id`) so a superseded run's teardown cannot
    clobber its successor's snapshot. The READ side — `_gate_decision` (kora.py:642-762),
    reached from the PreToolUse hook that is the whole containment boundary — has NO such
    guard: it takes no `task_id` at all and simply trusts whatever the fields hold at the
    moment it runs.

    Reproduced with the exact sequence the ledger names as reachable by "an ordinary pair of
    actions" (tools.py:309-318 request_cancel → kora.py:467-472 fire-and-forget cancel opens
    the window; a busy-check that already treats the old run as inactive lets a second launch
    start immediately, kora.py:497-518 unconditionally overwrites the snapshot). The
    interleaving is forced with explicit `asyncio.Event` synchronization — not a scheduling
    race — so the outcome is deterministic: task A's run is driven up to (and parked at) its
    first fully-snapshotted, in-flight point, then task B genuinely launches and reaches the
    same point, and ONLY THEN is a tool-call belonging to task A evaluated again.
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

    # Sanity: the decision an in-flight PreToolUse hook for task A is entitled to, RIGHT NOW —
    # a mutating write outside docs/ is denied because task A is docs_only.
    allowed_before, _, category_before = runner._gate_decision(
        "Write", {"file_path": str(root_a / "src" / "main.py")}
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

    # The exact same tool call that was correctly denied for task A a moment ago — standing in
    # for task A's in-flight PreToolUse hook still resolving its containment decision for a
    # tool call dispatched under task A — is evaluated again here.
    allowed_after, _, category_after = runner._gate_decision(
        "Write", {"file_path": str(root_a / "src" / "main.py")}
    )

    # CORRECT/documented behavior: a decision for task A's tool call must still be governed by
    # task A's OWN gate_mode (docs_only) — task B's launch must not be able to change what
    # task A is allowed to do. actual (bug): `_gate_decision` has no task_id/identity check, so
    # it reads task B's gate_mode ("full") instead and the SAME write flips to allowed.
    assert allowed_after is False
    assert category_after == "docs_only_violation"

    # Cleanup: release both parked runs so nothing is left dangling after the test.
    release_a.set()
    release_b.set()
    try:
        await asyncio.wait_for(first, 1.0)
    except asyncio.CancelledError:
        pass
    await asyncio.wait_for(second, 1.0)
