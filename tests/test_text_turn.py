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
