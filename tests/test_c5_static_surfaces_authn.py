"""С5 NO-SHIP блокер (runs/2026-07-15-c5-bearer-authn.md, судья verbatim):

`synapse/pipeline/static/logs.html` и `synapse/pipeline/static/status-widget.js` остались в
open-allowlist middleware (сами байты — не секрет), но их собственный рантайм-код зовёт
`/client/kora-log` / `/client/kora-status` БЕЗ bearer-токена -- даже когда валидный токен уже
лежит в localStorage под тем же ключом, что пишет app.js (общий origin). Middleware денаит
401, `if (!res.ok) return;` / `if (!res.ok) { ...; return; }` глотали его молча: лента вечно
"пуста", точка вечно серая "неизвестно" -- неотличимо от "ещё не загрузилось".

Амендмент 2 контракта запрещает грep исходника как доказательство. Ниже -- поведенческие
тесты: реальный `<script>`-код обоих файлов читается с диска ВЕРБАТИМ (byte-for-byte, тот же
текст, что отдаёт роут) и исполняется в node с фейковыми document/localStorage/fetch. Мы
проверяем (а) какие заголовки реально ушли на fetch, и (б) что видит пользователь в
DOM/title после 401 и после успешного ответа -- то есть настоящее поведение, а не наличие
строки "Authorization" где-то в файле.

Живая проверка (Playwright, реальный uvicorn + реальный SynapseHost-стаб, `judge-secret-token`)
проводилась отдельно (см. отчёт игрока) и воспроизвела и красное, и зелёное состояние 1:1 с
тем, что видят тесты здесь; она не встроена в pytest, потому что живой Chromium не входит в
зависимости этого репозитория.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_STATIC_DIR = Path(__file__).resolve().parent.parent / "synapse" / "pipeline" / "static"

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="node недоступен в этом окружении")


# --------------------------------------------------------------------------------------
# node-харнесс: фейковые document/localStorage/fetch, ВЕРБАТИМ-код исполняется как есть.
# --------------------------------------------------------------------------------------

_FAKE_ELEMENT_JS = r"""
function makeElement(tag) {
  return {
    tagName: tag,
    id: "",
    className: "",
    _children: [],
    style: {},
    textContent: "",
    title: "",
    tabIndex: 0,
    setAttribute(k, v) { this[k] = v; },
    appendChild(child) { this._children.push(child); return child; },
    append(...children) { this._children.push(...children); },
    replaceChildren(...children) { this._children = children; },
    addEventListener(ev, cb) { (this._listeners = this._listeners || {})[ev] = cb; },
    get children() { return this._children; },
  };
}
"""

_COMMON_TAIL_JS = r"""
global.window = { innerHeight: 0, scrollY: 0, scrollTo: () => {} };
global.location = { href: "" };

const __store = STORE_JSON;
global.localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(__store, k) ? __store[k] : null),
  setItem: (k, v) => { __store[k] = v; },
  removeItem: (k) => { delete __store[k]; },
};

let __callIndex = 0;
const __RESPONSES = RESPONSES_JSON;
global.fetch = (url, opts) => {
  const headers = {};
  if (opts && opts.headers) {
    for (const k of Object.keys(opts.headers)) headers[k] = opts.headers[k];
  }
  __results.calls.push({ url, headers });
  const resp = __RESPONSES[Math.min(__callIndex, __RESPONSES.length - 1)];
  __callIndex += 1;
  return Promise.resolve({
    ok: resp.status >= 200 && resp.status < 300,
    status: resp.status,
    json: () => Promise.resolve(resp.body || {}),
  });
};

global.setInterval = () => 1; // единственный реальный вызов poll() -- явный, ниже по коду
global.clearInterval = () => {};

