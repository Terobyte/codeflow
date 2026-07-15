"""Wave-1 bug-hunt regression tests (B1, B3, B14).

Each test asserts the CORRECT post-fix behavior, so it is RED on the frozen `a8dd919` tree
and turns green once the fix lands. See bugs-archive-2026-07-12.md for the full analysis of each ID.
"""
import pytest

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import EventClass, KoraEvent, TaskStatus, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.dispatcher.tools import KoraBridge, ToolHandlers
from synapse.journal import TurnJournal


def make_bridge(tmp_path):
    """A ToolHandlers wired to a real ConfirmFlow/TaskStore with capturing on_* callbacks —
    mirrors the make_handlers helper in tests/test_tools.py but also records on_task_committed
    and on_cancel so the destructive-relaunch path is observable."""
    clock = FakeClock(0.0)
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path), clock, session_id="w1")
    classifier = KeywordClassifier(cfg.destructive_keywords)
    confirm_flow = ConfirmFlow(
        store, clock, classifier, journal, cfg.affirm_words, cfg.deny_words,
        cfg.max_rereadbacks, cfg.confirm_timeout_s,
    )
    speaks: list[str] = []
    committed: list[tuple[str, str]] = []
    cancels: list[int] = []
    bridge = KoraBridge(
        store=store,
        confirm_flow=confirm_flow,
        clock=clock,
        cfg=cfg,
        on_speak=speaks.append,
        on_task_committed=lambda tid, text: committed.append((tid, text)),
        on_cancel=lambda: cancels.append(1),
    )
    handlers = ToolHandlers(bridge, journal)
    return handlers, store, confirm_flow, clock, journal, speaks, committed


@pytest.mark.asyncio
async def test_b1_cancelled_destructive_task_does_not_relaunch(tmp_path):
    """B1: a destructive task the user cancelled must not be resurrected to RUNNING nor
    launched by a following confirm_task. Currently request_cancel leaves ConfirmFlow._staged
    live and set_task_status has no CANCEL_REQUESTED guard, so confirm relaunches it -> RED."""
    handlers, store, confirm_flow, clock, journal, speaks, committed = make_bridge(tmp_path)

    # 1) destructive submit -> staged, PENDING_CONFIRMATION
    handlers.begin_turn("t1")
    await handlers.submit_task(text="удали старые бэкапы")
    assert store.task.status == TaskStatus.PENDING_CONFIRMATION

    # 2) user cancels: a real user turn happened, then the store flips to CANCEL_REQUESTED
    confirm_flow.note_user_turn("отмени", now=0.0)
    assert store.request_cancel() is True
    assert store.task.status == TaskStatus.CANCEL_REQUESTED

    # 3) user says "да" and the LLM (wrongly) tries to finish the confirm on the cancelled task
    confirm_flow.note_user_turn("да", now=0.0)
    handlers.begin_turn("t2")
    await handlers.confirm_task(decision="confirm")

    # CORRECT post-fix behavior: the cancelled destructive task must NOT resurrect to RUNNING,
    # and its producer must NOT be launched (on_task_committed must not fire).
    assert store.task.status != TaskStatus.RUNNING
    assert committed == []


def test_b3_completed_not_overwritten_by_later_failed():
    """B3: apply_event must not overwrite a terminal status. A task_failed arriving after
    task_completed must leave the status COMPLETED. Currently apply_event has no terminal
    guard (unlike set_task_status) so it flips to FAILED -> RED."""
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    store.start_task("t1", "собери отчёт", TaskStatus.RUNNING, now=0.0)

    completed = KoraEvent(
        id="e1", type="task_completed", cls=EventClass.CRITICAL,
        payload={}, speak_text="готово", ts=1.0,
    )
    store.apply_event(completed)
    assert store.task.status == TaskStatus.COMPLETED

    # A late / duplicate lifecycle event must NOT overwrite an already-terminal status.
    failed = KoraEvent(
        id="e2", type="task_failed", cls=EventClass.CRITICAL,
        payload={}, speak_text="ошибка", ts=2.0,
    )
    store.apply_event(failed)

    assert store.task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_b14_same_turn_different_args_not_deduped(tmp_path):
    """B14: the per-turn dedup latch keys on tool NAME only. Two submit_task calls with
    DIFFERENT args in one turn must both reach ConfirmFlow.submit; currently the second is
    swallowed and returns the first call's cached result -> RED."""
    handlers, store, confirm_flow, clock, journal, speaks, committed = make_bridge(tmp_path)

    submitted_texts: list[str] = []
    original_submit = confirm_flow.submit

    def spy_submit(text, now, thread_id=None):
        submitted_texts.append(text)
        return original_submit(text, now, thread_id=thread_id)

    confirm_flow.submit = spy_submit  # type: ignore[method-assign]

    handlers.begin_turn("t1")
    r1 = await handlers.submit_task(text="скачай книгу А")
    r2 = await handlers.submit_task(text="скачай книгу Б")

    # Different args in the same turn must NOT collapse: submit must run for BOTH texts,
    # and B must not be silently returned as A's cached result.
    assert submitted_texts == ["скачай книгу А", "скачай книгу Б"]
    assert r2 != r1
