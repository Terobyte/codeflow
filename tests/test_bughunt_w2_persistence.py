"""Wave 2 bug hunt — persistence / boot-survival regressions (RED until fixed).

Three independent boot-time crash vectors that a drifted / corrupt / old-schema on-disk
state must NOT be able to trigger. Each test asserts the POST-FIX contract ("survive, treat
as forgotten / drop the blob, do not crash") so it fails RED against today's tree.

- B18 `state.py::TaskStore._load` — only `(JSONDecodeError, OSError)` is caught. A valid-JSON
  non-dict (`null`/`[]`) → `AttributeError`; an old-schema task dict missing a required key →
  `KeyError`/`ValueError`. All uncaught → `build_host` crashes on every boot until the file is
  deleted. Fix: a corrupt/old file is treated as "no task" (forgotten), not a crash.
- B37 `confirm.py::ConfirmFlow.__init__` — `_Staged(**persisted)` → `TypeError` when the
  persisted `staged` blob carries a key `_Staged` doesn't accept (schema drift). Fix: a drifted
  staged blob is dropped, not a boot crash.
- B38 `kora.py::KoraRunner._handle_question` — `answers = {q["question"]: ...}` hard subscript
  → `KeyError` on a malformed/hallucinated AskUserQuestion missing `"question"`, AFTER the
  user's answer was already consumed. Fix: `.get`/skip the missing key, return a valid hook dict.
"""
from __future__ import annotations

import asyncio
import json

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.kora import KoraRunner
from synapse.bridge.state import SpeakLedger, TaskStatus, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal

AFFIRM = frozenset({"да", "подтверждаю", "делай"})
DENY = frozenset({"нет", "отмена", "стоп"})


# =========================================================================================
# B18 — corrupt / old-schema state.json must not crash TaskStore construction (build_host boot)
# =========================================================================================


def test_b18_non_dict_state_json_survives_boot(tmp_path):
    """Case (a): state.json is valid JSON but NOT a dict. `data.get("task")` on `None`/`list`
    currently raises AttributeError inside `_load` → TaskStore construction crashes. POST-FIX:
    a non-dict payload is treated as no task (forgotten), no exception."""
    clock = FakeClock(0.0)
    for i, content in enumerate(["null", "[]"]):
        journal_dir = tmp_path / f"nondict_{i}"
        journal_dir.mkdir()
        (journal_dir / "state.json").write_text(content, encoding="utf-8")

        # Currently: json.loads("null")->None / "[]"->list → `data.get("task")` AttributeError → RED.
        store = TaskStore(clock, journal_dir=str(journal_dir))

        assert store.task is None
        assert store.staged is None


def test_b18_old_schema_task_missing_key_survives_boot(tmp_path):
    """Case (b): a persisted task dict from an older schema is missing the required `"id"`.
    `_task_from_dict` does a hard `d["id"]` → KeyError inside `_load` → construction crashes.
    POST-FIX: an unparseable/old task is forgotten (treated as no task), no exception."""
    clock = FakeClock(0.0)
    journal_dir = tmp_path / "oldschema"
    journal_dir.mkdir()
    content = json.dumps(
        {
            "task": {"text": "старая задача", "status": "running", "events": []},  # no "id"
            "last_event_ts": None,
            "staged": None,
        },
        ensure_ascii=False,
    )
    (journal_dir / "state.json").write_text(content, encoding="utf-8")

    # Currently: _task_from_dict(task_data) → d["id"] KeyError (uncaught) → RED.
    store = TaskStore(clock, journal_dir=str(journal_dir))

    assert store.task is None


# =========================================================================================
# B37 — drifted persisted `staged` blob must be dropped, not crash ConfirmFlow boot
# =========================================================================================


def test_b37_drifted_staged_blob_does_not_crash_confirmflow_boot(tmp_path):
    """A persisted `staged` blob carrying a key `_Staged` doesn't accept (schema drift across a
    restart) currently makes `_Staged(**persisted)` raise TypeError in ConfirmFlow.__init__.
    POST-FIX: the drifted blob is dropped (staged=None), boot survives."""
    clock = FakeClock(0.0)
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    drifted_staged = {
        "task_id": "tk-1",
        "text": "удали бэкапы",
        "readback_text": 'Подтверди необратимую задачу: "удали бэкапы"',
        "rereadback_count": 0,
        "awaiting_user_turn": True,
        "last_readback_ts": 0.0,
        "bogus_drift_key": 123,  # a field _Staged's __init__ does NOT accept
    }
    content = json.dumps(
        {"task": None, "last_event_ts": None, "staged": drifted_staged}, ensure_ascii=False
    )
    (journal_dir / "state.json").write_text(content, encoding="utf-8")

    # store loads the raw blob fine — B18 is not the failure here.
    store = TaskStore(clock, journal_dir=str(journal_dir))
    assert store.staged == drifted_staged

    journal = TurnJournal(str(journal_dir), clock, session_id="t")
    classifier = KeywordClassifier({"удали"})

    # Currently: persisted truthy → `_Staged(**persisted)` → TypeError (unexpected kwarg) → RED.
    flow = ConfirmFlow(store, clock, classifier, journal, AFFIRM, DENY, 2, 30.0)

    assert flow.staged is None  # drifted blob dropped, not crashed on


# =========================================================================================
# B38 — malformed AskUserQuestion (no "question" key) must not KeyError after the answer lands
# =========================================================================================


async def test_b38_malformed_question_missing_key_does_not_keyerror(tmp_path):
    """A malformed / hallucinated AskUserQuestion whose question object has NO `"question"` key.
    After the user's answer resolves the parked future, `answers = {q["question"]: ...}` hard
    subscript raises KeyError — the reply is consumed then the task FAILS. POST-FIX: `.get`/skip
    the missing key and still return a valid `allow` hook dict."""
    clock = FakeClock(0.0)
    ws = tmp_path / "ws"
    cfg = SynapseConfig(kora_workspace_dir=str(ws))
    store = TaskStore(clock)
    ledger = SpeakLedger()
    journal = TurnJournal(str(tmp_path / "journal"), clock, session_id="s")
    speaks: list[str] = []
    runner = KoraRunner(cfg, store, ledger, clock, journal, speaks.append)
    store.start_task("tk", "задача", TaskStatus.RUNNING, 0.0)

    # malformed: a question object with header+options but NO "question" key.
    tool_input = {"questions": [{"header": "формат", "options": [{"label": "JSON"}, {"label": "HTML"}]}]}

    gate = asyncio.create_task(runner._handle_question(tool_input))
    await asyncio.sleep(0)  # let _handle_question run up to `await fut`
    assert runner.provide_answer("простой текст") is True  # answer consumed

    # Currently: on resume `answers = {q["question"]: ...}` → KeyError("question") → RED here.
    result = await gate

    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "updatedInput" in hso
    assert "answers" in hso["updatedInput"]
