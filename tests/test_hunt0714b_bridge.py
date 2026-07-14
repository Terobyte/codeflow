"""Failing-tests proof for Hunt 2026-07-14b bugs B16 and B19 (docs/bugs.md §"Hunt
2026-07-14b"). Each test states the CORRECT behavior as its pass condition and is RED
against the current (unfixed) code. See the docstring on each test for the exact
expected-vs-actual from the ledger.

Touches production code: NONE. Touches other test files / bugs.md: NONE.
Harness patterns (KoraRunner construction, ProjectStore + fake-home) reuse
tests/test_hunt0714_b.py's B03/B05 fixtures.
"""
from __future__ import annotations

import json
from pathlib import Path

from synapse.bridge.kora import KoraRunner
from synapse.bridge.state import SpeakLedger, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal
from synapse.projects import ProjectStore


def _make_kora_runner(tmp_path: Path, workspace_dir: Path) -> KoraRunner:
    clock = FakeClock(0.0)
    cfg = SynapseConfig(kora_workspace_dir=str(workspace_dir))
    store = TaskStore(clock)  # no journal_dir → no state.json persistence
    ledger = SpeakLedger()
    journal = TurnJournal(str(tmp_path / "journal"), clock, session_id="s")
    return KoraRunner(cfg, store, ledger, clock, journal, on_speak=None)


# =========================================================================================
# B16 — gate secret-containment is bypassable via Grep/Glob/LS recursion into a directory.
# location: synapse/bridge/kora.py:553-567,569-596
# expected: any read of a secret file under the workspace is denied for EVERY file tool —
#           incl. a directory-recursion tool (Grep/Glob/LS) pointed at a dir that contains
#           the secret; the module contract is "denied for ALL file tools even inside the
#           workspace".
# actual:   the gate only checks the resolved PATH it is handed. A no-/dot-path Grep against
#           a legit workspace resolves to the NON-secret workspace dir → allowed, and ripgrep
#           then recurses into secrets.yaml and returns its bytes (output_mode:"content").
#           Read(workspace/secrets.yaml) IS denied → the exact asymmetry the contract forbids.
# =========================================================================================


def test_B16_grep_recursion_into_secret_file_must_be_denied(tmp_path):
    # A perfectly LEGIT workspace root (not itself secret) that happens to hold a non-hidden
    # secret file the gate's own _SECRET_FILE_NAMES recognizes.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "secrets.yaml").write_text("token: super-secret\n", encoding="utf-8")
    runner = _make_kora_runner(tmp_path, workspace)

    # The single-file Read of that secret IS denied — establishes the invariant + the baseline
    # side of the asymmetry.
    read_allowed, _rd, read_cat = runner._gate_decision(
        "Read", {"file_path": str(workspace / "secrets.yaml")}
    )
    assert read_allowed is False and read_cat == "secret_path", (
        "sanity: Read of a secret file under the workspace must be denied (secret_path), "
        f"got allowed={read_allowed} category={read_cat!r}"
    )

    # A content Grep at path "." recurses into the very same secret bytes — it must be denied
    # for the SAME reason, or the secret leaks around the per-file check.
    grep_allowed, _gd, grep_cat = runner._gate_decision(
        "Grep", {"path": ".", "output_mode": "content"}
    )
    assert grep_allowed is False, (
        "a content Grep that recurses into a secret file under the workspace must be DENIED — "
        "the gate must not read via directory recursion what it denies per-file; "
        f"got allowed={grep_allowed} category={grep_cat!r}"
    )
    assert grep_cat == "secret_path"


# =========================================================================================
# B19 — ProjectStore._load re-admits secret-rooted projects with zero validation.
# location: synapse/projects.py:57-69 vs 28-47,83-89
# expected: a persisted project whose path is a _FORBIDDEN_HOME_SUBDIRS member (e.g. ~/.ssh)
#           is rejected/dropped on load exactly as add() would (validate_project_path is a
#           STORE invariant, not merely a write-path check) — get()/list() never serve it.
# actual:   _load builds {id,name,path} straight from projects.json with no validation, so a
#           secret-rooted row is loaded verbatim and served — re-arming the B16/B03 surface.
# =========================================================================================


def test_B19_load_must_drop_secret_rooted_project(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    secret_root = fake_home / ".ssh" / "some-project"
    secret_root.mkdir(parents=True)  # validate_project_path's is_dir() side would also pass
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    projects_json = tmp_path / "projects.json"
    projects_json.write_text(
        json.dumps([{"id": "p1", "name": "leak", "path": str(secret_root)}]),
        encoding="utf-8",
    )

    store = ProjectStore(projects_json)

    assert store.get("p1") is None, (
        "a persisted project rooted at ~/.ssh must NOT be served by get() after load — "
        f"validation must be a store invariant, got {store.get('p1')!r}"
    )
    assert all(p["id"] != "p1" for p in store.list()), (
        f"a secret-rooted project must not appear in list(), got {store.list()!r}"
    )
