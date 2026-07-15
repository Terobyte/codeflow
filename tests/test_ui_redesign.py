"""UI v5 редизайн (слайс R1, спека 2026-07-15 §16) — Night Atlas / Hero Drive.

Наследник Organic-канона UI v4: §16.6 отменяет ровно этот файл и требует переписать его с
сохранением СМЫСЛА каждой проверки. Соответствие старое → новое:

  Caprasimo/Figtree  → системная пара var(--serif)/var(--sans) (веб-шрифтов нет вовсе)
  #c67139 / #f5ead8  → --cream/--lilac поверх --night-*; фон темы #090b24
  color-scheme:light → color-scheme:dark
  .bg-blob/logo-wave → .ambient (звёздное поле) / .brand-mark

Проверки поведения (композер, aria-live, innerHTML, мик без disabled) не тронуты — редизайн
их не отменяет. Всё, что запинено в test_ui_client.py / test_bugs_audit.py /
test_kora_status_ui.py / test_slice5_pwa.py, обязано проходить НЕМОДИФИЦИРОВАННЫМ.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

CLIENT_DIR = Path(__file__).parent.parent / "synapse" / "pipeline" / "client"
STATIC_DIR = Path(__file__).parent.parent / "synapse" / "pipeline" / "static"

NIGHT_BG = "#090b24"  # фон темы Night Atlas: meta[theme-color] == manifest == THEME_META.night


def read(name: str) -> str:
    return (CLIENT_DIR / name).read_text(encoding="utf-8")


def strip_css_comments(css: str) -> str:
    """CSS-комментарии — проза, а не правила. Грепать их наравне с кодом = ложные срабатывания:
    пояснение «100dvh, НЕ 100vh: ...» роняло проверку на литерале внутри собственного объяснения."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def css_rules(css: str) -> list[tuple[str, str]]:
    """[(селектор, тело)] — вложенности нет, поэтому внутренние правила @media матчатся сами,
    а обёртка @media селектором не становится ([^{}] не пускает вложенную скобку)."""
    return [(sel.strip(), body) for sel, body in
            re.findall(r"([^{}]+)\{([^{}]*)\}", strip_css_comments(css))]


def reduced_motion_block(css: str) -> str:
    start = css.find("@media (prefers-reduced-motion: reduce)")
    assert start != -1, "style.css lost the prefers-reduced-motion guard entirely"
    depth, started = 0, False
    for i, ch in enumerate(css[start:], start=start):
        if ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
            if started and depth == 0:
                return css[start:i + 1]
    raise AssertionError("unbalanced prefers-reduced-motion block")


# ---------- тема: токены существуют ----------

def test_style_css_carries_night_atlas_design_tokens():
    css = read("style.css")
    for token in (
        # палитра Night Atlas (была: #c67139 / #f5ead8 Organic)
        "--night-950:#070817", "--cream:#f7e8bd", "--lilac:#a994ff", "--text:#f7f2e6",
        # шрифтовая пара: системная, БЕЗ веб-шрифтов (были: Caprasimo / Figtree)
        "--serif:", "Georgia", "--sans:", "ui-rounded",
        # тёмная тема (был: color-scheme: light)
        "color-scheme: dark",
        # механизмы, пережившие редизайн: стейты мика, фокус, guard анимаций
        "prefers-reduced-motion", ":focus-visible", 'data-state="on"',
        'data-state="connecting"', 'data-state="error"', 'data-state="idle"',
    ):
        assert token in css, f"style.css missing Night Atlas token {token!r}"
    # Organic-канон отменён целиком — ни один его токен не вернулся вместе с мержем
    for dead in ("Caprasimo", "Figtree", "#c67139", "#f5ead8", "color-scheme: light",
                 ".bg-blob", "logo-wave"):
        assert dead not in css, f"style.css still carries dead Organic token {dead!r}"


def test_style_css_has_no_webfont_dependency():
    """Обе темы стоят на системной паре: первый paint PWA не ждёт fonts.googleapis.com.
    Как и в проверке 100vh: комментарии режем — оба файла ПОЯСНЯЮТ этот запрет прозой,
    и грепать её наравне с правилами значит ловить собственное объяснение."""
    assert "fonts.googleapis.com" not in strip_css_comments(read("style.css"))
    assert "@font-face" not in strip_css_comments(read("style.css"))
    html = re.sub(r"<!--.*?-->", "", read("index.html"), flags=re.DOTALL)
    assert "fonts.googleapis.com" not in html
    assert "@font-face" not in html


