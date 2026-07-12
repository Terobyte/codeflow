"""MockLLM — a deterministic, regex/word-routed stand-in for the real cascade LLM, used by
the console e2e runner and tests. A real LLM in the console is explicitly out of scope for
M0 (Plan v1 "simpler alternative rejected": two cascade implementations would drift) — this
is purely for exercising the bridge/tools/journal/arbiter machinery offline.

Routing is WORD-set based, not substring — Russian short words like "да" are substrings of
many unrelated words ("удали", "тогда", "когда"), so naive `in text` matching misfires.
"""
from __future__ import annotations

import json
import re
from typing import Any

from synapse.dispatcher.tools import ToolCall
from synapse.prompt import CANON_PHRASE_STALE_KORA

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

_DENY_WORDS = {"нет", "отмена", "стоп"}
_AFFIRM_WORDS = {"да", "подтверждаю", "делай"}
_STATUS_WORDS = {"статус", "готово", "дела", "прогресс", "осталось"}
_CANCEL_WORDS = {"отмени", "останови"}
_SUBMIT_WORDS = {"скачай", "сделай", "вырежи", "удали", "сотри", "запусти", "напиши", "создай"}


def _words(text: str) -> set[str]:
    return set(_PUNCT_RE.sub("", text.lower()).split())


class MockLLM:
    async def complete(self, messages: list[dict[str, Any]], tools: list[Any]) -> tuple[str, list[ToolCall]]:
        last = messages[-1] if messages else {"role": "user", "content": ""}
        if last.get("role") == "tool":
            return self._respond_to_tool_result(last)
        if last.get("role") != "user":
            return "", []

        text = last.get("content", "") or ""
        words = _words(text)

        # B27: confirm/deny only when the WHOLE utterance is confirmation words — «да, скачай отчёт»
        # must route to submit, not swallow the task as a confirm. Status/cancel/submit stay on
        # intersection: they carry payload words by nature.
        if words and words <= _DENY_WORDS:
            return "", [ToolCall("confirm_task", {"decision": "deny"})]
        if words and words <= _AFFIRM_WORDS:
            return "", [ToolCall("confirm_task", {"decision": "confirm"})]
        if words & _STATUS_WORDS:
            return "", [ToolCall("get_task_status", {})]
        if words & _CANCEL_WORDS:
            return "", [ToolCall("request_cancel", {})]
        if words & _SUBMIT_WORDS:
            return "", [ToolCall("submit_task", {"text": text})]
        return "Понял, но такой возможности у меня нет.", []

    def _respond_to_tool_result(self, last: dict[str, Any]) -> tuple[str, list[ToolCall]]:
        name = last.get("name")
        try:
            result = json.loads(last.get("content") or "{}")
        except json.JSONDecodeError:
            result = {}

        if name == "get_task_status":
            return self._describe_status(result), []
        if name == "submit_task":
            return self._describe_submit(result), []
        if name == "confirm_task":
            return self._describe_confirm(result), []
        if name == "request_cancel":
            outcome = result.get("outcome")
            if outcome == "cancel_requested":
                return "Передал запрос на отмену Коре.", []
            return "Сейчас нет активной задачи для отмены.", []
        return "Хорошо.", []

    def _describe_status(self, result: dict[str, Any]) -> str:
        if result.get("liveness") != "ok":
            return CANON_PHRASE_STALE_KORA
        task = result.get("task")
        if not task:
            return "Активных задач нет."
        status = task.get("status")
        phrases = {
            "pending_confirmation": "Задача ждёт твоего подтверждения.",
            "running": "Сигнала о завершении пока не было, задача выполняется.",
            "completed": "Задача завершена, детали должна была озвучить Кора.",
            "failed": "Задача завершилась с ошибкой.",
            "cancel_requested": "Запрос на отмену передан, жду подтверждения от Коры.",
        }
        return phrases.get(status, "Сигнала о завершении пока не было.")

    def _describe_submit(self, result: dict[str, Any]) -> str:
        if result.get("outcome") == "committed":
            return "Принял, передаю Коре."
        # staged / rejected_active already spoken via the SPEAK path (Р-16, tools.py).
        return ""

    def _describe_confirm(self, result: dict[str, Any]) -> str:
        if result.get("outcome") == "committed":
            return "Хорошо, передаю Коре."
        # rejected / rereadback / reset already spoken via the SPEAK path (Р-16, tools.py).
        return ""
