"""UI-5 слайс «гигиена»: чистый контекст треда, компакт, rename/авто-title, архив, удаление проекта.

Task 8 — формальные якоря поверх UI-3-механики: код уже удовлетворяет инвариантам (история
ключуется по треду; регидрация берёт только kind=user/assistant; `_complete` принимает thread_id
явно, без мутируемого current_thread_id). Здесь — фиксация/регрессия.
"""
import asyncio
from types import SimpleNamespace

import pytest

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import TaskStore
from synapse.clock import FakeClock  # noqa: F401  (re-exported pattern from test_text_turn)
from synapse.config import SynapseConfig
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.dispatcher.tools import KoraBridge, ToolHandlers
from synapse.journal import TurnJournal


class ScriptedLLM:
    """Эхо последней user-реплики + числа user-сообщений; копит каждое увиденное сообщение."""

    def __init__(self) -> None:
        self.seen: list[list[dict]] = []

    async def complete(self, messages, tools):
        self.seen.append(messages)
        users = [m for m in messages if m.get("role") == "user"]
        return f"ok:{users[-1]['content']}:{len(users)}", []


def _loop(tmp_path, feed_reader=None, stage_block_for=None):
    clock = FakeClock()
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
                          cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    handlers = ToolHandlers(KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg), journal)
    llm = ScriptedLLM()
    loop = DispatcherTurnLoop(
        llm, handlers, confirm, store, journal, clock, cfg,
        thread_feed_reader=feed_reader, stage_block_for=stage_block_for,
    )
    return loop, llm


# --- Task 8: чистый контекст нового треда ----------------------------------------------


async def test_two_threads_do_not_leak_history(tmp_path):
    """История Б не содержит user/assistant реплик А (и наоборот)."""
    loop, llm = _loop(tmp_path)
    await loop.ingest_user_turn("первая реплика А", thread_id="thA")
    await loop.ingest_user_turn("вторая реплика А", thread_id="thA")
    # Б — отдельный тред, своя история
    await loop.ingest_user_turn("реплика Б", thread_id="thB")
    await loop.ingest_user_turn("ещё реплика Б", thread_id="thB")

    a_msgs = [m for m in llm.seen if any(m.get("role") == "user" and m.get("content") == "первая реплика А" for m in m)]
    # в любом сообщении, виденном на ходах А, не должно быть «реплика Б»
    for msgs in llm.seen:
        contents = str([m.get("content") for m in msgs])
        if "первая реплика А" in contents:
            assert "реплика Б" not in contents
        if "реплика Б" in contents:
            assert "первая реплика А" not in contents


async def test_thread_b_history_count_is_independent(tmp_path):
    """Ход в Б видит ровно свою историю (1 user), не накрученную ходами А."""
    loop, llm = _loop(tmp_path)
    await loop.ingest_user_turn("а1", thread_id="thA")
    await loop.ingest_user_turn("а2", thread_id="thA")
    _, reply = await loop.ingest_user_turn("б1", thread_id="thB")
    # ScriptedLLM эхо ok:<content>:<user-count> — у Б один user
    assert reply == "ok:б1:1"