def test_hero_drive_theme_block_exists_and_overrides_only_palette():
    """§16.2: .hero-mode — вторая тема, а не второй экран. Обе темы делят ОДНУ геометрию,
    поэтому геометрические токены живут только в :root и тема их не переопределяет."""
    css = read("style.css")
    assert ".hero-mode" in css
    hero_bodies = [body for sel, body in css_rules(css) if ".hero-mode" in sel]
    assert hero_bodies, ".hero-mode block is gone — Hero Drive theme has no overrides"
    root_block = next(body for sel, body in css_rules(css) if sel == ":root")
    for geometry in ("--sidebar-width", "--shell-gap"):
        assert geometry in root_block, f":root lost geometry token {geometry!r}"
        for body in hero_bodies:
            assert geometry not in body, (
                f"§16.2: .hero-mode redefines geometry token {geometry!r} — темы разъехались "
                "разметкой, свич больше не «только палитра»"
            )
    # тема обязана перекрасить палитру — иначе это пустой класс
    palette = "".join(hero_bodies)
    for token in ("--night-950:", "--cream:", "--text:"):
        assert token in palette, f".hero-mode does not repaint {token!r}"


# ---------- анимации ----------

def test_reduced_motion_covers_every_animation():
    """Было: список целей (.bg-blob/#live-orb/#live-wave/.msg). Стало: catch-all — ни одна новая
    анимация темы не может проскочить мимо гварда, потому что цель у него универсальная."""
    css = strip_css_comments(read("style.css"))
    block = reduced_motion_block(css)
    body = block[block.index("{") + 1:]
    assert re.search(r"\*\s*,\s*\*:before\s*,\s*\*:after\s*\{", body), (
        "prefers-reduced-motion guard is no longer a catch-all — новые анимации утекут мимо него"
    )
    assert "animation:none!important" in body.replace(" ", "")
    assert "transition-duration:.01ms!important" in body.replace(" ", "")
    # анимации, которые гвард обязан гасить, в теме реально есть (иначе тест — декорация)
    for keyframes in ("twinkle", "assetFloat", "float", "pulseCall", "wave", "micPulse", "blink"):
        assert "@keyframes " + keyframes in css, f"style.css lost animation {keyframes!r}"


def test_style_css_forbids_100vh_height():
    """iOS Safari режет вьюпорт адресной строкой — высота только 100dvh.
    Грепаем ПРАВИЛА, не прозу: комментарий, объясняющий запрет, содержит сам литерал."""
    css = strip_css_comments(read("style.css"))
    assert "100vh" not in css, "100vh вернулся в правила: композер уедет под адресную строку iOS"
    assert "100dvh" in css


# ---------- структура ----------

def test_index_has_night_atlas_structural_ids():
    body = read("index.html")
    for token in (
        # пережили редизайн (адресуются app.js по id)
        "disp-toggle", "tab-chat", "tab-diff", "view-diff",
        "live-overlay", "live-status", "live-mute", "live-end",
        # новое в R1
        "theme-toggle", "theme-toggle-icon", "theme-toggle-label", "brand-theme",
        "hero-new", "home-title", "thread-title", "thread-folder", "thread-ago",
        "stage-rail", "diff-wrap", "sky-status", "sky-title", "sky-detail",
        "live-mute-label", "side-scroll", "kora-card-dot",
        # оболочка темы (были: .bg-blob / logo-wave)
        "ambient", "brand-mark",
    ):
        assert token in body, f"index.html missing {token!r}"
    assert f'content="{NIGHT_BG}"' in body   # theme-color = фон Night Atlas, тёмный старт без вспышки
    assert 'content="default"' in body       # status-bar-style: не black-translucent
    assert "black-translucent" not in body
    # SVG-иллюстрации темы приезжают роутом /client/assets (§16.5), не mount-ом
    assert "./assets/code-night-atlas.svg" in body
    assert "./assets/codeflow-hero-drive.svg" in body


def test_theme_assets_exist_on_disk():
    for name in ("code-night-atlas.svg", "codeflow-hero-drive.svg"):
        path = CLIENT_DIR / "assets" / name
        assert path.exists(), f"{name} не портирован из прототипа — <img> отдаст битую картинку"
        assert path.read_text(encoding="utf-8").lstrip().startswith("<?xml") or \
            "<svg" in path.read_text(encoding="utf-8")[:200]


