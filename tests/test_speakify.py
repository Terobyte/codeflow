"""speakify — санитайз текста Коры перед TTS (tero 2026-07-14). httpx.MockTransport в
transport= (паттерн test_text_turn.py): успех; HTTP 500 → исходный; таймаут/исключение →
исходный; api_key=None → исходный и ноль сетевых вызовов."""
import httpx

from synapse.dispatcher.speakify import speakify


def _gemini_ok(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


async def test_success_returns_gemini_text():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_gemini_ok("Готово, задача выполнена."))

    out = await speakify(
        "изменил app.py:1024, тесты зелёные", api_key="k", model="gemini-3.5-flash",
        timeout_s=5.0, transport=httpx.MockTransport(handler),
    )
    assert out == "Готово, задача выполнена."
    assert len(seen) == 1
    assert seen[0].headers["x-goog-api-key"] == "k"
    assert "gemini-3.5-flash:generateContent" in str(seen[0].url)


async def test_http_500_falls_back_to_original():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    src = "изменил app.py:1024"
    out = await speakify(src, api_key="k", model="m", timeout_s=5.0,
                         transport=httpx.MockTransport(handler))
    assert out == src


async def test_timeout_falls_back_to_original():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    src = "текст с кодом"
    out = await speakify(src, api_key="k", model="m", timeout_s=5.0,
                         transport=httpx.MockTransport(handler))
    assert out == src


async def test_no_api_key_returns_original_and_makes_no_calls():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_gemini_ok("nope"))

    src = "любой текст"
    out = await speakify(src, api_key=None, model="m", timeout_s=5.0,
                         transport=httpx.MockTransport(handler))
    assert out == src
    assert seen == []


async def test_empty_candidates_falls_back_to_original():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": []})

    src = "исходный"
    out = await speakify(src, api_key="k", model="m", timeout_s=5.0,
                         transport=httpx.MockTransport(handler))
    assert out == src
