# bugs.md

Severity: **CRIT** = money/data loss · security · crash · **MAJOR** = wrong behaviour on real input · **MINOR** = edge degradation.
Status: `reported` → `proven` | `rejected(reason)` | `not-test-verifiable(reason + manual cmd)`; `proven` → `fixed(commit)` | `parked(why)`.

## Hunt 2026-07-13 — UI/UX client (2 hunters, read-only, tree @ 24a307c)

Scope: `synapse/pipeline/client/{app.js,index.html,style.css}`, `synapse/pipeline/static/{status-widget.js,logs.html}`; server routes read as contract reference only. Namespace `B-UX-*` (prior hunt's `B-CORE-*`/`B-UI-*`/`Ж*` are inline already-fixed markers, untouched).

### B-UX-1 — watchdog auto-reconnect races the mic button → orphaned zombie voice session — MAJOR — reported
- class: concurrency/lifecycle · location: `synapse/pipeline/client/app.js:455-462` · found-by: H-A
- symptom: two `connectVoice()` run concurrently; one `PipecatClient` becomes an orphan whose WebRTC session stays live server-side with no client ref to close it — mic stays open with no UI indication.
- trigger: watchdog hits zombie-recovery (3 misses ≈15s "no session", line 452) → `client = null` (456) → `await c.disconnect()` (457, suspends) → user taps `#mic-btn` in that window: `if (connecting) return` (404) passes because `connecting` is still `false`, `if (client)` (405) is false because `client` was already nulled → mic handler starts its own `connectVoice()`. probeSession then resumes, sets `connecting = true` (458, too late) and calls `connectVoice()` (462).
- expected vs actual: the disconnect+reconnect sequence must be atomic w.r.t. other connect attempts (`connecting = true` before `client = null`/await) → only one live session · actual: guard flag set one `await` after the null-out, leaving a race window → two sessions, one leaked.
- evidence: ordering at 455-458 (`const c = client;` / `client = null;` / `await c.disconnect()…` / `connecting = true;`) — suspension point between null-out and guard.

### B-UX-2 — Enter key bypasses the send-disable guard → double thread / double message — MAJOR — reported
- class: concurrency (composer) · location: `synapse/pipeline/client/app.js:311-351` (esp. 316 vs 349-351) · found-by: H-A
- symptom: two fast Enter presses fire `sendMessage()` twice concurrently. Home view → two `POST /api/threads` create **two threads** with the same text; thread view → double `POST …/message` → duplicate feed entries and two LLM turns for one intent.
- trigger: type text, press Enter twice before the first `await postJSON(...)` (330) resolves.
- expected vs actual: `$("msg-send").disabled = true` (316) is meant to block re-submit · actual: `disabled` only suppresses the button's `click` (346); the keydown listener (349-351) calls `sendMessage()` directly with no `disabled`/flag check, and `input.value` isn't cleared until success (337) so both invocations read the same text.
- evidence: 349-351 (`if (e.key === "Enter" && !e.isComposing && e.keyCode !== 229) sendMessage();` — no guard) vs 316. Server has no dedup: `_launch_run`/`api_threads_create` create unconditionally per call.

### B-UX-3 — `gate_card` feed entry renders as a bare "· " — structured run-start card lost — MAJOR — reported
- class: rendering · location: `synapse/pipeline/client/app.js:251-253` + `synapse/pipeline/app.py:310-313` · found-by: H-B
- symptom: every run start (gate `send_to_kora` / `write_code`, happy path) appends a feed entry the client renders as literally `"· "` — no stage, model, or indication a run began.
- trigger: open a thread, confirm "Отправить Коре" / "Написать код". 100% reproducible on the main path.
- expected vs actual: a readable run-started card ("запуск: code · модель …") from the entry's `stage`/`action`/`model` fields · actual: server emits `{kind:"gate_card", stage, action, model}` with **no `text`** (app.py:310-313); `addEntry` has no `gate_card` branch → falls to else (251-253): `KIND_ICONS["gate_card"]` undefined → `"·"`, `e.text` undefined → `""` → `"· "`. No `.feed-gate_card` CSS rule either.
- evidence: app.py:310-313 (no `text` key); app.js:9-10 (KIND_ICONS lacks gate_card), 251-253 (fallback).
- related: `kind:"event"` ("правки → сбор", app.py:257-258) hits the same fallback — renders text but unstyled (no `.feed-event`). Lesser; fold into the fix.

### B-UX-4 — `feedKey` collision drops one of two parallel tool results from the thread feed — MAJOR — reported
- class: rendering/dedup (data-loss) · location: `synapse/pipeline/client/app.js:70,277-283` + `synapse/bridge/kora.py:255-256,448-452` + `synapse/pipeline/app.py:363-370` · found-by: H-B
- symptom: two genuinely distinct feed entries with the same `ts`+`kind`+`text` collapse to one client key; the second is silently skipped and never rendered.
- trigger: Kora returns two (or more) tool results in one `UserMessage` (parallel `Read`/`Bash` — common agentic pattern), both success (or both error).
- expected vs actual: both entries visible · actual: `kora.py:448` stamps one `ts` per SDK message; `_message_to_log_entries` (kora.py:227) gives every block that shared `ts`; ToolResultBlock text is coarse `"ок"`/`"ошибка"` (255-256) → two identical `{ts,"tool_result","ок"}`. `_kora_log_sink` mirrors them into the **thread** feed (app.py:363-370), where `pollFeed`'s `renderedKeys.has(feedKey(e))` (app.js:279) drops the duplicate.
- evidence: app.js:70 (`feedKey = ts|kind|text`), 277-283 (dedup `continue`); kora.py:448 (single `ts`), 255-256 (coarse text); app.py:363-370 (mirror to thread feed).
- note: harm bounded (lost entry is a low-value duplicate string), but feed fidelity is broken; same root also collapses any identical (kind,text) blocks in one message.

### B-UX-5 — `loadLists()` has no in-flight guard → stale poll stomps fresher data — MINOR — reported
- class: concurrency (poller race) · location: `synapse/pipeline/client/app.js:211-232` · found-by: H-A
- symptom: a slow earlier `loadLists()` resolving after a faster later one overwrites `threads`/`projects` (214-215) with the older snapshot; a just-created thread transiently vanishes from sidebar/home until the next poll.
- trigger: `setInterval(loadLists, 5000)` (555) starts a fetch (up to 15s, FETCH_TIMEOUT_MS); before it resolves, `sendMessage`'s `loadLists()` (338) resolves first and renders the new thread; the interval call then resolves and re-renders the pre-creation list.
- expected vs actual: state should reflect the latest server data (as `pollFeed`'s `feedInFlight` / `browse`'s `latestBrowse` already do) · actual: no in-flight flag or sequence token; last-to-resolve wins.
- evidence: 211-232 lack any guard, unlike 265-266 and 509/515.

### B-UX-6 — `route()` throws `URIError` on a malformed hash → render loop crashes each tick — MINOR — reported
- class: correctness/edge · location: `synapse/pipeline/client/app.js:45-48` · found-by: H-B
- symptom: `decodeURIComponent` on an invalid percent-escape throws uncaught inside `route()`, called by `render`/`pollFeed`/`threadCard`/`renderSidebar`/`renderHome`/`sendMessage` — none wrap it.
- trigger: navigate to `<origin>/#/thread/%E0` (corrupted/shared/hand-edited link).
- expected vs actual: graceful fallback (treat as unknown thread / home) · actual: `render()` throws on init and on every `hashchange`/interval tick while the hash stays malformed.
- evidence: 45-48 — `decodeURIComponent(m[1])` with no try/catch.

### Pattern P-A11Y — click-only, non-focusable controls + no focus management (B-UX-7…10)
Root: interactive elements built as `<li>`/`<div>` with only a `click` listener, plus modals with no focus move/trap/restore. Real gaps; low practical priority for a touch+voice single-user PWA, hence MINOR — but each is a genuine keyboard/AT dead-end.

### B-UX-7 — picker folder rows unreachable by keyboard/AT — MINOR — reported
- class: a11y · location: `synapse/pipeline/client/app.js:520-529` · found-by: H-B
- symptom/expected/actual: the "выбор папки проекта" up-folder and every subfolder are plain `<li>` with a `click` listener only — no `tabindex`/`role`/keydown; can't be tabbed to or Enter/Space-activated, so the add-project flow can't be completed without a pointer.
- evidence: 520-529 — `el("li", …)` + `addEventListener("click", …)`, no keyboard affordance anywhere for these rows.

### B-UX-8 — picker dialog claims `aria-modal` but has zero focus management — MINOR — reported
- class: a11y · location: `synapse/pipeline/client/index.html:69` + `app.js:505-506` · found-by: H-B
- symptom/expected/actual: `role="dialog" aria-modal="true"` tells AT the rest is inert, but `openPicker`/`closePicker` never move focus into the dialog, never trap Tab, never restore focus on close; siblings aren't `inert`/`aria-hidden`.
- evidence: index.html:69 (aria-modal); app.js:505-506 (no `.focus()` / trap anywhere).

### B-UX-9 — Kora status dot is a mouse-only control — MINOR — reported
- class: a11y · location: `synapse/pipeline/static/status-widget.js:13-28` · found-by: H-B
- symptom/expected/actual: injected into `/client/dev` (prebuilt fallback) as the only affordance to reach `/client/logs`, but it's a bare `<div>` with a `click` listener — no `tabindex`/`role`/keydown (unlike the SPA's `#kora-card`, a real `<a>`). Not focusable, Enter/Space do nothing.
- evidence: 13-28 — `createElement("div")` + click only.

