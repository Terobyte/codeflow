"""M1 slice 1 — the real Kora producer (synapse.bridge.kora).

Everything here runs with NO network / NO SDK / NO classifier: the SDK client is faked via the
`client_factory` seam, and the fake messages are plain objects whose class NAME matches the SDK
dataclass name (the mapper duck-types on `type(msg).__name__`, never isinstance). `_build_options`
and `_gate` do touch the installed `claude-agent-sdk` for options/permission dataclasses, but
never spawn the CLI or hit the API.
"""
from __future__ import annotations

import asyncio
import functools
import itertools
import json
import os

import pytest

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.kora import (
    _PATH_KEY,
    KoraRunner,
    _completion_text,
    _failure_text,
    _is_secret_path,
    _message_to_events,
    apply_event_to_store,
)
from synapse.bridge.state import EventClass, KoraEvent, SpeakLedger, TaskStatus, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.dispatcher.tools import KoraBridge, ToolHandlers
from synapse.journal import TurnJournal


# --- fake SDK messages (class NAME is what the duck-typed mapper keys on) -------------------


class SystemMessage:
    def __init__(self, subtype, data=None):
        self.subtype = subtype
        self.data = data or {}


class AssistantMessage:
    def __init__(self, content):
        self.content = content


class UserMessage:
    def __init__(self, content):
        self.content = content


class ResultMessage:
    def __init__(self, is_error, num_turns=1, total_cost_usd=0.001):
        self.is_error = is_error
        self.num_turns = num_turns
        self.total_cost_usd = total_cost_usd


class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class ThinkingBlock:
    def __init__(self, thinking, signature=""):
        self.thinking = thinking
        self.signature = signature


class ToolResultBlock:
    def __init__(self, tool_use_id, content, is_error=False):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class RateLimitEvent:  # exercises the "unknown → kora_<snake>" branch
    pass


# --- fake async-context client -------------------------------------------------------------


def _static(messages):
    async def gen():
        for m in messages:
            yield m

    return gen


def _client_factory(gen_func, on_query=None):
    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, prompt, session_id="default"):
            if on_query is not None:
                on_query(prompt)

        def receive_response(self):
            return gen_func()

    return lambda opts: _FakeClient()


# --- helpers -------------------------------------------------------------------------------


def make_runner(tmp_path, client_factory=None, deadline_s=900.0):
    clock = FakeClock(0.0)
    ws = tmp_path / "ws"
    cfg = SynapseConfig(kora_workspace_dir=str(ws), kora_deadline_s=deadline_s)
    store = TaskStore(clock)  # journal_dir=None → no state.json persistence
    ledger = SpeakLedger()
    journal = TurnJournal(str(tmp_path / "journal"), clock, session_id="s")
    speaks: list[str] = []
    runner = KoraRunner(cfg, store, ledger, clock, journal, speaks.append, client_factory=client_factory)
    return runner, store, ledger, journal, ws, speaks


def _journal_rows(journal):
    return [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines() if line.strip()]


# =========================================================================================
# 1. Mapper — allowlist table, NOTHING is ever critical, redaction, sequential ids
# =========================================================================================


def test_mapper_full_stream_types_classes_ids_and_redaction():
    seq = itertools.count()
    msgs = [
        SystemMessage("init", {"session_id": "s1", "model": "m"}),
        AssistantMessage([TextBlock("h" * 500), ToolUseBlock("u1", "Write", {"file_path": "a", "content": "b"}), ThinkingBlock("secret")]),
        UserMessage([ToolResultBlock("u1", "ok", is_error=False)]),
        ResultMessage(is_error=False, num_turns=2, total_cost_usd=0.01),
        RateLimitEvent(),
    ]
    events = []
    for m in msgs:
        events += _message_to_events(m, "tk", "задача", 1.5, seq)

    # NOTHING is ever critical (⇒ zero false CRITICAL_WITHOUT_SPEAK by construction).
    assert all(e.cls == EventClass.NARRATABLE for e in events)
    assert [e.type for e in events] == [
        "task_started", "assistant_text", "tool_use", "thinking", "tool_result", "task_completed", "kora_rate_limit_event",
    ]
    # ids are sequential across the whole stream.
    assert [e.id for e in events] == [f"kora-tk-{i}" for i in range(len(events))]
    # privacy: tool_use keeps NAME + input KEYS, never values; thinking keeps nothing; text is capped.
    tool_use = next(e for e in events if e.type == "tool_use")
    assert tool_use.payload == {"name": "Write", "input_keys": ["content", "file_path"]}
    assert next(e for e in events if e.type == "thinking").payload == {}
    assert len(next(e for e in events if e.type == "assistant_text").payload["text"]) == 200
    # terminal carries the SPEAK verbatim + cost/turns.
    completed = next(e for e in events if e.type == "task_completed")
    assert completed.speak_text == _completion_text("задача")
    assert completed.payload == {"num_turns": 2, "total_cost_usd": 0.01}
    assert all(e.ts == 1.5 for e in events)


