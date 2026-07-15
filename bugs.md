# bugs.md (Реестр найденных ошибок и зон внимания)

Severity: **CRIT** = money/data loss · security · crash · **MAJOR** = wrong behaviour on real input · **MINOR** = edge degradation.
Status: `reported` → `proven` | `rejected(reason)` | `not-test-verifiable(reason + manual cmd)`; `proven` → `fixed(commit)` | `parked(why)`.

---

## 🎯 Распределение по зонам внимания (Scope Division)
Для распараллеливания работы агентов в будущих багхантах проект разбит на следующие изолированные зоны:

1. **Frontend & Client UI** (Префикс: `B-UX-*`)
   - **Область:** `synapse/pipeline/client/`, `synapse/pipeline/static/`
   - **Описание:** SPA-интерфейс, JS-роутер, стили, доступность (a11y), состояние реалтайм оверлея и статус-виджеты.

2. **WebRTC & Pipeline Server** (Префикс: `B-PIPE-*`)
   - **Область:** `synapse/pipeline/app.py`, `synapse/pipeline/webrtc_server.py`, `synapse/pipeline/arbiter.py`, `synapse/pipeline/context_guard.py`
   - **Описание:** Сигналинг WebRTC, агрегаторы контекста, инжекция TTS/STT, кеширование и серверный пайплайн.

3. **Bridge & Kora State** (Префикс: `B-BRIDGE-*`)
   - **Область:** `synapse/bridge/`, `synapse/projects.py`, `synapse/threads.py`
   - **Описание:** Запуск KoraRunner, гейт прав доступа (containment), подтверждение опасных действий (approvals) и персистентность.

4. **Dispatcher & Tools** (Префикс: `B-DISP-*`)
   - **Область:** `synapse/dispatcher/`
   - **Описание:** Главный цикл разбора реплик (DispatcherTurnLoop), биндинг инструментов, mock-LLM и классификаторы.

5. **Cascade & Strategy** (Префикс: `B-CASC-*`)
   - **Область:** `synapse/cascade/`
   - **Описание:** Стратегии переключения LLM (Switcher), CircuitBreaker для API, CostCap лимиты.

6. **Core & CLI Runners** (Префикс: `B-CORE-*`)
   - **Область:** `synapse/runners/`, `synapse/config.py`, `synapse/journal.py`, `synapse/prompt.py`
   - **Описание:** Глобальные конфиги, TurnJournal логирование, CLI скрипты записи команд.

---

## 💻 1. Frontend & Client UI (`B-UX-*`)
*В этой секции собраны ошибки интерфейса пользователя.*

### B-UX-1 — watchdog auto-reconnect races the mic button → orphaned zombie voice session — MAJOR — fixed(worktree)
- class: concurrency/lifecycle · location: `synapse/pipeline/client/app.js:1078-1098` · found-by: H-A
- symptom: two `connectVoice()` run concurrently; one `PipecatClient` becomes an orphan whose WebRTC session stays live server-side with no client ref to close it — mic stays open with no UI indication.
- trigger: watchdog hits zombie-recovery (3 misses ≈15s "no session", line 1076) → `client = null` (1083) → `await c.disconnect()` (1085, suspends) → user taps `#mic-btn` in that window: `if (connecting) return` (1020) passes because `connecting` is still `false`, `if (client)` (1024) is false because `client` was already nulled → mic handler starts its own `connectVoice()`. probeSession then resumes, sets `connecting = true` (1081, too late) and calls `connectVoice()` (1088).
- expected vs actual: the disconnect+reconnect sequence must be atomic w.r.t. other connect attempts (`connecting = true` before `client = null`/await) → only one live session · actual: guard flag set one `await` after the null-out, leaving a race window → two sessions, one leaked.
- evidence: ordering at 1081-1085 (`connecting = true;` / `const c = client;` / `client = null;` / `await c.disconnect()…`) — suspension point between null-out and guard.

### B-UX-2 — Enter key bypasses the send-disable guard → double thread / double message — MAJOR — fixed(worktree)
- class: concurrency (composer) · location: `synapse/pipeline/client/app.js:835-843` · found-by: H-A
- symptom: two fast Enter presses fire `sendMessage()` twice concurrently. Home view → two `POST /api/threads` create **two threads** with the same text; thread view → double `POST …/message` → duplicate feed entries and two LLM turns for one intent.
- trigger: type text, press Enter twice before the first `await postJSON(...)` resolves.
- expected vs actual: `$("msg-send").disabled = true` is meant to block re-submit · actual: `disabled` only suppresses the button's `click`; the keydown listener calls `sendMessage()` directly with no `disabled`/flag check, and `input.value` isn't cleared until success so both invocations read the same text.
- evidence: 835-843 (`if (e.key === "Enter" && !e.isComposing && e.keyCode !== 229) sendMessage();` — no guard) vs button disable state. Server has no dedup: `_launch_run`/`api_threads_create` create unconditionally per call.

### B-UX-3 — `gate_card` feed entry renders as a bare "· " — structured run-start card lost — MAJOR — fixed(worktree)
- class: rendering · location: `synapse/pipeline/client/app.js:602` + `synapse/pipeline/app.py:640-642` · found-by: H-B
- symptom: every run start (gate `send_to_kora` / `write_code`, happy path) appends a feed entry the client renders as literally `"· "` — no stage, model, or indication a run began.
- trigger: open a thread, confirm "Отправить Коре" / "Написать код". 100% reproducible on the main path.
- expected vs actual: a readable run-started card ("запуск: code · модель …") from the entry's `stage`/`action`/`model` fields · actual: server emits `{kind:"gate_card", stage, action, model}` with **no `text`** (app.py); `addEntry` has no `gate_card` branch → falls to else: `KIND_ICONS["gate_card"]` undefined → `"·"`, `e.text` undefined → `""` → `"· "`. No `.feed-gate_card` CSS rule either.
- evidence: app.py (no `text` key); app.js (KIND_ICONS lacks gate_card), fallback.
- related: `kind:"event"` ("правки → сбор") hits the same fallback — renders text but unstyled (no `.feed-event`). Lesser; fold into the fix.

### B-UX-4 — `feedKey` collision drops one of two parallel tool results from the thread feed — MAJOR — fixed(worktree)
- class: rendering/dedup (data-loss) · location: `synapse/pipeline/client/app.js:165,729-735` + `synapse/bridge/kora.py:344-346,362-368` + `synapse/pipeline/app.py:363-370` · found-by: H-B
- symptom: two genuinely distinct feed entries with the same `ts`+`kind`+`text` collapse to one client key; the second is silently skipped and never rendered.
- trigger: Kora returns two (or more) tool results in one `UserMessage` (parallel `Read`/`Bash` — common agentic pattern), both success (or both error).
- expected vs actual: both entries visible · actual: `kora.py` stamps one `ts` per SDK message; `_message_to_log_entries` gives every block that shared `ts`; ToolResultBlock text is coarse `"ок"`/`"ошибка"` → two identical `{ts,"tool_result","ок"}`. `_kora_log_sink` mirrors them into the **thread** feed, where `pollFeed`'s `renderedKeys.has(feedKey(e))` drops the duplicate.
- evidence: app.js (`feedKey = ts|kind|text`), dedup `continue`; kora.py (single `ts`), coarse text.
- note: harm bounded (lost entry is a low-value duplicate string), but feed fidelity is broken; same root also collapses any identical (kind,text) blocks in one message.

