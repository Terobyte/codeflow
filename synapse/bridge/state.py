"""Мост Коры / стейт (§4, A3: events+state+speak merged into one module — one enum, one
dataclass and a small ledger with a single consumer didn't earn three files).

- `EventClass`/`KoraEvent`/`parse_event` — Kora's event vocabulary, fail-safe classification
  (Р-15б: no/unknown class → CRITICAL).
- `TaskStore` — the single active task (§1 invariant), Kora liveness (Р-11), [СОСТОЯНИЕ]
  rendering with critical-event redaction (Р-15), and a narrow persisted snapshot
  (`<journal_dir>/state.json`) so a restart during a dead Kora reports stale immediately
  instead of resetting the liveness clock (R6, accepted-narrow).
- `SpeakLedger` — the "critical ⇒ paired SPEAK" runtime invariant (Р-15г).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from synapse.clock import Clock
from synapse.prompt import CANON_PHRASE_STALE_KORA


class EventClass(str, Enum):
    CRITICAL = "critical"
    NARRATABLE = "narratable"


class TaskStatus(str, Enum):
    IDLE = "idle"
    PENDING_CONFIRMATION = "pending_confirmation"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"


class Liveness(str, Enum):
    OK = "ok"
    STALE = "stale"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class KoraEvent:
    id: str
    type: str
    cls: EventClass
    payload: dict[str, Any]
    speak_text: str | None
    ts: float


def parse_event(raw: dict[str, Any]) -> KoraEvent:
    """Fail-safe (Р-15б): a missing or unrecognized `class` is treated as CRITICAL — an
    unclassified event is assumed sensitive until proven otherwise."""
    try:
        cls = EventClass(raw.get("class"))
    except ValueError:
        cls = EventClass.CRITICAL
    return KoraEvent(
        id=str(raw.get("id") or raw.get("event_id") or ""),
        type=str(raw.get("type", "")),
        cls=cls,
        payload=raw.get("payload") or {},
        speak_text=raw.get("speak"),
        ts=float(raw.get("ts", 0.0)),
    )


@dataclass
class TaskState:
    id: str
    text: str
    status: TaskStatus = TaskStatus.IDLE
    started_ts: float | None = None
    last_event_ts: float | None = None
    events: list[KoraEvent] = field(default_factory=list)


def _fmt_ts(ts: float | None) -> str:
    return "нет" if ts is None else f"{ts:.1f}"


def _render_event(ev: KoraEvent) -> str:
    if ev.cls == EventClass.CRITICAL:
        return f"{ev.type} @ {_fmt_ts(ev.ts)} — детали озвучивает Кора дословно"
    payload = ", ".join(f"{k}={v}" for k, v in ev.payload.items())
    suffix = f": {payload}" if payload else ""
    return f"{ev.type} @ {_fmt_ts(ev.ts)}{suffix}"


def _event_to_dict(ev: KoraEvent) -> dict[str, Any]:
    return {
        "id": ev.id,
        "type": ev.type,
        "cls": ev.cls.value,
        "payload": ev.payload,
        "speak_text": ev.speak_text,
        "ts": ev.ts,
    }


def _event_from_dict(d: dict[str, Any]) -> KoraEvent:
    return KoraEvent(
        id=d["id"],
        type=d["type"],
        cls=EventClass(d["cls"]),
        payload=d.get("payload") or {},
        speak_text=d.get("speak_text"),
        ts=d["ts"],
    )


def _task_to_dict(t: TaskState) -> dict[str, Any]:
    return {
        "id": t.id,
        "text": t.text,
        "status": t.status.value,
        "started_ts": t.started_ts,
        "last_event_ts": t.last_event_ts,
        "events": [_event_to_dict(e) for e in t.events],
    }


def _task_from_dict(d: dict[str, Any]) -> TaskState:
    return TaskState(
        id=d["id"],
        text=d["text"],
        status=TaskStatus(d["status"]),
        started_ts=d.get("started_ts"),
        last_event_ts=d.get("last_event_ts"),
        events=[_event_from_dict(e) for e in d.get("events", [])],
    )


class TaskStore:
    """Holds the single active task (§1: "одна активная задача"), Kora liveness, and
    [СОСТОЯНИЕ] rendering. `journal_dir=None` (used by the console demo) disables
    persistence entirely — no state.json is read or written."""

    _EVENT_STATUS = {
        "task_started": TaskStatus.RUNNING,
        "task_completed": TaskStatus.COMPLETED,
        "task_failed": TaskStatus.FAILED,
    }

    def __init__(self, clock: Clock, journal_dir: str | Path | None = None) -> None:
        self._clock = clock
        self._task: TaskState | None = None
        self._last_event_ts: float | None = None
        self._staged: dict[str, Any] | None = None
        # M1 slice 3 (E5): a purely TRANSIENT flag — set while Kora's stream is blocked in the
        # gate on an AskUserQuestion, cleared the moment the answer is delivered/cancelled. NOT
        # persisted (like the live SDK stream it tracks): a dead runner post-restart must never
        # strand a stale «Кора ждёт ответа». Carries NO question text/options (Р-8/Р-15
        # redaction — the question is voiced only via on_speak, never rendered into [СОСТОЯНИЕ]).
        self._awaiting_answer: bool = False
        self._state_path: Path | None = Path(journal_dir) / "state.json" if journal_dir else None
        if self._state_path is not None:
            self._load()

    @property
    def task(self) -> TaskState | None:
        return self._task

    @property
    def staged(self) -> dict[str, Any] | None:
        return self._staged

    @property
    def awaiting_answer(self) -> bool:
        return self._awaiting_answer

    def set_awaiting(self) -> None:
        """Kora's stream is now parked in the gate waiting for the user's answer (E5). TRANSIENT
        — deliberately does NOT call `_persist`: this tracks a live SDK stream that cannot
        survive a restart, so it must never be written to state.json (P10)."""
        self._awaiting_answer = True

    def clear_awaiting(self) -> None:
        """The answer was delivered or the parked run was cancelled/superseded. TRANSIENT — no
        `_persist`, same contract as `set_awaiting`."""
        self._awaiting_answer = False

    def has_active_task(self) -> bool:
        return self._task is not None and self._task.status in (
            TaskStatus.RUNNING,
            TaskStatus.PENDING_CONFIRMATION,
        )

    def start_task(self, task_id: str, text: str, status: TaskStatus, now: float) -> TaskState:
        """Creates the (only) active TaskState — used by ConfirmFlow on commit/staged."""
        self._task = TaskState(id=task_id, text=text, status=status, started_ts=now, last_event_ts=None, events=[])
        self._persist()
        return self._task

    def set_task_status(self, status: TaskStatus) -> None:
        if self._task is None:
            return
        if self._task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            # Terminal statuses are set only by Kora events (apply_event); a later
            # set_task_status (e.g. confirm-flow RUNNING) must not resurrect a finished task (Bug 6).
            return
        self._task.status = status
        self._persist()

    def clear_task(self) -> None:
        """Drops the current task entirely — used when a staged (never-sent-to-Kora)
        confirmation is abandoned (deny/timeout/rereadback-exhausted): there is nothing to
        remember, it never became Kora's problem."""
        self._task = None
        self._persist()

    def request_cancel(self) -> bool:
        if self._task is None or self._task.status not in (TaskStatus.RUNNING, TaskStatus.PENDING_CONFIRMATION):
            return False
        self._task.status = TaskStatus.CANCEL_REQUESTED
        self._persist()
        return True

    def set_staged(self, staged: dict[str, Any] | None) -> None:
        self._staged = staged
        self._persist()

    def heartbeat(self, now: float) -> None:
        self._last_event_ts = now
        self._persist()

    def apply_event(self, event: KoraEvent) -> None:
        self._last_event_ts = event.ts
        if self._task is not None:
            self._task.events.append(event)
            self._task.last_event_ts = event.ts
            new_status = self._EVENT_STATUS.get(event.type)
            # B3: never overwrite a terminal status — a second ResultMessage / a task_failed after
            # task_completed (the SDK stream loop has no break) must not flip COMPLETED→FAILED or
            # resurrect a finished task to RUNNING. Mirrors the guard in set_task_status (Bug 6).
            if new_status is not None and self._task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                self._task.status = new_status
        self._persist()

    def liveness(self, now: float, stale_after_s: float, unreachable_after_s: float) -> Liveness:
        # E5 (MAJOR-R1): while parked on a user answer, Kora is ALIVE — blocked on the human, not
        # dead. Report OK FIRST so a long human wait never trips a false STALE/UNREACHABLE (which
        # would make the dispatcher say «нет сигнала» on the happy path).
        if self._awaiting_answer:
            return Liveness.OK
        if self._last_event_ts is None:
            return Liveness.OK
        age = now - self._last_event_ts
        if age >= unreachable_after_s:
            return Liveness.UNREACHABLE
        if age >= stale_after_s:
            return Liveness.STALE
        return Liveness.OK

    def render_state(self, now: float, stale_after_s: float, unreachable_after_s: float) -> str:
        live = self.liveness(now, stale_after_s, unreachable_after_s)
        if self._task is None:
            return f"[СОСТОЯНИЕ]\nАктивной задачи нет.\nСвязь с Корой: {live.value}."
        t = self._task
        lines = [
            "[СОСТОЯНИЕ]",
            f"Задача: {t.text}",
            f"id: {t.id}",
            f"Статус: {t.status.value}",
            f"Начата: {_fmt_ts(t.started_ts)}",
            f"Последний сигнал: {_fmt_ts(t.last_event_ts)}",
            f"Связь с Корой: {live.value}",
        ]
        # E5 (MAJOR-R4): REDACTED marker only — the question text/options are voiced by Kora via
        # on_speak (Р-8/Р-15 compliant), NEVER rendered here. Gated on RUNNING (R6).
        if self._awaiting_answer and t.status == TaskStatus.RUNNING:
            lines.append("Кора ждёт твоего ответа на свой вопрос (детали озвучены голосом).")
        lines.append("События:")
        if not t.events:
            lines.append("  (пока нет)")
        for ev in t.events:
            lines.append(f"  - {_render_event(ev)}")
        return "\n".join(lines)

    def snapshot(self, now: float, stale_after_s: float, unreachable_after_s: float) -> dict[str, Any]:
        """Same redaction as render_state — used by the get_task_status() tool result."""
        live = self.liveness(now, stale_after_s, unreachable_after_s)
        if self._task is None:
            return {"task": None, "liveness": live.value}
        t = self._task
        return {
            "task": {
                "id": t.id,
                "text": t.text,
                "status": t.status.value,
                "started_ts": t.started_ts,
                "last_event_ts": t.last_event_ts,
                "events": [
                    (
                        {"type": ev.type, "ts": ev.ts, "cls": "critical", "note": "детали озвучивает Кора дословно"}
                        if ev.cls == EventClass.CRITICAL
                        else {"type": ev.type, "ts": ev.ts, "cls": "narratable", "payload": ev.payload}
                    )
                    for ev in t.events
                ],
            },
            "liveness": live.value,
            # E5: bool only, gated RUNNING (R6) — no question text leaks (Р-8/Р-15, MAJOR-R4).
            "awaiting_answer": self._awaiting_answer and t.status == TaskStatus.RUNNING,
        }

    def render_state_template(self, now: float, stale_after_s: float, unreachable_after_s: float) -> str:
        """Deterministic Russian status phrase, no LLM (§11.5 cost-cap hard-stop path)."""
        live = self.liveness(now, stale_after_s, unreachable_after_s)
        if live != Liveness.OK:
            return CANON_PHRASE_STALE_KORA
        # E5 (MAJOR-R1): deterministic truth on the cost-cap hard-stop path — a parked question
        # is not a stalled task. Checked before the generic status phrases below.
        if self._awaiting_answer:
            return "Кора ждёт твоего ответа на свой вопрос."
        if self._task is None:
            return "Активных задач нет."
        status_phrases = {
            TaskStatus.PENDING_CONFIRMATION: "Задача ждёт подтверждения.",
            TaskStatus.RUNNING: "Задача выполняется, сигнала о завершении пока не было.",
            TaskStatus.COMPLETED: "Задача завершена.",
            TaskStatus.FAILED: "Задача завершилась с ошибкой.",
            TaskStatus.CANCEL_REQUESTED: "Запрос на отмену передан Коре.",
            TaskStatus.IDLE: "Активных задач нет.",
        }
        return status_phrases[self._task.status]

    def _persist(self) -> None:
        if self._state_path is None:
            return
        data = {
            "task": _task_to_dict(self._task) if self._task else None,
            "last_event_ts": self._last_event_ts,
            "staged": self._staged,
        }
        tmp = self._state_path.with_suffix(".json.tmp")
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._state_path)

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        task_data = data.get("task")
        self._task = _task_from_dict(task_data) if task_data else None
        self._last_event_ts = data.get("last_event_ts")
        self._staged = data.get("staged")


