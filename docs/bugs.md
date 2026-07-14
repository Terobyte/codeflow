# bugs.md

Severity: **CRIT** = money/data loss · security · crash · **MAJOR** = wrong behaviour on real input · **MINOR** = edge degradation.
Status: `reported` → `proven` | `rejected(reason)` | `not-test-verifiable(reason + manual cmd)`; `proven` → `fixed(commit)` | `parked(why)`.

## Hunt 2026-07-14 — whole app (5 hunters, read-only, tree @ 1f1c046)

Scope: all of `synapse/`. Hybrid assignment — every file one deep owner + one lens each across the whole tree. Namespace `B*` (this is a fresh ledger; prior hunts' entries were archived/fixed inline). 14 bugs: 4 CRIT, 9 MAJOR, 1 MINOR. **All 14 proven then FIXED** — 2 sonnet test-writers wrote one red test per ID (disjoint files `tests/test_hunt0714_a.py` / `tests/test_hunt0714_b.py`), each verified red at its own assertion matching expected-vs-actual on tree @ 1f1c046; Opus fixed the risky/cross-layer/money/security/state defects directly + one sonnet on the isolated CLI (B13); each fix landed red→green with assertions untouched; **full suite 482 green, 0 fail**; concurrency fixes (B02/B08) stable green 3×.

Fix notes (nuanced ones — where the naive fix collided with an existing invariant):
- **B01**: register the ledger OPTIMISTICALLY + synchronously (keeps `test_speak_registers_ledger_always` and in-flight SPEAKs from false-alarming), then REVERT in the injection done-callback iff delivery raised — a dropped critical re-arms Р-15г. New `SpeakLedger.revert_speak`.
- **B04**: count the paid attempt on a SUCCESSFUL generation-end via a thin `_CostCountingLLMSwitcher`, gated on `active_tier_index()==0` so failover tiers (already pre-counted in `_advance`) aren't double-counted.
- **B08**: fixed at the JOURNAL level (`begin_turn` won't hijack an already-open turn's `_current`) — NOT by holding `turn_lock` across the LLM call, which would reintroduce B-PIPE-5 (a slow client blocking all others). Full per-turn voice serialization stays the parked pipecat residual.
- **B11**: gate-minted IDs moved to a disjoint `gate-` namespace (collision structurally impossible; no code parses the ID format).
- **B12**: prevented (atomic `TaskStore.stage_task`, one persist) AND reconciled (`ConfirmFlow.__init__` drops a `PENDING_CONFIRMATION`+`staged=null` scar from an older state.json).

Proof (test node id per bug):
- B01 `tests/test_hunt0714_a.py::test_B01_failed_speak_injection_must_leave_critical_unspoken`
- B02 `tests/test_hunt0714_a.py::test_B02_concurrent_turns_same_thread_corrupt_shared_history`
- B03 `tests/test_hunt0714_b.py::test_B03_no_path_fast_path_must_deny_secret_workspace_root`
- B04 `tests/test_hunt0714_a.py::test_B04_successful_paid_turn_must_count_against_cost_cap`
- B05 `tests/test_hunt0714_b.py::test_B05_ssh_dir_bypasses_secret_denylist`
- B06 `tests/test_hunt0714_a.py::test_B06_illegal_stage_transition_must_not_escape_as_valueerror`
- B07 `tests/test_hunt0714_a.py::test_B07_write_code_refuses_stale_plan_after_revise_and_new_propose`
- B08 `tests/test_hunt0714_a.py::test_B08_concurrent_begin_turn_must_not_steal_the_voice_turn_record`
- B09 `tests/test_hunt0714_b.py::test_B09_critical_speak_registered_before_ledger_entry_exists`
- B10 `tests/test_hunt0714_a.py::test_B10_malformed_json_post_returns_400_not_500`
- B11 `tests/test_hunt0714_b.py::test_B11_confirm_and_gate_task_id_generators_collide`
- B12 `tests/test_hunt0714_b.py::test_B12_crash_between_start_task_and_set_staged_wedges_flow`
- B13 `tests/test_hunt0714_b.py::test_B13_manifest_wiped_on_new_bg_without_resume`
- B14 `tests/test_hunt0714_b.py::test_B14_duplicate_terminal_event_appended_twice`

---

### B01 — `speak()` marks the SpeakLedger "spoken" before TTS delivery is confirmed; a dropped critical is never re-alerted — CRIT — fixed
- class: data-integrity / safety-invariant · location: `synapse/pipeline/app.py:201,209-223` · found-by: H4
- symptom: a `CRITICAL` Kora event with `speak_text` calls `speak_ledger.register_speak_text(...)` **synchronously** (line 201) and only then schedules `push_speak_frame` via `ensure_future` (209). If the injection later raises (output task torn down mid-emit — the exact "B9" scenario the code comments describe), `_on_speak_frame_done` (218-223) does `logger.warning(...)` and nothing else — never un-marks the ledger, never raises `CRITICAL_WITHOUT_SPEAK`. A critical that was never voiced is permanently recorded as delivered, defeating the Р-15г invariant the ledger exists to protect.
- trigger: `CRITICAL` event with `speak_text` while a live output task exists; `push_speak_frame`'s `queue_frame` raises.
- expected vs actual: a failed SPEAK must leave `spoken=False` so the Р-15г watchdog still fires · actual: `spoken` set `True` up front, delivery failure only logs.
- evidence: 201 (register before delivery), 209-214 (fire-and-forget), 218-223 (callback only logs — its own comment says "surface the failure instead of swallowing it", but it doesn't).

### B02 — `DispatcherTurnLoop` shares one unlocked history list per thread; concurrent turns corrupt history and cross-deliver replies — CRIT — fixed
- class: concurrency / shared mutable state · location: `synapse/dispatcher/loop.py:88-139` (`_history_for` 67-86, mutate 98-102, `await _complete` 104, rollback 131); entry `synapse/pipeline/webrtc_server.py:513-519` · found-by: H2
- symptom: `_history_for(thread_id)` hands the *same* list object to every caller. `ingest_user_turn` appends the user msg, snapshots `len`, then `await self._complete(...)` (a real network suspension). Two interleaved turns on one thread both mutate the shared list: user msgs stack with no separating assistant turn, each `_complete` sees the other's in-flight msgs, and each caller can receive the *other* turn's reply. On error, `del history[snapshot-1:]` cuts from a now-stale index, discarding the other turn's data too.
- trigger: two concurrent `POST /api/threads/{same_id}/message` (double-click, two tabs). Nothing serializes `ingest_user_turn` per thread; `turn_lock` is released before it's called (see B08).
- expected vs actual: turn A's caller gets A's reply, history alternates user/assistant · actual (reproduced by hunter): final history `[user A, user B, assistant "reply-2", assistant "reply-2"]`, both callers return `"reply-2"`.
- evidence: 98-99 (shared list append), 102 (snapshot), 104 (suspension), 127-131 (unlocked append/rollback).

### B03 — gate's no-path `Glob`/`Grep`/`LS` fast path skips the secret-path check, contradicting its own contract → secret exfiltration — CRIT — fixed
- class: security / input validation · location: `synapse/bridge/kora.py:534-554` (fast path 550-554) · found-by: H3
- symptom: `_gate_decision`'s docstring claims a resolved secret path is denied for ALL file tools even inside the workspace, "checked BEFORE the in-workspace allow". False for `Glob`/`Grep`/`LS` with no/blank path: the code `return True, None, "allow"` (553) without ever computing `resolved` or calling `_is_secret_path`. If the workspace root is (or resolves under) a secret dir, Kora can `Grep`/`Glob`/`LS` its contents — including reading file *contents* via Grep — with zero containment; results flow into `assistant_text` served by the unauthenticated kora-log/kora-status routes.
- trigger: workspace root = a secret-resolvable dir (reachable via B05); Kora calls `Grep({"pattern":"PRIVATE KEY"})` or `Glob({})`/`LS({})` with no `path` → cwd (the workspace).
- expected vs actual: any resolved path under a `_SECRET_DIR_SEGMENTS` segment denied for every file tool, incl. cwd-default · actual: no-path fast path skips resolution + secret check entirely.
- evidence: 550-553 (blank path + read/search tool → immediate allow, no `_is_secret_path`); docstring 537-539.
- chains-with: B05 (which arms the malicious workspace root). Together CRIT.

### B04 — `CostCap.record_paid_attempt` is only reached on the error path, so `max_paid_calls_per_day` never trips in normal operation — CRIT — fixed
- class: safety-invariant logic gap · location: `synapse/cascade/strategy.py:81,89-92` + `synapse/cascade/services.py:92-105` · found-by: H5
- symptom: `record_paid_attempt()` (the only incrementer of `_count`/`_tripped`) is called from exactly one site — `strategy.py:90` inside `_advance` — and `_advance` is reachable only from `handle_error`, which pipecat invokes only when the active service pushes a non-fatal `ErrorFrame` (failure only). The default active service is `services[0]` (tier1, paid). Every turn that succeeds on tier1 makes a real billed call but never counts it; the daily cap is structurally inert whenever tier1 is healthy — the common case — with no log or alert that it isn't working.
- trigger: boot with `max_paid_calls_per_day=500`; run any number of turns that succeed on tier1. `cost_cap.count == 0`, `tripped == False` after 1000+ turns.
- expected vs actual: per `services.py`'s own docstring, every paid-tier attempt (success or failure) counts against R9 · actual: only attempts made while already handling an error count.
- evidence: `grep -rn record_paid_attempt synapse/` → one definition + one call site (strategy.py:90); `pipeline/app.py:246` only calls `maybe_reset`.

### B05 — `validate_project_path` omits `.ssh`/`.aws`/`.kube`/`.docker` from its denylist → a project (hence Kora's workspace root) can be pinned to a secrets dir — MAJOR — fixed
- class: security / input validation · location: `synapse/projects.py:15,36-38` · found-by: H3
- symptom: `_FORBIDDEN_HOME_SUBDIRS = (".config",".gnupg","Library/Keychains")` covers 3 of the 8 dirs `bridge/kora.py`'s `_SECRET_DIR_SEGMENTS` treats as secret. `.ssh`/`.aws`/`.kube`/`.docker` pass validation, contradicting `webrtc_server.py:41`'s comment ("первая линия защиты — validate_project_path при add"). Accepted path is persisted, becomes a thread's `project_id`, and `_resolve_root_for` hands it to Kora as cwd — arming B03.
- trigger: `POST /api/projects {"name":"","path":"<home>/.ssh"}` (with matching Origin/Host for CSRF). `validate_project_path(~/.ssh)` returns the path instead of raising.
- expected vs actual: adding a project rooted at any `_SECRET_DIR_SEGMENTS` member rejected like `.config`/`.gnupg` · actual: silently accepted.
- evidence: 15 (short denylist) vs `kora.py:75` (`_SECRET_DIR_SEGMENTS`).
- chains-with: B03 (root cause that arms the exfiltration).

### B06 — `send_to_kora`/`write_code` gate branches skip the stage guard `revise` has → illegal-transition `ValueError` escapes uncaught (500 / voice-path crash) — MAJOR — fixed
- class: state machine / unguarded transition · location: `synapse/pipeline/app.py:277-352` (revise guarded 295-299, write_code unguarded 318-332, `_launch_run` 343) + `synapse/threads.py:130-141` · found-by: H1
- symptom: `_launch_run` calls `set_stage(th.id, stage)` with no try/except, unlike the `revise` branch which catches `ValueError` → `{"error":"illegal_stage"}`. When the current stage forbids the target, `set_stage` raises; it propagates out of `gate_action` uncaught (no global handler in `webrtc_server.py`; also unguarded in the voice tool path `dispatcher/tools.py:303-317`).
- trigger: e.g. `revise` (stage→`collect`) then immediately `send_to_kora` (no stage check) → `set_stage("spec_plan")` while stage is `collect` (only `propose` legal) → raises. Or double "write code" on a `done` thread whose plan file still exists.
- expected vs actual: every branch returns `{"error":"illegal_stage"}` like `revise` · actual: `send_to_kora`/`write_code` have zero stage validation → unhandled ValueError → 500.
- evidence: 295-299 (guarded) vs 318-332 + 343 (unguarded); `threads.py:136-138` (raise).

### B07 — `revise` doesn't reset `last_outcome`/plan file → `write_code` launches a stale plan against a new request — MAJOR — fixed
- class: state machine / stale flag desync · location: `synapse/pipeline/app.py:295-302` (revise) + `318-332` (write_code) + `synapse/threads.py:116-122` (`set_outcome`) · found-by: H1
- symptom: `write_code`'s only staleness signals are `last_outcome=="completed"` + plan-file existence, neither tied to the current `request_text`. `revise` regresses stage to `collect` but leaves `last_outcome` and the old `docs/plans/{id}.md` intact, so after a revise + a *different* propose, both guards pass and Kora is told to implement the old plan under the new request.
- trigger: send_to_kora request A → completes (plan A on disk, `last_outcome=completed`) → revise → propose request B → write_code: `plan_path.exists()`=True (A's plan), `last_outcome!="completed"`=False → launches `code` run "Реализуй по плану …{id}.md. Исходный запрос: B".
- expected vs actual: refuse (`stale_plan`) — no spec_plan completed for B · actual: runs A's plan against B, violating its own documented anti-stale invariant.
- evidence: 295-302 (revise touches only stage), 318-332 (checks), `threads.py:116-122` (`set_outcome` never reset on regression).

### B08 — `turn_lock` releases before `ingest_user_turn`, so a concurrent turn steals `TurnJournal._current` from an in-flight turn — MAJOR — fixed
- class: concurrency / lock scope too narrow · location: `synapse/pipeline/webrtc_server.py:513-519` vs `synapse/pipeline/app.py:699-716` + `synapse/journal.py:74,86-100` · found-by: H2
- symptom: `api_thread_message` does `async with host.turn_lock: current_http_thread["id"]=...` then calls `ingest_user_turn` **outside** the lock; its first act is `begin_turn`, overwriting the single shared `TurnJournal._current` (no per-task isolation). The voice path admits its tool-call tail runs after `turn_lock` release; if an HTTP `begin_turn` fires in that window, the voice turn's later `record_tool_call`/`check_grounding`/`end_turn` operate on the HTTP `TurnRecord`, and the voice record is orphaned.
- trigger: live voice turn's tool tail overlapping any `POST …/message`, or two concurrent HTTP messages to different threads.
- expected vs actual: each turn's tool calls/grounding recorded on its own record · actual (reproduced): `begin_turn(voice)`→`begin_turn(http)`→`record_tool_call` lands on http record, voice record `tool_calls == []`.
- evidence: `webrtc_server.py:513` (lock ends before 516), `loop.py:90` (`begin_turn` unlocked), `journal.py:86-93` (unconditional overwrite).

### B09 — `apply_event_to_store` registers a CRITICAL's speak BEFORE creating the ledger entry → false `CRITICAL_WITHOUT_SPEAK` — MAJOR — fixed
- class: state-corruption / ordering · location: `synapse/bridge/kora.py:299-317` · found-by: H2 + H4 (corroborated)
- symptom: for a lifecycle event that is `CRITICAL` with `speak_text`, `register_speak(event.id)` runs at 306-308 — but `register_critical(event)` (which *creates* the pending entry `register_speak` needs) runs after, at 316-317. `register_speak` finds nothing → no-op; then `register_critical` creates a fresh `spoken=False` entry. The spoken critical is recorded unspoken → `SpeakLedger.check()` later raises a false alert. `FakeKora.emit` (the "keep in sync" clone, `fake_kora.py:44-52`) has the correct order; the two bodies diverged.
- trigger: `apply_event_to_store` with a `KoraEvent(type in {task_started,task_completed,task_failed}, cls=CRITICAL, speak_text=…)`. Dormant in today's live producer (`_message_to_events` hardcodes `NARRATABLE`), but `apply_event_to_store` is exported and contract-bound to mirror FakeKora; fires the instant a CRITICAL lifecycle event reaches it (e.g. `parse_event`'s fail-safe default class = CRITICAL).
- expected vs actual: order matches FakeKora (`register_critical` first) · actual (reproduced): `ledger._pending[id].spoken == False`, `check()` returns `[('CRITICAL_WITHOUT_SPEAK', …)]`.
- evidence: 305-308 (register_speak first) vs 316-317 (register_critical after); `fake_kora.py:44-52` (correct order).

### B10 — five mutating `/api/*` POST routes call `await request.json()` unguarded → 500 on malformed JSON — MAJOR — fixed
- class: input validation / crash · location: `synapse/pipeline/webrtc_server.py:464,486,509,536,562` · found-by: H3
- symptom: `api_projects_add`/`api_threads_create`/`api_thread_message`/`api_thread_gate`/`api_active_thread` each do `data = await request.json()` with no try/except. `Request.json()` raises `JSONDecodeError` on a bad body; no exception handler is registered → unhandled 500, unlike `/start` (257-264) which explicitly wraps `json.loads` for a diagnosable 400.
- trigger: any CSRF-satisfying client (matching content-type/Origin/Host — trivially set by curl on the tailnet) posts a malformed body, e.g. `--data '{not valid json'`.
- expected vs actual: malformed JSON → 400 like `/start` · actual: unhandled `JSONDecodeError` → 500.
- evidence: 464 et al. (unguarded) vs 257-264 (the guard pattern that exists but wasn't applied).

### B11 — two independent module-level task-ID generators can mint identical IDs → `_task_index` overwrite, feed misattribution — MAJOR — fixed
- class: data integrity / ID collision · location: `synapse/bridge/confirm.py:21-25` vs `synapse/pipeline/app.py:342,359` + `synapse/threads.py:111-112` · found-by: H4
- symptom: `confirm.py` and `app.py` each have their own `itertools.count(1)` producing `task-{int(now*1000)}-{seq}`. The counters never share state, so a voice/dispatcher task and a UI-gate task can get the identical string. `_task_index[task_id] = thread_id` overwrites unconditionally → `thread_for_task()` returns the wrong thread, misrouting that task's live log into another (possibly cross-project) thread's feed.
- trigger: two submissions — one via `ConfirmFlow.submit`, one via `_launch_run` — in the same millisecond at the same seq (guaranteed for the first call of each under a fixed `FakeClock`).
- expected vs actual: task IDs unique process-wide · actual: no cross-module uniqueness; silent index overwrite.
- evidence: `confirm.py:21-25` + `app.py:342,359` (two counters, same format), `threads.py:111-112` (unconditional overwrite).

### B12 — `ConfirmFlow.submit` stages a task in two separate persisted writes → crash between them wedges the flow forever — MAJOR — fixed
- class: data integrity / non-atomic persist · location: `synapse/bridge/confirm.py:150-156` + `synapse/bridge/state.py:192-196,222-224` · found-by: H4
- symptom: `submit` does `store.start_task(...PENDING_CONFIRMATION...)` (persists task, `staged` still null) then `store.set_staged(...)` (persists staged) — two independent `_persist()` calls. A crash between them leaves `state.json` with a task stuck `PENDING_CONFIRMATION` but `staged: null`. On restart, `_load` zombie-reconciles only `RUNNING` tasks; `has_active_task()` returns True forever (blocks all `submit`), while `confirm()` rejects ("Подтверждать нечего") since `_staged is None`. Only `request_cancel` can free it.
- trigger: process kill after `start_task` returns but before `set_staged` completes.
- expected vs actual: staging is one atomically-persisted op · actual: two writes with a crash window desyncing `task` vs `staged`.
- evidence: `confirm.py:154-155` (two calls), `state.py:192-196` + `222-224` (two `_persist`).

### B13 — `record_commands` wipes prior manifest entries when re-run for a new `--bg` without `--resume` — MAJOR — fixed
- class: silent data loss · location: `synapse/runners/record_commands.py:18-19,44-45,74-78` · found-by: H5 (H4 parked the related non-atomic write)
- symptom: `manifest.json` is one file per `--out` dir holding entries across multiple `--bg` conditions (schema includes `bg`). `record_session` sets `manifest = [] if not resume`, ignoring existing entries; on the first `_save_manifest` of a new-`--bg` run it overwrites `manifest.json` with only new entries, discarding metadata rows for every earlier condition. The `.wav` files survive on disk but become untracked/undiscoverable.
- trigger: `record_session(...,"тихая",resume=False)` to completion, then `record_session(...,"улица",resume=False)` in the same `--out` — the тихая rows vanish on the first улица save.
- expected vs actual: manifest retains entries from earlier `--bg` sessions; `--resume` only governs re-recording the same phrase/bg · actual: any non-resume run in a used dir discards the prior index.
- evidence: 44 (`[] if not resume`), 74-78 (filename per-bg, but manifest saved from the wiped list).
- note: offline CLI tool, no live-service blast radius.

### B14 — `TaskStore.apply_event` appends duplicate terminal events past terminal status → persisted event list grows — MINOR — fixed
- class: data integrity / duplicated records · location: `synapse/bridge/state.py:230-241` · found-by: H4
- symptom: `apply_event` appends every event to `task.events` unconditionally (233); the terminal guard (239) only protects the *status* transition, not the append. A repeat terminal SDK message (the code's own B3 comment: "a second ResultMessage / a task_failed after task_completed") duplicates a `task_completed`/`task_failed` into the persisted `events` list and renders as an extra line in `snapshot`/`render_state`.
- trigger: Kora's SDK stream yields two terminal `ResultMessage`s for one task.
- expected vs actual: a duplicate terminal signal is a no-op for the task record · actual: `.status` protected but `.events` still grows + persists.
- evidence: 233 (append outside guard) vs 239 (guard covers only status).

---

### Parked (out of DEEP scope / not proven behavioural bugs)
- `synapse/pipeline/app.py:167-168,286` — `_gate_locks` dict grows one entry per thread_id with no eviction (unbounded memory over a long-lived host). — H2
- `synapse/pipeline/app.py:157-168` — `current_http_thread` setter resets `_output_task`/`_gate_locks`; harmless today (fires once in `__init__`), landmine if reassigned post-construction. — H1
- `synapse/pipeline/static/{status-widget.js,logs.html}` — dead code, unreferenced by UI v3 (tests assert absence). — H3
- `POST /api/threads/{id}/message` — no server-side length cap on `text` → unbounded feed growth. — H3
- `synapse/runners/record_commands.py:29-30` — `_save_manifest` uses direct `write_text` (no tmp+rename) unlike every other JSON store; crash mid-write corrupts whole manifest. — H4
- `synapse/config.py:18,96` — `google_api_key` loaded but never referenced; dead config field. — H5
- `.venv/.../pipecat/pipeline/service_switcher.py:318` (third-party) — fatal `ErrorFrame` never reaches `SynapseFailoverStrategy.handle_error`; cascade all-tiers-failed speech won't fire for fatal errors. — H5
- `synapse/dispatcher/mock_llm.py:20,45-46` — lone "стоп" routes to `confirm_task(deny)` with no pending confirmation; acceptable (MockLLM is demo plumbing). — H5
- `synapse/pipeline/webrtc_server.py` — `spawned_monitor` assigned but never read (dead code). — H2
- `synapse/bridge/kora.py` — `_pending_answer`/`_run_owner`/`_run_root`/`_run_model`/`_run_gate_mode` are four independently-guarded single-run slots with no unifying invariant (maintainability smell). — H2
---

## Hunt 2026-07-14b — whole app, second round (5 hunters, read-only, tree @ 44f6e22)

Scope: all of `synapse/` (post the UI-v4 codeflow redesign). Hybrid assignment — every file one deep owner + one lens each across the whole tree. IDs continue the `B*` namespace (never reused): **B15–B22**, 3 CRIT + 5 MAJOR. Prior round's B01–B14 confirmed still fixed; these are fresh defects, several are *residuals* the earlier fixes left open at an adjacent site. **All 8 PROVEN** — 3 sonnet test-writers, disjoint files (`tests/test_hunt0714b_app.py` / `_bridge.py` / `_dispatch.py`), one red test per ID, each verified red at its own assertion matching expected-vs-actual; full suite **482 passed + 8 new reds** (additive, no collection breakage / fixture collision / state leak). B22's proof is structural (canonical single-user-message shape, no live-API round-trip).

**FIX round outcome (autonomous, tero away): 7 of 8 FIXED, 1 PARKED.** Opus fixed the risky/security/money/concurrency defects directly (B15-attempt, B16, B17, B18, B19, B20, B21) + one sonnet on the isolated mechanical llm_client coalescing (B22), serial; each landed red→green with assertions untouched; full suite **489 passed + 1 xfail (B15)**; concurrency-flavoured fixes (B17/B20/B21) stable green 3×. **B15 PARKED** — proven but deferred to Tero: the proof test asserts the WRONG enforcement layer (dropping the downstream End frame can't un-bill a completed call and would hang the turn that trips the cap), so no correct fix flips it green; the correct fix (gate the paid REQUEST frame + emit the existing cost-cap fail speech) is pipecat surgery on the LIVE voice path plus a UX decision (what the user hears when a turn is cost-blocked mid-conversation) that must be live-tested, not landed autonomously. **Default cap is 500/day → this is live default behaviour, HIGH priority for Tero's review.** Test xfail'd (`strict=False`, tagged B15) as regression armour; owed: a corrected request-layer proof test + the request-time cost gate.

Fix notes (nuanced ones — where the naive fix collided with an existing invariant or a hard tradeoff):
- **B16**: the gate cannot police what `Grep`/`Glob`/`LS` read via directory recursion, so it now SCANS the target subtree for a readable secret file and denies `secret_path` if found (`_subtree_has_readable_secret`). A `_is_secret_path` scan that honoured dir-SEGMENTS would deny a search in ANY git checkout (`.git` is a secret segment) — so the scan skips hidden entries, mirroring the tools' default (no `--hidden`) traversal. Fail-closed: OVER-denies a gitignored secret (safe), does NOT parse `.gitignore`, and a secret reachable only via explicit `--hidden` is a residual parked for M1.1.
- **B17**: `push_speak_frame` now RETURNS whether it queued; `_on_speak_frame_done` reverts the optimistic ledger mark on a clean `False` (the finished/unbound silent-drop path) too — B01 only covered the raise/cancel paths. No arbiter fallback runs for this path (speak() saw the task live), so the revert is the sole re-arm.
- **B18**: `write_code` resolves the plan path against the same default workspace the runtime uses (`_resolve_root_for(th) or ~/synapse-kora-workspace`, mirroring kora.py:485) — `Path(None)` no longer crashes past the ValueError-only guard.
- **B19**: `validate_project_path` gained `require_exists=False`; `_load` re-validates every row through it, dropping secret-rooted/system/home rows on load (security is a store invariant) while a transiently-unmounted project survives (existence relaxed).
- **B20**: compaction now splices against the CURRENT list post-`await` (`if history[:len(older)] == older: history[:len(older)] = [summary]`), not the stale pre-await `tail` snapshot — the summarized older prefix is append-immutable, so concurrent same-thread commits at the tail survive; a concurrent rebind → no-op, not clobber.
- **B21**: `SynapseFailoverStrategy.advanced_this_generation()` (keyed on `GenerationGuard.current_generation`, stable across a turn's tier retries) lets the switcher skip counting a tier0 that `_advance` already counted on failover-back — the `idx==0` premise "tier0 is only ever the initial tier" is now guarded, not assumed.

Proof (test node id per bug — B15's is xfail'd, the rest flip green after their fix):
- B15 `tests/test_hunt0714b_app.py::test_B15_tripped_cost_cap_must_block_further_tier0_generation`
- B16 `tests/test_hunt0714b_bridge.py::test_B16_grep_recursion_into_secret_file_must_be_denied`
- B17 `tests/test_hunt0714b_app.py::test_B17_silent_drop_on_finished_task_must_revert_ledger`
- B18 `tests/test_hunt0714b_app.py::test_B18_write_code_projectless_none_root_returns_error_not_typeerror`
- B19 `tests/test_hunt0714b_bridge.py::test_B19_load_must_drop_secret_rooted_project`
- B20 `tests/test_hunt0714b_dispatch.py::test_B20_compaction_across_await_drops_concurrent_turn_commit`
- B21 `tests/test_hunt0714b_app.py::test_B21_failover_back_to_tier0_counts_exactly_once`
- B22 `tests/test_hunt0714b_dispatch.py::test_B22_parallel_tool_results_coalesce_into_single_user_message`

### B15 — cost cap trips but is never ENFORCED on the healthy primary tier → daily paid-call cap fails open — CRIT — parked (proven; deferred to Tero — request-layer fix + UX decision, live voice path)
- class: money / cost-cap fails-open · location: `synapse/pipeline/app.py:406-411` + `synapse/cascade/strategy.py:89-92` + `synapse/cascade/services.py:92-105` · found-by: H5 (corroborated: H4)
- symptom: `CostCap.record_paid_attempt` returns a bool that means "may this attempt proceed" (False once tripped). That return is honoured in exactly ONE place — the failover path `strategy._advance` (90-92 → `_fail_all("cost_cap")`). On the common happy path the initial tier (services[0], paid) runs with NO failover, so `_advance` never runs; the only accounting is `_CostCountingLLMSwitcher.push_frame` (409-410) which calls `record_paid_attempt` and **discards the return**. There is no pre-call gate on tier0. `grep -rn '\.tripped' synapse/` → zero readers. So once the cap trips, tier0 keeps making real billed calls every turn, unbounded, until the daily reset — violating the services.py contract "overshoot bounded to ≤1 call past the cap".
- trigger: `max_paid_calls_per_day=N`; run N+1 turns that all succeed on tier0 (the normal case). Turn N trips the cap; turns N+1… keep billing.
- expected vs actual: once tripped, no further paid call proceeds on any tier · actual: only failover is blocked; the primary tier bills without bound.
- evidence: 409-410 (return ignored); strategy.py:90 (sole consumer); services.py:100-101 (`if self._tripped: return False`). Distinct from B04 (which fixed *counting*); this is the *enforcement* residual.

### B16 — gate secret-containment is bypassable via `Grep`/`Glob`/`LS` recursion into a directory → secret-file exfiltration from a legit workspace — CRIT — fixed
- class: security / secret-path containment bypass · location: `synapse/bridge/kora.py:553-567,569-596` · found-by: H2
- symptom: the gate enforces secret containment per resolved PATH (`_is_secret_path(resolved)`, 565/583), but `Grep`/`Glob`/`LS` take a DIRECTORY and recurse *inside the tool*, which the gate never sees. A `Grep` with `path:"."` (or no path → cwd) against a perfectly legit workspace that happens to contain a non-hidden secret file (`secrets.yaml`, `credentials.json`, `token.txt` — names the gate's own `_SECRET_FILE_NAMES` lists) resolves to a NON-secret directory → allowed → ripgrep reads the secret file's contents and returns them. The gate denies `Read(workspace/secrets.yaml)` but allows a `Grep` that reads the very same bytes — the exact asymmetry the module's contract ("denied for ALL file tools even inside the workspace") forbids. `output_mode:"content"` turns it into direct exfiltration.
- trigger: legit workspace containing e.g. `secrets.yaml`; prompt-injected Kora runs `Grep({"pattern":".","path":".","output_mode":"content"})`.
- expected vs actual: any read of a secret file under the workspace is denied for every file tool · actual: directory-recursion tools recurse into secret files the gate cannot deny per-file. Distinct from B03 (root itself secret) and B05 (project denylist): here the root is legit, the leak is per-file-under-a-legit-root.
- evidence: 555-567 (no-path read/search: only `_is_secret_path(ws_resolved)` on the ROOT); 583-595 (with-path: `_is_secret_path` on the single path — a directory resolves non-secret → allow); the gate reads only `_PATH_KEY[tool]` and ignores `output_mode`/`pattern`.

### B17 — a critical SPEAK dropped on the `has_finished()` silent-drop path never reverts the ledger → Р-15г watchdog disarmed — CRIT — fixed
- class: data-integrity / safety-invariant + lifecycle · location: `synapse/pipeline/app.py:190-192,204-217,221-229` · found-by: H1
- symptom: `speak()` marks the ledger spoken optimistically (204) then fire-and-forgets `push_speak_frame`, reverting the mark ONLY if the future is cancelled (222) or raises (225-229). But `push_speak_frame` re-checks `not t.has_finished()` and, when the output task finished/was unbound in the scheduling window, **returns normally without queueing and without raising** (190-192, no else). `_on_speak_frame_done` then sees no cancel and no exception → does NOT revert. The critical stays `spoken=True` while producing no audio, so `SpeakLedger.check()` never emits `CRITICAL_WITHOUT_SPEAK`. This is precisely the silent-drop the guard's own docstring (187-188) describes; B01 closed the *raise* path but left this *clean-return* path open. The else-branch arbiter fallback (219) is NOT taken (the task was live at speak() time), so the SPEAK is truly lost with no fallback.
- trigger: CRITICAL event with `speak_text` while the output task is live (passes the 206 check), then the task finishes before the scheduled coroutine runs (client disconnect cancels `output_task`; or normal pipeline completion in the ensure_future→coro window).
- expected vs actual: a critical that produced no audio is left `spoken=False` so the watchdog re-fires (as the cancel/exception branches do) · actual: the finished/unbound silent-drop leaves it `spoken=True`, permanently disarming the watchdog for that critical.
- evidence: 190-192 (returns without raising when `t is None or t.has_finished()`); 221-229 (reverts only on `cancelled()`/`exception()`, never on a normal `None` return); 187-188 (docstring admits the drop is silent).

### B18 — `write_code` gate crashes with `TypeError` on a projectless thread under default config (root=None) → uncaught 500 / voice-turn crash — MAJOR — fixed
- class: error-handling / unguarded-exception · location: `synapse/pipeline/app.py:343-344` + `synapse/config.py:77` · found-by: H3
- symptom: the `write_code` branch does `root = self._resolve_root_for(th)` then `Path(root) / "docs" / ...` (343-344). `_resolve_root_for` returns `cfg.kora_workspace_dir` verbatim for a projectless thread, which defaults to `None` (config.py:77). `Path(None)` raises `TypeError`, which executes BEFORE the `try/except ValueError` guard at 351-353 (that guard wraps only `_launch_run` and catches only `ValueError`). The route `api_thread_gate` (webrtc_server.py) has no surrounding try/except → HTTP 500; the voice path crashes the turn identically. `send_to_kora` survives a None root because it passes it into `kora_runner.start` where a `or expanduser(...)` fallback applies; `write_code` builds the `Path` itself with no fallback — the two branches diverge.
- trigger: default config (no `KORA_WORKSPACE_DIR`) + a thread with no `project_id`; `POST /api/threads/{id}/gate {"action":"write_code","confirm":true}` or the voice `gate_action(action="write_code")`. Crash fires unconditionally at `Path(root)`, before the `plan_path.exists()`/`stale_plan` checks.
- expected vs actual: return a diagnosable error dict (`no_plan_file`/`stale_plan`), route maps to 400/404/409 · actual: `TypeError` propagates uncaught → 500 / voice crash.
- evidence: 343-344 (no None-guard, no `_workspace()` fallback); config.py:77 (`kora_workspace_dir: str | None = None`); 351-353 (guard is ValueError-only and below 344).

### B19 — `ProjectStore._load` re-admits secret-rooted projects with zero validation → the B05 fix only stops NEW secret projects, not persisted ones — MAJOR — fixed
- class: load-path / data-integrity (security) · location: `synapse/projects.py:57-69` vs `28-47,83-89` · found-by: H4
- symptom: `add()` gates every new project through `validate_project_path` (the B05-hardened denylist incl. `.ssh/.aws/.kube/.docker`), but `_load()` reconstructs projects straight from `projects.json` with NO validation. Validation is therefore only a write-path check, not a store invariant. A `projects.json` row whose `path` is a secret dir is loaded verbatim and served by `get()`; `SynapseHost._resolve_root_for` hands that path to Kora as her workspace root, re-arming the B16/B03 exfiltration surface.
- trigger: a project rooted at `~/.ssh` added BEFORE the B05 denylist shipped (when `.ssh` was still accepted), then the process is upgraded to the fixed build and restarted — `_load` restores it unchanged, no migration/purge. Also reachable by any hand/legacy edit of `projects.json`.
- expected vs actual: a persisted project rooted at a `_FORBIDDEN_HOME_SUBDIRS` member is rejected/dropped on load exactly as `add()` would · actual: `_load` admits it silently.
- evidence: 60-68 (`_load` builds `{id,name,path}` with no `validate_project_path`); 84 (`add` calls it); 15-18 (module comment asserts this denylist decides where the workspace root may be pinned — an invariant `_load` breaks).

### B20 — history compaction mutates the SHARED per-thread list across an `await`, dropping a concurrent same-thread turn's committed messages — MAJOR — fixed
- class: concurrency / shared mutable state across await · location: `synapse/dispatcher/loop.py:101,106,222-241` · found-by: H1 (corroborated: H4, H5)
- symptom: `ingest_user_turn` fetches the LIVE shared list (`history = self._history_for(thread_id)`, 101) and calls `await self._maybe_compact(thread_id, history)` (106). Inside, `_maybe_compact` snapshots `tail = history[cut:]` (223), then `await self._llm.complete(...)` (234, real suspension), then `history[:] = [{summary}, *tail]` (238-241). `tail` was captured before the await, so any messages a concurrent same-thread turn committed during the await (158-160) are silently overwritten and lost. B02 moved the TURN onto a `working` snapshot but left the COMPACTION path touching the shared list across an await with no lock.
- trigger: two concurrent `POST /api/threads/{same_id}/message` (double-click / two tabs); `ingest_user_turn` is not serialized (turn_lock released before it runs, B-PIPE-5); `dispatcher_compact_after` defaults to 40 so compaction is live once history > 40. Turn A snapshots `tail`, awaits the compaction LLM call; turn B commits its (user,assistant) pair; turn A resumes and rebinds from the stale `tail`, dropping B's messages.
- expected vs actual: compaction rewrites only the older half and preserves every message other turns committed (the B02 contract) · actual: B's committed messages vanish.
- evidence: 101 (live shared list), 106 (await compaction on it), 222-223 (pre-await snapshot), 234 (await), 238-241 (whole-list rebind from stale tail), 158-160 (concurrent post-await append).

### B21 — failover back to tier0 double-counts the cost cap (the `idx==0` premise "tier0 is only ever the initial tier" is false) — MAJOR — fixed
- class: money / failover double-charge · location: `synapse/pipeline/app.py:406-410` vs `synapse/cascade/strategy.py:89-92` · found-by: H5
- symptom: `_CostCountingLLMSwitcher` counts a paid attempt on any successful generation while `active_tier_index()==0`, on the stated assumption (app.py:394-397) that the initial tier is the one failover never pre-counts. But `strategy._advance` calls `record_paid_attempt` for WHATEVER tier it switches to, including tier0 when the breaker's `first_available` returns 0 (tier0's mute expired). So a generation that failed over back to tier0 is counted twice: once in `_advance` (89-90), once again by the success end-frame (409).
- trigger: turn 1 tier0 errors → fail over to tier1 (tier0 muted). Later turn (tier0 mute expired): tier1 errors → `_advance` → `first_available`→0 → `record_paid_attempt` (+1) → activate tier0 → tier0 succeeds → `LLMFullResponseEndFrame` with `active_tier_index()==0` → switcher counts tier0 again (+1).
- expected vs actual: one paid tier0 attempt = one increment · actual: two → cap trips prematurely, denying paid service before the real limit.
- evidence: strategy.py:89-92 (`record_paid_attempt` for `next_idx`, incl. 0 on failover-back); app.py:408-410 (counts again for `idx==0`); app.py:394-397 (the false premise, in prose).

### B22 — a multi-tool assistant turn emits N tool_results as N consecutive `user` messages → non-canonical Anthropic Messages shape — MAJOR — fixed
- class: API/contract misuse · location: `synapse/dispatcher/llm_client.py:44-52` (driven by `synapse/dispatcher/loop.py:133-141`) · found-by: H5
- symptom: when the model returns ≥2 `tool_use` blocks in one turn, the loop appends one `{"role":"tool"}` message per call (loop.py:140-141), and `_to_anthropic_messages` maps EACH to its own `{"role":"user","content":[tool_result]}` (llm_client.py:44-52). The result is one assistant turn with N tool_use blocks followed by N consecutive user messages, instead of the canonical single user message carrying all N tool_result blocks. loop.py:131-132's own comment asserts the API rejects a non-canonical shape ("без него Anthropic Messages API отклоняет историю"); single-tool turns (the MockLLM/test path) never exercise it, so it slips the suite.
- trigger: any HTTP `/message` turn where Claude emits parallel tool calls (e.g. `get_task_status` + `bind_project`).
- expected vs actual: one user message coalescing all N tool_result blocks · actual: N separate consecutive user messages.
- evidence: llm_client.py:44-52 (fresh user message per tool message, no coalescing); loop.py:133-141 (one assistant announce + one tool message per call). Verify note: proof is a STRUCTURAL test against the self-declared canonical shape (llm_client output coalesces the tool_results), not a live-API round-trip; if that contract can't be pinned to a red assertion, downgrade to not-test-verifiable.

### Parked — this round (candidates, not filed as proven)
- `synapse/dispatcher/tools.py:208-214` — `ToolHandlers.begin_turn` is non-idempotent (`self._dedup[turn_id] = {}` unconditionally); because B08's journal backstop returns the SAME turn_id for a concurrent overlapping turn, a second `begin_turn(turn_id)` wipes the in-flight turn's mutating-tool dedup latch → an intra-turn cascade retry of `submit_task`/`confirm_task` could double-execute. Compound (needs B08 overlap window + same-turn retry); entangled with the parked "full per-turn serialization" residual. — H1, H4
- `synapse/cascade/strategy.py:78-92` + `synapse/pipeline/app.py:406-410` — a FAILED tier0 attempt is counted by neither site (switcher counts only on success End; `_advance` counts the *next* tier). Under-count vs the "per-attempt" contract, but whether a failed attempt (e.g. a 429) is billed at all is a design call — parked pending that decision. — H5, H4
- `synapse/pipeline/webrtc_server.py:594-597` — delete-project removes the project then unbinds threads; a crash between leaves a dangling `project_id` (harmless: `_resolve_root_for` falls back). Order is backwards for crash-safety. — H4
- `synapse/pipeline/webrtc_server.py:289-303` — trickle-ICE PATCH offer routes return `{"status":"success"}` without validating the session id (unlike the POST offer route that 404s); unproven (pipecat-internal drop behaviour). — H3
- `synapse/journal.py:144-154` — `record_kora_event`'s `_write` has no `OSError` guard (unlike `alert`); a disk-full during a healthy Kora event would terminalize a genuinely-healthy task via the `_run` broad except. Timing-dependent, unproven. — H3

---

## Gate v2 residuals (tero run 2026-07-14-gate-v2-access, accepted by user)

- **Bash is not path-gateable** — MAJOR/accepted: gate v2 opens Bash for Kora ("читать везде, писать в проект" — user order). A shell can `cat` any readable file (incl. secrets past the lexical token scan) and reach the network. Mitigations shipped: lexical secret-token scan of every command (deny `secret_path`), full command journaled on every gate_allow (audit trail), Bash fully denied in docs_only mode. Risk stated to user once and accepted.
- **Bash-Kora can curl its own control plane** (P9) — MAJOR/accepted: with egress open, Kora can POST to `localhost:786x/api/*` (e.g. approve her own gate via `/api/threads/{id}/gate` — Kora-approves-Kora). localhost is NOT blocked (would break legit local tooling). Real fix is auth on /api/* beyond CSRF — parked P9, revisit before any non-tailnet exposure.

---

## 2026-07-14 — сбор проблем (live-тест Теро на staging :7861, НЕ чинить без отмашки; улики: /tmp/synapse-staging-7861/session-1784047140289.jsonl + threads/15e761850a89.feed.jsonl; run file ~/.claude/tero/runs/2026-07-14-gate-v3-write-liveness.md)

### B23 — idle-Кора репортится как UNREACHABLE: liveness меряет возраст последнего события безусловно — MAJOR — fixed
- class: state/liveness false-positive · location: `synapse/bridge/state.py:263-279` (потребители: `synapse/pipeline/app.py:260-266` monitor-алерт; `render_state`/`render_state_template` → промпт диспетчера, CANON_PHRASE «Кора не в сети — давно нет сигнала»; snapshot → get_task_status/kora-status)
- symptom: `liveness()` возвращает STALE/UNREACHABLE по `now - _last_event_ts` даже когда задачи НЕТ или она давно COMPLETED/FAILED. Кора здорова и просто простаивает ≥120с/≥300с → диспетчер говорит «Кора не в сети», отказывается диспатчить новую задачу и предлагает «выключить старую» (которая давно completed). `_last_event_ts` персистится в state.json → свежая голосовая сессия наследует «просрочку» вчерашней задачи и стартует сразу с unreachable.
- trigger: любой звонок после ≥5 минут простоя Коры. Live-repro 12:46: алерт `{"liveness":"unreachable"}` в 12:46:10 ДО task_started; после task_completed (12:50:36) снова stale (12:52:40) и unreachable (12:55:40).
- expected vs actual: staleness — свойство ОЖИДАЕМОГО сигнала: без активной задачи (RUNNING/PENDING_CONFIRMATION/CANCEL_REQUESTED) Кора idle = OK · actual: idle = unreachable, диспетчер блокирует работу и врёт пользователю.
- root cause: нет гейта на статус задачи перед возрастной проверкой; R6-персист (нужный для «рестарт при мёртвой Коре mid-task → stale сразу») усугубляет — часы наследуются между сессиями.
- fix sketch: в `liveness()` после `_awaiting_answer`-ветки: task None или terminal → OK; остальное как есть. R6 сохраняется (mid-task рестарт восстанавливает RUNNING).

### B24 — Кора отказывается создавать файлы вне workspace (рабочий стол) — политика v2 уже, чем хочет владелец — MAJOR/policy — fixed
- class: policy/UX · location: `synapse/bridge/kora.py` — `_gate_decision` (Write/Edit/NotebookEdit вне ws → deny `outside_workspace`) + `_system_prompt` («Создавать и изменять файлы можно только внутри {workspace}»)
- symptom: «создай helloworld.txt на рабочем столе» → Кора отказывается, даже tool не зовёт (журнал 12:50: 1 ход, ноль tool_use — остановил промпт). Формально это НЕ баг: работает политика «писать в проект», выбранная Теро в gate v2. Но 2026-07-14 Теро приказал расширить: **«везде она может писать»**.
- expected (новый приказ) vs actual: Write/Edit/NotebookEdit разрешены везде, кроме секретных путей (симметрично чтению), docs_only-режим сохраняется; промпт: писать можно везде кроме секретов, дефолт-директория — workspace/папка проекта, если путь не назван · actual: запись только в workspace.
- fix sketch: гейт v3 — убрать outside_workspace-deny для write-инструментов, секрет-чек (`_is_secret_path` по всем компонентам, включая файл ВНУТРИ секретной папки: ~/.ssh/new_key) оставить; переписать абзац промпта; перепин tests/test_kora, test_gate_v2, test_bughunt_w3 B21-проба (Write /etc/passwd → сменить на секретный путь, инвариант «deny detail без пути» сохранить).

### B25 — ответы диспетчера попадают в ленту треда с лагом в целый ход; последний — только после «Завершить» — MAJOR — fixed
- class: UX/data-flow lag · location: `synapse/pipeline/app.py:813` (единственные вызовы `_flush_voice_context`: начало СЛЕДУЮЩЕГО `_on_end_of_turn` + `flush_voice_feed` на disconnect), флашер :843-858
- symptom: в чате звонка реплики пользователя появляются сразу (D1' пишет транскрипт напрямую), а ответы диспетчера — только когда пользователь скажет СЛЕДУЮЩУЮ фразу (диффом контекста), последний ответ — только на disconnect. В live-виде треда выглядит как «его ответы просто войсом, а текста нет» (жалоба Теро 2026-07-14). Данные при этом НЕ теряются — лента на диске полная, задним числом.
- trigger: любой звонок; открыть тред во время звонка (SPA это позволяет) или посмотреть чат сразу после ответа.
- expected vs actual: ответ диспетчера в ленте через ≤3с после произнесения (интервал pollFeed) · actual: лаг до бесконечности (пока нет следующей реплики).
- root cause: флашер событийно не привязан к commit ответа; pipecat `LLMAssistantAggregator.push_aggregation` (llm_response_universal.py:1595-1612) кладёт `{"role":"assistant","content":<str>}` в контекст ровно в момент, когда ответ сказан (агрегатор downstream TTS) — но наш код это событие не слушает.
- fix sketch: post-commit колбэк в `GuardedAssistantAggregator` (make_guarded_assistant_aggregator): после `push_aggregation()` звать `_flush_voice_context`. Курсорный дифф уже идемпотентен.

### B26 — completion-SPEAK озвучивает «Задача выполнена», даже когда Кора ОТКАЗАЛАСЬ выполнять — MINOR (grounding, родня B13) — open
- class: grounding/misleading narration · location: completion-SPEAK темплейт из task_text (NO-EXFIL backstop, слайс 4)
- symptom: 12:50 Кора ответила отказом («Не могу выполнить эту задачу…») и завершила сессию без единого tool_use → task_completed → голос: «Задача выполнена: Создай файл helloworld.txt на рабочем столе». Пользователь слышит успех при фактическом отказе.
- expected vs actual: терминальная озвучка не должна утверждать успех, который не подтверждён · actual: темплейт «Задача выполнена: <task_text>» на любой task_completed.
- note: темплейт из task_text — сознательный NO-EXFIL-бэкстоп (текст Коры в SPEAK не идёт); фикс должен не сломать этот инвариант (например, нейтральное «Кора завершила работу над задачей: …»).

### B27 — одна Кора на всю систему: параллельные задачи невозможны — LIMITATION (by design v1) — open
- class: design limitation · location: хост-синглтон (слайс 0), `TaskStore` §1 «одна активная задача»
- symptom: Теро: «значит у нас только одна кора есть почему так». Вторая задача при живой первой невозможна; при зависшей — только cancel и заново.
- note: v1-скоуп срезан сознательно (M1-спека). Пул/очередь Кор — проектировать отдельно (M1.1), затрагивает TaskStore-инвариант, роутинг диспетчера, ленты тредов, gate-стейджи.

### B28 — во время звонка на экране нет текста ответов диспетчера (live-оверлей показывает только статус) — MINOR/UX — open
- class: UX gap · location: `synapse/pipeline/client/app.js:760-777` (live-overlay: только «Диспетчер слушает…/отвечает» + wave-бары)
- symptom: «текст не появляется на экране от диспетчера» — в голосовом оверлее нет live-captions; текст ответа виден только в треде (и то с лагом — B25).
- fix sketch: после B25 (мгновенный флаш) оверлею достаточно поллить ленту войс-треда и показывать последнюю assistant-запись; либо RTVI bot-transcript события из pipecat.

### B29 — диспетчер на мета-вопросы отвечает не по делу («Я всего лишь диспетчер и нахожусь здесь, у вас на линии») — MINOR (prompt quality, родня P10) — open
- class: prompt/anti-hallucination · location: `synapse/prompt.py` (системный промпт диспетчера)
- symptom: на «Почему не знаешь?» / «В смысле диспетчер?» — бессодержательные ответы; диспетчер не умеет объяснить, кто он и что происходит (например, почему Кора «не в сети» — см. B23).
- note: P10 (ревизия анти-галлюцинационного промпта) уже в парковке gate-v2 рана; этот кейс — конкретный репро туда.

### Доказательства (bughunt PROVE 2026-07-14, 2 соннет-тест-райтера, непересекающиеся файлы; фиксов НЕТ — приказ «чинить не будем»)
Каждый xfail(strict=True) краснеет на СВОЁМ ассерте под `--runxfail` ровно по documented expected-vs-actual; инвариант-компаньоны зелёные сейчас и обязаны пережить будущий фикс. Полная суита: **545 passed + 5 xfailed** (4 новых + доживший B15), additive, без collection-коллизий. Когда баг починят — strict-xfail станет xpass и громко уронит суиту (напоминание снять маркер, паттерн «regression armour» B15).
- **B23** → `tests/test_bugs_0714_bridge.py::test_B23_completed_task_idle_is_ok_not_unreachable` (idle COMPLETED-задача → liveness даёт UNREACHABLE вместо OK). Инварианты ЗЕЛЁНЫЕ: `::test_B23_running_task_stale_signal_stays_unreachable` (мёртвая Кора mid-task → UNREACHABLE) и `::test_B23_failed_task_stale_stays_unreachable_zombie_ambiguity` (FAILED+stale → UNREACHABLE — граница скоупа, см. фикс-запись).
- **B24** → `tests/test_bugs_0714_bridge.py::test_B24_write_outside_workspace_nonsecret_is_allowed` (Write в несекретный путь вне workspace → deny `outside_workspace`, желаемое allow). Security-инвариант ЗЕЛЁНЫЙ: `::test_B24_write_to_secret_path_stays_denied` (Write в `secrets.yaml` остаётся `secret_path`-deny — симметрия чтению обязана уцелеть).
- **B25** → `tests/test_bugs_0714_voiceflush.py::test_dispatcher_answer_reaches_feed_at_commit` (драйвит РЕАЛЬНЫЙ `GuardedLLMAssistantAggregator.push_aggregation()` → `_context.add_message`; на момент commit'а лента держит только `['user']`, ответ диспетчера отсутствует). Фикс зацепит post-commit-колбэк на тот же `push_aggregation`, и тест позеленеет.
- **B26 / B27 / B28 / B29 — `not-test-verifiable`** (тестом не покрыты, обоснование):
  - B26 (completion-SPEAK «Задача выполнена» при отказе Коры) — «правильная» озвучка не определена (grounding-суждение, родня B13); юнит зафиксировал бы произвольную формулировку. Ручная проверка: задача-отказ → слушать терминальную SPEAK.
  - B27 (одна Кора) — by-design v1-синглтон, уже пинится `tests/test_host_singleton.py`; «баг»-теста нет, это M1.1-скоуп.
  - B28 (нет live-captions в голосовом оверлее) — JS-оверлей `app.js`, не юнит-тестируемо осмысленно; проверять глазами в браузере во время звонка.
  - B29 (мета-ответы диспетчера) — качество промпта, требует LLM-суждения, не детерминированный ассерт; репро для парковки P10.

### Фикс (bughunt FIX 2026-07-14, приказ Теро «ок починили давай»; Opus сам, red→green, суита 549 passed + 1 xfailed)
- **B23** (`synapse/bridge/state.py` `liveness`): после `_awaiting_answer`-ветки добавлен гейт — **только COMPLETED → OK** (не FAILED). Проверка: `test_B23_completed_...` зелёный.
  - ⚠️ СКОУП-РЕШЕНИЕ (не как в fix sketch «task None или terminal → OK»): FAILED НЕ включён, т.к. статус FAILED ставится ДВУМЯ путями — Кора `task_failed` (idle, жива) И S13 зомби-реконсиляция на буте (`_load`: RUNNING-на-крэше → FAILED). По статусу они неразличимы → FAILED→OK сломал бы R6 (`test_persistence_roundtrip_restart_reports_stale_immediately`: мёртвый раннер после рестарта обязан репортить UNREACHABLE). Зомби всегда FAILED, никогда COMPLETED → COMPLETED→OK безусловно безопасен и совпадает с политикой `_status_color` (COMPLETED бьёт stale, FAILED — нет). No-task тоже НЕ включён: `test_liveness_thresholds` (heartbeat без задачи → UNREACHABLE) заморожен, а стейджинг-баг был на COMPLETED-пути.
  - PARK (design-tension P18): **genuine-FAILED idle тоже заслуживает OK**, но неотличим от зомби по статусу — нужен отдельный zombie-маркер (boot-reconcile событие уже есть: id `boot-reconcile-*`, reason «сервер перезапускался») чтобы расщепить два источника FAILED. До этого genuine-FAILED idle стареет в UNREACHABLE (тот же класс, что B23, только на FAILED-ветке).
- **B24** (`synapse/bridge/kora.py`): гейт v3 — мутирующие Write/Edit/NotebookEdit разрешены ВНЕ workspace (секрет-чек `_is_secret_path` на полном resolved-пути ДО этой ветки не тронут; docs_only-ран → вне ws = docs_only_violation). Проверки: `test_B24_write_outside_workspace_nonsecret_is_allowed` зелёный, `::test_B24_write_to_secret_path_stays_denied` зелёный. Перепинены под новый контракт: `test_kora` (sibling/home write→allow, journals-категории, read+write outside), `test_runspec` (default-ws больше не outside_workspace-deny), `test_bughunt_w3` B21-проба (Write секрета вне ws, инвариант «detail без пути» цел).
  - `_system_prompt` переписан: «создавать/изменять файлы можно где угодно кроме секретных; дефолт-путь — workspace». **`not-test-verifiable`** (LLM-поведение) — owed live-check: «создай helloworld.txt на рабочем столе» → Кора ДОЛЖНА выполнить (в стейджинге отказывалась на уровне промпта, tool не звала).
- **B25** (`synapse/pipeline/context_guard.py` + `app.py`): в `make_guarded_assistant_aggregator` добавлен опц. `on_commit`-колбэк — вызывается сразу после коммита ответа в контекст (guard на `is_aborted(gen)`, чтобы не слить отменённый failover-текст); в `app.py` прокинут `on_commit=_flush_voice_context`. Курсор флашера делает три вызова (commit / next-turn / disconnect) идемпотентными. Проверка: `test_dispatcher_answer_reaches_feed_at_commit` зелёный (драйвит реальный `push_aggregation`).

## 2026-07-14 — hands-on browser + fan-out UI hunt (16 находок, 3 CRIT; Opus senior + 4 соннет-хантера read-only)
Разведка: Opus кликал живой UI на стейджинге :7861 (Playwright) + 4 соннет-хантера на непересекающихся линзах (client/app.js edge-cases · webrtc_server routes security · gate/thread/project FSM · realtime voice→chat история). Приоритет Теро: **realtime→чат должен быть бесшовным и хранить ВСЮ историю** → войс-баги идут ПЕРВЫМИ. Все находки с file:line-уликами; фиксов пока НЕТ (ждём порядок). Дифф-кнопка («Пока нет изменений») — НЕ баг, а осознанная заглушка (`app.js:173` «плейсхолдер P2: реальный git diff не подключён»). `/api/browse` — traversal НЕ пробит (клетка `is_relative_to(home)`, скрытые каталоги спрятаны).

### B42 — REALTIME: недокоммиченный ответ диспетчера ТЕРЯЕТСЯ при «Завершить — в чат» посреди речи — CRIT — open
- class: history loss (прямо бьёт по приоритету Теро) · location: `webrtc_server.py:176-190` (on_client_disconnected → `task.cancel()`), `app.py:845-858` (`_flush_voice_context`), pipecat `llm_response_universal.py:1595-1612` (commit ТОЛЬКО на `LLMFullResponseEndFrame`) vs `1645-1657` (`_handle_end_or_cancel` — НЕ зовёт `push_aggregation`)
- symptom: юзер спросил, диспетчер НАЧАЛ отвечать (текст уже стримится через TTS, уже слышен), юзер тапает «Завершить — в чат» / вешает трубку → `task.cancel()` шлёт `CancelFrame` → агрегатор pipecat коммитит ответ в `context.messages` ТОЛЬКО на нормальном end-frame, на Cancel — НЕ коммитит → `_flush_voice_context` флашить нечего → в чате, куда попадает юзер, НЕТ ответа, который он только что СЛЫШАЛ.
- expected vs actual: лента треда обязана содержать каждый ответ, что юзер услышал · actual: самый частый кейс (обрыв главной hangup-кнопкой посреди ответа) роняет ответ на пол. B25 закрыл лаг commit→лента, но НЕ ответы, которые до commit не доходят.
- fix sketch: на teardown дофлашить хвост агрегатора ДО cancel — вызвать `push_aggregation()` (или его эквивалент) на войс-assistant-агрегаторе в on_client_disconnected ПЕРЕД `task.cancel()`, затем `flush_voice_feed()`. Гвард на пустой `_aggregation`.

### B43 — REALTIME: гонка реконнекта переклеивает живой звонок на ДРУГОЙ тред → история расщепляется — MAJOR — open
- class: history split / wrong-thread bind · location: `app.js:226-234` (гейт active-thread только на `!client`, НЕ на `liveRequested/connecting`), `app.js:898-927` (`probeSession` тихий реконнект: `client=null` на время хендшейка), `webrtc_server.py:687` (`voice_thread["id"]` переписывается безусловно)
- symptom: звонок привязан к T1; сеть моргнула → `probeSession` ставит `client=null` и запускает `connectVoice()` (ICE/DTLS — сотни мс…секунды). В это окно `client` falsy, хотя звонок жив. Юзер тапает другой тред → `render()` видит `client===null` → POST `/api/active-thread` переписывает `voice_thread["id"]=T2`. Реконнект дозавершается → `_on_end_of_turn` читает `voice_thread["id"]`=T2 → следующие ходы падают в T2, ранние в T1.
- expected vs actual: пока звонок жив (включая тихий реконнект) — история держится одного треда · actual: навигация в окне реконнекта расщепляет разговор по двум тредам. Меньший тот же зазор на ПЕРВОМ коннекте (между `liveRequested=true` и `client=me`).
- fix sketch: гейт active-thread в `render()` расширить на `!client && !liveRequested && !connecting`; ИЛИ сервер не переклеивает `voice_thread`, пока сессия mid-(re)connect.

### B44 — REALTIME: реконнект обнуляет живой LLM-контекст диспетчера — лента полная, но диспетчер «забыл» всё до обрыва — MAJOR — open
- class: continuity loss (лента ≠ память диспетчера) · location: `app.py:832` (`context = LLMContext(tools=ALL_SCHEMAS)` — единственная точка), `webrtc_server.py:145` (`build_session_pipeline` заново на КАЖДЫЙ reconnect), контраст `dispatcher/loop.py:83-108` (`_history_for` HTTP-путь ДЕЛАЕТ регидрацию из ленты)
- symptom: каждый reconnect (авто после блипа / ручной перезвон в тот же тред) получает свежий ПУСТОЙ `LLMContext`; системный промпт несёт только статичные правила + `stage_block`, НЕ прошлые ходы/`request_text`. Диспетчер переспрашивает то, на что уже есть ответы — хотя лента треда (пишется независимо из `_on_end_of_turn`/`_flush_voice_context`) показывает весь прошлый обмен.
- expected vs actual: reconnect продолжает разговор, диспетчер помнит обсуждённое · actual: видимое противоречие — экран показывает историю, диспетчер ведёт себя как амнезиак. Транскрипт не потерян, «бесшовность» — да.
- fix sketch: seed свежего `context` из `host.threads` feed (регидрация user/assistant, как `_history_for` на HTTP), либо переиспользовать тёплую `text_loop`-историю треда. NO-EXFIL: только слова user/диспетчера, кора-виды в контекст не идут.

### B45 — тап по заголовку треда (rename) сносит `#view-title` из DOM → следующий render кидает → ВСЕ обновления title/badge/стадии замирают до перезагрузки — CRIT — open
- class: crash + перманентная регрессия render-цикла · location: `app.js:723-734` (`renameCurrentThread`: `titleEl.replaceWith(input)` — узел с id снят), `app.js:698-703` (`commitRename`: `$("view-title")` → null → `titleEl.textContent=` TypeError), `index.html:72` (id на самом div, `el("input")` его не восстанавливает)
- symptom: юзер тапнул заголовок треда, ввёл имя, blur/Enter → `commitRename` первым делом `titleEl.textContent=` на null → TypeError ДО `try/finally` → `renaming` навсегда `true`; `#view-title` не восстановлен → каждый последующий `render()` (`app.js:196`) и `loadLists()` (`app.js:400`) тоже кидают на том же null — а они бегут на каждый hashchange и каждые 5с. Итог: один тап по заголовку намертво морозит title/badge/стадию до полной перезагрузки.
- repro: открыть тред → тапнуть заголовок → Enter. Тривиально достижимо.

### B46 — write_code stale-plan гвард переоткрыт: завершение НЕсвязанной direct-dispatch задачи ставит `last_outcome="completed"` → Кора кодит по НЕвалидированному плану (реопен B07) — CRIT — open
- class: data-integrity/safety · location: `app.py:285-297` (`_run_finished`: `set_outcome(thread_id, outcome)` безусловно), `kora.py:511-520` (колбэк `on_run_finished(thread_id, outcome)` — БЕЗ task_id/gate_mode/стадии), `app.py:354-376` (`write_code`: единственный freshness-сигнал `last_outcome=="completed"`), `app.py:317-331` (`revise` сбрасывает outcome=None — это и был фикс B07)
- repro: (1) тред T: propose→send_to_kora docs_only completes → `docs/plans/T.md` под запрос A, last_outcome=completed. (2) revise → stage=collect, last_outcome=None (B07), но T.md НЕ удалён. (3) тот же тред, `submit_task` на НЕсвязанную мелочь (ничто не стадия-гейтит его — см. B47) → completes → `_run_finished(T,"completed")` → last_outcome снова "completed". (4) propose(B) → `write_code(confirm)`: plan_path.exists()=True (стейл T.md запроса A), `last_outcome!="completed"`=False → гвард пропускает → Кора запускается «Реализуй по плану docs/plans/T.md, запрос: B» по ЧУЖОМУ плану, без единого свежего spec_plan для B.
- fix sketch: `on_run_finished` должен нести task_id/стадию; `_run_finished` ставит last_outcome="completed" ТОЛЬКО для gate-launched spec_plan-рана этого треда, не для любой завершённой задачи.

### B47 — direct-dispatch (`submit_task`/`confirm_task`) НЕ двигает `thread.stage` → завершённая задача навсегда с бейджем «СБОР» (наблюдалось живьём) — MAJOR — open
- class: FSM/UI inconsistency · location: `app.py:613-626` (`_on_task_committed` — нет set_stage), `app.py:662-669` (`_http_task_committed` — нет set_stage), `app.py:285-297` (`_run_finished`: единственный set_stage(«done») только если stage уже «code»)
- symptom: наблюдал живьём — «создай helloworld.txt» ушло через прямой send_to_kora диспетчера, задача COMPLETED, файл записан, а тред остался stage="collect" (бейдж «СБОР»). `last_outcome` читается "completed" при stage="collect" бесконечно.
- note: структурно, не by-design; промпт (`prompt.py:36`) рекламирует `submit_task` без стадийного квалификатора, стадия-правила навешиваются только на collect/propose. Родня B46 (тот же слепой канал).

### B48 — архивный тред остаётся полностью запускаемым/мутируемым: `archived` не проверяется ни в одном write-пути — MAJOR — open
- class: illegal state / zombie thread · location: `app.py` (`gate_action` 299-377, `_propose_for` 530-554, `_on_task_committed` 613-626, `_http_task_committed` 662-669) — никто не читает `th.archived`; `threads.py` (`append_task` 110-117, `append_feed` 244-247) — тоже
- symptom: `gate_action(id,"send_to_kora",confirm)` (голос или POST /gate) на архивном треде идёт как на живом (единственный гвард — глобальный busy + per-thread lock); `propose_request` двигает архивный collect→propose; оба commit-пути аппендят задачи/пускают Кору в него. Стейл `voice_thread["id"]` на архивный тред → он копит задачи/ленту и гоняет Кору после «уборки».
- fix sketch: read-гвард `th.archived` в gate_action/propose/commit-путях → отказ (или тихий no-op с фидбеком).

### B49 — архив треда разрешён, пока задача в `PENDING_CONFIRMATION` (busy-чек ловит только RUNNING) — MAJOR — open
- class: illegal transition · location: `webrtc_server.py:612-628` (`api_thread_archive`: `task.status == TaskStatus.RUNNING` only), `state.py:186-190` (`has_active_task` канонично включает PENDING_CONFIRMATION)
- repro: деструктивный ask на T → `ConfirmFlow.submit` → task=PENDING_CONFIRMATION → POST /threads/T/archive: RUNNING-чек False → архив проходит. Юзер говорит «да» → confirm_task → RUNNING → append_task в уже-архивный T + запуск Коры. «Ушедший» тред принимает и гоняет задачу.
- fix sketch: archive busy-чек = `has_active_task()` (RUNNING ∪ PENDING_CONFIRMATION), как везде.

### B50 — `/api/browse?path=%00` (null-байт) → 500 unhandled на неаутентифицированном роуте — MAJOR — open
- class: crash / input validation · location: `webrtc_server.py:44-47` (`_browse_dir`: `p.resolve()` ловит только OSError/RuntimeError; null-байт даёт `ValueError: embedded null character` мимо гарда)
- repro (live, TestClient): `GET /api/browse?path=%00` → 500. Любой вызыватель роняет эндпоинт.
- fix sketch: расширить except на ValueError → fallback-to-home (как для любого нерезолвимого пути) или 400.

### B51 — `confirm`/`fast` строка-vs-bool: `bool("false")==True` → `confirm:"false"` запускает НЕподтверждённый full-write Kora-ран — MAJOR — open
- class: confirmation-gate bypass (данные/безопасность) · location: `webrtc_server.py:663-664` (`confirm=bool(data.get("confirm"))`, `fast=bool(...)`), достигает `app.py:332-353` (confirm гейтит запуск, fast переключает docs_only→code/full write)
- repro (live): POST /gate `{"action":"send_to_kora","confirm":"false","fast":"false"}` → сервер получает confirm=True, fast=True → 200, запуск full-write. Штатный клиент шлёт настоящие bool'ы (не достижимо через app.js), но любой прямой/кривой вызыватель ломает гейт подтверждения.
- fix sketch: строгая интерпретация — `data.get("confirm") is True` или явный парс bool из JSON-типа, не `bool(str)`.

### B52 — gate-карточка навсегда disabled после 409 «busy», нет пути ретрая — MAJOR — open
- class: workflow dead-end · location: `app.js:529-548` (кнопки force-disabled синхронно ДО fetch; на 409 `finally` явно НЕ ре-энейблит, т.к. `note.textContent==="Кора занята — ждёт"`; gate-карточки рисуются один раз, поллингом не перерисовываются)
- symptom: Кора на миг занята → 409 → карточка мертва навсегда, юзер не может повторить, пока сервер не выпустит НОВЫЙ gate_card (если выпустит).

### B53 — успешный gate-экшен оставляет note «запускаю…» и ре-энейблит те же кнопки → приглашение к дублю; `live` захвачен один раз, устаревает — MAJOR — open
- class: duplicate action / no success feedback · location: `app.js:478-482,530,541-548` (`live` вычислен один раз на рендере, не ревалидируется; на успехе note не меняется с «запускаю…», `finally` ре-энейблит кнопки по стейл `live`)
- symptom: после «Пиши код» карточка всё ещё «запускаю…» и снова кликабельна; стейл-карточка может выстрелить экшен по стадии, которой уже нет.

### B54 — архив треда, который открыт → страница молча деградирует в пустую, ноль фидбека — MAJOR — open
- class: silent failure / no empty-state · location: `app.js:195-196` (`render`: `t?t.title:"тред"` — нет error-UI когда t undefined), `app.js:563-571` (`pollFeed` `catch{return}` — 404 на архивном треде глотается вечно; в отличие от `loadLists`, у pollFeed нет `setConn(LOAD_ERR)`)
- repro: открыть тред → «архив» на его же карточке в сайдбаре → `location.hash` всё ещё на нём → render даёт generic «тред», pollFeed вечно 404-ит молча. Юзер смотрит в статичную пустую страницу без единого намёка.

### B55 — неизвестный/стейл тред-роут → пустая вью + бесконечный 404-поллинг ленты, нет «не найдено» — MINOR — open
- class: silent failure · location: `app.js:563-571` (`pollFeed` `catch{return}` не останавливает интервал), нет not-found-состояния для неизвестного id
- repro (live, я кликал): navigate `#/thread/<garbage>` → 7+ одинаковых 404 `/feed?limit=500` подряд, POST `/api/active-thread` с битым id → 404, юзер видит пустой тред без «не найдено».

### B56 — `GET /api/threads?archived=false` трактуется truthy → прячет реальный список тредов — MINOR — open
- class: silent data-hiding · location: `webrtc_server.py:511` (`if archived and archived != "0"` — только "0"/пусто = off; "false"/"no" → ветка archived-only)
- repro (live): `?archived=false` → `{"threads":[]}`, без параметра → реальный список. Штатный app.js не шлёт, но контракт API нарушен.

### B57 — `feed?limit=0` возвращает ВСЮ ленту (и `limit=-5` — левый срез) — MINOR — open
- class: off-by-semantics · location: `webrtc_server.py:538` (`limit:int=200`) → `threads.py:266` (`.splitlines()[-limit:]`; `lst[-0:]==lst[0:]` → всё)
- repro (live): `?limit=0` при 2 записях → обе записи. Негативный limit даёт несвязанный срез.

### B58 — `POST /api/active-thread {"id":""}` → 404 вместо очистки активного треда — MINOR — open
- class: contract inconsistency · location: `webrtc_server.py:685,687` (гвард исключает только None; строка 687 трактует falsy как clear, но 685 404-ит на "" как на несуществующем id)
- repro (live): `{"id":""}` → 404. Штатный клиент шлёт null/реальный id.

### Awareness (вне линз, не в счёте 16): CSRF-зазор на `POST /api/offer` (`webrtc_server.py:311`) и `POST /start` (263) — без CSRF, cross-origin достижимы; `/api/offer` вытесняет единственную живую сессию. Комментарий файла зовёт их «unused by prebuilt, kept curl-testable» — принятый/известный зазор WebRTC-сигналинга, не CodeFlow-API. Флаг для осознанности.
