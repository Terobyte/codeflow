# Synapse — bug hunt (M1 slices 0-4), 2026-07-12

Tree frozen at `a8dd919` (173 tests green). 6 parallel hunters × distinct lenses. IDs immutable.
Severity: CRIT = crash/data-loss/security · MAJOR = wrong behavior on real input · MINOR = edge.
Disposition: `FIX-W1` (wave 1 target) · `carry` (later wave) · `known` (documented residual) · `fixed`.

Prior hunt archived: `bugs-archive-2026-07-11.md`.

---

## CRIT

### B1 — CANCEL_REQUESTED → RUNNING resurrection of a *cancelled destructive* task  ·  FIX-W1
`confirm.py:191`, `state.py:216`/`201`. Destructive submit → PENDING_CONFIRMATION + `_staged`. "отмени" → `request_cancel` flips store to CANCEL_REQUESTED but **never clears ConfirmFlow `_staged`**. Later "да" → `confirm("confirm")` → `_staged` still live, not timed out → `set_task_status(RUNNING)`; terminal guard covers only COMPLETED/FAILED → CANCEL_REQUESTED silently → RUNNING → `on_task_committed` **launches the destructive task the user cancelled.** Fix: `request_cancel`/cancel must clear the staged confirm; and/or `confirm` must refuse when status is CANCEL_REQUESTED.

### B11 — Grep/Glob content-scan reads in-workspace secret CONTENTS (gate sees only the path arg)  ·  known (slice-4 residual, no-exfil backstop)
`kora.py:383-385`,`400-404`. `Grep(pattern=".", path=workspace, output_mode=content)` recurses into `workspace/.env` etc. and returns their lines; the gate only checks the `path` arg (the dir → allowed). Documented in slice-4 parking lot: backstopped by NO-EXFIL (egress denied + completion-SPEAK templated from task_text, not Кора output + dispatcher sees only redacted [СОСТОЯНИЕ]/is_error) — the secret is trapped in Кора's ephemeral session. Full fix (output post-filter / deny broad content-grep) = M1.1; do NOT cripple Кора's search in v1.

---

## MAJOR

### B2 — `monitor_forever` dies permanently on any loop-body exception (silent)  ·  FIX-W1
`app.py:135-140`, launched `webrtc_server.py:86`. Unguarded `while True`; `journal.alert`→`os.fsync` OSError propagates → loop ends → `speak_ledger.check()` (the SOLE voice-path driver of Р-15г CRITICAL_WITHOUT_SPEAK, §8 крит.5) stops for the session. Task is a bare `ensure_future` only `.cancel()`d in finally → exception never retrieved, death silent. Fix: try/except(Exception, log/alert)+continue in the loop body, re-raise CancelledError.

### B3 — `apply_event` has no terminal guard → COMPLETED overwritten by a later FAILED/RUNNING  ·  FIX-W1
`state.py:230-238`. Sets `status=new_status` for any lifecycle event with no terminal check (unlike `set_task_status` at :201). A 2nd `ResultMessage`, or `task_failed` after `task_completed` (stream loop has no break), or a late `SystemMessage(init)` → wrong terminal reported to user. Fix: guard against overwriting COMPLETED/FAILED in `apply_event`.

### B4 — `config.from_env` unguarded `int()/float()` crashes the whole app on a malformed env value  ·  FIX-W1
`config.py:110-115`. `KORA_MAX_TURNS=forty` (or budget/deadline typo) → ValueError takes down `from_env()` instead of falling back to the default. Fix: guarded parse → keep dataclass default on ValueError (+ optionally log).

### B5 — Tool dispatch by `getattr` bypasses the `ALL_SCHEMAS` allowlist  ·  FIX-W1
`loop.py:90` (`getattr(self._handlers, call.name)`) + `:53`. A hallucinated/adversarial tool name colliding with a real method (`begin_turn`) invokes that method / crashes on arg-mismatch, mutating turn state. Fix: validate `call.name` against the ALL_SCHEMAS name-set before dispatch; unknown → error result.

### B6 — Prompt anchor-insertion silently no-ops if `PROMPT_V3` wording drifts → OWED safety rules vanish  ·  FIX-W1
`prompt.py:52-81`. `_apply_owed_additions` inserts rules 7/8/9 + possibilities г/д via exact `str.replace` on verbatim anchors. Any edit to `PROMPT_V3` makes a `replace` a silent no-op — safety rules dropped, `include_owed_prompt_rules` still True, no error. Fix: assert each replace changed the string (raise on missing anchor).

### B7 — Kora SDK subprocess not torn down on WebRTC disconnect  ·  REJECTED (by design)
`webrtc_server.py:65-74` disconnect cancels only the pipeline task, not Kora. Senior review: this is INTENDED — §2.7/slice-0 requires "task survives a reconnect (drop tab → reconnect → same task)". Cancelling Kora on disconnect terminalizes the logical task to FAILED and breaks that v1 DoD. Orphaned budget is bounded (`kora_deadline_s` + `max_budget_usd`); a grace-based cancel-if-no-reconnect is M1.1 (needs reconnect-reattach). Test removed.

### B8 — `active_sessions` grows unbounded (bare `/start` never popped)  ·  FIX-W1
`webrtc_server.py:117` create, `:95-96` only pop (in `run_session` finally, reached only via a completed offer). A `/start` with no follow-up offer (tab closed, ICE fail, retry, curl loop) leaks forever; value is attacker-sized JSON. Fix: cap/TTL/evict, or pop on offer-consumed; drop the structure if only readiness is needed.