### B-UX-5 — `loadLists()` has no in-flight guard → stale poll stomps fresher data — MINOR — fixed(worktree)
- class: concurrency (poller race) · location: `synapse/pipeline/client/app.js:466-491` · found-by: H-A
- symptom: a slow earlier `loadLists()` resolving after a faster later one overwrites `threads`/`projects` with the older snapshot; a just-created thread transiently vanishes from sidebar/home until the next poll.
- trigger: `setInterval(loadLists, 5000)` starts a fetch (up to 15s, FETCH_TIMEOUT_MS); before it resolves, `sendMessage`'s `loadLists()` resolves first and renders the new thread; the interval call then resolves and re-renders the pre-creation list.
- expected vs actual: state should reflect the latest server data (as `pollFeed`'s `feedInFlight` / `browse`'s `latestBrowse` already do) · actual: no in-flight flag or sequence token; last-to-resolve wins.
- evidence: loadLists lacks any guard.

### B-UX-6 — `route()` throws `URIError` on a malformed hash → render loop crashes each tick — MINOR — fixed(worktree)
- class: correctness/edge · location: `synapse/pipeline/client/app.js:113-121` · found-by: H-B
- symptom: `decodeURIComponent` on an invalid percent-escape throws uncaught inside `route()`, called by `render`/`pollFeed`/`threadCard`/`renderSidebar`/`renderHome`/`sendMessage` — none wrap it.
- trigger: navigate to `<origin>/#/thread/%E0` (corrupted/shared/hand-edited link).
- expected vs actual: graceful fallback (treat as unknown thread / home) · actual: `render()` throws on init and on every `hashchange`/interval tick while the hash stays malformed.
- evidence: `decodeURIComponent(m[1])` with no try/catch.

### B-UX-7 — picker folder rows unreachable by keyboard/AT — MINOR — fixed(worktree)
- class: a11y · location: `synapse/pipeline/client/app.js:1178-1189` · found-by: H-B
- symptom/expected/actual: the "выбор папки проекта" up-folder and every subfolder are plain `<li>` with a `click` listener only — no `tabindex`/`role`/keydown; can't be tabbed to or Enter/Space-activated, so the add-project flow can't be completed without a pointer.
- evidence: `el("li", …)` + `addEventListener("click", …)`, no keyboard affordance anywhere for these rows.

### B-UX-8 — picker dialog claims `aria-modal` but has zero focus management — MINOR — fixed(worktree)
- class: a11y · location: `synapse/pipeline/client/index.html:69` + `app.js:1154-1164` · found-by: H-B
- symptom/expected/actual: `role="dialog" aria-modal="true"` tells AT the rest is inert, but `openPicker`/`closePicker` never move focus into the dialog, never trap Tab, never restore focus on close; siblings aren't `inert`/`aria-hidden`.
- evidence: index.html (aria-modal); app.js (no `.focus()` / trap anywhere).

### B-UX-9 — Kora status dot is a mouse-only control — MINOR — fixed(worktree)
- class: a11y · location: `synapse/pipeline/static/status-widget.js:26-34` · found-by: H-B
- symptom/expected/actual: injected into `/client/dev` (prebuilt fallback) as the only affordance to reach `/client/logs`, but it's a bare `<div>` with a `click` listener — no `tabindex`/`role`/keydown (unlike the SPA's `#kora-card`, a real `<a>`). Not focusable, Enter/Space do nothing.
- evidence: `createElement("div")` + click only.

### B-UX-10 — mobile drawer: no focus trap; off-canvas controls stay in tab order when closed — MINOR — fixed(worktree)
- class: a11y · location: `synapse/pipeline/client/style.css:220-225` + `app.js:1112-1117` + `index.html:18-39` · found-by: H-B
- symptom/expected/actual: the drawer is hidden only via `transform: translateX(-102%)`, which doesn't remove it from the tab order/AT tree; `<aside>` precedes `<main>`, so a keyboard user lands on invisible off-screen controls first. When open, Tab isn't trapped — focus escapes into the backdrop-covered `<main>` (not `inert`/`aria-hidden`).
- evidence: style.css (transform only); app.js (no `inert`/`tabindex`/`aria-hidden` toggling).

---

## 📡 2. WebRTC & Pipeline Server (`B-PIPE-*`)
*В этой секции фиксируются ошибки WebRTC сигналинга, ASGI/HTTP роутов и сборки звуковых пайплайнов.*

### B-PIPE-1 — _run_finished stage transition failure silently swallowed after outcome write — MAJOR — fixed(worktree, UNVERIFIED: test crashes in its own setup (NameError) — fix unverified)
- class: silent failure · location: `synapse/pipeline/app.py:343-346` · found-by: H-PIPE
- symptom: when `_run_finished` attempts a stage transition (e.g., `code` → `done`) that fails due to a race, the ValueError is silently swallowed with `pass`. Thread's `last_outcome` was already written (line 336), but stage transition didn't happen — inconsistent state where outcome says "completed" but stage is still "code".
- trigger: two concurrent operations: (1) `_run_finished` calls `set_stage(thread_id, "done")` after completed code run, (2) another operation (user clicking "revise" via gate_action) concurrently changes stage to "collect". ValueError from illegal transition is caught and discarded.
- expected vs actual: either transition succeeds OR outcome write is rolled back if transition fails · actual: outcome written but stage transition silently fails, thread state inconsistent.
- evidence: lines 343-346 show `try: self.threads.set_stage(thread_id, target) except ValueError: pass` AFTER `self.threads.set_outcome(thread_id, outcome)` on line 336. Comment says "race: stage already changed — silent no-op" but this is not atomic with outcome write.

### B-PIPE-2 — kora_runner.start() failure after state mutations leaves zombie run — CRIT — fixed(worktree)
- class: silent failure · location: `synapse/pipeline/app.py:468-481` · found-by: H-PIPE
- symptom: `_launch_run` performs multiple state mutations (set_stage, start_task, append_task, set_last_model) then calls `kora_runner.start()`. If `kora_runner.start()` raises, all prior state mutations have succeeded but actual Kora run never started. Caller (`gate_action`) has already returned `{"ok": True, "stage": ...}` to client.
- trigger: (1) user clicks "send to kora" in UI, (2) `gate_action` → `_launch_run` executes successfully through line 476, (3) `kora_runner.start()` on line 474 raises (SDK init failure, invalid RunSpec, filesystem permission error creating workspace), (4) exception propagates to webrtc route handler as 500.
- expected vs actual: either all state changes succeed AND Kora starts, OR no state changes happen (atomic) · actual: thread stage changed to "spec_plan", task marked RUNNING in store, task appended to thread, model recorded — but no Kora run exists. UI shows "running" but nothing running. Watchdog will eventually fire KORA_UNREACHABLE, but user has no immediate feedback that launch failed.
- evidence: lines 468-477 show state mutations before `kora_runner.start()`, no try/except around start(), all mutations happen BEFORE actual work.
- valid finding; the launch saga (`begin_run`/`begin_task` + guarded rollback) is sound and reviewed. But the same changeset introduced `finish_run` and rewired `_run_finished` onto it, and that part shipped a silent data-loss regression — see B-PIPE-2a below. The saga itself is untouched by that fix.

