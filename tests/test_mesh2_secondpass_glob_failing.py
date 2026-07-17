"""Багхант МЕШ-2 (второй заход, `9f42166`) — phase-2 RED test for B-M2-12.

One failing test proving the consult read-only gate isolates the private journal/case tree by
resolving ONLY the `path` argument of file tools — the `Glob` `pattern` argument is never
inspected. A consult `Glob(path=<benign dir that neither is under nor is an ancestor of
journal_dir>, pattern="<..-traversal that climbs into the journal case tree>")` slips past the
`consult_case_private` isolation and is ALLOWED, even though the traversal points straight at the
private case tree. Case-isolation is the whole point of consult mode → this is a fail-open in a
security gate.

Offline, deterministic, duck-typed. Mirrors the harness + layout of the shipped consult gate
tests in tests/test_mesh2_review_kora.py (B-M2-4 / B-M2-5): build a KoraRunner, flip it into a
consult run (`_run_gate_mode="consult"`, `_run_root=tmp_path`), lay the journal case tree under
tmp_path/journal, and invoke `_gate_decision(...)` directly (task_id=None → identity guard off).
"""
from __future__ import annotations

from pathlib import Path

from synapse.bridge.state import SpeakLedger, TaskStore
from synapse.bridge.kora import KoraRunner
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal


def _runner(tmp_path):
    """Local copy of the tests/test_mesh2_review_kora.py `_runner` harness (no on_speak needed)."""
    clock = FakeClock(1.0)
    store = TaskStore(clock)
    runner = KoraRunner(
        SynapseConfig(
            kora_workspace_dir=str(tmp_path / "ws"),
            kora_cli_path="/bin/echo",
            journal_dir=str(tmp_path / "journal"),
            consult_idle_timeout_s=0.05,
        ),
        store,
        SpeakLedger(),
        clock,
        TurnJournal(str(tmp_path / "journal"), clock, session_id="mesh2"),
        None,
    )
    return runner, store


def test_b_m2_12_consult_glob_pattern_reaches_case_tree(tmp_path):
    runner, _ = _runner(tmp_path)
    # Same consult-run setup B-M2-4 uses: journal_dir (tmp_path/journal) sits UNDER the run root.
    runner._run_gate_mode = "consult"
    runner._run_root = tmp_path

    # The private case tree the consult run must never reach — exactly B-M2-4's layout.
    case = tmp_path / "journal" / "threads" / "th.case.md"
    case.parent.mkdir(parents=True, exist_ok=True)
    case.write_text("# Дело треда\nчужой бриф", encoding="utf-8")

    # A benign workspace subdir chosen so BOTH `_consult_case_overlap` branches MISS it:
    #   - it is NOT under journal_dir (tmp_path/journal), and
    #   - it is NOT an ancestor of journal_dir.
    # So the gate resolves `path` to something harmless and the case-isolation deny never fires
    # off the `path` argument.
    benign = tmp_path / "ws" / "src"
    benign.mkdir(parents=True, exist_ok=True)

    # The escape lives entirely in `pattern`: a ..-traversal from the benign subdir that climbs
    # back to tmp_path and dives into the journal case tree. `../..` from ws/src == tmp_path.
    pattern = "../../journal/threads/*.case.md"

    # Premise guard: the traversal must resolve to the journal case tree SPECIFICALLY, so that
    # ANY correct isolation fix (traversal-deny OR resolve-and-overlap on the pattern) denies it.
    assert (benign / pattern).resolve().parent == (tmp_path / "journal" / "threads").resolve()

    allowed, detail, category = runner._gate_decision(
        "Glob", {"path": str(benign), "pattern": pattern}
    )

    # DESIRED (fix-agnostic): case isolation must not depend on WHICH argument carries the
    # traversal — a consult Glob whose pattern points at the private case tree must be DENIED
    # with the same case-isolation category the shipped consult tests assert on. On current code
    # `_gate_decision` reads only `_PATH_KEY["Glob"]="path"`, the pattern is ignored, `path` is a
    # benign in-workspace dir → both overlap branches miss → ALLOW → these assertions fail.
    assert allowed is False, (
        f"consult Glob pattern-escape to the case tree was ALLOWED; "
        f"decision={(allowed, detail, category)}"
    )
    assert "consult_case_private" in category, (
        f"expected consult_case_private isolation deny; decision={(allowed, detail, category)}"
    )