def test_mapper_result_error_maps_to_task_failed_narratable():
    events = _message_to_events(ResultMessage(is_error=True), "tk", "задача", 1.0, itertools.count())
    assert len(events) == 1
    assert events[0].type == "task_failed"
    assert events[0].cls == EventClass.NARRATABLE
    assert events[0].speak_text == _failure_text("задача")


def test_mapper_system_non_init_is_quiet_and_never_task_status():
    events = _message_to_events(SystemMessage("compact_boundary", {}), "tk", "z", 1.0, itertools.count())
    assert [e.type for e in events] == ["kora_system"]
    assert events[0].speak_text is None


# =========================================================================================
# 2. apply_event_to_store — lifecycle→apply_event, else→heartbeat, zero register_critical
# =========================================================================================


def _apply_ctx(tmp_path):
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    store.start_task("tk", "z", TaskStatus.RUNNING, 0.0)  # producer created it before launch
    ledger = SpeakLedger()
    journal = TurnJournal(str(tmp_path), clock, session_id="s")
    speaks: list[str] = []
    return store, ledger, journal, speaks


def test_apply_started_running_no_critical(tmp_path):
    store, ledger, journal, speaks = _apply_ctx(tmp_path)
    ev = KoraEvent("e1", "task_started", EventClass.NARRATABLE, {}, None, 1.0)
    apply_event_to_store(ev, store, ledger, speaks.append, journal)
    assert store.task.status == TaskStatus.RUNNING
    assert ledger._pending == {}  # nothing critical → nothing registered


def test_apply_completed_speaks_and_completes(tmp_path):
    store, ledger, journal, speaks = _apply_ctx(tmp_path)
    ev = KoraEvent("e2", "task_completed", EventClass.NARRATABLE, {}, "Задача выполнена: z", 2.0)
    apply_event_to_store(ev, store, ledger, speaks.append, journal)
    assert store.task.status == TaskStatus.COMPLETED
    assert speaks == ["Задача выполнена: z"]
    assert ledger._pending == {}


def test_apply_failed(tmp_path):
    store, ledger, journal, speaks = _apply_ctx(tmp_path)
    ev = KoraEvent("e3", "task_failed", EventClass.NARRATABLE, {}, "Задача не выполнена: z", 3.0)
    apply_event_to_store(ev, store, ledger, speaks.append, journal)
    assert store.task.status == TaskStatus.FAILED


def test_apply_non_lifecycle_is_heartbeat_only(tmp_path):
    store, ledger, journal, speaks = _apply_ctx(tmp_path)
    before = list(store.task.events)
    ev = KoraEvent("e4", "tool_use", EventClass.NARRATABLE, {"name": "Write"}, None, 5.0)
    apply_event_to_store(ev, store, ledger, speaks.append, journal)
    assert store.task.events == before  # NOT appended — heartbeat only
    assert store.task.status == TaskStatus.RUNNING  # unchanged
    assert speaks == []


def test_apply_journals_every_event(tmp_path):
    store, ledger, journal, speaks = _apply_ctx(tmp_path)
    for ev in (
        KoraEvent("a", "task_started", EventClass.NARRATABLE, {}, None, 1.0),
        KoraEvent("b", "tool_use", EventClass.NARRATABLE, {}, None, 2.0),
        KoraEvent("c", "task_completed", EventClass.NARRATABLE, {}, "done", 3.0),
    ):
        apply_event_to_store(ev, store, ledger, speaks.append, journal)
    kora_rows = [r for r in _journal_rows(journal) if r["kind"] == "kora_event"]
    assert len(kora_rows) == 3  # journal gets ALL, store got only 2 lifecycle events
    assert [e.type for e in store.task.events] == ["task_started", "task_completed"]


