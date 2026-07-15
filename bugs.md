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

### B-PIPE-3 — monitor_forever exception handler continues silently, heartbeat checks skipped indefinitely — MAJOR — fixed(2026-07-15)
- class: silent failure · location: `synapse/pipeline/app.py:281-297` · found-by: H-PIPE
- symptom: `monitor_forever` loop catches all non-CancelledError exceptions and logs with `logger.exception`, then continues next iteration. If exception is transient (e.g., `os.fsync` failure during `journal.alert`), correct. But if persistent (e.g., `self.store` in corrupted state and `store.liveness()` raises every time), loop log-spams forever and never actually performs heartbeat checks.
- trigger: (1) TaskStore enters bad state (internal invariant violated, filesystem unavailable), (2) `store.liveness()` on line 285 raises on every iteration, (3) exception caught, logged, loop continues, (4) CRITICAL_WITHOUT_SPEAK checks (283-284) and KORA_UNREACHABLE alerts (285-291) never executed.
- expected vs actual: persistent failures should either halt monitor (fail-stop) or escalate to higher supervisor · actual: loop continues indefinitely, logging same exception every `heartbeat_interval_s`, while watchdog checks silently skipped.
- evidence: lines 281-297 show broad `except Exception` on line 296 catches ALL errors in block (282-293) including from `speak_ledger.check()`, `store.liveness()`, `journal.alert()`, `cost_cap.maybe_reset()`. Persistent failure in any makes entire monitor ineffective.

### B-PIPE-4 — TTSCacheObserver exception handler swallows all cache write failures — MAJOR — fixed(2026-07-15)
- class: silent failure · location: `synapse/pipeline/tts_cache.py:152-158` · found-by: H-PIPE
- symptom: `TTSCacheObserver.on_push_frame` wraps all cache logic in broad `except Exception` that logs and ignores ALL errors. If cache writes fail persistently (disk full, permissions error, filesystem corruption), frames silently dropped from cache and system continues as if fine. Users experience cache misses on every play, triggering expensive REST TTS synthesis, but no alert raised.
- trigger: (1) disk fills or cache directory permissions change, (2) `cache.put_pcm()` on line 201 (called from `_finalize`) raises `OSError`, (3) exception caught in `on_push_frame`, logged once, ignored, (4) every subsequent TTS frame fails to cache, all future plays require REST synthesis.
- expected vs actual: persistent cache write failures should either alert (journal.alert) OR disable cache gracefully · actual: errors logged but observer continues silently failing, degrading performance with no visibility.
- evidence: lines 152-158 show try/except around `_handle()`. Comment lines 153-154 justifies catching to avoid crashing audio pipeline, but doesn't address that persistent failures should surface as alerts rather than silent degradation.

### B-PIPE-5 — run_session finally block cleanup racing with state assignment leaves stale current/bind — MAJOR — rejected(misdiagnosis: pop уже защищён гвардом ровно на этот случай)
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

### B-DISP-3 — _guarded dedup dict comparison fragile for nested args — MINOR — rejected(misdiagnosis: dict `==` в Python УЖЕ order-independent; «фикс» родил бы ложные HIT'ы)
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

### B-DISP-7 — note_external_turn revives history cleared by clear_history (C6 asymmetry) — MAJOR — fixed(2026-07-15)
- class: concurrency/lifecycle · location: `synapse/dispatcher/loop.py:149-159` · found-by: 2026-07-15 sweep over the diff
- symptom: `note_external_turn` дописывает реплику в общую LLM-историю треда БЕЗ сверки поколения. `clear_history` инкрементит `_generations` (C6); `ingest_user_turn` сверяет поколение на коммите (B20-стиль), чтобы `clear`, прилетевший во время `await`, не воскресил очищенную историю. `note_external_turn` делает ровно то же — дописывает в shared history — но **без** этой проверки, образуя асимметрию с C6/B20. Голосовой путь зовёт его из `_flush_voice_context` (`app.py:1068`, ВНЕ `turn_lock`), а `clear_history` идёт из HTTP-роута под `turn_lock` — это разные локи, так что «clear» и голосовой flush не сериализуются.
- trigger: (1) голосовой ход идёт, `_flush_voice_context` готовит assistant-реплику, (2) юзер параллельно шлёт `clear` через HTTP-тред (тот же id) → `clear_history` чистит `hist[:] = []`, generation→N+1, (3) голосовой `note_external_turn("…", "assistant", text)` дописывает реплику БЕЗ сверки поколения → очищенная история воскресла. На следующем HTTP-ходе `_history_for` вернёт кэшированный (оживший) список вместо свежей регидрации из ленты.
- expected vs actual: после `clear` последующий `note_external_turn` — no-op (как холодная регидрация: writer уже положил запись в ленту, подхватится сам) · actual: реплика дописана, история воскресла.
- evidence: `loop.py:149-159` — `note_external_turn` берёт `_history_lock_for` и зовёт `_append_coalesced(hist, …)` без всякого `_generations.get(thread_id)` снимка/сверки, в отличие от `ingest_user_turn`'s commit-блока (`loop.py:256-260`). Зонд (unit): warm → `note_external(user)` len=1 → `clear_history` len=0 gen=1 → `note_external(assistant)` len=**1** (ожило).
- proven 2026-07-15: pinned red by `tests/test_bughunt_2026_07_15_failing.py::test_b_disp_7_note_external_turn_revives_cleared_history`. Fix: `note_external_turn` должен либо снимать поколение и сверять (как ingest), либо — проще и симметричнее холодной регидрации — инвалидировать кэш по факту clear (тогда writer уже в ленте, подхватится на следующем miss). parked до выбора API: чистый пред-фикс красный тест сейчас пинит только эффект, не форму решения.

---

## ⚖ 5. Cascade & Strategy (`B-CASC-*`)
*Ошибки каскадного переключения LLM провайдеров, CircuitBreaker и CostCap дневных ограничений.*

### B-CASC-1 — Negative day buckets corrupt cost cap tracking before reset hour — MAJOR — reported
- class: data integrity · location: `synapse/cascade/services.py:72-75` · found-by: H-CASC
- symptom: when system starts or records paid attempt before `rpd_reset_hour_utc` hours after epoch (e.g., during first 8 hours after Jan 1, 1970 00:00 UTC, or any time between midnight and 8 AM on day 0 relative to reset hour), `_day_bucket()` returns negative integer. Negative bucket stored in `_reset_day`. On next call after reset hour passes, comparison `bucket > self._reset_day` becomes `0 > -1` → True, triggering unintended reset.
- trigger: (1) system clock returns `now < rpd_reset_hour_utc * 3600` (e.g., `now=7*3600`, `rpd_reset_hour_utc=8`), (2) call `record_paid_attempt(now)` → `_day_bucket(now)` returns `-1`, (3) `_reset_day` set to `-1`, (4) next call with `now=9*3600` → `_day_bucket(now)` returns `0`, (5) `0 > -1` → cap resets within same calendar day.
- expected vs actual: day bucket should never be negative; bucket transitions only at actual day boundaries · actual: `_day_bucket(7*3600, 8)` returns `-1`, causing premature resets.
- evidence: lines 72-75 show `return int((now - self._reset_hour * 3600) // 86400)`. When `now < self._reset_hour * 3600`, numerator negative, producing negative buckets. Comparison at line 85 `bucket > self._reset_day` treats `-1 < 0` as "new day" when transitioning from negative to zero.

