"""TurnJournal — the observability spine (§3 "Наблюдаемость"). One JSONL file per session.

R2 (durability): `alert()` is the single piece of evidence the §8 крит.5 gate checks
("алерт «статус без grounding» — ноль срабатываний"). It writes its OWN line immediately,
flushed and fsync'd, independent of `end_turn()` — a crash between an alert and the end of
its turn must not erase the alert.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from synapse.clock import Clock

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from synapse.bridge.state import KoraEvent


class AlertKind(str, Enum):
    STATUS_WITHOUT_GROUNDING = "STATUS_WITHOUT_GROUNDING"
    CRITICAL_WITHOUT_SPEAK = "CRITICAL_WITHOUT_SPEAK"
    TAIL_TIER_ENTRY = "TAIL_TIER_ENTRY"
    ALL_TIERS_FAILED = "ALL_TIERS_FAILED"
    COST_CAP = "COST_CAP"
    CONFIRM_SELF_ATTEMPT = "CONFIRM_SELF_ATTEMPT"
    AUTH_FAILURE = "AUTH_FAILURE"
    KORA_RUN_FAILED = "KORA_RUN_FAILED"
    # B12 (Р-11): Kora's liveness degraded to stale/unreachable between turns — surfaced once on
    # the transition so a Kora that dies silently doesn't just read "running" until the next turn.
    KORA_UNREACHABLE = "KORA_UNREACHABLE"


@dataclass
class TurnRecord:
    turn_id: str
    ts: float
    transcript: str
    # UI-3 (спека §4, находка A): тред, в котором шёл ход. Ставится в loop.ingest_user_turn.
    thread_id: str = ""
    llm_output: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tts_texts: list[str] = field(default_factory=list)
    tier: dict[str, Any] | None = None
    breaker_mask: dict[str, Any] = field(default_factory=dict)
    retry: bool = False
    alerts: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: float | None = None


# Status vocabulary heuristic (§4): "a turn where the dispatcher talks about status/progress
# without a preceding get_task_status() this same turn is a journal alert." This is a lexical
# substring/regex match, NOT semantic understanding — it will both false-positive (casual use
# of "готово" unrelated to the task) and false-negative (a paraphrase without any of these
# roots). Documented honestly per item 6's spec, not sold as a real grounding detector.
_GROUNDING_PATTERN = re.compile(r"готов|заверш|выполн|прогресс|статус|осталось|законч", re.IGNORECASE)


class TurnJournal:
    def __init__(self, journal_dir: str | Path, clock: Clock, session_id: str | None = None) -> None:
        self._dir = Path(journal_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        sid = session_id or f"session-{int(clock.now() * 1000)}"
        self._path = self._dir / f"{sid}.jsonl"
        self._file = self._path.open("a", encoding="utf-8")
        self._current: TurnRecord | None = None
        self._turn_counter = 0
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def current(self) -> TurnRecord | None:
        return self._current

    def begin_turn(self, transcript: str) -> TurnRecord:
        self._turn_counter += 1
        self._current = TurnRecord(
            turn_id=f"t{self._turn_counter}",
            ts=self._clock.now(),
            transcript=transcript,
        )
        return self._current

    def record_tool_call(self, name: str, arguments: dict[str, Any], result: Any) -> None:
        """Single point of tool-call bookkeeping, called by the handler itself (not the
        caller) so the console/mock-LLM path and the real pipecat path record identically
        (item 15: "get_task_status ставит grounding-метку")."""
        if self._current is not None:
            self._current.tool_calls.append({"name": name, "arguments": arguments, "result": result})

    def alert(self, kind: AlertKind | str, detail: dict[str, Any] | str | None = None) -> None:
        row = {
            "kind": "alert",
            "alert_kind": AlertKind(kind).value,
            "ts": self._clock.now(),
            "turn_id": self._current.turn_id if self._current else None,
            "detail": detail,
        }
        if self._current is not None:
            self._current.alerts.append(row)
        # B39: alert() is called from cascade event handlers, confirm, and the monitor — a raising
        # os.fsync (disk full / fd closed) must NOT propagate into that machinery. Best-effort:
        # persist if we can, log if we can't. The in-record copy above survives regardless.
        try:
            self._write(row)
        except OSError as exc:
            logger.warning("journal alert write failed (best-effort, alert not persisted): %r", exc)

    def check_grounding(self, record: TurnRecord, has_active_task: bool) -> None:
        if not has_active_task:
            return
        if not _GROUNDING_PATTERN.search(record.llm_output or ""):
            return
        called_status = any(tc.get("name") == "get_task_status" for tc in record.tool_calls)
        if not called_status:
            self.alert(AlertKind.STATUS_WITHOUT_GROUNDING, {"llm_output": record.llm_output})

    def record_kora_event(self, event: "KoraEvent") -> None:
        """Standalone JSONL line for one mapped Kora event (M1 slice 1) — the full-fidelity
        observability sink, in contrast to `store`, which keeps only the coarse lifecycle
        (ALT-M1). Written flush-only, NOT fsync'd: this is high-volume (every SDK message)
        and losing the tail on a crash is acceptable, unlike `alert` which is the §8 крит.5
        evidence and stays fsync'd. Deliberately does NOT touch `_current` — a Kora event is
        asynchronous to the dispatcher's turn, so it must not attach to a TurnRecord."""
        self._write(
            {
                "kind": "kora_event",
                "type": event.type,
                "cls": event.cls.value,
                "ts": event.ts,
                "payload": event.payload,
                "has_speak": event.speak_text is not None,
            },
            fsync=False,
        )

    def end_turn(self) -> None:
        if self._current is None:
            return
        row = {"kind": "turn", **asdict(self._current)}
        self._write(row)
        self._current = None

    def _write(self, row: dict[str, Any], fsync: bool = True) -> None:
        if self._closed:
            # B28: the journal is a host singleton; monitor/KoraRunner tasks are not part of the
            # ASGI lifecycle and may fire a late write after shutdown ran close(). A closed journal
            # is a silent no-op, not a ValueError bubbling out of record_tool_call/alert.
            return
        self._file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        self._file.flush()
        if fsync:
            os.fsync(self._file.fileno())

    def close(self) -> None:
        self._closed = True
        self._file.close()
