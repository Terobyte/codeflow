# -*- coding: utf-8-sig -*-
"""Тест для проверки состояния гонки (concurrency race conditions) в SynapseHost и ToolHandlers."""
import asyncio
import pytest
from unittest.mock import MagicMock

from synapse.pipeline.app import TaskLocalThreadDict
from synapse.dispatcher.tools import ToolHandlers, KoraBridge
from synapse.journal import TurnJournal
from synapse.clock import FakeClock
from synapse.config import SynapseConfig

@pytest.mark.asyncio
async def test_current_http_thread_concurrency_isolation():
    # Создаем TaskLocalThreadDict
    d = TaskLocalThreadDict()
    d["id"] = None
    
    async def task_a():
        d["id"] = "thread_A"
        await asyncio.sleep(0.05)
        return d["id"]

    async def task_b():
        await asyncio.sleep(0.01)
        d["id"] = "thread_B"
        await asyncio.sleep(0.01)
        d["id"] = None
        return d["id"]

    res_a, res_b = await asyncio.gather(task_a(), task_b())
    
    # Task A должен сохранить свое значение thread_A, несмотря на то что Task B перезаписал его
    assert res_a == "thread_A"
    assert res_b is None

@pytest.mark.asyncio
async def test_tool_handlers_turn_id_concurrency_isolation(tmp_path):
    from unittest.mock import MagicMock
    from synapse.bridge.confirm import SubmitResult, ConfirmOutcome
    clock = FakeClock()
    journal = TurnJournal(str(tmp_path / "j"), clock)
    mock_confirm = MagicMock()
    mock_confirm.submit.return_value = SubmitResult(
        outcome=ConfirmOutcome.COMMITTED,
        task_id="t1",
        readback_text=None,
        reject_text=None
    )
    bridge = KoraBridge(store=MagicMock(), confirm_flow=mock_confirm, clock=clock, cfg=SynapseConfig())
    handlers = ToolHandlers(bridge, journal)
    
    async def run_turn_a():
        handlers.begin_turn("turn_A")
        await handlers.submit_task(text="task A")
        await asyncio.sleep(0.05)
        # Проверяем, что turn_id не изменился
        assert handlers._current_turn_id == "turn_A"
        # Проверяем, что в дедупе лежит правильная запись для turn_A
        turn_dedup = handlers._dedup.get("turn_A")
        assert turn_dedup is not None
        assert "submit_task" in turn_dedup
        assert turn_dedup["submit_task"].args == {"text": "task A"}

    async def run_turn_b():
        await asyncio.sleep(0.01)
        handlers.begin_turn("turn_B")
        await handlers.submit_task(text="task B")
        await asyncio.sleep(0.01)
        # Проверяем, что turn_id не изменился
        assert handlers._current_turn_id == "turn_B"
        turn_dedup = handlers._dedup.get("turn_B")
        assert turn_dedup is not None
        assert "submit_task" in turn_dedup
        assert turn_dedup["submit_task"].args == {"text": "task B"}

    await asyncio.gather(run_turn_a(), run_turn_b())
