"""Багхант МЕШ-2 (консилиум `consult_kora`) — phase-2 red tests.

One failing test per confirmed finding (bugs.md §"Багхант МЕШ-2"). Each asserts the DESIRED
behaviour and is red on current code AT ITS OWN ASSERTION (documented expected-vs-actual), not
at import/fixture/signature. Offline, duck-typed fakes, mirrors tests/test_full_mesh_m2.py.
"""
from __future__ import annotations

import asyncio
import contextlib
import os

import pytest

from synapse.bridge.kora import ConsultIdleTimeout, KoraRunner
from synapse.bridge.state import SpeakLedger, TaskStatus, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal


def _runner(tmp_path, *, idle=0.05, on_case_entry=None, on_speak=None):
    """Local copy of the test_full_mesh_m2 harness helper (adds an on_speak seam)."""
    clock = FakeClock(1.0)
    store = TaskStore(clock)
    runner = KoraRunner(
        SynapseConfig(
            kora_workspace_dir=str(tmp_path / "ws"),
            kora_cli_path="/bin/echo",
            journal_dir=str(tmp_path / "journal"),
            consult_idle_timeout_s=idle,
        ),
        store,
        SpeakLedger(),
        clock,
        TurnJournal(str(tmp_path / "journal"), clock, session_id="mesh2"),
        on_speak,
        on_case_entry=on_case_entry,
    )
    return runner, store


# ---------------------------------------------------------------------------------------------
# B-M2-2 — idle-timeout must leave RUNNING synchronously as a BENIGN status (not FAILED).
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_b_m2_2_idle_timeout_leaves_running_synchronously_benign(tmp_path):
    runner, store = _runner(tmp_path, idle=0.01)
    store.start_task("c1", "brief", TaskStatus.RUNNING, 1.0)
    runner._run_owner = "c1"
    runner._run_kind = "consult"
    store.set_awaiting()

    child = asyncio.create_task(asyncio.Event().wait())
    try:
        with pytest.raises(ConsultIdleTimeout):
            await runner._watch_deadline(child, 10.0)

        # DESIRED: the moment idle is detected the store leaves RUNNING synchronously, so a
        # concurrent resume can't deliver into a dying run — and the benign timeout is NOT
        # recorded as a crash (FAILED). On current code _watch_deadline raises without touching
        # store status → task stays RUNNING → this assertion fails.
        assert store.task.status != TaskStatus.RUNNING
        assert store.task.status != TaskStatus.FAILED
        # The concrete benign state mirrors request_cancel (bugs.md B-M2-2: "зеркало
        # request_cancel", which flips CANCEL_REQUESTED synchronously). No TaskStatus.CANCELLED
        # exists in the enum; CANCEL_REQUESTED is that benign non-RUNNING status.
        assert store.task.status == TaskStatus.CANCEL_REQUESTED
    finally:
        child.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await child


# ---------------------------------------------------------------------------------------------
# B-M2-3 — a consult run must NOT TTS-speak the lifecycle completion text (whole case file).
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_b_m2_3_consult_completion_not_spoken(tmp_path):
    case_text = "# Дело треда\n## Бриф Flow\nсекрет-обсуждение"
    spoken: list[str] = []
    runner, store = _runner(tmp_path, on_speak=spoken.append)
    store.start_task("c1", case_text, TaskStatus.RUNNING, 1.0)
    runner._run_owner = "c1"
    runner._run_kind = "consult"
    runner._run_thread_id = "th"

    class ResultMessage:  # duck-typed by class name in _message_to_events
        is_error = False
        num_turns = 1
        total_cost_usd = 0.0

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, text):
            pass

        async def receive_response(self):
            yield ResultMessage()

    runner._client_factory = lambda opts: FakeClient()
    runner._build_options = lambda task_id, text: {}

    await runner._stream("c1", case_text)

    # DESIRED: a consult speaks ONLY via reply_to_flow. A completion ResultMessage without a
    # final reply must not push the lifecycle completion text ("Задача выполнена: <task_text>")
    # to TTS — for a consult task_text IS the whole case markdown. On current code
    # apply_event_to_store speaks the completion text (no run_kind=="consult" guard) → fails.
    assert all("Задача выполнена" not in s for s in spoken)
    assert all("Дело треда" not in s for s in spoken)


