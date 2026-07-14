"""KoraRunner — the real Kora producer (M1 slice 1): on a task entering RUNNING (COMMITTED
from submit OR confirm), the host launches a Claude Agent SDK session in the configured
workspace directory, streams the SDK's messages, maps them with an allowlist table into
`KoraEvent`s (known → explicit class; unknown → NARRATABLE-quiet + log, NEVER critical —
R1-crit inversion Р-15б), and applies them to store/speak_ledger/journal, with critical facts
going to `on_speak` verbatim. This closes the producer hole that left every live voice task
stuck RUNNING forever (zombie).

Anti-zombie is STRUCTURAL, not exception-based (RISK-B1/B3/M4): `_run` is a try/finally whose
`finally` terminalizes a still-RUNNING task on ANY exit path — clean stream exit without a
terminal, CancelledError, a post-completion raise, a superseded stale run, or a watchdog
timeout. Every SDK task gets a FRESH per-task client (ALT-M2) so there is never a shared
persistent client to poison or double-consume.

The SDK import is LAZY (S4 idiom): this module imports fine without `claude-agent-sdk`
installed, and the message mapper duck-types by `type(msg).__name__` + hasattr — it never
`isinstance`-checks an SDK type, so tests feed plain scripted fakes with no network/SDK.

NB: `apply_event_to_store` deliberately mirrors `FakeKora.emit` (bridge/fake_kora.py, a
forbidden file this slice) — the critical⇒SPEAK pairing there is a SAFETY CLONE (Р-15г), not
boilerplate. Keep the two bodies in sync if either changes.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterator

from synapse.bridge.runspec import RunSpec
from synapse.bridge.state import EventClass, KoraEvent, SpeakLedger, TaskStatus, TaskStore
from synapse.clock import Clock
from synapse.config import SynapseConfig
from synapse.journal import AlertKind, TurnJournal

# The three lifecycle types that drive TaskStore._EVENT_STATUS (state.py). Everything else the
# mapper produces is a NARRATABLE liveness heartbeat only, never appended to task.events.
_LIFECYCLE_TYPES = frozenset({"task_started", "task_completed", "task_failed"})

# Fail-closed permission gate (RISK-M6): the single arg key holding the target path per tool.
# A tool absent from this map is not a file tool → Deny outright (slice 4 widens the policy).
_PATH_KEY = {
    "Write": "file_path",
    "Read": "file_path",
    "Edit": "file_path",
    "Glob": "path",
    "Grep": "path",
    "LS": "path",
    "NotebookEdit": "notebook_path",
}
# Read/search tools tolerate a missing path (they default to cwd, which is inside the
# workspace); a mutating tool with no path is fail-closed (Deny) — never let it default out.
_READ_SEARCH_TOOLS = frozenset({"Glob", "Grep", "LS"})

# UI-4 (docs_only): мутирующие файловые инструменты, сужаемые до docs-дерева и top-level md.
# Read/Glob/Grep/LS — читающие, docs_only их НЕ трогает; _SAFE_META_TOOLS тоже.
_MUTATING_FILE_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})

# Non-file tools that reach the network / a shell — named only for the gate's deny CATEGORY
# (they are already denied by the _PATH_KEY miss; naming does not change the outcome).
_EGRESS_TOOLS = frozenset({"Bash", "WebFetch", "WebSearch"})

# Non-file meta tools Кора legitimately needs (ToolSearch to load tools, TodoWrite to plan) —
# allowed explicitly BEFORE the egress/non_file fail-closed deny. Everything else non-file stays
# denied (Bash/WebFetch/WebSearch/Task/Skill/unknown): Кора edits files, no shell/net this slice.
_SAFE_META_TOOLS = frozenset({"ToolSearch", "TodoWrite"})

# Slice-4 secret containment (§2.8, BLOCKER-1): a secret INSIDE the workspace is still a
# secret — deny it for every file tool BEFORE the in-workspace allow. macOS APFS is
# case-INSENSITIVE (Read(".ENV") opens .env bytes while resolve() preserves the typed case),
# so every comparison below is CASEFOLDED. The secret VALUE is never read → never logged.
_SECRET_DIR_SEGMENTS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker", ".git", ".config", "keychains"})
# B22: exact-name matches accept rare false positives (a fixture literally named token.txt goes
# dark) — deny-only precedent set by "credentials".
_SECRET_FILE_NAMES = frozenset(
    {
        "credentials", "credentials.json", ".netrc", ".git-credentials", ".npmrc", ".pypirc",
        ".dockercfg", ".htpasswd", ".envrc",
        "secrets.yaml", "secrets.yml", "secrets.json", "secrets.toml", "token.txt", "tokens.txt",
        "apikey.txt", "api_key.txt", "service-account.json", ".pgpass", "settings.local.json",
        "local.settings.json",
        # UI v2 S12: запись в шелл-конфиг = persistence; ".config"-сегмент принимает редкие
        # false positives (deny-only, прецедент B22).
        ".zshrc", ".zshenv", ".zprofile", ".bashrc", ".bash_profile", ".profile",
    }
)
_SECRET_FILE_STEMS = frozenset({"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk"})
_SECRET_FILE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")  # str.endswith accepts a tuple
# Commit-safe templates that share the `.env.` prefix but carry no secret value.
_ENV_TEMPLATE_SUFFIXES = frozenset({".example", ".sample", ".template", ".dist", ".md"})


def _is_secret_path(p: Path) -> bool:
    """True when the resolved path names a secret file OR sits under a secret dir segment.
    ALL comparisons are casefolded (BLOCKER-1: case-insensitive APFS). `.env`, any
    `.env.<x>` except commit-safe templates, and any `*.env`; plain `environment.py`/`env.py`
    still never match."""
    if {seg.casefold() for seg in p.parts} & _SECRET_DIR_SEGMENTS:
        return True
    name = p.name.casefold()
    if name == ".env":
        return True
    if name.startswith(".env."):
        return name[4:] not in _ENV_TEMPLATE_SUFFIXES
    if name.endswith(".env"):
        # B22: prod.env / dev.env — any *.env basename is an env file; .env.example-style
        # templates don't end with ".env" so the exemption above is untouched.
        return True
    if name in _SECRET_FILE_NAMES:
        return True
    if name in _SECRET_FILE_STEMS:
        return True
    if name.endswith(_SECRET_FILE_SUFFIXES):
        return True
    return False


_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _snake(name: str) -> str:
    return _CAMEL_RE.sub("_", name).lower()


def _completion_text(task_text: str) -> str:
    return f"Задача выполнена: {task_text}"


def _failure_text(task_text: str) -> str:
    return f"Задача не выполнена: {task_text}"


def _blocks(content: Any) -> list[Any]:
    """SDK message content is either a plain string or a list of typed blocks; only the list
    form carries the blocks we map. Duck-typed, never isinstance on an SDK type."""
    return content if isinstance(content, list) else []


def _message_to_events(
    msg: Any, task_id: str, task_text: str, ts: float, seq_gen: Iterator[int]
) -> list[KoraEvent]:
    """Allowlist mapping table (§2.1). Builds every KoraEvent DIRECTLY with
    cls=EventClass.NARRATABLE — NEVER via parse_event, whose fail-safe default is CRITICAL.
    Nothing Kora streams is ever critical this slice, so there is zero risk of a false
    CRITICAL_WITHOUT_SPEAK by construction. Duck-typed on class name + attributes."""

    def mk(type_: str, payload: dict[str, Any], speak_text: str | None = None) -> KoraEvent:
        return KoraEvent(
            id=f"kora-{task_id}-{next(seq_gen)}",
            type=type_,
            cls=EventClass.NARRATABLE,
            payload=payload,
            speak_text=speak_text,
            ts=ts,
        )

    name = type(msg).__name__
    events: list[KoraEvent] = []

    if name == "SystemMessage":
        subtype = getattr(msg, "subtype", None)
        if subtype == "init":
            data = getattr(msg, "data", None) or {}
            events.append(mk("task_started", {"session_id": data.get("session_id"), "model": data.get("model")}))
        else:
            events.append(mk("kora_system", {"subtype": subtype}))

    elif name == "AssistantMessage":
        for block in _blocks(getattr(msg, "content", None)):
            bname = type(block).__name__
            if bname == "TextBlock":
                events.append(mk("assistant_text", {"text": (getattr(block, "text", "") or "")[:200]}))
            elif bname == "ToolUseBlock":
                # Log the tool NAME and its input KEYS only, never the values (privacy — a
                # Write's file_path/content is Kora's business, not the dispatcher context).
                inp = getattr(block, "input", None) or {}
                keys = sorted(inp.keys()) if isinstance(inp, dict) else []
                events.append(mk("tool_use", {"name": getattr(block, "name", None), "input_keys": keys}))
            elif bname == "ThinkingBlock":
                # The FACT that Kora thought, never the thought content.
                events.append(mk("thinking", {}))

    elif name == "UserMessage":
        for block in _blocks(getattr(msg, "content", None)):
            if type(block).__name__ == "ToolResultBlock":
                events.append(mk("tool_result", {"is_error": bool(getattr(block, "is_error", False))}))

    elif name == "ResultMessage":
        # The deterministic terminal signal is `is_error` (§2b live-validated): False → done,
        # True → failed. These are the ONLY two events that carry a SPEAK (verbatim to Kora's
        # voice) and drive a terminal store status.
        payload = {
            "num_turns": getattr(msg, "num_turns", None),
            "total_cost_usd": getattr(msg, "total_cost_usd", None),
        }
        if bool(getattr(msg, "is_error", False)):
            events.append(mk("task_failed", payload, speak_text=_failure_text(task_text)))
        else:
            events.append(mk("task_completed", payload, speak_text=_completion_text(task_text)))

    else:
        # Task*Message (Kora's INTERNAL subagents), RateLimitEvent, StreamEvent, anything
        # unknown → NARRATABLE-quiet liveness + journal. Prefixed `kora_` so it can never
        # collide with the lifecycle `task_started/completed/failed` that drive state.py.
        events.append(mk(f"kora_{_snake(name)}", {}))

    return events


def _message_to_log_entries(msg: Any, ts: float) -> list[dict[str, Any]]:
    """Display-only twin of `_message_to_events` — kora status UI (tero run 2026-07-12).
    Feeds ONLY the host's ring buffer behind GET /client/kora-log (the «размышления Коры»
    page); never the journal, store, [СОСТОЯНИЕ] or the dispatcher's LLM context, so Р-15
    is untouched by construction. Deliberate, dispositioned exception to the keys-only
    policy of `_message_to_events`: tool-input VALUES are shown here — anything Kora could
    put in an input she can equally say in a TextBlock (which this feed exists to display),
    secret paths are gate-denied BEFORE execution (slice 4), and the reader is the machine's
    owner over tailnet-only HTTP. Duck-typed like `_message_to_events`; unknown message
    types are skipped on purpose (Task*Message subagent spam would flood a 500-line display
    feed — full fidelity already lives in the journal via kora_* events)."""
    entries: list[dict[str, Any]] = []

    def add(kind: str, text: str) -> None:
        entries.append({"ts": ts, "kind": kind, "text": text})

    name = type(msg).__name__
    if name == "SystemMessage":
        subtype = getattr(msg, "subtype", None)
        if subtype == "init":
            data = getattr(msg, "data", None) or {}
            add("system", f"старт сессии, модель {data.get('model')}")
        else:
            add("system", str(subtype))

    elif name == "AssistantMessage":
        for block in _blocks(getattr(msg, "content", None)):
            bname = type(block).__name__
            if bname == "TextBlock":
                add("text", (getattr(block, "text", "") or "")[:4000])
            elif bname == "ThinkingBlock":
                add("thinking", (getattr(block, "thinking", "") or "")[:4000])
            elif bname == "ToolUseBlock":
                inp = getattr(block, "input", None) or {}
                try:
                    args = json.dumps(inp, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    args = str(inp)
                add("tool_use", f"{getattr(block, 'name', None)}: {args}"[:300])

    elif name == "UserMessage":
        for block in _blocks(getattr(msg, "content", None)):
            if type(block).__name__ == "ToolResultBlock":
                # B-UX-4: two parallel tool results in ONE UserMessage share this `ts` and the
                # coarse "ок"/"ошибка" text, so the client's ts|kind|text feedKey collapses them
                # and drops the second. Stamp the SDK's stable, unique `tool_use_id` on the entry
                # so feedKey can tell genuinely distinct results apart (stable across polls too).
                e: dict[str, Any] = {
                    "ts": ts,
                    "kind": "tool_result",
                    "text": "ошибка" if bool(getattr(block, "is_error", False)) else "ок",
                }
                tuid = getattr(block, "tool_use_id", None)
                if tuid is not None:
                    e["id"] = str(tuid)
                entries.append(e)

    elif name == "ResultMessage":
        text = "задача упала" if bool(getattr(msg, "is_error", False)) else "задача завершена"
        turns = getattr(msg, "num_turns", None)
        cost = getattr(msg, "total_cost_usd", None)
        if turns is not None:
            text += f" · ходов: {turns}"
        if isinstance(cost, (int, float)):
            text += f" · ${cost:.4f}"
        add("result", text)

    return entries


def apply_event_to_store(
    event: KoraEvent,
    store: TaskStore,
    speak_ledger: SpeakLedger,
    on_speak: Callable[[str], None] | None,
    journal: TurnJournal,
) -> None:
    """Mirror of FakeKora.emit (SAFETY CLONE, not boilerplate — see module docstring), with the
    ALT-M1 split: the journal gets EVERY event (full fidelity), but `store` gets only the coarse
    lifecycle — a lifecycle event via `store.apply_event` (which appends to task.events + sets
    the terminal status), everything else via `store.heartbeat` (liveness only, no append). This
    keeps the dispatcher's [СОСТОЯНИЕ]/snapshot from rendering all N of Kora's internal tool
    calls, which would blow up lean context and cost O(N²) re-serialization.

    Speak dispatch deliberately diverges from FakeKora.emit: lifecycle-only (NO-EXFIL backstop),
    while critical registration mirrors it for ALL events."""
    journal.record_kora_event(event)
    if event.type in _LIFECYCLE_TYPES:
        store.apply_event(event)
        # NO-EXFIL (slice 4): speak stays STRUCTURALLY lifecycle-only — a non-lifecycle event's
        # speak_text must never reach TTS even if a future producer sets one (workspace content
        # is injectable; completion-SPEAK is templated from task_text, never from Kora output).
        if event.speak_text:
            if on_speak is not None:
                on_speak(event.speak_text)
            speak_ledger.register_speak(event.id, event.ts)
    else:
        store.heartbeat(event.ts)
    # B20: critical registration is hoisted OUT of the lifecycle gate. A CRITICAL event that
    # only heartbeats the store must still arm the ledger — otherwise the Р-15г
    # CRITICAL_WITHOUT_SPEAK watchdog is blind to it (silent drop). Speak being structurally
    # lifecycle-only, a non-lifecycle CRITICAL now trips that alert LOUDLY instead of vanishing.
    # Deliberate, documented divergence from FakeKora.emit (see class docstring).
    if event.cls == EventClass.CRITICAL:
        speak_ledger.register_critical(event)


class KoraRunner:
    """Launches/streams one Claude Agent SDK task at a time (§1 single-active-task invariant).
    `client_factory` is the sole test seam: None → a lazily-imported real ClaudeSDKClient;
    tests inject a fake async-context client that yields scripted fake messages, so the whole
    runner is exercised with no network/SDK/classifier."""

    def __init__(
        self,
        cfg: SynapseConfig,
        store: TaskStore,
        speak_ledger: SpeakLedger,
        clock: Clock,
        journal: TurnJournal,
        on_speak: Callable[[str], None] | None,
        client_factory: Callable[[Any], Any] | None = None,
        log_sink: Callable[[dict[str, Any]], None] | None = None,
        on_run_finished: Callable[[str, str], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._speak_ledger = speak_ledger
        self._clock = clock
        self._journal = journal
        self._on_speak = on_speak
        self._client_factory = client_factory or self._default_client_factory
        # Display-only feed for the /client/logs page — kora status UI (tero run 2026-07-12).
        # None → feature unwired (tests, console); the runner never depends on it.
        self._log_sink = log_sink
        # UI-2 (находка G): колбэк исхода запуска → тред. None → feature unwired (тесты без тредов).
        self._on_run_finished = on_run_finished
        self._active: asyncio.Task[None] | None = None
        # M1 slice 3 (E5): while Kora's stream is blocked in the gate on an AskUserQuestion, this
        # holds the future the dispatcher's answer_kora resolves. None whenever no question is
        # parked. Its ENTIRE lifecycle (null + store-flag clear) lives inside `_handle_question`'s
        # try/finally under an identity guard, so a superseding run never clobbers the successor's
        # question (MAJOR-C1); `_run`'s finally stays terminalize-only.
        self._pending_answer: asyncio.Future[str] | None = None
        # UI-2 (спека §3, находка B): per-run снапшот launch-параметров. Ставится в начале
        # _run ДО создания клиента; ЕДИНСТВЕННЫЙ источник корня для _build_options /
        # _system_prompt / _gate_decision на время рана. Владелец = task_id (identity-guard,
        # как у _pending_answer): finally суперсиженного рана не трёт снапшот преемника.
        self._run_owner: str | None = None
        self._run_root: Path | None = None
        self._run_model: str | None = None
        # UI-4 (docs_only): четвёртый слот снапшота тем же паттерном. None = «рана нет»
        # (честный сентинел, как _run_root); НЕ ставить дефолт "full" в слот, иначе
        # is-not-None всегда истинно и зеркало ломается. ПЕРВЫЙ читатель spec.gate_mode.
        self._run_gate_mode: str | None = None

    # --- launch / cancel (host-facing) -----------------------------------------------------

    def start(self, task_id: str, text: str, spec: RunSpec | None = None) -> None:
        """Fire-and-forget launch on the live async loop. Supersede (cancel) any lingering run
        rather than refuse — refusing would leave the new task's store entry RUNNING with no
        producer (a zombie). If there is no running loop (console + kora_enabled, or a sync
        test), create_task raises RuntimeError → terminalize so we never strand RUNNING."""
        if self._active is not None and not self._active.done():
            self._active.cancel()
        coro = self._run(task_id, text, spec or RunSpec(thread_id=""))
        try:
            self._active = asyncio.create_task(coro)
        except RuntimeError:
            coro.close()  # no running loop — close the coroutine so it isn't "never awaited"
            self._terminalize_if_running(task_id)

    def request_cancel(self) -> None:
        """Wired to the dispatcher's request_cancel (RISK-B2 proper): cancelling `_active`
        propagates into the SDK stream's async-with, tearing down the CLI subprocess — so
        «отмени задачу» actually stops Kora, not just frees the slot."""
        if self._active is not None and not self._active.done():
            self._active.cancel()

    def provide_answer(self, text: str) -> bool:
        """Host-facing (wired to the dispatcher's answer_kora tool via KoraBridge.on_answer):
        deliver the user's reply, verbatim, to the parked AskUserQuestion. Clears the store's
        awaiting flag SYNCHRONOUSLY BEFORE `set_result` (R5) so there is no resume-gap window in
        which [СОСТОЯНИЕ] still shows «ждёт ответа» after the answer was accepted. Returns True
        iff a question was actually pending — False lets the tool report `no_pending_question`."""
        fut = self._pending_answer
        if fut is not None and not fut.done():
            self._store.clear_awaiting()
            fut.set_result(text)
            return True
        return False

    # --- run / stream ----------------------------------------------------------------------

    async def _run(self, task_id: str, text: str, spec: RunSpec | None = None) -> None:
        # Снапшот АТОМАРНО до создания клиента (спека §3): резолв project_root|null → путь
        # происходит ровно один раз, здесь. None → дефолтный RunSpec (обратная совместимость
        # существующих тестов, зовущих _run без spec).
        spec = spec or RunSpec(thread_id="")
        root = Path(spec.project_root) if spec.project_root else self._workspace()
        self._run_owner, self._run_root = task_id, root
        self._run_model = spec.model or self._cfg.kora_model
        self._run_gate_mode = spec.gate_mode or "full"
        try:
            await asyncio.wait_for(self._stream(task_id, text), self._cfg.kora_deadline_s)
        except Exception as exc:  # noqa: BLE001 — includes TimeoutError; CancelledError is a
            # BaseException and is NOT caught here, so a cancel/shutdown propagates while the
            # finally below still terminalizes. Any real error is alerted, never swallowed.
            self._journal.alert(AlertKind.KORA_RUN_FAILED, {"task_id": task_id, "error": repr(exc)})
        finally:
            if self._run_owner == task_id:  # identity-guard: не трогать снапшот преемника
                self._run_owner = None
                self._run_root = None
                self._run_model = None
                self._run_gate_mode = None
            self._terminalize_if_running(task_id)
            # UI-2 (находка G): исход запуска → тред. Источник — терминальный статус стора
            # ПОСЛЕ terminalize; чужой task в сторе (суперсид) → исход не наш, молчим.
            if self._on_run_finished is not None and spec.thread_id:
                task = self._store.task
                if task is not None and task.id == task_id:
                    outcome = {
                        TaskStatus.COMPLETED: "completed",
                        TaskStatus.FAILED: "failed",
                    }.get(task.status, "cancelled")
                    self._on_run_finished(spec.thread_id, outcome)

    async def _stream(self, task_id: str, text: str) -> None:
        opts = self._build_options(task_id, text)
        seq_gen = itertools.count()
        # Both log-sink insertions below swallow EVERY exception on purpose (kora status UI,
        # tero run 2026-07-12): the sink is display-only, and anything escaping _stream lands
        # in _run's broad `except Exception` → KORA_RUN_FAILED + terminalize — a cosmetic bug
        # must never mark a genuinely running task FAILED. Degradation is silent by design:
        # the /client/logs feed freezes while the traffic light (a separate path) keeps working.
        if self._log_sink is not None:
            try:
                self._log_sink({"ts": self._clock.now(), "kind": "task", "text": text, "task_id": task_id})
            except Exception:  # noqa: BLE001
                pass
        async with self._client_factory(opts) as client:
            await client.query(text)
            async for msg in client.receive_response():
                # A superseded/stale run must not write into whatever task now occupies the
                # store (RISK-B2): bail the moment the store's task is gone or has a different id.
                if self._store.task is None or self._store.task.id != task_id:
                    return
                ts = self._clock.now()
                if self._log_sink is not None:
                    try:
                        for entry in _message_to_log_entries(msg, ts):
                            self._log_sink({**entry, "task_id": task_id})
                    except Exception:  # noqa: BLE001
                        pass
                for event in _message_to_events(msg, task_id, text, ts, seq_gen):
                    apply_event_to_store(event, self._store, self._speak_ledger, self._on_speak, self._journal)

    def _terminalize_if_running(self, task_id: str) -> None:
        """Structural anti-zombie core: only touches a task that is STILL this task and STILL
        RUNNING. No-op if the store moved on (superseded), already terminal (COMPLETED/FAILED),
        or cancel-requested (CANCEL_REQUESTED — slot already free). set_task_status is itself
        guarded (state.py) so it can never resurrect a finished task."""
        task = self._store.task
        if task is not None and task.id == task_id and task.status == TaskStatus.RUNNING:
            self._store.set_task_status(TaskStatus.FAILED)

    # --- options / gate --------------------------------------------------------------------

    def _workspace(self) -> Path:
        raw = self._cfg.kora_workspace_dir or os.path.expanduser("~/synapse-kora-workspace")
        return Path(raw)

    def _current_root(self) -> Path:
        """Один корень на все три головы (спека §3): во время рана — снапшот RunSpec;
        вне рана (юнит-вызов options/гейта без _run) — конфиг-дефолт."""
        return self._run_root if self._run_root is not None else self._workspace()

    def _current_gate_mode(self) -> str:
        """Зеркало _current_root: во время рана — снапшот gate_mode; вне рана — 'full'
        (fail-open корректен для docs_only — это сужение, а не замена)."""
        return self._run_gate_mode if self._run_gate_mode is not None else "full"

    def _system_prompt(self, workspace: Path, task_text: str) -> str:
        # LOAD-BEARING (§2d CASE 1): with setting_sources=[] Kora does not otherwise know its
        # cwd and will invent absolute paths that the gate then (correctly) denies. Naming the
        # workspace + steering to Write/Edit (not shell) is what made the live smoke succeed.
        return (
            f"Ты — Кора, исполнитель задач Синапса. Твоя рабочая директория: {workspace}. "
            f"Создавай и изменяй файлы только внутри неё, используя инструменты Write/Edit, "
            f"а не команды shell. Не обращайся к абсолютным путям за пределами этой директории. "
            f"Задача пользователя: {task_text}"
        )

    def _build_options(self, task_id: str, text: str) -> Any:
        """Builds ClaudeAgentOptions with the slice-1 security posture (§2c/§2d): cwd=workspace,
        permission_mode='default', allowed_tools=[] (a tool in allowed_tools SHADOWS the gate —
        proven by CanUseToolShadowedWarning), setting_sources=[] (user/project settings also
        shadow the gate). The authoritative boundary is a PreToolUse HOOK, not can_use_tool
        (slice-4 repair): can_use_tool is a PERMISSION-PROMPT callback that fires ONLY for
        permission-requiring tools — Read/Glob/Bash bypassed it and a secret leaked — whereas a
        PreToolUse hook fires for EVERY tool, so the gate runs on all of them. SDK import is lazy
        so the module loads without the package."""
        from claude_agent_sdk import ClaudeAgentOptions
        from claude_agent_sdk.types import HookMatcher

        workspace = self._current_root()
        workspace.mkdir(parents=True, exist_ok=True)
        return ClaudeAgentOptions(
            cwd=str(workspace),
            permission_mode="default",
            allowed_tools=[],
            disallowed_tools=[],
            setting_sources=[],
            hooks={"PreToolUse": [HookMatcher(hooks=[self._pretool_hook], timeout=None)]},
            model=self._run_model or self._cfg.kora_model,
            cli_path=self._cfg.kora_cli_path,
            max_turns=self._cfg.kora_max_turns,
            max_budget_usd=self._cfg.kora_max_budget_usd,
            system_prompt=self._system_prompt(workspace, text),
        )

    def _gate_decision(self, tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str | None, str]:
        """Pure fail-closed containment decision (RISK-M6, slice-4 hardened). Returns
        (allowed, detail, category) — an EXPLICIT category, never string-parsed by the caller.
        A non-file tool is denied (egress/non_file_tool); a mutating file tool with no/blank
        path is denied (missing_path); a resolved secret path is denied for ALL file tools even
        inside the workspace (secret_path, checked BEFORE the in-workspace allow); a path is
        allowed only when its resolved form is inside the workspace via
        Path.resolve().is_relative_to (NOT startswith — that would leak the sibling /ws-evil),
        else outside_workspace. Categories: allow/secret_path/outside_workspace/missing_path/
        egress/non_file_tool/path_error."""
        key = _PATH_KEY.get(tool_name)
        if key is None:
            if tool_name in _SAFE_META_TOOLS:
                return True, None, "allow_meta"
            cat = "egress" if tool_name in _EGRESS_TOOLS else "non_file_tool"
            return False, f"{cat}: {tool_name}", cat
        raw = (tool_input or {}).get(key)
        if not isinstance(raw, str) or not raw.strip():
            if tool_name in _READ_SEARCH_TOOLS:
                return True, None, "allow"  # defaults to cwd, which is inside the workspace
            return False, f"missing_path: {tool_name}", "missing_path"
        workspace = self._current_root()
        p = Path(raw)
        if not p.is_absolute():
            p = workspace / p
        try:
            resolved = p.resolve()
            ws_resolved = workspace.resolve()
        except (OSError, RuntimeError, ValueError):
            return False, "path resolution failed", "path_error"
        # Secret containment runs on the FULL resolved path, BEFORE the in-workspace allow, so a
        # secret living inside the workspace (workspace/.env, workspace/.ssh/id_rsa) is still
        # denied. B21: the deny detail is category-only — it becomes the agent-facing
        # `permissionDecisionReason`, and a prompt-injectable Кора must not be handed the resolved
        # absolute path (home dir / username / secret-file layout disclosure oracle).
        if _is_secret_path(resolved):
            return False, "secret_path", "secret_path"
        if resolved.is_relative_to(ws_resolved):
            # UI-4 (docs_only): сужение ПОСЛЕ секрет-чека и in-workspace. Мутирующий инструмент
            # разрешён только в поддереве <ws>/docs/ ИЛИ в top-level .md файле корня; всё остальное
            # — deny (Р3 whitelist docs-путей). Читающие инструменты не трогаются. Категория-only
            # в detail — прецедент B21 (не светить resolved-путь агенту).
            if self._current_gate_mode() == "docs_only" and tool_name in _MUTATING_FILE_TOOLS:
                in_docs = resolved.is_relative_to(ws_resolved / "docs")
                top_md = resolved.parent == ws_resolved and resolved.suffix == ".md"
                if not (in_docs or top_md):
                    return False, "docs_only_violation", "docs_only_violation"
            return True, str(resolved), "allow"
        return False, "outside_workspace", "outside_workspace"

    async def _pretool_hook(self, input_data: dict[str, Any], tool_use_id: Any, context: Any) -> dict[str, Any]:
        # The ONE gate (slice-4 repair): a PreToolUse hook fires for EVERY tool Кора invokes,
        # unlike can_use_tool which only fired for permission-requiring tools (Read/Glob/Bash
        # slipped past it and a secret leaked). It returns an EXPLICIT allow/deny hook dict per
        # tool — a bare {} would be "no-decision" and headless-BLOCK mutating tools.
        name = input_data.get("tool_name")
        tinput = input_data.get("tool_input") or {}
        # E5 (§2b): AskUserQuestion is Кора asking the USER something mid-task. It has no path key
        # → the fail-closed policy would Deny it. Intercept FIRST and turn it into the interactive
        # block that parks the stream until the dispatcher delivers the answer.
        if name == "AskUserQuestion":
            return await self._handle_question(tinput)

        allowed, detail, category = self._gate_decision(name, tinput)
        self._journal.record_kora_event(
            KoraEvent(
                id=f"kora-gate-{int(self._clock.now() * 1000)}",
                type="gate_allow" if allowed else "gate_deny",
                cls=EventClass.NARRATABLE,
                payload={"tool": name, "detail": detail, "category": category},
                speak_text=None,
                ts=self._clock.now(),
            )
        )
        if allowed:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": detail or category,
            }
        }

    @staticmethod
    def _build_question_prompt(questions: list[dict[str, Any]]) -> str:
        """Voice the primary question + its option labels + the free-form invitation (§2b: the
        user may pick a label OR answer in their own words — the CLI does not validate
        answer ∈ labels). This text goes to on_speak ONLY, never into [СОСТОЯНИЕ] (Р-8/Р-15)."""
        if not questions:
            return "Кора задаёт уточняющий вопрос. Ответь своими словами."
        q = questions[0]
        text = str(q.get("question") or "").strip()
        labels = [str(o.get("label", "")).strip() for o in (q.get("options") or []) if o.get("label")]
        parts = [text] if text else []
        if labels:
            parts.append("Варианты: " + ", ".join(labels) + ".")
        parts.append("Или ответь своими словами.")
        return " ".join(parts)

    async def _handle_question(self, tool_input: dict[str, Any]) -> Any:
        """The E5 interactive gate branch (§2b). Parks Kora's stream on a future until the
        dispatcher's answer_kora resolves it, then returns the answer verbatim into
        `updated_input.answers` (the SDK applies it and Kora continues the SAME task — 0 slice-1
        rework). ALL cleanup of the future + store flag is localized here under an identity guard
        so a superseded run never touches a successor's question (MAJOR-C1)."""
        questions = (tool_input or {}).get("questions") or []
        spoken = self._build_question_prompt(questions)
        # Keys-only journaling (P4/R4): the count, never the question text (matches slice-1 logging).
        self._journal.record_kora_event(
            KoraEvent(
                id=f"kora-question-{int(self._clock.now() * 1000)}",
                type="kora_question_asked",
                cls=EventClass.NARRATABLE,
                payload={"num_questions": len(questions)},
                speak_text=None,
                ts=self._clock.now(),
            )
        )
        # Order matters: the future must exist BEFORE the awaiting flag becomes visible, so the
        # dispatcher (which only routes to answer_kora after seeing awaiting in [СОСТОЯНИЕ]) can
        # never resolve a not-yet-created future.
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending_answer = fut
        self._store.set_awaiting()
        if self._on_speak is not None:  # P6 None-guard
            self._on_speak(spoken)
        try:
            answer_text = await fut
        finally:
            # Identity guard (MAJOR-C1): only clean up if THIS invocation still owns the slot. A
            # superseding run that overwrote `_pending_answer` must keep its own state intact.
            if self._pending_answer is fut:
                self._pending_answer = None
                self._store.clear_awaiting()
        # Verbatim (§2.9): the user's reply goes UNTOUCHED into every question key, label or
        # free-form alike (§2b — the CLI does not validate it against the options).
        # B38: a malformed/hallucinated AskUserQuestion may omit "question" — skip such entries
        # (use .get, like _build_question_prompt) instead of KeyError-ing AFTER the user already
        # answered, which would fail the task and discard the reply.
        answers = {
            q["question"]: answer_text
            for q in questions
            if isinstance(q, dict) and q.get("question")
        }
        self._journal.record_kora_event(
            KoraEvent(
                id=f"kora-answer-{int(self._clock.now() * 1000)}",
                type="kora_question_answered",
                cls=EventClass.NARRATABLE,
                payload={"num_questions": len(questions)},
                speak_text=None,
                ts=self._clock.now(),
            )
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {"questions": questions, "answers": answers},
            }
        }

    def _default_client_factory(self, opts: Any) -> Any:
        from claude_agent_sdk import ClaudeSDKClient

        return ClaudeSDKClient(opts)
