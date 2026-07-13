# Синапс UI v2 — план реализации слайсов UI-1..UI-3

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Домашний воркфлоу: каждый слайс исполняется tero-раном; этот план — канон для ран-файлов.

**Goal:** Свой тонкий PWA-клиент «как Codex» с голосом через vendored pipecat JS SDK (UI-1), бэк-фундамент тредов — RunSpec/тред-стор/персист ленты/зомби-реконсиляция (UI-2), треды и текстовый ход в UI (UI-3). Спека: `docs/superpowers/specs/2026-07-13-synapse-ui-v2-design.md` (v4, апрув Теро 2026-07-13).

**Architecture:** `/client/` начинает отдавать НАШ клиент (pre-read-bytes-роуты, как весь `/client/*` сегодня), prebuilt уезжает НЕПАТЧЕННЫМ на `/client/dev` (mount той же `PipecatPrebuiltUI`-статики). Голос — vendored однофайловый ESM-бандл `PipecatClient`+`SmallWebRTCTransport`, бьющий в существующий session-less `POST /api/offer`. Тред = надстройка над TaskStore (синглтон «одна активная задача» не тронут): `ThreadStore` персистит метаданные+ленту, `RunSpec` несёт launch-параметры в `KoraRunner.start/_run` (один снапшот для cwd/промпта/гейта). Текстовый ход = `DispatcherTurnLoop` с пер-тред историей + новый `AnthropicLLMClient`.