### B-CASC-2 — CostCap allows exactly max calls but docs imply < max — MINOR — rejected(спор о значении «max»; «фикс» отключил бы платные вызовы при max=1)
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

### B-CORE-2 — Thread never joined in record_commands.py on early exit — MINOR (был CRIT — переоценён, см. fix) — fixed(2026-07-15)
- class: resource leak · location: `synapse/runners/record_commands.py:64-71` · found-by: H-CORE
- symptom: daemon thread running `_wait_for_enter` never joined; on process exit it's forcibly killed, but recording loop can exit early (e.g., `stop_event.set()`) leaving thread dangling until GC or process death.
- trigger: (1) start recording, (2) user presses Enter to stop, (3) `stop_event.set()` fires, (4) loop exits at line 68, (5) finally block closes stream (lines 70-71), (6) thread at line 64 never joined — remains alive until it completes `input()` on its own or process exits.
- expected vs actual: `waiter.join(timeout=0.1)` after loop to clean up promptly · actual: thread left running.
- evidence: lines 64-71 show `waiter = threading.Thread(target=_wait_for_enter, daemon=True)`, `waiter.start()`, recording loop, finally closes stream. No `waiter.join()`. Daemon flag prevents blocking process exit, but thread remains alive consuming stack until it naturally completes. Not leak that grows unbounded (one thread per recording session), but still resource held longer than necessary.

### B-CORE-3 — TurnJournal._write fsync exception leaves _file inconsistent — MAJOR — fixed(2026-07-15)
- class: resource leak · location: `synapse/journal.py:169-172` · found-by: H-CORE
- symptom: if `os.fsync(self._file.fileno())` raises (disk full, fd closed externally), exception propagates but file remains open with unflushed data or inconsistent fd state.
- trigger: (1) journal writes row via `_write()`, (2) line 170 `self._file.flush()` succeeds, (3) line 172 `os.fsync(self._file.fileno())` raises `OSError` (disk full, ENOSPC), (4) exception propagates to caller (e.g., `alert()` swallows it at line 126, but `end_turn()` does not), (5) file handle remains open, potentially in bad state.
- expected vs actual: `_write()` should catch `OSError` on fsync, set `self._closed = True`, close file to prevent further corruption · actual: exception propagates; file remains open.
- evidence: lines 169-172 show `self._file.write(...)`, `self._file.flush()`, `if fsync: os.fsync(self._file.fileno())`. No try/except around fsync. While `alert()` has try/except (lines 123-126), `end_turn()` (line 160) and `record_kora_event()` (line 154) call `_write()` without protection, so fsync failure propagates and may leave journal in bad state.

### B-CORE-4 — TTS cache tmp file cleanup races with process exit — MINOR — fixed(2026-07-15)
- class: resource leak · location: `synapse/pipeline/tts_cache.py:42-46,62-71` · found-by: H-CORE
- symptom: if process killed (SIGKILL) or crashes hard between line 65 (`os.replace(tmp, path)`) completing and line 69 (`tmp.unlink()`), tmp file leaks on disk.
- trigger: (1) `_atomic_write()` creates `.tmp` file (line 62), (2) lines 64-65 write data, call `os.replace(tmp, path)` — succeeds, (3) process crashes or killed before line 69 `tmp.unlink()` runs, (4) `.tmp` file remains on disk forever (the `if tmp.exists()` check at line 67 will never run).
- expected vs actual: accept this edge case, or use startup cleanup sweep for stale `.tmp` files · actual: tmp files leak on hard crash.
- evidence: lines 62-71 show `tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")`, try block writes and replaces, finally unlinks. `os.replace()` atomic and main goal achieved, but tmp file not cleaned atomically with replace. Minor leak (tmp files .hidden, each uniquely named, accumulate slowly), but over time cache directory grows.
- proven 2026-07-15: pinned red by `tests/test_bughunt_2026_07_15_failing.py::test_b_core_4_tts_cache_init_does_not_sweep_orphaned_tmp` — an orphaned `.*.tmp` survives `TTSCache(...)` construction, so it accumulates across restarts (no startup sweep in `__init__`, lines 42-46 only `mkdir`). Fix: sweep `root.glob(".*.tmp")` in `__init__` and unlink survivors (same `.*.tmp` pattern `_atomic_write` mints), best-effort (ignore missing/OSError).

### B-CORE-5 — subprocess not killed on exception during communicate() — MINOR — reported
- class: resource leak · location: `synapse/pipeline/webrtc_server.py:576-582` · found-by: H-CORE
- symptom: if exception (other than `TimeoutError`) occurs during `proc.communicate()` at line 577, subprocess `proc` never killed and remains zombie.
- trigger: (1) `_git()` starts subprocess (lines 572-575), (2) line 577 `await asyncio.wait_for(proc.communicate(), 10.0)` raises exception OTHER than `TimeoutError` (e.g., `CancelledError`, `RuntimeError`), (3) exception propagates without killing `proc`, (4) subprocess remains alive as zombie until reaped by init or process exit.
- expected vs actual: catch all exceptions, kill proc, wait for it, then re-raise · actual: only `TimeoutError` handled.
- evidence: lines 572-582 show subprocess creation, try/except around wait_for. Only `TimeoutError` caught (lines 578-581 kill and wait). Any other exception (e.g., `CancelledError` if HTTP request cancelled) escapes without cleanup.

### B-CORE-6 — KoraRunner._active task leaks on RuntimeError during start() — MAJOR — fixed(2026-07-15)
- class: resource leak · location: `synapse/bridge/kora.py:453-465` · found-by: H-CORE
- symptom: if `asyncio.create_task(coro)` raises `RuntimeError` (no running loop — the console + `kora_enabled` path, or a sync test), the `except RuntimeError` branch closes the coroutine and terminalizes the task, but leaves `self._active` pointing at the PREVIOUSLY-CANCELLED task instead of `None`. The runner's live-run invariant ("`_active` is a live run or `None`") is violated.
- trigger: (1) a prior run left `self._active` set and not done, (2) `start()` line 458-459 cancels it, (3) line 462 `asyncio.create_task(coro)` raises `RuntimeError` (no loop), (4) `coro.close()` (464) + `_terminalize_if_running` (465) run, (5) `self._active` is NEVER reset to `None` — it still references the cancelled task.
- expected vs actual: after a failed launch there is no live run, so `_active is None` · actual: `_active` dangles at the cancelled task; the next `start()` happens to recover (`.done()` is True after cancel) but the invariant is silently broken.
- evidence: lines 458-465 show the cancel, the `try/except RuntimeError`, `coro.close()`, `_terminalize_if_running` — but no `self._active = None` in the except branch. The success path (462) rebinds `_active` to the new task, masking the defect on the happy path.
- proven 2026-07-15: pinned red by `tests/test_bughunt_2026_07_15_failing.py::test_b_core_6_runner_active_not_cleared_when_create_task_raises` — `_active` is still the stale mock after a `create_task` RuntimeError. Fix: `self._active = None` in the `except RuntimeError` branch (the original H-CORE framing, "await the cancelled task", was a different concern — GC reaps a cancelled task fine; the real hole is the dangling reference).

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

