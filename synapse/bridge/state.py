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
import copy
import threading
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


@dataclass(frozen=True)
class TaskStartCheckpoint:
    """Rollback token for the host's cross-store run-start saga.

    A gate launch replaces the global task slot before the external runner is started.  If
    that last step fails, restoring only ``None`` would discard the previous terminal task
    (and its liveness history).  The token therefore carries the complete pre-launch state
    and is accepted only while the task it belongs to still owns the slot.
    """

    started_task_id: str
    task: TaskState | None
    last_event_ts: float | None
    staged: dict[str, Any] | None


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


def should_hide_task(
    task: TaskState | None, asking_thread_id: str | None, owner_thread_id: str | None
) -> bool:
    """Терминальная (COMPLETED/FAILED) задача привязана к своему треду-владельцу и НЕ должна
    всплывать как «текущий статус» в чужом треде. Стор — глобальный синглтон (один Кора, одна
    задача в state.json), но завершённая задача не должна течь в несвязанный разговор: без этого
    диспетчер в КАЖДОМ треде повторял «задача выполнена, Кора смотрела проект X час назад».
    Активная задача (RUNNING/PENDING_CONFIRMATION) остаётся глобальной — Кора реально занята, и
    has_active_task всё равно блокирует параллельный submit. Осиротевшая терминальная задача
    (owner=None — стейл-остаток из state.json, чей тред не резолвится) прячется ото ВСЕХ: None !=
    любой thread_id → hide; в родном треде owner совпадает с asking → показываем результат."""
    if task is None or task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        return False
    return owner_thread_id != asking_thread_id


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
        # TaskStore is called by the ASGI loop, Kora callbacks and a few synchronous adapters.
        # Serialize the complete mutate+snapshot+rename critical section: locking only the
        # rename still allows one writer to snapshot another writer's half-applied mutation.
        self._lock = threading.RLock()
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
        with self._lock:
            self._start_task_unlocked(task_id, text, status, now)
            self._persist_unlocked()
            return self._task

    def begin_task(
        self, task_id: str, text: str, status: TaskStatus, now: float
    ) -> TaskStartCheckpoint:
        """Replace the task slot and return a conditional rollback token.

        This is intentionally narrower than a general transaction API: it exists for the
        host's launch saga, where TaskStore and ThreadStore must be compensated if the runner
        rejects the launch.  Ordinary confirm-flow callers continue to use ``start_task``.
        """
        with self._lock:
            checkpoint = TaskStartCheckpoint(
                started_task_id=task_id,
                task=copy.deepcopy(self._task),
                last_event_ts=self._last_event_ts,
                staged=copy.deepcopy(self._staged),
            )
            self._start_task_unlocked(task_id, text, status, now)
            try:
                self._persist_unlocked()
            except Exception:
                self._restore_checkpoint_unlocked(checkpoint)
                raise
            return checkpoint

    def rollback_task_start(self, checkpoint: TaskStartCheckpoint) -> bool:
        """Restore a failed launch iff its task still owns the global slot."""
        with self._lock:
            if self._task is None or self._task.id != checkpoint.started_task_id:
                return False
            self._restore_checkpoint_unlocked(checkpoint)
            self._persist_unlocked()
            return True

    def _start_task_unlocked(
        self, task_id: str, text: str, status: TaskStatus, now: float
    ) -> None:
        self._task = TaskState(
            id=task_id, text=text, status=status, started_ts=now,
            last_event_ts=None, events=[],
        )
        # Liveness belongs to the task occupying the slot. Carrying the previous task's
        # heartbeat into a new run can immediately classify the new run as unreachable.
        self._last_event_ts = None

    def _restore_checkpoint_unlocked(self, checkpoint: TaskStartCheckpoint) -> None:
        self._task = copy.deepcopy(checkpoint.task)
        self._last_event_ts = checkpoint.last_event_ts
        self._staged = copy.deepcopy(checkpoint.staged)

    def stage_task(self, task_id: str, text: str, staged: dict[str, Any], now: float) -> TaskState:
        """B12: atomically create a PENDING_CONFIRMATION task AND its staged blob in ONE persist.
        ConfirmFlow.submit used to do start_task() then set_staged() as two separate writes, so a
        crash between them left a PENDING_CONFIRMATION task with staged=null on disk — wedging the
        flow forever (has_active_task() blocks every submit while confirm() rejects). One persist
        closes the window; `_load` reconciles any state.json still carrying the old two-write scar."""
        with self._lock:
            self._task = TaskState(
                id=task_id, text=text, status=TaskStatus.PENDING_CONFIRMATION,
                started_ts=now, last_event_ts=None, events=[],
            )
            self._last_event_ts = None
            self._staged = staged
            self._persist_unlocked()
            return self._task

    def set_task_status(self, status: TaskStatus) -> None:
        with self._lock:
            if self._task is None:
                return
            if self._task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                # Terminal statuses are set only by Kora events (apply_event); a later
                # set_task_status (e.g. confirm-flow RUNNING) must not resurrect a finished task (Bug 6).
                return
            self._task.status = status
            self._persist_unlocked()

    def clear_task(self) -> None:
        """Drops the current task entirely — used when a staged (never-sent-to-Kora)
        confirmation is abandoned (deny/timeout/rereadback-exhausted): there is nothing to
        remember, it never became Kora's problem."""
        with self._lock:
            self._task = None
            self._last_event_ts = None
            self._persist_unlocked()

    def request_cancel(self) -> bool:
        with self._lock:
            if self._task is None or self._task.status not in (TaskStatus.RUNNING, TaskStatus.PENDING_CONFIRMATION):
                return False
            self._task.status = TaskStatus.CANCEL_REQUESTED
            self._persist_unlocked()
            return True

    def set_staged(self, staged: dict[str, Any] | None) -> None:
        with self._lock:
            self._staged = staged
            self._persist_unlocked()

    def heartbeat(self, now: float) -> None:
        with self._lock:
            self._last_event_ts = now
            self._persist_unlocked()

    def apply_event(self, event: KoraEvent) -> None:
        with self._lock:
            self._last_event_ts = event.ts
            if self._task is not None:
                new_status = self._EVENT_STATUS.get(event.type)
                already_terminal = self._task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
                terminal_event = new_status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
                # B14: once the task is terminal, a REPEAT terminal signal (a second ResultMessage /
                # a task_failed after task_completed — the SDK stream loop has no break) must be a
                # true no-op for the record. The B3 guard below already protects `.status`, but the
                # append was OUTSIDE it, so the duplicate terminal event still grew `task.events`
                # (rendered as a phantom extra line in snapshot/render_state). Skip the append too.
                if not (already_terminal and terminal_event):
                    self._task.events.append(event)
                    self._task.last_event_ts = event.ts
                    # B3: never overwrite a terminal status (COMPLETED→FAILED or resurrect to RUNNING).
                    if new_status is not None and not already_terminal:
                        self._task.status = new_status
            self._persist_unlocked()

    def liveness(self, now: float, stale_after_s: float, unreachable_after_s: float) -> Liveness:
        # E5 (MAJOR-R1): while parked on a user answer, Kora is ALIVE — blocked on the human, not
        # dead. Report OK FIRST so a long human wait never trips a false STALE/UNREACHABLE (which
        # would make the dispatcher say «нет сигнала» on the happy path).
        # B19: gated on the task actually RUNNING (mirrors render_state/snapshot) — after
        # request_cancel flips status, a stale _awaiting_answer flag must not keep reporting OK.
        # No-task+awaiting stays OK (transient set_awaiting window, see test_answer_kora §4).
        if self._awaiting_answer and (self._task is None or self._task.status == TaskStatus.RUNNING):
            return Liveness.OK
        # B23: a genuinely COMPLETED task means Кора finished and stopped emitting heartbeats —
        # the ever-growing age of the last event is NOT a liveness signal, so report OK. Otherwise
        # the dispatcher says «Кора не в сети» / refuses to dispatch after a completed task
        # (staging 2026-07-14: STALE/UNREACHABLE alerts fired at 12:52/12:55 AFTER task_completed
        # at 12:50). This mirrors _status_color's R2 treatment (COMPLETED beats a stale liveness),
        # moved into liveness() so the dispatcher path sees the same truth.
        # COMPLETED only, NOT FAILED: a task is set FAILED both genuinely (a Kora task_failed) AND
        # by the S13 zombie-reconcile on boot (RUNNING-at-crash → FAILED, _load), and those two are
        # indistinguishable from status alone — collapsing FAILED→OK here would make a dead-runner
        # restart falsely report OK and break R6 (test_persistence_roundtrip_...). A zombie only
        # ever lands on FAILED, never COMPLETED, so COMPLETED→OK is unconditionally safe. The
        # residual (a genuinely-failed idle task still ages into UNREACHABLE) is parked as a design
        # tension in bugs.md — it needs a distinct zombie marker to split the two FAILED sources.
        if self._task is not None and self._task.status == TaskStatus.COMPLETED:
            return Liveness.OK
        if self._last_event_ts is None:
            return Liveness.OK
        age = now - self._last_event_ts
        if age >= unreachable_after_s:
            return Liveness.UNREACHABLE
        if age >= stale_after_s:
            return Liveness.STALE
        return Liveness.OK

    def render_state(self, now: float, stale_after_s: float, unreachable_after_s: float,
                     *, hide_task: bool = False) -> str:
        live = self.liveness(now, stale_after_s, unreachable_after_s)
        # hide_task: терминальная задача чужого треда (should_hide_task) — рендерим как «нет
        # задачи», чтобы завершённая задача не текла в несвязанный разговор диспетчера.
        if self._task is None or hide_task:
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

    def snapshot(self, now: float, stale_after_s: float, unreachable_after_s: float,
                 *, hide_task: bool = False) -> dict[str, Any]:
        """Same redaction as render_state — used by the get_task_status() tool result.
        hide_task: терминальная задача чужого треда прячется (see should_hide_task)."""
        live = self.liveness(now, stale_after_s, unreachable_after_s)
        if self._task is None or hide_task:
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
        if self._awaiting_answer and self._task is not None and self._task.status == TaskStatus.RUNNING:
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

    def resync_greeting(self, now: float, stale_after_s: float, unreachable_after_s: float) -> str | None:
        # M1 slice 5 (§2.7): deterministic "welcome back" prefix for the reconnect resync greeting
        # (no LLM — R2-крит: the first thing spoken after a dead zone can't ride the slowest
        # path). `None` on a virgin host (no task ever started): "с возвращением" would be a lie
        # with nothing to resync to. Otherwise delegates the status suffix to
        # `render_state_template` rather than re-deriving the awaiting/stale tri-state priority a
        # third time (A4 disposition) — this inherits that method's already-tested behavior.
        if self._task is None:
            return None
        text = self._task.text
        if len(text) > 60:
            text = text[:60] + "…"
        suffix = self.render_state_template(now, stale_after_s, unreachable_after_s)
        return f"С возвращением. Задача «{text}»: {suffix}"

    def _persist(self) -> None:
        with self._lock:
            self._persist_unlocked()

    def _persist_unlocked(self) -> None:
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
        # B18: a corrupt or old-schema state.json must NOT crash boot — persisted state is a
        # best-effort restart aid (R6), not a hard dependency. A non-dict payload, a missing/renamed
        # field, or a bad enum value → treat as "no persisted state" instead of propagating a
        # crash out of TaskStore.__init__ → build_host on every boot until the file is deleted.
        if not isinstance(data, dict):
            return
        try:
            task_data = data.get("task")
            self._task = _task_from_dict(task_data) if isinstance(task_data, dict) else None
            self._last_event_ts = data.get("last_event_ts")
            staged = data.get("staged")
            self._staged = staged if isinstance(staged, dict) else None
        except (KeyError, ValueError, TypeError):
            self._task = None
            self._last_event_ts = None
            self._staged = None
        # S13 (UI v2, слайс UI-2): зомби-реконсиляция бута. RUNNING в state.json на старте
        # процесса = сервер умер посреди рана: живого продюсера после рестарта не существует
        # по определению, а оставить как есть — liveness врёт OK и has_active_task() режет
        # любой submit НАВСЕГДА. Это не resurrection (статус идёт В терминал, не из него);
        # PENDING_CONFIRMATION/CANCEL_REQUESTED не трогаем — их чинит обычный флоу.
        if self._task is not None and self._task.status == TaskStatus.RUNNING:
            self._task.status = TaskStatus.FAILED
            # `_last_event_ts` deliberately keeps the pre-crash value: it measures when KORA last
            # spoke, and this reconcile event is the server writing a note to itself, not a signal
            # from a runner that no longer exists. Refreshing it to boot time would age the zombie
            # to 0 and report OK — the exact lie this block exists to stop (and R6,
            # test_persistence_roundtrip_restart_reports_stale_immediately, pins it).
            boot_ts = self._clock.now()
            self._task.events.append(
                KoraEvent(
                    id=f"boot-reconcile-{self._task.id}",
                    type="task_failed",
                    cls=EventClass.NARRATABLE,
                    payload={"reason": "сервер перезапускался"},
                    speak_text=None,
                    ts=boot_ts,
                )
            )
            self._persist()


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

    def register_speak_text(self, text: str, ts: float) -> list[str]:
        """Mark every pending critical whose SPEAK text matches as spoken. The console runner
        registers by event_id; the voice path only has Kora's ready text at on_speak time, so
        match on speak_text (M0: register_critical wiring itself awaits the WebSocket Kora bridge).
        Returns the ids it newly marked so the caller can REVERT them (B01) if the SPEAK it
        optimistically registered turns out to have been dropped in delivery."""
        marked: list[str] = []
        for event_id, entry in self._pending.items():
            if not entry.spoken and entry.event.speak_text == text:
                entry.spoken = True
                marked.append(event_id)
        return marked

    def revert_speak(self, event_ids: list[str] | None) -> None:
        """B01: un-mark criticals whose optimistic SPEAK registration was not actually delivered
        (the out-of-band injection raised). Reverting re-arms the Р-15г watchdog for the dropped
        critical instead of leaving it permanently recorded as spoken."""
        for event_id in event_ids or []:
            entry = self._pending.get(event_id)
            if entry is not None:
                entry.spoken = False

    def unspoken(self, now: float, min_age_s: float) -> list[KoraEvent]:
        # M1 slice 5 (§2.7): undelivered criticals to replay on a reconnect resync. `min_age_s`
        # excludes an event still fresh enough that its ORGANIC on_speak may just be in flight
        # (R2 disposition) — replaying it too would double-voice the same critical. `alerted`
        # does NOT exclude: the Р-15г watchdog having already logged the miss doesn't mean the
        # user ever actually heard it, and the voice still owes them the fact.
        return [
            e.event
            for e in self._pending.values()
            if not e.spoken and e.event.speak_text and now - e.event.ts >= min_age_s
        ]

    def check(self, now: float, window_s: float) -> list[tuple[str, dict[str, Any]]]:
        alerts: list[tuple[str, dict[str, Any]]] = []
        for event_id, entry in self._pending.items():
            if entry.spoken or entry.alerted:
                continue
            if now - entry.event.ts >= window_s:
                entry.alerted = True
                alerts.append(("CRITICAL_WITHOUT_SPEAK", {"event_id": event_id, "type": entry.event.type}))
        return alerts