# =========================================================================================
# 3. _run — full happy cycle → COMPLETED, store=lifecycle, journal=full
# =========================================================================================


async def test_run_full_cycle_completes_and_store_stays_lean(tmp_path):
    msgs = [
        SystemMessage("init", {"session_id": "s1", "model": "m"}),
        AssistantMessage([TextBlock("работаю")]),
        AssistantMessage([ToolUseBlock("u1", "Write", {"file_path": "a.txt", "content": "x"})]),
        UserMessage([ToolResultBlock("u1", "ok")]),
        ResultMessage(is_error=False, num_turns=2, total_cost_usd=0.01),
    ]
    runner, store, ledger, journal, ws, speaks = make_runner(tmp_path, client_factory=_client_factory(_static(msgs)))
    store.start_task("tk", "создай файл", TaskStatus.RUNNING, 0.0)

    await runner._run("tk", "создай файл")

    assert store.task.status == TaskStatus.COMPLETED
    # store keeps only the coarse lifecycle (ALT-M1) — the 3 intermediate messages did NOT append.
    assert [e.type for e in store.task.events] == ["task_started", "task_completed"]
    assert speaks == [_completion_text("создай файл")]
    # journal captured every one of the 5 messages.
    assert len([r for r in _journal_rows(journal) if r["kind"] == "kora_event"]) == 5


# =========================================================================================
# 4. Structural anti-zombie — NO exit path leaves RUNNING
# =========================================================================================


async def test_clean_exit_without_terminal_is_terminalized(tmp_path):
    # Stream ends with no ResultMessage (RISK-B1: CLI exits 0 with no result frame).
    msgs = [SystemMessage("init", {}), AssistantMessage([TextBlock("...")])]
    runner, store, *_ = make_runner(tmp_path, client_factory=_client_factory(_static(msgs)))
    store.start_task("tk", "t", TaskStatus.RUNNING, 0.0)

    await runner._run("tk", "t")

    assert store.task.status == TaskStatus.FAILED  # finally → terminalize


async def test_cancelled_error_is_terminalized(tmp_path):
    reached = asyncio.Event()
    release = asyncio.Event()

    async def gen():
        yield SystemMessage("init", {})
        reached.set()
        await release.wait()  # never released — we cancel instead

    runner, store, *_ = make_runner(tmp_path, client_factory=_client_factory(gen))
    store.start_task("tk", "t", TaskStatus.RUNNING, 0.0)

    task = asyncio.create_task(runner._run("tk", "t"))
    await asyncio.wait_for(reached.wait(), 1.0)  # init applied, stream now blocked
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.task.status == TaskStatus.FAILED  # finally ran on cancel, CancelledError not swallowed


async def test_post_completion_raise_stays_completed(tmp_path):
    async def gen():
        yield SystemMessage("init", {})
        yield ResultMessage(is_error=False)
        raise RuntimeError("boom after completion")

    runner, store, ledger, journal, ws, speaks = make_runner(tmp_path, client_factory=_client_factory(gen))
    store.start_task("tk", "t", TaskStatus.RUNNING, 0.0)

    await runner._run("tk", "t")  # RuntimeError caught by except Exception, not re-raised

    assert store.task.status == TaskStatus.COMPLETED  # terminalize is a no-op on a terminal task
    alerts = [r for r in _journal_rows(journal) if r["kind"] == "alert"]
    assert any(a["alert_kind"] == "KORA_RUN_FAILED" for a in alerts)


async def test_watchdog_timeout_is_terminalized(tmp_path):
    async def gen():
        yield SystemMessage("init", {})
        await asyncio.sleep(10)  # far exceeds the tiny deadline below

    runner, store, *_ = make_runner(tmp_path, client_factory=_client_factory(gen), deadline_s=0.02)
    store.start_task("tk", "t", TaskStatus.RUNNING, 0.0)

    await runner._run("tk", "t")

    assert store.task.status == TaskStatus.FAILED  # wait_for TimeoutError → finally terminalize