### B-PIPE-2a — finish_run CAS freezes last_outcome on the first task of a thread — MAJOR — fixed(2026-07-15)
- class: atomicity/regression (introduced by B-PIPE-2's changeset, not present before) · location: `synapse/pipeline/app.py:348-354` · found-by: critic sweep over the uncommitted diff
- symptom: on a pure direct-dispatch thread, only the FIRST task's outcome is ever recorded. Every later task — success or failure — is dropped silently, and the UI outcome badge (`client/app.js:327,338,353`, gated on `stage === "done"`) reports task #1's result for the rest of the thread's life.
- trigger: (1) submit a direct-dispatch task in a thread, it completes → `finish_run(expected_stage="collect", completed_stage="done")` writes `last_outcome="completed"` and moves the thread collect→done, (2) submit a second task in the SAME thread (the voice/HTTP channels reuse `voice_thread`/`current_http_thread` and never reset the stage), (3) it FAILS, (4) `_run_finished` calls `finish_run(expected_stage="collect")`, (5) thread sits at `"done"` → CAS mismatch → `return False`, nothing written, nothing logged. `"done"` is terminal in `_STAGE_TRANSITIONS` (no outgoing edge), so the CAS can never match again.
- expected vs actual: `last_outcome` reflects the most recent run (old code called `set_outcome` unconditionally) · actual: frozen on task #1 forever.
- root cause: the refactor merged two operations with DIFFERENT preconditions into one compare-and-set. The outcome write was unconditional; only the B47 collect→done transition was stage-gated. Merging them under the transition's precondition made the outcome write inherit a guard it never had. Atomicity is only sound for operations that share a precondition.
- fixed 2026-07-15: guard against the CURRENT stage (`expected_stage=th.stage`) and gate only the transition — mirroring what the same changeset's own task_id-less branch already does two lines above. Pinned by `tests/test_bugs_0714_realtime.py::test_second_direct_dispatch_outcome_is_not_dropped` (verified red against the defect, on the target assertion). The stale-run generation guard that motivated the CAS survives for gate runs, which do set a stage at launch and therefore have a real generation token.

### B-PIPE-3 — monitor_forever exception handler continues silently, heartbeat checks skipped indefinitely — MAJOR — reported
- class: silent failure · location: `synapse/pipeline/app.py:281-297` · found-by: H-PIPE
- symptom: `monitor_forever` loop catches all non-CancelledError exceptions and logs with `logger.exception`, then continues next iteration. If exception is transient (e.g., `os.fsync` failure during `journal.alert`), correct. But if persistent (e.g., `self.store` in corrupted state and `store.liveness()` raises every time), loop log-spams forever and never actually performs heartbeat checks.
- trigger: (1) TaskStore enters bad state (internal invariant violated, filesystem unavailable), (2) `store.liveness()` on line 285 raises on every iteration, (3) exception caught, logged, loop continues, (4) CRITICAL_WITHOUT_SPEAK checks (283-284) and KORA_UNREACHABLE alerts (285-291) never executed.
- expected vs actual: persistent failures should either halt monitor (fail-stop) or escalate to higher supervisor · actual: loop continues indefinitely, logging same exception every `heartbeat_interval_s`, while watchdog checks silently skipped.
- evidence: lines 281-297 show broad `except Exception` on line 296 catches ALL errors in block (282-293) including from `speak_ledger.check()`, `store.liveness()`, `journal.alert()`, `cost_cap.maybe_reset()`. Persistent failure in any makes entire monitor ineffective.

### B-PIPE-4 — TTSCacheObserver exception handler swallows all cache write failures — MAJOR — reported
- class: silent failure · location: `synapse/pipeline/tts_cache.py:152-158` · found-by: H-PIPE
- symptom: `TTSCacheObserver.on_push_frame` wraps all cache logic in broad `except Exception` that logs and ignores ALL errors. If cache writes fail persistently (disk full, permissions error, filesystem corruption), frames silently dropped from cache and system continues as if fine. Users experience cache misses on every play, triggering expensive REST TTS synthesis, but no alert raised.
- trigger: (1) disk fills or cache directory permissions change, (2) `cache.put_pcm()` on line 201 (called from `_finalize`) raises `OSError`, (3) exception caught in `on_push_frame`, logged once, ignored, (4) every subsequent TTS frame fails to cache, all future plays require REST synthesis.
- expected vs actual: persistent cache write failures should either alert (journal.alert) OR disable cache gracefully · actual: errors logged but observer continues silently failing, degrading performance with no visibility.
- evidence: lines 152-158 show try/except around `_handle()`. Comment lines 153-154 justifies catching to avoid crashing audio pipeline, but doesn't address that persistent failures should surface as alerts rather than silent degradation.

### B-PIPE-5 — run_session finally block cleanup racing with state assignment leaves stale current/bind — MAJOR — reported
- class: silent failure · location: `synapse/pipeline/webrtc_server.py:223-254` · found-by: H-PIPE
- symptom: `finally` block acquires `lock` to clean up `current["task"]` and `current["session_id"]`, but race window exists: if new connection preempts old one AFTER old task enters finally but BEFORE old task acquires lock, new task has already published itself as `current["task"]`. When old task's finally runs, it checks `if current["task"] is task` (line 238) — check fails (it's new task), so old task takes `else` branch and checks session IDs. If new connection reused same session_id (unlikely but structurally possible via `active_sessions` dict), old task pops it from `active_sessions` on line 247, breaking new connection's session.
- trigger: (1) Connection A starts `run_session` with `session_id="abc"`, (2) A hits error, enters finally (line 223), (3) before A acquires `lock` (line 237), Connection B starts with SAME `session_id="abc"` (reused from `active_sessions`), (4) B acquires `lock`, sets `current["task"]` to B's task, publishes `current["session_id"]="abc"`, (5) A now acquires `lock`, sees `current["task"] is not task` (line 238), takes else branch, (6) lines 246-247 conditional logic can pop B's session_id from `active_sessions`, breaking B.
- expected vs actual: finally cleanup only affects session/task it owns · actual: racy cleanup can corrupt session state of new connection.
- evidence: lines 236-247 show logic attempting to handle preemption, but comment reveals assumption: "our session_id is no longer active" — not necessarily true, new task might have inherited/reused session_id.

### B-PIPE-6 — _browse_dir null-byte ValueError silently falls back to home, attacker can probe filesystem — MINOR — reported
- class: silent failure · location: `synapse/pipeline/webrtc_server.py:44-64` · found-by: H-PIPE
- symptom: `_browse_dir` validates paths and returns None for unreadable directories. But line 55's broad exception handler catches `ValueError` (from null bytes in path), `OSError`, and `RuntimeError`, silently falling back to `home` directory. Attacker can provide paths like `"/etc\x00/passwd"` and receive successful listing of home directory instead of error, masking that path validation failed.
- trigger: (1) user provides path with null byte: `GET /api/browse?path=/etc%00/passwd`, (2) `Path(raw)` on line 53 raises `ValueError`, (3) exception caught on line 55, `rp` set to `base` (home directory), (4) function returns successful listing of home directory.
- expected vs actual: invalid path (null byte) should return `{"error": "invalid"}` or similar · actual: falls back to home directory listing, hiding validation failure.
- evidence: lines 50-56 show comment B50 documents this behavior, but fallback is wrong for VALIDATION errors (ValueError from null byte) vs RESOLUTION errors (OSError from non-existent path). Null bytes should be rejected, not silently accepted as "home".

---

## 🕳 3. Bridge & Kora State (`B-BRIDGE-*`)
*Ошибки, связанные с запуском KoraRunner, разграничением прав файлового гейта и состоянием выполнения задач.*

### B-BRIDGE-1 — TaskStore._persist race: concurrent writes lost or corrupted — MAJOR — fixed(worktree)
- class: concurrency/lifecycle · location: `synapse/bridge/state.py:405-417` · found-by: H-BRIDGE
- symptom: lost writes or corrupted state.json when concurrent tool handlers or Kora events call TaskStore mutation methods simultaneously. Two threads calling `apply_event()` and `set_task_status()` at same moment can interleave their tmp-file writes, causing one write to overwrite other's data mid-flight.
- trigger: (1) voice channel: Kora stream event arrives (`apply_event` called from `_stream` loop line 553 in kora.py), (2) simultaneously: HTTP request calls `request_cancel` → `set_task_status` (state.py:562), (3) both read `self._task`, both serialize to tmp, second `tmp.replace()` wins and first write lost.
- expected vs actual: all mutations serialize correctly, state.json reflects both event append AND status change · actual: second write clobbers first; either event missing from `task.events` OR status flip lost (depending which write won).
- evidence: state.py:405-417 shows `_persist()` has NO lock, just tmp+replace. Line 260 `apply_event()` calls `_persist()`, line 236 `set_task_status()` calls `_persist()`. kora.py:553 async stream loop calls `apply_event_to_store` → `store.apply_event`, line 562 `_terminalize_if_running` calls `store.set_task_status(TaskStatus.FAILED)`. Projects.py has `asyncio.Lock` around `_persist` (line 108, 117); TaskStore does NOT.

