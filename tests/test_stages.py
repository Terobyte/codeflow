"""UI-4 стадии: тесты FSM треда, гейт-режима docs_only, gate_action, HTTP-гейта,
инструментов диспетчера и стадийного промпта. Пополняется по таскам плана UI-4/UI-5."""
from __future__ import annotations

from synapse.clock import FakeClock
from synapse.threads import ThreadStore, _STAGE_TRANSITIONS


# ---------------------------------------------------------------------------
# Task 1: FSM треда в ThreadStore
# ---------------------------------------------------------------------------


def _store(tmp_path) -> ThreadStore:
    return ThreadStore(FakeClock(1_000_000.0), tmp_path)


def test_legal_stage_transitions_full_cycle():
    s = _store(tmp_path := __import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    t = s.create("x")
    # полный стадийный путь
    s.set_stage(t.id, "propose")
    assert s.get(t.id).stage == "propose"
    s.set_stage(t.id, "spec_plan")
    assert s.get(t.id).stage == "spec_plan"
    s.set_stage(t.id, "code")
    assert s.get(t.id).stage == "code"
    s.set_stage(t.id, "done")
    assert s.get(t.id).stage == "done"


def test_fast_path_propose_to_code():
    s = _store(__import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    t = s.create("x")
    s.set_stage(t.id, "propose")
    s.set_stage(t.id, "code")  # быстрый путь, минуя spec_plan
    assert s.get(t.id).stage == "code"


def test_revise_from_every_working_stage_back_to_collect():
    for start in ("propose", "spec_plan", "code"):
        s = _store(__import__("pathlib").Path(__import__("tempfile").mkdtemp()))
        t = s.create("x")
        # дотянуть тред до рабочей стадии по легальному пути
        s.set_stage(t.id, "propose")
        if start in ("spec_plan", "code"):
            s.set_stage(t.id, "spec_plan")
        if start == "code":
            s.set_stage(t.id, "code")
        assert s.get(t.id).stage == start
        # [Правки → СБОР] из любой рабочей стадии (спека:57/60/96)
        s.set_stage(t.id, "collect")
        assert s.get(t.id).stage == "collect"


def test_illegal_stage_transitions_raise():
    import pytest
    bad = [
        ("collect", "code"),       # перепрыгивание стадий
        ("collect", "spec_plan"),
        ("collect", "done"),
        ("code", "propose"),       # назад по «лесенке» без revise
        ("done", "collect"),       # done — терминальная
        ("done", "code"),
        ("done", "propose"),
    ]
    for frm, to in bad:
        s = _store(__import__("pathlib").Path(__import__("tempfile").mkdtemp()))
        t = s.create("x")
        if frm != "collect":
            s.set_stage(t.id, "propose")
            if frm in ("spec_plan", "code"):
                s.set_stage(t.id, "spec_plan")
            if frm == "code":
                s.set_stage(t.id, "code")
            if frm == "done":
                s.set_stage(t.id, "code")
                s.set_stage(t.id, "done")
        assert s.get(t.id).stage == frm, (frm, to)
        with pytest.raises(ValueError):
            s.set_stage(t.id, to)


def test_recovery_trace_failed_code_then_revise():
    """CODE-запуск упал (стадия остаётся code) → revise → collect → полный цикл заново."""
    s = _store(__import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    t = s.create("x")
    s.set_stage(t.id, "propose")
    s.set_stage(t.id, "spec_plan")
    s.set_stage(t.id, "code")
    # ран упал — стадия НЕ меняется (исход ортогонален стадии)
    assert s.get(t.id).stage == "code"
    s.set_stage(t.id, "collect")  # revise из code
    # полный цикл заново
    s.set_stage(t.id, "propose")
    s.set_stage(t.id, "spec_plan")
    s.set_stage(t.id, "code")
    s.set_stage(t.id, "done")
    assert s.get(t.id).stage == "done"


def test_set_request_persists_request_text():
    s = _store(__import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    t = s.create("x")
    s.set_request(t.id, "сделай мне штуку")
    assert s.get(t.id).request_text == "сделай мне штуку"


def test_bind_project_guard():
    """Находка F: привязка ок пока task_ids пуст; повтор/после запуска/знач→знач — отказ."""
    s = _store(__import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    t = s.create("x")
    # null → значение при пустых task_ids: ок
    assert s.bind_project(t.id, "proj1") is True
    assert s.get(t.id).project_id == "proj1"
    # значение → значение: отказ
    assert s.bind_project(t.id, "proj2") is False
    assert s.get(t.id).project_id == "proj1"
    # привязка после запуска (task_ids непуст): отказ
    s.append_task(t.id, "task-1")
    assert s.bind_project(t.id, "proj3") is False
    assert s.get(t.id).project_id == "proj1"


def test_set_last_model_persists():
    s = _store(__import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    t = s.create("x")
    s.set_last_model(t.id, "claude-sonnet-5")
    assert s.get(t.id).last_model == "claude-sonnet-5"


def test_load_restores_new_fields():
    import pathlib, tempfile
    root = pathlib.Path(tempfile.mkdtemp())
    s = ThreadStore(FakeClock(2_000_000.0), root)
    t = s.create("x")
    s.set_request(t.id, "запрос")
    s.set_last_model(t.id, "claude-opus-4-8")
    s.set_stage(t.id, "propose")
    s.bind_project(t.id, "proj-z")
    # новый стор из того же каталога — как после рестарта
    s2 = ThreadStore(FakeClock(3_000_000.0), root)
    t2 = s2.get(t.id)
    assert t2.request_text == "запрос"
    assert t2.last_model == "claude-opus-4-8"
    assert t2.stage == "propose"
    assert t2.project_id == "proj-z"
    assert t2.archived is False


def test_load_old_file_without_new_fields_uses_defaults():
    """Старый файл без request_text/last_model/archived — дефолты, не крэш."""
    import json, pathlib, tempfile
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "old.json").write_text(json.dumps({
        "id": "oldthread", "title": "старый", "stage": "collect",
        "created_ts": 0.0, "updated_ts": 0.0, "task_ids": [],
    }), encoding="utf-8")
    s = ThreadStore(FakeClock(1.0), root)
    t = s.get("oldthread")
    assert t is not None
    assert t.request_text is None
    assert t.last_model is None
    assert t.archived is False


def test_stage_transitions_table_is_complete():
    # Stage FSM: collect → propose; propose → spec_plan|code|collect; spec_plan → code|collect;
    # code → done|collect; done — терминальная (нет исходящих).
    assert _STAGE_TRANSITIONS["collect"] == frozenset({"propose"})
    assert _STAGE_TRANSITIONS["propose"] == frozenset({"spec_plan", "code", "collect"})
    assert _STAGE_TRANSITIONS["spec_plan"] == frozenset({"code", "collect"})
    assert _STAGE_TRANSITIONS["code"] == frozenset({"done", "collect"})
    assert _STAGE_TRANSITIONS["done"] == frozenset()
