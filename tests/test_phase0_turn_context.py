# -*- coding: utf-8-sig -*-
"""С1 — TurnContext: единый контекст хода (Ф0.1).

Якоря DoD С1: «в каждом voice и HTTP вызове один и тот же `[СОСТОЯНИЕ]` snapshot».
- parity: при одинаковых входах system-строка голоса == system-строке HTTP;
- голосовой system message содержит `[СОСТОЯНИЕ]` (сегодня закрывало дыру — голос не видел
  состояния вообще);
- hide-скоуп в голосе: терминальная задача чужого треда спрятана;
- awaiting: `[СОСТОЯНИЕ]` в голосовом промпте несёт `awaiting_answer`.
"""
import pytest

from synapse.bridge.state import TaskStatus, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.dispatcher.turn_context import build_turn_context


def _cfg(tmp_path):
    return SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path),
    )


def test_build_turn_context_carries_state_block(tmp_path):
    """С1: фабрика собирает system_prompt + [СОСТОЯНИЕ] в system_message."""
    cfg = _cfg(tmp_path)
    store = TaskStore(FakeClock(1000.0))
    ctx = build_turn_context(cfg=cfg, store=store, clock=FakeClock(1000.0), thread_id=None)
    assert "[СОСТОЯНИЕ]" in ctx.state_block
    assert "[СОСТОЯНИЕ]" in ctx.system_message
    assert ctx.system_message == ctx.system_prompt + "\n\n" + ctx.state_block


def test_build_turn_context_state_hidden_for_foreign_terminal_task(tmp_path):
    """С1: hide-скоуп — терминальная задача чужого треда прячется из [СОСТОЯНИЕ] в голосе."""
    cfg = _cfg(tmp_path)
    clock = FakeClock(1000.0)
    store = TaskStore(clock)
    store.start_task("task_X", "чужая задача", TaskStatus.COMPLETED, 1000.0)
    # asking из треда A, задача принадлежит треду B
    owner_thread_for = lambda task_id: "thread_B"
    ctx = build_turn_context(
        cfg=cfg, store=store, clock=clock, thread_id="thread_A",
        owner_thread_for=owner_thread_for,
    )
    assert "чужая задача" not in ctx.state_block
    assert "Активной задачи нет." in ctx.state_block


def test_build_turn_context_shows_terminal_task_in_owner_thread(tmp_path):
    """С1: контраст — в родном треде терминальная задача показывается (не прячется)."""
    cfg = _cfg(tmp_path)
    clock = FakeClock(1000.0)
    store = TaskStore(clock)
    store.start_task("task_X", "своя задача", TaskStatus.COMPLETED, 1000.0)
    owner_thread_for = lambda task_id: "thread_A"
    ctx = build_turn_context(
        cfg=cfg, store=store, clock=clock, thread_id="thread_A",
        owner_thread_for=owner_thread_for,
    )
    assert "своя задача" in ctx.state_block


def test_build_turn_context_awaiting_answer_in_state(tmp_path):
    """С1: awaiting_answer попадает в [СОСТОЯНИЕ] → LLM видит основание позвать answer_kora
    без догадок (это и закрывало вероятностный роутинг голоса)."""
    cfg = _cfg(tmp_path)
    clock = FakeClock(1000.0)
    store = TaskStore(clock)
    store.start_task("task_X", "задача с вопросом", TaskStatus.RUNNING, 1000.0)
    store.set_awaiting()
    owner_thread_for = lambda task_id: "thread_A"
    ctx = build_turn_context(
        cfg=cfg, store=store, clock=clock, thread_id="thread_A",
        owner_thread_for=owner_thread_for,
    )
    assert "Кора ждёт твоего ответа" in ctx.state_block


def test_voice_and_http_system_message_parity(tmp_path):
    """С1 DoD: при одинаковых входах system-строка голоса == system-строке HTTP. Оба канала
    теперь идут через build_turn_context — parity по построению; якорь ловит расхождение,
    если кто-то вернёт инлайн-сборку в один из путей."""
    cfg = _cfg(tmp_path)
    clock = FakeClock(1000.0)
    store = TaskStore(clock)
    store.start_task("task_X", "общая задача", TaskStatus.RUNNING, 1000.0)
    stage = lambda tid: "[stage rules]"
    owner = lambda task_id: "thread_A"

    # «HTTP» (loop) и «голос» (host) — оба через одну фабрику с одинаковыми резолверами:
    http_ctx = build_turn_context(
        cfg=cfg, store=store, clock=clock, thread_id="thread_A",
        stage_block_for=stage, owner_thread_for=owner,
    )
    voice_ctx = build_turn_context(
        cfg=cfg, store=store, clock=clock, thread_id="thread_A",
        stage_block_for=stage, owner_thread_for=owner,
    )
    assert http_ctx.system_message == voice_ctx.system_message