---

## 🔐 Hunt 2026-07-15 (вечер) — Фаза 0: auth + money (5 hunters)

Зона: `bridge/{approvals,affirm,confirm}.py` · `bridge/kora.py` (гейт) · `dispatcher/{tools,llm_client,loop}.py` · `cascade/{services,breaker,strategy,classify}.py` · `pipeline/{app,webrtc_server}.py` (места Ф0). Дерево заморожено на `058faf2`.
Линзы: H1 security/input-validation · H2 state-machines · H3 money-correctness · H4 silent-failures · H5 concurrency.
В бриф вшит **гейт достижимости** (назови реальный call path) — после урока B-CASC-3, где «фикс» недостижимой премисы сам оказался money-багом.

### B-BRIDGE-6 — ConfirmFlow: двухключевой контракт необратимых задач не скоупится к треду — CRIT — fixed(2026-07-15)
- class: security · location: `synapse/bridge/confirm.py:152-158` (`note_user_turn`), `:178-187` (self-attempt guard + affirm) · found-by: H1 (corroborated: H2 нашёл ту же асимметрию с другой стороны)
- symptom: `ConfirmFlow` защищает **необратимые** задачи (`is_destructive` → readback → подтверждение), но не имеет понятия треда/канала вообще. `_staged` — один на весь процесс, `_last_user_turn_transcript` — одна строка, у `note_user_turn(transcript, now)` нет параметра треда. Любой ход в любом треде (a) гасит `awaiting_user_turn` чужого staged-таска и (b) становится тем транскриптом, который `confirm()` отклассифицирует. Оба ключа выдаёт посторонний разговор.
- trigger: (1) тред A: реплика проходит `is_destructive` (`config.py:63-74`) → `submit()` стажирует таск, `awaiting_user_turn=True` (confirm.py:142). (2) В пределах `confirm_timeout_s=30` любой ход в треде B: `POST /api/threads/{B}/message` → `loop.py:187 note_user_turn(B-текст)` → глобальный флаг снят, транскрипт перезаписан текстом B. (3) Активная задача глобальна по дизайну (`state.py:162-163`), поэтому LLM треда B видит «задача ждёт подтверждения» и зовёт `confirm_task(decision="confirm")` (`_VALID_TOOL_NAMES`, `loop.py:281`). (4) `confirm()`: self-attempt guard не срабатывает (флаг снят чужим ходом), TTL не истёк, `_classify_response(текст B)` — любое бытовое «да» (`config.py:61`) → COMMITTED → необратимая задача A запускается.
- expected vs actual: expected — оба ключа обязаны прийти из того же треда, что владеет pending; ровно это свойство `ApprovalService` получил явно: пер-тредовые `_pending`/`_last_user_turn`/`_user_turn_seq` (`approvals.py:88-94`) + монотонный вотермарк (`approvals.py:138`), закреплено `tests/test_phase0_approval.py::test_note_user_turn_is_thread_scoped` · actual — у `ConfirmFlow` ни одного `thread_id` в сигнатурах, ни вотермарка; вместо него булев `awaiting_user_turn`, снимаемый любым ходом откуда угодно.
- reachability: голос → `app.py:988` (`_on_end_of_turn`, каждая живая реплика); HTTP → `loop.py:187` (`ingest_user_turn`) ← `webrtc_server.py:669`. Один инстанс `ConfirmFlow` строится в `app.py:571-574` и раздаётся в `bridge` (:778), `http_bridge` (:797), `text_loop` (:840), `host` (:867) — тот же Python-объект.
- evidence: confirm.py:152-158 (безусловная перезапись глобального транскрипта + снятие чужого флага); confirm.py:178-187 (guard читает снятый флаг); контраст — approvals.py:88-94/107-112/138 (тред-скоуп + вотермарк).
- note: асимметрия «новый механизм закалён, старый нет». C3/C4 усиливали `gate_action`/`ApprovalService`; `ConfirmFlow` охраняет не менее опасный путь и остался с исходной моделью угроз.
- fix(2026-07-15): скоуп разговора сквозь весь механизм — `_Staged.thread_id` + пер-разговорный `_last_user_turn` dict (форма ApprovalService), `submit/note_user_turn/confirm` приняли `thread_id`. Оба ключа обязаны прийти из разговора-владельца: `note_user_turn` снимает `awaiting_user_turn` только своему staged, `confirm` из чужого треда → REJECTED (и не запускает, и НЕ гасит — `_reset` по чужому «нет» тоже чужое решение).
- **Несущая находка фикса (иначе фикс убил бы фичу):** наивный «нет треда → отказ» ломает ГОЛОС. Авто-тред рождается лениво (`_on_task_committed`/D1'-эйджер), поэтому в момент `submit()` необратимой задачи `thread_id_for()` штатно None — скоуп None означал бы «подтвердить некому», и голосовой confirm умер бы совсем. Разговор ≠ тред: канал без треда — это тоже ОДИН разговор. Отсюда `KoraBridge.channel` ("voice"/"http") и единая точка `confirm_scope() = thread_id_for() or channel`; голосовой `note_user_turn` в app.py считает скоуп ТЕМ ЖЕ выражением (разойдись они — задачу нельзя было бы подтвердить вообще).
- fix-побочное: staged-блоб, записанный ДО скоупинга, восстанавливается без разговора-владельца → подтвердить его не может никто, а синглтон-стор он занял бы навсегда. Роняем на старте (`store.set_staged(None)`) и отдаём висящий PENDING B12-реконсиляции: неатрибутируемая необратимая задача не переживает рестарт.
- тесты: `tests/test_hunt0715_auth.py::test_b_bridge_6_...` (fail-closed: разговор неизвестен → отказ) + кросс-тредовое доказательство в `tests/test_confirm.py` (A ставит → Б говорит «да» → confirm треда Б отклонён, задача A цела; A подтверждает сам → COMMITTED). Второй писан ПОСЛЕ фикса: на старом API сценарий «Б подтверждает задачу A» был невыразим — параметра треда не существовало, — поэтому доказательство охоты кодировало лишь ОТСУТСТВИЕ скоупа. Дискриминация показана подклассом со старым поведением.

### B-CASC-5 — залипший failover-тир уходит от CostCap: дневной лимит слепнет после первого сбоя — CRIT — fixed
- class: money correctness · location: `synapse/pipeline/app.py:547-556` (`_CostCountingLLMSwitcher.push_frame`) × `synapse/cascade/strategy.py:96-124` (`_advance`) · found-by: H3
- symptom: `record_paid_attempt` достижим ровно из двух мест: `_advance` (только по ошибке, т.е. только в момент переключения) и `push_frame`, где счёт зажат условием `idx == 0`. После файловера на платный тир idx>0 активный тир **залипает** до конца соединения: вендорный `_active_service` присваивается только в `__init__` (`services[0]`) и в `_set_active_if_available`, который у нас зовётся исключительно из `_advance`; `ManuallySwitchServiceFrame` не шлёт никто. Значит каждый следующий УСПЕШНЫЙ ход на залипшем тире — реальный платный вызов, который не считает никто.
- trigger: (1) `webrtc_server.py:160 build_session_pipeline` строит один свитчер на всё WebRTC-соединение (докстринг app.py:931-936: свежий только на реконнект). (2) Ход 1: тир0 отдаёт любую нефатальную ошибку (RPM/TIMEOUT/5xx) → `handle_error` → `_advance` → тир1 посчитан один раз (strategy.py:103), `_active_service = services[1]`. (3) Ход 2+: тир1 здоров → `ErrorFrame` нет → `_advance` не зовётся; `push_frame` видит `idx==1` → `idx == 0` ложно → счёта нет. Повторяется неограниченно.
- expected vs actual: expected — каждая платная попытка инкрементит дневной счётчик (собственный докстринг strategy.py:12: «CostCap gates every paid-tier attempt») · actual — после одного файловера неограниченное число оплаченных вызовов невидимы для `CostCap`; `max_paid_calls_per_day` не триггерится до конца соединения.
- reachability: живой путь, без тестовой оснастки — одного рядового рейт-лимита на первичном тире в живом звонке достаточно. `webrtc_server.py:160` → `app.py:941` → `strategy.py:73 handle_error` → `strategy.py:96 _advance` (счёт один раз) → все дальнейшие `LLMFullResponseEndFrame` в `app.py:547` с `idx==1` молча мимо счёта.
- evidence: app.py:553-555 (`if (idx == 0 and self._labels[idx].paid and not self.strategy.advanced_this_generation())`); strategy.py:102-110 (единственный второй call site, только на error-пути); вендор `pipecat/pipeline/service_switcher.py:60,119` (`_active_service` — только `__init__` и `_set_active_if_available`), `grep -rn "_set_active_if_available\|ManuallySwitchServiceFrame" synapse/` → единственный хит strategy.py:112.
- note: это **рецидив R9**. Докстринг B04 (app.py:530-538) описывает ровно эту дыру — «a healthy tier1 turn made a real billed call that never counted and the daily cap was structurally inert» — и закрывает её только для стартового тира. Тесты обошли границу с обеих сторон: `test_hunt0714_a.py:189-213` (один ход на тире0), `test_hunt0714b_app.py:229-269` (возврат на тир0 не двоит). Второго успешного хода на залипшем ненулевом тире не делает ни один.

### B-DISP-8 — пустой ответ провайдера отдаётся как успешный 200 без degraded — MAJOR — fixed(2026-07-15)
- class: silent failure · location: `synapse/dispatcher/llm_client.py:91-99` · `synapse/dispatcher/loop.py:259-261` · `synapse/pipeline/webrtc_server.py:703-705` · found-by: H4
- symptom: если в `content` нет ни одного непустого `text`-блока и ни одного `tool_use`, `AnthropicLLMClient.complete` возвращает `("", [])`. Ниже по потоку это неотличимо от легитимного ответа: роут коммитит пустую `assistant`-запись в ленту и отдаёт `{"reply": ""}` со статусом 200 **без ключа `degraded`** — той же формы, что настоящий ответ. Соседние отказы в том же роуте (`CostCapBlocked`/`ProviderUnavailable`, webrtc_server.py:670-687) честно ставят `degraded: True` + журнальный алерт. Пустой ответ не получает ни того, ни другого.
- trigger: `POST /api/threads/{id}/message` любым текстом, провайдер отвечает 200 с вырожденным `content` (обрезанная генерация).
- expected vs actual: expected — принцип объявлен самим проектом в P2 (`e5dd0a4`): «пустой ответ провайдера не даёт ok=True» · actual — эта проверка приехала **только** в `tools/bench_llm_providers.py` (пинится `test_p2_run_task_empty_response_not_ok`), в боевой диспетчер — нет. На выходе `_complete()` в проде нет ни одной проверки (`webrtc_server.py:641` валидирует лишь ВХОДНОЙ текст).
- reachability: `webrtc_server.py:669` → `loop.py:218` (`tool_calls == []` → цикл `loop.py:225` не крутится) → `loop.py:261 return record, text` с `text == ""` → `webrtc_server.py:703-705`.
- evidence: llm_client.py:99 (`return "".join(text_parts), calls`); loop.py:259-261 (`if text:` — falsy просто не пишется в историю, сигнала об отказе нет); контраст webrtc_server.py:670-687 vs :703-705. Корроборация: `loop.py:352-356` (`_maybe_compact`) в том же файле явно защищается от пустой сводки (`(summary or "").strip() or "[история сжата]"`) — режим отказа известен и обработан рядом, но не на главном пути хода.
- fix(2026-07-15): пустая реплика на выходе хода → `{"reply": "", "degraded": True}` + алерт `ALL_TIERS_FAILED{reason: empty_response}`, как у соседних отказных веток. Чинится ровно заявленный баг (неотличимость), а не UX: подставлять человеческий фоллбэк-текст — продуктовое решение шире бага, отдельным тикетом (клиент сейчас `degraded` вообще не читает).
- fix-побочное (тот же дефект, вторая его половина): пустая `assistant`-запись БОЛЬШЕ не пишется в ленту. `history_from_feed` развернул бы её в assistant-сообщение с пустым текстом, а такой шейп Anthropic отвергает — одно молчание провайдера отравляло рехидрацию всего треда на холодном кэше.

### B-DISP-9 — поздний пасс падает после закоммиченного tool-вызова: degraded-200 противоречит реальному состоянию — MAJOR — fixed(2026-07-15)
- class: state machine (два состояния расходятся) · location: `synapse/dispatcher/loop.py:216-244` · `synapse/dispatcher/llm_client.py:132` · `synapse/pipeline/webrtc_server.py:668-687` · found-by: H2 (corroborated: H3 запарковал ровно это)
- symptom: `ingest_user_turn` крутит до `_MAX_TOOL_PASSES=5` пассов. Пасс 1 может выполнить мутирующий инструмент (`gate_action`/`submit_task`/`confirm_task`/`request_cancel`), эффект коммитится синхронно (напр. `_launch_run` реально стартует Кору, app.py:481-524). `_complete()` пасса ≥2 — тот же `GuardedLLMClient`, который резервирует слот в начале **каждого** вызова (llm_client.py:132) и нормализует сбой в `ProviderUnavailable`. Если на пассе 2 срабатывает дневной кап (правдоподобно: голос и текст делят один `CostCap`, а пасс 1 только что списал попытку) — исключение уходит наверх мимо всякого catch (loop.py:241-244) в роут.
- trigger: `POST /api/threads/{id}/message` («отправляй») при почти исчерпанном капе: пасс 1 → `gate_action(send_to_kora, confirm=true)` → Кора реально запущена (стадия сдвинута, таск RUNNING); пасс 2 → `CostCapBlocked` → `webrtc_server.py:670-687` → `{"reply": "Дневной лимит…", "degraded": True}`, 200.
- expected vs actual: expected — ответ отражает то, что произошло в ходе · actual — пользователю сказано «лимит, ничего не вышло», хотя ран уже запущен и исполняется; наивный повтор упирается в `{"error":"busy"}` (app.py:416-417) без всякой связи с «неудавшимся» предыдущим ходом.
- reachability: `webrtc_server.py:629` → `loop.py:180` → `loop.py:236` (диспатч коммитит) → `loop.py:237` (`_complete` бросает) → `webrtc_server.py:670/679`. Детерминировано при заданной последовательности.
- evidence: llm_client.py:132-133 (резерв-и-бросок на КАЖДОМ `complete()`, не только первом); loop.py:236-237 (диспатч строго до потенциально бросающего второго `_complete`); webrtc_server.py:670-687 (сплошной degraded-200 без упоминания уже закоммиченных tool-вызовов).
- fix(2026-07-15): обе отказные ветки роута сведены в одну точку `_degraded(idle_text, active_text, …)`, и реплику выбирает **правда стора** (`has_active_task()`), а не ветка исключения: при живой задаче ответ говорит «лимит/связь — договорить не могу, но задача запущена, не отправляй заново, спроси статус». Починены обе ветки, не только CostCapBlocked: корень один, у ProviderUnavailable то же враньё.

### B-BRIDGE-7 — gate_action(revise) идёт мимо busy-чека: стадия врёт при живом ране — MAJOR — fixed(2026-07-15)
- class: state machine · location: `synapse/pipeline/app.py:395-413` (revise) vs `:418-432` (send_to_kora/write_code — под `_launch_lock` + busy-чек) · found-by: H2
- symptom: ветка `revise` зовёт `set_stage(thread_id, "collect")` без `store.has_active_task()` и не трогает `kora_runner`. `_STAGE_TRANSITIONS` разрешает `code→collect` и `spec_plan→collect` (threads.py:22-28), так что переход проходит и при живом ране этого треда. `KoraRunner._run` снимает `_run_root`/`_run_gate_mode` один раз на старте (kora.py:501-505) — от `ThreadStore` он независим.
- trigger: `gate_action(send_to_kora, confirm=true)` запускает реальный ран, стадия → `code`. До финиша — `gate_action(action="revise")` (голос или текст, оба `user_initiated=False`, app.py:711-719): стадия → `collect`, `last_outcome=None`, pending инвалидирован, возвращается `{"ok": True, "stage": "collect"}` — при `store.task.status == RUNNING` того же треда.
- expected vs actual: expected — revise либо блокируется на занятом синглтоне, либо реально гасит ран · actual — UI показывает «COLLECT — сбор» (правила стадии буквально говорят «Не запускай Кору»), пока Кора продолжает писать файлы на диск и жечь `kora_max_budget_usd` по до-revise RunSpec.
- reachability: `tools.py:344 gate_action` → `on_gate` → `app.py:711/716` → `app.py:701 _gate_for` → `app.py:372` → revise-ветка `app.py:395`. Busy-чека на пути нет.
- evidence: app.py:395-413 (нет `has_active_task()`, в отличие от :421); kora.py:501-505 (снапшот на старте, иммунен к позднему `set_stage`).
- design-tension: `threads.py:180` документирует revise-во-время-дренажа явно («old completion is ignored») — т.е. сценарий предвидели и защитили **бухгалтерию**, но не сам ран. Отдельный `request_cancel` существует. Возможно, контракт «revise ≠ cancel» намеренный, и баг только во вранье стадии. Решение — за владельцем.
- fix(2026-07-15): выбран вариант «блокировать»: revise при `has_active_task()` → `{"error": "busy"}`, как соседние launch-ветки. Отмену живого рана revise НЕ выдумывает — это разрушительно (убивает работу Коры молча) и остаётся явным действием пользователя (`request_cancel`), после которого revise проходит. Минимальный вариант: гасит враньё, не присваивая себе продуктовых полномочий.
- **Поймано на верификации:** тест охоты был over-constrained — докстринг обещал «не пиню выбор между блокировать/отменять», а строка `assert result.get("ok") is True` де-факто запрещала «блокировать» (заблокированный revise отдаёт `{"error":"busy"}`, ключа `ok` там нет). Тест вернут писателю: утверждение сведено к самому инварианту (`not (stage=="collect" and has_active_task())`), докстринг приведён в соответствие. Та же форма, что у B-CASC-5: **тест утверждал больше, чем хотел, и молча пинил один из вариантов фикса.**

### B-BRIDGE-8 — ApprovalService: явное «нет» неотличимо от «ещё не ответил» — MINOR — fixed(2026-07-15)
- class: state machine (нет обязательного перехода) · location: `synapse/bridge/approvals.py:140-144` · found-by: H2 (corroborated: H4; H1 запарковал как намеренное)
- symptom: `consume()` сворачивает `deny` и `unclear` в один `None`. Caller (`app.py:430-432`) на `approval is None` безусловно пере-стажирует свежий pending и возвращает тот же `{"error": "confirm_required", "readback": …}` — независимо от того, отказал пользователь или промолчал. Перехода «отклонено» не существует вовсе.
- trigger: pending staged на треде; следующий ход содержит deny-слово («нет»); LLM зовёт `gate_action` с теми же аргументами → вотермарк проходит, affirm-проверка даёт `deny` → тихий re-stage вместо отмены.
- expected vs actual: expected — явное «нет» гасит pending, как у соседа: `ConfirmFlow.confirm()` (confirm.py:188-189) отдельно ловит `deny` → `_reset("хорошо, задачу отменяю")` · actual — pending перезаряжается со свежим TTL и задаётся ровно тот же вопрос.
- reachability: `tools.py:344` → `app.py:711-719` → `app.py:372-432` → `approvals.py:119-144`.
- evidence: approvals.py:141-144 (deny/unclear в одной ветке; комментарий оправдывает только половину про unclear); confirm.py:187-191 (у сиблинга ветка deny есть).
- design-tension: комментарий в коде утверждает намеренность («deny / unclear → pending НЕ гасится»). Запуска не происходит ни в одном случае — это UX/контрактная асимметрия, не дыра в авторизации. Отсюда MINOR, а не MAJOR. Решение — за владельцем.
- fix(2026-07-15): `deny` отделён от `unclear` — явное «нет» ГАСИТ pending (`_pending.pop`), как `_reset` у брата-механизма; `unclear` по-прежнему держит pending живым (повторный readback в gate_action — это половина комментария, которая была верна). Воскресить отклонённое может только новый `stage()`. Замороженные `test_deny_does_not_consume` / `test_unclear_does_not_consume_and_keeps_pending` зелёные без правок — фикс лёг ровно в зазор между ними.

### B-BRIDGE-9 — KoraRunner: read-side гейта без identity-guard, который есть на write-side — MAJOR — fixed(2026-07-15)
- class: concurrency/lifecycle · location: `synapse/bridge/kora.py:497-518` (снапшот 502-505 / очистка 512-517), `:578-586` (`_current_root`/`_current_gate_mode`), `:642-762` (`_gate_decision`) · found-by: H5
- symptom: `_run_root`/`_run_gate_mode`/`_run_model` — обычные поля инстанса, общие для всех ранов. `finally` в `_run()` защищён identity-guard-ом (`if self._run_owner == task_id`), чтобы вытесненный ран не затёр преемника — но guard стоит **только на стороне записи/сноса**. Сторона чтения (`_gate_decision`, вызываемая из PreToolUse-хука, который и есть граница containment) guard-а не имеет вовсе: у неё нет параметра `task_id`, и она доверяет тому, что сейчас лежит в полях.
- trigger: (1) тред T1 стартует `docs_only`-ран (root `rootX`). (2) `request_cancel` (`tools.py:309-318` → `kora.py:467-472`): `self._active.cancel()` только планирует отмену, не джойнит; `TaskStore.request_cancel` (state.py:322-328) ставит CANCEL_REQUESTED, а `has_active_task()` (state.py:224-228) считает это «не активен». (3) Busy-чек `app.py:421` немедленно пропускает новый запуск — тред T2, root `rootY`, `gate_mode="full"`. (4) `_run()` таска B перетирает поля (kora.py:502-505). (5) Если у таска A остался in-flight PreToolUse-хук (SDK диспатчит колбэк отдельной задачей через `_tg.start_soon`), он читает `"full"`/`rootY` вместо своих `"docs_only"`/`rootX`.
- expected vs actual: expected — tool-вызовы вытесненного рана судятся по ЕГО параметрам запуска либо отклоняются · actual — решение containment для таска A принимается по gate mode/root таска B: запись, которая должна быть `docs_only_violation`, может быть разрешена, и/или файл уедет в корень чужого проекта.
- reachability: `tools.py:313` → `kora.py:472` (fire-and-forget cancel) открывает окно; `app.py:421` (busy-чек уже False) → `app.py:452/475` → `kora.py:453 start()` → `kora.py:503-505` — второй конкурирующий вызов в этом окне. Обе точки достижимы рядовой парой действий (отмена, затем перезапуск).
- evidence: kora.py:502-505 (безусловная перезапись) против kora.py:513-517 (условная очистка под guard-ом) — guard ровно на одной стороне; kora.py:578-586 и `_gate_decision` не принимают `task_id` вообще; `tests/test_kora.py:624-649` пинит вытеснение по идентичности таска, но не корректность `_run_gate_mode`/`_run_root` в окне перекрытия.
- risk: премиса требует in-flight хука одновременно с отменой — детерминированный красный может не получиться. Если так → `not-test-verifiable` + ручная команда, а не подгонка теста. (Снят: красный получился детерминированным — через `asyncio.Event`, не гонку планировщика.)
- fix(2026-07-15): личность рана доносится до стороны чтения. `_build_options(task_id, …)` знает task_id ровно там, где строит хук → `HookMatcher(hooks=[functools.partial(self._pretool_hook, task_id=task_id)])`; `_pretool_hook(…, task_id=None)` сверяет личность ДО перехвата AskUserQuestion (вопрос — тоже действие: потерявший владение ран не должен парковать future и говорить с пользователем от имени несуществующей задачи) и передаёт её в `_gate_decision(tool_name, tool_input, task_id=None)`, где стоит гвард: `self._run_owner != task_id` → `(False, "superseded_run")`.
- **Fail-closed, а не «судить по своим старым правилам»:** восстанавливать снапшот вытесненного рана не нужно — ран, потерявший владение, доигрывается в отмену и права действовать не имеет вовсе. Это и проще, и строже. `task_id=None` гвард не включает — юнит-вызовы предиката (десятки в test_gate_v2/test_runspec/test_stages) ставят снапшот напрямую и остались зелёными без правок.
- **Поймано на верификации:** тест охоты был САМОПРОТИВОРЕЧИВ — утверждал `_run_root == root_b` (поля заняты задачей B) и тут же требовал, чтобы вызов БЕЗ личности ответил «docs_only_violation» за задачу A. Не выполнимо ничем: не получив личность, предикат не может знать, чей вызов судит. Тест возвращён писателю и переписан на новое API (`task_id="taskA"` → `superseded_run`, плюс проверка через настоящий хук: `permissionDecision == "deny"`). Дискриминация показана подклассом, отбрасывающим task_id: RED на старом поведении (`allowed_after=True`), GREEN на проде.

### Hunt 2026-07-15 (вечер) — Summary
- **7 находок** из 8 сырых репортов (слито 2 дубликата: `deny==unclear` принесли трое, «поздний пасс после коммита tool-а» — двое).
- **Severity:** 2 CRIT · 4 MAJOR · 1 MINOR. **Итог: все 7 `fixed`** (B-CASC-5 первым заходом, остальные 6 — вторым; см. «Фаза 3 — добито» ниже).
- **Проверено старшим лично** (не по пересказу охотника): B-CASC-5 — вендорный `service_switcher.py` прочитан, залипание подтверждено; B-DISP-8 — `llm_client.py:99` возвращает `("", [])`; B-BRIDGE-7 — revise-ветка без busy-чека; B-BRIDGE-6 — контраст `confirm.py` vs `approvals.py` вычитан построчно.
- **Сквозной паттерн:** *усиление приезжает в новый механизм, старый остаётся с исходной моделью угроз.* B-BRIDGE-6 (ConfirmFlow не получил тред-скоуп и вотермарк, которые получил ApprovalService), B-CASC-5 (фикс B04 закрыл R9 только для стартового тира), B-DISP-8 (проверка P2 приехала в бенчмарк, но не в прод). Три независимых охотника с разными линзами вышли на одну форму.
- **Не переоткрыто:** B-CASC-1/2/4, B-DISP-3/4/6, B-PIPE-3/4/5/6, B-BRIDGE-2 (open); B-CASC-3, B-DISP-5 (rejected) — гейт достижимости в брифе сработал, воскрешений нет.
- **B-CORE-2 (CRIT):** Thread never joined in record_commands.py — daemon thread left dangling

**Next phase:** dispatch test-writers (phase 2) to write red tests for CRIT/MAJOR bugs, verify via `proven` status, then fix (phase 3).

**Hunters:**
- H-PIPE (silent failures & error handling) — 6 bugs
- H-BRIDGE (concurrency, races & lifecycle) — 5 bugs
- H-DISP (state machines & illegal transitions) — 6 bugs
- H-CASC (data integrity & money correctness) — 4 bugs
- H-CORE (resource leaks & cleanup) — 6 bugs

---

## 📊 Hunt 2026-07-15 (свит 2) — верификация диффа origin/main..HEAD

**Scope:** невыпушенная работа (13 коммитов, 50 файлов, +9507/−1156). Сверил все 27 утверждений из `tests/test_*_reported_bugs_failing.py` с исходником и прогнал тесты (13 fail / 14 pass); плюс прошёлся по новому коду, которого в тех файлах нет (`speakable.py`, `approvals.py`, `tts_cache.py`, KV-1a/KV-2, `note_external_turn`).

**Итог по 27 отчётам первого свипа:**
- **3 настоящих бага** → доказаны красными тестами (ниже).
- **7 намеренного дизайна** — тесты спорят с принятым решением, обоснование в комментариях: B-PIPE-3 (monitor_forever не падает — единственный watchdog Р-15г), B-PIPE-4 (observer не пробрасывает, иначе роняет живое TTS), B-DISP-5 (зомби-UNREACHABLE честен, R6), B-CASC-2 (inclusive max — разумно), B-CASC-3 (состояние недостижимо), B-CORE-3 (закрыть журнал = потерять все будущие алерты), и rejects ниже.
- **3 сломанных теста** — падают в собственном сетапе, до кода не доходят: B-PIPE-1 (`NameError: name 'threads'`), B-DISP-1 (`FrozenInstanceError`), B-PIPE-5 (fragile lock/PipelineRunner mock).
- **2 фича-запроса**, не баги: B-DISP-3 (порядок элементов списка в args — семантическая интерпретация), B-CORE-2 (daemon-thread не join — косметика).
- подтверждённо починенные коммитом `272cf7f` и последующими: B-PIPE-2, B-PIPE-6, B-BRIDGE-1..5, B-DISP-2/4/6, B-CASC-1/4, B-CORE-1/5.

**Доказанные красными тестами (3):**
- **B-CORE-4** (MINOR → proven) — `TTSCache.__init__` не вычищает осиротевшие `.tmp`; `test_b_core_4_tts_cache_init_does_not_sweep_orphaned_tmp`.
- **B-CORE-6** (MAJOR → proven, диагноз уточнён) — `KoraRunner.start()` при `create_task` RuntimeError оставляет `_active` dangling на отменённом таске вместо `None`; `test_b_core_6_runner_active_not_cleared_when_create_task_raises`. Исходная формулировка H-CORE («await the cancelled task») была о другом — GC убирает отменённый таск нормально; реальная дыра — оборванная ссылка.
- **B-DISP-8** (MAJOR, новая — proven/parked) — `note_external_turn` дописывает в общую историю БЕЗ сверки поколения, асимметрия с C6/B20: `clear` + голосовой flush воскрешают очищенную историю; `test_b_disp_7_note_external_turn_revives_cleared_history`. parked до выбора API фикса.

**Канонические тесты:** `tests/test_bughunt_2026_07_15_failing.py` (3 red). Дублирующие/сломанные утверждения в `tests/test_new_reported_bugs_failing.py` (где часть тестов падает в собственном сетапе) следует при консолидации удалить, чтобы не держать два источника правды.

### Фаза 2+3 — итог прогона (2026-07-15, вечер)
- **Тесты:** `tests/test_hunt0715_money.py` (B-CASC-5, B-DISP-8, B-DISP-9) · `tests/test_hunt0715_auth.py` (B-BRIDGE-6..9). Все 7 красные на своих ассертах, прогнаны старшим лично.
- **Починен 1 (первый заход):** B-CASC-5 — `app.py:547-556`, условие `idx == 0` → `idx is not None`; дискриминатором остаётся `advanced_this_generation()`, который и так уже был написан рядом. Красный→зелёный доказан прямым откатом фикса. Регрессии B04/B21/costcap зелёные.
- **Доказаны, не починены (6):** xfail(strict=True) с указателем на реестр — суита зелёная, доказательство живо, а починка снимет xfail сама и strict закричит.

### Фаза 3 — добито (2026-07-15, второй заход): 6/6, xfail не осталось
Все шесть в зоне auth/money → чинил старший лично, ни один не отдан игроку. Суита **793 green / 1 xfailed** (B15, чужой). Порядок — от дешёвых к архитектурным; после каждого фикса полный прогон.
- **B-BRIDGE-8** — `deny` отделён от `unclear` (pop pending). Лёг ровно в зазор между двумя замороженными тестами, оба зелёные без правок.
- **B-DISP-9 + B-DISP-8** — обе отказные ветки роута сведены в `_degraded(...)`, реплику выбирает правда стора; пустой ответ метится `degraded` и больше не пишется в ленту.
- **B-BRIDGE-7** — busy-чек в revise («блокировать», не «отменять»: отмена разрушительна и остаётся явным действием пользователя).
- **B-BRIDGE-9** — личность рана доносится до стороны чтения гейта; чужой снапшот → fail-closed `superseded_run`.
- **B-BRIDGE-6 (CRIT)** — скоуп разговора сквозь ConfirmFlow + `KoraBridge.confirm_scope()`.

**Три из шести тестов охоты не могли позеленеть ни при каком корректном фиксе** — и это, а не сами баги, главный улов захода. Все три **утверждали больше, чем хотели**:
- B-BRIDGE-7 — докстринг обещал не пинить выбор фикса, а `assert ok is True` молча запрещал «блокировать»;
- B-BRIDGE-9 — тест был САМОПРОТИВОРЕЧИВ: требовал ответа за задачу A от вызова, не несущего личности вообще;
- B-BRIDGE-6 — на старом API сценарий «Б подтверждает задачу A» был НЕВЫРАЗИМ (параметра треда не существовало), поэтому доказательство кодировало лишь ОТСУТСТВИЕ скоупа.
Каждый вернулся писателю (Opus тестов не пишет). Настоящее кросс-тредовое доказательство B-BRIDGE-6 (`test_confirm.py`) написано ПОСЛЕ фикса, краснота показана подклассом со старым поведением. **Обобщение к уроку B-CASC-5: красный тест — это claim не только о коде, но и о себе. Фикс, отказавшийся зеленить свой тест, — сигнал, а не помеха.**
- **Цена API-фикса (честно):** скоупинг ConfirmFlow сломал 8 замороженных тестов — все мигрировали писатели, ассерты не тронуты, два теста стали СТРОЖЕ (security-posture хука пинит теперь и личность рана). Ещё один — `test_b_bridge_5` в бэклоге — был зелёным и упал сигнатурой мока: любой спай на `confirm_flow.submit` ломается об API. Мигрирован тем же порядком.
- **Поймано на верификации:** первая редакция теста B-CASC-5 была красной по НЕПРАВИЛЬНОЙ причине — слала 3 end-фрейма в ОДНОЙ генерации и требовала счёт 3. Это премиса B21 (двойной счёт), а не B-CASC-5; зазеленение такого теста вернуло бы B21. Возвращено писателю, премиса переписана на 3 генерации. **Урок ровно тот же, что у B-CASC-3: красный тест — это claim, проверять надо НА ЧЁМ он красный.**
- **Поймано на полной суите:** ID `B-DISP-7` уже был занят утренним заходом (`note_external_turn revives history`, proven). Мои находки перенумерованы в B-DISP-8/B-DISP-9. Ловится только прогоном ВСЕЙ суиты, не своих файлов.

### Backlog добит (2026-07-15, третий заход): 7 fixed, 3 rejected, 2 негодных теста заменены
Взят «легитимно открытый» остаток бэклога. **10 «багов» → 7 настоящих, 3 не-бага.** Отслеживаемая
суита **798 green / 1 xfailed** (B15, чужой). Красных в бэклог-файлах: 16 → 9, и каждый оставшийся
учтён (см. разбор ниже) — ни одного неизвестного.

**Починено (7):**
- **B-CORE-6** — `self._active = None` в except-ветке `start()`. Инвариант «_active это ЖИВОЙ ран либо None».
- **B-CORE-4** — `TTSCache._sweep_orphaned_tmp()` в `__init__`. Возраст не проверяем: `journal_dir`
  эксклюзивен для процесса (там же `state.json`), чужого ЖИВОГО tmp в корне быть не может.
- **B-CORE-2** — `waiter.join(timeout=1.0)` + конструктор потока вынесен ДО `try` (иначе finally
  ловил бы NameError). **Severity переоценён CRIT→MINOR:** реестр сам писал «not a leak that grows
  unbounded». Честная граница фикса: на пути, где `stream.read()` бросил, вейтер остаётся висеть в
  `input()` — join его не добудится (прервать `input()` в Python нечем), и он уйдёт драться за stdin
  со следующей фразой. Полное лечение = убрать `input()` из потока; за рамками бага, не заявлено сделанным.
- **B-CORE-3** — сбойная запись ЗАКРЫВАЕТ журнал (`_closed = True` + close + logger.error), пробрасывая
  дальше (контракт распространения не тронут — B39 ловит как ловил). Основание — **fsyncgate**: упавший
  fsync на Linux потребляет ошибку и может выбросить грязные страницы, следующий fsync вернёт УСПЕХ при
  уже потерянных данных. Для §8-евиденса денег/авторизации врать о долговечности хуже, чем замолчать;
  `_closed` уже несёт семантику тихого no-op (B28), запасной канал — logger.
- **B-DISP-7** — `clear_history` теперь ВЫКИДЫВАЕТ кэш треда, а не оставляет пустой список, + роут пишет
  clear-маркер в ленту ПЕРВЫМ и ВНУТРИ `turn_lock`. Порядок несущий (см. урок ниже).
- **B-PIPE-3** — `cost_cap.maybe_reset(now)` вынесен в ОТДЕЛЬНЫЙ guarded-шаг + `MONITOR_DEGRADED`
  один раз на серию из `_MONITOR_DEGRADED_AFTER=3` сбоев подряд (успех обнуляет). Цикл по-прежнему не умирает.
- **B-PIPE-4** — `TTSCacheObserver(cache, tts, journal=None)` + `TTS_CACHE_DEGRADED` один раз на серию.
  Ловим у САМОЙ записи в `_finalize`, не на уровне `on_push_frame`: обсервер зовут на каждый фрейм, и
  почти все кэша не касаются — любой успешный `TTSStartedFrame` сбрасывал бы анти-спам в флуд.

**Не баги — rejected (3). Все три «фикса» были бы ХУЖЕ «багов»:**
- **B-DISP-3** — охотник ошибся о самом Python: `{'a':1,'b':2} == {'b':2,'a':1}` → True, рекурсивно, и
  `2 == 2.0` → True. Единственное реальное различие — порядок СПИСКА, а он семантичен. Тест требовал,
  чтобы `[1,2]` и `[2,1]` дали дедуп-**HIT**; рядом в `_guarded` уже записано решение обратного трейдоффа:
  «a false dedup hit cannot be recovered». Ложный промах = инструмент отработал дважды; ложное попадание =
  легитимный вызов проглочен молча. Плюс ни одна схема инструмента не берёт вложенных аргументов.
- **B-CASC-2** — спор о значении слова «max», а не баг. `max=1` в конфиге значит «один платный вызов»;
  при эксклюзивной границе он значил бы НОЛЬ. Семантика пинится замороженным
  `test_b30_costcap_recovers_after_day_boundary`: `assert cap.record_paid_attempt(now=day0) is True
  # the tripping call is itself allowed`. Шаблон B-CASC-3 в чистом виде — денежная семантика по наводке имени.
- **B-PIPE-5** — misdiagnosis: `pop` в else-ветке уже стоит ЗА гвардом `current["session_id"] != session_id`,
  то есть ровно за случаем «B переиспользовал тот же sid» → ничего не выпадает, B не ломается. Охотник
  прочитал условие наоборот. Плюс тест падал `TypeError` в собственном сетапе, а не на ассерте.

**Два негодных доказательства заменены (та же болезнь, третий заход подряд):**
- **B-PIPE-3** — `test_reported_bugs_failing.py` требовал, чтобы монитор УМЕР с первым исключением. Это
  переоткрытие **B2** (замороженный тест назван прямым текстом: «monitor_forever dies permanently on any
  loop-body exception»), и хуже: мёртвый монитор гарантированно не тикает `maybe_reset` — то самое
  восстановление денег, которое баг и защищает. Реестр писал «halt ИЛИ escalate»; тест выбрал halt, не
  заметив, что escalate'ить в системе некому, а halt убивает деньги.
- **B-PIPE-4** — требовал `pytest.raises(OSError)` из `on_push_frame`, т.е. ровно того, что запрещает **R-1**
  («обсервер НИКОГДА не пробрасывает — уронило бы живое аудио») и что противоречит собственному «expected»
  этого же бага («alert ИЛИ disable gracefully»).
Оба возвращены писателям; новые красные — `tests/test_hunt0715_monitor.py`, `tests/test_hunt0715_tts_cache_alert.py`
(5 тестов, включая анти-спам-стражи и зеркало R-1). Старые негодные оставлены красными в бэклог-файлах вне гита.

**Урок захода — B-DISP-7: фикс инвалидации был бы регрессией без переупорядочивания.** «Воскрешение» было
не тем, чем звалось: `clear_history` делает `hist[:] = []`, старых реплик уже нет — дописывается ОДНА новая.
Настоящий инвариант: **тёплая история обязана совпадать с тем, что вернула бы холодная регидрация**, а
`history_from_feed` режет ленту по ПОСЛЕДНЕМУ clear-маркеру. Значит правду знает ЛЕНТА, и сделать
`note_external_turn` безусловным no-op было бы ошибкой — реплика, легшая ПОСЛЕ маркера, честно принадлежит
новому контексту. Отсюда фикс: уронить кэш → следующий читатель регидрируется из ленты → порядок «реплика vs
маркер» решает всё. НО: роут писал маркер ПОСЛЕ `clear_history` и ВНЕ лока — уронив кэш в этом окне, чтение
подняло бы из ленты НЕочищенную историю, отменило clear и закэшировало результат. **Инвалидация кэша без
переноса маркера внутрь лока = регрессия.** Ищи это в любом фиксе «сделаем кэш честнее».

### Backlog утреннего захода — разбор (не трогать вслепую)
13 красных тестов лежат вне гита (`tests/test_reported_bugs_failing.py`, `test_new_reported_bugs_failing.py`, `test_bughunt_2026_07_15_failing.py`). «13 красных» ≠ «13 багов»:
- **Чинить нельзя (2):** B-CASC-3, B-DISP-5 — `rejected`, премисы недостижимы. «Фикс» B-CASC-3 уже однажды открыл затрипленный дневной лимит.
- **Красный принадлежит тесту (2):** B-PIPE-1, B-DISP-1 — `fixed(worktree, UNVERIFIED: test crashes in its own setup)`.
- **Легитимно открыты (9):** B-PIPE-3/4/5, B-DISP-3, B-CASC-2, B-CORE-2 (CRIT), B-CORE-3, B-CORE-4 (proven), B-CORE-6 (proven).
