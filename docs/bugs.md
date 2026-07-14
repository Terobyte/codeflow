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
