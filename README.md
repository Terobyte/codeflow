# CodeFlow

```
 ██████╗ ██████╗ ██████╗ ███████╗███████╗██╗      ██████╗ ██╗    ██╗
██╔════╝██╔═══██╗██╔══██╗██╔════╝██╔════╝██║     ██╔═══██╗██║    ██║
██║     ██║   ██║██║  ██║█████╗  █████╗  ██║     ██║   ██║██║ █╗ ██║
██║     ██║   ██║██║  ██║██╔══╝  ██╔══╝  ██║     ██║   ██║██║███╗██║
╚██████╗╚██████╔╝██████╔╝███████╗██║     ███████╗╚██████╔╝╚███╔███╔╝
 ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝     ╚══════╝ ╚═════╝  ╚══╝╚══╝

          a realtime coding agent that lives on YOUR machine
```

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![Tests](https://img.shields.io/badge/tests-646%20passing%20·%201%20xfail-brightgreen)
![Client](https://img.shields.io/badge/client-zero%20build%20step-orange)

**CodeFlow is a realtime coding agent.** Not "type a prompt, wait, refresh" — you *talk* to it, live, over WebRTC, and it writes real files into your real repos on your own machine. Every voice-coding demo you've seen runs in someone else's disposable cloud sandbox that forgets your venvs, your dotfiles, your half-finished branch. CodeFlow makes the opposite bet: the agent lives where your code lives, and you drive it from a PWA on your phone, from anywhere on your tailnet.

Realtime means realtime, in both directions:

- Your speech streams in over WebRTC; the agent's replies stream back as voice **into the same live call**.
- While the agent codes, you watch its thoughts, tool calls, and results in a **live activity feed** — and a live `git diff` of your project.
- When it finishes mid-call, the completion is **spoken into the call you're already on**.
- When it hits something ambiguous, it stops and **asks you, out loud**. You answer by voice; the answer is delivered verbatim into the *same running task*. No restart, no lost context.

## Two halves

| | What it is | Built on |
|---|---|---|
| **Flow** | The realtime half. Listens, talks, extracts what you actually want built through a staged conversation, and dispatches it. | pipecat-ai pipeline: Deepgram Flux STT → LLM cascade (OpenRouter `google/gemini-3.5-flash`, failover `claude-haiku-4-5` behind a circuit breaker + daily cost cap) → Fish Audio TTS |
| **Code** | The agent half. A real Claude Agent SDK session inside your project folder — reads, writes, edits, runs Bash. Every tool call passes a permission gate first. | `claude-agent-sdk`; per-run model picker: `claude-sonnet-5` (default), `claude-opus-4-8`, `claude-fable-5` |

```
                     ┌──────────────────────────────┐
                     │    you — phone, anywhere     │
                     │  CodeFlow PWA · installable  │
                     └──────────────▲───────────────┘
                                    ║
                                    ║  WebRTC · over your tailnet
                                    ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  FLOW — the realtime half                                        │
   │  Deepgram Flux STT ──► LLM cascade ──► arbiter ──► Fish TTS      │
   │  (gemini-3.5-flash ▸ failover haiku-4-5, breaker + cost cap)     │
   └───────────────┬──────────────────────────────────▲───────────────┘
                   │ task · your answers              │ status · questions
                   ▼                                  │ spoken completion
   ┌──────────────────────────────────────────────────┴───────────────┐
   │  CODE — the agent half                                           │
   │  a real Claude Agent SDK session inside your project folder      │
   └───────────────┬──────────────────────────────────────────────────┘
                   │ every single tool call
                   ▼
        ╔══════════════════════╗   secret paths ──────────► DENY
        ║   PreToolUse GATE    ║   WebFetch / WebSearch ──► DENY
        ║   fail-closed        ║   Bash ──► lexical scan + journal
        ╚══════════╤═══════════╝
                   │ allow
                   ▼
        your repo — real files · real git · live in the diff tab
```

## The workflow is a state machine, not a vibe

Every thread moves through a real FSM (`synapse/threads.py` — illegal transitions raise):

```
 COLLECT ───► PROPOSE ───► PLAN ───► CODING ───► DONE
    ▲            │           │          │
    └────────────┴───────────┴──────────┘
      revise — any working stage falls back to COLLECT
```

- **COLLECT** — Flow batches its clarifying questions (two rounds max), reads a summary back, and moves on only after your explicit "correct".
- **PROPOSE** — the request is frozen as text. Two buttons: **Send to Code** (plan first) or **Write code now** (straight to code — marked dangerous, takes a deliberate second tap).
- **PLAN** — Code runs in **docs-only gate mode**: during planning it is *physically* allowed to write only under `docs/` (plus top-level `.md`). The plan lands at `docs/plans/<thread_id>.md` in your repo. It cannot touch source yet, even if it wants to. Bash is fully denied in this mode.
- **CODING** — `write_code` refuses unless the plan file exists on disk *and* the planning run actually completed (`no_plan_file` / `stale_plan` guards). Then Code gets the full gate.
- **DONE** — set automatically when the code run completes.

The client renders the stage as a colored pill and only offers the buttons that are legal *right now*. Voice-initiated launches are stricter than clicks: a **two-key approval** — Flow stages a readback, you confirm in a separate turn, and the confirmation digest must still match the request, action, model, and stage. A tool call claiming `confirm=true` is not authority. Only you are.

One Code run at a time, enforced at the host. Runs carry a deadline (15 min default), a turn cap, and a per-task budget cap. Anti-zombie is structural: a `try/finally` terminalizes any still-RUNNING task on *every* exit path — cancel, timeout, crash, superseded run — so nothing ever hangs "running" forever.

## The gate

Code's boundary is a **PreToolUse hook** (`synapse/bridge/kora.py`) that fires for *every* tool the agent invokes and returns an explicit allow/deny. Not a permission-prompt callback — those fire only for some tools; that earlier version let a secret leak in testing and got ripped out. Current policy:

- **Secret paths denied everywhere**, casefolded (APFS is case-insensitive): `.env` and every `*.env`, `id_rsa` / `*.pem` / `*.key`, `~/.ssh` / `~/.aws` / `.kube`, shell rc files and histories, `credentials*`, `.npmrc`, `.claude.json`, … Directory-recursing tools (`Grep`/`Glob`/`LS`) get a subtree scan, so they can't slurp a secret the per-file check would have caught.
- **Bash allowed** — with a deny-only lexical scan of the command for secret tokens (`cat ~/.env` dies before it runs) and every allowed command written verbatim to the journal as an audit trail.
- **WebFetch / WebSearch: denied.** Unknown non-file tools: denied. Fail-closed by default.
- **NO-EXFIL backstop:** what gets *spoken* on completion is a template over your own task text — never the agent's output — so nothing Code reads in your workspace can inject itself into the TTS channel. Deny reasons handed back to the agent are category-only: no path-disclosure oracle.

Honest framing, same as the code comments say: this is armor against *accidents* — an agent tripping over your secrets — not against a determined adversary with an open shell.

## Flow is not allowed to make things up

The dispatcher's system prompt is a set of iron rules, and the test suite enforces them: the **only** source of truth about a task is the state block. Flow may not claim progress, invent file names, percentages, or ETAs. "No completion signal yet" is a correct answer, not a failure. Pressure ("come on, just estimate") changes nothing. Critical facts are spoken by Code through its own channel — Flow doesn't paraphrase them.

## The client

A hand-built PWA at `/client/` — three files served off disk (`index.html`, `app.js`, `style.css`) plus one vendored dependency. **No build step, no bundler, no node_modules.** The pipecat JS SDK is checked in as a self-contained ESM bundle (`@pipecat-ai/client-js` 1.12.0 + `small-webrtc-transport` 1.10.5, BSD-2-Clause — `synapse/pipeline/client/vendor/VENDOR.md`), and a test fails if a bare import ever sneaks in.

- **SPA with hash routing** (`#/` ↔ `#/thread/<id>`) — deliberately, so a live voice call *survives navigating* between threads.
- **Projects → threads sidebar**: a thread binds to a project folder; Code runs inside that folder.
- **Diff tab**: real `git status` + `git diff HEAD` from the thread's project root.
- **Code activity feed**: thoughts, tool calls, results, live.
- **Reconnect watchdog with zero wall-clock timers** (iOS freezes them when the phone locks): the client polls `GET /client/session-alive` and trusts the server's answer, reconnecting in place.
- Installable from the browser via web manifest — home-screen icon, full screen.

State-changing `/api/*` routes are JSON-only with same-origin CSRF checks. Thread metadata persists via atomic tmp+rename writes; feeds are append-only JSONL — everything survives a restart.

## Run it

Python 3.12+. Developed on macOS.

```bash
git clone https://github.com/Terobyte/codeflow.git && cd codeflow
python3.12 -m venv .venv
.venv/bin/pip install -e ".[voice,dev]"
.venv/bin/pip install claude-agent-sdk   # Code's engine — lazy-imported, not in the extras
cp .env.example .env                     # fill in your keys (names below)
.venv/bin/python -m synapse.pipeline.app # → http://localhost:7860/client/
```

Code also needs the Claude Code CLI reachable (`claude` on PATH, or point `KORA_CLI_PATH` at it).

| Variable | For |
|---|---|
| `OPENROUTER_API_KEY` | Flow's primary realtime LLM tier |
| `ANTHROPIC_API_KEY` | fallback tier + text-thread turns |
| `DEEPGRAM_API_KEY` | STT (Deepgram Flux) |
| `FISH_AUDIO_API_KEY`, `FISH_REFERENCE_ID` | TTS (Fish Audio) — bring your own voice reference |
| `GOOGLE_API_KEY` | optional, only for the provider benchmark below |

Optional knobs (defaults in `synapse/config.py`): `KORA_WORKSPACE_DIR` (default `~/synapse-kora-workspace`, used when a thread has no project), `KORA_MODEL`, `KORA_MAX_TURNS` (40), `KORA_MAX_BUDGET_USD` (1.0), `KORA_DEADLINE_S` (900), `KORA_ENABLED`, `FISH_TTS_MODEL`, `DISPATCHER_COMPACT_AFTER`.

### From your phone

```bash
tailscale serve --bg 7860
# → https://<your-machine>.<your-tailnet>.ts.net
```

Why Tailscale and not an HTTP tunnel: WebRTC media is UDP and doesn't ride through tunnels like cloudflared, and `tailscale serve` gives a stable HTTPS URL (a secure context, which `getUserMedia` demands) visible **only to your own devices**. Open it on the phone, add to home screen, talk, walk away from your desk. The code lands on your Mac anyway.

### No keys? Console demo

A deterministic text-in/text-out harness drives the same bridge/tools/journal code on a virtual clock — zero network:

```bash
.venv/bin/python -m synapse.runners.console --scenario tests/scenarios/demo.jsonl
```

## Tests

```bash
.venv/bin/pytest -q
# 646 passed, 1 xfailed, ~13 s
```

- Runs entirely **offline** — no network, no keys. The SDK stream is duck-typed, so tests feed scripted fakes; the LLM tiers are fakes; the clock is virtual.
- The client JS/HTML is tested by **lexical and structural assertions on the raw source** — deliberately zero browser or Playwright dependency in CI. The vendor-bundle test literally regexes for bare-specifier imports that would break zero-build serving.
- The one xfail is a `strict=True` pinned reproduction of a known, documented bug (cost-cap enforcement — `docs/bugs.md`). Fix the bug without updating the test and the suite fails loudly. Known bugs get regression armor, not silence.

## Benchmarking the LLM tiers

`tools/bench_llm_providers.py` sends the same 10 short tasks to direct Gemini and to OpenRouter, alternating endpoint order between rounds, and records transport success, contract success, retry recovery, and median/p95 full-response latency. Reports contain no keys, prompts, or generated text, and land in the gitignored `benchmarks/`:

```bash
.venv/bin/python tools/bench_llm_providers.py --attempts 3 --retries 1 --timeout 20
```

Reads `GOOGLE_API_KEY` (or `GEMINI_API_KEY`) and `OPENROUTER_API_KEY` from `.env`; pin the model pair with `GEMINI_MODEL` / `OPENROUTER_MODEL`. Add `--openrouter-no-fallback` to exclude OpenRouter's provider fallbacks from the measurement.

## Repo map

```
synapse/
  bridge/        Code runner (Claude Agent SDK), the permission gate, approvals, task state
  cascade/       LLM tier failover: circuit breaker, cost cap, classifier
  dispatcher/    Flow's brain: tool schemas, turn loop, LLM clients
  pipeline/      WebRTC server, voice pipeline assembly, TTS arbiter + cache, the PWA client
  threads.py     thread store + the stage FSM
  journal.py     append-only event journal (every gate decision is in here)
tools/           vendored-bundle build script, provider benchmark
tests/           the suite (647 tests)
```

## Fine print

- **Flow currently speaks Russian.** The dispatcher's system prompt and spoken lines are Russian-first (UI chrome is English). Swap `synapse/prompt.py` and the Fish voice reference for another language; the machinery doesn't care.
- **Internal codenames leak on purpose.** Code was born "Кора" (Kora) and Flow was "the dispatcher" inside the Synapse project — so the package is `synapse`, the env vars are `KORA_*`, and the module you run is `synapse.pipeline.app`. Renaming a working system the night before a deadline is how you get a broken system.
- v1 boundaries, stated plainly: one active Code run at a time; WebFetch/WebSearch denied; the gate is anti-accident, not anti-malice; the PWA installs but isn't offline-first (no service worker).
