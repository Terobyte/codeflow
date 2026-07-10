"""Console e2e runner: `python -m synapse.runners.console --scenario <file.jsonl>` (or
stdin). Text-in/text-out, no network, no real audio, no real sleeps — everything moves on a
FakeClock driven by each scenario line's `advance_s` field (R10).

Scenario line shapes (one JSON object per line):
  {"advance_s": <float>, "user": "<text>"}          — a user turn
  {"advance_s": <float>, "kora_event": {...}}        — an event from (fake) Kora
  {"advance_s": <float>}                             — just move the clock (e.g. simulate
                                                        Kora going silent before a stale check)
`advance_s` defaults to 0.0 if omitted.

Exit code 0 iff the run completes with zero journal alerts (an M0 demo run is expected to be
clean end-to-end: every status question is grounded, every critical event gets its SPEAK,
etc.) — any alert is a scenario/implementation bug, not "expected noise".
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.fake_kora import FakeKora
from synapse.bridge.state import SpeakLedger, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.dispatcher.mock_llm import MockLLM
from synapse.dispatcher.tools import KoraBridge, ToolHandlers
from synapse.journal import AlertKind, TurnJournal
from synapse.pipeline.arbiter import ArbiterPolicy


def _read_scenario(path: str | None) -> list[dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    steps = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        steps.append(json.loads(line))
    return steps


async def run_scenario(
    steps: list[dict[str, Any]],
    cfg: SynapseConfig | None = None,
    journal_dir: str | None = None,
    session_id: str = "console-demo",
) -> int:
    cfg = cfg or SynapseConfig()
    clock = FakeClock(start=0.0)
    journal = TurnJournal(journal_dir or cfg.journal_dir, clock, session_id=session_id)
    store = TaskStore(clock, journal_dir=None)  # one-shot demo run: no cross-run persistence
    speak_ledger = SpeakLedger()
    classifier = KeywordClassifier(cfg.destructive_keywords)
    confirm_flow = ConfirmFlow(
        store, clock, classifier, journal,
        cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s,
    )
    arbiter = ArbiterPolicy()

    def on_speak(text: str) -> None:
        # SPEAK is delivered immediately (Р-5 priority) rather than batched with the
        # dispatcher's own sentences at end-of-turn -- matches real-time delivery and keeps
        # the printed order chronological in this synchronous text demo.
        arbiter.push_speak(text)
        for item in arbiter.drain_all():
            print(f"TTS: {item.text}")
            if journal.current is not None:
                journal.current.tts_texts.append(item.text)

    bridge = KoraBridge(store=store, confirm_flow=confirm_flow, clock=clock, on_speak=on_speak, cfg=cfg)
    handlers = ToolHandlers(bridge, journal)
    fake_kora = FakeKora(store, speak_ledger, clock, on_speak=on_speak)
    llm = MockLLM()
    loop = DispatcherTurnLoop(llm, handlers, confirm_flow, store, journal, clock, cfg)

    unexpected_alerts: list[dict[str, Any]] = []
    original_alert = journal.alert

    def _tracking_alert(kind: Any, detail: Any = None) -> None:
        unexpected_alerts.append({"kind": AlertKind(kind).value, "detail": detail})
        original_alert(kind, detail)

    journal.alert = _tracking_alert  # type: ignore[method-assign]

    for step in steps:
        advance = float(step.get("advance_s", 0.0))
        if advance:
            clock.advance(advance)
        now = clock.now()

        if "kora_event" in step:
            event = fake_kora.emit(step["kora_event"], now=now)
            print(f"[Kora] {event.type} @ {now:.1f}")
        elif "user" in step:
            text = step["user"]
            print(f"User: {text}")
            record, reply = await loop.ingest_user_turn(text)
            arbiter.push_dispatcher_text(reply)
            for item in arbiter.drain_all():
                record.tts_texts.append(item.text)
                print(f"TTS: {item.text}")
            journal.end_turn()
            if reply:
                print(f"Dispatcher: {reply}")

        for kind, detail in speak_ledger.check(now, cfg.critical_speak_window_s):
            journal.alert(AlertKind(kind), detail)
        store.liveness(now, cfg.stale_after_s, cfg.unreachable_after_s)

    journal.close()

    if unexpected_alerts:
        print(f"FAILED: {len(unexpected_alerts)} unexpected alert(s): {unexpected_alerts}", file=sys.stderr)
        return 1
    print("OK: scenario complete, 0 unexpected alerts")
    return 0


def main(argv: list[str] | None = None) -> int:
    import asyncio

    parser = argparse.ArgumentParser(description="Synapse console e2e runner")
    parser.add_argument("--scenario", type=str, default=None, help="Path to a .jsonl scenario file; omit to read stdin.")
    args = parser.parse_args(argv)
    steps = _read_scenario(args.scenario)
    return asyncio.run(run_scenario(steps))


if __name__ == "__main__":
    raise SystemExit(main())
