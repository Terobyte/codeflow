"""Wave-3 bug-hunt regression tests (RED before the fix, GREEN after).

One test per bug ID. Each asserts POST-FIX behavior so it fails at ITS OWN assertion today:

- B13  voice STT path never arms the turn latch (`handlers.begin_turn` uncalled) → R1 dedup dead.
- B21  `kora.py` gate deny reason leaks the full resolved absolute path to the injectable agent.
- B39  `journal.alert()` write is unguarded → a raising fsync propagates into the caller.
- B36  `ArbiterPolicy` prepends multiple pending SPEAKs newest-first (LIFO) instead of FIFO.

Production code is NOT touched by this file.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from synapse.bridge.kora import KoraRunner
from synapse.bridge.state import SpeakLedger, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.journal import AlertKind, TurnJournal
from synapse.pipeline.arbiter import ArbiterPolicy


# =========================================================================================
# B13 — the pipecat STT on_end_of_turn handler must ARM the turn latch (handlers.begin_turn),
# not only note_user_turn. Without begin_turn in voice, `_current_turn_id` stays None → the R1
# dedup latch is inert and a cascade retry can double-execute a mutating tool.
# =========================================================================================


def test_b13_voice_end_of_turn_arms_turn_latch(tmp_path):
    # Needs the `voice` extra (deepgram flux STT). Skip (not error) if unavailable.
    pytest.importorskip("pipecat.services.deepgram.flux.stt")
    from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService

    from synapse.pipeline.app import build_host, build_session_pipeline

    cfg = SynapseConfig(
        google_api_key="fake-google-key",
        openrouter_api_key="fake-openrouter-key",
        anthropic_api_key="fake-anthropic-key",
        deepgram_api_key="fake-deepgram-key",
        fish_audio_api_key="fake-fish-key",
        fish_reference_id="fake-fish-ref",
        journal_dir=str(tmp_path),
    )
    host = build_host(cfg)
    session = build_session_pipeline(host)

    # The STT service is the head of the per-connection chain; grab it by type.
    stt = next(p for p in session.pipeline.processors if isinstance(p, DeepgramFluxSTTService))
    # pipecat stores raw registered handlers at _event_handlers[name].handlers (see BaseObject).
    handler = stt._event_handlers["on_end_of_turn"].handlers[0]

    assert host.handlers._current_turn_id is None  # latch cold before any user turn

    # Fire the real handler exactly as pipecat would on end-of-turn (service, transcript).
    import asyncio

    asyncio.run(handler(stt, "создай файл заметки"))

    # POST-FIX: the voice turn must arm the dedup latch (handlers.begin_turn ran). Currently the
    # handler only calls note_user_turn, so _current_turn_id stays None → RED here.
    assert host.handlers._current_turn_id is not None


# =========================================================================================
# B21 — the gate deny reason handed back to Kora (the injectable agent) must NOT embed the full
# resolved absolute host path (username/layout disclosure oracle). Only a category token.
# =========================================================================================


def _make_runner(tmp_path):
    clock = FakeClock(0.0)
    ws = tmp_path / "ws"
    cfg = SynapseConfig(kora_workspace_dir=str(ws), kora_deadline_s=900.0)
    store = TaskStore(clock)
    ledger = SpeakLedger()
    journal = TurnJournal(str(tmp_path / "journal"), clock, session_id="s")
    runner = KoraRunner(cfg, store, ledger, clock, journal, on_speak=None)
    return runner, ws


def test_b21_gate_deny_reason_does_not_leak_absolute_path(tmp_path):
    runner, ws = _make_runner(tmp_path)

    # (a) out-of-workspace SECRET escape — the classic disclosure oracle. Gate v3 (B24): plain
    # mutating writes outside the workspace are now ALLOWED, so the deny this invariant guards is
    # the secret-path one, which fires everywhere incl. outside ws. The reason must still be a bare
    # category token, never the resolved absolute path.
    secret_outside = os.path.expanduser("~/.ssh/id_rsa")
    allowed, detail, category = runner._gate_decision("Write", {"file_path": secret_outside})
    assert allowed is False and category == "secret_path"
    resolved = str(Path(secret_outside).resolve())
    reason = detail or ""
    # POST-FIX: the reason is a category token only, never the resolved absolute path.
    assert resolved not in reason, f"deny reason leaks the resolved path: {reason!r}"
    assert secret_outside not in reason, f"deny reason leaks the target path: {reason!r}"

    # (b) secret-path deny reason must not leak the resolved absolute path either.
    secret = str(ws / ".env")
    allowed2, detail2, category2 = runner._gate_decision("Read", {"file_path": secret})
    assert allowed2 is False and category2 == "secret_path"
    reason2 = detail2 or ""
    resolved2 = str((ws / ".env").resolve())
    assert resolved2 not in reason2, f"secret deny reason leaks the resolved path: {reason2!r}"
    assert str(ws) not in reason2, f"secret deny reason leaks the workspace path: {reason2!r}"


# =========================================================================================
# B39 — journal.alert() is the §8 крит.5 evidence sink and is called BARE from cascade
# on_tail_tier/on_all_failed + confirm self-attempt. A disk error (fsync raising) inside alert()
# must be best-effort and NOT propagate into the pipecat machinery.
# =========================================================================================


def test_b39_alert_does_not_propagate_a_raising_fsync(tmp_path, monkeypatch):
    clock = FakeClock(0.0)
    journal = TurnJournal(str(tmp_path), clock, session_id="s")

    def boom(*args, **kwargs):
        raise OSError("simulated disk failure on fsync")

    # journal._write does os.fsync(...) for a fsync'd row (alert is fsync'd) — make it raise.
    monkeypatch.setattr(os, "fsync", boom)

    propagated: BaseException | None = None
    try:
        journal.alert(AlertKind.TAIL_TIER_ENTRY, {"x": 1})
    except BaseException as exc:  # noqa: BLE001 — we are testing for NON-propagation.
        propagated = exc

    # POST-FIX: alert() swallows the disk error (best-effort). Currently it propagates → RED.
    assert propagated is None, f"alert() must be best-effort but propagated: {propagated!r}"


# =========================================================================================
# B36 — two pending SPEAKs with no drain between them must drain in FIFO order: the OLDER
# critical readback ("A") before the NEWER ("B"). Current push_speak prepends newest-first (LIFO).
# =========================================================================================


def test_b36_multiple_speaks_drain_fifo_not_lifo():
    a = ArbiterPolicy()
    a.push_speak("A")  # older critical
    a.push_speak("B")  # newer critical, no drain in between
    items = [i.text for i in a.drain_all()]
    # POST-FIX: FIFO for equal-priority SPEAKs. Currently B is prepended ahead of A → ["B","A"] RED.
    assert items == ["A", "B"], f"SPEAKs drained LIFO, expected FIFO: {items!r}"
