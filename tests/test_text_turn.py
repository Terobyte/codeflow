"""UI v2 слайс UI-3: текстовый ход — llm-клиент, tool_use-шейп истории, пер-тред контекст."""
import json

import httpx
import pytest

from synapse.dispatcher.llm_client import AnthropicLLMClient
from synapse.dispatcher.tools import ALL_SCHEMAS, ToolCall


def _mock(response_json, capture):
    def handler(request: httpx.Request) -> httpx.Response:
        capture["request"] = json.loads(request.content)
        capture["headers"] = dict(request.headers)
        return httpx.Response(200, json=response_json)
    return httpx.MockTransport(handler)


async def test_complete_maps_messages_tools_and_parses_tool_use():
    capture = {}
    resp = {
        "content": [
            {"type": "text", "text": "Отправляю Коре."},
            {"type": "tool_use", "id": "tu_1", "name": "submit_task",
             "input": {"text": "сделай файл"}},
        ]
    }
    client = AnthropicLLMClient("k", "claude-haiku-4-5", transport=_mock(resp, capture))
    text, calls = await client.complete(
        [
            {"role": "system", "content": "промпт\n\n[СОСТОЯНИЕ]..."},
            {"role": "user", "content": "сделай файл"},
        ],
        ALL_SCHEMAS,
    )
    assert text == "Отправляю Коре."
    assert calls == [ToolCall(name="submit_task", arguments={"text": "сделай файл"}, id="tu_1")]
    req = capture["request"]
    assert req["model"] == "claude-haiku-4-5"
    assert "[СОСТОЯНИЕ]" in req["system"]
    assert req["messages"][0] == {"role": "user", "content": "сделай файл"}
    names = [t["name"] for t in req["tools"]]
    assert "submit_task" in names and "answer_kora" in names
    assert capture["headers"]["x-api-key"] == "k"


async def test_complete_round_trips_tool_results_as_blocks():
    capture = {}
    client = AnthropicLLMClient("k", "m", transport=_mock({"content": [{"type": "text", "text": "готово"}]}, capture))
    await client.complete(
        [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "запускаю",
             "tool_calls": [{"id": "tu_1", "name": "submit_task", "arguments": {"text": "x"}}]},
            {"role": "tool", "tool_call_id": "tu_1", "name": "submit_task", "content": "{\"outcome\": \"committed\"}"},
        ],
        ALL_SCHEMAS,
    )
    msgs = capture["request"]["messages"]
    assert msgs[1]["content"][0]["type"] == "text"          # assistant: текст+tool_use блоки
    assert msgs[1]["content"][1] == {"type": "tool_use", "id": "tu_1", "name": "submit_task",
                                     "input": {"text": "x"}}
    assert msgs[2]["content"][0]["type"] == "tool_result"   # user: tool_result с тем же id
    assert msgs[2]["content"][0]["tool_use_id"] == "tu_1"


from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import TaskStore
from synapse.config import SynapseConfig
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.dispatcher.tools import KoraBridge, ToolHandlers
from synapse.journal import TurnJournal


class FakeClock:
    def __init__(self, t=0.0): self.t = t
    def now(self): return self.t


class ScriptedLLM:
    """Возвращает текст с эхом ПОСЛЕДНЕЙ user-реплики и числа реплик в истории."""
    def __init__(self): self.seen = []
    async def complete(self, messages, tools):
        self.seen.append(messages)
        users = [m for m in messages if m["role"] == "user"]
        return f"ok:{users[-1]['content']}:{len(users)}", []


def _loop(tmp_path, feed_reader=None):
    clock = FakeClock()
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
                          cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    handlers = ToolHandlers(KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg), journal)
    llm = ScriptedLLM()
    return DispatcherTurnLoop(llm, handlers, confirm, store, journal, clock, cfg,
                              thread_feed_reader=feed_reader), llm


async def test_histories_are_isolated_per_thread(tmp_path):
    loop, llm = _loop(tmp_path)
    await loop.ingest_user_turn("привет из А", thread_id="thA")
    await loop.ingest_user_turn("привет из Б", thread_id="thB")
    record, reply = await loop.ingest_user_turn("ещё из А", thread_id="thA")
    # история треда А: 2 user-реплики, реплика Б НЕ просочилась
    assert reply == "ok:ещё из А:2"
    assert record.thread_id == "thA"
    a_msgs = llm.seen[-1]
    assert not any("из Б" in str(m.get("content", "")) for m in a_msgs)