async def test_stale_run_does_not_write_into_a_different_task(tmp_path):
    # The store's task changes id mid-stream → the stale run must bail before applying.
    msgs = [SystemMessage("init", {}), ResultMessage(is_error=False)]
    runner, store, *_ = make_runner(tmp_path, client_factory=_client_factory(_static(msgs)))
    store.start_task("OTHER", "t", TaskStatus.RUNNING, 0.0)

    await runner._run("tk", "t")  # task_id "tk" != store task "OTHER"

    assert store.task.id == "OTHER"
    assert store.task.status == TaskStatus.RUNNING  # untouched by the stale run
    assert store.task.events == []


# =========================================================================================
# 5. Gate policy — fail-closed workspace containment (RISK-M6). These exercise the PURE
# `_gate_decision` predicate (allowed, detail, category); the PreToolUse-hook delivery that
# wraps it is covered in Section 5c.
# =========================================================================================


def test_gate_allows_inside_workspace(tmp_path):
    runner, store, ledger, journal, ws, speaks = make_runner(tmp_path)
    assert runner._gate_decision("Write", {"file_path": str(ws / "a.txt")})[0] is True
    assert runner._gate_decision("Write", {"file_path": "rel/inside.txt"})[0] is True  # relative → cwd


def test_gate_allows_sibling_directory_write_nonsecret(tmp_path):
    # B24 (gate v3, «везде она может писать»): a Write to a non-secret sibling dir outside the
    # workspace is now ALLOWED. /ws-evil is still NOT under /ws (the resolve()/is_relative_to check
    # keeps it out of the in-workspace branch — startswith would leak it); it's the new
    # mutating-outside allow that accepts it, while a secret there still denies (secret check runs
    # BEFORE this branch).
    runner, store, ledger, journal, ws, speaks = make_runner(tmp_path)
    sibling = ws.parent / (ws.name + "-evil") / "a.txt"
    allowed, _detail, category = runner._gate_decision("Write", {"file_path": str(sibling)})
    assert allowed is True and category == "allow"
    secret_sibling = ws.parent / (ws.name + "-evil") / ".env"
    a2, _d2, c2 = runner._gate_decision("Write", {"file_path": str(secret_sibling)})
    assert a2 is False and c2 == "secret_path"


def test_gate_allows_home_write_nonsecret(tmp_path):
    # B24: Кора пишет где угодно на машине, кроме секретов — обычный файл в $HOME разрешён.
    runner, store, ledger, journal, ws, speaks = make_runner(tmp_path)
    allowed, _detail, category = runner._gate_decision("Write", {"file_path": os.path.expanduser("~/synapse_escape_probe.txt")})
    assert allowed is True and category == "allow"


def test_gate_denies_missing_path_for_mutating_tool(tmp_path):
    runner, *_ = make_runner(tmp_path)
    allowed, _detail, category = runner._gate_decision("Write", {})  # fail-closed
    assert allowed is False and category == "missing_path"


def test_gate_allows_missing_path_for_read_search(tmp_path):
    runner, *_ = make_runner(tmp_path)
    assert runner._gate_decision("Glob", {})[0] is True  # defaults to cwd ∈ workspace


def test_gate_allows_bash_and_denies_web_egress(tmp_path):
    # Gate v2 A1': Bash → allow (категория allow_egress, detail = command[:200]);
    # WebFetch/WebSearch остаются deny egress (юзер не заказывал — P13).
    runner, *_ = make_runner(tmp_path)
    allowed, detail, category = runner._gate_decision("Bash", {"command": "ls -la"})
    assert allowed is True and category == "allow_egress" and detail == "ls -la"
    for tool, inp in (("WebFetch", {"url": "http://example.com"}), ("WebSearch", {"query": "x"})):
        allowed, _detail, category = runner._gate_decision(tool, inp)
        assert allowed is False and category == "egress", f"{tool} must stay denied"


def test_gate_denies_unknown_non_file_tools(tmp_path):
    runner, *_ = make_runner(tmp_path)
    allowed, _detail, category = runner._gate_decision("Task", {"description": "x"})
    assert allowed is False and category == "non_file_tool"


