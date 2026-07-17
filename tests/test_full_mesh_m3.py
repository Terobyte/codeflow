"""МЕШ-3: bounded autonomous Flow follow-ups and stage-independent conversation."""
from __future__ import annotations

import asyncio

import pytest

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import AwaitingRequest, SpeakLedger, TaskStatus, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.dispatcher.tools import KoraBridge, ToolCall, ToolHandlers
from synapse.journal import TurnJournal


def _cfg(tmp_path, *, budget=1):
    return SynapseConfig(
        google_api_key="g", openrouter_api_key="o", anthropic_api_key="a",
        deepgram_api_key="d", fish_audio_api_key="f", fish_reference_id="r",
        journal_dir=str(tmp_path), kora_workspace_dir=str(tmp_path / "ws"),
        kora_cli_path="/bin/echo", autonomy_budget=budget,
    )


class _Runner:
    active_run_kind = None

    def __init__(self, host):
        self.host = host

    def start(self, task_id, text, spec):
        self.active_run_kind = spec.run_kind

    def provide_answer(self, request_id, text):
        self.host.store.clear_awaiting(request_id)
        return "answer_delivered"


@pytest.mark.asyncio
async def test_consult_is_available_in_code_stage_and_preserves_fsm(tmp_path):
    from synapse.pipeline.app import build_host

    host = build_host(_cfg(tmp_path), FakeClock(1.0))
    thread = host.threads.create("idea")
    host.threads.set_stage(thread.id, "propose")
    host.threads.set_stage(thread.id, "code")
    host.kora_runner = _Runner(host)

    result = await host.consult_kora(thread.id, "оцени риск")

    assert result["outcome"] == "consult_started"
    assert host.threads.get(thread.id).stage == "code"


@pytest.mark.asyncio
async def test_autonomy_budget_is_per_session_and_user_turn_does_not_replenish(tmp_path):
    from synapse.pipeline.app import build_host

    host = build_host(_cfg(tmp_path, budget=1), FakeClock(1.0))
    thread = host.threads.create("idea")
    runner = _Runner(host)
    runner.active_run_kind = "consult"
    host.kora_runner = runner
    host.store.start_task("consult-1", "brief", TaskStatus.RUNNING, 1.0)
    host._consult_budget_remaining["consult-1"] = 1
    spoken = []
    host.speak = spoken.append

    def park(request_id):
        host.store.set_awaiting(AwaitingRequest(
            1, request_id, thread.id, "consult-1", "consult", "уточни", "ответ: …", 1.0,
        ))

    park("r1")
    first = await host.consult_kora(thread.id, "первый follow-up", autonomous=True)
    assert first["outcome"] == "consult_resumed"
    assert spoken == ["Я ещё раз уточню это у Коры."]

    # Background/user STT fans out into approval services, but autonomy is session-owned.
    host.approvals.note_user_turn(thread.id, "фоновая реплика", 2.0)
    host.answer_approvals.note_user_turn(thread.id, "фоновая реплика", 2.0)
    park("r2")
    second = await host.consult_kora(thread.id, "второй follow-up", autonomous=True)
    assert second["outcome"] == "autonomy_budget_exhausted"
    assert spoken == ["Я ещё раз уточню это у Коры."]


@pytest.mark.asyncio
async def test_park_callback_schedules_exact_request(tmp_path):
    from synapse.bridge.kora import KoraRunner

    clock = FakeClock(1.0)
    store = TaskStore(clock)
    parked = []
    runner = KoraRunner(
        SynapseConfig(
            kora_workspace_dir=str(tmp_path / "ws"), kora_cli_path="/bin/echo",
            journal_dir=str(tmp_path / "j"),
        ),
        store, SpeakLedger(), clock,
        TurnJournal(str(tmp_path / "j"), clock), None,
        on_consult_parked=parked.append,
    )
    store.start_task("c1", "brief", TaskStatus.RUNNING, 1.0)
    runner._run_owner = "c1"
    runner._run_thread_id = "th"
    runner._run_kind = "consult"
    pending = asyncio.create_task(runner._build_reply_tool("c1").handler({
        "speak_text": "ответ", "flow_instruction": "уточни", "final": False,
    }))
    await asyncio.sleep(0)
    assert len(parked) == 1
    assert parked[0] == store.awaiting
    assert runner.provide_answer(parked[0].request_id, "бриф") == "answer_delivered"
    await pending


@pytest.mark.asyncio
async def test_autonomous_dispatcher_turn_has_only_consult_tool_and_no_user_fanout(tmp_path):
    clock = FakeClock(1.0)
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(
        store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
        cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s,
    )
    delivered = []
    bridge = KoraBridge(
        store=store, confirm_flow=confirm, clock=clock, cfg=cfg,
        on_consult=lambda briefing: delivered.append(briefing) or {"outcome": "ok"},
    )
    handlers = ToolHandlers(bridge, journal)

    class LLM:
        def __init__(self):
            self.calls = 0
            self.tool_names = []

        async def complete(self, messages, tools):
            self.calls += 1
            self.tool_names.append([tool.name for tool in tools])
            if self.calls == 1:
                return "", [ToolCall("consult_kora", {"briefing": "follow-up"}, "tc1")]
            return "готово", []

    fanout = []
    llm = LLM()
    loop = DispatcherTurnLoop(
        llm, handlers, confirm, store, journal, clock, cfg,
        on_user_turn=lambda *args: fanout.append(args),
    )
    await loop.ingest_autonomous_turn("internal", "th")

    assert delivered == ["follow-up"]
    assert fanout == []
    assert llm.tool_names == [["consult_kora"], ["consult_kora"]]
    assert journal.current is None


def test_autonomy_budget_env_is_defensive_and_non_negative():
    assert SynapseConfig.from_env({"KORA_AUTONOMY_BUDGET": "3"}).autonomy_budget == 3
    assert SynapseConfig.from_env({"KORA_AUTONOMY_BUDGET": "-2"}).autonomy_budget == 0
    assert SynapseConfig.from_env({"KORA_AUTONOMY_BUDGET": "wat"}).autonomy_budget == 1