### B-BRIDGE-2 — KoraRunner.provide_answer race: InvalidStateError on cancelled future — MAJOR — reported
- class: concurrency/lifecycle · location: `synapse/bridge/kora.py:474-485` · found-by: H-BRIDGE
- symptom: race between `provide_answer` checking `fut.done()` and setting result. If parked `_handle_question` future is cancelled externally (task superseded) between `if fut is not None` check and `fut.set_result(text)`, answer call raises `InvalidStateError` and propagates to dispatcher tool handler, marking legitimate answer as failed.
- trigger: (1) Kora parked on AskUserQuestion, `_pending_answer` holds future F, (2) user replies, dispatcher calls `provide_answer(text)`, (3) concurrently: second task submission cancels `self._active` (line 459), which cancels F, (4) `provide_answer` passes `fut is not None and not fut.done()` check (F not cancelled YET), (5) F gets cancelled AFTER check but BEFORE `fut.set_result(text)` (line 483), (6) `set_result` on cancelled future raises `InvalidStateError`.
- expected vs actual: `provide_answer` returns False gracefully when future cancelled/done; user can retry · actual: uncaught exception propagates from `answer_kora` tool handler, entire turn fails.
- evidence: kora.py:474-485 shows `provide_answer` checks `not fut.done()` but doesn't catch `InvalidStateError` from `set_result`. Line 459 `start()` cancels `self._active` if not done → propagates into `_stream`'s `async for`. Line 829 `_handle_question` awaits `fut` with no timeout; cancellation bubbles. asyncio.Future.set_result raises InvalidStateError if future cancelled/done.

### B-BRIDGE-3 — apply_event race: second event lost when first's _persist in flight — CRIT — fixed(worktree)
- class: concurrency/lifecycle · location: `synapse/bridge/state.py:260-277`, `synapse/bridge/kora.py:540-553` · found-by: H-BRIDGE
- symptom: `apply_event` mutates `self._task.events` list and `self._task.last_event_ts` atomically per event, but `_persist()` serializes ENTIRE `self._task` object. Second event arriving AFTER first event's `events.append` but BEFORE its `_persist()` completes can see partially-updated task (event 1 appended, event 2 appended, but event 1's `_persist` hasn't written yet) — when event 1's delayed `_persist` fires, it writes state.json WITHOUT event 2 (which it never saw in its pre-call snapshot). On process restart, event 2 missing from `task.events`.
- trigger: (1) Kora stream emits event E1 at ts=100, `apply_event` appends E1 to `task.events`, calls `_persist()`, (2) `_persist()` reads `self._task` (includes E1), starts `json.dumps(data, ...)` (slow for large event lists), (3) before step 1's `tmp.replace()` completes, event E2 arrives at ts=101, (4) `apply_event` for E2 appends E2 to SAME `task.events` list, calls `_persist()`, (5) E2's `_persist` serializes `task` (now has E1+E2), writes tmp, calls `tmp.replace(state_path)`, (6) E1's `_persist` (still in flight from step 2) finishes its write, calls `tmp.replace(state_path)` AFTER E2, (7) E1's write (only knew about E1) clobbers E2's write, (8) state.json on disk has E1 but not E2; on restart E2 gone.
- expected vs actual: both events appear in persisted state.json in order · actual: second event lost; state.json only has first event.
- evidence: state.py:272 shows `self._task.events.append(event)` mutates shared list. Line 277 `self._persist()` called AFTER mutation, no lock. Lines 409-416 `_persist` serializes full `_task_to_dict(self._task)` — sees snapshot at call time. kora.py:552-553 async for loop calls `apply_event_to_store` for every message; no await between loop iterations means two messages can fire apply_event concurrently if async scheduler interleaves. No lock guards `_task` object or `_persist` call.

### B-BRIDGE-4 — ThreadStore.append_feed race: lost entries or corrupted ring-buffer rewrite — MINOR — fixed(worktree; incidental to persistence boundary)
- class: concurrency/lifecycle · location: `synapse/threads.py:246-261` · found-by: H-BRIDGE
- symptom: lost feed entries or corrupted ring-buffer rewrite when two concurrent `append_feed` calls race on same thread_id. Both read `self._feed_counts[thread_id]`, both increment it, both may trigger `n > self._feed_max * 1.2` rewrite at same time, and one rewrite clobbers other's tmp file.
- trigger: (1) voice channel appends entry A to thread T's feed (line 249), reads count=2399, (2) HTTP channel appends entry B to same thread T (line 249), reads count=2399 (same stale value, no lock), (3) both increment to 2400, both see `2400 > 2000*1.2` (line 256), (4) both call `path.read_text().splitlines()[-2000:]`, serialize tmp, call `tmp.replace(path)` (line 260), (5) second replace clobbers first; one of two entries (A or B) missing from rewritten feed.
- expected vs actual: both entries appear in feed; rewrite serialized if needed · actual: one entry lost; feed count may be inconsistent with actual line count.
- evidence: threads.py:246-261 shows `append_feed` has NO lock. Lines 250-254 read-modify-write of `_feed_counts` with no atomicity. Lines 256-261 rewrite triggered racily; two concurrent rewrites both read same old file, write competing tmps. projects.py:108,117 uses `async with self._lock` for analogous `_persist`; ThreadStore's `append_feed` does NOT.

### B-BRIDGE-5 — ToolHandlers cross-turn dedup collision: late tools share anonymous slot — MAJOR — fixed(worktree)
- class: concurrency/lifecycle · location: `synapse/dispatcher/tools.py:208-234` · found-by: H-BRIDGE
- symptom: late tool tail from prior turn can collide with fresh turn's dedup slot when `end_turn()` clears `_last_turn_id` but old turn's async tool handler still in flight. Late tool sees `_current_turn_id` as None (ContextVar cleared), falls back to `_last_turn_id` (also None after `end_turn`), gets anonymous turn id, writes into `_dedup["<anonymous>"]` — which NEXT turn also writes into if IT has late tail, causing cross-turn dedup collision (tool X from turn A dedups tool X from turn B).
- trigger: (1) turn A begins, `begin_turn("A")` sets ContextVar and `_last_turn_id = "A"`, (2) turn A's LLM calls `submit_task("X")`, handler starts, awaits inside `_guarded`, (3) turn A completes, `end_turn()` clears ContextVar, sets `_last_turn_id = None`, pops `_dedup["A"]`, (4) submit_task from step 2 resumes AFTER end_turn; reads `_current_turn_id` (None), reads `_last_turn_id` (None), (5) `_guarded` at line 240 uses `turn_id = self._current_turn_id or ""` → `""`, sets entry in `_dedup["<anonymous>"]`, (6) turn B begins immediately, same pattern: late tool from B ALSO writes `_dedup["<anonymous>"]`, (7) if both same tool name+args, they collide and return stale result cross-turn.
- expected vs actual: late tools from turn A use A's dedup slot (kept alive until tool completes), never collide with turn B · actual: late tools from different turns share `<anonymous>` slot, cross-turn dedup false positives.
- evidence: tools.py:208-234 shows `begin_turn` / `end_turn` / `_guarded`. Lines 224-227 `end_turn` sets `_current_turn_id = None` and `_last_turn_id = None` BEFORE checking if work in flight. Line 239 `turn_id = self._current_turn_id or ""` — late tool from cleared turn gets `""`. Line 240 both use `_dedup.setdefault(turn_id or "<anonymous>", {})` — collision point. Comment lines 228-231 says "keep `<anonymous>` slot ready after real turn closes" but doesn't keep REAL turn's slot alive for in-flight tools.