def test_gate_allows_read_and_write_outside_workspace_but_not_secret(tmp_path):
    # Gate v3 (B24): и читающие, И мутирующие инструменты ходят по всей машине, кроме секретов
    # (per-path secret-чек ДО allow). До B24 запись вне ws была outside_workspace-deny (A3').
    runner, store, ledger, journal, ws, speaks = make_runner(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "notes.txt").write_text("x", encoding="utf-8")
    allowed, _detail, category = runner._gate_decision("Read", {"file_path": str(outside / "notes.txt")})
    assert allowed is True and category == "allow"
    # секрет вне workspace (чтение) — по-прежнему deny
    allowed2, _d2, category2 = runner._gate_decision("Read", {"file_path": str(outside / ".env")})
    assert allowed2 is False and category2 == "secret_path"
    # мутирующий вне workspace — теперь ALLOW (B24)
    allowed3, _d3, category3 = runner._gate_decision("Write", {"file_path": str(outside / "notes.txt")})
    assert allowed3 is True and category3 == "allow"
    # …но секрет вне workspace мутирующим — по-прежнему deny (secret-чек ДО allow)
    allowed4, _d4, category4 = runner._gate_decision("Write", {"file_path": str(outside / ".env")})
    assert allowed4 is False and category4 == "secret_path"


def test_gate_pathless_read_stays_missing_path_deny(tmp_path):
    # Gate v2 A2' (MINOR): дефолт-к-cwd умеют только Glob/Grep/LS; Read без пути — deny.
    runner, *_ = make_runner(tmp_path)
    allowed, _detail, category = runner._gate_decision("Read", {})
    assert allowed is False and category == "missing_path"


def test_gate_outside_ws_directory_bounded_scan(tmp_path):
    # Gate v2 A2' (sec-1): каталог ВНЕ workspace сканится ограниченно (depth≤2): видимый
    # секрет на глубине ≤2 → deny; секрет ГЛУБЖЕ границы → allow (принятый best-effort).
    runner, *_ = make_runner(tmp_path)
    shallow = tmp_path / "proj-a"
    (shallow / "sub").mkdir(parents=True)
    (shallow / "sub" / "secrets.yaml").write_text("t: s\n", encoding="utf-8")
    allowed, _d, category = runner._gate_decision("Grep", {"path": str(shallow)})
    assert allowed is False and category == "secret_path"

    deep = tmp_path / "proj-b"
    (deep / "a" / "b" / "c").mkdir(parents=True)
    (deep / "a" / "b" / "c" / "secrets.yaml").write_text("t: s\n", encoding="utf-8")
    allowed2, _d2, category2 = runner._gate_decision("Grep", {"path": str(deep)})
    assert allowed2 is True and category2 == "allow"  # глубже depth-границы — не сканим


def test_gate_allows_safe_meta_tools(tmp_path):
    # ToolSearch (load tools) + TodoWrite (planning) are the only non-file tools allowed —
    # everything else non-file stays fail-closed (slice-4 §5b Plan v3 item 4).
    runner, *_ = make_runner(tmp_path)
    assert runner._gate_decision("ToolSearch", {"query": "x"}) == (True, None, "allow_meta")
    assert runner._gate_decision("TodoWrite", {"todos": []}) == (True, None, "allow_meta")


# =========================================================================================
# 5b. Gate secret containment (slice 4, §2.8) — a secret INSIDE the workspace is still a
# secret: denied for every file tool BEFORE the in-workspace allow, casefold-proof (APFS is
# case-insensitive), while commit-safe `.env` templates and `.py` names that merely CONTAIN
# "env" stay allowed. Deny reasons are journaled with an explicit category.
# =========================================================================================


# Every file tool the gate recognizes → must deny a secret regardless of which one Kora picks.
_FILE_TOOLS = ("Read", "Edit", "Write", "Grep", "Glob", "LS", "NotebookEdit")


@pytest.mark.parametrize(
    "rel",
    [".env", "sub/.env.local", ".ssh/id_rsa", "server.pem", "server.key", ".aws/credentials", ".npmrc", ".git/config", "id_ecdsa"],
)
def test_gate_denies_secret_inside_workspace_for_every_file_tool(tmp_path, rel):
    runner, store, ledger, journal, ws, speaks = make_runner(tmp_path)
    for tool in _FILE_TOOLS:
        allowed, _detail, category = runner._gate_decision(tool, {_PATH_KEY[tool]: str(ws / rel)})
        assert allowed is False and category == "secret_path", f"{tool} on in-workspace secret {rel!r} must be denied"


