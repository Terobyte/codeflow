"""Realtime «зомби-мик» (баг Теро 2026-07-14): realtime-overlay САМ уходит в чат, а WebRTC-
клиент остаётся жив — микрофон слушает/пишет, хотя UI показывает обычный чат. Лечится только
reload'ом.

Корень (подтверждён чтением client/app.js + vendor/pipecat.mjs):
  - `#live-overlay.hidden` пишет ТОЛЬКО `syncLiveOverlay`, зовётся ТОЛЬКО из `setMicState`;
    `setMicState("error"|"idle")` прячет overlay (liveRequested=false) и обнуляет `client`.
  - Локальный мик-трек отпускается ТОЛЬКО внутри vendored `stop()` (`mediaManager.disconnect()`),
    достижимого лишь через `client.disconnect()`. Закрытие peer-connection трек НЕ глушит (MDN).
  - vendored транспорт авто-дисконнектит только на `fatal` ошибках; дефолт `fatal:false` →
    любой не-фатальный ErrorFrame летит в наш `onError`.
  - Дефект-КЛАСС: пути, которые обнуляют `client`/прячут overlay по ошибке, НЕ зовут
    `disconnect()` → транспорт + getUserMedia остаются живы, а handle потерян (zombie).

Браузера в CI нет (конвенция test_ui_client.py: «никакого браузера в CI»), клиентская
state-машина рантаймом из pytest недостижима — поэтому пиним ИНВАРИАНТ на уровне исходника,
структурно (тело каждого abandon-блока обязано звать teardown транспорта), а не магик-токеном.
"""
from __future__ import annotations

from pathlib import Path

import pytest

CLIENT_DIR = Path(__file__).parent.parent / "synapse" / "pipeline" / "client"


def _app_js() -> str:
    return (CLIENT_DIR / "app.js").read_text(encoding="utf-8")


def _block_after(src: str, marker: str, start: int = 0) -> str:
    """Тело `{...}`-блока, начиная с ПЕРВОЙ `{` на/после `marker` (искомого от `start`),
    балансировкой скобок. String-aware: скобки внутри "…"/'…'/`…` не считаются (в этих блоках их
    и нет, но не полагаемся на удачу). Возвращает текст между внешними { } (без самих скобок).
    `start` нужен для catch-блоков: маркер `catch` не уникален, позицию даёт вызывающий."""
    i = src.index(marker, start)
    while src[i] != "{":
        i += 1
    depth = 0
    start = i
    quote = None
    while i < len(src):
        c = src[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start + 1 : i]
        i += 1
    raise AssertionError(f"незакрытый блок после {marker!r}")


def _calls_teardown(block: str) -> bool:
    """Блок реально гасит транспорт: прямой `.disconnect(` или общий teardown-хелпер."""
    return ".disconnect(" in block or "abandonVoice(" in block


# ── Дефект #1 (primary): onError прячет overlay + обнуляет client БЕЗ disconnect ────────────
def test_onError_tears_down_transport_not_just_ui():
    js = _app_js()
    body = _block_after(js, "onError:")
    # onError уже переводит UI в «ошибку» и роняет handle — значит ОБЯЗАН и погасить транспорт,
    # иначе pipecat держит мик живым, а кнопки/вотчдог мертвы (zombie-мик до reload).
    assert 'setMicState("error"' in body, "sanity: onError должен уводить UI в состояние ошибки"
    assert _calls_teardown(body), (
        "onError обнуляет client и прячет overlay, но НЕ зовёт disconnect() — "
        "локальный мик-трек остаётся жив (зомби-мик)"
    )


# ── Дефект #2: провал авто-реконнекта бросает client БЕЗ disconnect ─────────────────────────
def test_auto_reconnect_failure_tears_down_orphaned_client():
    js = _app_js()
    # Тело catch, содержащего лог «auto-reconnect failed» (сканируем от предшествующего catch).
    idx = js.index("auto-reconnect failed")
    catch_at = js.rindex("catch", 0, idx)
    body = _block_after(js, "catch", start=catch_at)  # именно этот catch, не первый в файле
    assert _calls_teardown(body), (
        "провал connectVoice в вотчдоге обнуляет client, но не гасит только что поднятый "
        "транспорт — мик, захваченный на реконнекте, осиротеет"
    )


# ── Контракт, который фикс НЕ должен сломать: happy-path hang-up всё ещё глушит мик ──────────
def test_disconnectVoice_still_stops_transport():
    js = _app_js()
    body = _block_after(js, "async function disconnectVoice(")
    assert ".disconnect(" in body, "disconnectVoice обязан звать c.disconnect() (teardown мика)"


def test_connect_failure_catch_stops_transport():
    js = _app_js()
    idx = js.index('console.error("voice connect failed:')
    catch_at = js.rindex("catch", 0, idx)  # это тело catch (лог внутри), поднимаемся к его 'catch'
    body = _block_after(js, "catch", start=catch_at)
    assert _calls_teardown(body), "провал первичного connect обязан гасить транспорт"


# ── Инвариант-«нет тихого abandon»: overlay нельзя погасить, не погасив транспорт ────────────
def test_no_error_state_without_teardown_in_voice_paths():
    """Каждый `setMicState("error", …)` в голосовых обработчиках соседствует с teardown-вызовом
    в своём же блоке. Ловит регрессию, где новый путь снова роняет UI, забыв про мик."""
    js = _app_js()
    for marker, block_start in (
        ("onError:", "onError:"),
        ("auto-reconnect failed", None),
        ('console.error("voice connect failed:', None),
    ):
        if block_start is not None:
            body = _block_after(js, block_start)
        else:
            idx = js.index(marker)
            catch_at = js.rindex("catch", 0, idx)
            body = _block_after(js, "catch", start=catch_at)
        if 'setMicState("error"' in body:
            assert _calls_teardown(body), f"error-состояние без teardown в блоке у {marker!r}"
