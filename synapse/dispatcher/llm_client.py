"""AnthropicLLMClient — реализация протокола LLMClient (loop.py) поверх Anthropic
Messages API для ТЕКСТОВЫХ ходов диспетчера (UI-3, POST /message). Голосовой путь
(pipecat-каскад с failover) не тронут: это отдельный, дешёвый и синхронный клиент
одного tier'а. Ключ — из SynapseConfig (env), никогда не хардкод."""
from __future__ import annotations

import json
from typing import Any

import httpx

from synapse.dispatcher.tools import ToolCall

_API_URL = "https://api.anthropic.com/v1/messages"
_VERSION = "2023-06-01"


def _schema_to_tool(schema: Any) -> dict[str, Any]:
    return {
        "name": schema.name,
        "description": schema.description,
        "input_schema": {
            "type": "object",
            "properties": schema.properties or {},
            "required": schema.required or [],
        },
    }


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    out: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for c in m.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"],
                               "input": c["arguments"]})
            out.append({"role": "assistant", "content": blocks or m.get("content", "")})
        elif m["role"] == "tool":
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": m["content"],
                }],
            })
    return system, out


class AnthropicLLMClient:
    def __init__(self, api_key: str, model: str, timeout_s: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_s
        self._transport = transport

    async def complete(self, messages: list[dict[str, Any]], tools: list[Any]) -> tuple[str, list[ToolCall]]:
        system, msgs = _to_anthropic_messages(messages)
        payload = {
            "model": self._model,
            "max_tokens": 1024,
            "system": system,
            "messages": msgs,
            "tools": [_schema_to_tool(s) for s in tools],
        }
        async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
            resp = await client.post(
                _API_URL, json=payload,
                headers={"x-api-key": self._api_key, "anthropic-version": _VERSION},
            )
            resp.raise_for_status()
            data = resp.json()
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(name=block["name"], arguments=block.get("input") or {},
                                      id=block.get("id", "")))
        return "".join(text_parts), calls