def test_gate_secret_denial_is_casefold_proof(tmp_path):
    # BLOCKER-1: macOS APFS is case-insensitive, so Read(".ENV") opens the same bytes as .env.
    runner, store, ledger, journal, ws, speaks = make_runner(tmp_path)
    for rel in (".ENV", ".SSH/id_rsa", "SERVER.PEM"):
        assert runner._gate_decision("Read", {"file_path": str(ws / rel)})[0] is False, f"{rel!r} must be denied (case-insensitive fs)"


def test_gate_allows_commit_safe_env_templates(tmp_path):
    runner, store, ledger, journal, ws, speaks = make_runner(tmp_path)
    for rel in (".env.example", ".env.sample", ".env.template", ".env.dist"):
        assert runner._gate_decision("Read", {"file_path": str(ws / rel)})[0] is True, f"{rel!r} is a commit-safe template → allowed"


def test_gate_does_not_false_positive_on_env_substring_names(tmp_path):
    # The pattern must be exact `.env` / `.env.`-prefix, never the substring "env".
    runner, store, ledger, journal, ws, speaks = make_runner(tmp_path)
    for rel in ("environment.py", "prevent.py", "env.py", "main.py"):
        assert runner._gate_decision("Read", {"file_path": str(ws / rel)})[0] is True, f"{rel!r} is not a secret → allowed"


# =========================================================================================
# 5c. PreToolUse hook delivery (slice-4 repair) — the ONE gate now fires for EVERY tool. The
# hook wraps `_gate_decision`, journals the gate_allow/gate_deny event, and returns an EXPLICIT
# permissionDecision so headless never falls back to a no-decision block.
# =========================================================================================


async def _decision(runner, tool, inp):
    res = await runner._pretool_hook({"tool_name": tool, "tool_input": inp}, None, None)
    return res["hookSpecificOutput"]["permissionDecision"]


async def test_pretool_hook_returns_explicit_permission_decisions(tmp_path):
    runner, store, ledger, journal, ws, speaks = make_runner(tmp_path)
    # file inside workspace → allow; secret inside workspace → deny; Bash → allow (gate v2 A1');
    # web egress → deny; meta → allow.
    assert await _decision(runner, "Write", {"file_path": str(ws / "a.txt")}) == "allow"
    assert await _decision(runner, "Read", {"file_path": str(ws / ".env")}) == "deny"
    assert await _decision(runner, "Bash", {"command": "ls"}) == "allow"
    assert await _decision(runner, "WebFetch", {"url": "http://example.com"}) == "deny"
    assert await _decision(runner, "ToolSearch", {"query": "x"}) == "allow"
    assert await _decision(runner, "TodoWrite", {"todos": []}) == "allow"


async def test_pretool_hook_deny_carries_reason(tmp_path):
    runner, store, ledger, journal, ws, speaks = make_runner(tmp_path)
    res = await runner._pretool_hook({"tool_name": "Read", "tool_input": {"file_path": str(ws / ".env")}}, None, None)
    hso = res["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"]  # non-empty (detail or category)


async def test_gate_journals_explicit_deny_categories(tmp_path):
    runner, store, ledger, journal, ws, speaks = make_runner(tmp_path)
    # B24: a plain Write outside the workspace is now ALLOWED, so the mutating deny that still
    # fires everywhere is the secret-path one (a secret file outside ws written by Write).
    secret_outside = ws.parent / (ws.name + "-evil") / ".env"
    cases = [
        (("Read", {"file_path": str(ws / ".env")}), "secret_path"),
        (("WebSearch", {"query": "x"}), "egress"),  # gate v2 A1': Bash больше не deny-кейс
        (("WebFetch", {"url": "http://example.com"}), "egress"),
        (("Task", {"description": "x"}), "non_file_tool"),
        (("Write", {}), "missing_path"),
        (("Write", {"file_path": str(secret_outside)}), "secret_path"),
    ]
    for (tool, inp), _expected in cases:
        assert await _decision(runner, tool, inp) == "deny"
    deny_rows = [r for r in _journal_rows(journal) if r["kind"] == "kora_event" and r["type"] == "gate_deny"]
    assert [r["payload"]["category"] for r in deny_rows] == [expected for _, expected in cases]


