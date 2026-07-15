from synapse.bridge.confirm import ConfirmDecisionOutcome, ConfirmFlow, ConfirmOutcome, KeywordClassifier
from synapse.bridge.state import TaskStatus, TaskStore
from synapse.clock import FakeClock
from synapse.journal import TurnJournal

AFFIRM = frozenset({"да", "подтверждаю", "делай"})
DENY = frozenset({"нет", "отмена", "стоп"})


def make_flow(tmp_path, max_rereadbacks=2, confirm_timeout_s=30.0):
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path), clock, session_id="t")
    classifier = KeywordClassifier({"удали", "снеси"})
    flow = ConfirmFlow(store, clock, classifier, journal, AFFIRM, DENY, max_rereadbacks, confirm_timeout_s)
    return flow, store, clock, journal


def test_submit_destructive_stages_with_template_readback(tmp_path):
    flow, store, clock, journal = make_flow(tmp_path)
    res = flow.submit("удали старые бэкапы", now=0.0)
    assert res.outcome == ConfirmOutcome.STAGED
    assert res.readback_text == 'Подтверди необратимую задачу: "удали старые бэкапы"'
    assert store.task.status == TaskStatus.PENDING_CONFIRMATION


def test_submit_nondestructive_commits_immediately(tmp_path):
    flow, store, clock, journal = make_flow(tmp_path)
    res = flow.submit("скачай книгу", now=0.0)
    assert res.outcome == ConfirmOutcome.COMMITTED
    assert store.task.status == TaskStatus.RUNNING


def test_submit_while_active_task_is_rejected(tmp_path):
    flow, store, clock, journal = make_flow(tmp_path)
    flow.submit("скачай книгу", now=0.0)
    res = flow.submit("сделай другое", now=1.0)
    assert res.outcome == ConfirmOutcome.REJECTED_ACTIVE
    assert res.reject_text


