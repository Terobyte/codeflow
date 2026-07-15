# -*- coding: utf-8-sig -*-
"""С2 — Жизненный цикл хода в журнале (Ф0.2).

Якоря DoD С2: каждый ПОСЛЕДОВАТЕЛЬНЫЙ ход имеет свою begin/end-пару и свой turn_id,
записи не текут в следующий ход. (Параллельное перекрытие — принятый B08-residual до Фазы 1.)
- JSONL содержит turn-строку на каждый HTTP-ход (сегодня RED — ходы сливались);
- два последовательных ходa → разные turn_id;
- late tool call после end_turn не приписан следующему ходу (anti-misattribution).
"""
import json
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.dispatcher.tools import KoraBridge, ToolHandlers
from synapse.journal import TurnJournal


class _ScriptedLLM:
    """Эхо последней user-реплики."""
    def __init__(self): self.seen = []
    async def complete(self, messages, tools):
        self.seen.append(messages)
        users = [m for m in messages if m["role"] == "user"]
        return f"ok:{users[-1]['content']}", []


def _loop(tmp_path):
    clock = FakeClock()
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
                          cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    handlers = ToolHandlers(KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg), journal)
    return DispatcherTurnLoop(_ScriptedLLM(), handlers, confirm, store, journal, clock, cfg), journal, handlers


def _read_turns(journal: TurnJournal) -> list[dict]:
    rows = [json.loads(line) for line in Path(journal.path).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return [r for r in rows if r.get("kind") == "turn"]


@pytest.mark.asyncio
async def test_http_turn_each_turn_writes_its_own_jsonl_line(tmp_path):
    """С2 DoD: JSONL содержит turn-строку на каждый HTTP-ход. Раньше ходы не закрывались на
    happy-path → B08-бэкстоп сливал все в одну запись, turn-строки не появлялись вовсе."""
    loop, journal, handlers = _loop(tmp_path)
    await loop.ingest_user_turn("ход один", thread_id="thA")
    journal.end_turn()  # роут message зовёт это в finally (имитация HTTP-пути)
    handlers.end_turn()
    await loop.ingest_user_turn("ход два", thread_id="thA")
    journal.end_turn()
    handlers.end_turn()
    turns = _read_turns(journal)
    assert len(turns) == 2, f"ожидал 2 turn-строки, got {len(turns)}"
    assert turns[0]["turn_id"] != turns[1]["turn_id"], "turn_id не вырос между ходами"


@pytest.mark.asyncio
async def test_sequential_turns_get_distinct_turn_ids(tmp_path):
    """С2 DoD: два последовательных хода → разные turn_id, записи не текут."""
    loop, journal, handlers = _loop(tmp_path)
    rec1, _ = await loop.ingest_user_turn("первый", thread_id="th")
    journal.end_turn(); handlers.end_turn()
    rec2, _ = await loop.ingest_user_turn("второй", thread_id="th")
    journal.end_turn(); handlers.end_turn()
    assert rec1.turn_id != rec2.turn_id
    # turn_id растёт монотонно
    n1 = int(rec1.turn_id.lstrip("t"))
    n2 = int(rec2.turn_id.lstrip("t"))
    assert n2 == n1 + 1


@pytest.mark.asyncio
async def test_end_turn_makes_current_journal_record_none(tmp_path):
    """С2: end_turn закрывает запись — journal.current None. Без этого следующий begin_turn
    подхватил бы ту же запись (B08-бэкстоп — мердж)."""
    loop, journal, handlers = _loop(tmp_path)
    await loop.ingest_user_turn("ход", thread_id="th")
    assert journal.current is not None
    journal.end_turn(); handlers.end_turn()
    assert journal.current is None


def test_handlers_end_turn_resets_last_turn_id(tmp_path):
    """С2 Task 2.3: handlers.end_turn() сбрасывает _last_turn_id. Поздний tool-хвост после
    конца хода получает честный turn_id="" (anti-misattribution), не id следующего хода."""
    clock = FakeClock()
    journal = TurnJournal(str(tmp_path / "j"), clock)
    cfg = SynapseConfig()
    handlers = ToolHandlers(KoraBridge(store=MagicMock(), confirm_flow=MagicMock(), clock=clock, cfg=cfg), journal)
    handlers.begin_turn("turn_5")
    assert handlers._last_turn_id == "turn_5"
    handlers.end_turn()
    assert handlers._last_turn_id is None


def test_handlers_end_turn_is_idempotent(tmp_path):
    """С2: повторный end_turn безвреден (роут зовёт и в finally, и голос — в on_commit/teardown)."""
    clock = FakeClock()
    journal = TurnJournal(str(tmp_path / "j"), clock)
    cfg = SynapseConfig()
    handlers = ToolHandlers(KoraBridge(store=MagicMock(), confirm_flow=MagicMock(), clock=clock, cfg=cfg), journal)
    handlers.begin_turn("turn_5")
    handlers.end_turn()
    handlers.end_turn()  # не падает
    assert handlers._last_turn_id is None


def test_late_tool_call_after_end_turn_not_misattributed(tmp_path):
    """С2 DoD: после end_turn поздний tool-call получает честный turn_id="" , а не
    приписывается закрытому ходу через _last_turn_id fallback.

    Модель: поздний tool-хвост бежит в pipecat-таске, чей ContextVar = default (трейлинг-таск
    не звал begin_turn) → getter `_current_turn_id_var.get() or self._last_turn_id` добирает
    fallback. Раньше fallback жил вечно (= последний begin) → хвост приписывался чужому ходу.
    С2 сбрасывает fallback в end_turn → getter даёт None → `_guarded` ключ ""."""
    clock = FakeClock()
    journal = TurnJournal(str(tmp_path / "j"), clock)
    cfg = SynapseConfig()
    store = TaskStore(clock)
    confirm = ConfirmFlow(store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
                          cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    handlers = ToolHandlers(KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg), journal)
    handlers.begin_turn("turn_A")
    assert handlers._last_turn_id == "turn_A"
    handlers.end_turn()  # ход закрыт → fallback сброшен
    # Существенная инварианта: fallback сброшен. Трейлинг-таск (ContextVar=default) доберёт его
    # через getter → None → `_guarded`/`record_tool_call` используют `or ""` → ключ "".
    assert handlers._last_turn_id is None
    # Прямая демонстрация: в свежем контексте (ContextVar default None) getter возвращает fallback.
    def _read_in_fresh_context():
        # copy_context копирует текущие значения; reset'им ContextVar в default внутри копии,
        # моделируя трейлинг-таск, который НЕ звал begin_turn.
        handlers._current_turn_id_var.set(None)  # трейлинг-таск не имеет своего id
        return handlers._current_turn_id
    trailing = _read_in_fresh_context()
    assert trailing is None
    assert (trailing or "") == ""  # _guarded/record_tool_call ключ для позднего хвоста