async def test_gate_journals_allow_category(tmp_path):
    runner, store, ledger, journal, ws, speaks = make_runner(tmp_path)
    await runner._pretool_hook({"tool_name": "Grep", "tool_input": {}}, None, None)  # missing-path read/search → allow
    allow_rows = [r for r in _journal_rows(journal) if r["kind"] == "kora_event" and r["type"] == "gate_allow"]
    assert allow_rows and allow_rows[-1]["payload"]["category"] == "allow"


async def test_gate_journals_bash_allow_with_command(tmp_path):
    # Gate v2 A7'-а: Bash слеп для файлового гейта — gate_allow обязан нести САМУ команду
    # (аудит-след), обрезанную до 200 символов.
    runner, store, ledger, journal, ws, speaks = make_runner(tmp_path)
    long_cmd = "echo " + "x" * 300
    assert await _decision(runner, "Bash", {"command": long_cmd}) == "allow"
    allow_rows = [r for r in _journal_rows(journal) if r["kind"] == "kora_event" and r["type"] == "gate_allow"]
    assert allow_rows and allow_rows[-1]["payload"]["category"] == "allow_egress"
    assert allow_rows[-1]["payload"]["tool"] == "Bash"
    assert allow_rows[-1]["payload"]["command"] == long_cmd[:200]


def test_is_secret_path_predicate_unit():
    # Direct predicate coverage independent of resolve(): segments, stems, suffixes, templates.
    from pathlib import Path

    assert _is_secret_path(Path("/ws/.env"))
    assert _is_secret_path(Path("/ws/deep/.ssh/id_rsa"))
    assert _is_secret_path(Path("/ws/.git-credentials"))
    assert _is_secret_path(Path("/ws/id_ed25519"))
    assert _is_secret_path(Path("/ws/key.p12"))
    assert _is_secret_path(Path("/ws/.ENV"))  # casefold
    assert not _is_secret_path(Path("/ws/.env.example"))
    assert not _is_secret_path(Path("/ws/environment.py"))
    assert not _is_secret_path(Path("/ws/env.py"))


# =========================================================================================
# 6. _build_options — slice-1 security posture
# =========================================================================================


def test_build_options_security_posture(tmp_path):
    runner, store, ledger, journal, ws, speaks = make_runner(tmp_path)
    opts = runner._build_options("tk", "создай файл smoke.txt")
    assert opts.cwd == str(ws)
    assert opts.permission_mode == "default"
    assert opts.allowed_tools == []  # empty → the gate is authoritative (no shadowing)
    assert opts.setting_sources == []  # no user/project settings shadow the gate
    # slice-4 repair: the boundary is a PreToolUse HOOK (fires for EVERY tool), NOT can_use_tool
    # (which only fired for permission-requiring tools and let Read/Glob/Bash bypass the gate).
    assert opts.can_use_tool is None
    matcher = opts.hooks["PreToolUse"][0]
    assert len(matcher.hooks) == 1
    hook = matcher.hooks[0]
    # B-BRIDGE-9: the hook must carry THIS run's identity (task_id) baked in — a bare bound
    # method let the gate judge a call by whatever run last occupied `self._run_owner`, not by
    # who actually issued the call. functools.partial binds task_id at build time so nothing
    # downstream (a later _build_options for a different run) can swap it out from under this
    # hook instance — the strict check: same underlying _pretool_hook of THIS runner, no stray
    # positional args, and the exact task_id passed to _build_options above.
    assert isinstance(hook, functools.partial)
    assert hook.func == runner._pretool_hook  # bound method: same __self__/__func__
    assert hook.args == ()
    assert hook.keywords == {"task_id": "tk"}
    assert str(ws) in opts.system_prompt  # LOAD-BEARING: prompt must name the workspace


# =========================================================================================
# 7. start() supersede — two starts → one active, first cancelled
# =========================================================================================


async def test_start_supersedes_lingering_run(tmp_path):
    reached = asyncio.Event()
    release = asyncio.Event()

    async def gen():
        yield SystemMessage("init", {})
        reached.set()
        await release.wait()

    runner, store, *_ = make_runner(tmp_path, client_factory=_client_factory(gen))
    store.start_task("tk", "t", TaskStatus.RUNNING, 0.0)

    runner.start("tk", "t")
    first = runner._active
    await asyncio.wait_for(reached.wait(), 1.0)

    runner.start("tk", "t")  # supersede
    second = runner._active
    assert first is not second

    with pytest.raises(asyncio.CancelledError):
        await first  # the lingering run was cancelled, not left running

    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second


