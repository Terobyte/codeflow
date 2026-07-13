"""UI v2 слайс UI-1 + UI v3 редизайн: вендор-бандл, наша статика /client, SPA-shell,
роут-своп, mount-order (S24/S26). Лексические проверки — паттерн test_kora_status_ui.py
(никакого браузера в CI)."""
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


def test_index_is_spa_shell():
    """UI v3: один shell — сайдбар (проекты/треды/статус-карточка Коры), вью дома и треда,
    единый композер. kora-dot умер (Ж4: тултип не существует на тачскрине)."""
    body = (CLIENT_DIR / "index.html").read_text(encoding="utf-8")
    for token in (
        "shell", "sidebar", "menu-btn", "new-thread", "projects-list", "add-project",
        "threads-list", "kora-card", "kora-card-sub", "./logs",
        "view-home", "view-thread", "feed-list", "typing",
        "mic-btn", "msg-input", "msg-send", 'data-state="idle"', "bot-audio",
        "manifest.webmanifest", "apple-touch-icon", "reconnect.js", "app.js", "style.css",
        'lang="ru"', "viewport-fit=cover", "picker-dirs", "picker-choose",
    ):
        assert token in body, f"index.html missing {token!r}"
    assert "kora-dot" not in body
    assert "status-widget.js" not in body  # светофор нативный, не инжект-виджет
    assert "не подключено" not in body     # статус виден только когда есть что сказать


def test_app_js_is_spa_router_and_xss_safe():
    body = (CLIENT_DIR / "app.js").read_text(encoding="utf-8")
    for token in (
        "#/thread/", "hashchange",            # SPA-роутер: голос живёт при навигации (Ж6)
        "kora-status", "visibilitychange", "textContent",
        "/api/threads", "/api/projects", "active-thread", "/feed", "/message",
        "/api/browse", "encodeURIComponent(path)", "picker-choose", "🧠",
    ):
        assert token in body, f"app.js missing {token!r}"
    assert "innerHTML" not in body
    assert "window.open" not in body  # R3: standalone iOS PWA — навигация, не окна
    assert "prompt(" not in body      # абсолютный путь руками умер вместе с prompt()
    assert "location.href" not in body  # SPA: только hash-навигация, никаких перезагрузок


def test_app_js_wires_voice_with_visible_states():
    body = (CLIENT_DIR / "app.js").read_text(encoding="utf-8")
    for token in (
        './vendor/pipecat.mjs"', "PipecatClient", "SmallWebRTCTransport",
        'webrtcUrl: "/api/offer"', "enableMic: true", "onTrackStarted", "MediaStream",
        # Ж2-фиксы: гвард НЕ требует participant (transport зовёт onTrackStarted(track)
        # без него — иначе аудио бота молча выбрасывается) + видимые стейты и таймаут
        # вместо вечного «подключаюсь…» на зависшем getUserMedia.
        "!participant?.local", "dataset.state", "microphone", "withTimeout",
        "console.error",
    ):
        assert token in body, f"app.js voice wiring missing {token!r}"
    assert "participant && !participant.local" not in body


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
        assert "mic-btn" in body               # наш клиент (маркер композера)
        assert "status-widget.js" not in body  # инжекты слайса 5 умерли вместе с патчем


async def test_thread_url_redirects_into_spa_hash():
    """Старые ссылки /client/thread?id=X живут: 30x на /client/#/thread/X (id URL-квотится)."""
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    resp = await _endpoint(app, "client_thread")(id="abc123")
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/client/#/thread/abc123"
    resp = await _endpoint(app, "client_thread")(id="a/б?c")
    assert resp.headers["location"] == "/client/#/thread/a%2F%D0%B1%3Fc"


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
    assert "client_thread_js" not in names  # страница треда умерла вместе с thread.js


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
        "client_app_js", "client_style_css", "client_vendor_pipecat", "client_thread",
    ):
        assert idx[name] < mount_i, f"{name} must be registered BEFORE the /client/dev mount (S24)"


def test_client_files_served_from_disk_not_startup_ram():
    """UI v3: index/app/style читаются per-request — итерации дизайна без рестарта.
    Лексически: в build_web_app нет стартового read_bytes для них (vendored — можно)."""
    webrtc_server = _webrtc_server_or_skip()
    src = Path(webrtc_server.__file__).read_text(encoding="utf-8")
    for stale in ("_index_bytes", "_app_js_bytes", "_style_css_bytes",
                  "_thread_html_bytes", "_thread_js_bytes"):
        assert stale not in src, f"{stale}: клиент снова закеширован на старте"
    assert "_client_file(" in src


def test_thread_page_files_are_gone():
    assert not (CLIENT_DIR / "thread.html").exists()
    assert not (CLIENT_DIR / "thread.js").exists()


def test_browse_dir_is_caged_to_home(tmp_path):
    webrtc_server = _webrtc_server_or_skip()
    home = tmp_path / "home"
    (home / "Projects" / "app").mkdir(parents=True)
    (home / ".ssh").mkdir()
    (home / "notes.txt").write_text("x", encoding="utf-8")
    # корень клетки: parent отсутствует, скрытые директории и файлы не показываются
    root = webrtc_server._browse_dir(None, home)
    assert root["path"] == str(home.resolve()) and root["parent"] is None
    assert root["dirs"] == ["Projects"]
    # спуск и подъём
    inner = webrtc_server._browse_dir(str(home / "Projects"), home)
    assert inner["dirs"] == ["app"] and inner["parent"] == str(home.resolve())
    # побег из клетки (выше HOME, битый путь) молча приземляется на home
    for escape in ("/", str(tmp_path), str(home / ".." / ".."), "/etc"):
        assert webrtc_server._browse_dir(escape, home)["path"] == str(home.resolve())
