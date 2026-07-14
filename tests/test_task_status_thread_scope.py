"""Скоуп терминальной задачи к треду-владельцу (баг Теро 2026-07-14): диспетчер в КАЖДОМ
треде повторял «задача выполнена, Кора смотрела проект agentx час назад», хотя тред новый.

Корень: TaskStore — глобальный синглтон (один Кора, одна задача в state.json). И [СОСТОЯНИЕ]
(loop._render_state → render_state), и get_task_status (→ snapshot) отдавали эту одну задачу
не глядя на тред. Завершённая задача «залипала» как глобальный статус и текла во все разговоры.

Фикс: should_hide_task прячет COMPLETED/FAILED задачу из ЧУЖОГО треда (owner != asking); в
родном треде она видна. Активная задача (RUNNING/PENDING_CONFIRMATION) остаётся глобальной —
Кора реально занята. Осиротевшая терминальная (owner=None) прячется ото всех (стейл-остаток).
"""
from __future__ import annotations

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import TaskStatus, TaskStore, should_hide_task
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.dispatcher.tools import KoraBridge, ToolHandlers
from synapse.journal import TurnJournal
from synapse.threads import ThreadStore


def _completed_store(task_id: str = "t1", text: str = "опиши agentx") -> TaskStore:
    store = TaskStore(FakeClock(0.0))
    store.start_task(task_id, text, TaskStatus.COMPLETED, now=0.0)
    return store


# ── Предикат should_hide_task (чистый) ──────────────────────────────────────────────────────
def test_predicate_terminal_hidden_from_foreign_thread():
    store = _completed_store()
    assert should_hide_task(store.task, asking_thread_id="B", owner_thread_id="A") is True


def test_predicate_terminal_visible_in_owner_thread():
    store = _completed_store()
    assert should_hide_task(store.task, asking_thread_id="A", owner_thread_id="A") is False


def test_predicate_failed_also_scoped():
    store = TaskStore(FakeClock(0.0))
    store.start_task("t1", "задача", TaskStatus.FAILED, now=0.0)
    assert should_hide_task(store.task, asking_thread_id="B", owner_thread_id="A") is True


def test_predicate_active_task_stays_global():
    # RUNNING/PENDING_CONFIRMATION — Кора реально занята; статус честно виден во всех тредах.
    for status in (TaskStatus.RUNNING, TaskStatus.PENDING_CONFIRMATION):
        store = TaskStore(FakeClock(0.0))
        store.start_task("t1", "задача", status, now=0.0)
        assert should_hide_task(store.task, asking_thread_id="B", owner_thread_id="A") is False


def test_predicate_none_task_never_hidden():
    assert should_hide_task(None, asking_thread_id="B", owner_thread_id="A") is False


def test_predicate_orphan_terminal_hidden_from_any_thread():
    # owner=None (стейл-остаток из state.json, тред не резолвится) — прячем ото всех тредов.
    store = _completed_store()
    assert should_hide_task(store.task, asking_thread_id="B", owner_thread_id=None) is True


def test_predicate_orphan_terminal_no_asking_thread_shows():
    # Транзиент: войс без треда (asking=None) и owner ещё не привязан — None==None, не прячем.
    store = _completed_store()
    assert should_hide_task(store.task, asking_thread_id=None, owner_thread_id=None) is False


# ── store.render_state / snapshot: hide_task рендерит «нет задачи» ────────────────────────────
def test_render_state_hidden_reports_no_task():
    store = _completed_store(text="секрет чужого треда")
    hidden = store.render_state(now=1.0, stale_after_s=120, unreachable_after_s=300, hide_task=True)
    assert "Активной задачи нет" in hidden
    assert "секрет чужого треда" not in hidden
    # контроль: без hide_task та же задача видна
    shown = store.render_state(now=1.0, stale_after_s=120, unreachable_after_s=300)
    assert "секрет чужого треда" in shown


def test_snapshot_hidden_reports_no_task():
    store = _completed_store()
    assert store.snapshot(1.0, 120, 300, hide_task=True)["task"] is None
    assert store.snapshot(1.0, 120, 300)["task"] is not None


# ── loop._render_state: терминальная задача чужого треда исчезает из [СОСТОЯНИЕ] ──────────────
def _loop(store: TaskStore, owner_thread_for) -> DispatcherTurnLoop:
    # _render_state трогает только store/cfg/owner_thread_for — остальные зависимости-заглушки.
    return DispatcherTurnLoop(
        None, None, None, store, None, FakeClock(0.0), SynapseConfig(),
        owner_thread_for=owner_thread_for,
    )


def test_loop_state_block_hides_completed_task_in_foreign_thread():
    store = _completed_store(text="разобралась с проектом agentx")
    loop = _loop(store, owner_thread_for=lambda tid: "A")  # задача принадлежит треду A
    foreign = loop._render_state(now=1.0, thread_id="B")
    assert "Активной задачи нет" in foreign
    assert "agentx" not in foreign
    owner = loop._render_state(now=1.0, thread_id="A")
    assert "agentx" in owner


def test_loop_state_block_no_resolver_keeps_legacy_global():
    # owner_thread_for не проведён (старая проводка/тест) → поведение как раньше, задача видна.
    store = _completed_store(text="agentx")
    loop = _loop(store, owner_thread_for=None)
    assert "agentx" in loop._render_state(now=1.0, thread_id="B")


# ── tools.get_task_status: зеркало для tool-пути ─────────────────────────────────────────────
def _bridge_with_threads(tmp_path, store, current):
    clock = FakeClock(0.0)
    cfg = SynapseConfig()
    journal = TurnJournal(str(tmp_path), clock, session_id="s")
    classifier = KeywordClassifier(cfg.destructive_keywords)
    confirm_flow = ConfirmFlow(
        store, clock, classifier, journal, cfg.affirm_words, cfg.deny_words,
        cfg.max_rereadbacks, cfg.confirm_timeout_s,
    )
    threads = ThreadStore(clock, tmp_path / "threads")
    bridge = KoraBridge(
        store=store, confirm_flow=confirm_flow, clock=clock, cfg=cfg,
        threads=threads, thread_id_for=lambda: current["id"],
    )
    return ToolHandlers(bridge, journal), threads, journal


async def test_get_task_status_hides_completed_task_in_foreign_thread(tmp_path):
    store = _completed_store(task_id="t1", text="проект agentx описан")
    current = {"id": None}
    handlers, threads, journal = _bridge_with_threads(tmp_path, store, current)
    owner = threads.create("A")
    threads.append_task(owner.id, "t1")   # задача t1 принадлежит треду owner
    other = threads.create("B")

    # Из родного треда — задача видна.
    current["id"] = owner.id
    journal.begin_turn("статус?"); handlers.begin_turn("x")
    assert (await handlers.get_task_status())["task"] is not None

    # Из чужого треда — задачи не видно.
    current["id"] = other.id
    journal.begin_turn("что там?"); handlers.begin_turn("y")
    assert (await handlers.get_task_status())["task"] is None