# ---------------------------------------------------------------------------------------------
# B-M2-4 — case-store isolation must survive RECURSION tools (Grep/Glob/LS), not only a path arg.
# ---------------------------------------------------------------------------------------------
def test_b_m2_4_case_isolation_survives_recursion_tools(tmp_path):
    runner, _ = _runner(tmp_path)
    runner._run_gate_mode = "consult"
    runner._run_root = tmp_path  # journal_dir (tmp_path/journal) is UNDER the run root

    case = tmp_path / "journal" / "threads" / "th.case.md"
    case.parent.mkdir(parents=True, exist_ok=True)
    case.write_text("# Дело треда\nчужой бриф", encoding="utf-8")

    # (a) no-path Grep defaults to the workspace/run root and recurses INTO journal.
    grep_allowed, _, _ = runner._gate_decision("Grep", {"pattern": "Дело треда"})
    # (b) a broad-root Glob and LS aimed at the run root (ancestor of journal).
    glob_allowed, _, _ = runner._gate_decision(
        "Glob", {"path": str(tmp_path), "pattern": "**/*.case.md"}
    )
    ls_allowed, _, _ = runner._gate_decision("LS", {"path": str(tmp_path)})

    # DESIRED: all DENIED — a consult must not reach the case store via directory recursion.
    # On current code the deny fires only when the resolved ARG is under journal_dir; a
    # recursion tool rooted at an ancestor recurses in and is ALLOWED → these assertions fail.
    assert grep_allowed is False
    assert glob_allowed is False
    assert ls_allowed is False


# ---------------------------------------------------------------------------------------------
# B-M2-5 — case-store deny must be case-insensitive (APFS); is_relative_to is case-sensitive.
# ---------------------------------------------------------------------------------------------
def test_b_m2_5_case_isolation_is_casefolded(tmp_path):
    runner, _ = _runner(tmp_path)
    runner._run_gate_mode = "consult"
    runner._run_root = tmp_path

    case = tmp_path / "journal" / "threads" / "th.case.md"
    case.parent.mkdir(parents=True, exist_ok=True)
    case.write_text("# Дело треда", encoding="utf-8")

    recased = str(tmp_path / "JOURNAL" / "threads" / "th.case.md")
    if not os.path.exists(recased):
        pytest.skip("case-sensitive FS: re-cased journal segment does not resolve to the file")

    # DESIRED: the re-cased path opens the SAME case-store inode on APFS, so it must STILL be
    # denied (matches the _is_secret_path BLOCKER-1 casefold hardening). On current macOS code
    # resolve() preserves the typed case, is_relative_to(journal_dir) is case-sensitive → False
    # → the consult_case_private deny never fires → the case read is ALLOWED → this fails.
    allowed = runner._gate_decision("Read", {"file_path": recased})[0]
    assert allowed is False


# ---------------------------------------------------------------------------------------------
# B-M2-7 — a failed durable case-write must be ALERTED, not silently swallowed.
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_b_m2_7_failed_case_write_is_alerted(tmp_path):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    runner, store = _runner(tmp_path, on_case_entry=boom)
    alerts: list[tuple] = []
    runner._journal.alert = lambda *a, **k: alerts.append((a, k))

    store.start_task("c1", "brief", TaskStatus.RUNNING, 1.0)
    runner._run_owner = "c1"
    runner._run_kind = "consult"
    runner._run_thread_id = "th"

    tool = runner._build_reply_tool("c1")
    await tool.handler({"speak_text": "ответ", "final": True})

    # DESIRED: swallowing the I/O error keeps the SDK session alive, but a real durable-memory
    # write failure must raise a journal alert (the reply would otherwise vanish from the only
    # durable store, П-3). On current code the except is a bare `pass` → alerts stays empty.
    assert alerts, "a failed durable case-write must raise a journal alert"