def test_confirm_without_user_turn_is_self_attempt_and_alerts(tmp_path):
    flow, store, clock, journal = make_flow(tmp_path)
    flow.submit("удали старые бэкапы", now=0.0)
    result = flow.confirm("confirm", now=1.0)
    assert result.outcome == ConfirmDecisionOutcome.REJECTED
    journal.close()
    lines = (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()
    alert_rows = [line for line in lines if '"CONFIRM_SELF_ATTEMPT"' in line]
    assert len(alert_rows) == 1


def test_confirm_affirm_commits(tmp_path):
    flow, store, clock, journal = make_flow(tmp_path)
    flow.submit("удали старые бэкапы", now=0.0, thread_id="t1")
    flow.note_user_turn("да, подтверждаю", now=1.0, thread_id="t1")
    result = flow.confirm("confirm", now=1.0, thread_id="t1")
    assert result.outcome == ConfirmDecisionOutcome.COMMITTED
    assert store.task.status == TaskStatus.RUNNING


def test_confirm_llm_and_affirm_mismatch_rejected(tmp_path):
    flow, store, clock, journal = make_flow(tmp_path)
    flow.submit("удали старые бэкапы", now=0.0)
    flow.note_user_turn("да", now=1.0)
    result = flow.confirm("deny", now=1.0)
    assert result.outcome == ConfirmDecisionOutcome.REJECTED
    assert store.task.status == TaskStatus.PENDING_CONFIRMATION


def test_confirm_deny_resets(tmp_path):
    flow, store, clock, journal = make_flow(tmp_path)
    flow.submit("удали старые бэкапы", now=0.0, thread_id="t1")
    flow.note_user_turn("нет", now=1.0, thread_id="t1")
    result = flow.confirm("deny", now=1.0, thread_id="t1")
    assert result.outcome == ConfirmDecisionOutcome.RESET
    assert store.task is None


def test_confirm_unclear_rereadbacks_then_resets_after_max(tmp_path):
    flow, store, clock, journal = make_flow(tmp_path, max_rereadbacks=2)
    flow.submit("удали старые бэкапы", now=0.0, thread_id="t1")

    flow.note_user_turn("шум", now=1.0, thread_id="t1")
    r1 = flow.confirm("confirm", now=1.0, thread_id="t1")
    assert r1.outcome == ConfirmDecisionOutcome.REREADBACK

    flow.note_user_turn("ещё шум", now=2.0, thread_id="t1")
    r2 = flow.confirm("confirm", now=2.0, thread_id="t1")
    assert r2.outcome == ConfirmDecisionOutcome.REREADBACK

    flow.note_user_turn("снова шум", now=3.0, thread_id="t1")
    r3 = flow.confirm("confirm", now=3.0, thread_id="t1")
    assert r3.outcome == ConfirmDecisionOutcome.RESET
    assert store.task is None


def test_confirm_timeout_resets(tmp_path):
    flow, store, clock, journal = make_flow(tmp_path, confirm_timeout_s=5.0)
    flow.submit("удали старые бэкапы", now=0.0, thread_id="t1")
    flow.note_user_turn("да", now=10.0, thread_id="t1")  # after the 5s window
    result = flow.confirm("confirm", now=10.0, thread_id="t1")
    assert result.outcome == ConfirmDecisionOutcome.RESET


def test_confirm_with_nothing_staged_is_rejected(tmp_path):
    flow, store, clock, journal = make_flow(tmp_path)
    result = flow.confirm("confirm", now=0.0)
    assert result.outcome == ConfirmDecisionOutcome.REJECTED


def test_confirm_is_scoped_to_the_conversation_that_staged_it(tmp_path):
    """B-BRIDGE-6: one ConfirmFlow serves every thread and both channels — a destructive task
    staged by conversation A must be confirmable ONLY by A. A foreign conversation's OWN
    ordinary turn (its own "да"/"нет") must not feed A's double-key: it must not launch A's
    task, and it must not reset it either — a stranger's "no" is not the owner's decision.
    Only A's own turn may actually commit it."""
    flow, store, clock, journal = make_flow(tmp_path)

    # A stages a destructive task -> PENDING_CONFIRMATION, awaiting A's own turn.
    submit_res = flow.submit("удали старые бэкапы", now=0.0, thread_id="A")
    assert submit_res.outcome == ConfirmOutcome.STAGED
    assert flow.staged is not None
    assert flow.staged.awaiting_user_turn is True

    # B has its own, unrelated turn in the same window ("да") — this must NOT count as A's
    # answer, and B's confirm must not commit A's task.
    flow.note_user_turn("да", now=1.0, thread_id="B")
    foreign_affirm = flow.confirm("confirm", now=1.0, thread_id="B")
    assert foreign_affirm.outcome == ConfirmDecisionOutcome.REJECTED
    assert store.task is not None
    assert store.task.status == TaskStatus.PENDING_CONFIRMATION
    assert flow.staged is not None
    assert flow.staged.task_id == submit_res.task_id
    assert flow.staged.awaiting_user_turn is True  # B's turn must not have cleared A's key

    # C's own "нет" must not reset A's task either — _reset is the OWNER's decision, and a
    # stranger declining on A's behalf is just as illegitimate as a stranger confirming.
    flow.note_user_turn("нет", now=1.5, thread_id="C")
    foreign_deny = flow.confirm("deny", now=1.5, thread_id="C")
    assert foreign_deny.outcome == ConfirmDecisionOutcome.REJECTED
    assert store.task is not None
    assert store.task.status == TaskStatus.PENDING_CONFIRMATION
    assert flow.staged is not None

    # Positive half: A's own turn actually confirms it — proves the guard discriminates
    # "foreign" from "owner" rather than just rejecting everything.
    flow.note_user_turn("да, подтверждаю", now=2.0, thread_id="A")
    own = flow.confirm("confirm", now=2.0, thread_id="A")
    assert own.outcome == ConfirmDecisionOutcome.COMMITTED
    assert own.task_id == submit_res.task_id
    assert store.task.status == TaskStatus.RUNNING
