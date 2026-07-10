# Синапс — voice cascade M0

Voice dispatcher bridge to Kora (a Claude-Code-class executor running on the user's home
machine). Design: `docs/superpowers/specs/2026-07-08-synapse-m0-voice-cascade-design.md`.

**Status: M0 skeleton.** The bridge/dispatcher/cascade/journal core is implemented and
tested offline (no network, no keys). STT (Deepgram Flux) and TTS (Fish Audio) are wired but
remain **candidates**, not frozen decisions — §7 испытание №5 (STT bake-off) and the TTS
smoke-test are what actually decide the slot. Do not treat `python -m synapse.pipeline.app`
as a finished voice product; it is the M0 scaffold those trials run against.

## Quickstart

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

### Console demo (no keys needed)

Text-in/text-out harness: a deterministic MockLLM + FakeKora drive the same
bridge/tools/journal/arbiter code the real voice pipeline uses, on a virtual clock (no real
sleeps, no network):

```bash
.venv/bin/python -m synapse.runners.console --scenario tests/scenarios/demo.jsonl
```

Or pipe your own scenario via stdin — each line is a JSON object:
`{"advance_s": <float>, "user": "<text>"}` or `{"advance_s": <float>, "kora_event": {...}}`.

### Voice run (real keys + microphone)

```bash
.venv/bin/pip install -e ".[voice]"
brew install portaudio   # macOS — LocalAudioTransport needs pyaudio/portaudio
cp .env.example .env     # fill in GOOGLE_API_KEY / OPENROUTER_API_KEY / ANTHROPIC_API_KEY /
                          # DEEPGRAM_API_KEY / FISH_AUDIO_API_KEY / FISH_REFERENCE_ID
.venv/bin/python -m synapse.pipeline.app
```

### №5 — record voice commands (Р-7)

```bash
.venv/bin/pip install -e ".[record]"
.venv/bin/python -m synapse.runners.record_commands --phrases phrases.txt --out recordings/ --bg тихая
```

## Known limitations (honest, not silently assumed away)

- **RPD reset boundary (Р-14) is a UTC hour, no DST correction** — up to ±1h slop against
  the real free-tier reset for half the year (§11.4 M0 assumption).
- **CostCap (§11.5) overshoot is bounded to ≤1 paid call** past the configured daily cap —
  the call that trips the cap is itself allowed to complete; the cap is checked per paid
  *attempt*, not per turn, and is ahead of the owed §11.5 design (kept because it's cheap,
  self-contained, and the driver — cost — is real even before the rest of §11.5 lands).
- **Restart-time state persistence (R6) is narrow**, not full durability: only
  `{task, last_event_ts, staged}` round-trips through `<journal_dir>/state.json`, specifically
  so a restart during a dead Kora reports `stale`/`unreachable` immediately instead of
  resetting the liveness clock. Turn history, in-flight confirm transcripts, and the journal
  itself are not part of this — see §11 owed items for full durability.
- **Fish Audio TTS is WebSocket** (`tts/live`), same approach as `FishAudioTTSService` in
  pipecat — this repo's own reference client (`fishaudio-engine`, see spec Приложение Г) flags
  WS as DEPRECATED in AskME production due to Starter-tier limits (5 concurrent connections,
  429s under load); AskME's production path is HTTP REST. This risk carries over here and is
  unresolved — a candidate, not a frozen decision (§7 smoke-test decides the TTS slot).
- **Kora bridge transport is in-process only in M0** (A1): the WebSocket server described in
  the design doc's §3 diagram was cut as scope-creep — it had zero verify-command coverage
  and §11.1 already marks user↔Синапс↔Кора auth as deferred. `FakeKora` (in-process) is the
  only "Kora" this M0 skeleton talks to; a real transport is a follow-up milestone.
- **The console's MockLLM is a deterministic word-router**, not a real model — a real LLM in
  the console path is out of scope for M0 (running the cascade twice, once for voice and once
  for text, would let the two implementations drift).
- **`GenerationGuard`'s guarded-context-aggregator factory (S1) is implemented and unit
  tested** (`test_context_guard.py`, both race orders), but wiring an instance of it into the
  live pipecat pipeline (replacing `LLMContextAggregatorPair`'s default assistant aggregator)
  is not yet done in `pipeline/app.py` — the offline test suite doesn't exercise a live
  cascade retry end-to-end. Documented follow-up, not a silent gap.
- Prompt v3 (Приложение А) is included verbatim; the OWED additions (rules 7/8, the refined
  possibility "а", possibility "г") are project-status text per the design doc, not yet
  re-validated by a `confab_regression.py` run — `SynapseConfig.include_owed_prompt_rules`
  exists specifically to turn them off if a future regression run rejects them.
