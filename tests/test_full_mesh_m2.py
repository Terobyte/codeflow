"""МЕШ-2: read-only consultations, durable case, and bounded lifecycle."""
from __future__ import annotations

import asyncio

import pytest

from synapse.bridge.kora import ConsultIdleTimeout, KoraRunner
from synapse.bridge.state import SpeakLedger, TaskStatus, TaskStore
from synapse.bridge.state import AwaitingRequest
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal
from synapse.threads import ThreadStore


def _runner(tmp_path, *, idle=0.05, on_case_entry=None):
    clock = FakeClock(1.0)
    store = TaskStore(clock)
    runner = KoraRunner(
        SynapseConfig(
            kora_workspace_dir=str(tmp_path / "ws"),
            kora_cli_path="/bin/echo",
            journal_dir=str(tmp_path / "journal"),
            consult_idle_timeout_s=idle,
        ),
        store,
        SpeakLedger(),
        clock,
        TurnJournal(str(tmp_path / "journal"), clock, session_id="mesh2"),
        None,
        on_case_entry=on_case_entry,
    )
    return runner, store


def test_consult_gate_is_unconditionally_read_only(tmp_path):
    runner, _ = _runner(tmp_path)
    runner._run_gate_mode = "consult"
    runner._run_root = tmp_path

    for tool, payload in (
        ("Write", {"file_path": str(tmp_path / "x")}),
        ("Edit", {"file_path": str(tmp_path / "x")}),
        ("NotebookEdit", {"notebook_path": str(tmp_path / "x.ipynb")}),
        ("Bash", {"command": "pwd"}),
    ):
        allowed, _, category = runner._gate_decision(tool, payload)
        assert allowed is False
        assert category == "consult_read_only"

    readable = tmp_path / "readme.txt"
    readable.write_text("ok", encoding="utf-8")
    assert runner._gate_decision("Read", {"file_path": str(readable)})[0] is True
    private = tmp_path / "journal" / "threads" / "th.case.md"
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text("memory", encoding="utf-8")
    assert runner._gate_decision("Read", {"file_path": str(private)})[2] == "consult_case_private"


@pytest.mark.asyncio
async def test_consult_reply_parks_resumes_and_records_answer(tmp_path):
    entries = []
    runner, store = _runner(tmp_path, on_case_entry=lambda *x: entries.append(x))
    store.start_task("c1", "brief", TaskStatus.RUNNING, 1.0)
    runner._run_owner = "c1"
    runner._run_thread_id = "th"
    runner._run_kind = "consult"
    tool = runner._build_reply_tool("c1")

    pending = asyncio.create_task(tool.handler({
        "speak_text": "Вижу два варианта.",
        "flow_instruction": "Уточни приоритет.",
        "answer_format": "приоритет: …",
        "final": False,
    }))
    await asyncio.sleep(0)
    request = store.awaiting
    assert request is not None and request.run_kind == "consult"
    assert entries == [("th", "Ответ Коры", "Вижу два варианта.")]
    assert runner.provide_answer(request.request_id, "скорость") == "answer_delivered"
    assert (await pending)["content"][0]["text"] == "скорость"


@pytest.mark.asyncio
async def test_consult_park_has_own_idle_timeout(tmp_path):
    runner, store = _runner(tmp_path, idle=0.01)
    store.start_task("c1", "brief", TaskStatus.RUNNING, 1.0)
    runner._run_kind = "consult"
    store.set_awaiting()
    child = asyncio.create_task(asyncio.Event().wait())
    with pytest.raises(ConsultIdleTimeout):
        await runner._watch_deadline(child, 10.0)
    with pytest.raises(asyncio.CancelledError):
        await child
    assert child.cancelled()


def test_case_file_is_host_side_and_aux_run_preserves_stage(tmp_path):
    threads = ThreadStore(FakeClock(1.0), tmp_path / "threads")
    thread = threads.create("idea")
    threads.set_stage(thread.id, "propose")
    checkpoint = threads.begin_aux_run(thread.id, "consult-1")
    assert threads.get(thread.id).stage == "propose"
    assert threads.append_case(thread.id, "Бриф Flow", "обсуди кеш") is True
    assert "обсуди кеш" in threads.read_case(thread.id)
    assert threads.case_path(thread.id).parent == tmp_path / "threads"
    assert threads.rollback_run(checkpoint) is True
    assert threads.get(thread.id).stage == "propose"


@pytest.mark.asyncio
async def test_host_consult_starts_and_resumes_without_moving_fsm(tmp_path):
    from synapse.pipeline.app import build_host

    cfg = SynapseConfig(
        google_api_key="g", openrouter_api_key="o", anthropic_api_key="a",
        deepgram_api_key="d", fish_audio_api_key="f", fish_reference_id="r",
        journal_dir=str(tmp_path), kora_workspace_dir=str(tmp_path / "ws"),
        kora_cli_path="/bin/echo",
    )
    host = build_host(cfg, FakeClock(1.0))
    thread = host.threads.create("idea")
    host.threads.set_stage(thread.id, "propose")

    class Runner:
        active_run_kind = None

        def start(self, task_id, text, spec):
            self.active_run_kind = spec.run_kind
            self.started = (task_id, text, spec)

        def provide_answer(self, request_id, text):
            self.delivered = (request_id, text)
            host.store.clear_awaiting(request_id)
            return "answer_delivered"

    runner = Runner()
    host.kora_runner = runner
    started = await host.consult_kora(thread.id, "сравни варианты кеша")
    assert started["outcome"] == "consult_started"
    assert runner.started[2].gate_mode == "consult"
    assert host.threads.get(thread.id).stage == "propose"

    task = host.store.task
    host.store.set_awaiting(AwaitingRequest(
        1, "r1", thread.id, task.id, "consult", "уточни нагрузку", "нагрузка: …", 1.0
    ))
    resumed = await host.consult_kora(thread.id, "нагрузка — 500 rps")
    assert resumed["outcome"] == "consult_resumed"
    assert runner.delivered == ("r1", "нагрузка — 500 rps")
    assert host.threads.get(thread.id).stage == "propose"
