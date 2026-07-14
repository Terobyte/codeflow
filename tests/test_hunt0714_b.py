"""Failing-tests proof for Hunt 2026-07-14 bugs B03, B05, B09, B11, B12, B13, B14
(docs/bugs.md). Each test documents the CORRECT behavior as its pass condition and is
expected to be RED against the current (unfixed) code — see the docstring on each test
for the exact expected-vs-actual from the ledger.

Touches production code: NONE. Touches other test files: NONE.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import sys
import types
from pathlib import Path

import pytest

from synapse.bridge.confirm import ConfirmDecisionOutcome, ConfirmFlow, KeywordClassifier
from synapse.bridge.kora import KoraRunner, apply_event_to_store
from synapse.bridge.runspec import RunSpec
from synapse.bridge.state import EventClass, KoraEvent, SpeakLedger, TaskState, TaskStatus, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal
from synapse.projects import ProjectValidationError, validate_project_path


# =========================================================================================
# B03 — gate's no-path Glob/Grep/LS fast path skips the secret-path check.
# location: synapse/bridge/kora.py:534-554 (fast path 550-554)
# expected: any resolved path under a _SECRET_DIR_SEGMENTS segment denied for every file
#           tool, incl. the cwd-default of a no-path Grep/Glob/LS.
# actual:   no-path fast path returns (True, None, "allow") without ever resolving cwd or
#           calling _is_secret_path — so a workspace root that itself sits under a secret
#           dir segment (e.g. .../.ssh) is granted Grep/Glob/LS with zero containment.
# =========================================================================================


def _make_kora_runner(tmp_path: Path, workspace_dir: Path) -> KoraRunner:
    clock = FakeClock(0.0)
    cfg = SynapseConfig(kora_workspace_dir=str(workspace_dir))
    store = TaskStore(clock)  # no journal_dir → no state.json persistence
    ledger = SpeakLedger()
    journal = TurnJournal(str(tmp_path / "journal"), clock, session_id="s")
    return KoraRunner(cfg, store, ledger, clock, journal, on_speak=None)


def test_B03_no_path_fast_path_must_deny_secret_workspace_root(tmp_path):
    # Workspace root itself resolves under a secret dir segment (".ssh").
    secret_root = tmp_path / "home" / ".ssh" / "some-project"
    secret_root.mkdir(parents=True)
    runner = _make_kora_runner(tmp_path, secret_root)

    allowed, _detail, category = runner._gate_decision("Grep", {})  # no path → defaults to cwd

    assert allowed is False, (
        "a Grep/Glob/LS with no path defaulting to a workspace root under a secret dir "
        f"segment must be DENIED, got allowed=True category={category!r}"
    )
    assert category == "secret_path"


# =========================================================================================
# B05 — validate_project_path omits .ssh/.aws/.kube/.docker from its denylist.
# location: synapse/projects.py:15,36-38
# expected: a path rooted at ~/.ssh (or .aws/.kube/.docker) raises ProjectValidationError,
#           just like ~/.config or ~/.gnupg already do.
# actual:   _FORBIDDEN_HOME_SUBDIRS only covers .config/.gnupg/Library/Keychains — .ssh
#           passes through and the path is silently accepted (returned).
# =========================================================================================


def test_B05_ssh_dir_bypasses_secret_denylist(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    ssh_dir = fake_home / ".ssh"
    ssh_dir.mkdir()  # validate_project_path requires the dir to exist (is_dir() check)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    with pytest.raises(ProjectValidationError):
        validate_project_path(str(ssh_dir))


# =========================================================================================
# B09 — apply_event_to_store registers a CRITICAL's speak BEFORE the ledger entry exists.
# location: synapse/bridge/kora.py:299-317
# expected: register order matches FakeKora.emit (register_critical creates the pending
#           entry FIRST, then register_speak marks it spoken) — a CRITICAL lifecycle event
#           with speak_text ends up recorded spoken=True, and SpeakLedger.check() is clean.
# actual:   register_speak(event.id) runs first (no-op, nothing pending yet), THEN
#           register_critical creates a fresh spoken=False entry → a spoken critical is
#           recorded as never spoken → check() raises a false CRITICAL_WITHOUT_SPEAK.
# =========================================================================================


def test_B09_critical_speak_registered_before_ledger_entry_exists(tmp_path):
    clock = FakeClock(100.0)
    store = TaskStore(clock)
    store.start_task("t1", "do the thing", TaskStatus.RUNNING, clock.now())
    ledger = SpeakLedger()
    journal = TurnJournal(str(tmp_path / "journal"), clock, session_id="s")
    spoken_texts: list[str] = []

    event = KoraEvent(
        id="ev-critical-1",
        type="task_completed",
        cls=EventClass.CRITICAL,
        payload={},
        speak_text="Задача завершена.",
        ts=clock.now(),
    )

    apply_event_to_store(event, store, ledger, spoken_texts.append, journal)

    assert ledger._pending["ev-critical-1"].spoken is True, (
        "a CRITICAL lifecycle event with speak_text must be recorded spoken=True — "
        "it WAS delivered via on_speak"
    )
    alerts = ledger.check(now=clock.now() + 10.0, window_s=5.0)
    assert alerts == [], f"a genuinely-spoken critical must not trip CRITICAL_WITHOUT_SPEAK, got {alerts!r}"


# =========================================================================================
# B11 — confirm.py's _new_task_id and app.py's gate task-id minting are two independent
# itertools.count(1) generators with the identical "task-{ms}-{seq}" format → identical
# IDs collide under a fixed clock, silently overwriting _task_index.
# location: synapse/bridge/confirm.py:21-25 vs synapse/pipeline/app.py:342,359
# expected: task IDs are unique process-wide (the confirm-minted id and the gate-minted id
#           at the same clock tick must differ).
# actual:   both counters start fresh at 1 and format identically → "task-<t>-1" == "task-<t>-1".
# =========================================================================================


class _FakeKoraRunnerB11:
    """Stub KoraRunner — records start(...) calls, no SDK/network (mirrors test_stages.py's
    _FakeRunner pattern)."""

    def __init__(self) -> None:
        self.starts: list[tuple[str, str, RunSpec]] = []

    def start(self, task_id: str, text: str, spec: RunSpec) -> None:
        self.starts.append((task_id, text, spec))


def _build_gate_host(tmp_path: Path, clock: FakeClock):
    from synapse.pipeline.app import build_host

    cfg = SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path / "j"), kora_workspace_dir=str(tmp_path / "ws"),
    )
    host = build_host(cfg, clock=clock)
    host.kora_runner = _FakeKoraRunnerB11()
    return host


async def test_B11_confirm_and_gate_task_id_generators_collide(tmp_path, monkeypatch):
    import synapse.bridge.confirm as confirm_mod
    import synapse.pipeline.app as app_mod

    # Reset both module-level counters to a fresh state (they are process-lifetime globals
    # shared across the whole test session) so the "first mint of each, same millisecond"
    # collision the ledger describes is deterministic regardless of test order.
    monkeypatch.setattr(confirm_mod, "_task_id_counter", itertools.count(1))
    monkeypatch.setattr(app_mod, "_GATE_TASK_SEQ", itertools.count(1))

    clock = FakeClock(1_000.0)

    # (a) the confirm generator's mint at this clock tick.
    confirm_task_id = confirm_mod._new_task_id(clock.now())

    # (b) the gate generator's mint at the SAME clock tick, via the real gate_action path.
    host = _build_gate_host(tmp_path, clock)
    t = host.threads.create("x")
    host.threads.set_stage(t.id, "propose")
    host.threads.set_request(t.id, "сделай штуку")
    res = await host.gate_action(t.id, "send_to_kora", confirm=True, fast=True)
    assert res.get("ok") is True
    gate_task_id = host.kora_runner.starts[-1][0]

    assert confirm_task_id != gate_task_id, (
        f"two independent generators minted the identical task id {confirm_task_id!r} at "
        "the same clock tick — _task_index would silently overwrite"
    )


# =========================================================================================
# B12 — ConfirmFlow.submit stages a task in TWO separate persisted writes
# (store.start_task then store.set_staged); a crash between them leaves state.json with a
# task stuck PENDING_CONFIRMATION but staged=null, and the "normal flow" the code's own
# comment claims fixes it (state.py:389) does NOT: has_active_task() blocks all submit()
# forever, confirm() rejects because self._staged is None.
# location: synapse/bridge/confirm.py:150-156 + synapse/bridge/state.py:192-196,222-224,385-389
# expected: the flow is NOT wedged forever — either has_active_task() is False after
#           reload, or confirm()/the normal path can still resolve the dangling task.
# actual:   has_active_task() stays True forever AND confirm() rejects
#           ("Подтверждать нечего") — only request_cancel can free it.
# =========================================================================================


def test_B12_crash_between_start_task_and_set_staged_wedges_flow(tmp_path):
    clock = FakeClock(1_000.0)
    journal_dir = tmp_path / "j"
    journal = TurnJournal(str(journal_dir), clock, session_id="s")
    classifier = KeywordClassifier(["удали"])

    store = TaskStore(clock, journal_dir=journal_dir)
    # Simulate the FIRST persisted write of submit() (store.start_task, which already
    # persists per state.py:192-196) WITHOUT the second (store.set_staged) — i.e. a crash
    # exactly between confirm.py:154 and confirm.py:155.
    task_id = "task-1000000-1"
    store.start_task(task_id, "удали всё", TaskStatus.PENDING_CONFIRMATION, clock.now())

    # "Restart": a fresh TaskStore + ConfirmFlow constructed from the on-disk state.json only.
    store2 = TaskStore(clock, journal_dir=journal_dir)
    assert store2.staged is None  # the crash window: task persisted, staged never was
    assert store2.task is not None and store2.task.status == TaskStatus.PENDING_CONFIRMATION

    flow2 = ConfirmFlow(
        store2, clock, classifier, journal,
        affirm_words=frozenset({"да"}), deny_words=frozenset({"нет"}),
        max_rereadbacks=2, confirm_timeout_s=30.0,
    )

    not_wedged = (not store2.has_active_task()) or (
        flow2.confirm("confirm", clock.now()).outcome != ConfirmDecisionOutcome.REJECTED
    )
    assert not_wedged, (
        "a PENDING_CONFIRMATION task with staged=null after restart must be reconciled — "
        "instead has_active_task() blocks submit() forever AND confirm() rejects it"
    )


# =========================================================================================
# B13 — record_commands.record_session wipes prior manifest entries when re-run for a new
# --bg without --resume (manifest.json is one file per --out dir spanning multiple --bg
# conditions).
# location: synapse/runners/record_commands.py:18-19,44-45,74-78
# expected: manifest retains entries from earlier --bg sessions; --resume only governs
#           re-recording the same phrase/bg.
# actual:   `manifest = [] if not resume` discards every earlier condition's rows on the
#           first save of a new non-resume run in the same --out dir.
# =========================================================================================


def _install_fake_audio_stack(monkeypatch) -> None:
    import numpy as np

    class _FakeInputStream:
        def __init__(self, *a, **k) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def close(self) -> None:
            pass

        def read(self, n: int):
            return np.zeros((n, 1), dtype="int16"), False

    fake_sd = types.ModuleType("sounddevice")
    fake_sd.InputStream = _FakeInputStream
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    fake_sf = types.ModuleType("soundfile")

    def _write(path, data, samplerate) -> None:
        Path(path).write_bytes(b"")

    fake_sf.write = _write
    monkeypatch.setitem(sys.modules, "soundfile", fake_sf)

    # Both record_session's own "press Enter to start" prompt and the background waiter
    # thread's bare input() call return immediately — no real mic/terminal needed.
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")


def test_B13_manifest_wiped_on_new_bg_without_resume(tmp_path, monkeypatch):
    _install_fake_audio_stack(monkeypatch)
    from synapse.runners import record_commands as rc

    phrases_path = tmp_path / "phrases.txt"
    phrases_path.write_text("привет\n", encoding="utf-8")
    out_dir = tmp_path / "out"

    rc.record_session(str(phrases_path), str(out_dir), "тихая", resume=False)
    manifest_after_first = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert [e["bg"] for e in manifest_after_first] == ["тихая"]

    rc.record_session(str(phrases_path), str(out_dir), "улица", resume=False)
    manifest_after_second = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))

    bgs = {e["bg"] for e in manifest_after_second}
    assert "тихая" in bgs, (
        f"manifest must retain entries from earlier --bg sessions, got only {bgs!r}"
    )


# =========================================================================================
# B14 — TaskStore.apply_event appends duplicate terminal events past terminal status.
# location: synapse/bridge/state.py:230-241
# expected: a repeat terminal signal (second task_completed / a task_failed after
#           task_completed) is a no-op for the persisted task record — task.events keeps
#           exactly ONE terminal entry.
# actual:   the terminal guard (239) only protects `.status`; the append at 233 runs
#           unconditionally, so task.events grows a duplicate terminal entry.
# =========================================================================================


def test_B14_duplicate_terminal_event_appended_twice(tmp_path):
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    store.start_task("t1", "do the thing", TaskStatus.RUNNING, clock.now())

    ev1 = KoraEvent(
        id="done-1", type="task_completed", cls=EventClass.NARRATABLE,
        payload={}, speak_text=None, ts=1.0,
    )
    ev2 = KoraEvent(
        id="done-2", type="task_completed", cls=EventClass.NARRATABLE,
        payload={}, speak_text=None, ts=2.0,
    )
    store.apply_event(ev1)
    store.apply_event(ev2)  # a second terminal ResultMessage for the same task

    terminal_events = [e for e in store.task.events if e.type in ("task_completed", "task_failed")]
    assert len(terminal_events) == 1, (
        f"a repeat terminal event must be a no-op for task.events, got {len(terminal_events)} "
        f"terminal entries: {[e.id for e in terminal_events]!r}"
    )