@dataclass
class _PendingCritical:
    event: KoraEvent
    spoken: bool = False
    alerted: bool = False


class SpeakLedger:
    """Runtime invariant (Р-15г): a `critical` event that doesn't get a paired SPEAK within
    the configured window is a journal alert — otherwise the [СОСТОЯНИЕ] redaction (Р-15)
    would turn a producer's forgotten SPEAK into silence nobody notices."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingCritical] = {}

    def register_critical(self, event: KoraEvent) -> None:
        if event.cls != EventClass.CRITICAL:
            return
        self._pending[event.id] = _PendingCritical(event=event)

    def register_speak(self, event_id: str, ts: float) -> None:
        entry = self._pending.get(event_id)
        if entry is not None:
            entry.spoken = True

    def register_speak_text(self, text: str, ts: float) -> None:
        """Mark every pending critical whose SPEAK text matches as spoken. The console runner
        registers by event_id; the voice path only has Kora's ready text at on_speak time, so
        match on speak_text (M0: register_critical wiring itself awaits the WebSocket Kora bridge)."""
        for entry in self._pending.values():
            if not entry.spoken and entry.event.speak_text == text:
                entry.spoken = True

    def check(self, now: float, window_s: float) -> list[tuple[str, dict[str, Any]]]:
        alerts: list[tuple[str, dict[str, Any]]] = []
        for event_id, entry in self._pending.items():
            if entry.spoken or entry.alerted:
                continue
            if now - entry.event.ts >= window_s:
                entry.alerted = True
                alerts.append(("CRITICAL_WITHOUT_SPEAK", {"event_id": event_id, "type": entry.event.type}))
        return alerts