def test_start_without_running_loop_terminalizes(tmp_path):
    # No running event loop (sync call) → create_task raises RuntimeError → terminalize, never
    # leave the store RUNNING with no producer (RISK-MIN8).
    runner, store, *_ = make_runner(tmp_path, client_factory=_client_factory(_static([])))
    store.start_task("tk", "t", TaskStatus.RUNNING, 0.0)
    runner.start("tk", "t")
    assert store.task.status == TaskStatus.FAILED


# =========================================================================================
# 8. Wiring — COMMITTED launches Kora, request_cancel tears it down
# =========================================================================================


def make_handlers(tmp_path):
    clock = FakeClock(0.0)
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path), clock, session_id="s")
    classifier = KeywordClassifier(cfg.destructive_keywords)
    confirm_flow = ConfirmFlow(
        store, clock, classifier, journal, cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s
    )
    committed: list[tuple[str, str]] = []
    cancels: list[int] = []
    bridge = KoraBridge(
        store=store,
        confirm_flow=confirm_flow,
        clock=clock,
        cfg=cfg,
        on_task_committed=lambda tid, text: committed.append((tid, text)),
        on_cancel=lambda: cancels.append(1),
    )
    handlers = ToolHandlers(bridge, journal)
    return handlers, store, confirm_flow, committed, cancels, clock


async def test_submit_committed_launches_kora(tmp_path):
    handlers, store, _, committed, _, _ = make_handlers(tmp_path)
    handlers.begin_turn("t1")
    res = await handlers.submit_task(text="скачай книгу")
    assert res["outcome"] == "committed"
    assert committed == [(res["task_id"], "скачай книгу")]


async def test_submit_staged_does_not_launch(tmp_path):
    handlers, store, _, committed, _, _ = make_handlers(tmp_path)
    handlers.begin_turn("t1")
    res = await handlers.submit_task(text="удали всё")  # destructive → only staged here
    assert res["outcome"] == "staged"
    assert committed == []


async def test_submit_rejected_active_does_not_launch(tmp_path):
    handlers, store, _, committed, _, _ = make_handlers(tmp_path)
    store.start_task("existing", "x", TaskStatus.RUNNING, 0.0)
    handlers.begin_turn("t1")
    res = await handlers.submit_task(text="скачай книгу")
    assert res["outcome"] == "rejected_active"
    assert committed == []


async def test_confirm_committed_launches_with_store_text(tmp_path):
    handlers, store, confirm_flow, committed, _, clock = make_handlers(tmp_path)
    handlers.begin_turn("t1")
    assert (await handlers.submit_task(text="удали старьё"))["outcome"] == "staged"
    # double-key: a user turn must intervene, and it must affirm. submit/confirm go through the
    # real handlers, i.e. through KoraBridge.confirm_scope() — make_handlers wires no
    # thread_id_for, so confirm_scope() falls back to the bridge's default channel ("voice").
    # note_user_turn is called directly here (bypassing the dispatcher loop), so it must be
    # scoped to that SAME "voice" conversation, or B-BRIDGE-6 makes it a no-op (thread_id=None
    # is ignored entirely) and the double-key never clears.
    confirm_flow.note_user_turn("да", clock.now(), thread_id="voice")
    handlers.begin_turn("t2")
    res = await handlers.confirm_task(decision="confirm")
    assert res["outcome"] == "committed"
    assert len(committed) == 1
    # text comes from store.task.text (the task), NOT ConfirmResult.text (the SPEAK phrase).
    assert committed[0][1] == "удали старьё"
    assert committed[0][0] == store.task.id


async def test_request_cancel_fires_on_cancel(tmp_path):
    handlers, store, _, _, cancels, _ = make_handlers(tmp_path)
    store.start_task("t1", "x", TaskStatus.RUNNING, 0.0)
    handlers.begin_turn("t1")
    res = await handlers.request_cancel()
    assert res["outcome"] == "cancel_requested"
    assert cancels == [1]


async def test_request_cancel_no_task_does_not_fire(tmp_path):
    handlers, store, _, _, cancels, _ = make_handlers(tmp_path)
    handlers.begin_turn("t1")
    res = await handlers.request_cancel()
    assert res["outcome"] == "no_active_task"
    assert cancels == []
