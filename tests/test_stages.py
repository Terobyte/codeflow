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


# ---------------------------------------------------------------------------
# Task 2: гейт-режим docs_only в KoraRunner
# ---------------------------------------------------------------------------

from synapse.bridge.kora import KoraRunner
from synapse.bridge.runspec import RunSpec
from synapse.bridge.state import SpeakLedger, TaskStatus, TaskStore
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal


class _Fc:  # мини FakeClock, чтобы не тащить зависимость по порядку импортов
    def __init__(self, t=0.0): self.t = t
    def now(self): return self.t


def _gate_runner(tmp_path, captured, gate_mode):
    """Стаб-раннер как в test_runspec.py: FakeClient зовёт _gate_decision во время рана,
    когда снапшот gate_mode уже стоит. captures — список (tool, input) → решение."""
    cfg = SynapseConfig(kora_workspace_dir=str(tmp_path / "ws"))
    clock = _Fc()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)

    class FakeClient:
        def __init__(self, opts): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def query(self, text): pass
        async def receive_response(self):
            r = captured["runner"]
            for (tool, inp) in captured["probes"]:
                captured["results"].append(r._gate_decision(tool, inp))
            if False:
                yield None

    runner = KoraRunner(cfg, store, SpeakLedger(), clock, journal, None,
                        client_factory=lambda opts: FakeClient(opts))
    captured["runner"] = runner
    return runner, store


async def _run_gate(tmp_path, gate_mode, probes):
    """Прогоняет ран с gate_mode и возвращает список решений по probes (во время рана,
    когда снапшот gate_mode уже стоит — паттерн test_runspec.py)."""
    captured = {"probes": probes, "results": []}
    runner, store = _gate_runner(tmp_path, captured, gate_mode)
    store.start_task("t1", "з", TaskStatus.RUNNING, 0.0)
    await runner._run("t1", "з", RunSpec(thread_id="th1", gate_mode=gate_mode))
    return captured["results"]


def _mk(tmp_path, *parts):
    p = tmp_path / "ws"
    for seg in parts:
        p = p / seg
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    return p


async def test_docs_only_allows_write_into_docs_subtree(tmp_path):
    _mk(tmp_path, "docs", "plans", "x.md")
    f = tmp_path / "ws" / "docs" / "plans" / "x.md"
    [res] = await _run_gate(tmp_path, "docs_only", [("Write", {"file_path": str(f)})])
    allowed, _, cat = res
    assert allowed and cat == "allow"


async def test_docs_only_denies_write_into_src(tmp_path):
    _mk(tmp_path, "src", "main.py")
    f = tmp_path / "ws" / "src" / "main.py"
    [res] = await _run_gate(tmp_path, "docs_only", [("Write", {"file_path": str(f)})])
    allowed, _, cat = res
    assert not allowed and cat == "docs_only_violation"


async def test_docs_only_allows_top_level_md_edit(tmp_path):
    _mk(tmp_path, "plan.md")
    f = tmp_path / "ws" / "plan.md"
    [res] = await _run_gate(tmp_path, "docs_only", [("Edit", {"file_path": str(f)})])
    allowed, _, cat = res
    assert allowed and cat == "allow"


async def test_docs_only_denies_top_level_non_md(tmp_path):
    _mk(tmp_path, "config.toml")
    f = tmp_path / "ws" / "config.toml"
    [res] = await _run_gate(tmp_path, "docs_only", [("Write", {"file_path": str(f)})])
    allowed, _, cat = res
    assert not allowed and cat == "docs_only_violation"


async def test_docs_only_allows_read_and_grep_anywhere(tmp_path):
    _mk(tmp_path, "src", "deep.py")
    f = tmp_path / "ws" / "src" / "deep.py"
    res = await _run_gate(tmp_path, "docs_only", [
        ("Read", {"file_path": str(f)}),
        ("Grep", {"path": str(tmp_path / "ws" / "src")}),
    ])
    assert all(allowed and cat == "allow" for allowed, _, cat in res)


async def test_docs_only_secret_still_denied_before_docs_check(tmp_path):
    """Порядок проверок: секрет ловится ДО docs_only, даже внутри docs/ (docs/.env)."""
    _mk(tmp_path, "docs", ".env")
    f = tmp_path / "ws" / "docs" / ".env"
    [res] = await _run_gate(tmp_path, "docs_only", [("Write", {"file_path": str(f)})])
    allowed, _, cat = res
    assert not allowed and cat == "secret_path"


async def test_full_gate_mode_byte_identical_to_pre_docs_only(tmp_path):
    """gate_mode=full — мутирующая Write в src/ разрешена (поведение прежнее)."""
    _mk(tmp_path, "src", "main.py")
    f = tmp_path / "ws" / "src" / "main.py"
    [res] = await _run_gate(tmp_path, "full", [("Write", {"file_path": str(f)})])
    allowed, _, cat = res
    assert allowed and cat == "allow"


def test_no_run_snapshot_defaults_to_full_gate_mode(tmp_path):
    """Вне рана (снапшот пуст) _current_gate_mode() → 'full' — fail-open корректен."""
    captured = {"probes": [], "results": []}
    runner, _ = _gate_runner(tmp_path, captured, "full")
    assert runner._current_gate_mode() == "full"


async def test_snapshot_gate_mode_cleared_after_run(tmp_path):
    """Снапшот gate_mode чистится в finally identity-guard, как _run_root/_run_model."""
    captured = {"probes": [], "results": []}
    runner, store = _gate_runner(tmp_path, captured, "docs_only")
    store.start_task("t9", "з", TaskStatus.RUNNING, 0.0)
    await runner._run("t9", "з", RunSpec(thread_id="th9", gate_mode="docs_only"))
    assert runner._run_gate_mode is None
    assert runner._current_gate_mode() == "full"  # вне рана → full


async def test_parked_answer_not_delivered_after_run_ends(tmp_path):
    """Факт 12 (межзапускный reset): SPEC_PLAN-запуск паркует AskUserQuestion, ран завершается,
    provide_answer в НОВЫЙ (ещё не стартовавший) ран не доставляется — awaiting чист. На UI-пути
    supersede не делается (409 на занятом), поэтому чистота держится на finally-очистке
    _handle_question. Здесь проверяем observable: после конца рана provide_answer → False
    (вопрос не кому доставлять), awaiting флаг погашен."""
    captured = {"probes": [], "results": []}
    runner, store = _gate_runner(tmp_path, captured, "docs_only")
    store.start_task("tA", "з", TaskStatus.RUNNING, 0.0)
    await runner._run("tA", "з", RunSpec(thread_id="thA", gate_mode="docs_only"))
    # ран закончился, снапшот чист — ответ некому доставлять
    assert runner.provide_answer("да") is False
    assert store.awaiting_answer is False
