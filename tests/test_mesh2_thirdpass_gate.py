"""Багхант МЕШ-2 (третий заход, `ca4ce0d`) — RED tests for B-M2-16 / B-M2-17 / B-M2-18.

Three failing tests proving Pattern P-M2: the consult read-only gate's case-isolation check
(`consult_case_private`) depends on the ARGUMENT FORM, not just its content. The B-M2-12 fix
resolves and checks the `pattern`/`glob` string against the journal tree, and the ordinary
`path` string is resolved and checked too — but the moment either argument arrives as a
non-string (a list) instead of a string, or the traversal hides behind a pre-existing symlink
segment, the same escape that is correctly denied in its string/lexical form sails through as
`allow`.

Offline, deterministic, duck-typed. Harness cloned from
tests/test_mesh2_secondpass_glob_failing.py (B-M2-12): build a KoraRunner, flip it into a
consult run (`_run_gate_mode="consult"`, `_run_root=...`), lay the journal case tree on disk,
and invoke `_gate_decision(...)` directly (task_id=None → identity guard off).
"""
from __future__ import annotations

import os

import pytest

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


def test_b_m2_16_consult_glob_list_pattern_bypasses_traversal_check(tmp_path):
    runner, _ = _runner(tmp_path)
    # Same consult-run setup B-M2-4/B-M2-12 use: journal_dir (tmp_path/journal) sits UNDER the
    # run root.
    runner._run_gate_mode = "consult"
    runner._run_root = tmp_path

    # The private case tree the consult run must never reach.
    case = tmp_path / "journal" / "threads" / "th.case.md"
    case.parent.mkdir(parents=True, exist_ok=True)
    case.write_text("# Дело треда\nчужой бриф", encoding="utf-8")

    # Benign in-workspace `path` that neither is under nor is an ancestor of journal_dir — same
    # shape B-M2-12's test uses, so the escape must live entirely in `pattern`.
    benign = tmp_path / "ws" / "src"
    benign.mkdir(parents=True, exist_ok=True)

    string_pattern = "../../journal/threads/*.case.md"
    list_pattern = ["..", "..", "journal", "threads", "*.case.md"]

    # Premise guard: the traversal, written as a string, really does resolve into the private
    # case tree, and the shipped B-M2-12 fix denies it in that form. This proves the escape below
    # is purely about argument SHAPE, not content.
    assert (benign / string_pattern).resolve().parent == (
        tmp_path / "journal" / "threads"
    ).resolve()
    premise_allowed, _premise_detail, premise_category = runner._gate_decision(
        "Glob", {"path": str(benign), "pattern": string_pattern}
    )
    assert premise_allowed is False, (
        f"premise broken: string-form traversal pattern was itself allowed; "
        f"decision={(premise_allowed, _premise_detail, premise_category)}"
    )
    assert "consult_case_private" in premise_category

    # Same traversal, same segments, expressed as a list instead of a joined string. kora.py:1099
    # gates the B-M2-12 anti-traversal check behind `isinstance(search_pattern, str)` — a list
    # skips the check entirely and falls through to path-only validation, which never inspects
    # `pattern`.
    allowed, detail, category = runner._gate_decision(
        "Glob", {"path": str(benign), "pattern": list_pattern}
    )
    assert allowed is False, (
        f"consult Glob list-pattern traversal to the case tree was ALLOWED; "
        f"decision={(allowed, detail, category)}"
    )
    assert "consult_case_private" in category, (
        f"expected consult_case_private isolation deny; decision={(allowed, detail, category)}"
    )


def test_b_m2_17_consult_glob_list_path_bypasses_case_isolation(tmp_path):
    runner, _ = _runner(tmp_path)
    runner._run_gate_mode = "consult"
    # Run root DISJOINT from journal_dir — a sibling, not an ancestor — so the no-path/default
    # branch (which validates the workspace root instead of the real argument) does not itself
    # accidentally overlap the case tree and mask the bug.
    runner._run_root = tmp_path / "ws"
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)

    journal_dir = tmp_path / "journal"
    case = journal_dir / "threads" / "th.case.md"
    case.parent.mkdir(parents=True, exist_ok=True)
    case.write_text("# Дело треда\nчужой бриф", encoding="utf-8")

    # Premise guard: the string form of the very same path IS denied by the shipped
    # case-isolation check.
    premise_allowed, _premise_detail, premise_category = runner._gate_decision(
        "Glob", {"path": str(journal_dir), "pattern": "threads/*.case.md"}
    )
    assert premise_allowed is False, (
        f"premise broken: string-form path at the case tree was itself allowed; "
        f"decision={(premise_allowed, _premise_detail, premise_category)}"
    )
    assert "consult_case_private" in premise_category

    # Same target, wrapped in a one-element list. `raw = tool_input.get("path")` is a list, so
    # `isinstance(raw, str)` is False at kora.py:1129 and Glob falls into the "missing path"
    # branch for read/search tools, which defaults to (and validates) the run-root workspace
    # instead of the real, list-wrapped argument.
    allowed, detail, category = runner._gate_decision(
        "Glob", {"path": [str(journal_dir)], "pattern": "threads/*.case.md"}
    )
    assert allowed is False, (
        f"consult Glob list-path to the case tree was ALLOWED; decision={(allowed, detail, category)}"
    )
    assert "consult_case_private" in category, (
        f"expected consult_case_private isolation deny; decision={(allowed, detail, category)}"
    )


def test_b_m2_18_consult_glob_symlink_pattern_segment_escapes_lexical_check(tmp_path):
    runner, _ = _runner(tmp_path)
    runner._run_gate_mode = "consult"
    runner._run_root = tmp_path

    journal_dir = tmp_path / "journal"
    case = journal_dir / "threads" / "th.case.md"
    case.parent.mkdir(parents=True, exist_ok=True)
    case.write_text("# Дело треда\nчужой бриф", encoding="utf-8")

    benign = tmp_path / "ws" / "src"
    benign.mkdir(parents=True, exist_ok=True)

    # A REAL pre-existing symlink whose named segment ("case_link") is neither ".." nor
    # absolute — the lexical `..`/is_absolute() check at kora.py:1093-1102 has nothing to catch,
    # even though the segment resolves straight into the private case tree.
    link = benign / "case_link"
    try:
        os.symlink(journal_dir, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"filesystem/OS cannot create symlinks here: {exc}")

    # Premise guard: the symlink really does target the private case tree.
    assert (link / "threads").resolve().is_relative_to(journal_dir.resolve())

    allowed, detail, category = runner._gate_decision(
        "Glob", {"path": str(benign), "pattern": "case_link/threads/*.case.md"}
    )
    assert allowed is False, (
        f"consult Glob symlink-pattern escape to the case tree was ALLOWED; "
        f"decision={(allowed, detail, category)}"
    )
    assert "consult_case_private" in category, (
        f"expected consult_case_private isolation deny; decision={(allowed, detail, category)}"
    )
