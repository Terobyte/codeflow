"""TurnJournal — the observability spine (§3 "Наблюдаемость"). One JSONL file per session.

R2 (durability): `alert()` is the single piece of evidence the §8 крит.5 gate checks
("алерт «статус без grounding» — ноль срабатываний"). It writes its OWN line immediately,
flushed and fsync'd, independent of `end_turn()` — a crash between an alert and the end of
its turn must not erase the alert.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from synapse.clock import Clock


class AlertKind(str, Enum):
    STATUS_WITHOUT_GROUNDING = "STATUS_WITHOUT_GROUNDING"
    CRITICAL_WITHOUT_SPEAK = "CRITICAL_WITHOUT_SPEAK"
    TAIL_TIER_ENTRY = "TAIL_TIER_ENTRY"
    ALL_TIERS_FAILED = "ALL_TIERS_FAILED"
    COST_CAP = "COST_CAP"
    CONFIRM_SELF_ATTEMPT = "CONFIRM_SELF_ATTEMPT"
    AUTH_FAILURE = "AUTH_FAILURE"


@dataclass
class TurnRecord:
    turn_id: str
    ts: float
    transcript: str
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
        self._write(row)

    def check_grounding(self, record: TurnRecord, has_active_task: bool) -> None:
        if not has_active_task:
            return
        if not _GROUNDING_PATTERN.search(record.llm_output or ""):
            return
        called_status = any(tc.get("name") == "get_task_status" for tc in record.tool_calls)
        if not called_status:
            self.alert(AlertKind.STATUS_WITHOUT_GROUNDING, {"llm_output": record.llm_output})

    def end_turn(self) -> None:
        if self._current is None:
            return
        row = {"kind": "turn", **asdict(self._current)}
        self._write(row)
        self._current = None

    def _write(self, row: dict[str, Any]) -> None:
        self._file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self) -> None:
        self._file.close()
