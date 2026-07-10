"""FakeKora — in-process scripted double of Kora for tests/demo (A1: no WebSocket transport
in M0 — server.py was cut as scope-creep with zero verify-command coverage; this is the only
"Kora" M0 speaks to). Applies events directly to TaskStore/SpeakLedger and calls the SPEAK
callback synchronously, using virtual time from Clock so no real sleeps are needed (R10).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from synapse.bridge.state import EventClass, KoraEvent, SpeakLedger, TaskStore, parse_event
from synapse.clock import Clock


@dataclass
class ScriptedEvent:
    delay_s: float
    raw: dict[str, Any]


class FakeKora:
    def __init__(
        self,
        store: TaskStore,
        speak_ledger: SpeakLedger,
        clock: Clock,
        on_speak: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._speak_ledger = speak_ledger
        self._clock = clock
        self._on_speak = on_speak

    def emit(self, raw_event: dict[str, Any], now: float | None = None) -> KoraEvent:
        """Apply one Kora event immediately (heartbeat/task_started/progress/
        task_completed/task_failed). `now` overrides the clock; defaults to clock.now()."""
        ts = now if now is not None else self._clock.now()
        raw = dict(raw_event)
        raw.setdefault("ts", ts)
        event = parse_event(raw)

        if event.type == "heartbeat":
            self._store.heartbeat(event.ts)
        else:
            self._store.apply_event(event)
            if event.cls == EventClass.CRITICAL:
                self._speak_ledger.register_critical(event)

        if event.speak_text:
            if self._on_speak is not None:
                self._on_speak(event.speak_text)
            self._speak_ledger.register_speak(event.id, event.ts)

        return event

    def run_script(self, script: list[ScriptedEvent]) -> None:
        """Fires scripted events back-to-back, advancing the clock by each delay_s first
        (if the clock supports `advance()` — i.e. a FakeClock)."""
        advance = getattr(self._clock, "advance", None)
        for item in script:
            if callable(advance):
                advance(item.delay_s)
            self.emit(item.raw, now=self._clock.now())