---

## 🛠 4. Dispatcher & Tools (`B-DISP-*`)
*Ошибки разбора реплик LLM-диспетчера, Mutex-блокировок ходов и привязки инструментов.*

### B-DISP-1 — history compaction race: concurrent threads corrupt splices — MAJOR — fixed(worktree, UNVERIFIED: test crashes in its own setup (FrozenInstanceError) — fix unverified)
- class: state machines/illegal transitions · location: `synapse/dispatcher/loop.py:340` · found-by: H-DISP
- symptom: concurrent compaction operations on same thread can corrupt history when they verify `history[:len(older)] == older` at overlapping times. Two parallel `ingest_user_turn` calls on same thread could both pass comparison check before either splices, then both splice, causing second to corrupt result of first.
- trigger: (1) two HTTP requests arrive concurrently for same thread_id, (2) both trigger `ingest_user_turn` → `_maybe_compact`, (3) both read `history` at line 177 when it's > threshold, (4) thread A reaches line 340, checks `history[:len(older)] == older` → True, (5) thread B reaches line 340 before A splices, checks `history[:len(older)] == older` → True (still same history), (6) thread A splices: `history[:len(older)] = [compact]`, (7) thread B splices using its stale `older` snapshot → corrupts A's compact.
- expected vs actual: only one compaction executes; second sees modified history and skips (line 340 guard) · actual: both execute; second corrupts first's result using stale `older` reference.
- evidence: lines 288-341 show compaction logic. Guard at line 340 `if history[:len(older)] == older:` prevents splicing stale snapshot, but comparison itself not atomic with splice. Between True result and splice at line 341, another task can splice first, invalidating comparison. Comment at lines 332-339 explicitly addresses concurrent appends during LLM call but misses compaction race: two compactions can run simultaneously because `_maybe_compact` has no lock and shares same mutable `history` list. Also line 154 `force_compact` calls `_maybe_compact` — explicit user command could be concurrent with `ingest_user_turn`.

### B-DISP-2 — end_turn() doesn't clear anonymous dedup slot when turn_id=None — MINOR — fixed(worktree; incidental to scoped dedup)
- class: state machines/illegal transitions · location: `synapse/dispatcher/tools.py:227-231` · found-by: H-DISP
- symptom: when `end_turn()` called with `_current_turn_id = None`, cleanup at line 227 `self._dedup.pop(turn_id, None)` pops `None` from dict (no-op), but fallback anonymous slot at line 231 `self._dedup.setdefault("<anonymous>", {})` still created. On NEXT `end_turn()` call for real turn, if that turn's `_last_turn_id` was never set (e.g., no turn ever called `begin_turn`), anonymous slot remains unbounded because it's only created, never cleaned.
- trigger: (1) call `end_turn()` when `_current_turn_id = None` (after another `end_turn()` already cleared it), (2) anonymous slot created at line 231, (3) late tool calls with no turn context fill anonymous slot, (4) call `end_turn()` again with `_current_turn_id = None`, (5) line 227 pops `None` (no-op), line 231 recreates `<anonymous>` → previous anonymous entries persist, (6) anonymous slot grows unbounded across multiple `end_turn()` calls with no active turn.
- expected vs actual: each `end_turn()` clears previous anonymous slot if existed · actual: anonymous slot created but never explicitly cleared when turn_id is None.
- evidence: line 227 `self._dedup.pop(turn_id, None)` when `turn_id = None` removes nothing. Line 231 `self._dedup.setdefault("<anonymous>", {})` always runs after pop, creating fresh slot. But no `self._dedup.pop("<anonymous>", None)` when `turn_id is None`, so prior anonymous entries survive. Comment at lines 228-230 says "Keep its one bounded slot ready after real turn closes" but doesn't address None case — when turn_id is None (already cleared), anonymous slot should also be cleared/reset, not just setdefault'd (which preserves existing entries).

### B-DISP-3 — _guarded dedup dict comparison fragile for nested args — MINOR — reported
- class: state machines/illegal transitions · location: `synapse/dispatcher/tools.py:243` · found-by: H-DISP
- symptom: dedup check `entry.args == args` compares two dicts using Python's `==`, which compares keys and values but doesn't guarantee order-independent comparison for nested structures. If `args` contains nested dicts or lists with different insertion orders but semantically identical content, equality check can fail, causing false dedup miss — same logical call executed twice.
- trigger: tool takes dict-valued argument (e.g., nested config object); two semantically identical but structurally different dicts (e.g., `{"a": 1, "b": 2}` vs dict from JSON with float `2.0`) might not compare equal. Current tools all take flat str/bool args, so actual risk low, but design fragile — future tool with nested args would break dedup silently.
- expected vs actual: dedup matches when logical arguments identical · actual: false misses if args contain non-comparable nested structures (rare but possible with complex tool schemas).
- evidence: line 243 `if entry is not None and entry.args == args:` uses Python's `==` on dicts. For simple flat dicts this works, but comment at line 178 says "Include arguments in match" to prevent `submit("A")` then `submit("B")` from collapsing. However, no normalization or hashing applied to `args` — if tool takes dict-valued argument, two semantically identical but structurally different dicts might not compare equal.

### B-DISP-4 — start_task doesn't reset store-level _last_event_ts, false UNREACHABLE — MINOR — reported
- class: state machines/illegal transitions · location: `synapse/bridge/state.py:208-212`, lines 301-305 · found-by: H-DISP
- symptom: `liveness()` treats COMPLETED as always OK, but doesn't reset `_last_event_ts` on new task. When new task started via `start_task()` at line 208, `_last_event_ts` NOT reset — retains timestamp of previous (completed) task's last event. If new task created and immediately becomes terminal (Kora fails instantly), `liveness()` check uses stale old timestamp, not new task's timestamp.
- trigger: (1) task A completes at t=100, `_last_event_ts = 100`, (2) `start_task` creates Task B at t=200, `started_ts=200`, but `_last_event_ts` still `= 100` (not reset), (3) task B immediately fails (no events emitted), status = FAILED, (4) call `liveness(now=300, stale_after=50)`, (5) line 301 check `if self._task.status == TaskStatus.COMPLETED:` is False (status FAILED), (6) line 303 `if self._last_event_ts is None:` is False (still 100), (7) line 305 `age = now - self._last_event_ts` → `300 - 100 = 200`, (8) line 306 `if age >= unreachable_after_s:` likely True → returns UNREACHABLE.
- expected vs actual: newly started task should reset liveness clock; instant failure not "unreachable" (Kora never given chance to heartbeat) · actual: new task inherits old task's `_last_event_ts`, causing false UNREACHABLE if no events arrive before it fails.
- evidence: `start_task` (lines 208-212) sets `started_ts=now` and `last_event_ts=None` on TaskState, but store's `_last_event_ts` (line 167, separate from `task.last_event_ts`) never reset. Liveness check at line 303 reads `self._last_event_ts`, which is store-level timestamp, not `task.last_event_ts`. Store-level `_last_event_ts` updated by `heartbeat()` (line 256) and `apply_event()` (line 261), never by `start_task`. So store-level `_last_event_ts` persists across tasks.

