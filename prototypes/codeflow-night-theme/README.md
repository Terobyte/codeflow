# CodeFlow theme prototype

Standalone, backend-free UI/UX prototype for the CodeFlow redesign. It does not import or
modify the production client (`synapse/pipeline/client/`), but it is a **1:1 functional
mirror** of it: every control here corresponds to a real feature of the production app, and
every production feature has a counterpart here. No invented buttons, no fake metrics.

Open `index.html` directly, or serve this folder with any static HTTP server.

## Functional parity with the production client

- routes `#/`, `#/thread/<id>`, `#/activity`; a thread always opens on the Chat tab;
- sidebar: New task (home + focus composer), project tree with thread branches, loose
  threads under "No project", add project via folder picker, delete project (confirm,
  threads survive), archive thread (confirm), active-project toggle;
- composer is global: the first message from home creates a thread in the active project;
  project chip (home only) shows where the thread will be born; Enter sends,
  Shift+Enter — new line;
- topbar: thread title (click to rename inline), outcome badge (✓/✖/⏹), stage chip,
  Chat/Diff tabs; the stage rail in the thread is display-only — stages are a server FSM
  and move only through gate cards;
- feed kinds: user/Flow/Code messages (play TTS stub on agent messages), thinking and
  tool collapsibles, ▶ task, 🏁 result, gate cards with model select, double-tap confirm
  on dangerous actions, and the inactive "stage changed" state;
- Diff tab: changed-file chips + unified diff (add/del/hunk coloring), empty state;
- activity page: Code status scene + tool journal (mirrors kora-status / kora-log);
- voice: mic button states idle→connecting→on, "Flow · realtime" switch gates the start
  (never the hang-up), live overlay with listening/replying states, mute, "End — back to
  chat", Escape ends the call;
- Code status card in the sidebar links to the active thread while running, otherwise to
  the activity page.

## Spec preview — settings, files, radio

These screens are not in the production client yet; they preview the approved design spec
`docs/superpowers/specs/2026-07-15-synapse-kora-voice-files-radio-design.md`:

- `#/settings/ai` (gear in the sidebar footer): Appearance (the Night Atlas / Hero Drive
  style switch — instant, per-device), Dispatcher providers (enabled switch, read-only key
  status with env name + mask, model select, Test), manual primary/fallback route with the
  "fallback = different provider" rule and a warning when the route model diverges from
  the provider's selected model, Kora defaults (Claude Agent SDK read-only, model,
  max turns, budget, deadline), Voice read-only (env-backed, editor owned by Settings →
  Voice M+1). Unsaved changes show a revision save bar (CAS revision model, no auto-merge);
- feed `kind="file"` card (deliver_file artifact): name, size, mime, Download (Blob →
  object URL, mirroring the bearer auth-fetch), Listen for text files;
- radio: sticky player bar in the thread (narrator voice, "fragment N of M", play/pause,
  stop), bookmark saved on each fragment end → "Continue · fragment N" on the file card;
  mic and radio are mutually exclusive (mic tap pauses radio, radio start hangs up the
  call), a feed Play button also pauses radio — one audio at a time.

## Implementation handoff

The CSS `:root` block is the Night Atlas token contract; `.hero-mode` contains the Hero
Drive overrides. Both themes share the same component geometry. Layout breakpoints are
1050px and 760px. All state and handlers live in `app.js` on top of plain data arrays —
production integration replaces those arrays with the existing `/api/*` calls and polling
without changing the visual components.