### B9 — `speak()` fire-and-forget `ensure_future` swallows exceptions + drops the SPEAK  ·  FIX-W1
`app.py:120`,`128`. Ledger marked spoken FIRST (line 120), then `ensure_future(push_speak_frame)` with no ref/done-callback. If `queue_frame` raises (teardown/cancel), exception never retrieved AND — because the ledger says spoken — CRITICAL_WITHOUT_SPEAK can never fire for the lost critical. Fix: attach a done-callback that logs/alerts on failure (and reconsider marking spoken before emit).

### B10 — Dispatcher tool loop discards second-pass tool_calls  ·  carry
`loop.py:69`. Strictly 2-pass: `text, _ = await self._complete()` drops any pass-2 tool_calls (a real LLM chaining get_task_status→request_cancel loses the follow-up silently). Fix: loop until no tool_calls or a bounded max.

### B12 — Р-11 between-turns liveness is decorative: `monitor_forever` discards `liveness()` and no stale/unreachable alert exists  ·  carry
`app.py:140`, `state.py:240-253`. `liveness()` is a pure query; its result is thrown away and there is NO AlertKind/SPEAK for STALE/UNREACHABLE anywhere. A Kora that dies between turns emits nothing until the LLM next renders [СОСТОЯНИЕ]. Fix: emit an alert (and/or proactive SPEAK) when liveness degrades in the monitor.

### B13 — `journal.begin_turn` never called in the voice path → tool calls unrecorded + `check_grounding` never runs in production  ·  carry
`app.py:265-268`, `journal.py:90-91`, `loop.py:52`. Only DispatcherTurnLoop/console calls `begin_turn`; the pipecat STT `on_end_of_turn` only calls `note_user_turn`. So `_current=None` in voice: `record_tool_call` no-ops, STATUS_WITHOUT_GROUNDING (§8 крит.5) can never fire live. Also makes the R1 dedup latch inert in voice (B14). Fix: wire begin_turn/end_turn/check_grounding into the voice tool path.

### B15 — Arbiter `_drain()` on every TextFrame defeats SPEAK preemption + `flush_dispatcher`  ·  carry (needs verification vs slice-2 push design)
`arbiter.py:104-113`. `_drain` empties the queue after each frame → a later Kora `TTSSpeakFrame` sees an empty queue, so drop-tail/`flush_dispatcher` (`:70-79`) never engages; SPEAK lands behind already-flushed dispatcher sentences. Verify against the slice-2 direct-inject design (which bypasses the queue) before fixing — may be moot on the live push path.

### B16 — WebRTC signaling has zero authentication → unauthenticated preempt of the live user + cost abuse  ·  carry (slice-5 / Cloudflare Access)
`webrtc_server.py:108-129`,`76-85`,`138-140`. Unauth `/start`→offer binds `current["task"]` and cancels the old ("preempted") → kicks the real speaker, spins paid Flux/Fish/LLM. This is §2.8's tunnel/Access requirement = slice 5 (needs Тero's Cloudflare). Local-preempt hardening notable.

---

## MINOR

- **B14** `tools.py:144-150` dedup latch keys on tool NAME only (not args) → two same-name calls with different args in one turn: 2nd swallowed, returns 1st's result. FIX-W1 (cheap, add args to key).
- **B17** `state.py:218`+`kora.py:318-319` cancelled launched task never terminalizes → permanent CANCEL_REQUESTED limbo (terminalize only acts on RUNNING). carry.
- **B18** `state.py:344-347` corrupt/old-schema `state.json` swallowed (RUNNING task forgotten → R6 defeated; old-schema KeyError uncaught → build_host crash). carry.
- **B19** `state.py:244` liveness not gated on RUNNING while render/snapshot are → cancel-window inconsistency (liveness OK, snapshot awaiting=false). carry.
- **B20** `kora.py:206-216` `apply_event_to_store` drops SPEAK/critical for non-lifecycle events, diverging from `FakeKora.emit` "safety clone" the docstring claims is in sync. carry (latent).
- **B21** `kora.py:401`/`404`/`436` deny reason returns full absolute host paths to the injectable agent (username/layout disclosure oracle). carry.
- **B22** `kora.py:70-97` secret containment is a denylist → `prod.env`/`secrets.yaml`/`token.txt` read freely. carry (denylist incompleteness; B11 backstop applies).
- **B23** `strategy.py:87-90` `_advance` aborts failover (no next tier, no `_fail_all`, no alert) if `_set_active_if_available` returns None. carry (PLAUSIBLE).
- **B24** `webrtc_server.py:76-96` `current["task"]`+`bind_output` published OUTSIDE the cleanup try/finally → a raise in the setup window leaks the bind slot. carry.
- **B25** `webrtc_server.py:112-115` `/start` swallows all JSON parse errors → malformed body = empty handshake, not a diagnosable 400. carry.
- **B26** `config.py:106-107` `KORA_ENABLED=""` → False (asymmetric bool parsing vs other fields treating empty as unset). carry.
- **B27** `mock_llm.py:42-51` affirm/deny word-sets shadow submit/cancel routing (console/test path only). carry.
- **B28** `journal.py:65`/`app.py:151` TurnJournal fd never closed on live path (single fd, non-accumulating). carry.
- **B29** `webrtc_server.py:86`/`90` monitor task cancelled but never awaited on teardown (bounded 1:1, cosmetic). carry.

---

## Wave 1 — DONE (2026-07-12). FIXED red→green: **B1, B2, B3, B4, B5, B6, B8, B9, B14** (9 bugs, +9 regression tests, suite 173→182). B7 REJECTED by-design. B11 known-residual (no-exfil backstop).
Tests: `tests/test_bughunt_w1_state.py` (B1/B3/B14), `test_bughunt_w1_app_config.py` (B2/B4/B6/B9), `test_bughunt_w1_dispatch_webrtc.py` (B5/B8).
Carried to later waves: B10, B12, B13, B15, B16, B17-B29.
