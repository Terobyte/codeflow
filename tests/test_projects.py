"""UI v2 слайс UI-3: проекты (S12/S28) — валидация пути и атомарный projects.json."""
import json
from pathlib import Path

import pytest

from synapse.projects import ProjectStore, ProjectValidationError, validate_project_path


def test_validation_rejects_dangerous_paths(tmp_path):
    for bad in ["/", str(Path.home()), "/etc", "/private/etc", "/usr", "/System", "/Library",
                str(Path.home() / ".config"), str(Path.home() / ".gnupg"),
                str(Path.home() / "Library/Keychains"), "relative/path",
                str(tmp_path / "не-существует")]:
        with pytest.raises(ProjectValidationError):
            validate_project_path(bad)


def test_validation_accepts_real_project_dir(tmp_path):
    proj = tmp_path / "myproj"; proj.mkdir()
    assert validate_project_path(str(proj)) == proj.resolve()


async def test_store_add_list_atomic(tmp_path):
    store = ProjectStore(tmp_path / "projects.json")
    proj_dir = tmp_path / "p1"; proj_dir.mkdir()
    p = await store.add("Проект", str(proj_dir))
    assert p["name"] == "Проект" and p["path"] == str(proj_dir.resolve())
    again = ProjectStore(tmp_path / "projects.json")
    assert [x["id"] for x in again.list()] == [p["id"]]
    assert again.get(p["id"])["path"] == p["path"]


def test_gate_denylist_covers_shell_configs_and_config_dir(tmp_path):
    from synapse.bridge.kora import _is_secret_path
    for p in [tmp_path / ".zshrc", tmp_path / ".bash_profile", tmp_path / ".profile",
              tmp_path / ".config" / "gh" / "hosts.yml",
              tmp_path / "Keychains" / "login.keychain-db"]:
        assert _is_secret_path(p), f"{p} must be secret"
