"""UI v2 слайс UI-1: вендор-бандл, наша статика /client, роут-своп, mount-order (S24/S26).
Лексические проверки — паттерн test_kora_status_ui.py (никакого браузера в CI)."""
import re
from pathlib import Path

import pytest

CLIENT_DIR = Path(__file__).parent.parent / "synapse" / "pipeline" / "client"

# Строковые литералы внутри минифицированного кода не матчатся: ищем именно import/from
# как STATEMENT (обязательный пробел до кавычки), без ./ в начале — bare specifier ломает
# «ноль сборки» в браузере. \s+ не \s*: «from":"./dist» в package.json-метаданных — не импорт.
_BARE_IMPORT_RE = re.compile(r'(?:\bfrom\s+|\bimport\s+)"(?![\./])([^"]+)"')


def test_vendor_bundle_is_self_contained():
    bundle = (CLIENT_DIR / "vendor" / "pipecat.mjs").read_text(encoding="utf-8")
    bare = _BARE_IMPORT_RE.findall(bundle)
    assert bare == [], f"bare-specifier imports break zero-build serving: {bare}"
    for exported in ("PipecatClient", "SmallWebRTCTransport", "RTVIEvent"):
        assert exported in bundle, f"vendor bundle lost export {exported}"


def test_vendor_md_pins_versions_and_license():
    md = (CLIENT_DIR / "vendor" / "VENDOR.md").read_text(encoding="utf-8")
    for token in ("1.12.0", "1.10.5", "0.25.5", "BSD-2-Clause", "vendor_pipecat.sh"):
        assert token in md


def test_our_index_is_pwa_wrapper_and_wires_our_scripts():
    body = (CLIENT_DIR / "index.html").read_text(encoding="utf-8")
    for token in (
        "Открыть агента", "manifest.webmanifest", "apple-touch-icon",
        "reconnect.js", "app.js", "style.css", "kora-dot", "bot-audio",
        'lang="ru"', "viewport-fit=cover",
    ):
        assert token in body, f"index.html missing {token!r}"
    assert "status-widget.js" not in body  # светофор у нас нативный, не инжект-виджет


def test_app_js_polls_status_and_is_xss_safe():
    body = (CLIENT_DIR / "app.js").read_text(encoding="utf-8")
    for token in ("kora-status", "visibilitychange", "textContent", "./logs"):
        assert token in body
    assert "innerHTML" not in body
    assert "window.open" not in body  # R3: standalone iOS PWA — навигация, не окна


def test_app_js_wires_voice_through_vendored_sdk():
    body = (CLIENT_DIR / "app.js").read_text(encoding="utf-8")
    for token in (
        './vendor/pipecat.mjs"', "PipecatClient", "SmallWebRTCTransport",
        'webrtcUrl: "/api/offer"', "enableMic: true", "onTrackStarted", "MediaStream",
    ):
        assert token in body, f"app.js voice wiring missing {token!r}"
