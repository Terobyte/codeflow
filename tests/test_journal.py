import json

from synapse.clock import FakeClock
from synapse.journal import AlertKind, TurnJournal


def test_grounding_alert_when_status_claimed_without_tool_call(tmp_path):
    clock = FakeClock(0.0)
    journal = TurnJournal(str(tmp_path), clock, session_id="s")
    record = journal.begin_turn("как дела?")
    record.llm_output = "Задача почти готова"
    journal.check_grounding(record, has_active_task=True)
    assert any(a["alert_kind"] == "STATUS_WITHOUT_GROUNDING" for a in record.alerts)


def test_grounding_no_alert_when_status_tool_called(tmp_path):
    clock = FakeClock(0.0)
    journal = TurnJournal(str(tmp_path), clock, session_id="s")
    record = journal.begin_turn("как дела?")
    record.llm_output = "Задача выполняется"
    record.tool_calls.append({"name": "get_task_status", "arguments": {}, "result": {}})
    journal.check_grounding(record, has_active_task=True)
    assert record.alerts == []


def test_grounding_no_alert_when_no_active_task(tmp_path):
    clock = FakeClock(0.0)
    journal = TurnJournal(str(tmp_path), clock, session_id="s")
    record = journal.begin_turn("привет")
    record.llm_output = "Задача готова"  # matches the vocabulary, but no active task at all
    journal.check_grounding(record, has_active_task=False)
    assert record.alerts == []


def test_alert_line_is_durable_before_end_turn(tmp_path):
    clock = FakeClock(0.0)
    journal = TurnJournal(str(tmp_path), clock, session_id="s")
    journal.begin_turn("test")
    journal.alert(AlertKind.COST_CAP, {"x": 1})
    # end_turn() has NOT been called yet -- the alert line must already be on disk (R2).
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["kind"] == "alert"
    assert row["alert_kind"] == "COST_CAP"
    journal.end_turn()
    lines2 = journal.path.read_text(encoding="utf-8").splitlines()
    assert len(lines2) == 2


def test_end_turn_writes_tier_mask_retry_fields(tmp_path):
    clock = FakeClock(0.0)
    journal = TurnJournal(str(tmp_path), clock, session_id="s")
    record = journal.begin_turn("test")
    record.tier = {"endpoint": "anthropic", "model": "claude-haiku-4-5"}
    record.breaker_mask = {"0": None, "1": "AUTH"}
    record.retry = True
    journal.end_turn()
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[-1])
    assert row["tier"] == {"endpoint": "anthropic", "model": "claude-haiku-4-5"}
    assert row["breaker_mask"] == {"0": None, "1": "AUTH"}
    assert row["retry"] is True


def test_record_tool_call_appends_to_current_turn(tmp_path):
    clock = FakeClock(0.0)
    journal = TurnJournal(str(tmp_path), clock, session_id="s")
    record = journal.begin_turn("test")
    journal.record_tool_call("get_task_status", {}, {"ok": True})
    assert record.tool_calls == [{"name": "get_task_status", "arguments": {}, "result": {"ok": True}}]
