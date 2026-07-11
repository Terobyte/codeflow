from synapse.bridge.state import EventClass, KoraEvent, SpeakLedger


def test_critical_with_paired_speak_is_not_alerted():
    ledger = SpeakLedger()
    ev = KoraEvent(id="e1", type="task_completed", cls=EventClass.CRITICAL, payload={}, speak_text="готово", ts=0.0)
    ledger.register_critical(ev)
    ledger.register_speak("e1", ts=0.0)
    assert ledger.check(now=100.0, window_s=5.0) == []


def test_critical_without_speak_alerts_after_window_not_before():
    ledger = SpeakLedger()
    ev = KoraEvent(id="e1", type="task_completed", cls=EventClass.CRITICAL, payload={}, speak_text=None, ts=0.0)
    ledger.register_critical(ev)
    assert ledger.check(now=4.9, window_s=5.0) == []
    alerts = ledger.check(now=5.0, window_s=5.0)
    assert len(alerts) == 1
    assert alerts[0][0] == "CRITICAL_WITHOUT_SPEAK"
    assert alerts[0][1]["event_id"] == "e1"


def test_narratable_events_are_never_tracked():
    ledger = SpeakLedger()
    ev = KoraEvent(id="e1", type="progress", cls=EventClass.NARRATABLE, payload={}, speak_text=None, ts=0.0)
    ledger.register_critical(ev)  # no-op for narratable
    assert ledger.check(now=1000.0, window_s=5.0) == []


def test_speak_registered_after_the_fact_still_suppresses_alert():
    ledger = SpeakLedger()
    ev = KoraEvent(id="e1", type="task_completed", cls=EventClass.CRITICAL, payload={}, speak_text=None, ts=0.0)
    ledger.register_critical(ev)
    ledger.register_speak("e1", ts=1.0)
    assert ledger.check(now=1000.0, window_s=5.0) == []


import pytest

def test_critical_without_speak_alerts_only_once():
    ledger = SpeakLedger()
    ev = KoraEvent(id="e1", type="task_completed", cls=EventClass.CRITICAL, payload={}, speak_text=None, ts=0.0)
    ledger.register_critical(ev)
    assert ledger.check(now=4.9, window_s=5.0) == []
    alerts1 = ledger.check(now=5.0, window_s=5.0)
    assert len(alerts1) == 1
    assert alerts1[0][0] == "CRITICAL_WITHOUT_SPEAK"
    alerts2 = ledger.check(now=10.0, window_s=5.0)
    assert alerts2 == []