**Tech Stack:** vanilla JS (ноль сборки в serve-пути; вендор-бандл собирается один раз esbuild'ом и коммитится), FastAPI-роуты до mount'ов, pytest (анки `tests/test_kora_status_ui.py` — паттерны `_endpoint`/`_stub_host`), httpx (только для `AnthropicLLMClient`).

## Global Constraints

- Коммиты: короткие, lowercase, по-человечески; **НИКОГДА** никакой AI-атрибуции/Co-Authored-By; без `feat:`/`fix:` префиксов.
- **NO-EXFIL (Р-15)**: лента кора-шагов display-only; сырые кора-шаги НИКОГДА не попадают в LLM-контекст диспетчера (kora.py:205-215). Персист-писатель ленты пишет в файл, не в контекст.
- **Синглтон «одна активная задача»** (TaskStore) не меняется ни одним таском.
- **XSS-дисциплина**: в клиентском JS только `textContent`/`style`-присваивания, никакого `innerHTML` (лексические тесты это проверяют).
- Ключи только из `.env` через `SynapseConfig.from_env()` — никаких хардкодов значений.
- **Живой сервер PID 69700 (порт 7860) не рестартовать** — рестарт только по явному слову Теро; все проверки плана — тестами/локальным вторым портом.
- Замороженные тесты меняются ТОЛЬКО по перечню Task 4 Step 5 (одобрено спекой: поведение /client/ меняется намеренно).
- Пины вендоринга: `@pipecat-ai/client-js@1.12.0`, `@pipecat-ai/small-webrtc-transport@1.10.5`, `esbuild@0.25.5`, лицензии BSD-2-Clause.
- Запуск тестов: `python -m pytest tests/ -x -q` (полная суита сейчас зелёная, 272+; каждый таск гоняет свой файл + полную суиту перед коммитом).

## Факты, добытые в план-фазе (проба Р1 уже выполнена — это НЕ гипотезы)

1. **Bare-specifier'ы есть в обоих ESM-бандлах**: client-js → `bowser`, `events`, `uuid`; transport → `@daily-co/daily-js`, `@pipecat-ai/client-js`, `lodash/cloneDeep`. «Ноль сборки» на сырых npm-файлах невозможен; готового IIFE апстрим не публикует.
2. **Однократная сборка работает**: `esbuild entry.js --bundle --format=esm --platform=browser --minify` → самодостаточный `pipecat-bundle.mjs` 416KB, **0 bare-импортов** (`events` резолвится из npm-пакета `events` — браузерная реализация, прямая зависимость client-js). Это и есть фолбэк S26 «берём собранный бандл» — собираем сами и коммитим; serve-путь остаётся без сборки.
3. **Контракт транспорта**: `new SmallWebRTCTransport({ webrtcUrl: "/api/offer" })` (поле из `SmallWebRTCTransportConnectionOptions`; deprecated в пользу `webrtcRequestParams`, но в пине 1.10.5 работает — вендор пинован, апгрейд = повторная проба). Session-less `POST/PATCH /api/offer` уже есть в webrtc_server.py:245-252 («kept curl-testable»). `/start`-хендшейк НЕ нужен нашему клиенту.
4. **Контракт клиента**: `new PipecatClient({ transport, enableMic: true, callbacks })`; бот-аудио приходит через `onTrackStarted(track, participant)` — прикрепить к `<audio>` через `MediaStream([track])`; колбэки `onConnected/onDisconnected/onError/onTrackStarted` существуют в 1.12.0 (проверено по d.ts).
5. **Кто читает `_workspace()` сегодня**: `_build_options` (kora.py:451; передаёт путь в `_system_prompt`:464 — путь вшит в ТЕКСТ промпта) и `_gate_decision` (kora.py:488) — два прямых читателя. RunSpec-снапшот обязан заменить ОБА (находка B раунда 5).
6. **Лента**: записи `_message_to_log_entries` — dict `{"ts", "kind", "text"}` без task_id; runner добавит тег. Обе точки вставки log_sink в `_stream` уже глотают все исключения (kora.py:391-409) — персист-писателю не нужен свой try.
7. **Долг-пререквизит UI-3** (plan.md §5): history-шейп loop.py — `role: "tool"` записи без assistant-анонса tool_use — чинится ДО подключения реального LLM (Task 12 Step 1).

## File Structure

```
tools/vendor_pipecat.sh                    NEW  однократная регенерация вендор-бандла
synapse/pipeline/client/index.html         NEW  дом (шапка+светофор, «Открыть агента», списки UI-3)
synapse/pipeline/client/thread.html        NEW  (UI-3) тред-вью: лента + текстовый ход
synapse/pipeline/client/style.css          NEW  тёмная тема
synapse/pipeline/client/app.js             NEW  светофор-полл + голос-коннект (дом)
synapse/pipeline/client/thread.js          NEW  (UI-3) лента-полл + отправка message
synapse/pipeline/client/vendor/pipecat.mjs NEW  собранный бандл (коммитится)
synapse/pipeline/client/vendor/VENDOR.md   NEW  пины/лицензии/регенерация
synapse/bridge/runspec.py                  NEW  RunSpec (frozen dataclass)
synapse/threads.py                         NEW  ThreadStore: метаданные + лента
synapse/projects.py                        NEW  (UI-3) ProjectStore + валидация пути
synapse/dispatcher/llm_client.py           NEW  (UI-3) AnthropicLLMClient для текстовых ходов
synapse/pipeline/webrtc_server.py          MOD  роут-своп /client, /client/dev, API-роуты UI-3
synapse/pipeline/app.py                    MOD  build_host: ThreadStore/автотред/sink/turn_lock
synapse/bridge/kora.py                     MOD  RunSpec в start/_run, снапшот, тег task_id, денилист+
synapse/bridge/state.py                    MOD  зомби-реконсиляция в _load (S13)
synapse/config.py                          MOD  thread_feed_max
synapse/dispatcher/loop.py                 MOD  (UI-3) пер-тред история, thread_id, tool_use-шейп
synapse/dispatcher/tools.py                MOD  (UI-3) ToolCall.id
synapse/journal.py                         MOD  (UI-3) TurnRecord.thread_id
tests/test_ui_client.py                    NEW  UI-1: вендор/статика/роут-своп/mount-order
tests/test_runspec.py                      NEW  UI-2: три головы, снапшот, реконсиляция
tests/test_threads.py                      NEW  UI-2: ThreadStore, feed, автотред
tests/test_projects.py                     NEW  UI-3: валидация, atomic write
tests/test_text_turn.py                    NEW  UI-3: llm_client, пер-тред история, message API, CSRF
tests/test_webrtc_server.py                MOD  mount-путь
tests/test_slice5_pwa.py                   MOD  патч-тесты → наш index / dev непатчен
tests/test_kora_status_ui.py               MOD  инжект-тест → ссылки нашего index
```

---
---

# СЛАЙС UI-1 «голосовой каркас»

Живой продукт: телефон открывает НАШ клиент на `/client/`, тапом подключает голос, Кора выполняет и отвечает SPEAK'ом; prebuilt-консоль живёт на `/client/dev`. Риск Р1 доказывается первым.

### Task 1: Вендор-бандл pipecat JS SDK (S26, Р1)

**Files:**
- Create: `tools/vendor_pipecat.sh`
- Create: `synapse/pipeline/client/vendor/pipecat.mjs` (генерируется скриптом, коммитится)
- Create: `synapse/pipeline/client/vendor/VENDOR.md`
- Test: `tests/test_ui_client.py`

**Interfaces:**
- Produces: ESM-модуль `./vendor/pipecat.mjs` с named-экспортами `PipecatClient`, `RTVIEvent`, `SmallWebRTCTransport` — app.js (Task 3) импортирует ровно их.

- [x] **Step 1: Написать падающий тест самодостаточности бандла**

```python
# tests/test_ui_client.py
"""UI v2 слайс UI-1: вендор-бандл, наша статика /client, роут-своп, mount-order (S24/S26).
Лексические проверки — паттерн test_kora_status_ui.py (никакого браузера в CI)."""
import re
from pathlib import Path

import pytest

CLIENT_DIR = Path(__file__).parent.parent / "synapse" / "pipeline" / "client"

# Строковые литералы внутри минифицированного кода не матчатся: ищем именно import/from
# перед строкой БЕЗ ./ или / в начале (bare specifier ломает «ноль сборки» в браузере).
_BARE_IMPORT_RE = re.compile(r'(?:\bfrom|\bimport)\s*"(?![\./])([^"]+)"')


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
```

- [x] **Step 2: Прогнать тест — убедиться, что падает**

Run: `python -m pytest tests/test_ui_client.py -x -q`
Expected: FAIL — `FileNotFoundError: .../client/vendor/pipecat.mjs`

- [x] **Step 3: Написать скрипт регенерации**

```bash
#!/usr/bin/env bash
# Однократная регенерация вендор-бандла pipecat JS SDK (UI v2, S26).
# Требует node>=18 с npm. Результат КОММИТИТСЯ; serve-путь остаётся без сборки.
# Факт план-фазы: сырые ESM-бандлы несут bare-specifier'ы (bowser/events/uuid/
# daily-js/lodash) — поэтому bundle, а не import-map.
set -euo pipefail
CLIENT_JS_VERSION="1.12.0"
TRANSPORT_VERSION="1.10.5"
ESBUILD_VERSION="0.25.5"
OUT="$(cd "$(dirname "$0")/.." && pwd)/synapse/pipeline/client/vendor/pipecat.mjs"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"
npm init -y >/dev/null
npm install --no-audit --no-fund \
  "@pipecat-ai/client-js@${CLIENT_JS_VERSION}" \
  "@pipecat-ai/small-webrtc-transport@${TRANSPORT_VERSION}" >/dev/null
printf '%s\n%s\n' \
  'export { PipecatClient, RTVIEvent } from "@pipecat-ai/client-js";' \
  'export { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";' > entry.js
mkdir -p "$(dirname "$OUT")"
npx --yes "esbuild@${ESBUILD_VERSION}" entry.js --bundle --format=esm \
  --platform=browser --minify --outfile="$OUT"
echo "OK: $OUT"
```

Записать в `tools/vendor_pipecat.sh`, затем: `chmod +x tools/vendor_pipecat.sh && ./tools/vendor_pipecat.sh`
Expected: `OK: .../synapse/pipeline/client/vendor/pipecat.mjs` (~416KB)

- [x] **Step 4: Написать VENDOR.md**

```markdown
# Vendored pipecat JS SDK

| пакет | версия | лицензия |
|---|---|---|
| @pipecat-ai/client-js | 1.12.0 | BSD-2-Clause |
| @pipecat-ai/small-webrtc-transport | 1.10.5 | BSD-2-Clause |
| esbuild (только сборка) | 0.25.5 | MIT |

`pipecat.mjs` — самодостаточный ESM-бандл (0 bare-импортов), собран один раз
`tools/vendor_pipecat.sh` и закоммичен: сырые npm-бандлы импортируют
bowser/events/uuid/@daily-co/daily-js/lodash bare-specifier'ами и в браузере
без сборки не работают (проба 2026-07-13). Апгрейд = поднять пины в скрипте,
перегенерировать, прогнать `tests/test_ui_client.py` и живой голос-смоук.
Экспорты: `PipecatClient`, `RTVIEvent`, `SmallWebRTCTransport`.
```

- [x] **Step 5: Прогнать тесты — зелёные**

Run: `python -m pytest tests/test_ui_client.py -x -q`
Expected: 2 passed

- [x] **Step 6: Commit**

```bash
git add tools/vendor_pipecat.sh synapse/pipeline/client/vendor/ tests/test_ui_client.py
git commit -m "ui-1: vendor pipecat js sdk as single esm bundle, pins + license + self-contained test"
```

### Task 2: Статика клиента — index.html, style.css, app.js (светофор, без голоса)

**Files:**
- Create: `synapse/pipeline/client/index.html`
- Create: `synapse/pipeline/client/style.css`
- Create: `synapse/pipeline/client/app.js`
- Test: `tests/test_ui_client.py`

**Interfaces:**
- Consumes: существующие роуты `/client/manifest.webmanifest`, `/client/reconnect.js`, `/client/kora-status` (JSON `{color, liveness, task_status, awaiting_answer, task_text}`), `/client/logs` — относительными ссылками `./...` (наш index живёт на `/client/`).
- Produces: DOM-ids `kora-dot`, `agent-btn`, `conn-status`, `bot-audio`, `threads-list`, `projects-list` — их использует app.js (Task 3) и UI-3.

- [x] **Step 1: Дописать падающие лексические тесты**

```python
# добавить в tests/test_ui_client.py
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
```

- [x] **Step 2: Прогнать — падает** (`python -m pytest tests/test_ui_client.py -x -q` → FAIL, нет index.html)

- [x] **Step 3: index.html**

```html
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Синапс</title>
  <link rel="manifest" href="./manifest.webmanifest">
  <link rel="apple-touch-icon" href="./apple-touch-icon.png">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="theme-color" content="#0b0f14">
  <link rel="stylesheet" href="./style.css">
  <script defer src="./reconnect.js"></script>
  <script type="module" src="./app.js"></script>
</head>
<body>
  <header>
    <h1>Синапс</h1>
    <div id="kora-dot" title="Кора: статус неизвестен"></div>
  </header>
  <main>
    <button id="agent-btn" type="button">🎙 Открыть агента</button>
    <p id="conn-status">не подключено</p>
    <section id="threads-section" hidden>
      <h2>Треды</h2>
      <ul id="threads-list"></ul>
    </section>
    <section id="projects-section" hidden>
      <h2>Проекты</h2>
      <ul id="projects-list"></ul>
    </section>
    <p class="links"><a href="./logs">лента Коры</a> · <a href="./dev/">dev-консоль</a></p>
  </main>
  <audio id="bot-audio" autoplay playsinline></audio>
</body>
</html>
```

(Секции threads/projects стоят `hidden` — UI-3 их наполняет и открывает; UI-1 их не трогает.)

- [x] **Step 4: style.css**

```css
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100dvh; background: #0b0f14; color: #dbe4ee;
  font: 16px/1.45 -apple-system, "SF Pro Text", system-ui, sans-serif;
  padding: env(safe-area-inset-top) 16px env(safe-area-inset-bottom);
}
header { display: flex; align-items: center; justify-content: space-between; padding: 12px 0; }
h1 { font-size: 20px; margin: 0; font-weight: 600; }
#kora-dot {
  width: 14px; height: 14px; border-radius: 50%; background: #888;
  box-shadow: 0 0 4px rgba(0,0,0,.6); cursor: pointer;
}
main { display: flex; flex-direction: column; gap: 16px; padding-top: 8vh; }
#agent-btn {
  font-size: 22px; padding: 22px; border-radius: 16px; border: 1px solid #223;
  background: #121a24; color: #dbe4ee; cursor: pointer;
}
#agent-btn:active { background: #1a2634; }
#conn-status { color: #8fa3b8; margin: 0; text-align: center; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: #6c8096; margin: 8px 0 4px; }
ul { list-style: none; margin: 0; padding: 0; }
li { padding: 10px 4px; border-bottom: 1px solid #16202c; }
a { color: #7fb3e8; text-decoration: none; }
.links { text-align: center; color: #55677c; }
```

- [x] **Step 5: app.js — светофор-полл (идиома status-widget.js: цвет ГОТОВЫМ с сервера, интервал+visibilitychange, сеть упала → серый)**

```js
// Синапс UI v2, слайс UI-1: дом. Светофор — цвет приходит ГОТОВЫМ с /client/kora-status
// (_status_color на сервере, здесь ни логики статуса, ни wall-clock). Только
// textContent/style-присваивания (XSS: task_text — произвольный текст задачи).
const COLORS = { green: "#2ecc71", yellow: "#f1c40f", red: "#e74c3c" };
const dot = document.getElementById("kora-dot");

async function pollStatus() {
  let res;
  try {
    res = await fetch("./kora-status", { cache: "no-store" });
  } catch {
    dot.style.background = "#888";
    return;
  }
  if (!res.ok) { dot.style.background = "#888"; return; }
  const data = await res.json();
  dot.style.background = COLORS[data.color] || "#888";
  dot.title = (data.task_text ? "Кора: " + data.task_text : "Кора: нет задачи") + " · " + data.liveness;
}
pollStatus();
setInterval(pollStatus, 3000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollStatus(); });
dot.addEventListener("click", () => { location.href = "./logs"; });
```

- [x] **Step 6: Прогнать — зелёные** (`python -m pytest tests/test_ui_client.py -x -q`)

- [x] **Step 7: Commit**

```bash
git add synapse/pipeline/client/ tests/test_ui_client.py
git commit -m "ui-1: own thin client skeleton — home, dark theme, native traffic-light poll"
```

### Task 3: Голос в app.js через вендор-бандл (Р1-ядро)

**Files:**
- Modify: `synapse/pipeline/client/app.js`
- Test: `tests/test_ui_client.py`

**Interfaces:**
- Consumes: `./vendor/pipecat.mjs` (Task 1), session-less `POST /api/offer` (webrtc_server.py:245 — существует), DOM Task 2.
- Produces: тап «Открыть агента» = connect/disconnect-латч; бот-аудио в `#bot-audio`.

- [x] **Step 1: Падающий лексический тест**

```python
def test_app_js_wires_voice_through_vendored_sdk():
    body = (CLIENT_DIR / "app.js").read_text(encoding="utf-8")
    for token in (
        './vendor/pipecat.mjs"', "PipecatClient", "SmallWebRTCTransport",
        'webrtcUrl: "/api/offer"', "enableMic: true", "onTrackStarted", "MediaStream",
    ):
        assert token in body, f"app.js voice wiring missing {token!r}"
```

- [x] **Step 2: Прогнать — падает**

- [x] **Step 3: Дописать в app.js (после блока светофора)**

```js
// Голос (Р1): vendored SDK → session-less POST /api/offer (курлобельный роут
// webrtc_server.py; /start-хендшейк — деталь prebuilt-клиента, нам не нужен).
// Коннект-логика НАША — закрывает парковку слайса 5 (prebuilt умирал после 3 ретраев).
import { PipecatClient, SmallWebRTCTransport } from "./vendor/pipecat.mjs";

const btn = document.getElementById("agent-btn");
const connStatus = document.getElementById("conn-status");
const botAudio = document.getElementById("bot-audio");
let client = null;

function setConn(text) { connStatus.textContent = text; }

async function connectVoice() {
  client = new PipecatClient({
    transport: new SmallWebRTCTransport({ webrtcUrl: "/api/offer" }),
    enableMic: true,
    callbacks: {
      onConnected: () => { setConn("подключено — говори"); btn.textContent = "⏹ Завершить"; },
      onDisconnected: () => { setConn("не подключено"); btn.textContent = "🎙 Открыть агента"; client = null; },
      onTrackStarted: (track, participant) => {
        if (track.kind === "audio" && participant && !participant.local) {
          botAudio.srcObject = new MediaStream([track]);
        }
      },
      onError: () => setConn("ошибка соединения"),
    },
  });
  setConn("подключаюсь…");
  await client.connect();
}

btn.addEventListener("click", async () => {
  if (client) { const c = client; client = null; await c.disconnect(); return; }
  try {
    await connectVoice();
  } catch {
    setConn("не удалось подключиться");
    client = null;
  }
});
```

- [x] **Step 4: Прогнать файл + полную суиту** (`python -m pytest tests/test_ui_client.py -x -q && python -m pytest tests/ -q`)

- [x] **Step 5: Commit**

```bash
git add synapse/pipeline/client/app.js tests/test_ui_client.py
git commit -m "ui-1: voice connect through vendored sdk — tap latch, bot audio, own retry surface"
```

### Task 4: Роут-своп — /client/ = наш клиент, /client/dev = непатченный prebuilt

**Files:**
- Modify: `synapse/pipeline/webrtc_server.py` (блок статики :44-55, :258-284, :286-292, mount :360)
- Modify: `tests/test_webrtc_server.py:35`, `tests/test_slice5_pwa.py`, `tests/test_kora_status_ui.py:451-456`

**Interfaces:**
- Produces: endpoint-функции `client_index`/`client_index_html` (имена сохраняются — их знают тесты), новые `client_app_js`, `client_style_css`, `client_vendor_pipecat`; mount `name="client-dev"` на `/client/dev` с ТЕМ ЖЕ объектом `PipecatPrebuiltUI` (непатченность by construction).

- [x] **Step 1: Падающие тесты роут-свопа**

```python
# добавить в tests/test_ui_client.py
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


import asyncio

def test_client_root_serves_our_index_not_prebuilt():
    webrtc_server = _webrtc_server_or_skip()
    app = webrtc_server.build_web_app(host=object())
    for name in ("client_index", "client_index_html"):
        body = asyncio.get_event_loop().run_until_complete(_body(app, name))
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
```

(Если в суите уже настроен pytest-asyncio auto-mode — см. соседние async-тесты test_kora_status_ui.py — оформить `test_client_root_serves_our_index_not_prebuilt` как `async def` с `await _body(...)` вместо `run_until_complete`.)

- [x] **Step 2: Прогнать — падают**

- [x] **Step 3: Правка webrtc_server.py.** Удалить `_PWA_HEAD` (:47-55) и весь патч-блок (:258-284: `dist_index_path`…`_patched_index_bytes`, включая B6-RuntimeError — патча больше нет, гварду нечего охранять). Вместо них:

```python
# UI v2 слайс UI-1 (спека §4 «миграция»): /client/ отдаёт НАШ тонкий клиент; prebuilt
# уезжает НЕПАТЧЕННЫМ на /client/dev (тот же PipecatPrebuiltUI-объект). Инжекты слайса 5
# умирают вместе с патч-логикой: PWA-обёртка и реконнект теперь обязанность нашего index.
_CLIENT_DIR = Path(__file__).parent / "client"
_index_bytes = (_CLIENT_DIR / "index.html").read_bytes()
_app_js_bytes = (_CLIENT_DIR / "app.js").read_bytes()
_style_css_bytes = (_CLIENT_DIR / "style.css").read_bytes()
_vendor_pipecat_bytes = (_CLIENT_DIR / "vendor" / "pipecat.mjs").read_bytes()
```

`client_index`/`client_index_html` меняют `_patched_index_bytes` → `_index_bytes`. После роута `client_apple_touch_icon` добавить:

```python
    @app.get("/client/app.js")
    async def client_app_js():
        return Response(content=_app_js_bytes, media_type="text/javascript")

    @app.get("/client/style.css")
    async def client_style_css():
        return Response(content=_style_css_bytes, media_type="text/css")

    @app.get("/client/vendor/pipecat.mjs")
    async def client_vendor_pipecat():
        return Response(content=_vendor_pipecat_bytes, media_type="text/javascript")
```

Последней строкой перед `return app` (вместо старого mount):

```python
    app.mount("/client/dev", PipecatPrebuiltUI, name="client-dev")
```

- [x] **Step 4: Прогнать новые тесты — зелёные; полная суита покажет ровно замороженные падения**

Run: `python -m pytest tests/ -q`
Expected: падают ТОЛЬКО: `test_webrtc_server.py::test_web_app_exposes_offer_routes_and_client_mount` (mount-путь), `test_slice5_pwa.py` патч-тесты, `test_kora_status_ui.py::test_index_routes_inject_status_widget`. Любое другое падение = стоп, разбор.

- [x] **Step 5: Обновить замороженные тесты (одобрено спекой v4 — поведение /client/ меняется намеренно):**
  - `tests/test_webrtc_server.py:35`: `r.path == "/client"` → `r.path == "/client/dev"`.
  - `tests/test_kora_status_ui.py::test_index_routes_inject_status_widget` → переименовать в `test_index_routes_serve_our_client` и заменить ассерт: `assert "app.js" in body` и `assert "status-widget.js" not in body` (роут `client_status_widget_js` сам остаётся — его тест `test_status_widget_route_serves_safe_js` НЕ трогать).
  - `tests/test_slice5_pwa.py`: тесты «index патчится» и «B6 RuntimeError на смене анкера» удалить вместе с их monkeypatch-обвязкой; тесты PWA-статики (manifest/иконки/reconnect-роуты) сохранить как есть — роуты живы. В шапку файла — строка: «/client/ = наш клиент (UI v2 слайс UI-1); патч-инжект в prebuilt удалён, prebuilt непатченный на /client/dev».

- [x] **Step 6: Полная суита зелёная** (`python -m pytest tests/ -q`)

- [x] **Step 7: Commit**

```bash
git add synapse/pipeline/webrtc_server.py tests/
git commit -m "ui-1: /client serves own client, prebuilt unpatched at /client/dev, kill head-injection"
```

### Task 5: Mount-order-тесты (S24) + DoD-чеклист

**Files:**
- Modify: `tests/test_ui_client.py`

- [x] **Step 1: Тест порядка регистрации (расширяет паттерн test_kora_status_ui.py:488)**

```python
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
    ):
        assert idx[name] < mount_i, f"{name} must be registered BEFORE the /client/dev mount (S24)"
```

- [x] **Step 2: Прогнать файл + полную суиту; commit**

```bash
git add tests/test_ui_client.py
git commit -m "ui-1: mount-order tests — every exact /client route beats the /client/dev mount"
```

- [ ] **Step 3: DoD live-чеклист (руками Теро, план только фиксирует список):** телефон → `https://temirlans-macbook-air.tail97957e.ts.net/client/` открывает НАШ дом; A2HS ставит PWA с нашей иконкой; тап «Открыть агента» → пермишен мика → «подключено»; голосовой submit → Кора создаёт файл → completion-SPEAK слышен; светофор меняет цвет на живом ране; `/client/dev/` — prebuilt-консоль работает end-to-end (offer-хендшейк); reconnect: лок/wake телефона → страница жива или перезагрузилась по session-alive. Провал голоса в нашем клиенте = фолбэк Р1: голосовая часть остаётся на `/client/dev`, слайсы UI-2/UI-3 не блокируются.

---
---

# СЛАЙС UI-2 «фундамент» (бэк, визуал не трогает)

Живой продукт: рестарт сервера не блокирует задачи (S13) и не теряет ленту (S3); каждый запуск Коры несёт RunSpec и принадлежит треду.

### Task 6: RunSpec + per-run снапшот в KoraRunner (находка B: ВСЕ читатели)

**Files:**
- Create: `synapse/bridge/runspec.py`
- Modify: `synapse/bridge/kora.py` (:306-349 ctor/start, :373-381 `_run`, :439-465 `_build_options`, :488 `_gate_decision`)
- Test: `tests/test_runspec.py`

**Interfaces:**
- Produces: `RunSpec(thread_id: str, project_root: str | None = None, gate_mode: str = "full", model: str | None = None)`; `KoraRunner.start(task_id, text, spec: RunSpec | None = None)` (None → дефолтный RunSpec — обратная совместимость всех существующих тестов); приватный `KoraRunner._current_root() -> Path` — ЕДИНСТВЕННАЯ точка чтения корня для options/промпта/гейта.

- [x] **Step 1: runspec.py**

```python
"""RunSpec — единый носитель launch-параметров запуска Коры (спека UI v2 §3, раунды 4-5).
В начале `_run` атомарно кладётся в per-run снапшот; cwd опций, workspace в тексте
промпта и клетка гейта читают ОДИН источник — рассинхрон «трёх голов» невозможен по
построению (находка B). gate_mode/model потребляются слайсом UI-4; поля есть с рождения,
чтобы сигнатура больше не менялась."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunSpec:
    thread_id: str
    project_root: str | None = None  # None → дефолт-воркспейс (KORA_WORKSPACE_DIR)
    gate_mode: str = "full"          # "docs_only" появляется в UI-4
    model: str | None = None         # None → cfg.kora_model
```

- [x] **Step 2: Падающий тест «трёх голов» — гейт зовётся ИЗНУТРИ живого рана**

```python
# tests/test_runspec.py
"""UI v2 слайс UI-2: RunSpec — один снапшот для cwd/промпта/гейта (спека §3, находка B)."""
import asyncio
from pathlib import Path

import pytest

from synapse.bridge.kora import KoraRunner
from synapse.bridge.runspec import RunSpec
from synapse.bridge.state import SpeakLedger, TaskStatus, TaskStore
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal


class FakeClock:
    def __init__(self, t=0.0): self.t = t
    def now(self): return self.t


def _runner(tmp_path, captured):
    cfg = SynapseConfig(kora_workspace_dir=str(tmp_path / "default-ws"))
    clock = FakeClock()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)

    class FakeClient:
        def __init__(self, opts): captured["opts"] = opts
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def query(self, text): pass
        async def receive_response(self):
            r = captured["runner"]
            captured["gate_in_project"] = r._gate_decision(
                "Write", {"file_path": str(captured["proj"] / "a.txt")}
            )
            captured["gate_in_default_ws"] = r._gate_decision(
                "Write", {"file_path": str(tmp_path / "default-ws" / "b.txt")}
            )
            if False:
                yield None

    runner = KoraRunner(cfg, store, SpeakLedger(), clock, journal, None,
                        client_factory=lambda opts: FakeClient(opts))
    captured["runner"] = runner
    return runner, store


async def test_runspec_project_root_reaches_cwd_prompt_and_gate(tmp_path):
    proj = tmp_path / "proj"; proj.mkdir()
    captured = {"proj": proj}
    runner, store = _runner(tmp_path, captured)
    store.start_task("t1", "задача", TaskStatus.RUNNING, 0.0)

    await runner._run("t1", "задача", RunSpec(thread_id="th1", project_root=str(proj)))

    opts = captured["opts"]
    resolved = str(proj.resolve())
    assert opts.cwd == resolved                      # голова 1: cwd опций
    assert resolved in opts.system_prompt            # голова 2: путь в ТЕКСТЕ промпта
    allowed, _, _ = captured["gate_in_project"]
    assert allowed                                   # голова 3: гейт-клетка = проект
    denied, _, cat = captured["gate_in_default_ws"]
    assert not denied and cat == "outside_workspace" # дефолт-воркспейс теперь ЧУЖОЙ


async def test_runspec_none_project_root_falls_back_to_default_workspace(tmp_path):
    captured = {"proj": tmp_path / "unused"}
    captured["proj"].mkdir()
    runner, store = _runner(tmp_path, captured)
    store.start_task("t2", "задача", TaskStatus.RUNNING, 0.0)

    await runner._run("t2", "задача", RunSpec(thread_id="th1", project_root=None))
    assert captured["opts"].cwd == str((tmp_path / "default-ws").resolve())


async def test_snapshot_cleared_after_run_with_identity_guard(tmp_path):
    captured = {"proj": tmp_path / "p"}; captured["proj"].mkdir()
    runner, store = _runner(tmp_path, captured)
    store.start_task("t3", "з", TaskStatus.RUNNING, 0.0)
    await runner._run("t3", "з", RunSpec(thread_id="th1", project_root=str(captured["proj"])))
    assert runner._run_root is None and runner._run_owner is None
```

- [x] **Step 3: Прогнать — падает** (`python -m pytest tests/test_runspec.py -x -q` — TypeError: `_run` не принимает spec)

- [x] **Step 4: Правка kora.py.** Импорт `from synapse.bridge.runspec import RunSpec`. В `__init__` после `self._pending_answer`:

```python
        # UI-2 (спека §3, находка B): per-run снапшот launch-параметров. Ставится в начале
        # _run ДО создания клиента; ЕДИНСТВЕННЫЙ источник корня для _build_options /
        # _system_prompt / _gate_decision на время рана. Владелец = task_id (identity-guard,
        # как у _pending_answer): finally суперсиженного рана не трёт снапшот преемника.
        self._run_owner: str | None = None
        self._run_root: Path | None = None
        self._run_model: str | None = None
```

`start` и `_run`:

```python
    def start(self, task_id: str, text: str, spec: RunSpec | None = None) -> None:
        if self._active is not None and not self._active.done():
            self._active.cancel()
        coro = self._run(task_id, text, spec or RunSpec(thread_id=""))
        try:
            self._active = asyncio.create_task(coro)
        except RuntimeError:
            coro.close()
            self._terminalize_if_running(task_id)
```

```python
    async def _run(self, task_id: str, text: str, spec: RunSpec) -> None:
        # Снапшот АТОМАРНО до создания клиента (спека §3): резолв project_root|null → путь
        # происходит ровно один раз, здесь.
        root = Path(spec.project_root) if spec.project_root else self._workspace()
        self._run_owner, self._run_root = task_id, root
        self._run_model = spec.model or self._cfg.kora_model
        try:
            await asyncio.wait_for(self._stream(task_id, text), self._cfg.kora_deadline_s)
        except Exception as exc:  # noqa: BLE001 — CancelledError пролетает, finally работает
            self._journal.alert(AlertKind.KORA_RUN_FAILED, {"task_id": task_id, "error": repr(exc)})
        finally:
            if self._run_owner == task_id:  # identity-guard: не трогать снапшот преемника
                self._run_owner = None
                self._run_root = None
                self._run_model = None
            self._terminalize_if_running(task_id)
```

Единая точка чтения + перевод ОБОИХ читателей (находка B):

```python
    def _current_root(self) -> Path:
        """Один корень на все три головы (спека §3): во время рана — снапшот RunSpec;
        вне рана (юнит-вызов options/гейта без _run) — конфиг-дефолт."""
        return self._run_root if self._run_root is not None else self._workspace()
```

В `_build_options`: `workspace = self._current_root()` (вместо `self._workspace()`), `model=self._run_model or self._cfg.kora_model` (вместо `self._cfg.kora_model`). В `_gate_decision`: `workspace = self._current_root()` (вместо `self._workspace()`). После правки `grep -n "self._workspace()" synapse/bridge/kora.py` обязан показать ровно ДВА вхождения: `_run` (резолв null) и `_current_root` (фолбэк вне рана).

- [x] **Step 5: Прогнать файл + полную суиту** (существующие test_kora.py зовут `start(task_id, text)` — дефолтный RunSpec сохраняет старое поведение)

Run: `python -m pytest tests/test_runspec.py tests/test_kora.py -q && python -m pytest tests/ -q`
Expected: все зелёные

- [x] **Step 6: Commit**

```bash
git add synapse/bridge/runspec.py synapse/bridge/kora.py tests/test_runspec.py
git commit -m "ui-2: runspec carries thread/root/model into each kora run, one snapshot feeds cwd+prompt+gate"
```

**Известный residual (в Parking lot ран-файла):** окно суперсида — старый ран между `cancel()` и фактическим CancelledError может увидеть снапшот преемника (класс RISK-B2, как существующий bail-по-store-identity). Митигация уже есть: `_stream` бейлится по task_id стора; полный фикс (снапшот в локали рана, прокинутый в hook) — если критик слайс-рана потребует.

### Task 7: ThreadStore — метаданные (находка G) + лента (S3)

**Files:**
- Create: `synapse/threads.py`
- Modify: `synapse/config.py` (после `kora_log_max`)
- Test: `tests/test_threads.py`

**Interfaces:**
- Produces: `Thread` (dataclass: `id, title, project_id, stage, last_outcome, created_ts, updated_ts, task_ids`); `ThreadStore(clock, root, feed_max)` c методами `create(title, project_id=None) -> Thread`, `get(id)`, `list() -> list[Thread]` (updated desc), `append_task(thread_id, task_id)`, `set_outcome(thread_id, outcome)`, `thread_for_task(task_id) -> Thread | None`, `append_feed(thread_id, entry: dict)`, `read_feed(thread_id, limit=200) -> list[dict]`. Всё это потребляют Task 8/9 и API UI-3.

- [x] **Step 1: Падающие тесты**

```python
# tests/test_threads.py
"""UI v2 слайс UI-2: ThreadStore — персист метаданных (находка G) и ленты (S3)."""
import json

from synapse.threads import ThreadStore


class FakeClock:
    def __init__(self, t=0.0): self.t = t
    def now(self): return self.t


def test_create_persist_reload_and_task_index(tmp_path):
    clock = FakeClock(10.0)
    ts = ThreadStore(clock, tmp_path, feed_max=100)
    th = ts.create("сделай лендинг")
    ts.append_task(th.id, "task-1")
    clock.t = 20.0
    ts.set_outcome(th.id, "completed")

    reloaded = ThreadStore(FakeClock(), tmp_path, feed_max=100)  # рестарт хоста
    got = reloaded.get(th.id)
    assert got is not None and got.title == "сделай лендинг"
    assert got.task_ids == ["task-1"] and got.last_outcome == "completed"
    assert got.updated_ts == 20.0
    assert reloaded.thread_for_task("task-1").id == th.id
    assert reloaded.thread_for_task("nope") is None


def test_list_sorted_by_updated_desc(tmp_path):
    clock = FakeClock(1.0)
    ts = ThreadStore(clock, tmp_path, feed_max=100)
    a = ts.create("a"); clock.t = 2.0
    b = ts.create("b"); clock.t = 3.0
    ts.append_task(a.id, "t1")  # a обновился позже
    assert [t.id for t in ts.list()] == [a.id, b.id]


def test_feed_appends_survive_restart_and_cap(tmp_path):
    ts = ThreadStore(FakeClock(), tmp_path, feed_max=5)
    th = ts.create("x")
    for i in range(13):
        ts.append_feed(th.id, {"ts": float(i), "kind": "text", "text": f"e{i}"})
    tail = ThreadStore(FakeClock(), tmp_path, feed_max=5).read_feed(th.id)
    assert len(tail) <= 6                      # кап держит файл ~feed_max (допуск на фактор 1.2)
    assert tail[-1]["text"] == "e12"            # хвост свежий
    assert all(isinstance(e, dict) for e in tail)


def test_corrupt_thread_json_is_skipped_not_fatal(tmp_path):
    (tmp_path / "broken.json").write_text("{oops", encoding="utf-8")
    ts = ThreadStore(FakeClock(), tmp_path, feed_max=5)
    assert ts.list() == []
```

- [x] **Step 2: Прогнать — падает** (нет модуля)

- [x] **Step 3: synapse/threads.py**

```python
"""ThreadStore — треды UI v2 (спека §4). Тред = НАДСТРОЙКА над TaskStore: синглтон «одна
активная задача» не тронут, тред хранит СВОИ task_ids + ленту. Писатель метаданных —
синхронно в точках переходов (находка G), atomic tmp+rename как state.json. Лента (S3) —
append-only jsonl per-thread; ring-буфер хоста остаётся горячим кэшем, файл — правдой,
переживающей рестарт. Никакой Р-15-логики здесь нет: лента display-only по построению
(пишется вторым потребителем log_sink, читается только HTTP-роутом)."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from synapse.clock import Clock


@dataclass
class Thread:
    id: str
    title: str
    project_id: str | None = None
    stage: str = "collect"           # collect|propose|spec_plan|code|done — FSM въезжает в UI-4
    last_outcome: str | None = None  # completed|failed|cancelled — исход ПОСЛЕДНЕГО запуска
    created_ts: float = 0.0
    updated_ts: float = 0.0
    task_ids: list[str] = field(default_factory=list)


class ThreadStore:
    def __init__(self, clock: Clock, root: str | Path, feed_max: int = 2000) -> None:
        self._clock = clock
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._feed_max = feed_max
        self._threads: dict[str, Thread] = {}
        self._task_index: dict[str, str] = {}
        self._feed_counts: dict[str, int] = {}
        self._load()

    # --- метаданные (находка G) ---------------------------------------------------------

    def _load(self) -> None:
        for p in self._root.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue  # битый файл — пропуск, не крэш бута (паттерн B18)
            if not isinstance(d, dict) or not d.get("id"):
                continue
            t = Thread(
                id=str(d["id"]),
                title=str(d.get("title") or ""),
                project_id=d.get("project_id"),
                stage=str(d.get("stage") or "collect"),
                last_outcome=d.get("last_outcome"),
                created_ts=float(d.get("created_ts") or 0.0),
                updated_ts=float(d.get("updated_ts") or 0.0),
                task_ids=[str(x) for x in (d.get("task_ids") or [])],
            )
            self._threads[t.id] = t
            for tid in t.task_ids:
                self._task_index[tid] = t.id

    def _persist(self, t: Thread) -> None:
        path = self._root / f"{t.id}.json"
        tmp = path.with_suffix(".json.tmp")
        data = {
            "id": t.id, "title": t.title, "project_id": t.project_id, "stage": t.stage,
            "last_outcome": t.last_outcome, "created_ts": t.created_ts,
            "updated_ts": t.updated_ts, "task_ids": t.task_ids,
        }
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def create(self, title: str, project_id: str | None = None) -> Thread:
        now = self._clock.now()
        t = Thread(id=uuid.uuid4().hex[:12], title=title[:80], project_id=project_id,
                   created_ts=now, updated_ts=now)
        self._threads[t.id] = t
        self._persist(t)
        return t

    def get(self, thread_id: str) -> Thread | None:
        return self._threads.get(thread_id)

    def list(self) -> list[Thread]:
        return sorted(self._threads.values(), key=lambda t: t.updated_ts, reverse=True)

    def append_task(self, thread_id: str, task_id: str) -> None:
        t = self._threads.get(thread_id)
        if t is None:
            return
        t.task_ids.append(task_id)
        self._task_index[task_id] = thread_id
        t.updated_ts = self._clock.now()
        self._persist(t)

    def set_outcome(self, thread_id: str, outcome: str) -> None:
        t = self._threads.get(thread_id)
        if t is None:
            return
        t.last_outcome = outcome
        t.updated_ts = self._clock.now()
        self._persist(t)

    def thread_for_task(self, task_id: str) -> Thread | None:
        tid = self._task_index.get(task_id)
        return self._threads.get(tid) if tid else None

    # --- лента (S3) -----------------------------------------------------------------------

    def _feed_path(self, thread_id: str) -> Path:
        return self._root / f"{thread_id}.feed.jsonl"

    def append_feed(self, thread_id: str, entry: dict) -> None:
        path = self._feed_path(thread_id)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        n = self._feed_counts.get(thread_id)
        if n is None:
            n = sum(1 for _ in path.open(encoding="utf-8"))
        else:
            n += 1
        self._feed_counts[thread_id] = n
        if n > self._feed_max * 1.2:  # редкий rewrite вместо перечитывания на каждый append
            lines = path.read_text(encoding="utf-8").splitlines()[-self._feed_max:]
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp.replace(path)
            self._feed_counts[thread_id] = len(lines)

    def read_feed(self, thread_id: str, limit: int = 200) -> list[dict]:
        path = self._feed_path(thread_id)
        if not path.exists():
            return []
        out: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                out.append(d)
        return out
```

- [x] **Step 4: config.py — после `kora_log_max: int = 500` добавить**

```python
    # UI v2 (S3/S32): кап файла истории треда, аналог kora_log_max.
    thread_feed_max: int = 2000
```

- [x] **Step 5: Прогнать файл + суиту; commit**

```bash
git add synapse/threads.py synapse/config.py tests/test_threads.py
git commit -m "ui-2: threadstore — thread metadata writer + per-thread feed file with cap"
```

### Task 8: Автотред голосового submit + RunSpec-wiring + исход запуска

**Files:**
- Modify: `synapse/pipeline/app.py` (`SynapseHost.__init__`, `build_host` :213-233)
- Modify: `synapse/bridge/kora.py` (ctor: параметр `on_run_finished`; `_run` finally)
- Test: `tests/test_threads.py`

**Interfaces:**
- Consumes: `ThreadStore` (Task 7), `RunSpec` (Task 6), `KoraBridge.on_task_committed: Callable[[str, str], None]` (tools.py:107 — сигнатура НЕ меняется, меняется чем её заполняет build_host).
- Produces: `SynapseHost.threads: ThreadStore | None` и `SynapseHost.voice_thread: dict` (`{"id": str | None}`) — их читают роуты UI-3; `KoraRunner(on_run_finished=Callable[[str, str], None] | None)` — колбэк `(thread_id, outcome)`.

- [x] **Step 1: Падающий тест (уровень runner+store, без pipecat)**

```python
# добавить в tests/test_threads.py
import asyncio

from synapse.bridge.kora import KoraRunner
from synapse.bridge.runspec import RunSpec
from synapse.bridge.state import SpeakLedger, TaskStatus, TaskStore
from synapse.config import SynapseConfig
from synapse.journal import TurnJournal


class _OkClient:
    """Скриптованный клиент: один ResultMessage-подобный no-op — ран завершается сам."""
    def __init__(self, opts): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def query(self, text): pass
    async def receive_response(self):
        if False:
            yield None


async def test_run_finished_reports_thread_outcome(tmp_path):
    clock = FakeClock()
    store = TaskStore(clock)
    outcomes = []
    cfg = SynapseConfig(kora_workspace_dir=str(tmp_path / "ws"))
    runner = KoraRunner(cfg, store, SpeakLedger(), clock, TurnJournal(str(tmp_path / "j"), clock),
                        None, client_factory=_OkClient,
                        on_run_finished=lambda thread_id, outcome: outcomes.append((thread_id, outcome)))
    store.start_task("t1", "задача", TaskStatus.RUNNING, 0.0)
    await runner._run("t1", "задача", RunSpec(thread_id="th9"))
    # пустой стрим без task_completed → терминализация в FAILED → исход failed
    assert outcomes == [("th9", "failed")]
```

- [x] **Step 2: Прогнать — падает** (нет параметра on_run_finished)

- [x] **Step 3: kora.py.** Ctor: параметр `on_run_finished: Callable[[str, str], None] | None = None` → `self._on_run_finished = on_run_finished`. В `_run` finally, ПОСЛЕ `self._terminalize_if_running(task_id)`:

```python
            # UI-2 (находка G): исход запуска → тред. Источник — терминальный статус стора
            # ПОСЛЕ terminalize; чужой task в сторе (суперсид) → исход не наш, молчим.
            if self._on_run_finished is not None and spec.thread_id:
                task = self._store.task
                if task is not None and task.id == task_id:
                    outcome = {
                        TaskStatus.COMPLETED: "completed",
                        TaskStatus.FAILED: "failed",
                    }.get(task.status, "cancelled")
                    self._on_run_finished(spec.thread_id, outcome)
```

- [x] **Step 4: build_host wiring (app.py).** Импорты: `from synapse.bridge.runspec import RunSpec`, `from synapse.threads import ThreadStore`, `from pathlib import Path`. После `kora_log: deque = ...`:

```python
    # UI v2 слайс UI-2: треды. Автотред голосового submit: у голоса всегда есть текущий
    # тред (UI-3 даст клиенту его выбирать); нет → создаётся из текста задачи. Тред-стор
    # персистит метаданные синхронно в точках переходов (находка G).
    threads = ThreadStore(clock, Path(cfg.journal_dir) / "threads", feed_max=cfg.thread_feed_max)
    voice_thread: dict = {"id": None}
```

`kora_runner = KoraRunner(...)` получает `on_run_finished=threads.set_outcome`. Вместо `on_task_committed=kora_runner.start if kora_runner else None`:

```python
    def _on_task_committed(task_id: str, text: str) -> None:
        th = threads.get(voice_thread["id"]) if voice_thread["id"] else None
        if th is None:
            th = threads.create(title=text)
            voice_thread["id"] = th.id
        threads.append_task(th.id, task_id)
        kora_runner.start(task_id, text, RunSpec(thread_id=th.id, project_root=None))
```

и в KoraBridge: `on_task_committed=_on_task_committed if kora_runner else None`. SynapseHost ctor получает два опциональных поля (в конец сигнатуры, дефолты None — стабы тестов не ломаются): `threads: Any = None`, `voice_thread: dict | None = None`; сохранить `self.threads = threads`, `self.voice_thread = voice_thread if voice_thread is not None else {"id": None}`; build_host передаёт оба.

- [x] **Step 5: Тест wiring-а (лёгкий, через ToolHandlers как в существующих тестах диспетчера):** голосовой submit нетривиальной задачи создаёт тред, `thread_for_task(task_id)` находит его. Использовать паттерн стабов из tests/test_tools.py (ConfirmFlow с KeywordClassifier, submit неразрушающего текста → COMMITTED → on_task_committed).

```python
# добавить в tests/test_threads.py
from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.dispatcher.tools import KoraBridge, ToolHandlers


async def test_voice_submit_gets_auto_thread(tmp_path):
    clock = FakeClock()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    cfg = SynapseConfig()
    classifier = KeywordClassifier(cfg.destructive_keywords)
    confirm = ConfirmFlow(store, clock, classifier, journal, cfg.affirm_words,
                          cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    threads = ThreadStore(clock, tmp_path / "threads")
    voice_thread = {"id": None}
    started = []

    def _committed(task_id, text):
        th = threads.get(voice_thread["id"]) if voice_thread["id"] else None
        if th is None:
            th = threads.create(title=text)
            voice_thread["id"] = th.id
        threads.append_task(th.id, task_id)
        started.append((task_id, th.id))

    bridge = KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg,
                        on_task_committed=_committed)
    handlers = ToolHandlers(bridge, journal)
    handlers.begin_turn("turn-1")
    res = await handlers.submit_task(text="создай файл заметок")
    assert res["outcome"] == "committed"
    task_id, thread_id = started[0]
    assert threads.thread_for_task(task_id).id == thread_id
    assert threads.get(thread_id).title.startswith("создай файл")
```

(Если enum-значение исхода в SubmitResult иное — взять точную строку из tests/test_tools.py при написании, там она уже заасерчена.)

- [x] **Step 6: Прогнать файл + полную суиту; commit**

```bash
git add synapse/pipeline/app.py synapse/bridge/kora.py tests/test_threads.py
git commit -m "ui-2: voice submit gets auto-thread, runspec wired through commit path, run outcome lands on thread"
```

### Task 9: Персист-писатель ленты (S3) — второй потребитель log_sink

**Files:**
- Modify: `synapse/bridge/kora.py` (`_stream` :391-409 — тег task_id)
- Modify: `synapse/pipeline/app.py` (`build_host`: составной sink)
- Test: `tests/test_threads.py`

**Interfaces:**
- Consumes: записи `{"ts", "kind", "text"}` (факт 6); `ThreadStore.append_feed/thread_for_task`.
- Produces: каждая запись ленты несёт `task_id`; файл `journals/threads/<id>.feed.jsonl` получает все записи запусков треда. `/client/kora-log` не меняется (лишний ключ безвреден для logs.html — она читает kind/text/ts).

- [x] **Step 1: Падающий тест**

```python
# добавить в tests/test_threads.py
async def test_feed_writer_persists_kora_entries_by_task(tmp_path):
    clock = FakeClock()
    store = TaskStore(clock)
    threads = ThreadStore(clock, tmp_path / "threads")
    th = threads.create("тред")
    threads.append_task(th.id, "t1")

    def sink(entry: dict) -> None:  # копия wiring-а build_host
        tid = entry.get("task_id")
        target = threads.thread_for_task(tid) if tid else None
        if target is not None:
            threads.append_feed(target.id, entry)

    cfg = SynapseConfig(kora_workspace_dir=str(tmp_path / "ws"))
    runner = KoraRunner(cfg, store, SpeakLedger(), clock,
                        TurnJournal(str(tmp_path / "j"), clock), None,
                        client_factory=_OkClient, log_sink=sink)
    store.start_task("t1", "задача", TaskStatus.RUNNING, 0.0)
    await runner._run("t1", "задача", RunSpec(thread_id=th.id))

    feed = ThreadStore(FakeClock(), tmp_path / "threads").read_feed(th.id)  # рестарт
    assert feed and feed[0]["kind"] == "task" and feed[0]["task_id"] == "t1"
```

- [x] **Step 2: Прогнать — падает** (записи без task_id → лента пуста)

- [x] **Step 3: kora.py `_stream` — обе точки вставки получают тег:** первая: `self._log_sink({"ts": self._clock.now(), "kind": "task", "text": text, "task_id": task_id})`; вторая, в цикле: `for entry in _message_to_log_entries(msg, ts): self._log_sink({**entry, "task_id": task_id})`. Try/except вокруг обеих УЖЕ есть — не добавлять новых.

- [x] **Step 4: build_host — составной sink вместо `log_sink=kora_log.append`:**

```python
    def _kora_log_sink(entry: dict) -> None:
        # Горячий кэш live-стрима (ring) + правда на диске (S3). Исключения глотает
        # вызывающая сторона (_stream) — display-путь не валит ран по конструкции.
        kora_log.append(entry)
        tid = entry.get("task_id")
        th = threads.thread_for_task(tid) if tid else None
        if th is not None:
            threads.append_feed(th.id, entry)
```

и `KoraRunner(..., log_sink=_kora_log_sink)`.

- [x] **Step 5: Прогнать файл + суиту; commit**

```bash
git add synapse/bridge/kora.py synapse/pipeline/app.py tests/test_threads.py
git commit -m "ui-2: feed writer — log_sink gets a persistent per-thread consumer, entries tagged with task_id"
```

### Task 10: Зомби-реконсиляция бута (S13)

**Files:**
- Modify: `synapse/bridge/state.py` (`_load` :362-384)
- Test: `tests/test_runspec.py` (рядом с UI-2-тестами)

- [x] **Step 1: Падающий тест**

```python
# добавить в tests/test_runspec.py
import json as _json


def test_boot_reconciles_running_zombie_to_failed(tmp_path):
    clock = FakeClock()
    store = TaskStore(clock, journal_dir=tmp_path)
    store.start_task("z1", "зависшая", TaskStatus.RUNNING, 5.0)
    # «крэш»: новый процесс поднимает тот же journal_dir
    reborn = TaskStore(FakeClock(100.0), journal_dir=tmp_path)
    assert reborn.task.status == TaskStatus.FAILED
    assert not reborn.has_active_task()          # submit больше не режется навсегда
    assert any("перезапуск" in str(e.payload) for e in reborn.task.events)
    # реконсиляция ПЕРСИСТИТСЯ: третий бут видит уже терминальный статус
    third = TaskStore(FakeClock(200.0), journal_dir=tmp_path)
    assert third.task.status == TaskStatus.FAILED


def test_boot_keeps_terminal_statuses_untouched(tmp_path):
    clock = FakeClock()
    store = TaskStore(clock, journal_dir=tmp_path)
    store.start_task("c1", "готовая", TaskStatus.RUNNING, 5.0)
    store.set_task_status(TaskStatus.COMPLETED)
    reborn = TaskStore(FakeClock(100.0), journal_dir=tmp_path)
    assert reborn.task.status == TaskStatus.COMPLETED
```

- [x] **Step 2: Прогнать — падает** (RUNNING переживает бут)

- [x] **Step 3: state.py `_load` — в самый конец успешной ветки (после присвоения `self._staged`):**

```python
        # S13 (UI v2, слайс UI-2): зомби-реконсиляция бута. RUNNING в state.json на старте
        # процесса = сервер умер посреди рана: живого продюсера после рестарта не существует
        # по определению, а оставить как есть — liveness врёт OK и has_active_task() режет
        # любой submit НАВСЕГДА. Это не resurrection (статус идёт В терминал, не из него);
        # PENDING_CONFIRMATION/CANCEL_REQUESTED не трогаем — их чинит обычный флоу.
        if self._task is not None and self._task.status == TaskStatus.RUNNING:
            self._task.status = TaskStatus.FAILED
            self._task.events.append(
                KoraEvent(
                    id=f"boot-reconcile-{self._task.id}",
                    type="task_failed",
                    cls=EventClass.NARRATABLE,
                    payload={"reason": "сервер перезапускался"},
                    speak_text=None,
                    ts=self._clock.now(),
                )
            )
            self._persist()
```

- [x] **Step 4: Прогнать файл + ПОЛНУЮ суиту** (test_state.py/test_bughunt_w2_persistence.py трогают _load — любое их падение разобрать: если тест фиксирует «RUNNING переживает бут» как поведение — он замороженный, менять ТОЛЬКО с пометкой S13 в диффе; ожидание: таких нет, S13 был признанным багом)

- [x] **Step 5: Commit**

```bash
git add synapse/bridge/state.py tests/test_runspec.py
git commit -m "ui-2: boot reconciliation — running task in state.json becomes failed, submit unblocked (s13)"
```

---
---

# СЛАЙС UI-3 «треды и текст»

Живой продукт: на доме — проекты и треды; тред открывается с персист-лентой; текстовый ход в диспетчера из треда (пер-тред контекст, находка A); голос знает открытый тред.

### Task 11: ProjectStore + валидация пути (S12) + расширение гейт-денилиста

**Files:**
- Create: `synapse/projects.py`
- Modify: `synapse/bridge/kora.py` (:70-81 денилист)
- Test: `tests/test_projects.py`

**Interfaces:**
- Produces: `validate_project_path(raw: str) -> Path` (raises `ProjectValidationError`), `ProjectStore(path)` c `list() -> list[dict]`, `get(project_id) -> dict | None`, `async add(name, path) -> dict` (`{"id","name","path"}`). Потребляют роуты Task 14 и (в UI-4) привязка тредов.

- [x] **Step 1: Падающие тесты**

```python
# tests/test_projects.py
"""UI v2 слайс UI-3: проекты (S12/S28) — валидация пути и атомарный projects.json."""
import asyncio
import json
from pathlib import Path

import pytest

from synapse.projects import ProjectStore, ProjectValidationError, validate_project_path


def test_validation_rejects_dangerous_paths(tmp_path):
    for bad in ["/", str(Path.home()), "/etc", "/private/etc", "/usr", "/System", "/Library",
                str(Path.home() / ".config"), str(Path.home() / ".gnupg"),
                str(Path.home() / "Library/Keychains"), "relative/path",
                str(tmp_path / "не-существует")]:
        with pytest.raises(ProjectValidationError):
            validate_project_path(bad)


def test_validation_accepts_real_project_dir(tmp_path):
    proj = tmp_path / "myproj"; proj.mkdir()
    assert validate_project_path(str(proj)) == proj.resolve()


async def test_store_add_list_atomic(tmp_path):
    store = ProjectStore(tmp_path / "projects.json")
    proj_dir = tmp_path / "p1"; proj_dir.mkdir()
    p = await store.add("Проект", str(proj_dir))
    assert p["name"] == "Проект" and p["path"] == str(proj_dir.resolve())
    again = ProjectStore(tmp_path / "projects.json")
    assert [x["id"] for x in again.list()] == [p["id"]]
    assert again.get(p["id"])["path"] == p["path"]


def test_gate_denylist_covers_shell_configs_and_config_dir(tmp_path):
    from synapse.bridge.kora import _is_secret_path
    for p in [tmp_path / ".zshrc", tmp_path / ".bash_profile", tmp_path / ".profile",
              tmp_path / ".config" / "gh" / "hosts.yml",
              tmp_path / "Keychains" / "login.keychain-db"]:
        assert _is_secret_path(p), f"{p} must be secret"
```

- [x] **Step 2: Прогнать — падает**

- [x] **Step 3: synapse/projects.py**

```python
"""ProjectStore — проекты UI v2 (спека §4). Валидация пути (S12): проект = существующая
директория, НЕ корень/HOME/системные пути/секретные директории — это клетка Коры, ошибка
здесь = write-доступ агента куда не надо. projects.json пишется атомарно под asyncio.Lock
(S28: UI и голос не гоняются). Секрет-ФАЙЛЫ внутри валидного проекта ловит гейт-денилист
kora.py (_is_secret_path) — вторая линия, не эта."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

# Проверяются и сырой абсолютный путь, и resolved (macOS: /etc -> /private/etc).
_SYSTEM_ROOTS = ("/System", "/Library", "/usr", "/etc", "/private/etc", "/bin", "/sbin")
_FORBIDDEN_HOME_SUBDIRS = (".config", ".gnupg", "Library/Keychains")


class ProjectValidationError(ValueError):
    pass


def validate_project_path(raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ProjectValidationError("нужен абсолютный путь")
    try:
        rp = p.resolve()
    except (OSError, RuntimeError) as e:
        raise ProjectValidationError("путь не резолвится") from e
    home = Path.home().resolve()
    if rp == Path("/") or rp == home:
        raise ProjectValidationError("корень и домашняя директория целиком запрещены")
    for root in _SYSTEM_ROOTS:
        if str(p).startswith(root + "/") or str(p) == root or rp.is_relative_to(root):
            raise ProjectValidationError("системные пути запрещены")
    for sub in _FORBIDDEN_HOME_SUBDIRS:
        if rp.is_relative_to(home / sub):
            raise ProjectValidationError("секретные директории запрещены")
    if not rp.is_dir():
        raise ProjectValidationError("директория не существует")
    return rp


class ProjectStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._projects: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(data, list):
            self._projects = [
                {"id": str(d["id"]), "name": str(d.get("name") or ""), "path": str(d.get("path") or "")}
                for d in data
                if isinstance(d, dict) and d.get("id")
            ]

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._projects, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path)

    def list(self) -> list[dict]:
        return [dict(p) for p in self._projects]

    def get(self, project_id: str) -> dict | None:
        return next((dict(p) for p in self._projects if p["id"] == project_id), None)

    async def add(self, name: str, path: str) -> dict:
        rp = validate_project_path(path)
        async with self._lock:
            proj = {"id": uuid.uuid4().hex[:8], "name": (name or rp.name)[:60], "path": str(rp)}
            self._projects.append(proj)
            self._persist()
            return proj
```

- [x] **Step 4: kora.py денилист (спека §4 S12).** `_SECRET_DIR_SEGMENTS` += `".config", "keychains"`; `_SECRET_FILE_NAMES` += `".zshrc", ".zshenv", ".zprofile", ".bashrc", ".bash_profile", ".profile"`. Рядом комментарий: «UI v2 S12: запись в шелл-конфиг = persistence; ".config"-сегмент принимает редкие false positives (deny-only, прецедент B22)».

- [x] **Step 5: Прогнать файл + суиту (test_kora.py гейт-тесты — денилист расширился, старые allow-кейсы не должны упасть); commit**

```bash
git add synapse/projects.py synapse/bridge/kora.py tests/test_projects.py
git commit -m "ui-3: project store with path validation, gate denylist grows shell configs and ~/.config"
```

### Task 12: AnthropicLLMClient + tool_use-шейп истории (пререквизит из plan.md §5)

**Files:**
- Modify: `synapse/dispatcher/tools.py` (`ToolCall` :88-91)
- Modify: `synapse/dispatcher/loop.py` (`_dispatch_tool` :102-114, tool-цикл :78-84)
- Create: `synapse/dispatcher/llm_client.py`
- Test: `tests/test_text_turn.py`

**Interfaces:**
- Produces: `ToolCall(name, arguments, id: str = "")`; история loop'а получает канонический шейп: перед tool-результатами пишется `{"role": "assistant", "content": text, "tool_calls": [{"id", "name", "arguments"}]}`, tool-результат — `{"role": "tool", "tool_call_id", "name", "content"}`; `AnthropicLLMClient(api_key, model, timeout_s=30.0, transport=None)` реализует протокол `LLMClient.complete(messages, tools) -> (text, list[ToolCall])`.
- Consumes: `FunctionSchema` pipecat (name/description/properties/required) → Anthropic `input_schema`.

- [x] **Step 1: Падающие тесты (httpx.MockTransport, без сети)**

```python
# tests/test_text_turn.py
"""UI v2 слайс UI-3: текстовый ход — llm-клиент, tool_use-шейп истории, пер-тред контекст."""
import json

import httpx
import pytest

from synapse.dispatcher.llm_client import AnthropicLLMClient
from synapse.dispatcher.tools import ALL_SCHEMAS, ToolCall


def _mock(response_json, capture):
    def handler(request: httpx.Request) -> httpx.Response:
        capture["request"] = json.loads(request.content)
        capture["headers"] = dict(request.headers)
        return httpx.Response(200, json=response_json)
    return httpx.MockTransport(handler)


async def test_complete_maps_messages_tools_and_parses_tool_use():
    capture = {}
    resp = {
        "content": [
            {"type": "text", "text": "Отправляю Коре."},
            {"type": "tool_use", "id": "tu_1", "name": "submit_task",
             "input": {"text": "сделай файл"}},
        ]
    }
    client = AnthropicLLMClient("k", "claude-haiku-4-5", transport=_mock(resp, capture))
    text, calls = await client.complete(
        [
            {"role": "system", "content": "промпт\n\n[СОСТОЯНИЕ]..."},
            {"role": "user", "content": "сделай файл"},
        ],
        ALL_SCHEMAS,
    )
    assert text == "Отправляю Коре."
    assert calls == [ToolCall(name="submit_task", arguments={"text": "сделай файл"}, id="tu_1")]
    req = capture["request"]
    assert req["model"] == "claude-haiku-4-5"
    assert "[СОСТОЯНИЕ]" in req["system"]
    assert req["messages"][0] == {"role": "user", "content": "сделай файл"}
    names = [t["name"] for t in req["tools"]]
    assert "submit_task" in names and "answer_kora" in names
    assert capture["headers"]["x-api-key"] == "k"


async def test_complete_round_trips_tool_results_as_blocks():
    capture = {}
    client = AnthropicLLMClient("k", "m", transport=_mock({"content": [{"type": "text", "text": "готово"}]}, capture))
    await client.complete(
        [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "запускаю",
             "tool_calls": [{"id": "tu_1", "name": "submit_task", "arguments": {"text": "x"}}]},
            {"role": "tool", "tool_call_id": "tu_1", "name": "submit_task", "content": "{\"outcome\": \"committed\"}"},
        ],
        ALL_SCHEMAS,
    )
    msgs = capture["request"]["messages"]
    assert msgs[1]["content"][0]["type"] == "text"          # assistant: текст+tool_use блоки
    assert msgs[1]["content"][1] == {"type": "tool_use", "id": "tu_1", "name": "submit_task",
                                     "input": {"text": "x"}}
    assert msgs[2]["content"][0]["type"] == "tool_result"   # user: tool_result с тем же id
    assert msgs[2]["content"][0]["tool_use_id"] == "tu_1"
```

- [x] **Step 2: Прогнать — падает**

- [x] **Step 3: tools.py — `ToolCall` получает id:**

```python
@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    # UI-3: id вызова из LLM-ответа — нужен для канонического tool_use/tool_result-шейпа
    # истории (Anthropic Messages API); мок-пути оставляют "".
    id: str = ""
```

- [x] **Step 4: loop.py — канонический шейп (долг plan.md §5 «чинить ДО подключения реального LLM»).** В `ingest_user_turn` tool-цикл: перед диспатчем пачки писать assistant-анонс; `_dispatch_tool` пишет tool_call_id:

```python
        while tool_calls and passes < _MAX_TOOL_PASSES:
            # UI-3: канонический шейп — tool-результату предшествует assistant-ход с
            # tool_use-анонсом (без него Anthropic Messages API отклоняет историю).
            self._history.append({
                "role": "assistant",
                "content": text or "",
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments} for c in tool_calls
                ],
            })
            for call in tool_calls:
                await self._dispatch_tool(call)
            text, tool_calls = await self._complete()
            if text:
                record.llm_output = text
            passes += 1
```

```python
        self._history.append(
            {"role": "tool", "tool_call_id": call.id, "name": call.name,
             "content": json.dumps(result, ensure_ascii=False)}
        )
```

- [x] **Step 5: llm_client.py**

```python
"""AnthropicLLMClient — реализация протокола LLMClient (loop.py) поверх Anthropic
Messages API для ТЕКСТОВЫХ ходов диспетчера (UI-3, POST /message). Голосовой путь
(pipecat-каскад с failover) не тронут: это отдельный, дешёвый и синхронный клиент
одного tier'а. Ключ — из SynapseConfig (env), никогда не хардкод."""
from __future__ import annotations

import json
from typing import Any

import httpx

from synapse.dispatcher.tools import ToolCall

_API_URL = "https://api.anthropic.com/v1/messages"
_VERSION = "2023-06-01"


def _schema_to_tool(schema: Any) -> dict[str, Any]:
    return {
        "name": schema.name,
        "description": schema.description,
        "input_schema": {
            "type": "object",
            "properties": schema.properties or {},
            "required": schema.required or [],
        },
    }


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    out: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for c in m.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"],
                               "input": c["arguments"]})
            out.append({"role": "assistant", "content": blocks or m.get("content", "")})
        elif m["role"] == "tool":
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": m["content"],
                }],
            })
    return system, out


class AnthropicLLMClient:
    def __init__(self, api_key: str, model: str, timeout_s: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_s
        self._transport = transport

    async def complete(self, messages: list[dict[str, Any]], tools: list[Any]) -> tuple[str, list[ToolCall]]:
        system, msgs = _to_anthropic_messages(messages)
        payload = {
            "model": self._model,
            "max_tokens": 1024,
            "system": system,
            "messages": msgs,
            "tools": [_schema_to_tool(s) for s in tools],
        }
        async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
            resp = await client.post(
                _API_URL, json=payload,
                headers={"x-api-key": self._api_key, "anthropic-version": _VERSION},
            )
            resp.raise_for_status()
            data = resp.json()
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(name=block["name"], arguments=block.get("input") or {},
                                      id=block.get("id", "")))
        return "".join(text_parts), calls
```

- [x] **Step 6: Прогнать файл + ПОЛНУЮ суиту** (test_bughunt_w5/w1_dispatch трогают loop-историю — если какой-то ассертит точный старый шейп `{"role": "tool", "name": ...}` без tool_call_id, поправить ассерт на новый шейп с пометкой UI-3 в диффе)

- [x] **Step 7: Commit**

```bash
git add synapse/dispatcher/ tests/test_text_turn.py
git commit -m "ui-3: anthropic llm client for text turns, canonical tool_use history shape (plan.md §5 debt)"
```

### Task 13: Пер-тред контекст диспетчера (находка A)

**Files:**
- Modify: `synapse/dispatcher/loop.py` (:58, :60-91)
- Modify: `synapse/journal.py` (TurnRecord :42-43, запись :105)
- Modify: `synapse/runners/console.py:102` (не меняется функционально — дефолт)
- Test: `tests/test_text_turn.py`

**Interfaces:**
- Produces: `DispatcherTurnLoop(..., thread_feed_reader: Callable[[str], list[dict]] | None = None)`; `ingest_user_turn(transcript, thread_id: str = "voice")`; `TurnRecord.thread_id: str = ""`.
- Consumes: `ThreadStore.read_feed` (регидрация: записи `kind in ("user", "assistant")` — их пишет Task 14).

- [x] **Step 1: Падающие тесты**

```python
# добавить в tests/test_text_turn.py
from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import TaskStore
from synapse.config import SynapseConfig
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.dispatcher.tools import KoraBridge, ToolHandlers
from synapse.journal import TurnJournal


class FakeClock:
    def __init__(self, t=0.0): self.t = t
    def now(self): return self.t


class ScriptedLLM:
    """Возвращает текст с эхом ПОСЛЕДНЕЙ user-реплики и числа реплик в истории."""
    def __init__(self): self.seen = []
    async def complete(self, messages, tools):
        self.seen.append(messages)
        users = [m for m in messages if m["role"] == "user"]
        return f"ok:{users[-1]['content']}:{len(users)}", []


def _loop(tmp_path, feed_reader=None):
    clock = FakeClock()
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
                          cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    handlers = ToolHandlers(KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg), journal)
    llm = ScriptedLLM()
    return DispatcherTurnLoop(llm, handlers, confirm, store, journal, clock, cfg,
                              thread_feed_reader=feed_reader), llm


async def test_histories_are_isolated_per_thread(tmp_path):
    loop, llm = _loop(tmp_path)
    await loop.ingest_user_turn("привет из А", thread_id="thA")
    await loop.ingest_user_turn("привет из Б", thread_id="thB")
    record, reply = await loop.ingest_user_turn("ещё из А", thread_id="thA")
    # история треда А: 2 user-реплики, реплика Б НЕ просочилась
    assert reply == "ok:ещё из А:2"
    assert record.thread_id == "thA"
    a_msgs = llm.seen[-1]
    assert not any("из Б" in str(m.get("content", "")) for m in a_msgs)


async def test_cold_thread_rehydrates_from_feed(tmp_path):
    feed = {"thX": [
        {"kind": "user", "text": "старая реплика"},
        {"kind": "assistant", "text": "старый ответ"},
        {"kind": "tool_use", "text": "Write: ..."},   # кора-шаг — НЕ регидрируется (NO-EXFIL)
    ]}
    loop, llm = _loop(tmp_path, feed_reader=lambda tid: feed.get(tid, []))
    _, reply = await loop.ingest_user_turn("новая", thread_id="thX")
    msgs = llm.seen[-1]
    assert any(m["role"] == "user" and m["content"] == "старая реплика" for m in msgs)
    assert any(m["role"] == "assistant" and m["content"] == "старый ответ" for m in msgs)
    assert not any("Write:" in str(m.get("content", "")) for m in msgs)
```

- [x] **Step 2: Прогнать — падает** (нет thread_id/thread_feed_reader)

- [x] **Step 3: loop.py.** Ctor: `thread_feed_reader: Callable[[str], list[dict]] | None = None` (импорт Callable из typing) → `self._thread_feed_reader`; `self._history` → `self._histories: dict[str, list[dict[str, Any]]] = {}` + доступ:

```python
    def _history_for(self, thread_id: str) -> list[dict[str, Any]]:
        """Пер-тред контекст (спека §4, находка A): история LLM ключуется по треду.
        Холодный тред регидрируется из персиста РЕПЛИК (kind user/assistant) — кора-шаги
        display-only и в LLM-контекст не попадают НИКОГДА (NO-EXFIL)."""
        hist = self._histories.get(thread_id)
        if hist is None:
            hist = []
            if self._thread_feed_reader is not None:
                for e in self._thread_feed_reader(thread_id):
                    kind = e.get("kind")
                    if kind == "user":
                        hist.append({"role": "user", "content": str(e.get("text", ""))})
                    elif kind == "assistant":
                        hist.append({"role": "assistant", "content": str(e.get("text", ""))})
            self._histories[thread_id] = hist
        return hist
```

`ingest_user_turn(self, transcript: str, thread_id: str = "voice")`: `history = self._history_for(thread_id)`; все `self._history.append(...)` → `history.append(...)`; `_complete()` и `_dispatch_tool()` получают `history` параметром (`_complete(self, history)` использует его вместо `self._history`; `_dispatch_tool(self, call, history)`). После `record = self._journal.begin_turn(transcript)` добавить `record.thread_id = thread_id`.

- [x] **Step 4: journal.py.** `TurnRecord` — поле `thread_id: str = ""` (после `turn_id`); в dict записи турна (:105 район) добавить `"thread_id": self._current.thread_id if self._current else ""` рядом с turn_id.

- [x] **Step 5: Прогнать файл + полную суиту** (console.py зовёт `ingest_user_turn(text)` — дефолт `thread_id="voice"` сохраняет поведение; тесты loop'а из bughunt-волн — проверить, что не ассертят `loop._history` напрямую; если да — заменить на `loop._histories["voice"]` с пометкой UI-3)

- [x] **Step 6: Commit**

```bash
git add synapse/dispatcher/loop.py synapse/journal.py tests/test_text_turn.py
git commit -m "ui-3: per-thread dispatcher context — histories keyed by thread, rehydrate replies only, journal tagged"
```

### Task 14: HTTP API — projects/threads/feed/message + CSRF + очередь + C-guard

**Files:**
- Modify: `synapse/pipeline/app.py` (`build_host`: ProjectStore, text-канал, turn_lock, guard'ы)
- Modify: `synapse/pipeline/webrtc_server.py` (роуты ДО mount'ов)
- Test: `tests/test_text_turn.py`

**Interfaces:**
- Consumes: `SynapseHost.threads/voice_thread` (Task 8), `ProjectStore` (Task 11), `DispatcherTurnLoop` c thread_id (Task 13), `AnthropicLLMClient` (Task 12).
- Produces на host: `projects: ProjectStore`, `text_loop: DispatcherTurnLoop | None`, `turn_lock: asyncio.Lock`, `current_http_thread: dict` (`{"id": None}`). Роуты: `GET/POST /api/projects`, `GET/POST /api/threads`, `GET /api/threads/{id}/feed`, `POST /api/threads/{id}/message`, `POST /api/active-thread`. Мутирующие требуют `Content-Type: application/json` + Origin/Referer против Host (S4).

- [x] **Step 1: build_host дополнения (app.py).** После блока threads:

```python
    projects = ProjectStore(Path(cfg.journal_dir) / "projects.json")
    turn_lock = asyncio.Lock()
    current_http_thread: dict = {"id": None}

    # C-guard (спека §4, находка C — детерминированный серверный слой поверх промптового):
    # answer_kora доставляет ответ ТОЛЬКО когда ход идёт в треде awaiting-запуска. Деривация
    # та же, что у бейджа ❓ (S16): тред активной задачи.
    def _awaiting_thread_id() -> str | None:
        task = store.task
        th = threads.thread_for_task(task.id) if task is not None else None
        return th.id if th is not None else None

    def _voice_answer(text: str) -> bool:
        if kora_runner is None:
            return False
        awaiting = _awaiting_thread_id()
        if awaiting is not None and voice_thread["id"] not in (None, awaiting):
            return False  # голос стоит в чужом треде — ответ Коре не отсюда
        return kora_runner.provide_answer(text)

    def _http_answer(text: str) -> bool:
        if kora_runner is None:
            return False
        awaiting = _awaiting_thread_id()
        if awaiting is None or current_http_thread["id"] != awaiting:
            return False  # реплика из треда Б не должна улетать ответом Коре в А
        return kora_runner.provide_answer(text)
```

KoraBridge (голосовой): `on_answer=_voice_answer if kora_runner else None` (вместо прямого provide_answer). HTTP-канал — СВОЙ ToolHandlers (S7: `_current_turn_id`/дедуп не делятся с голосом) и свой bridge с тем же store/confirm (синглтоны):

```python
    http_bridge = KoraBridge(
        store=store, confirm_flow=confirm_flow, clock=clock, cfg=cfg,
        on_speak=on_speak,  # честно: readback/подтверждения озвучиваются в подключённый голос
        on_task_committed=None,  # заполняется ниже — тред хода, не голосовой
        on_cancel=kora_runner.request_cancel if kora_runner else None,
        on_answer=_http_answer if kora_runner else None,
    )

    def _http_task_committed(task_id: str, text: str) -> None:
        tid = current_http_thread["id"]
        th = threads.get(tid) if tid else None
        if th is None:
            th = threads.create(title=text)
        threads.append_task(th.id, task_id)
        root = None
        if th.project_id:
            proj = projects.get(th.project_id)
            root = proj["path"] if proj else None
        kora_runner.start(task_id, text, RunSpec(thread_id=th.id, project_root=root))

    if kora_runner is not None:
        http_bridge.on_task_committed = _http_task_committed
    http_handlers = ToolHandlers(http_bridge, journal)

    text_loop = None
    if cfg.anthropic_api_key:
        from synapse.dispatcher.llm_client import AnthropicLLMClient
        from synapse.dispatcher.loop import DispatcherTurnLoop
        text_loop = DispatcherTurnLoop(
            AnthropicLLMClient(cfg.anthropic_api_key, cfg.tier2_model),
            http_handlers, confirm_flow, store, journal, clock, cfg,
            thread_feed_reader=threads.read_feed,
        )
```

SynapseHost ctor — новые опциональные поля (дефолты None): `projects=None, text_loop=None, turn_lock=None, current_http_thread=None`; сохранить как атрибуты (turn_lock: `self.turn_lock = turn_lock or asyncio.Lock()`); build_host передаёт все.

- [x] **Step 2: Роуты (webrtc_server.py, ПЕРЕД mount'ами, после kora-роутов).**

```python
    # UI v2 слайс UI-3: API тредов/проектов. Анти-CSRF (S4): tailnet — сетевая граница,
    # не браузерная; мутирующий /api/* требует JSON content-type (HTML-форма не может)
    # + Origin/Referer против Host.
    def _csrf_ok(request: Request) -> bool:
        if not request.headers.get("content-type", "").startswith("application/json"):
            return False
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if origin:
            from urllib.parse import urlparse
            if urlparse(origin).netloc != request.headers.get("host", ""):
                return False
        return True

    def _thread_dict(t) -> dict:
        return {"id": t.id, "title": t.title, "project_id": t.project_id, "stage": t.stage,
                "last_outcome": t.last_outcome, "updated_ts": t.updated_ts,
                "created_ts": t.created_ts}

    @app.get("/api/projects")
    async def api_projects_list():
        return JSONResponse({"projects": host.projects.list()})

    @app.post("/api/projects")
    async def api_projects_add(request: Request):
        if not _csrf_ok(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        from synapse.projects import ProjectValidationError
        data = await request.json()
        try:
            proj = await host.projects.add(str(data.get("name") or ""), str(data.get("path") or ""))
        except ProjectValidationError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(proj)

    @app.get("/api/threads")
    async def api_threads_list():
        return JSONResponse({"threads": [_thread_dict(t) for t in host.threads.list()]})

    @app.post("/api/threads")
    async def api_threads_create(request: Request):
        if not _csrf_ok(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        data = await request.json()
        t = host.threads.create(str(data.get("title") or "новый тред"),
                                project_id=data.get("project_id"))
        return JSONResponse(_thread_dict(t))

    @app.get("/api/threads/{thread_id}/feed")
    async def api_thread_feed(thread_id: str, limit: int = 200):
        if host.threads.get(thread_id) is None:
            return JSONResponse({"error": "no such thread"}, status_code=404)
        return JSONResponse({"entries": host.threads.read_feed(thread_id, limit=limit)})

    @app.post("/api/threads/{thread_id}/message")
    async def api_thread_message(thread_id: str, request: Request):
        if not _csrf_ok(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        if host.text_loop is None:
            return JSONResponse({"error": "text turns disabled (no anthropic key)"}, status_code=503)
        if host.threads.get(thread_id) is None:
            return JSONResponse({"error": "no such thread"}, status_code=404)
        data = await request.json()
        text = str(data.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "empty text"}, status_code=400)
        async with host.turn_lock:  # S7: одна очередь ходов на хост
            host.current_http_thread["id"] = thread_id
            try:
                record, reply = await host.text_loop.ingest_user_turn(text, thread_id=thread_id)
            finally:
                host.current_http_thread["id"] = None
        now = host.clock.now()
        host.threads.append_feed(thread_id, {"ts": now, "kind": "user", "text": text})
        host.threads.append_feed(thread_id, {"ts": now, "kind": "assistant", "text": reply})
        return JSONResponse({"reply": reply})

    @app.post("/api/active-thread")
    async def api_active_thread(request: Request):
        if not _csrf_ok(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        data = await request.json()
        tid = data.get("id")
        if tid is not None and host.threads.get(str(tid)) is None:
            return JSONResponse({"error": "no such thread"}, status_code=404)
        host.voice_thread["id"] = str(tid) if tid else None
        return JSONResponse({"ok": True})
```

Плюс очередь на голосовой стороне (app.py, `_on_end_of_turn` :309-320): обернуть тело в `async with host.turn_lock:` — HTTP-ход не начнётся посреди открытия голосового хода. **Residual (Parking lot):** хвост голосового хода (tool-вызовы в pipecat-потоке) живёт после отпуска лока — полная сериализация требует pipecat-хирургии (сосед B13-grounding); канальная изоляция `_current_turn_id` уже снята отдельным http_handlers.

- [x] **Step 3: Падающие тесты API (стаб-хост, паттерн `_endpoint`; text_loop — ScriptedLLM)**

```python
# добавить в tests/test_text_turn.py — обвязка: собрать РЕАЛЬНЫЙ host через build_host
# нельзя (ключи/сеть) — собрать SimpleNamespace-стаб с точными полями, которые читают роуты:
# clock/store/threads/projects/text_loop/turn_lock/current_http_thread/voice_thread.
import asyncio
from types import SimpleNamespace

from synapse.threads import ThreadStore


def _api_host(tmp_path):
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    loop_obj, llm = _loop(tmp_path, feed_reader=threads.read_feed)
    from synapse.projects import ProjectStore
    return SimpleNamespace(
        clock=clock, store=loop_obj._store, threads=threads,
        projects=ProjectStore(tmp_path / "projects.json"),
        text_loop=loop_obj, turn_lock=asyncio.Lock(),
        current_http_thread={"id": None}, voice_thread={"id": None},
        journal=SimpleNamespace(close=lambda: None),
    )


class FakeRequest:
    def __init__(self, body=None, json_ct=True, origin=None, host="testserver"):
        self._body = body or {}
        self.headers = {"content-type": "application/json" if json_ct else "text/plain",
                        "host": host}
        if origin:
            self.headers["origin"] = origin
    async def json(self): return self._body


async def test_message_turn_is_thread_scoped_and_persisted(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("тред")
    ep = _endpoint(app, "api_thread_message")
    resp = await ep(th.id, FakeRequest({"text": "привет"}))
    assert resp.status_code == 200
    feed = host.threads.read_feed(th.id)
    assert [e["kind"] for e in feed] == ["user", "assistant"]


async def test_mutating_api_rejects_non_json_and_foreign_origin(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("тред")
    ep = _endpoint(app, "api_thread_message")
    assert (await ep(th.id, FakeRequest({"text": "x"}, json_ct=False))).status_code == 403
    assert (await ep(th.id, FakeRequest({"text": "x"}, origin="https://evil.example"))).status_code == 403


async def test_project_add_validates_path(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    ep = _endpoint(app, "api_projects_add")
    assert (await ep(FakeRequest({"name": "x", "path": "/etc"}))).status_code == 400
    proj_dir = tmp_path / "ok"; proj_dir.mkdir()
    assert (await ep(FakeRequest({"name": "x", "path": str(proj_dir)}))).status_code == 200
```

(`_webrtc_or_skip` — копия `_webrtc_server_or_skip` из test_ui_client.py; `_endpoint` — оттуда же. Существующие route-тесты зовут `build_web_app(host=object())` — роуты UI-3 читают host только ВНУТРИ хендлеров, регистрация с голым object() не падает: проверить, что это так, ничего не читать на module-scope.)

- [x] **Step 4: Прогнать файл + полную суиту**

- [x] **Step 5: C-guard-тест (детерминированный слой находки C)**

```python
async def test_http_answer_guard_blocks_wrong_thread(tmp_path):
    # прямой юнит на замыкание _http_answer невозможен (живёт в build_host) — проверяем
    # правило на уровне его составляющих: awaiting-тред ≠ current_http_thread → False.
    clock = FakeClock()
    store = TaskStore(clock)
    threads = ThreadStore(clock, tmp_path / "threads")
    a = threads.create("A"); b = threads.create("B")
    threads.append_task(a.id, "t1")
    store.start_task("t1", "з", TaskStatus.RUNNING, 0.0)
    store.set_awaiting()
    current_http_thread = {"id": b.id}
    delivered = []

    def _http_answer(text: str) -> bool:  # копия правила из build_host
        task = store.task
        th = threads.thread_for_task(task.id) if task is not None else None
        awaiting = th.id if th is not None else None
        if awaiting is None or current_http_thread["id"] != awaiting:
            return False
        delivered.append(text)
        return True

    assert _http_answer("ответ из Б") is False and delivered == []
    current_http_thread["id"] = a.id
    assert _http_answer("ответ из А") is True and delivered == ["ответ из А"]
```

(Импорт `TaskStatus` уже есть в файле. Правило продублировано в тесте намеренно — это спецификация формулы; wiring-расхождение поймает live-смоук слайса.)

- [x] **Step 6: Commit**

```bash
git add synapse/pipeline/ tests/test_text_turn.py
git commit -m "ui-3: threads/projects/message api with csrf checks, http tool channel, turn queue, thread-scoped answer guard"
```

### Task 15: UI тредов — дом наполняется, thread.html с лентой и текстовым ходом

**Files:**
- Modify: `synapse/pipeline/client/index.html`, `synapse/pipeline/client/app.js`
- Create: `synapse/pipeline/client/thread.html`, `synapse/pipeline/client/thread.js`
- Modify: `synapse/pipeline/webrtc_server.py` (роуты `/client/thread`, `/client/thread.js`)
- Test: `tests/test_ui_client.py`

**Interfaces:**
- Consumes: `GET /api/threads`, `GET /api/projects`, `POST /api/projects`, `GET /api/threads/{id}/feed`, `POST /api/threads/{id}/message`, `POST /api/active-thread` (Task 14).

- [x] **Step 1: Падающие лексические тесты**

```python
# добавить в tests/test_ui_client.py
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
```

- [x] **Step 2: thread.html**

```html
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Синапс — тред</title>
  <meta name="theme-color" content="#0b0f14">
  <link rel="stylesheet" href="./style.css">
  <script type="module" src="./thread.js"></script>
</head>
<body>
  <header>
    <a href="./" id="back">← назад</a>
    <span id="thread-title"></span>
    <div id="kora-dot" title="Кора: статус неизвестен"></div>
  </header>
  <main>
    <ul id="feed-list"></ul>
  </main>
  <footer>
    <input id="msg-input" type="text" placeholder="Напиши диспетчеру…" autocomplete="off">
    <button id="msg-send" type="button">➤</button>
  </footer>
</body>
</html>
```

- [x] **Step 3: thread.js**

```js
// UI v2 слайс UI-3: тред-вью. Лента = персист-файл треда (poll), текстовый ход =
// POST message тем же диспетчером. Только textContent (XSS: текст ленты произволен).
const params = new URLSearchParams(location.search);
const threadId = params.get("id");
const feedList = document.getElementById("feed-list");
const input = document.getElementById("msg-input");
const send = document.getElementById("msg-send");
const title = document.getElementById("thread-title");
const dot = document.getElementById("kora-dot");
const COLORS = { green: "#2ecc71", yellow: "#f1c40f", red: "#e74c3c" };
const KIND_ICONS = { task: "▶", text: "💬", thinking: "🧠", tool_use: "🔧",
                     tool_result: "·", result: "🏁", system: "⚙", user: "🗣", assistant: "🤖" };
let renderedCount = 0;

async function post(url, body) {
  return fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

function addEntry(e) {
  const li = document.createElement("li");
  li.textContent = (KIND_ICONS[e.kind] || "·") + " " + (e.text || "");
  if (e.kind === "thinking") {  // сворачиваемые рассуждения: строка, тап разворачивает
    const full = li.textContent;
    li.textContent = "🧠 Кора размышляет… (тап)";
    li.addEventListener("click", () => { li.textContent = full; }, { once: true });
  }
  feedList.appendChild(li);
}

async function pollFeed() {
  let res;
  try { res = await fetch(`/api/threads/${threadId}/feed?limit=500`, { cache: "no-store" }); }
  catch { return; }
  if (!res.ok) return;
  const data = await res.json();
  const fresh = data.entries.slice(renderedCount);
  fresh.forEach(addEntry);
  if (fresh.length) {
    renderedCount = data.entries.length;
    feedList.lastElementChild.scrollIntoView({ block: "end" });
  }
}

async function pollStatus() {
  let res;
  try { res = await fetch("./kora-status", { cache: "no-store" }); } catch { dot.style.background = "#888"; return; }
  if (!res.ok) { dot.style.background = "#888"; return; }
  const data = await res.json();
  dot.style.background = COLORS[data.color] || "#888";
}

send.addEventListener("click", async () => {
  const text = input.value.trim();
  if (!text) return;
  input.value = ""; send.disabled = true;
  try {
    const res = await post(`/api/threads/${threadId}/message`, { text });
    if (res.ok) await pollFeed();
  } finally {
    send.disabled = false;
  }
});
input.addEventListener("keydown", (e) => { if (e.key === "Enter") send.click(); });

title.textContent = "тред " + (threadId || "");
post("/api/active-thread", { id: threadId });  // голос теперь адресуется этому треду
pollFeed(); pollStatus();
setInterval(pollFeed, 3000); setInterval(pollStatus, 3000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) { pollFeed(); pollStatus(); } });
```

- [x] **Step 4: app.js — дом наполняет списки + сбрасывает голосовой тред**

```js
// Дом = голос на авто-треде: открытие дома сбрасывает активный тред.
fetch("/api/active-thread", { method: "POST", headers: { "content-type": "application/json" },
                              body: JSON.stringify({ id: null }) }).catch(() => {});

async function loadLists() {
  try {
    const [tRes, pRes] = await Promise.all([
      fetch("/api/threads", { cache: "no-store" }), fetch("/api/projects", { cache: "no-store" }),
    ]);
    if (tRes.ok) {
      const { threads } = await tRes.json();
      const ul = document.getElementById("threads-list");
      ul.replaceChildren();
      threads.slice(0, 20).forEach((t) => {
        const li = document.createElement("li");
        const a = document.createElement("a");
        a.href = "./thread?id=" + encodeURIComponent(t.id);
        a.textContent = (t.last_outcome === "failed" ? "✖ " : "") + t.title;
        li.appendChild(a);
        ul.appendChild(li);
      });
      document.getElementById("threads-section").hidden = threads.length === 0;
    }
    if (pRes.ok) {
      const { projects } = await pRes.json();
      const ul = document.getElementById("projects-list");
      ul.replaceChildren();
      projects.forEach((p) => {
        const li = document.createElement("li");
        li.textContent = p.name;
        ul.appendChild(li);
      });
      document.getElementById("projects-section").hidden = false;
    }
  } catch { /* сеть упала — дом остаётся пустым, не гадаем */ }
}
loadLists();
```

(Добавление проекта из UI: кнопка «+ проект» с двумя `prompt()`-полями name/path → `POST /api/projects` — вставить в секцию projects-section; `prompt` допустим v1, форма — полировка UI-4.)

- [x] **Step 5: Роуты (webrtc_server.py, рядом с client_app_js):** pre-read `_thread_html_bytes`, `_thread_js_bytes`; `GET /client/thread` → text/html, `GET /client/thread.js` → text/javascript. Добавить оба имени в mount-order-тест Task 5.

- [x] **Step 6: Прогнать файл + полную суиту; commit**

```bash
git add synapse/pipeline/client/ synapse/pipeline/webrtc_server.py tests/test_ui_client.py
git commit -m "ui-3: home lists threads/projects, thread view with persisted feed, text turn, active-thread for voice"
```

### Task 16: Интеграционная проверка слайса + DoD

- [x] **Step 1: Полная суита** — `python -m pytest tests/ -q` зелёная.
- [x] **Step 2: Локальный смоук на ВТОРОМ порту (живой сервер 7860 НЕ трогать):** `KORA_ENABLED=false python -c "...uvicorn на 7861 со стаб-ключами..."` — если ключей нет, пропустить и оставить на live-чек. Browser-чек: `/client/` дом, `/client/thread?id=...` открывается, `/client/dev/` живой.
- [ ] **Step 3: DoD live (Теро, после рестарта живого сервера ПО ЕГО СЛОВУ):** текстовый ход из треда доходит до диспетчера и отвечает; голосовой submit с дома создаёт тред, лента наполняется на глазах; рестарт сервера — треды/лента на месте, зомби-задача помечена «сервер перезапускался»; реплика в чужой тред при awaiting НЕ уходит ответом Коре.
- [x] **Step 4: Parking lot (переносится в ран-файлы слайсов):** окно суперсида снапшота (Task 6); хвост голосового хода вне turn_lock (Task 14); голосовой ход не пишет реплику в ленту треда (пишет только HTTP-ход; голосовые реплики появятся в ленте после B13-grounding — известный residual); `prompt()`-ввод проекта.

---
---

# Граница плана: UI-4 «стадии» и UI-5 «гигиена»

Сознательно НЕ планируются в этом файле — планируются отдельным документом ПОСЛЕ живого прогона UI-1..UI-3, потому что: (1) Р1 — если голос в нашем клиенте не взлетит на телефоне, фолбэк «голос на /client/dev» меняет клиентскую основу, поверх которой рисуются гейт-карточки UI-4; (2) спека сама отложила в план-фазу UI-4 два решения, зависящих от живого поведения (точный whitelist docs-путей и конвенция пути план-файла); (3) COLLECT-промпт диспетчера (Р2) калибруется по фактическому качеству текстового канала из UI-3. Скоуп UI-4/UI-5 зафиксирован в спеке §5 (пп. 4-5) — план допишется как `2026-XX-XX-synapse-ui-v2-slices-4-5.md`.

# Self-review (выполнен при написании)

1. **Spec coverage UI-1..UI-3**: S24 (Task 5), S26 (Task 1), Р1 (Task 3 + DoD), S3 (Task 9), S13 (Task 10), находка B (Task 6), находка G (Task 7), автотред+RunSpec-wiring (Task 8), S12+денилист (Task 11), долг tool_use-шейпа (Task 12), находка A (Task 13), S4/S7+находки C/H (Task 14), требование 11 «лента внутри треда» (Task 15). Голосовое добавление проекта — вне v1 (S11), FSM/гейт-карточки/модель-селект — UI-4 (граница выше).
2. **Placeholder scan**: TBD/TODO нет; все код-шаги несут конкретный код; «взять точную строку из test_tools.py» в Task 8 — указание на существующий ассерт, не дыра.
3. **Type consistency**: `RunSpec` поля везде `{thread_id, project_root, gate_mode, model}`; `ToolCall.id` введён в Task 12 и используется в Task 12 Step 4/5; `ThreadStore.read_feed` сигнатура совпадает у Task 7 (определение), Task 13 (`thread_feed_reader=threads.read_feed`) и Task 14 (роут); endpoint-имена в mount-order-тесте (Task 5) соответствуют определениям Task 4 и дополняются в Task 15 Step 5.
