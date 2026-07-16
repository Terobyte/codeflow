# -*- coding: utf-8 -*-
"""Красные (негативные) тесты для дефектов, найденных в полном код-ревью 2026-07-15.
Каждый тест утверждает ЖЕЛАЕМНОЕ поведение и падает сегодня — регрессионная броня,
которая позеленеет ровно когда баг починен (assertions не трогаются фиксом).

Покрытие (по срезам ревью, не задвоенное с существующими B-CASC-*/B-DISP-*/B-PIPE-*):
- B-CORE-7: TTSCache.get / get_speak_text — гонка TOCTOU exists()→read_*(): FileNotFoundError
  улетает в реалтайм-путь; кэш заявлен «best-effort, НИКОГДА не пробрасывать» (R-1).
- B-CORE-8: CostCap.reset() не чистит _reset_day → inconsistent state (день «забыт» после reset).
- B-CORE-9: DispatcherTurnLoop._dispatch_tool делает json.dumps(result) без защиты → TypeError
  на непоследовательном tool-результате убивает ВЕСЬ ход (а не один tool-вызов).
- B-CORE-10: CostCap.record_paid_attempt — check-then-increment без блокировки; синглтон шарится
  между голосовым каскадом (strategy.py) и текстовым каналом (llm_client.py), поэтому
  конкурентные admit-ы превышают дневной лимит сильнее задокументированного «≤1».

Все тесты офлайн, без сети/ключей (duck-typed fakes, как весь suite).
"""
from __future__ import annotations

import threading

import pytest

from synapse.cascade.services import CostCap
from synapse.pipeline.tts_cache import TTSCache


# ─── B-CORE-7 ──────────────────────────────────────────────────────────────────
# TTSCache.get / get_speak_text гоняют `if p.exists(): p.read_bytes()` без try/except.
# R-1 (tts_cache.py:184) заявляет обсервер/кэш «НИКОГДА не пробрасывает» в реалтайм-путь —
# но сами get-методы этого контракта не держат: между exists() и read_*() файл может исчезнуть
# (вытеснение из кэша, ручная очистка, гонка с _atomic_write под тем же ключом), и тогда
# FileNotFoundError улетает вызывающему. get() дёргается из реалтайм Play-пути (webrtc_server
# зовёт cache.get через asyncio.to_thread) — исключение там падает на аудио-ответ.

def test_b_core_7_tts_cache_get_must_not_raise_when_file_vanishes(tmp_path):
    cache = TTSCache(root=tmp_path, model="m", voice="v")
    wav_path = cache.wav_path("hello")
    wav_path.write_bytes(b"WAV")
    assert cache.get("hello") == b"WAV"
    # имитируем гонку: файл исчез МЕЖДУ exists() и read_bytes()
    wav_path.unlink()
    try:
        out = cache.get("hello")
    except FileNotFoundError:
        pytest.fail("B-CORE-7: cache.get() raised FileNotFoundError (TOCTOU exists→read)")
    # желаемое: кэш-промах как честный None, а не исключение
    assert out is None


def test_b_core_7_tts_cache_get_speak_text_must_not_raise_when_file_vanishes(tmp_path):
    cache = TTSCache(root=tmp_path, model="m", voice="v")
    txt_path = cache.speak_text_path("hello")
    txt_path.write_text("привет", encoding="utf-8")
    txt_path.unlink()  # та же гонка для санитайз-кэша
    try:
        out = cache.get_speak_text("hello")
    except FileNotFoundError:
        pytest.fail("B-CORE-7: get_speak_text() raised FileNotFoundError (TOCTOU exists→read)")
    assert out is None


# ─── B-CORE-8 ──────────────────────────────────────────────────────────────────
# CostCap.reset() обнуляет _count/_tripped, но НЕ _reset_day. После reset() синглтон «помнит»
# день, в котором считал, хотя сам счёт скинут — inconsistent state. Гарантия maybe_reset
# «bucket > _reset_day сбрасывает» сегодня держится, но reset() — это явная «полная отмена»
# (его зовут reset_tier-пути/тесты), и несброшенный _reset_day — мина для будущего читателя:
# первый же record_paid_attempt(now=тот-же-день) увидит «день уже установлен», и _count пойдёт
# расти с 0 поверх «старого» дня, что неотличимо от корректного поведения ровно до тех пор,
# пока кто-то не решит, что reset() = чистый лист.

def test_b_core_8_cost_cap_reset_must_clear_reset_day():
    cap = CostCap(max_paid_calls_per_day=3, rpd_reset_hour_utc=8)
    now = 1_000_000_000.0  # фиксированный «сегодня»
    cap.record_paid_attempt(now)
    assert cap._reset_day is not None  # день установлен после первого attempt
    cap.reset()
    # желаемое: reset() = полный чистый лист, день тоже
    assert cap._reset_day is None, "B-CORE-8: reset() left _reset_day set (inconsistent state)"
    assert cap.count == 0
    assert not cap.tripped


