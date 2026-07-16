"""Final Phase-0 acceptance anchors for the gaps found after C0-C5 landed."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

import synapse.bridge.kora as kora_module
from synapse.bridge.kora import KoraRunner, _KORA_ENV_ALLOWLIST
from synapse.bridge.runspec import RunSpec
from synapse.bridge.state import SpeakLedger, TaskStatus, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal


def _runner(tmp_path, *, deadline_s: float = 0.02):
    clock = FakeClock(0.0)
    cfg = SynapseConfig(
        journal_dir=str(tmp_path / "audit"),
        kora_workspace_dir=str(tmp_path / "project"),
        kora_deadline_s=deadline_s,
    )
    store = TaskStore(clock)
    journal = TurnJournal(cfg.journal_dir, clock, session_id="phase0")
    runner = KoraRunner(cfg, store, SpeakLedger(), clock, journal, lambda _text: None)
    return runner, store, journal


def test_c6_sdk_subprocess_env_is_allowlisted(tmp_path, monkeypatch):
    runner, _store, _journal = _runner(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "worker-key")
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "must-not-leak")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "must-not-leak")
    monkeypatch.setenv("TG_BOT_TOKEN", "must-not-leak")

    opts = runner._build_options("task", "do work")

    assert set(opts.env) <= set(_KORA_ENV_ALLOWLIST) | {"SYNAPSE_KORA_REAL_CLI"}
    assert opts.env["ANTHROPIC_API_KEY"] == "worker-key"
    assert not ({"FISH_AUDIO_API_KEY", "DEEPGRAM_API_KEY", "TG_BOT_TOKEN"} & set(opts.env))
    assert opts.cli_path == runner._sanitized_cli_path()


def test_c6_sanitizer_execs_real_cli_with_clean_environment(tmp_path):
    runner, _store, _journal = _runner(tmp_path)
    env = {
        **os.environ,
        "SYNAPSE_KORA_REAL_CLI": "/usr/bin/env",
        "ANTHROPIC_API_KEY": "worker-key",
        "FISH_AUDIO_API_KEY": "must-not-leak",
        "DEEPGRAM_API_KEY": "must-not-leak",
        "SYNAPSE_API_TOKEN": "must-not-leak",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_AGENT_SDK_VERSION": "test",
    }
    completed = subprocess.run(
        [runner._sanitized_cli_path()], env=env, check=True, capture_output=True, text=True
    )
    child_env = dict(
        line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line
    )
    assert child_env["ANTHROPIC_API_KEY"] == "worker-key"
    assert child_env["CLAUDE_CODE_ENTRYPOINT"] == "sdk-py"
    assert "FISH_AUDIO_API_KEY" not in child_env
    assert "DEEPGRAM_API_KEY" not in child_env
    assert "SYNAPSE_API_TOKEN" not in child_env
    assert "SYNAPSE_KORA_REAL_CLI" not in child_env


def test_c6_journal_and_synapse_repo_are_protected_from_mutation(tmp_path):
    runner, _store, _journal = _runner(tmp_path)
    journal_file = Path(runner._cfg.journal_dir) / "state.json"
    repo = Path(kora_module.__file__).resolve().parents[2]

    assert runner._gate_decision("Write", {"file_path": str(journal_file)}) == (
        False, "protected_path", "protected_path"
    )
    assert runner._gate_decision("Write", {"file_path": str(repo / "synapse/bridge/kora.py")}) == (
        False, "protected_path", "protected_path"
    )
    # Protection is mutation-only: deterministic reads remain available.
    assert runner._gate_decision("Read", {"file_path": str(journal_file)})[0] is True


def test_c6_repo_protection_lifts_for_explicit_synapse_project_but_journal_does_not(tmp_path):
    runner, _store, _journal = _runner(tmp_path)
    repo = Path(kora_module.__file__).resolve().parents[2]
    runner._run_root = repo

    assert runner._gate_decision("Write", {"file_path": str(repo / "synapse/bridge/kora.py")})[0] is True
    journal_file = Path(runner._cfg.journal_dir) / "state.json"
    assert runner._gate_decision("Write", {"file_path": str(journal_file)})[2] == "protected_path"


def test_c6_bash_absolute_journal_path_is_denied(tmp_path):
    runner, _store, _journal = _runner(tmp_path)
    journal_file = Path(runner._cfg.journal_dir).resolve() / "state.json"
    allowed, reason, category = runner._gate_decision(
        "Bash", {"command": f"echo hacked > {journal_file}"}
    )
    assert (allowed, reason, category) == (False, "protected_path", "protected_path")


@pytest.mark.asyncio
async def test_c6_human_question_parking_does_not_spend_deadline(tmp_path, monkeypatch):
    runner, store, _journal = _runner(tmp_path, deadline_s=0.02)
    monkeypatch.setattr(kora_module, "_WATCHDOG_TICK_S", 0.005)
    store.start_task("task", "work", TaskStatus.RUNNING, 0.0)

    async def parked_stream(_task_id, _text):
        await runner._handle_question({"questions": [{"question": "Continue?", "options": []}]})
        store.set_task_status(TaskStatus.COMPLETED)

    runner._stream = parked_stream
    run_task = asyncio.create_task(runner._run("task", "work", RunSpec(thread_id="thread")))
    while not store.awaiting_answer:
        await asyncio.sleep(0)

    await asyncio.sleep(0.05)  # longer than the active budget
    assert not run_task.done()
    assert runner.provide_answer("yes") is True
    await asyncio.wait_for(run_task, 1.0)
    assert store.task.status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_c6_cancel_while_parked_tears_down_child_and_clears_slot(tmp_path, monkeypatch):
    runner, store, _journal = _runner(tmp_path, deadline_s=1.0)
    monkeypatch.setattr(kora_module, "_WATCHDOG_TICK_S", 0.005)
    store.start_task("task", "work", TaskStatus.RUNNING, 0.0)
    torn_down = asyncio.Event()

    async def parked_stream(_task_id, _text):
        try:
            await runner._handle_question({"questions": [{"question": "Continue?", "options": []}]})
        finally:
            torn_down.set()

    runner._stream = parked_stream
    run_task = asyncio.create_task(runner._run("task", "work"))
    while not store.awaiting_answer:
        await asyncio.sleep(0)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert torn_down.is_set()
    assert store.awaiting_answer is False
    assert runner._pending_answer is None
    assert store.task.status is TaskStatus.FAILED


@pytest.mark.asyncio
async def test_c3_voice_approval_id_is_persisted_with_launch(tmp_path):
    from synapse.pipeline.app import build_host

    cfg = SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path / "journal"), kora_workspace_dir=str(tmp_path / "workspace"),
    )
    host = build_host(cfg)

    class FakeRunner:
        def start(self, _task_id, _text, _spec):
            return None

    host.kora_runner = FakeRunner()
    thread = host.threads.create("approval audit")
    host.threads.set_stage(thread.id, "propose")
    host.threads.set_request(thread.id, "do it")
    first = await host.gate_action(thread.id, "send_to_kora", user_initiated=False)
    assert first["error"] == "confirm_required"
    host.approvals.note_user_turn(thread.id, "да", host.clock.now())
    assert (await host.gate_action(thread.id, "send_to_kora", user_initiated=False))["ok"] is True

    rows = [json.loads(line) for line in host.journal.path.read_text(encoding="utf-8").splitlines()]
    launch = next(row for row in rows if row.get("kind") == "gate_launch")
    assert launch["approval_id"].startswith("apr-")
    assert launch["thread_id"] == thread.id


@pytest.mark.asyncio
async def test_c3_missing_approval_service_fails_closed(tmp_path):
    from tests.test_phase0_approval import _gate_host, _propose

    host = _gate_host(tmp_path)
    host.approvals = None
    thread = _propose(host)
    result = await host.gate_action(
        thread.id, "send_to_kora", confirm=True, user_initiated=False
    )
    assert result == {"error": "approval_unavailable"}
    assert host.kora_runner.starts == []


@pytest.mark.asyncio
async def test_c2_disconnect_partial_tail_is_in_turn_journal(tmp_path):
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMAssistantAggregator,
        LLMUserAggregator,
    )
    from pipecat.utils.string import TextPartForConcatenation
    from tests.test_bugs_0714_realtime import _voice_host_or_skip

    host, session, stt, handler = _voice_host_or_skip(tmp_path)
    await handler(stt, "what is the status")
    aggregator = next(
        processor
        for processor in session.pipeline.processors
        if isinstance(processor, LLMAssistantAggregator)
        and not isinstance(processor, LLMUserAggregator)
    )
    aggregator._aggregation = [
        TextPartForConcatenation("задача выполнена", includes_inter_part_spaces=True)
    ]

    session.flush_voice_feed()

    rows = [json.loads(line) for line in host.journal.path.read_text(encoding="utf-8").splitlines()]
    turn = next(row for row in rows if row.get("kind") == "turn")
    assert turn["llm_output"] == "задача выполнена"
    assert host.journal.current is None