### B-UX-10 — mobile drawer: no focus trap; off-canvas controls stay in tab order when closed — MINOR — reported
- class: a11y · location: `synapse/pipeline/client/style.css:220-225` + `app.js:480-484` + `index.html:18-39` · found-by: H-B
- symptom/expected/actual: the drawer is hidden only via `transform: translateX(-102%)`, which doesn't remove it from the tab order/AT tree; `<aside>` precedes `<main>`, so a keyboard user lands on invisible off-screen controls first. When open, Tab isn't trapped — focus escapes into the backdrop-covered `<main>` (not `inert`/`aria-hidden`).
- evidence: style.css:220-225 (transform only); app.js:480-484 (no `inert`/`tabindex`/`aria-hidden` toggling).

### Closed without fix
- **A1 rejected** — mic-btn disconnect branch (app.js:405-411) not fenced by `connecting`. Not a standalone bug: tap-off→tap-on is a legitimate user reconnect; the disconnect runs on the captured old client `c` while the connect builds a fresh `client`, and the identity-guard (`client === me`) neutralises the old client's late callbacks. A genuine double-`connectVoice()` requires the watchdog path — recorded as **B-UX-1** (shared root: disconnect not fenced by `connecting`).

### Parked (out of hunt scope / not a hard bug)
- unbounded `renderedKeys`/`#feed-list` growth on very long-lived threads (no pruning) — memory, not correctness.
- `#mic-btn` static `aria-label` across idle/connecting/on/error states — no state feedback to AT.
- tap targets `#side-close`/`#menu-btn` ~34-36px (<44px guideline); `pollStatus`/`picker-choose` lack in-flight guards (cosmetic flicker / low-risk double-POST).