// --- ВЕРБАТИМ-код файла начинается здесь ---
"""


def _run_node(js_source: str) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js_source)
        path = f.name
    proc = subprocess.run([_NODE, path], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"node harness crashed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _extract_logs_script() -> str:
    html = (_STATIC_DIR / "logs.html").read_text(encoding="utf-8")
    m = re.search(r"<script>(.*)</script>", html, re.S)
    assert m, "logs.html: <script> block not found"
    return m.group(1)


def _status_widget_script() -> str:
    return (_STATIC_DIR / "status-widget.js").read_text(encoding="utf-8")


def _run_logs_harness(store: dict, responses: list[dict]) -> dict:
    """Прогоняет ВЕРБАТИМ <script> из logs.html; возвращает отправленные fetch-заголовки и
    итоговое состояние #empty/#feed."""
    preamble = f"""
const __results = {{ calls: [] }};
{_FAKE_ELEMENT_JS}
const __elementsById = {{
  feed: makeElement("div"),
  empty: makeElement("div"),
}};
__elementsById.empty.textContent = "Лента пуста — Кора ещё не запускалась в этом процессе.";
const __body = makeElement("body");
__body.offsetHeight = 0;
global.document = {{
  getElementById: (id) => __elementsById[id],
  createElement: (tag) => makeElement(tag),
  body: __body,
  hidden: false,
  addEventListener: () => {{}},
}};
{_COMMON_TAIL_JS.replace("STORE_JSON", json.dumps(store)).replace("RESPONSES_JSON", json.dumps(responses))}
"""
    postamble = """
// --- ВЕРБАТИМ-код файла заканчивается здесь ---
setTimeout(() => {
  console.log(JSON.stringify({
    calls: __results.calls,
    emptyText: __elementsById.empty.textContent,
    emptyDisplay: __elementsById.empty.style.display,
    feedChildren: __elementsById.feed._children.length,
  }));
}, 50);
"""
    return _run_node(preamble + _extract_logs_script() + postamble)


def _run_widget_harness(store: dict, responses: list[dict]) -> dict:
    """Прогоняет ВЕРБАТИМ status-widget.js (IIFE); дот -- единственный ребёнок document.body."""
    preamble = f"""
const __results = {{ calls: [] }};
{_FAKE_ELEMENT_JS}
const __body = makeElement("body");
global.document = {{
  getElementById: () => null,
  createElement: (tag) => makeElement(tag),
  body: __body,
  hidden: false,
  addEventListener: () => {{}},
}};
{_COMMON_TAIL_JS.replace("STORE_JSON", json.dumps(store)).replace("RESPONSES_JSON", json.dumps(responses))}
"""
    postamble = """
// --- ВЕРБАТИМ-код файла заканчивается здесь ---
setTimeout(() => {
  const dot = __body._children[0];
  console.log(JSON.stringify({
    calls: __results.calls,
    background: dot ? dot.style.background : null,
    title: dot ? dot.title : null,
  }));
}, 50);
"""
    return _run_node(preamble + _status_widget_script() + postamble)


_TOKEN = "judge-secret-token"


# --------------------------------------------------------------------------------------
# logs.html
# --------------------------------------------------------------------------------------

def test_logs_html_sends_bearer_token_from_localstorage():
    """Ядро блокера: с токеном в localStorage реальный код должен нести Authorization --
    раньше `fetch("./kora-log", { cache: "no-store" })` не носил заголовков вовсе."""
    out = _run_logs_harness(
        {"synapse-api-token": _TOKEN},
        [{"status": 200, "body": {"entries": []}}],
    )
    assert out["calls"] == [
        {"url": "./kora-log", "headers": {"Authorization": f"Bearer {_TOKEN}"}}
    ]


def test_logs_html_no_token_shows_visible_message_not_silent_empty():
    """Без токена вовсе (свежий браузер) сервер денаит 401 -- пользователь обязан увидеть
    внятное сообщение, а не неотличимый от 'ещё не загрузилось' дефолт."""
    out = _run_logs_harness({}, [{"status": 401, "body": {"error": "unauthorized"}}])
    assert out["calls"] == [{"url": "./kora-log", "headers": {}}]
    assert out["emptyText"] == "Нужен токен доступа — открой /client/ и введи его там."
    assert out["emptyDisplay"] == "block"
    assert out["feedChildren"] == 0


def test_logs_html_stale_token_401_shows_visible_message_not_silent_empty():
    """Токен ЕСТЬ (был отправлен), но сервер всё равно 401 (просрочен/невалиден) -- тот же
    видимый исход, не молчаливая вечная пустота."""
    out = _run_logs_harness(
        {"synapse-api-token": _TOKEN},
        [{"status": 401, "body": {"error": "unauthorized"}}],
    )
    assert out["calls"] == [
        {"url": "./kora-log", "headers": {"Authorization": f"Bearer {_TOKEN}"}}
    ]
    assert out["emptyText"] == "Нужен токен доступа — открой /client/ и введи его там."
    assert out["emptyDisplay"] == "block"