async def test_cold_thread_rehydrates_from_feed(tmp_path):
    feed = {"thX": [
        {"kind": "user", "text": "старая реплика"},
        {"kind": "assistant", "text": "старый ответ"},
        {"kind": "tool_use", "text": "Write: ..."},   # кора-шаг — НЕ регидрируется (NO-EXFIL)
    ]}
    loop, llm = _loop(tmp_path, feed_reader=lambda tid: feed.get(tid, []))
    _, reply = await loop.ingest_user_turn("новая", thread_id="thX")
    msgs = llm.seen[-1]
    assert any(m["role"] == "user" and m["content"] == "старая реплика" for m in msgs)
    assert any(m["role"] == "assistant" and m["content"] == "старый ответ" for m in msgs)
    assert not any("Write:" in str(m.get("content", "")) for m in msgs)


# --- Task 14: HTTP API — projects/threads/feed/message + CSRF + C-guard -------------------
import asyncio
from types import SimpleNamespace

from synapse.bridge.state import TaskStatus
from synapse.threads import ThreadStore


def _webrtc_or_skip():
    pytest.importorskip("aiortc"); pytest.importorskip("cv2"); pytest.importorskip("fastapi")
    try:
        from synapse.pipeline import webrtc_server
        return webrtc_server
    except (ImportError, RuntimeError) as e:
        pytest.skip(f"webrtc deps unavailable: {e}")


def _endpoint(app, name):
    return next(r.endpoint for r in app.routes if getattr(getattr(r, "endpoint", None), "__name__", "") == name)


def _api_host(tmp_path):
    # Собрать РЕАЛЬНЫЙ host через build_host нельзя (ключи/сеть) — SimpleNamespace-стаб
    # с точными полями, которые читают роуты.
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    loop_obj, llm = _loop(tmp_path, feed_reader=threads.read_feed)
    from synapse.projects import ProjectStore
    return SimpleNamespace(
        clock=clock, store=loop_obj._store, threads=threads,
        projects=ProjectStore(tmp_path / "projects.json"),
        text_loop=loop_obj, turn_lock=asyncio.Lock(),
        current_http_thread={"id": None}, voice_thread={"id": None},
        journal=SimpleNamespace(close=lambda: None),
    )


class FakeRequest:
    def __init__(self, body=None, json_ct=True, origin=None, host="testserver"):
        self._body = body or {}
        self.headers = {"content-type": "application/json" if json_ct else "text/plain",
                        "host": host}
        if origin:
            self.headers["origin"] = origin
    async def json(self): return self._body


async def test_message_turn_is_thread_scoped_and_persisted(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("тред")
    ep = _endpoint(app, "api_thread_message")
    resp = await ep(th.id, FakeRequest({"text": "привет"}))
    assert resp.status_code == 200
    feed = host.threads.read_feed(th.id)
    assert [e["kind"] for e in feed] == ["user", "assistant"]


async def test_mutating_api_rejects_non_json_and_foreign_origin(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("тред")
    ep = _endpoint(app, "api_thread_message")
    assert (await ep(th.id, FakeRequest({"text": "x"}, json_ct=False))).status_code == 403
    assert (await ep(th.id, FakeRequest({"text": "x"}, origin="https://evil.example"))).status_code == 403


async def test_project_add_validates_path(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    ep = _endpoint(app, "api_projects_add")
    assert (await ep(FakeRequest({"name": "x", "path": "/etc"}))).status_code == 400
    proj_dir = tmp_path / "ok"; proj_dir.mkdir()
    assert (await ep(FakeRequest({"name": "x", "path": str(proj_dir)}))).status_code == 200


async def test_http_answer_guard_blocks_wrong_thread(tmp_path):
    # прямой юнит на замыкание _http_answer невозможен (живёт в build_host) — проверяем
    # правило на уровне его составляющих: awaiting-тред ≠ current_http_thread → False.
    clock = FakeClock()
    store = TaskStore(clock)
    threads = ThreadStore(clock, tmp_path / "threads")
    a = threads.create("A"); b = threads.create("B")
    threads.append_task(a.id, "t1")
    store.start_task("t1", "з", TaskStatus.RUNNING, 0.0)
    store.set_awaiting()
    current_http_thread = {"id": b.id}
    delivered = []

    def _http_answer(text: str) -> bool:  # копия правила из build_host
        task = store.task
        th = threads.thread_for_task(task.id) if task is not None else None
        awaiting = th.id if th is not None else None
        if awaiting is None or current_http_thread["id"] != awaiting:
            return False
        delivered.append(text)
        return True

    assert _http_answer("ответ из Б") is False and delivered == []
    current_http_thread["id"] = a.id
    assert _http_answer("ответ из А") is True and delivered == ["ответ из А"]
