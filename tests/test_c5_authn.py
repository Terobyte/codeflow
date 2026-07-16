"""С5 — bearer authn для control plane (runs/2026-07-15-c5-bearer-authn.md).

Acceptance checks ровно по контракту: без заголовка защищённые роуты 401, с валидным
токеном — не 401, bootstrap-статика открыта без токена, отказ пишет AUTH_FAILURE в
журнал, `api_token=None` не пускает буквальный `"Bearer None"`, сравнение идёт через
`hmac.compare_digest` (лексический якорь), и `run()`-хелпер fail-closed без токена.

Хост — минимальный стаб control-plane (реальные `SynapseConfig`/`TurnJournal`/`ThreadStore`,
без Kora/пайплайна), паттерн `_TTSHost`/`_gate_host` из соседних route-тестов.
"""
from __future__ import annotations

import inspect
import json

import pytest

pytest.importorskip("aiortc")
pytest.importorskip("cv2")
pytest.importorskip("fastapi")

from starlette.testclient import TestClient

from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal
from synapse.pipeline import webrtc_server
from synapse.pipeline.app import _require_api_token
from synapse.pipeline.webrtc_server import build_web_app
from synapse.threads import ThreadStore

_CSRF = {"content-type": "application/json", "origin": "http://testserver"}


class _Host:
    """Минимальный control-plane хост: cfg (токен настраивается по тесту) + реальные
    journal/threads. text_loop=None -- POST /api/threads/{id}/message с токеном честно
    отвечает 503 (текстовые ходы выключены), это ЗДЕСЬ не баг: нас интересует только
    401-vs-не-401 на границе authn, а не поведение самого роута."""

    def __init__(self, tmp_path, api_token):
        self.cfg = SynapseConfig(api_token=api_token)
        self.journal = TurnJournal(str(tmp_path / "journal"), FakeClock(), session_id="c5")
        self.threads = ThreadStore(FakeClock(), str(tmp_path / "threads"))
        self.text_loop = None


def _client(host):
    return TestClient(build_web_app(host), raise_server_exceptions=False)


# --------------------------- deny без токена ---------------------------

def test_browse_without_token_is_401(tmp_path):
    host = _Host(tmp_path, api_token="secret-token")
    resp = _client(host).get("/api/browse")
    assert resp.status_code == 401


def test_thread_message_without_token_is_401(tmp_path):
    host = _Host(tmp_path, api_token="secret-token")
    th = host.threads.create("t")
    resp = _client(host).post(f"/api/threads/{th.id}/message", json={"text": "hi"})
    assert resp.status_code == 401


# --------------------------- allow с валидным токеном ---------------------------

def test_browse_with_token_is_not_401(tmp_path):
    host = _Host(tmp_path, api_token="secret-token")
    resp = _client(host).get(
        "/api/browse", headers={"authorization": "Bearer secret-token"}
    )
    assert resp.status_code != 401


def test_thread_message_with_token_is_not_401(tmp_path):
    host = _Host(tmp_path, api_token="secret-token")
    th = host.threads.create("t")
    resp = _client(host).post(
        f"/api/threads/{th.id}/message",
        json={"text": "hi"},
        headers={**_CSRF, "authorization": "Bearer secret-token"},
    )
    assert resp.status_code != 401


# --------------------------- bootstrap-статика остаётся открытой ---------------------------

def test_static_client_app_js_is_open_without_token(tmp_path):
    host = _Host(tmp_path, api_token="secret-token")
    resp = _client(host).get("/client/app.js")
    assert resp.status_code == 200


# --------------------------- AUTH_FAILURE в журнале ---------------------------

