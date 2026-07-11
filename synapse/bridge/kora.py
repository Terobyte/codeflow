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
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterator

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
    calls, which would blow up lean context and cost O(N²) re-serialization."""
    journal.record_kora_event(event)
    if event.type in _LIFECYCLE_TYPES:
        store.apply_event(event)
        # Never fires this slice (nothing is critical), but the pairing is a Р-15г safety clone.
        if event.cls == EventClass.CRITICAL:
            speak_ledger.register_critical(event)
        if event.speak_text:
            if on_speak is not None:
                on_speak(event.speak_text)
            speak_ledger.register_speak(event.id, event.ts)
    else:
        store.heartbeat(event.ts)


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
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._speak_ledger = speak_ledger
        self._clock = clock
        self._journal = journal
        self._on_speak = on_speak
        self._client_factory = client_factory or self._default_client_factory
        self._active: asyncio.Task[None] | None = None

    # --- launch / cancel (host-facing) -----------------------------------------------------

    def start(self, task_id: str, text: str) -> None:
        """Fire-and-forget launch on the live async loop. Supersede (cancel) any lingering run
        rather than refuse — refusing would leave the new task's store entry RUNNING with no
        producer (a zombie). If there is no running loop (console + kora_enabled, or a sync
        test), create_task raises RuntimeError → terminalize so we never strand RUNNING."""
        if self._active is not None and not self._active.done():
            self._active.cancel()
        coro = self._run(task_id, text)
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

    # --- run / stream ----------------------------------------------------------------------

    async def _run(self, task_id: str, text: str) -> None:
        try:
            await asyncio.wait_for(self._stream(task_id, text), self._cfg.kora_deadline_s)
        except Exception as exc:  # noqa: BLE001 — includes TimeoutError; CancelledError is a
            # BaseException and is NOT caught here, so a cancel/shutdown propagates while the
            # finally below still terminalizes. Any real error is alerted, never swallowed.
            self._journal.alert(AlertKind.KORA_RUN_FAILED, {"task_id": task_id, "error": repr(exc)})
        finally:
            self._terminalize_if_running(task_id)

    async def _stream(self, task_id: str, text: str) -> None:
        opts = self._build_options(task_id, text)
        seq_gen = itertools.count()
        async with self._client_factory(opts) as client:
            await client.query(text)
            async for msg in client.receive_response():
                # A superseded/stale run must not write into whatever task now occupies the
                # store (RISK-B2): bail the moment the store's task is gone or has a different id.
                if self._store.task is None or self._store.task.id != task_id:
                    return
                ts = self._clock.now()
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
        permission_mode='default', allowed_tools=[] (a tool in allowed_tools SHADOWS
        can_use_tool — proven by CanUseToolShadowedWarning), setting_sources=[] (user/project
        settings also shadow the gate), can_use_tool=self._gate as the authoritative boundary.
        SDK import is lazy so the module loads without the package."""
        from claude_agent_sdk import ClaudeAgentOptions

        workspace = self._workspace()
        workspace.mkdir(parents=True, exist_ok=True)
        return ClaudeAgentOptions(
            cwd=str(workspace),
            permission_mode="default",
            allowed_tools=[],
            disallowed_tools=[],
            setting_sources=[],
            can_use_tool=self._gate,
            model=self._cfg.kora_model,
            cli_path=self._cfg.kora_cli_path,
            max_turns=self._cfg.kora_max_turns,
            max_budget_usd=self._cfg.kora_max_budget_usd,
            system_prompt=self._system_prompt(workspace, text),
        )

    def _gate_decision(self, tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str | None]:
        """Pure fail-closed containment decision (RISK-M6). Returns (allowed, detail). A
        non-file tool is denied outright; a mutating file tool with no/blank path is denied; a
        path is allowed only when its resolved form is inside the workspace via
        Path.resolve().is_relative_to (NOT startswith — that would leak the sibling /ws-evil)."""
        key = _PATH_KEY.get(tool_name)
        if key is None:
            return False, f"tool {tool_name} not permitted (slice 1)"
        raw = (tool_input or {}).get(key)
        if not isinstance(raw, str) or not raw.strip():
            if tool_name in _READ_SEARCH_TOOLS:
                return True, None  # defaults to cwd, which is inside the workspace
            return False, f"{tool_name}: missing/blank {key}"
        workspace = self._workspace()
        p = Path(raw)
        if not p.is_absolute():
            p = workspace / p
        try:
            resolved = p.resolve()
            ws_resolved = workspace.resolve()
        except (OSError, RuntimeError, ValueError):
            return False, "path resolution failed"
        if resolved.is_relative_to(ws_resolved):
            return True, str(resolved)
        return False, f"path escapes workspace: {resolved}"

    async def _gate(self, tool_name: str, tool_input: dict[str, Any], ctx: Any = None) -> Any:
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        allowed, detail = self._gate_decision(tool_name, tool_input)
        self._journal.record_kora_event(
            KoraEvent(
                id=f"kora-gate-{int(self._clock.now() * 1000)}",
                type="gate_allow" if allowed else "gate_deny",
                cls=EventClass.NARRATABLE,
                payload={"tool": tool_name, "detail": detail},
                speak_text=None,
                ts=self._clock.now(),
            )
        )
        if allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message=detail or "denied")

    def _default_client_factory(self, opts: Any) -> Any:
        from claude_agent_sdk import ClaudeSDKClient

        return ClaudeSDKClient(opts)