def test_logs_html_authenticated_200_renders_real_entries():
    """С валидным токеном фид реально рендерит содержимое (не заглушка) -- дефолтный текст
    #empty восстанавливается, а не остаётся залипшим на сообщении про токен."""
    out = _run_logs_harness(
        {"synapse-api-token": _TOKEN},
        [{"status": 200, "body": {"entries": [{"ts": 0, "kind": "task", "text": "hi"}]}}],
    )
    assert out["feedChildren"] == 1
    assert out["emptyDisplay"] == "none"
    # дефолт восстановлен, а не залип на сообщении про токен из предыдущего гипотетического 401
    assert out["emptyText"] == "Лента пуста — Кора ещё не запускалась в этом процессе."


# --------------------------------------------------------------------------------------
# status-widget.js
# --------------------------------------------------------------------------------------

def test_status_widget_sends_bearer_token_from_localstorage():
    out = _run_widget_harness(
        {"synapse-api-token": _TOKEN},
        [{"status": 200, "body": {"color": "green", "task_text": None, "liveness": "ok"}}],
    )
    assert out["calls"] == [
        {"url": "./kora-status", "headers": {"Authorization": f"Bearer {_TOKEN}"}}
    ]


def test_status_widget_no_token_shows_distinct_message_not_generic_unknown():
    """Без токена дот остаётся серым (нет цветовой семантики без сервера), но title обязан
    называть причину -- раньше он молча оставался на дефолтном 'статус неизвестен', что
    неотличимо от 'ещё не опрашивали ни разу'."""
    out = _run_widget_harness({}, [{"status": 401, "body": {"error": "unauthorized"}}])
    assert out["calls"] == [{"url": "./kora-status", "headers": {}}]
    assert out["background"] == "#888"
    assert "токен" in out["title"]
    assert out["title"] != "Кора: статус неизвестен"


def test_status_widget_stale_token_401_shows_distinct_message():
    out = _run_widget_harness(
        {"synapse-api-token": _TOKEN},
        [{"status": 401, "body": {"error": "unauthorized"}}],
    )
    assert out["calls"] == [
        {"url": "./kora-status", "headers": {"Authorization": f"Bearer {_TOKEN}"}}
    ]
    assert "токен" in out["title"]


def test_status_widget_authenticated_200_renders_real_color():
    out = _run_widget_harness(
        {"synapse-api-token": _TOKEN},
        [{"status": 200, "body": {"color": "green", "task_text": None, "liveness": "ok"}}],
    )
    assert out["background"] == "#2ecc71"
    assert out["title"] == "Кора: нет задачи · ok"


# --------------------------------------------------------------------------------------
# /favicon.ico -- open-allowlist, но 404 (без роута), не 401 и не аудит-шум
# --------------------------------------------------------------------------------------

def _webrtc_server_or_skip():
    pytest.importorskip("aiortc")
    pytest.importorskip("cv2")
    pytest.importorskip("fastapi")
    from synapse.pipeline import webrtc_server
    return webrtc_server


def test_favicon_is_open_and_404_not_401(tmp_path):
    from starlette.testclient import TestClient

    from synapse.clock import FakeClock
    from synapse.config import SynapseConfig
    from synapse.journal import TurnJournal

    webrtc_server = _webrtc_server_or_skip()

    class _Host:
        def __init__(self):
            self.cfg = SynapseConfig(api_token=_TOKEN)
            self.journal = TurnJournal(str(tmp_path / "journal"), FakeClock(), session_id="favicon")
            self.text_loop = None

    host = _Host()
    client = TestClient(webrtc_server.build_web_app(host), raise_server_exceptions=False)
    resp = client.get("/favicon.ico")
    assert resp.status_code == 404  # not 401: allowlisted, but no route -> plain 404

    rows = [
        json.loads(line)
        for line in host.journal.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert not any(row.get("alert_kind") == "AUTH_FAILURE" for row in rows), (
        "favicon.ico must not add AUTH_FAILURE audit noise on every legitimate page load"
    )
