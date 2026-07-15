"""Детерминированный affirm/deny классификатор — общий для ConfirmFlow и ApprovalService.

Вынесен из confirm.py (С3, слайс ApprovalService): двухключевой контракт gate_action
переиспользует ровно ту же классификацию ответа пользователя, что и ConfirmFlow. Узкий,
лексический (не семантический) — false-positive/negative честно задокументированы.
"""
from __future__ import annotations

import re

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _normalize(text: str) -> str:
    return _PUNCT_RE.sub("", text.lower()).strip()


def _words(text: str) -> set[str]:
    return set(_normalize(text).split())


def _classify_response(text: str, affirm_words: frozenset[str], deny_words: frozenset[str]) -> str:
    """deny优先ствует над affirm (fail-safe: «нет, да» трактуется как deny). Возвращает
    'deny' | 'affirm' | 'unclear'. Бит-в-бит та же логика, что жила в confirm.py."""
    words = _words(text)
    if words & deny_words:
        return "deny"
    if words & affirm_words:
        return "affirm"
    return "unclear"


def classify_affirm(text: str, affirm_words: frozenset[str], deny_words: frozenset[str]) -> str:
    """Публичный алиас для ApprovalService — тот же контракт, что у ConfirmFlow."""
    return _classify_response(text, affirm_words, deny_words)
