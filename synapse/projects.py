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
# B05: this denylist is the FIRST line of defence — it decides where Kora's workspace root may
# be pinned. It must cover every secret-dir segment `bridge/kora.py::_SECRET_DIR_SEGMENTS`
# treats as sensitive, or a project rooted at ~/.ssh (etc.) sails past validation and ARMS the
# gate's no-path exfiltration hole (B03). Keep in sync with _SECRET_DIR_SEGMENTS.
_FORBIDDEN_HOME_SUBDIRS = (
    ".config", ".gnupg", "Library/Keychains", ".ssh", ".aws", ".kube", ".docker",
)


class ProjectValidationError(ValueError):
    pass


def validate_project_path(raw: str, *, require_exists: bool = True) -> Path:
    """Валидация корня проекта. `require_exists=False` (путь загрузки, B19) снимает ТОЛЬКО
    проверку существования директории — секрет/системный/домашний денилист остаётся жёстким.
    Проект на временно отмонтированном диске обязан пережить рестарт, а вот секрет-корень —
    нет: безопасность — инвариант СТОРА, а не только write-пути (см. `_load`)."""
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
    # Security boundaries must follow the host filesystem's practical semantics. macOS is
    # normally case-insensitive, while Path.is_relative_to is lexical and case-sensitive:
    # with require_exists=False, ~/.SSH used to bypass the ~/.ssh denylist. Compare components
    # with casefold so both persisted and newly supplied spellings hit the same boundary.
    rp_parts = tuple(part.casefold() for part in rp.parts)
    home_parts = tuple(part.casefold() for part in home.parts)
    relative_parts = rp_parts[len(home_parts):] if rp_parts[:len(home_parts)] == home_parts else ()
    for sub in _FORBIDDEN_HOME_SUBDIRS:
        forbidden_parts = tuple(part.casefold() for part in Path(sub).parts)
        if relative_parts[:len(forbidden_parts)] == forbidden_parts:
            raise ProjectValidationError("секретные директории запрещены")
    if require_exists and not rp.is_dir():
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
            loaded: list[dict] = []
            for d in data:
                if not (isinstance(d, dict) and d.get("id")):
                    continue
                raw_path = str(d.get("path") or "")
                try:
                    # B19: re-validate on load with the SAME security denylist add() uses, so a
                    # project persisted before the B05 denylist shipped — or a hand-edited
                    # projects.json — cannot re-admit a secret-rooted workspace (B03/B16 surface).
                    # require_exists=False: a transiently-missing dir survives; only the denylist
                    # is enforced. Validation is a store invariant, not just a write-path check.
                    validate_project_path(raw_path, require_exists=False)
                except ProjectValidationError:
                    continue
                loaded.append({"id": str(d["id"]), "name": str(d.get("name") or ""), "path": raw_path})
            self._projects = loaded

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

    async def remove(self, project_id: str) -> bool:
        """Удалить проект по id (UI-5, S31). Атомарный rewrite под тем же lock, что add.
        Возвращает True если проект был и удалён, False если не найден."""
        async with self._lock:
            before = len(self._projects)
            self._projects = [p for p in self._projects if p["id"] != project_id]
            if len(self._projects) == before:
                return False
            self._persist()
            return True