def test_index_composer_has_mic_left_of_input_send_right():
    body = read("index.html")
    mic_i = body.index('id="mic-btn"')
    input_i = body.index('id="msg-input"')
    send_i = body.index('id="msg-send"')
    assert mic_i < input_i < send_i, "PF3: composer canon is mic-left / input / send-right"


def test_index_live_overlay_has_aria_live_status():
    body = read("index.html")
    live_status_tag = body[body.index('id="live-status"') - 60 : body.index('id="live-status"') + 40]
    assert 'aria-live="polite"' in live_status_tag


# ---------- app.js ----------

def test_app_js_has_disp_and_kora_roles_and_no_innerhtml():
    js = read("app.js")
    for token in (
        "synapse-disp-on", "msg-disp", "msg-kora", "dispOn",
        "liveRequested", "liveMuteFn", "disconnectVoice",
        "onBotStartedSpeaking", "onBotStoppedSpeaking", "svgEl",
        "avatar-kora",  # R1: аватар Коры отличается заливкой от аватара Диспетчера
    ):
        assert token in js, f"app.js missing {token!r}"
    assert "innerHTML" not in js
    assert "insertAdjacentHTML" not in js
    assert "document.write" not in js


def test_app_js_mic_never_gets_disabled_attribute_for_disp_toggle():
    js = read("app.js")
    # R2: dispOn=false must never set mic-btn.disabled — only a visual dim class.
    assert '$("mic-btn").disabled' not in js
    assert "disp-off" in js


def test_theme_switch_reads_and_writes_one_state():
    """§16.2: свичей два (топбар сейчас, настройки в R2), состояние одно. Ключ localStorage
    пишется РОВНО из одного места (applyTheme) и читается ровно на инициализации — второй
    источник правды и есть тот баг, из-за которого свичи разъезжаются."""
    js = read("app.js")
    assert js.count('localStorage.setItem("codeflow-theme"') == 1, (
        "codeflow-theme пишется не только из applyTheme — у темы завёлся второй писатель"
    )
    assert js.count('localStorage.getItem("codeflow-theme")') == 1
    assert "function applyTheme" in js and "function currentTheme" in js
    # оба состояния темы читаются из ОДНОГО места — класса на body, не из копии в переменной
    assert 'classList.contains("hero-mode")' in js
    assert 'classList.toggle("hero-mode"' in js
    # топбар-свич не красит сам — зовёт applyTheme
    toggle = js[js.index('$("theme-toggle").addEventListener'):]
    assert "applyTheme(" in toggle[:200]


def test_theme_applied_before_first_render():
    """Тема ставится ДО первого рендера и БЕЗ повторной записи (persist=false): чтение
    сохранённого выбора — не новый выбор, а Hero Drive не должен моргать ночью на старте."""
    js = read("app.js")
    init_i = js.index('applyTheme(localStorage.getItem("codeflow-theme")')
    assert "false)" in js[init_i:init_i + 120], "инициализация темы persist-ит обратно то, что прочла"
    assert init_i < js.index("loadLists().then(render)"), "тема применяется после первого рендера"


def test_stage_rail_is_display_only():
    """§16.7 R1: рельс показывает серверный FSM. Клик по стадии не делает ничего — стадию
    двигают только гейт-карточки; слушателя на стадии нет и быть не должно."""
    js = read("app.js")
    rail = js[js.index("function renderStageRail"):]
    rail = rail[:rail.index("\n}\n") + 3]
    for token in ('"stage"', "done", "active", "rail-line", "active-line", "STAGE_ORDER"):
        assert token in rail, f"renderStageRail missing {token!r}"
    assert "addEventListener" not in rail, "рельс кликабелен — стадия принадлежит серверу, не UI"
    assert 'const STAGE_ORDER = ["collect", "propose", "spec_plan", "code", "done"]' in js


def test_thread_header_survives_rename_editor():
    """B45: узел заголовка ЗАКОННО снят из DOM inline-редактором rename — писатель обязан
    пережить null, а не рушить render()/loadLists() на каждом тике."""
    js = read("app.js")
    body = js[js.index("function setThreadHeader"):]
    body = body[:body.index("\n}\n") + 3]
    for node in ("thread-title", "thread-folder", "thread-ago"):
        assert node in body, f"setThreadHeader does not write {node!r}"
    assert body.count("if (") >= 3, "setThreadHeader пишет в узел без null-гварда (B45)"
    # rename работает с обоих заголовков и через один путь
    assert '$("view-title").addEventListener' in js
    assert '$("thread-title").addEventListener' in js
    assert "relTime(" in body, "время шапки должно идти через общий форматтер relTime"


