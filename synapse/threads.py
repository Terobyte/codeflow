"""ThreadStore — треды UI v2 (спека §4). Тред = НАДСТРОЙКА над TaskStore: синглтон «одна
активная задача» не тронут, тред хранит СВОИ task_ids + ленту. Писатель метаданных —
синхронно в точках переходов (находка G), atomic tmp+rename как state.json. Лента (S3) —
append-only jsonl per-thread; ring-буфер хоста остаётся горячим кэшем, файл — правдой,
переживающей рестарт. Никакой Р-15-логики здесь нет: лента display-only по построению
(пишется вторым потребителем log_sink, читается только HTTP-роутом)."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from synapse.clock import Clock

# Таблица легальных переходов стадии (UI-4). done — терминальная, без исходящих;
# collect → propose — «вперёд» гейт-флоу из сбора; collect → done — завершение чистого
# direct-dispatch треда (B47: тред без гейт-флоу, единственная активность — прямая задача
# диспетчера, обязан покидать «СБОР» когда задача выполнена); revise (→ collect) доступен
# из каждой рабочей стадии (propose/spec_plan/code), см. спеку:57/60/96.
_STAGE_TRANSITIONS: dict[str, frozenset[str]] = {
    "collect": frozenset({"propose", "done"}),
    "propose": frozenset({"spec_plan", "code", "collect"}),
    "spec_plan": frozenset({"code", "collect"}),
    "code": frozenset({"done", "collect"}),
    "done": frozenset(),
}


@dataclass
class Thread:
    id: str
    title: str
    project_id: str | None = None
    stage: str = "collect"           # collect|propose|spec_plan|code|done — FSM въезжает в UI-4
    last_outcome: str | None = None  # completed|failed|cancelled — исход ПОСЛЕДНЕГО запуска
    request_text: str | None = None  # свод запроса — носитель между COLLECT и запусками (UI-4)
    last_model: str | None = None    # модель последнего гейт-запуска (UI-4)
    archived: bool = False           # архив треда (UI-5)
    created_ts: float = 0.0
    updated_ts: float = 0.0
    task_ids: list[str] = field(default_factory=list)


class ThreadStore:
    def __init__(self, clock: Clock, root: str | Path, feed_max: int = 2000) -> None:
        self._clock = clock
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._feed_max = feed_max
        self._threads: dict[str, Thread] = {}
        self._task_index: dict[str, str] = {}
        self._feed_counts: dict[str, int] = {}
        self._load()

    # --- метаданные (находка G) ---------------------------------------------------------

    def _load(self) -> None:
        for p in self._root.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue  # битый файл — пропуск, не крэш бута (паттерн B18)
            if not isinstance(d, dict) or not d.get("id"):
                continue
            t = Thread(
                id=str(d["id"]),
                title=str(d.get("title") or ""),
                project_id=d.get("project_id"),
                stage=str(d.get("stage") or "collect"),
                last_outcome=d.get("last_outcome"),
                request_text=d.get("request_text"),
                last_model=d.get("last_model"),
                archived=bool(d.get("archived", False)),
                created_ts=float(d.get("created_ts") or 0.0),
                updated_ts=float(d.get("updated_ts") or 0.0),
                task_ids=[str(x) for x in (d.get("task_ids") or [])],
            )
            self._threads[t.id] = t
            for tid in t.task_ids:
                self._task_index[tid] = t.id

    def _persist(self, t: Thread) -> None:
        path = self._root / f"{t.id}.json"
        tmp = path.with_suffix(".json.tmp")
        data = {
            "id": t.id, "title": t.title, "project_id": t.project_id, "stage": t.stage,
            "last_outcome": t.last_outcome, "request_text": t.request_text,
            "last_model": t.last_model, "archived": t.archived,
            "created_ts": t.created_ts, "updated_ts": t.updated_ts, "task_ids": t.task_ids,
        }
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def create(self, title: str, project_id: str | None = None) -> Thread:
        now = self._clock.now()
        t = Thread(id=uuid.uuid4().hex[:12], title=title[:80], project_id=project_id,
                   created_ts=now, updated_ts=now)
        self._threads[t.id] = t
        self._persist(t)
        return t

    def get(self, thread_id: str) -> Thread | None:
        return self._threads.get(thread_id)

    def list(self, include_archived: bool = False) -> list[Thread]:
        items = self._threads.values() if include_archived else (
            t for t in self._threads.values() if not t.archived
        )
        return sorted(items, key=lambda t: t.updated_ts, reverse=True)

    def append_task(self, thread_id: str, task_id: str) -> None:
        t = self._threads.get(thread_id)
        if t is None:
            return
        t.task_ids.append(task_id)
        self._task_index[task_id] = thread_id
        t.updated_ts = self._clock.now()
        self._persist(t)

    def set_outcome(self, thread_id: str, outcome: str | None) -> None:
        """Исход последнего запуска. `None` (B07) СБРАСЫВАЕТ исход — revise регрессирует стадию в
        collect и обязан обнулить «completed» от прошлого запроса, иначе write_code примет старый
        план как свежий для нового запроса."""
        t = self._threads.get(thread_id)
        if t is None:
            return
        t.last_outcome = outcome
        t.updated_ts = self._clock.now()
        self._persist(t)

    def thread_for_task(self, task_id: str) -> Thread | None:
        tid = self._task_index.get(task_id)
        return self._threads.get(tid) if tid else None

    # --- стадийный FSM (UI-4) -----------------------------------------------------------

    def set_stage(self, thread_id: str, stage: str) -> None:
        """Перевод стадии по таблице _STAGE_TRANSITIONS. Нелегальный переход → ValueError,
        персист не трогается. Стадия двигается ТОЛЬКО здесь (S2)."""
        t = self._threads.get(thread_id)
        if t is None:
            return
        allowed = _STAGE_TRANSITIONS.get(t.stage, frozenset())
        if stage not in allowed:
            raise ValueError(f"illegal stage transition {t.stage!r} → {stage!r}")
        t.stage = stage
        t.updated_ts = self._clock.now()
        self._persist(t)

    def set_request(self, thread_id: str, text: str) -> None:
        """Свод запроса — носитель между COLLECT и запусками стадий."""
        t = self._threads.get(thread_id)
        if t is None:
            return
        t.request_text = text
        t.updated_ts = self._clock.now()
        self._persist(t)

    def set_last_model(self, thread_id: str, model: str) -> None:
        """Модель последнего гейт-запуска (дефолт-кандидат для следующего, находка E)."""
        t = self._threads.get(thread_id)
        if t is None:
            return
        t.last_model = model
        t.updated_ts = self._clock.now()
        self._persist(t)

    def bind_project(self, thread_id: str, project_id: str) -> bool:
        """Привязка проекта к треду (находка F): ок только при null→значение и пустых task_ids.
        Повторная привязка / после запуска / значение→значение → отказ (False)."""
        t = self._threads.get(thread_id)
        if t is None:
            return False
        if t.project_id is not None or t.task_ids:
            return False
        t.project_id = project_id
        t.updated_ts = self._clock.now()
        self._persist(t)
        return True

    # --- заголовки: авто-title + rename (UI-5, S30) -------------------------------------

    def maybe_autotitle(self, thread_id: str, text: str) -> bool:
        """Авто-title из первой пользовательской реплики — ТОЛЬКО пока title несёт сентинель
        «новый тред» (треды домашнего композера без title; голосовой/HTTP commit-пути создают
        тред с title=text задачи — им auto-title не нужен и здесь no-op). Обрезка 80, как create.
        Второй ход не переименовывает осмысленный title (title уже не сентинель)."""
        t = self._threads.get(thread_id)
        if t is None:
            return False
        if t.title != "новый тред":
            return False  # осмысленный title (commit-путь или уже переименованный) — не трогаем
        title = (text or "").strip().replace("\n", " ")[:80]
        if not title:
            return False
        t.title = title
        t.updated_ts = self._clock.now()
        self._persist(t)
        return True

    def rename(self, thread_id: str, title: str) -> bool:
        """Явное переименование треда пользователем. Пустой title → отказ (роут вернёт 400).
        Возвращает True если тред существует и переименован."""
        t = self._threads.get(thread_id)
        if t is None:
            return False
        t.title = title[:80]
        t.updated_ts = self._clock.now()
        self._persist(t)
        return True

    # --- архив треда (UI-5, S31) -------------------------------------------------------

    def set_archived(self, thread_id: str, archived: bool) -> bool:
        """Архивация/разархивация треда. Архивированный тред исключается из обычного list()
        и GET /api/threads, виден с ?archived=1. Лента и метаданные сохраняются."""
        t = self._threads.get(thread_id)
        if t is None:
            return False
        t.archived = archived
        t.updated_ts = self._clock.now()
        self._persist(t)
        return True

    def unbind_project(self, project_id: str) -> int:
        """Снять привязку к удалённому проекту со всех его тредов (UI-5, S31): треды НЕ
        удаляются, лишь project_id → None + event «проект удалён» в их ленты. Возвращает
        число затронутых тредов."""
        count = 0
        now = self._clock.now()
        for t in self._threads.values():
            if t.project_id == project_id:
                t.project_id = None
                t.updated_ts = now
                self._persist(t)
                self.append_feed(t.id, {"ts": now, "kind": "event", "text": "проект удалён"})
                count += 1
        return count

    # --- лента (S3) -----------------------------------------------------------------------

    def _feed_path(self, thread_id: str) -> Path:
        return self._root / f"{thread_id}.feed.jsonl"

    def append_feed(self, thread_id: str, entry: dict) -> None:
        path = self._feed_path(thread_id)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        n = self._feed_counts.get(thread_id)
        if n is None:
            n = sum(1 for _ in path.open(encoding="utf-8"))
        else:
            n += 1
        self._feed_counts[thread_id] = n
        if n > self._feed_max * 1.2:  # редкий rewrite вместо перечитывания на каждый append
            lines = path.read_text(encoding="utf-8").splitlines()[-self._feed_max:]
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp.replace(path)
            self._feed_counts[thread_id] = len(lines)

    def read_feed(self, thread_id: str, limit: int = 200) -> list[dict]:
        path = self._feed_path(thread_id)
        if not path.exists():
            return []
        out: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                out.append(d)
        return out
