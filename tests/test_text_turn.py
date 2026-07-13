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