def test_live_mute_label_has_its_own_node():
    """§16.5: textContent по самой кнопке стёр бы svg-иконку внутри неё."""
    js = read("app.js")
    assert '$("live-mute-label").textContent' in js
    assert '$("live-mute").textContent' not in js
    # состояние остаётся на самой кнопке
    assert '$("live-mute").setAttribute("aria-pressed"' in js
    assert '$("live-mute").classList.toggle("muted"' in js


# ---------- §16.5 правило 3: ноль изменений сетевых контрактов ----------
# Центральный инвариант редизайна: «ни один fetch не появляется, не исчезает и не меняет форму».
# Пинится ЛЕКСИЧЕСКИ — весь сетевой ввод-вывод клиента идёт через пять хелперов (+ SDK), и
# каждый их вызов начинается со стабильного литерала-префикса.

NET_HELPERS = ("getJSON", "postJSON", "patchJSON", "deleteJSON", "postBlob")

# Ожидаемое множество выписано ЯВНО: новый эндпоинт → красный, пропавший → тоже красный.
EXPECTED_ENDPOINTS = {
    "/api/threads",       # GET список + POST создание треда
    "/api/threads/",      # префикс: /diff, /feed, /gate, /message, /archive, PATCH rename
    "/api/projects",      # GET список + POST добавление проекта
    "/api/projects/",     # префикс: DELETE проекта
    "/api/active-thread", # POST привязки голоса
    "/api/tts",           # POST Play-озвучки (Blob)
    "./kora-status",      # GET светофора Коры (карточка сайдбара + sky-сцена)
    "./kora-log",         # GET журнала активности
    "./session-alive",    # GET правды вотчдога (+ voice_thread для hang-up)
}


def network_call_sites(js: str) -> tuple[set[str], list[tuple[str, str]]]:
    """(эндпоинты, нерезолвимые вызовы).

    Формы аргумента: строковый литерал ("/api/tts", а также голова конкатенации
    "/api/threads/" + id + "/diff"), template-literal с интерполяцией (`/api/threads/${id}/gate`
    → берём стабильный префикс до ${) и переменная (getJSON(url)) — последняя лексически не
    резолвится и ЧЕСТНО уезжает в unresolved, а не молча теряется."""
    sites: set[str] = set()
    unresolved: list[tuple[str, str]] = []
    for m in re.finditer(r"\b(" + "|".join(NET_HELPERS) + r")\(", js):
        if re.search(r"function\s+$", js[:m.start()]):
            continue  # это ОПРЕДЕЛЕНИЕ хелпера (async function getJSON(url)), не вызов
        rest = js[m.end():]
        if rest.startswith('"'):
            sites.add(rest[1:rest.index('"', 1)])
        elif rest.startswith("`"):
            tpl = rest[1:]
            stop = [i for i in (tpl.find("${"), tpl.find("`")) if i != -1]
            sites.add(tpl[:min(stop)])
        else:
            unresolved.append((m.group(1), rest[:24].split(")")[0]))
    return sites, unresolved


def test_app_js_network_contract_is_unchanged():
    """§16.5 правило 3 — единственный тест, который реально пинит сетевой контракт клиента.
    Редизайн, требующий нового роута, — не редизайн; пропавший роут — тихая регрессия."""
    js = read("app.js")
    endpoints, unresolved = network_call_sites(js)
    assert endpoints == EXPECTED_ENDPOINTS, (
        "§16.5 правило 3: сетевой контракт поехал.\n"
        f"  появились: {sorted(endpoints - EXPECTED_ENDPOINTS)}\n"
        f"  исчезли:   {sorted(EXPECTED_ENDPOINTS - endpoints)}"
    )
    # Честность экстрактора: РОВНО один вызов передаёт переменную вместо литерала — browse.
    # Станет два → красный, и человек посмотрит, что за эндпоинт спрятался за переменной.
    assert len(unresolved) == 1 and unresolved[0] == ("getJSON", "url"), (
        f"новый вызов с нелитеральным URL — эндпоинт спрятан от проверки: {unresolved}"
    )
    browse = js[js.index("async function browse"):]
    browse = browse[:browse.index("\n}\n")]
    assert '"/api/browse"' in browse, "эндпоинт browse больше не литерал — пинить нечего"
    assert "getJSON(url)" in browse
    # Мимо пяти хелперов в сеть ходит ровно один путь — SDK голоса (POST /api/offer).
    assert 'webrtcUrl: "/api/offer"' in js
    # Ни один fetch не ходит по литеральному URL напрямую: пять хелперов принимают url
    # параметром, и это единственная дверь наружу.
    assert not re.search(r'\bfetch\("', js), "новый fetch по литеральному URL мимо хелперов"
    assert len(re.findall(r"\bfetch\(", js)) == len(NET_HELPERS), (
        "число сырых fetch разошлось с числом хелперов — появился новый сетевой путь"
    )


