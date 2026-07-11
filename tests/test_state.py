from synapse.bridge.state import EventClass, KoraEvent, Liveness, TaskStatus, TaskStore, parse_event
from synapse.clock import FakeClock
from synapse.prompt import CANON_PHRASE_STALE_KORA


def test_parse_event_fail_safe_defaults_to_critical():
    ev = parse_event({"type": "task_completed", "payload": {"file": "x"}, "ts": 1.0})
    assert ev.cls == EventClass.CRITICAL
    ev2 = parse_event({"type": "task_completed", "class": "not-a-real-class", "ts": 1.0})
    assert ev2.cls == EventClass.CRITICAL


def test_parse_event_honors_explicit_narratable():
    ev = parse_event({"type": "progress", "class": "narratable", "ts": 1.0})
    assert ev.cls == EventClass.NARRATABLE


def test_render_state_redacts_critical_and_keeps_narratable():
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    store.start_task("t1", "удали старое", TaskStatus.RUNNING, now=0.0)
    critical = KoraEvent(
        id="e1", type="task_completed", cls=EventClass.CRITICAL,
        payload={"file": "secret.txt"}, speak_text="готово", ts=1.0,
    )
    narratable = KoraEvent(
        id="e2", type="progress", cls=EventClass.NARRATABLE,
        payload={"stage": "загрузка"}, speak_text=None, ts=2.0,
    )
    store.apply_event(critical)
    store.apply_event(narratable)

    rendered = store.render_state(now=3.0, stale_after_s=120, unreachable_after_s=300)
    assert "secret.txt" not in rendered
    assert "детали озвучивает Кора дословно" in rendered
    assert "загрузка" in rendered

    snap = store.snapshot(now=3.0, stale_after_s=120, unreachable_after_s=300)
    events = snap["task"]["events"]
    critical_snap = next(e for e in events if e["type"] == "task_completed")
    assert "payload" not in critical_snap
    narratable_snap = next(e for e in events if e["type"] == "progress")
    assert narratable_snap["payload"] == {"stage": "загрузка"}


def test_liveness_thresholds():
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    store.heartbeat(0.0)
    assert store.liveness(now=10.0, stale_after_s=120, unreachable_after_s=300) == Liveness.OK
    assert store.liveness(now=120.0, stale_after_s=120, unreachable_after_s=300) == Liveness.STALE
    assert store.liveness(now=300.0, stale_after_s=120, unreachable_after_s=300) == Liveness.UNREACHABLE


def test_liveness_ok_with_no_signal_yet():
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    assert store.liveness(now=10_000.0, stale_after_s=120, unreachable_after_s=300) == Liveness.OK


def test_render_state_template_deterministic_phrases():
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    assert store.render_state_template(0.0, 120, 300) == "Активных задач нет."
    store.start_task("t1", "текст", TaskStatus.RUNNING, now=0.0)
    store.heartbeat(0.0)
    assert "выполняется" in store.render_state_template(0.0, 120, 300)
    assert store.render_state_template(400.0, 120, 300) == CANON_PHRASE_STALE_KORA


def test_persistence_roundtrip_restart_reports_stale_immediately(tmp_path):
    clock = FakeClock(0.0)
    store = TaskStore(clock, journal_dir=str(tmp_path))
    store.start_task("t1", "задача", TaskStatus.RUNNING, now=0.0)
    store.heartbeat(0.0)

    # Simulate a process restart: fresh TaskStore, fresh clock far in the future -- without
    # persistence this would reset last_event_ts and delay stale detection (R6).
    clock2 = FakeClock(1000.0)
    store2 = TaskStore(clock2, journal_dir=str(tmp_path))
    assert store2.task is not None
    assert store2.task.id == "t1"
    assert store2.liveness(now=1000.0, stale_after_s=120, unreachable_after_s=300) == Liveness.UNREACHABLE


def test_request_cancel_only_affects_active_task():
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    assert store.request_cancel() is False
    store.start_task("t1", "задача", TaskStatus.RUNNING, now=0.0)
    assert store.request_cancel() is True
    assert store.task.status == TaskStatus.CANCEL_REQUESTED


import pytest

def test_set_task_status_noop_on_terminal_statuses():
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    
    # COMPLETED status
    store.start_task("t1", "задача", TaskStatus.COMPLETED, now=0.0)
    store.set_task_status(TaskStatus.RUNNING)
    assert store.task.status == TaskStatus.COMPLETED

    # FAILED status
    store.start_task("t2", "задача", TaskStatus.FAILED, now=0.0)
    store.set_task_status(TaskStatus.RUNNING)
    assert store.task.status == TaskStatus.FAILED
