"""Gate v2, часть C: серверные чат-команды compact/clear (tero run 2026-07-14).

- C1': exact-match всего сообщения (bare и slash), обработка ДО ingest_user_turn —
  LLM-ход диспетчера не зовётся, user-запись в ленту не пишется;
- C2': регидрация режет по последнему kind=="clear" + coalesce подряд same-role;
- C6: generation-гонка — ход, начатый до clear, не воскрешает очищенную историю;
- C3': анти-галлюцинационная строка в промпте диспетчера.

Роут-тесты — SimpleNamespace-хост (паттерн test_text_turn._api_host), loop-тесты — чистый
DispatcherTurnLoop со скриптованным LLM. Ни сети, ни SDK.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import TaskStore
from synapse.config import SynapseConfig
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.dispatcher.tools import KoraBridge, ToolHandlers
from synapse.journal import TurnJournal
from synapse.threads import ThreadStore


class FakeClock:
    def __init__(self, t=0.0): self.t = t
    def now(self): return self.t


class ScriptedLLM:
    def __init__(self): self.seen = []
    async def complete(self, messages, tools):
        self.seen.append(messages)
        return "ответ", []


def _loop(tmp_path, feed_reader=None, llm=None, on_compact=None):
    clock = FakeClock()
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
                          cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    handlers = ToolHandlers(KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg), journal)
    llm = llm or ScriptedLLM()
    return DispatcherTurnLoop(llm, handlers, confirm, store, journal, clock, cfg,
                              thread_feed_reader=feed_reader, on_compact=on_compact), llm


def _seed_history(loop, tid, pairs=3):
    hist = loop._history_for(tid)
    for i in range(pairs):
        hist.append({"role": "user", "content": f"вопрос {i}"})
        hist.append({"role": "assistant", "content": f"ответ {i}"})
    return hist


# --- роут-уровень: api_thread_message -----------------------------------------------------


def _webrtc_or_skip():
    pytest.importorskip("aiortc"); pytest.importorskip("cv2"); pytest.importorskip("fastapi")
    try:
        from synapse.pipeline import webrtc_server
        return webrtc_server
    except (ImportError, RuntimeError) as e:
        pytest.skip(f"webrtc deps unavailable: {e}")


def _endpoint(app, name):
    return next(r.endpoint for r in app.routes if getattr(getattr(r, "endpoint", None), "__name__", "") == name)


class FakeRequest:
    def __init__(self, body=None, origin="http://testserver", host="testserver"):
        self._body = body or {}
        self.headers = {"content-type": "application/json", "host": host, "origin": origin}
    async def json(self): return self._body


def _api_host(tmp_path):
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")

    def on_compact(tid):
        threads.append_feed(tid, {"ts": clock.now(), "kind": "event", "text": "контекст сжат"})

    loop_obj, llm = _loop(tmp_path, feed_reader=threads.read_feed, on_compact=on_compact)
    from synapse.projects import ProjectStore
    host = SimpleNamespace(
        clock=clock, store=loop_obj._store, threads=threads,
        projects=ProjectStore(tmp_path / "projects.json"),
        text_loop=loop_obj, turn_lock=asyncio.Lock(),
        current_http_thread={"id": None}, voice_thread={"id": None},
        voice_project={"id": None},
        # С2: роут зовёт journal.end_turn() и http_handlers.end_turn() на конце хода.
        journal=SimpleNamespace(close=lambda: None, end_turn=lambda: None,
                                check_grounding=lambda *a, **k: None),
        http_handlers=SimpleNamespace(end_turn=lambda: None),
    )
    return host, loop_obj, llm


async def test_compact_command_compacts_without_dispatcher_turn(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host, loop_obj, llm = _api_host(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("тред")
    hist = _seed_history(loop_obj, th.id)
    ep = _endpoint(app, "api_thread_message")

    import json
    resp = await ep(th.id, FakeRequest({"text": "compact"}))
    assert resp.status_code == 200
    assert json.loads(resp.body) == {"ok": True, "command": "compact"}
    # ровно ОДИН LLM-вызов — внутренняя суммаризация компакта, не ход диспетчера
    assert len(llm.seen) == 1
    assert "Сожми" in llm.seen[0][0]["content"]
    # история реально сжата: голова = [КОМПАКТ]-выжимка
    assert hist[0]["content"].startswith("[КОМПАКТ]")
    assert len(hist) < 6
    # в ленте НЕТ user-записи «compact»; событие «контекст сжат» пишет on_compact
    kinds = [e["kind"] for e in host.threads.read_feed(th.id)]
    assert "user" not in kinds
    assert kinds[-1] == "event"


async def test_command_variants_slash_and_case(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host, loop_obj, llm = _api_host(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("тред")
    ep = _endpoint(app, "api_thread_message")
    import json
    for text in ("/compact", "Compact", " CLEAR ", "/clear"):
        resp = await ep(th.id, FakeRequest({"text": text}))
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["ok"] is True and body["command"] in ("compact", "clear")
    assert llm.seen == []  # пустая история: даже compact не дошёл до суммаризации


async def test_clear_command_clears_history_and_writes_marker(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host, loop_obj, llm = _api_host(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("тред")
    hist = _seed_history(loop_obj, th.id)
    ep = _endpoint(app, "api_thread_message")

    import json
    resp = await ep(th.id, FakeRequest({"text": "clear"}))
    assert json.loads(resp.body) == {"ok": True, "command": "clear"}
    assert hist == []                      # LLM-история очищена in-place
    assert llm.seen == []                  # ни хода, ни суммаризации
    feed = host.threads.read_feed(th.id)
    assert [e["kind"] for e in feed] == ["clear"]
    assert feed[-1]["text"] == "история очищена"
    assert feed[-1]["id"].startswith("clear-")  # id-штамп против feedKey-коллизии


async def test_normal_text_is_not_a_command(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host, loop_obj, llm = _api_host(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("тред")
    ep = _endpoint(app, "api_thread_message")
    resp = await ep(th.id, FakeRequest({"text": "compact please"}))
    assert resp.status_code == 200
    # обычный ход: user+assistant в ленте, LLM-ход состоялся
    assert [e["kind"] for e in host.threads.read_feed(th.id)] == ["user", "assistant"]
    assert len(llm.seen) == 1


# --- loop-уровень: force_compact / clear_history / регидрация / generation ----------------


async def test_force_compact_noop_on_empty_history(tmp_path):
    loop_obj, llm = _loop(tmp_path)
    await loop_obj.force_compact("th-empty")
    assert llm.seen == []  # нечего жать — LLM не тронут


async def test_rehydration_cuts_at_last_clear_marker(tmp_path):
    feed = {"thX": [
        {"kind": "user", "text": "до clear"},
        {"kind": "assistant", "text": "старый ответ"},
        {"kind": "clear", "text": "история очищена", "id": "clear-1"},
        {"kind": "user", "text": "после clear"},
    ]}
    loop_obj, llm = _loop(tmp_path, feed_reader=lambda tid: feed.get(tid, []))
    hist = loop_obj._history_for("thX")
    assert hist == [{"role": "user", "content": "после clear"}]


async def test_rehydration_coalesces_consecutive_same_role(tmp_path):
    feed = {"thY": [
        {"kind": "user", "text": "раз"},
        {"kind": "user", "text": "два"},       # войс-реплики D1' идут подряд без ответа
        {"kind": "assistant", "text": "ответ"},
    ]}
    loop_obj, llm = _loop(tmp_path, feed_reader=lambda tid: feed.get(tid, []))
    hist = loop_obj._history_for("thY")
    assert hist == [
        {"role": "user", "content": "раз\nдва"},
        {"role": "assistant", "content": "ответ"},
    ]


async def test_clear_generation_skips_late_turn_commit(tmp_path):
    # C6 (sec-6): ход начат → clear во время его await → коммит хода СКИПАЕТСЯ,
    # очищенная история не воскресает парой (user, assistant).
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingLLM:
        def __init__(self): self.seen = []
        async def complete(self, messages, tools):
            self.seen.append(messages)
            entered.set()
            await release.wait()
            return "поздний ответ", []

    loop_obj, llm = _loop(tmp_path, llm=BlockingLLM())
    hist = loop_obj._history_for("thZ")

    turn = asyncio.create_task(loop_obj.ingest_user_turn("вопрос", thread_id="thZ"))
    await asyncio.wait_for(entered.wait(), 1.0)   # ход внутри LLM-вызова
    loop_obj.clear_history("thZ")                 # ← команда clear посреди хода
    release.set()
    _record, reply = await turn
    assert reply == "поздний ответ"               # сам ответ юзеру доехал
    assert hist == []                             # но история осталась очищенной


async def test_clear_history_survives_cold_thread(tmp_path):
    loop_obj, llm = _loop(tmp_path)
    loop_obj.clear_history("th-cold")  # кэш-мисс: не падает, только поколение
    assert loop_obj._generations["th-cold"] == 1


# --- C3': промпт диспетчера ----------------------------------------------------------------


def test_prompt_carries_anti_hallucination_commands_note():
    from synapse.prompt import build_system_prompt
    for owed in (True, False):  # вне owed-гейта
        prompt = build_system_prompt(SynapseConfig(include_owed_prompt_rules=owed))
        assert "Не обещай несуществующих режимов" in prompt
        assert "compact" in prompt and "clear" in prompt
        assert "текстовом чате" in prompt