def _fn_body(js: str, signature: str) -> str:
    body = js[js.index(signature):]
    return body[:body.index("\n}\n")]


def test_activity_page_reuses_existing_endpoints_only():
    """§16.5: редизайн не заводит сетевых контрактов. Журнал и sky-сцена питаются теми же
    ./kora-log и ./kora-status, что были до него."""
    js = read("app.js")
    assert "journal-entry" in js and "journal-icon" in js and "ji-" in js
    for node in ("sky-status", "sky-title", "sky-detail"):
        assert node in js, f"app.js does not feed {node!r}"
    # sky-сцена сидит внутри pollStatus-данных, а не за своим fetch-ом
    assert "function setSky" in js
    assert network_call_sites(_fn_body(js, "async function pollActivity"))[0] == {"./kora-log"}
    assert network_call_sites(_fn_body(js, "async function pollStatus"))[0] == {"./kora-status"}
    assert network_call_sites(_fn_body(js, "function setSky"))[0] == set(), (
        "sky-сцена завела свой fetch вместо данных pollStatus"
    )
    assert "/client/sky" not in js and "/api/sky" not in js
    assert js.count("kora-log") == 1 and "kora-status" in js


# ---------- манифест ----------

def test_manifest_theme_matches_night_atlas_bg():
    data = json.loads((STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert data["theme_color"] == NIGHT_BG
    assert data["background_color"] == NIGHT_BG
    # pinned fields untouched by the redesign
    assert data["name"] == "CodeFlow"
    assert data["start_url"] == "/client/"
    # §16.6: PWA-иконки остаются старыми до отдельного шага — расхождение зафиксировано осознанно
    assert data["icons"], "иконки не входят в порт, но и исчезнуть не должны"


def test_manifest_theme_color_agrees_with_client():
    """Три места держат один цвет: manifest, meta[theme-color] и THEME_META в app.js.
    Расхождение = видимый шов системной обвязки standalone-PWA."""
    data = json.loads((STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert f'content="{data["theme_color"]}"' in read("index.html")
    assert f'night: "{data["theme_color"]}"' in read("app.js")


# ---------- ассеты отдаются роутом, а не молча лежат в директории ----------

def _webrtc_server_or_skip():
    pytest.importorskip("aiortc"); pytest.importorskip("cv2"); pytest.importorskip("fastapi")
    try:
        from synapse.pipeline import webrtc_server
        return webrtc_server
    except (ImportError, RuntimeError) as e:
        pytest.skip(f"webrtc deps unavailable: {e}")


def _endpoint(app, name):
    return next(r.endpoint for r in app.routes
                if getattr(getattr(r, "endpoint", None), "__name__", "") == name)


async def test_theme_assets_are_served_by_whitelisted_route():
    """§16.5: /client отдаётся точными роутами. Положить SVG в директорию недостаточно —
    он не отдастся; а имя из URL никогда не склеивается в путь (белый список решает первым)."""
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    asset = _endpoint(app, "client_asset")
    for name in ("code-night-atlas.svg", "codeflow-hero-drive.svg"):
        resp = await asset(name)
        assert resp.status_code == 200
        assert resp.media_type == "image/svg+xml"
        assert b"<svg" in resp.body
    # обход каталога и просто чужое имя — 404 без единого касания диска
    for escape in ("../../.env", "../app.js", "/etc/passwd", "nope.svg", "", "."):
        resp = await asset(escape)
        assert resp.status_code == 404, f"asset route served {escape!r}"


async def test_asset_route_registered_before_dev_mount():
    """S24: exact-роуты — ДО mount /client/dev, иначе StaticFiles съедает префикс."""
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    routes = app.router.routes
    mount_i = next(i for i, r in enumerate(routes) if r.__class__.__name__ == "Mount")
    asset_i = next(i for i, r in enumerate(routes)
                   if getattr(getattr(r, "endpoint", None), "__name__", "") == "client_asset")
    assert asset_i < mount_i