def test_auth_failure_is_journaled(tmp_path):
    host = _Host(tmp_path, api_token="secret-token")
    resp = _client(host).get("/api/browse")
    assert resp.status_code == 401
    rows = [
        json.loads(line)
        for line in host.journal.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(row.get("alert_kind") == "AUTH_FAILURE" for row in rows), (
        f"AUTH_FAILURE alert not found in journal, got rows={rows!r}"
    )


# --------------------------- api_token=None -> deny, не "выключено" ---------------------------

def test_none_token_literal_bearer_none_header_is_still_401(tmp_path):
    """cfg.api_token=None означает deny -- f"Bearer {None}" не имеет права стать валидным
    токеном, даже если клиент буквально пришлёт заголовок "Bearer None"."""
    host = _Host(tmp_path, api_token=None)
    resp = _client(host).get(
        "/api/browse", headers={"authorization": "Bearer None"}
    )
    assert resp.status_code == 401


def test_stub_host_without_cfg_at_all_denies_not_crashes(tmp_path):
    """Часть тестовых хостов в репо — bare object() без .cfg вовсе; getattr(getattr(...))
    обязан деградировать в deny (401), не бросить AttributeError/500."""
    resp = TestClient(build_web_app(object()), raise_server_exceptions=False).get(
        "/api/browse"
    )
    assert resp.status_code == 401


# --------------------------- лексический якорь: constant-time сравнение ---------------------------

def test_middleware_compares_token_with_hmac_compare_digest():
    src = inspect.getsource(webrtc_server)
    assert "hmac.compare_digest" in src
    # Амендмент 2: якорь остаётся дешёвой бронёй против замены на `==`, но контракт больше
    # не считает его доказательством -- поведенческие тесты ниже это доказательство и есть.


# ------------------- Амендмент 1: не-ASCII на границе authn не роняет 500 -------------------
#
# `hmac.compare_digest` на `str` бросает `TypeError`, если хоть один операнд не-ASCII.
# `supplied` -- то, что uvicorn декодирует latin-1 из сырых байт сокета; `expected` --
# f"Bearer {token}" из конфигурируемого секрета. Ни один операнд не был защищён: любой
# не-ASCII заголовок ИЛИ не-ASCII токен в конфиге превращали 401 в 500 -- и в последнем
# случае это било вообще ЛЮБОЙ запрос к защищённым роутам, включая запрос с ПРАВИЛЬНЫМ
# токеном. `starlette.testclient` (httpx) отказывается кодировать не-ASCII `str`-заголовок
# (UnicodeEncodeError на выходе из testclient, до ASGI-приложения), но `bytes`-значение
# пропускает как есть -- так и curl отдаёт байты уровню ASGI. Раздел ниже фиксирует все три
# сценария из амендмента как поведенческие тесты (не grep исходника).

def test_non_ascii_authorization_header_bytes_is_401_not_500(tmp_path):
    """Сценарий 1 амендмента: враждебный не-ASCII заголовок (сырые байты, как curl) обязан
    давать 401, не 500 -- ни один неаутентифицированный клиент не обязан уметь ASCII."""
    host = _Host(tmp_path, api_token="secret-token")
    resp = _client(host).get(
        "/api/browse", headers={"authorization": b"Bearer \xff\xfe"}
    )
    assert resp.status_code == 401, (
        f"non-ASCII header must deny (401), got {resp.status_code}: {resp.text!r}"
    )


def test_non_ascii_config_token_without_header_is_401_not_500(tmp_path):
    """Сценарий 3 амендмента: не-ASCII токен в конфиге (русский секрет в .env) не имеет
    права ронять запрос БЕЗ заголовка вовсе -- это обязано остаться 401 (deny), как и с
    ASCII-токеном, а не 500 из-за краша сравнения на этапе конфигурации."""
    host = _Host(tmp_path, api_token="секрет-токен-admin-picked")
    resp = _client(host).get("/api/browse")
    assert resp.status_code == 401, (
        f"non-ASCII config token + no header must deny (401), got {resp.status_code}: "
        f"{resp.text!r}"
    )


def test_non_ascii_config_token_with_correct_token_is_not_500(tmp_path):
    """Сценарий 2 амендмента: не-ASCII токен в конфиге + запрос с ПРАВИЛЬНЫМ токеном обязан
    давать корректный исход, не 500. Это решение сравнивает сырыми байтами заголовка против
    UTF-8-кодировки секрета (то, что реально уходит по проводу для не-ASCII заголовка) --
    совпадающий запрос аутентифицирует."""
    token = "секрет-токен-admin-picked"
    host = _Host(tmp_path, api_token=token)
    resp = _client(host).get(
        "/api/browse",
        headers={"authorization": f"Bearer {token}".encode("utf-8")},
    )
    assert resp.status_code != 500, f"correct non-ASCII token must not 500: {resp.text!r}"
    assert resp.status_code == 200, (
        f"correct non-ASCII token must authenticate (200), got {resp.status_code}: "
        f"{resp.text!r}"
    )


def test_non_ascii_hostile_header_still_journals_auth_failure(tmp_path):
    """До фикса краш происходил ВНУТРИ условия `if`, до тела с `journal.alert` -- AUTH_FAILURE
    был невидим ровно для враждебного не-ASCII ввода, то есть для ровно того класса атак,
    который аудит С5 обязан ловить. После фикса отказ обязан журналироваться как любой
    другой deny."""
    host = _Host(tmp_path, api_token="secret-token")
    resp = _client(host).get(
        "/api/browse", headers={"authorization": b"Bearer \xff\xfe"}
    )
    assert resp.status_code == 401
    rows = [
        json.loads(line)
        for line in host.journal.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(row.get("alert_kind") == "AUTH_FAILURE" for row in rows), (
        f"AUTH_FAILURE alert not found in journal for non-ASCII header, got rows={rows!r}"
    )


# --------------------------- run() fail-closed ---------------------------

def test_require_api_token_raises_without_token():
    cfg = SynapseConfig.from_env({})  # SYNAPSE_API_TOKEN unset -> api_token=None
    with pytest.raises(RuntimeError):
        _require_api_token(cfg)


def test_require_api_token_accepts_insecure_dev():
    cfg = SynapseConfig.from_env({"SYNAPSE_API_TOKEN": "insecure-dev"})
    _require_api_token(cfg)  # не бросает


# ------------------- Амендмент 1, вторая ветка: не-ASCII токен -------------------
#
# Middleware от не-ASCII токена больше не падает (сравнение побайтовое), но «не падает» !=
# «работает»: браузер такой токен ЛИБО не отправит вовсе, ЛИБО отправит не теми байтами.
# Проверено в живом Chromium (`new Headers({authorization: "Bearer " + tok})`):
#   "café"      -> заголовок собирается, но уходит одним байтом 0xE9 (ISO-8859-1), тогда как
#                  сервер ждёт utf-8 0xC3 0xA9 -> телефон получает ВЕЧНЫЙ 401, curl работает;
#   "парольник" -> TypeError: String contains non ISO-8859-1 code point.
# Оба исхода недиагностируемы у пользователя, поэтому ловим их на старте сервера.

@pytest.mark.parametrize("token", ["парольник", "café"])
def test_require_api_token_rejects_non_ascii_token(token):
    cfg = SynapseConfig.from_env({"SYNAPSE_API_TOKEN": token})
    assert cfg.api_token == token  # парсер не при чём: from_env честно донёс значение
    with pytest.raises(RuntimeError, match="не-ASCII"):
        _require_api_token(cfg)
