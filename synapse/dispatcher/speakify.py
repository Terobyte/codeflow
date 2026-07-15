"""speakify — очистка технического текста Коры перед озвучкой (tero run 2026-07-14). Код,
пути, идентификаторы плохо звучат голосом; direct Gemini Flash переписывает их короткими
человеческими описаниями. Любая ошибка / нет ключа → честный fallback на исходный текст
(Play всё равно должен звучать). Direct Gemini (GOOGLE_API_KEY), НЕ OpenRouter — отдельная
квота от tier1-каскада. transport-DI под httpx.MockTransport в тестах (паттерн llm_client)."""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_PROMPT = (
    "Перепиши текст для озвучки голосом: убери код, пути, идентификаторы и технический шум, "
    "замени короткими человеческими описаниями; сохрани смысл, факты и язык оригинала; "
    "не длиннее оригинала. Верни только текст для озвучки.\n\n"
)


async def speakify(
    text: str,
    *,
    api_key: str | None,
    model: str,
    timeout_s: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    if not text or not api_key:
        return text
    url = _GEMINI_URL.format(model=model)
    payload = {
        "contents": [{"parts": [{"text": _PROMPT + text}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 800},
    }
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout_s) as client:
            resp = await client.post(url, json=payload, headers={"x-goog-api-key": api_key})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        # HTTP-ошибка / таймаут / битый JSON / любое иное — честный fallback на исходный текст.
        logger.warning("speakify: Gemini call failed (%s); falling back to raw text", exc)
        return text
    try:
        parts = data["candidates"][0]["content"]["parts"]
        out = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError):
        out = ""
    return out or text
