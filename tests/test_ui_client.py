"""UI v2 слайс UI-1: вендор-бандл, наша статика /client, роут-своп, mount-order (S24/S26).
Лексические проверки — паттерн test_kora_status_ui.py (никакого браузера в CI)."""
import asyncio
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


def _webrtc_server_or_skip():
    pytest.importorskip("aiortc"); pytest.importorskip("cv2"); pytest.importorskip("fastapi")
    try:
        from synapse.pipeline import webrtc_server
        return webrtc_server
    except (ImportError, RuntimeError) as e:
        pytest.skip(f"webrtc deps unavailable: {e}")


def _endpoint(app, name):
    return next(r.endpoint for r in app.routes if getattr(getattr(r, "endpoint", None), "__name__", "") == name)


async def _body(app, name):
    resp = await _endpoint(app, name)()
    return resp.body.decode("utf-8")


async def test_client_root_serves_our_index_not_prebuilt():
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    for name in ("client_index", "client_index_html"):
        body = await _body(app, name)
        assert "Открыть агента" in body      # наш клиент
        assert "status-widget.js" not in body  # инжекты слайса 5 умерли вместе с патчем


def test_prebuilt_mounted_unpatched_at_client_dev():
    webrtc_server = _webrtc_server_or_skip()
    from starlette.routing import Mount
    app = webrtc_server.build_web_app(host=object())
    mounts = [r for r in app.routes if isinstance(r, Mount)]
    assert [m.path for m in mounts] == ["/client/dev"]
    # тот же объект статики, что раньше жил на /client — значит dist отдается КАК ЕСТЬ
    assert mounts[0].app is webrtc_server.PipecatPrebuiltUI


def test_our_static_routes_exist():
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    names = {getattr(getattr(r, "endpoint", None), "__name__", "") for r in app.routes}
    for n in ("client_app_js", "client_style_css", "client_vendor_pipecat"):
        assert n in names


def test_all_exact_client_routes_registered_before_dev_mount():
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    routes = app.router.routes
    mount_i = next(i for i, r in enumerate(routes) if r.__class__.__name__ == "Mount")
    idx = {getattr(getattr(r, "endpoint", None), "__name__", None): i for i, r in enumerate(routes)}
    for name in (
        "client_index", "client_index_html", "client_manifest", "client_reconnect_js",
        "client_icon_192", "client_icon_512", "client_apple_touch_icon", "session_alive",
        "kora_status", "kora_log_feed", "client_logs", "client_status_widget_js",
        "client_app_js", "client_style_css", "client_vendor_pipecat",
        "client_thread", "client_thread_js",
    ):
        assert idx[name] < mount_i, f"{name} must be registered BEFORE the /client/dev mount (S24)"


def test_thread_page_wires_feed_and_message_and_is_xss_safe():
    body = (CLIENT_DIR / "thread.html").read_text(encoding="utf-8")
    for token in ("thread.js", "style.css", "feed-list", "msg-input", "msg-send", "← назад"):
        assert token in body
    js = (CLIENT_DIR / "thread.js").read_text(encoding="utf-8")
    for token in ("/feed", "/message", "active-thread", "textContent", "visibilitychange",
                  "application/json", "🧠"):
        assert token in js
    assert "innerHTML" not in js and "innerHTML" not in body


def test_home_lists_threads_and_projects():
    js = (CLIENT_DIR / "app.js").read_text(encoding="utf-8")
    for token in ("/api/threads", "/api/projects", "threads-list", "projects-list", "./thread?id="):
        assert token in js