### B-DISP-5 — zombie reconciliation appends event but doesn't update _last_event_ts — MINOR — rejected(misdiagnosis: UNREACHABLE is correct here)
- class: state machines/illegal transitions · location: `synapse/bridge/state.py:446-458` · found-by: H-DISP
- symptom: during boot, if RUNNING task found in state.json, zombie reconciliation (S13) sets status=FAILED and appends KoraEvent at lines 448-457. However, event's `ts` field set to `self._clock.now()` (line 455), but store's `_last_event_ts` (used by liveness) NOT updated to match. Zombie task has event with ts=boot_time, but `_last_event_ts` still reflects pre-crash timestamp, causing liveness check to report STALE/UNREACHABLE even though we just added fresh event.
- trigger: (1) task crashes while RUNNING at t=100, last event at t=100, (2) server restarts at t=500, (3) `_load()` runs, zombie reconcile at line 446 detects RUNNING, (4) line 447 `self._task.status = TaskStatus.FAILED`, (5) lines 448-457 append KoraEvent with `ts=self._clock.now()` (500), (6) line 458 `self._persist()` writes updated task, (7) but `self._last_event_ts` still 100 (loaded from state.json at line 434, never updated), (8) call `liveness(now=600, stale_after=50, unreachable_after=100)`, (9) line 301 task.status is FAILED (not COMPLETED) → no OK override, (10) line 303 `_last_event_ts` not None (it's 100), (11) line 305 `age = 600 - 100 = 500` → UNREACHABLE.
- expected vs actual: after zombie reconciliation adds event at boot time, liveness should use that event's timestamp (fresh) · actual: liveness still uses pre-crash timestamp, falsely reporting UNREACHABLE.
- evidence: line 455 `ts=self._clock.now()` sets event's timestamp to boot time. Lines 448-457 event appended to `self._task.events`, but `self._last_event_ts` never updated. Line 434 `self._last_event_ts = data.get("last_event_ts")` loads old value from disk. No `self._last_event_ts = self._clock.now()` after adding reconciliation event. Reconciliation should call `self._last_event_ts = self._clock.now()` before `self._persist()` at line 458.
- rejected 2026-07-15: the premise «liveness should use that event's timestamp» is wrong. `liveness()` answers «when did Кора last emit a signal», not «when was the events list last appended to». The reconcile entry is the server writing a note to itself («сервер перезапускался») about a runner that died in the crash — there is no process to be alive. UNREACHABLE is the honest answer, not a false positive.
- the proposed fix was applied in a worktree and broke R6 (`test_state.py::test_persistence_roundtrip_restart_reports_stale_immediately`): refreshing `_last_event_ts` to boot time ages the zombie to 0 → liveness reports OK. That is the exact lie the S13 block it sits inside exists to stop («оставить как есть — liveness врёт OK»), reached through a side door instead of the FAILED→OK collapse that `liveness()`'s own comment (state.py:375-381) already forbids. Reverted; the guard comment now names the invariant in place.
- the real tension underneath is already parked and unchanged: a genuinely-failed idle task also ages into UNREACHABLE. Splitting it needs a distinct zombie marker to tell the two FAILED sources apart — not a reset liveness clock, which breaks the zombie case to soothe the other.

### B-DISP-6 — history_from_feed crashes on non-dict entries with AttributeError — MINOR — reported
- class: state machines/illegal transitions · location: `synapse/dispatcher/loop.py:50-68` · found-by: H-DISP
- symptom: function iterates over `entries` and calls `.get("kind")` without checking if each entry is dict. If feed contains non-dict entry (string, None, or list due to corruption or future schema change), line 64 `kind = e.get("kind")` raises AttributeError ("'str' object has no attribute 'get'").
- trigger: (1) thread feed gets corrupted, contains `[{"kind": "user", "text": "hi"}, "garbage", {"kind": "assistant", "text": "hello"}]`, (2) call `history_from_feed(entries)`, (3) lines 63-66 iterate over entries, (4) `e = "garbage"`, line 64 `e.get("kind")` → AttributeError.
- expected vs actual: malformed entries skipped or treated as error · actual: function crashes with AttributeError, halting history rehydration.
- evidence: lines 63-66 `for e in entries: kind = e.get("kind")` assumes each `e` is dict. No type check or try/except. Comment at line 51 says "единая точка регидрации" and emphasizes consistency, but doesn't mention resilience to malformed input. Function used in cold-cache rehydration (line 121), so corrupted feed file would crash dispatcher's first turn on that thread.

---

## ⚖ 5. Cascade & Strategy (`B-CASC-*`)
*Ошибки каскадного переключения LLM провайдеров, CircuitBreaker и CostCap дневных ограничений.*

### B-CASC-1 — Negative day buckets corrupt cost cap tracking before reset hour — MAJOR — reported
- class: data integrity · location: `synapse/cascade/services.py:72-75` · found-by: H-CASC
- symptom: when system starts or records paid attempt before `rpd_reset_hour_utc` hours after epoch (e.g., during first 8 hours after Jan 1, 1970 00:00 UTC, or any time between midnight and 8 AM on day 0 relative to reset hour), `_day_bucket()` returns negative integer. Negative bucket stored in `_reset_day`. On next call after reset hour passes, comparison `bucket > self._reset_day` becomes `0 > -1` → True, triggering unintended reset.
- trigger: (1) system clock returns `now < rpd_reset_hour_utc * 3600` (e.g., `now=7*3600`, `rpd_reset_hour_utc=8`), (2) call `record_paid_attempt(now)` → `_day_bucket(now)` returns `-1`, (3) `_reset_day` set to `-1`, (4) next call with `now=9*3600` → `_day_bucket(now)` returns `0`, (5) `0 > -1` → cap resets within same calendar day.
- expected vs actual: day bucket should never be negative; bucket transitions only at actual day boundaries · actual: `_day_bucket(7*3600, 8)` returns `-1`, causing premature resets.
- evidence: lines 72-75 show `return int((now - self._reset_hour * 3600) // 86400)`. When `now < self._reset_hour * 3600`, numerator negative, producing negative buckets. Comparison at line 85 `bucket > self._reset_day` treats `-1 < 0` as "new day" when transitioning from negative to zero.

### B-CASC-2 — CostCap allows exactly max calls but docs imply < max — MINOR — reported
- class: data integrity · location: `synapse/cascade/services.py:103` · found-by: H-CASC
- symptom: cap trips on or after reaching `_max`, not before. With `max_paid_calls_per_day=3`, calls 1, 2, and 3 all succeed (returning True), only call 4 blocked. Off-by-one from intuitive interpretation of "max 3 per day" meaning "up to 2 allowed".
- trigger: `cap = CostCap(max_paid_calls_per_day=3)`, three consecutive `cap.record_paid_attempt()` all return True (allowed), fourth blocked.
- expected vs actual: if "max 3 per day" means "no more than 3", 3rd call should trip cap and return True, blocking 4th. OR, if means "up to 3 allowed", code correct but name/docs misleading · actual: comparison `self._count >= self._max` on line 103 allows count to equal max before tripping. Permits exactly `_max` successful calls (1-indexed), may be 1 more than intended if "max" meant as exclusive upper bound.
- evidence: lines 102-104 show increment, then `if self._count >= self._max: self._tripped = True`, then `return True` (tripping call itself allowed). Docstring lines 93-95 says "overshoot bounded to single attempt that trips it" and "Returns True if this attempt may proceed", confirming tripping call allowed. However, config name `max_paid_calls_per_day` and semantic "max" in most APIs means "up to but not exceeding" (exclusive upper bound). Current code treats as inclusive (≥ triggers trip, so count can reach `_max`).

### B-CASC-3 — Day bucket fails to reset when reset_day=None, permanent money-blocking — CRIT — rejected(unreachable premise; the fix was a money bug)
- class: money correctness · location: `synapse/cascade/services.py:77-89` · found-by: H-CASC
- symptom: when `maybe_reset(now)` called for FIRST time with `now` at or after day boundary but `_reset_day is None`, code sets `_reset_day = bucket` and returns False (lines 82-84), WITHOUT checking if reset actually needed. First call after long idle (e.g., process restart) will fail to reset even if `now` in new day bucket, leaving stale `_count` and `_tripped` state from before restart.
- trigger: (1) process starts, `_reset_day = None`, (2) previous run had tripped cap (`_tripped=True`, `_count=500`), (3) state not persisted (or lost), (4) first call: `maybe_reset(now)` with `now` in day bucket 100, (5) code path: `_reset_day is None` → set `_reset_day=100`, return False, (6) cap remains tripped FOREVER because first call didn't reset, and all future calls see `bucket == _reset_day` (no advancement).
- expected vs actual: on first call, if `bucket` represents new day (or any day after reset should have happened), cap should reset · actual: first call ALWAYS initializes `_reset_day` to current bucket and returns False, regardless of whether cap previously tripped or what day it was last used.
- evidence: lines 81-84 show `if self._reset_day is None: self._reset_day = bucket; return False`. Path bypasses reset logic entirely. Reset only happens on lines 85-88 when `bucket > self._reset_day`, but requires `_reset_day` already set from prior call. If first call after restart lands in new day, cap never resets because: Call 1 `_reset_day=None` → set to current bucket, no reset; Call 2+ `bucket == _reset_day` → no reset. Only recovery if `now` advances to `bucket+1`, but by then cap blocked legitimate calls for entire day. **CRIT**: permanently blocks all paid attempts after restart if previous run tripped cap, even if full day (or more) passed. "Per day" recovery (B30's fix) completely bypassed by None sentinel initialization path.
- rejected 2026-07-15: the trigger contradicts itself. Step (2) posits a carried-over `_tripped=True, _count=500`, step (3) concedes "state not persisted (or lost)" — and (3) is the true one. `CostCap` has no rehydration path at all: the sole construction site (`pipeline/app.py:819`) always starts at `_count=0, _tripped=False`, so after a restart there is nothing to recover from and the None-path cannot block anything. One `grep -rn 'CostCap('` settles it.
- the fix was applied in a worktree and inverted the cap into a money bug (verified by repro, now pinned by `tests/test_costcap.py::test_maybe_reset_anchoring_the_day_never_clears_a_same_day_trip`). The reachable way to hold `_reset_day is None` while `_count > 0` is not a restart — it is `record_paid_attempt()` called without `now`, a sanctioned path (`tests/test_host_singleton.py:76`) that counts the attempt without anchoring the day. The recovery branch then read a legitimate same-day trip as restart debris and cleared it on the next `maybe_reset` tick — and `monitor_forever` ticks every heartbeat — so a tripped daily cap silently reopened and paid calls went through. Reverted to anchor-only; the day-rollover reset (B30) is untouched and pinned by `test_maybe_reset_still_clears_on_a_real_day_rollover`.

### B-CASC-4 — RPD reset mutes tier for 24h when failure at reset hour — MAJOR — reported
- class: data integrity · location: `synapse/cascade/breaker.py:85-90` · found-by: H-CASC
- symptom: when RPD (requests-per-day) failure occurs exactly at reset hour (e.g., 8:00:00 AM UTC), `_next_rpd_reset` computes mute-until timestamp as TOMORROW's reset hour, not today's. Mutes tier for 24 hours instead of unmuting immediately or within minutes.
- trigger: (1) RPD failure at `now = 2026-07-15 08:00:00 UTC` (exactly at reset hour), (2) `_next_rpd_reset(now, 8)` called, (3) `current = 2026-07-15 08:00:00`, `reset_today = 2026-07-15 08:00:00`, (4) `current >= reset_today` → True (line 88), (5) `reset_today += timedelta(days=1)` → `2026-07-16 08:00:00`, (6) tier muted until tomorrow, even though today's quota just reset.
- expected vs actual: if failure occurs at or slightly after reset hour, tier should either (a) unmute immediately (next reset is "now"), OR (b) mute until few seconds/minutes later (short grace period). Current logic treats "at reset hour" as "already past today's reset" and mutes for full day · actual: `current >= reset_today` uses `>=`, so `current == reset_today` triggers +1 day path. Tier failing at 08:00:00 muted until tomorrow's 08:00:00, blocking all RPD-quota turns for 24 hours even though quota just reset.
- evidence: lines 88-89 show `if current >= reset_today: reset_today += timedelta(days=1)`. Equality case (`current == reset_today`) should arguably return `reset_today.timestamp()` (unmute now) or `reset_today + small_delta`, not skip to tomorrow. Comment says "rolling to tomorrow if that hour already passed today", technically correct at reset instant, but INTENT is to mute until NEXT reset, and "next reset" when exactly at reset boundary is ambiguous. **MAJOR**: causes 24-hour outage window for RPD-limited tiers if failure lands on reset second. Rare (1-second window per day), but when happens, full-day outage instead of near-instant recovery.

---

## ⚙ 6. Core & CLI Runners (`B-CORE-*`)
*Ошибки CLI утилит разметки датасетов, считывания .env настроек и ведения журнала TurnJournal.*

### B-CORE-1 — TurnJournal fd leaks on exception during initialization — MAJOR — reported
- class: resource leak · location: `synapse/journal.py:73` · found-by: H-CORE
- symptom: file descriptor remains open forever if exception occurs after line 73 but before journal properly managed or closed.
- trigger: (1) create TurnJournal, (2) exception occurs during setup/usage before any close() path reached (downstream code crashes), (3) file handle at `self._file` never closed.
- expected vs actual: file opened in context manager or with explicit try/finally protection · actual: bare `.open()` with cleanup only via explicit `close()` call.
- evidence: line 73 `self._file = self._path.open("a", encoding="utf-8")`. No context manager, no try/finally around lifetime. File only closed in two places: line 176 `close()` method (requires explicit call), line 115 console.py calls `journal.close()`, line 125 webrtc_server.py shutdown handler calls `host.journal.close()`. If any code path fails to call `close()` (early exception in setup, or forgotten call site), fd leaks.

### B-CORE-2 — Thread never joined in record_commands.py on early exit — CRIT — reported
- class: resource leak · location: `synapse/runners/record_commands.py:64-71` · found-by: H-CORE
- symptom: daemon thread running `_wait_for_enter` never joined; on process exit it's forcibly killed, but recording loop can exit early (e.g., `stop_event.set()`) leaving thread dangling until GC or process death.
- trigger: (1) start recording, (2) user presses Enter to stop, (3) `stop_event.set()` fires, (4) loop exits at line 68, (5) finally block closes stream (lines 70-71), (6) thread at line 64 never joined — remains alive until it completes `input()` on its own or process exits.
- expected vs actual: `waiter.join(timeout=0.1)` after loop to clean up promptly · actual: thread left running.
- evidence: lines 64-71 show `waiter = threading.Thread(target=_wait_for_enter, daemon=True)`, `waiter.start()`, recording loop, finally closes stream. No `waiter.join()`. Daemon flag prevents blocking process exit, but thread remains alive consuming stack until it naturally completes. Not leak that grows unbounded (one thread per recording session), but still resource held longer than necessary.

### B-CORE-3 — TurnJournal._write fsync exception leaves _file inconsistent — MAJOR — reported
- class: resource leak · location: `synapse/journal.py:169-172` · found-by: H-CORE
- symptom: if `os.fsync(self._file.fileno())` raises (disk full, fd closed externally), exception propagates but file remains open with unflushed data or inconsistent fd state.
- trigger: (1) journal writes row via `_write()`, (2) line 170 `self._file.flush()` succeeds, (3) line 172 `os.fsync(self._file.fileno())` raises `OSError` (disk full, ENOSPC), (4) exception propagates to caller (e.g., `alert()` swallows it at line 126, but `end_turn()` does not), (5) file handle remains open, potentially in bad state.
- expected vs actual: `_write()` should catch `OSError` on fsync, set `self._closed = True`, close file to prevent further corruption · actual: exception propagates; file remains open.
- evidence: lines 169-172 show `self._file.write(...)`, `self._file.flush()`, `if fsync: os.fsync(self._file.fileno())`. No try/except around fsync. While `alert()` has try/except (lines 123-126), `end_turn()` (line 160) and `record_kora_event()` (line 154) call `_write()` without protection, so fsync failure propagates and may leave journal in bad state.

### B-CORE-4 — TTS cache tmp file cleanup races with process exit — MINOR — reported
- class: resource leak · location: `synapse/pipeline/tts_cache.py:62-71` · found-by: H-CORE
- symptom: if process killed (SIGKILL) or crashes hard between line 65 (`os.replace(tmp, path)`) completing and line 69 (`tmp.unlink()`), tmp file leaks on disk.
- trigger: (1) `_atomic_write()` creates `.tmp` file (line 62), (2) lines 64-65 write data, call `os.replace(tmp, path)` — succeeds, (3) process crashes or killed before line 69 `tmp.unlink()` runs, (4) `.tmp` file remains on disk forever (the `if tmp.exists()` check at line 67 will never run).
- expected vs actual: accept this edge case, or use startup cleanup sweep for stale `.tmp` files · actual: tmp files leak on hard crash.
- evidence: lines 62-71 show `tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")`, try block writes and replaces, finally unlinks. `os.replace()` atomic and main goal achieved, but tmp file not cleaned atomically with replace. Minor leak (tmp files .hidden, each uniquely named, accumulate slowly), but over time cache directory grows.

### B-CORE-5 — subprocess not killed on exception during communicate() — MINOR — reported
- class: resource leak · location: `synapse/pipeline/webrtc_server.py:576-582` · found-by: H-CORE
- symptom: if exception (other than `TimeoutError`) occurs during `proc.communicate()` at line 577, subprocess `proc` never killed and remains zombie.
- trigger: (1) `_git()` starts subprocess (lines 572-575), (2) line 577 `await asyncio.wait_for(proc.communicate(), 10.0)` raises exception OTHER than `TimeoutError` (e.g., `CancelledError`, `RuntimeError`), (3) exception propagates without killing `proc`, (4) subprocess remains alive as zombie until reaped by init or process exit.
- expected vs actual: catch all exceptions, kill proc, wait for it, then re-raise · actual: only `TimeoutError` handled.
- evidence: lines 572-582 show subprocess creation, try/except around wait_for. Only `TimeoutError` caught (lines 578-581 kill and wait). Any other exception (e.g., `CancelledError` if HTTP request cancelled) escapes without cleanup.

### B-CORE-6 — KoraRunner._active task leaks on RuntimeError during start() — MAJOR — reported
- class: resource leak · location: `synapse/bridge/kora.py:460-465` · found-by: H-CORE
- symptom: if `asyncio.create_task(coro)` at line 462 raises `RuntimeError` (no running loop), coroutine closed at line 464, but if `_active` was not None and was cancelled at line 459, that task never awaited and may leak.
- trigger: (1) `start()` called when `_active` not None and not done, (2) line 459 `self._active.cancel()` schedules cancellation, (3) line 462 `asyncio.create_task(coro)` raises `RuntimeError` (no loop), (4) line 464 `coro.close()` runs, line 465 `_terminalize_if_running(task_id)` runs, (5) old `_active` task (cancelled at line 459) never awaited — remains in cancelled state and may leak coroutine/resources.
- expected vs actual: `try: await old_active except CancelledError: pass` before closing coro · actual: cancelled task not awaited.
- evidence: lines 458-465 show `if self._active is not None and not self._active.done(): self._active.cancel()`, then `coro = self._run(...)`, try/except around `create_task`. Cancelled `_active` never awaited. While cancelling schedules cleanup, without `await` task object itself may not be fully cleaned until GC.

---

### Closed without fix
- **A1 rejected** — mic-btn disconnect branch (app.js:405-411) not fenced by `connecting`. Not a standalone bug: tap-off→tap-on is a legitimate user reconnect; the disconnect runs on the captured old client `c` while the connect builds a fresh `client`, and the identity-guard (`client === me`) neutralises the old client's late callbacks. A genuine double-`connectVoice()` requires the watchdog path — recorded as **B-UX-1** (shared root: disconnect not fenced by `connecting`).

### Parked (out of hunt scope / not a hard bug)
- unbounded `renderedKeys`/`#feed-list` growth on very long-lived threads (no pruning) — memory, not correctness.
- `#mic-btn` static `aria-label` across idle/connecting/on/error states — no state feedback to AT.
- tap targets `#side-close`/`#menu-btn` ~34-36px (<44px guideline); `pollStatus`/`picker-choose` lack in-flight guards (cosmetic flicker / low-risk double-POST).

---

## 📊 Hunt 2026-07-15 — Summary

**Scope:** Zones 2-6 (WebRTC/Pipeline, Bridge/Kora, Dispatcher/Tools, Cascade/Strategy, Core/CLI) — zone 1 (Frontend/Client UI) was completed in prior hunt 2026-07-14.

**Method:** 5 parallel sonnet-hunters, each with:
- **DEEP pass** — own assigned files line-by-line, every branch
- **LENS pass** — one bug class across WHOLE scope (silent failures, concurrency/races, state machines, data integrity, resource leaks)

**Results:**
- **33 bugs found** across 5 zones (6 PIPE + 5 BRIDGE + 6 DISP + 4 CASC + 6 CORE + 6 prior UX)
- **Severity distribution:** 5 CRIT · 18 MAJOR · 10 MINOR
- **Status:** all `reported` — phase 2 (test-writing) not started

**Key patterns:**
- **Concurrency (BRIDGE, DISP):** shared mutable state without locks (TaskStore._persist, ThreadStore.append_feed, history compaction, cross-turn dedup collision)
- **Silent failures (PIPE):** broad exception handlers swallow errors, state mutations before risky operations (kora_runner.start zombie, monitor_forever continues on persistent errors, TTSCacheObserver)
- **Resource leaks (CORE):** file descriptors, threads, subprocesses, asyncio tasks not cleaned on error paths
- **Money correctness (CASC):** negative day buckets, reset_day=None bypass (CRIT — permanent blocking after restart), RPD reset at exact hour mutes 24h

**Critical findings:**
- **B-PIPE-2 (CRIT):** kora_runner.start() failure after state mutations leaves zombie run — UI shows "running" but nothing running, watchdog eventual but no immediate feedback
- **B-BRIDGE-3 (CRIT):** apply_event race — second event lost when first's _persist in flight (data loss on restart)
- **B-CASC-3 (CRIT):** reset_day=None bypasses daily reset — permanent money-blocking after restart if previous run tripped cap
- **B-CORE-2 (CRIT):** Thread never joined in record_commands.py — daemon thread left dangling

**Next phase:** dispatch test-writers (phase 2) to write red tests for CRIT/MAJOR bugs, verify via `proven` status, then fix (phase 3).

**Hunters:**
- H-PIPE (silent failures & error handling) — 6 bugs
- H-BRIDGE (concurrency, races & lifecycle) — 5 bugs
- H-DISP (state machines & illegal transitions) — 6 bugs
- H-CASC (data integrity & money correctness) — 4 bugs
- H-CORE (resource leaks & cleanup) — 6 bugs
