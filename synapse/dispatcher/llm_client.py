"""AnthropicLLMClient — реализация протокола LLMClient (loop.py) поверх Anthropic
Messages API для ТЕКСТОВЫХ ходов диспетчера (UI-3, POST /message). Голосовой путь
(pipecat-каскад с failover) не тронут: это отдельный, дешёвый и синхронный клиент
одного tier'а. Ключ — из SynapseConfig (env), никогда не хардкод."""
from __future__ import annotations

import json
from typing import Any

import httpx

from synapse.cascade.services import CostCap
from synapse.clock import Clock
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
    # Buffer for a run of consecutive tool messages so their tool_result blocks
    # coalesce into ONE user message (canonical Anthropic parallel-tool-use shape).
    tool_results: list[dict[str, Any]] = []

    def _flush_tool_results() -> None:
        if tool_results:
            out.append({"role": "user", "content": list(tool_results)})
            tool_results.clear()

    for m in messages:
        if m["role"] == "tool":
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": m["content"],
            })
            continue
        _flush_tool_results()
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
    _flush_tool_results()
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


class CostCapBlocked(RuntimeError):
    """Ф0.4: дневной лимит платных запросов исчерпан — запрос НЕ уходит в paid tier."""


class ProviderUnavailable(RuntimeError):
    """CR-7: провайдер-сбой (httpx-транспорт) нормализован в один тип для fallback-а."""


class GuardedLLMClient:
    """Обёртка текстового канала (С4): request-time блокировка cost cap ДО сетевого вызова
    (Ф0.4) + нормализация провайдер-сбоев в ProviderUnavailable для детерминированного
    fallback-а в роуте (CR-7). Считает КАЖДЫЙ complete — tool-пассы и компакт-вызовы тоже
    платные. Тот же cost_cap-синглтон, что у голосового каскада: один дневной лимит на оба
    канала, а не два раздельных.

    Полиморфен с LLMClient (loop.py) — implements complete(). loop/роут не видят разницы
    между AnthropicLLMClient и GuardedLLMClient; fallback ловит типы исключений в роуте."""

    def __init__(self, inner: AnthropicLLMClient, cost_cap: CostCap, clock: Clock) -> None:
        self._inner = inner
        self._cost_cap = cost_cap
        self._clock = clock

    async def complete(self, messages: list[dict[str, Any]], tools: list[Any]) -> tuple[str, list[ToolCall]]:
        now = self._clock.now()
        # Reserve synchronously BEFORE the network await.  A separate tripped-check followed by
        # a post-response increment lets every concurrent request pass the check and overshoot.
        # CostCap.record_paid_attempt is the atomic check+reservation seam and also handles the
        # daily reset.  Provider failures remain attempts: the paid slot was consumed when the
        # request was admitted, not retroactively after a successful response.
        if not self._cost_cap.record_paid_attempt(now):
            raise CostCapBlocked()
        try:
            out = await self._inner.complete(messages, tools)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            # CR-7: нормализуем любой провайдер-сбой в один тип → роут даёт детерминированную
            # реплику вместо 500 без ответа.
            raise ProviderUnavailable(str(exc)) from exc
        return out