async def test_cold_rehydration_reads_only_user_assistant(tmp_path):
    """Холодная регидрация тащит ТОЛЬКО kind=user/assistant; кора-виды и лента-события — нет."""
    feed = {"thX": [
        {"kind": "user", "text": "старый вопрос"},
        {"kind": "assistant", "text": "старый ответ"},
        {"kind": "gate_card", "stage": "propose", "action": "send_to_kora"},
        {"kind": "event", "text": "правки → сбор"},
        {"kind": "task", "text": "запуск задачи"},
        {"kind": "system", "text": "старт сессии"},
        {"kind": "thinking", "text": "размышление Коры"},
        {"kind": "tool_use", "text": "Write: ..."},
        {"kind": "tool_result", "text": "ок"},
        {"kind": "result", "text": "завершено"},
    ]}
    loop, llm = _loop(tmp_path, feed_reader=lambda tid: feed.get(tid, []))
    await loop.ingest_user_turn("новая", thread_id="thX")
    msgs = llm.seen[-1]
    # регидрированные реплики — на месте
    assert any(m.get("role") == "user" and m.get("content") == "старый вопрос" for m in msgs)
    assert any(m.get("role") == "assistant" and m.get("content") == "старый ответ" for m in msgs)
    # история = только user/assistant (после system-сообщения); НИ один display/kora-kind не
    # стал сообщением. Проверяем по составу ролей и по точному множеству контентов истории.
    history_msgs = [m for m in msgs if m.get("role") in ("user", "assistant")]
    assert {m["role"] for m in history_msgs} <= {"user", "assistant"}
    # ни один «запрещённый» текст из feed-видов не должен появиться как самостоятельное сообщение
    forbidden_texts = {
        "правки → сбор", "запуск задачи", "старт сессии",
        "размышление Коры", "Write: ...", "ок", "завершено",
    }
    actual_contents = {m.get("content") for m in history_msgs}
    leaked = forbidden_texts & actual_contents
    assert not leaked, f"NO-EXFIL нарушен: в историю попали feed-виды {leaked}"
    # gate_card — dict-запись, не строка; убеждаемся что её action-текст тоже не просочился
    assert not any(isinstance(m.get("content"), dict) for m in history_msgs)


async def test_state_block_is_global_across_threads(tmp_path):
    """Глобальный [СОСТОЯНИЕ]-блок одинаков для обоих тредов (синглтон store, тот же clock)."""
    loop, llm = _loop(tmp_path)
    await loop.ingest_user_turn("ход А", thread_id="thA")
    await loop.ingest_user_turn("ход Б", thread_id="thB")
    a_system = llm.seen[-2][0]  # первое сообщение хода А — system
    b_system = llm.seen[-1][0]  # первое сообщение хода Б — system
    assert a_system["role"] == "system" and b_system["role"] == "system"
    # state_block — общий хвост после промпта; часы не тикали (FakeClock) → идентичен
    assert a_system["content"] == b_system["content"]


async def test_complete_receives_thread_id_explicitly(tmp_path):
    """thread_id передаётся явно в _complete; нет мутируемого current_thread_id на loop."""
    loop, llm = _loop(tmp_path)
    # нет атрибута current_thread_id/_current_thread_id — инвариант «не вводи мутируемое поле»
    assert not hasattr(loop, "_current_thread_id")
    assert not hasattr(loop, "current_thread_id")
    await loop.ingest_user_turn("x", thread_id="thA")
    await loop.ingest_user_turn("y", thread_id="thB")
    # истории изолированы — значит thread_id дошёл до _history_for корректно на каждом ходе.
    # После хода история = [user, assistant]; user-реплика каждого треда своя и не протекла.
    a_user = [m for m in loop._histories["thA"] if m["role"] == "user"]
    b_user = [m for m in loop._histories["thB"] if m["role"] == "user"]
    assert [m["content"] for m in a_user] == ["x"]
    assert [m["content"] for m in b_user] == ["y"]


async def test_stage_block_dispatched_per_thread(tmp_path):
    """stage_block_for зовётся с правильным thread_id для каждого хода (без глобального стейта)."""
    seen_ids: list[str | None] = []
    loop, llm = _loop(
        tmp_path,
        stage_block_for=lambda tid: (seen_ids.append(tid), "СТАДИЙНЫЙ БЛОК")[1],
    )
    await loop.ingest_user_turn("ход А", thread_id="thA")
    await loop.ingest_user_turn("ход Б", thread_id="thB")
    assert seen_ids[-2:] == ["thA", "thB"]
    # системное сообщение каждого хода содержит стадийный блок
    assert "СТАДИЙНЫЙ БЛОК" in llm.seen[-2][0]["content"]
    assert "СТАДИЙНЫЙ БЛОК" in llm.seen[-1][0]["content"]
