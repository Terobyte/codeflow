"""С5 Run 2 (клиент) — bearer-токен в app.js (runs/2026-07-15-c5-bearer-authn.md).

Амендмент 2 контракта: лексический греп НЕ считается доказательством поведения. Там, где
можно реально ИСПОЛНИТЬ код (isAsciiToken/withAuth не трогают DOM), тесты ниже вырезают
функции ИЗ РЕАЛЬНОГО app.js (не переписывают вручную — иначе тест доказывает копию, а не
код) и гоняют их в node. Полный E2E прогон (single-flight-диалог на 5 параллельных 401,
webrtcRequestParams/new Headers на РЕАЛЬНОМ вендор-бандле → живой POST /api/offer с
`authorization: Bearer …`, и watchdog НЕ зацикливающий reload на затяжной серии 401 от
/client/session-alive) сделан живьём через Playwright MCP против настоящего запущенного
сервера (stub-хост по образцу tests/test_c5_authn.py::_Host) — вывод в отчёте ранa, не
воспроизведён здесь как pytest (в репо нет python-пакета playwright/node-тест-раннера).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).parent.parent / "synapse" / "pipeline" / "client" / "app.js"


def _read() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _extract_fn(js: str, signature: str) -> str:
    """Вырезает ОДНУ функцию по её точной сигнатуре — от неё до строки с одиноким '}'
    на нулевом уровне вложенности. Тот же приём, что test_ui_redesign.py::_fn_body, но
    считает фигурные скобки, а не ищет "\\n}\\n" текстом — тела ниже содержат вложенные
    object-литералы, чей "}" в столбец 0 никогда не попадает, так что оба приёма
    эквивалентны на практике; здесь считаем явно, чтобы не зависеть от отступов."""
    start = js.index(signature)
    depth = 0
    i = js.index("{", start)
    body_start = i
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[start : j + 1]
    raise AssertionError(f"unbalanced braces extracting {signature!r}")


def _node_or_skip() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH — cannot execute extracted JS behaviorally")
    return node


def _run_node(js_source: str) -> dict:
    node = _node_or_skip()
    proc = subprocess.run(
        [node, "--input-type=module", "-"],
        input=js_source,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node script failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return json.loads(proc.stdout)


# --------------------------- isAsciiToken: реальное исполнение ---------------------------


def test_is_ascii_token_rejects_non_ascii_accepts_plain_ascii():
    """Вырезаем isAsciiToken ИЗ app.js и реально гоняем его в node — не переписываем
    руками (drift risk), а исполняем byte-in-byte то, что реально попадёт в браузер."""
    js = _read()
    fn_src = _extract_fn(js, "function isAsciiToken(t)")
    assert "\\x00-\\x7f" in fn_src or "x00-\\x7f" in fn_src or "isAsciiToken" in fn_src

    script = f"""
{fn_src}
const cases = [
  ["secret-token-abc", true],
  ["insecure-dev", true],
  ["", true],
  ["café", false],
  ["секрет-токен-admin-picked", false],
  ["парольник", false],
];
const results = cases.map(([input, expected]) => ({{
  input, expected, actual: isAsciiToken(input),
}}));
console.log(JSON.stringify(results));
"""
    results = _run_node(script)
    for row in results:
        assert row["actual"] == row["expected"], (
            f"isAsciiToken({row['input']!r}) = {row['actual']}, expected {row['expected']}"
        )


# --------------------------- withAuth: retry-once + single decision point ---------------------------


def test_with_auth_retries_exactly_once_and_never_loops_on_persistent_401():
    """Вырезаем withAuth ИЗ app.js (доOfetch-замыкание + одно решение "взять токен/поймать
    401/спросить/повторить") и гоняем с фейковым ensureToken. Доказывает поведенчески: (1)
    успех без 401 не трогает ensureToken вовсе; (2) один 401 -> один ensureToken -> один
    повтор; (3) отмена (ensureToken -> null) НЕ повторяет запрос — возвращает исходный 401;
    (4) даже если токен получен, но повтор ОПЯТЬ 401 — второго ensureToken НЕ будет
    (иначе стойкий плохой токен зациклил бы диалог внутри одного withAuth-вызова)."""
    js = _read()
    fn_src = _extract_fn(js, "async function withAuth(doFetch)")
    assert "res.status === 401" in fn_src

    script = f"""
let ensureTokenCalls = 0;
let ensureTokenReturn = null;
async function ensureToken() {{ ensureTokenCalls++; return ensureTokenReturn; }}

{fn_src}

async function scenario(statuses, tokenOnPrompt) {{
  ensureTokenCalls = 0;
  ensureTokenReturn = tokenOnPrompt;
  let calls = 0;
  const doFetch = async () => {{
    const status = statuses[Math.min(calls, statuses.length - 1)];
    calls++;
    return {{ status }};
  }};
  const res = await withAuth(doFetch);
  return {{ finalStatus: res.status, doFetchCalls: calls, ensureTokenCalls }};
}}

(async () => {{
  const out = {{}};
  out.success_no_prompt = await scenario([200], "unused");
  out.single_401_then_ok = await scenario([401, 200], "fresh-token");
  out.cancelled_prompt_returns_original_401 = await scenario([401, 200], null);
  out.persistent_401_retries_once_not_forever = await scenario([401, 401, 401], "bad-but-nonempty");
  console.log(JSON.stringify(out));
}})();
"""
    out = _run_node(script)

    assert out["success_no_prompt"] == {
        "finalStatus": 200, "doFetchCalls": 1, "ensureTokenCalls": 0,
    }, "200 на первой попытке не должен звать ensureToken вовсе"

    assert out["single_401_then_ok"] == {
        "finalStatus": 200, "doFetchCalls": 2, "ensureTokenCalls": 1,
    }, "один 401 -> один ensureToken -> один повтор -> успех"

    assert out["cancelled_prompt_returns_original_401"] == {
        "finalStatus": 401, "doFetchCalls": 1, "ensureTokenCalls": 1,
    }, "отмена диалога (ensureToken -> null) не имеет права повторять запрос"

    assert out["persistent_401_retries_once_not_forever"] == {
        "finalStatus": 401, "doFetchCalls": 2, "ensureTokenCalls": 1,
    }, "стойкий плохой токен -> РОВНО один повтор внутри withAuth, не бесконечный цикл prompt-ов"


# --------------------------- структурные (лексические — НЕ доказательство) якоря ---------------------------


NET_HELPERS = ("getJSON", "postJSON", "postBlob", "patchJSON", "deleteJSON")


@pytest.mark.parametrize("helper", NET_HELPERS)
def test_all_five_fetch_helpers_route_through_with_auth(helper):
    """Структурный якорь (не поведенческий): единая точка добавления токена — все 5
    fetch-хелперов оборачивают свой fetch() в withAuth(), а не ставят заголовок сами по
    пять раз. Поведение withAuth уже доказано выше живым исполнением."""
    js = _read()
    sig = next(
        s for s in (f"async function {helper}(", f"function {helper}(") if s in js
    )
    fn_src = _extract_fn(js, sig)
    assert "withAuth(" in fn_src, f"{helper} does not route through withAuth()"
    assert "authHeaders(" in fn_src, f"{helper} does not attach bearer headers via authHeaders()"


def test_token_lives_in_one_localstorage_key():
    js = _read()
    assert js.count('const TOKEN_KEY = "synapse-api-token"') == 1
    # ровно два писателя/читатели остаются буквой localStorage — getToken/setToken;
    # весь остальной код обязан звать их, а не трогать localStorage(TOKEN) напрямую.
    assert js.count("localStorage.getItem(TOKEN_KEY)") == 1
    assert js.count("localStorage.setItem(TOKEN_KEY") == 1


def test_voice_transport_uses_webrtc_request_params_with_real_headers_object():
    """С5 развилка 1: webrtcUrl (deprecated, string-only) заменён на webrtcRequestParams —
    headers ОБЯЗАН быть new Headers(...), не голый объект (бандл зовёт .entries() на нём)."""
    js = _read()
    connect = _extract_fn(js, "async function connectVoice()")
    assert "webrtcRequestParams" in connect
    assert 'endpoint: "/api/offer"' in connect
    assert "new Headers({ Authorization: " in connect
    assert "webrtcUrl:" not in connect
    # токен нужен ДО первого SDP-обмена — если его ещё нет, connectVoice сам просит один раз
    assert "ensureToken()" in connect


def test_token_prompt_is_not_window_prompt():
    """Проектная дисциплина (UI-5 rename) уже запрещала window.prompt(); держим тот же якорь
    для диалога токена. См. также tests/test_hygiene.py::test_rename_ui_handlers_present_and_xss_safe
    и tests/test_ui_client.py — они пинят этот же инвариант на весь файл."""
    js = _read()
    assert "prompt(" not in js
    assert "openTokenDialog" in js and "buildTokenDialog" in js


def test_watchdog_probe_session_unchanged_by_c5():
    """Живой прогон (Playwright MCP, см. отчёт рана) доказал: серия 401 на /client/session-alive
    не двигает aliveMisses и не зовёт maybeReload — здесь фиксируем СТРУКТУРНО, что
    probeSession() не тронут этим раном (401 обрабатывается ниже, в getJSON/withAuth, а не
    здесь) — реализация osталась той же "getJSON бросает -> catch -> return", которая уже
    была неявно безопасна для 401 до С5."""
    js = _read()
    probe = _extract_fn(js, "async function probeSession()")
    assert "catch { return; }" in probe or "catch {\n    return;" in probe or "catch{return;}" in probe.replace(" ", "").replace("\n", "")
    assert "maybeReload()" in js
    # aliveMisses инкрементится ТОЛЬКО после успешного (не-throw) ответа getJSON
    assert re.search(r"aliveMisses\+\+|\+\+aliveMisses", probe)
