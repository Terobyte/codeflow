"""ProjectStore — проекты UI v2 (спека §4). Валидация пути (S12): проект = существующая
директория, НЕ корень/HOME/системные пути/секретные директории — это клетка Коры, ошибка
здесь = write-доступ агента куда не надо. projects.json пишется атомарно под asyncio.Lock
(S28: UI и голос не гоняются). Секрет-ФАЙЛЫ внутри валидного проекта ловит гейт-денилист
kora.py (_is_secret_path) — вторая линия, не эта."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

# Проверяются и сырой абсолютный путь, и resolved (macOS: /etc -> /private/etc).
_SYSTEM_ROOTS = ("/System", "/Library", "/usr", "/etc", "/private/etc", "/bin", "/sbin")
_FORBIDDEN_HOME_SUBDIRS = (".config", ".gnupg", "Library/Keychains")


class ProjectValidationError(ValueError):
    pass


def validate_project_path(raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ProjectValidationError("нужен абсолютный путь")
    try:
        rp = p.resolve()
    except (OSError, RuntimeError) as e:
        raise ProjectValidationError("путь не резолвится") from e
    home = Path.home().resolve()
    if rp == Path("/") or rp == home:
        raise ProjectValidationError("корень и домашняя директория целиком запрещены")
    for root in _SYSTEM_ROOTS:
        if str(p).startswith(root + "/") or str(p) == root or rp.is_relative_to(root):
            raise ProjectValidationError("системные пути запрещены")
    for sub in _FORBIDDEN_HOME_SUBDIRS:
        if rp.is_relative_to(home / sub):
            raise ProjectValidationError("секретные директории запрещены")
    if not rp.is_dir():
        raise ProjectValidationError("директория не существует")
    return rp


class ProjectStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._projects: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(data, list):
            self._projects = [
                {"id": str(d["id"]), "name": str(d.get("name") or ""), "path": str(d.get("path") or "")}
                for d in data
                if isinstance(d, dict) and d.get("id")
            ]

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._projects, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path)

    def list(self) -> list[dict]:
        return [dict(p) for p in self._projects]

    def get(self, project_id: str) -> dict | None:
        return next((dict(p) for p in self._projects if p["id"] == project_id), None)

    async def add(self, name: str, path: str) -> dict:
        rp = validate_project_path(path)
        async with self._lock:
            proj = {"id": uuid.uuid4().hex[:8], "name": (name or rp.name)[:60], "path": str(rp)}
            self._projects.append(proj)
            self._persist()
            return proj