# ─── B-CORE-9 ──────────────────────────────────────────────────────────────────
# _dispatch_tool сериализует tool-результат через json.dumps(result, ensure_ascii=False) без
# защиты. Handler-обёртка ловит TypeError лишь от ВЫЗОВА handler'а (:304-307), но не от
# сериализации результата. Любой колбэк, вернувший непоследовательное значение (datetime, Path,
# кастомный объект из on_gate/on_propose), роняет json.dumps → TypeError → поднимается в
# ingest_user_turn → `except Exception: end_turn(); raise` → ход умирает целиком вместо того,
# чтобы вернуть одному tool-вызову ошибку и продолжить. Воспроизводится duck-typed: handler
# возвращает непоследовательный dict — _dispatch_tool обязан обернуть сериализацию.

@pytest.mark.xfail(reason="broken duplicate: sync-calls async _dispatch_tool (never awaited) → fails in its own setup, not on the code. B-CORE-9 FIXED 2026-07-16 (json.dumps default=str net); green proof: test_new_reported_bugs_failing.py::test_b_core_9", strict=False)
def test_b_core_9_dispatch_tool_must_not_kill_turn_on_non_serializable_result():
    from synapse.dispatcher.loop import DispatcherTurnLoop
    from synapse.dispatcher.tools import ToolCall

    class _NotJson:
        """Имитация значения, которое реальный колбэк может вернуть (Path/datetime/объект)."""

    class _FakeLLM:
        async def complete(self, messages, tools):
            return "", []

    class _FakeJournal:
        def begin_turn(self, *_a, **_k):
            class _R:
                turn_id = "t"
                thread_id = None
                llm_output = ""
                latency_ms = 0.0
            return _R()

        def end_turn(self, *_a, **_k):
            pass

        def check_grounding(self, *_a, **_k):
            pass

    class _FakeStore:
        def has_active_task(self):
            return False

        def liveness(self, *_a, **_k):
            return "ok"

        def snapshot(self, *_a, **_k):
            return {"task": None, "liveness": "ok"}

    class _FakeClock:
        def now(self):
            return 0.0

    from synapse.config import SynapseConfig
    loop = DispatcherTurnLoop(
        llm=_FakeLLM(),
        handlers=None,
        cfg=SynapseConfig(),
        clock=_FakeClock(),
        journal=_FakeJournal(),
        store=_FakeStore(),
        confirm_flow=None,
    )

    class _Call:
        id = "c1"
        name = "get_task_status"  # валидный tool; результат подсунем непоследовательный
        arguments = {}

    # monkeypatch handler → вернёт непоследовательное значение (как on_gate мог бы)
    loop.__dict__["_handlers"] = type(
        "H", (), {"get_task_status": staticmethod(lambda **_: {"obj": _NotJson()})}
    )()

    try:
        result = loop._dispatch_tool(_Call(), [])
    except TypeError:
        pytest.fail(
            "B-CORE-9: _dispatch_tool let json.dumps raise TypeError — a non-serializable "
            "tool result killed the turn instead of returning a per-tool error"
        )
    # желаемое: одна tool-ошибка, не крах хода; история получила content-запись
    assert isinstance(result, dict)


# ─── B-CORE-10 ─────────────────────────────────────────────────────────────────
# CostCap.record_paid_attempt — check-then-increment без всякой синхронизации (в services.py
# нет ни Lock, ни asyncio-примитива). Тот же синглтон потребляют ДВА пути на РАЗНЫХ loop-ах:
# голосовой каскад (strategy.py:103, pipecat-loop) и текстовый канал (llm_client.py:132,
# ASGI-loop). При конкурентных admit-ах каждый проходит tripped-чек до того, как любой
# инкрементнёт → превышение дневного лимита больше документированного «≤1 past the limit».
# Воспроизводим барьером: N потоков делают record_paid_attempt одновременно при max=N-2.

def test_b_core_10_cost_cap_record_paid_attempt_must_be_atomic_under_concurrency():
    cap = CostCap(max_paid_calls_per_day=5)
    now = 1_000_000_000.0
    cap.maybe_reset(now)  # якорим день, как делает monitor_forever перед ходом
    allowed = []

    def admit():
        allowed.append(cap.record_paid_attempt(now))

    # 20 конкурентных admit-ов при max=5. Атомарный check+increment пропустит ровно 5
    # (6-й увидит _tripped и получит False); гонка пропускает больше.
    workers = [threading.Thread(target=admit) for _ in range(20)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    admitted = sum(1 for a in allowed if a)
    # документированный потолок: ≤ max_paid_calls_per_day admits (овершот ≤0 при атомарности;
    # README обещает «overshoot bounded to ≤1 call past the limit» — т.е. ≤ max+1)
    assert admitted <= 5 + 1, (
        f"B-CORE-10: CostCap admitted {admitted} paid calls past a max of 5 under concurrency "
        f"(no lock on the shared voice+text singleton → cap overshoot exceeds the documented ≤1)"
    )
