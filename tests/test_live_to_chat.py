"""Gate v2, части B и D: «Завершить — в чат» + реплики звонка в ленте треда (tero run 2026-07-14).

- B1'/A12': /client/session-alive несёт voice_thread id — ТОЛЬКО когда host реально несёт
  voice_thread-словарь (голые стабы route-тестов сохраняют прежний payload);
- B2': лексические якоря app.js — навигация в тред звонка из disconnectVoice, render()
  не меняет привязку мид-колл;
- D1': eager-создание треда на первой голосовой реплике + kind:"user" в ленту;
- D3': context-diff флашер — assistant предыдущего хода на новом ходе, последний ответ
  на disconnect (flush_voice_feed), интеррапт = что в контексте, то и в ленте;
- D4': note_external_turn — тёплый кэш пополняется, холодный no-op.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from synapse.config import SynapseConfig

CLIENT_DIR = Path(__file__).parent.parent / "synapse" / "pipeline" / "client"
APP_DIR = Path(__file__).parent.parent / "synapse" / "pipeline"


def _webrtc_or_skip():
    pytest.importorskip("aiortc"); pytest.importorskip("cv2"); pytest.importorskip("fastapi")
    try:
        from synapse.pipeline import webrtc_server
        return webrtc_server
    except (ImportError, RuntimeError) as e:
        pytest.skip(f"webrtc deps unavailable: {e}")


def _endpoint(app, name):
    return next(r.endpoint for r in app.routes if getattr(getattr(r, "endpoint", None), "__name__", "") == name)


# =========================================================================================
# B1'/A12' — session-alive payload
# =========================================================================================


async def test_session_alive_carries_voice_thread_when_host_has_one():
    webrtc_server = _webrtc_or_skip()
    host = SimpleNamespace(voice_thread={"id": "th-call-1"}, journal=SimpleNamespace(close=lambda: None))
    app = webrtc_server.build_web_app(host=host)
    resp = await _endpoint(app, "session_alive")()
    assert json.loads(resp.body) == {"active": False, "voice_thread": "th-call-1"}


async def test_session_alive_stub_host_payload_unchanged():
    # A12': голый host-стаб (object) — прежний payload без ключа voice_thread
    # (test_slice5_pwa пинит его exact-равенством).
    webrtc_server = _webrtc_or_skip()
    app = webrtc_server.build_web_app(host=object())
    resp = await _endpoint(app, "session_alive")()
    assert json.loads(resp.body) == {"active": False}


# =========================================================================================
# B2' — лексические якоря app.js (навигация из disconnectVoice; render() не рвёт привязку)
# =========================================================================================


def _block_after(src: str, marker: str) -> str:
    # окно до следующего top-level объявления функции (или 4000 симв.) — render()/addEntry длинные
    i = src.index(marker)
    j = src.find("\nfunction ", i + 1)
    if j == -1 or j - i > 4000:
        j = i + 4000
    return src[i:j]


def test_app_js_disconnect_voice_navigates_to_call_thread():
    js = (CLIENT_DIR / "app.js").read_text(encoding="utf-8")
    body = _block_after(js, "async function disconnectVoice")
    assert "session-alive" in body, "disconnectVoice должен читать id треда звонка из session-alive"
    assert "voice_thread" in body
    assert 'location.hash = "#/thread/"' in body
    assert "pollFeed" in body and "loadLists" in body
    assert "innerHTML" not in js


def test_app_js_render_keeps_binding_while_voice_connected():
    js = (CLIENT_DIR / "app.js").read_text(encoding="utf-8")
    render_body = _block_after(js, "function render()")
    assert "!client" in render_body, "render() не должен слать active-thread пока голос подключён"


def test_app_js_renders_clear_kind_as_event_row():
    js = (CLIENT_DIR / "app.js").read_text(encoding="utf-8")
    add_body = _block_after(js, "function addEntry")
    assert '"clear"' in add_body, "kind:clear должен рендериться event-строкой (C4')"


# =========================================================================================
# D — реплики звонка в ленту (реальный build_host + build_session_pipeline, паттерн B13)
# =========================================================================================


def _voice_host_or_skip(tmp_path):
    pytest.importorskip("pipecat.services.deepgram.flux.stt")
    from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService

    from synapse.pipeline.app import build_host, build_session_pipeline

    cfg = SynapseConfig(
        google_api_key="fake-google-key",
        openrouter_api_key="fake-openrouter-key",
        anthropic_api_key="fake-anthropic-key",
        deepgram_api_key="fake-deepgram-key",
        fish_audio_api_key="fake-fish-key",
        fish_reference_id="fake-fish-ref",
        journal_dir=str(tmp_path),
    )
    host = build_host(cfg)
    session = build_session_pipeline(host)
    stt = next(p for p in session.pipeline.processors if isinstance(p, DeepgramFluxSTTService))
    handler = stt._event_handlers["on_end_of_turn"].handlers[0]
    return host, session, stt, handler


def _context_of(session):
    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator

    ua = next(p for p in session.pipeline.processors if isinstance(p, LLMUserAggregator))
    ctx = getattr(ua, "context", None)
    if ctx is None:
        ctx = ua._context
    return ctx


async def test_first_voice_turn_creates_thread_and_writes_user_entry(tmp_path):
    host, session, stt, handler = _voice_host_or_skip(tmp_path)
    assert host.voice_thread["id"] is None

    await handler(stt, "сделай мне сайт")

    tid = host.voice_thread["id"]
    assert tid is not None  # D1': eager-создание, буферов нет
    th = host.threads.get(tid)
    assert th is not None and th.title == "сделай мне сайт"  # maybe_autotitle из транскрипта
    feed = host.threads.read_feed(tid)
    assert [e["kind"] for e in feed] == ["user"]
    assert feed[0]["text"] == "сделай мне сайт"


async def test_context_diff_flushes_previous_assistant_on_next_turn(tmp_path):
    host, session, stt, handler = _voice_host_or_skip(tmp_path)
    await handler(stt, "первый вопрос")
    tid = host.voice_thread["id"]

    # эмулируем завершённый ход: агрегаторы дописали (user, assistant) в живой контекст
    ctx = _context_of(session)
    ctx.set_messages([
        *ctx.get_messages(),
        {"role": "user", "content": "первый вопрос"},
        {"role": "assistant", "content": "вот мой ответ"},
    ])

    await handler(stt, "второй вопрос")

    feed = host.threads.read_feed(tid)
    kinds = [e["kind"] for e in feed]
    assert kinds == ["user", "assistant", "user"]  # ответ ПРЕДЫДУЩЕГО хода — перед новым user
    assert feed[1]["text"] == "вот мой ответ"
    # user из context-diff НЕ дублируется (его пишет D1' напрямую): ровно 2 user-записи
    assert sum(1 for k in kinds if k == "user") == 2


async def test_disconnect_flush_writes_last_assistant_reply(tmp_path):
    host, session, stt, handler = _voice_host_or_skip(tmp_path)
    await handler(stt, "вопрос")
    tid = host.voice_thread["id"]

    ctx = _context_of(session)
    # интеррапт-семантика D3': в контексте лежит РЕАЛЬНО сказанное (обрезанное) — в ленту
    # уходит ровно оно, не полный ответ LLM.
    ctx.set_messages([
        *ctx.get_messages(),
        {"role": "user", "content": "вопрос"},
        {"role": "assistant", "content": "начал отвечать и был прер"},
    ])

    assert session.flush_voice_feed is not None
    session.flush_voice_feed()  # webrtc_server зовёт это в on_client_disconnected

    feed = host.threads.read_feed(tid)
    assert [e["kind"] for e in feed] == ["user", "assistant"]
    assert feed[1]["text"] == "начал отвечать и был прер"

    # повторный флаш (double-disconnect) идемпотентен: курсор уже съел сообщения
    session.flush_voice_feed()
    assert [e["kind"] for e in host.threads.read_feed(tid)] == ["user", "assistant"]


async def test_flush_skips_tool_and_non_string_content(tmp_path):
    host, session, stt, handler = _voice_host_or_skip(tmp_path)
    await handler(stt, "вопрос")
    tid = host.voice_thread["id"]

    ctx = _context_of(session)
    ctx.set_messages([
        *ctx.get_messages(),
        {"role": "tool", "content": "секретный tool-результат"},
        {"role": "assistant", "content": [{"type": "text", "text": "блочный"}]},  # не строка
        {"role": "assistant", "content": "   "},                                 # пустой
        {"role": "assistant", "content": "нормальный ответ"},
    ])
    session.flush_voice_feed()
    feed = host.threads.read_feed(tid)
    assert [e["kind"] for e in feed] == ["user", "assistant"]
    assert feed[1]["text"] == "нормальный ответ"


# =========================================================================================
# D4' — note_external_turn: тёплый кэш пополняется, холодный no-op
# =========================================================================================


def _bare_loop(tmp_path, feed_reader=None):
    from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
    from synapse.bridge.state import TaskStore
    from synapse.clock import FakeClock
    from synapse.dispatcher.loop import DispatcherTurnLoop
    from synapse.dispatcher.tools import KoraBridge, ToolHandlers
    from synapse.journal import TurnJournal

    clock = FakeClock(0.0)
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
                          cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    handlers = ToolHandlers(KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg), journal)

    class _LLM:
        async def complete(self, messages, tools): return "ок", []

    return DispatcherTurnLoop(_LLM(), handlers, confirm, store, journal, clock, cfg,
                              thread_feed_reader=feed_reader)


def test_note_external_turn_appends_to_warm_history(tmp_path):
    loop_obj = _bare_loop(tmp_path)
    hist = loop_obj._history_for("th1")  # прогрели кэш
    loop_obj.note_external_turn("th1", "user", "голосовая реплика")
    loop_obj.note_external_turn("th1", "assistant", "голосовой ответ")
    assert hist == [
        {"role": "user", "content": "голосовая реплика"},
        {"role": "assistant", "content": "голосовой ответ"},
    ]
    # coalesce: подряд same-role склеивается, а не плодит соседей
    loop_obj.note_external_turn("th1", "assistant", "ещё ответ")
    assert hist[-1]["content"] == "голосовой ответ\nещё ответ"


def test_note_external_turn_cold_cache_is_noop(tmp_path):
    feed = {"thC": [{"kind": "user", "text": "из ленты"}]}
    loop_obj = _bare_loop(tmp_path, feed_reader=lambda tid: feed.get(tid, []))
    loop_obj.note_external_turn("thC", "user", "тёплой истории нет")
    assert "thC" not in loop_obj._histories  # no-op: регидрация покроет из feed
    # холодная регидрация видит ленту, а не потерянный note
    assert loop_obj._history_for("thC") == [{"role": "user", "content": "из ленты"}]


# =========================================================================================
# D5' — лексический wiring: app.py зовёт флашер/ноут, webrtc_server зовёт flush на disconnect
# =========================================================================================


def test_app_py_wires_voice_feed_continuity():
    src = (APP_DIR / "app.py").read_text(encoding="utf-8")
    for token in ("_flush_voice_context", "note_external_turn", "maybe_autotitle",
                  "flush_voice_feed"):
        assert token in src, f"app.py wiring missing {token!r}"


def test_webrtc_server_flushes_on_disconnect():
    src = (APP_DIR / "webrtc_server.py").read_text(encoding="utf-8")
    assert "flush_voice_feed" in src
    idx = src.index("on_client_disconnected")
    assert "flush_voice_feed" in src[idx:idx + 1500], (
        "on_client_disconnected должен флашить context-diff (последний ответ звонка)")
