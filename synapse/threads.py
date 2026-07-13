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


@dataclass
class Thread:
    id: str
    title: str
    project_id: str | None = None
    stage: str = "collect"           # collect|propose|spec_plan|code|done — FSM въезжает в UI-4
    last_outcome: str | None = None  # completed|failed|cancelled — исход ПОСЛЕДНЕГО запуска
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
            "last_outcome": t.last_outcome, "created_ts": t.created_ts,
            "updated_ts": t.updated_ts, "task_ids": t.task_ids,
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

    def list(self) -> list[Thread]:
        return sorted(self._threads.values(), key=lambda t: t.updated_ts, reverse=True)

    def append_task(self, thread_id: str, task_id: str) -> None:
        t = self._threads.get(thread_id)
        if t is None:
            return
        t.task_ids.append(task_id)
        self._task_index[task_id] = thread_id
        t.updated_ts = self._clock.now()
        self._persist(t)

    def set_outcome(self, thread_id: str, outcome: str) -> None:
        t = self._threads.get(thread_id)
        if t is None:
            return
        t.last_outcome = outcome
        t.updated_ts = self._clock.now()
        self._persist(t)

    def thread_for_task(self, task_id: str) -> Thread | None:
        tid = self._task_index.get(task_id)
        return self._threads.get(tid) if tid else None

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
