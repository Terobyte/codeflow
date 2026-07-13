# Синапс UI v2 — план реализации слайсов UI-4..UI-5

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Домашний воркфлоу: каждый слайс исполняется tero-раном; этот план — канон для ран-файлов.

**Goal:** Стадийный флоу тредов (UI-4): FSM COLLECT→PROPOSE→SPEC_PLAN→CODE→DONE, гейт-карточки и gate-эндпоинт, гейт-режим `docs_only`, модель пер-запуск, привязка треда к проекту. Гигиена (UI-5): чистый контекст нового треда, компакт, rename, архив. Спека: `docs/superpowers/specs/2026-07-13-synapse-ui-v2-design.md` (v4). Пререквизит: UI-1..UI-3 сданы (`7b2e541`…`a81d578`, 325 тестов) + live-DoD Теро по слайсам 1–3.

**Architecture:** Стадия — свойство треда (`Thread.stage`, поле уже живёт с UI-2), двигается ТОЛЬКО через `ThreadStore.set_stage` с таблицей легальных переходов; исход запуска ортогонален стадии (S2, `last_outcome` уже есть). Гейт-действия сходятся в ОДНОЙ хост-функции `gate_action(thread_id, action, model, confirm)` в `build_host` — её зовут и HTTP-роут `POST /api/threads/{id}/gate`, и новый голосовой инструмент диспетчера (S4: «голосовые эквиваленты добавляются туда же»). Запуск стадии = обычный `KoraRunner.start` с `RunSpec(gate_mode=..., model=...)` — поля есть с UI-2, потребители появляются здесь. `docs_only` — третья проверка в `_gate_decision` ПОСЛЕ секрет-денилиста и in-workspace (сужение, не замена). Свод (запрос) персистится на треде (`request_text`) — носитель между COLLECT и запусками. Гейт-карточки = записи ленты `kind: "gate_card"` — рендерятся thread.js как кнопки, персист/регидрация бесплатны (лента уже переживает рестарт; в LLM-контекст не попадают — регидрация UI-3 берёт только user/assistant).

**Tech Stack:** как UI-1..3 — vanilla JS (textContent-only), FastAPI-роуты до mount'ов, pytest (паттерны `_endpoint`/`SimpleNamespace`-хост из `tests/test_text_turn.py`), никаких новых зависимостей.

## Global Constraints

- Коммиты: короткие, lowercase, по-человечески; **НИКОГДА** никакой AI-атрибуции/Co-Authored-By; без `feat:`/`fix:` префиксов.
- **NO-EXFIL (Р-15)**: сырые кора-шаги НИКОГДА не в LLM-контекст диспетчера; компакт (UI-5) жмёт ТОЛЬКО реплики user/assistant этого треда; gate_card-записи ленты в регидрацию не попадают (фильтр UI-3 по kind сохраняется).
- **Синглтон «одна активная задача»** (TaskStore) не меняется. Гейт-запуск при чужом живом ране → 409, не supersede (S6: UI-путь НИКОГДА не зовёт start поверх живого рана).
- **XSS-дисциплина**: только `textContent`/`style`, никакого `innerHTML` (лексические тесты).
- Ключи только из `.env` через `SynapseConfig.from_env()`.
- **Живой сервер на 7860 не рестартовать без явного слова Теро**; проверки — тестами/вторым портом (staging 7861, tailnet :8443).
- Анти-CSRF UI-3 (`_csrf_ok`) обязателен на всех НОВЫХ мутирующих роутах.
- Замороженные тесты UI-1..3 не меняются; исключения перечислены в Task 1 Step 4 и Task 4 Step 4 (намеренные изменения поведения по спеке).
- Запуск тестов: `.venv/bin/python -m pytest tests/ -q` (сейчас 325 зелёных; каждый таск гоняет свой файл + полную суиту перед коммитом).

## Факты, добытые в план-фазе (проверено по коду 2026-07-13, HEAD `a81d578`)

1. **RunSpec уже несёт всё** (`bridge/runspec.py`): `thread_id, project_root, gate_mode="full", model=None` — сигнатуры `start/_run` менять не надо, UI-4 только ПОТРЕБЛЯЕТ gate_mode/model.
2. **Снапшот-паттерн готов**: `_run` кладёт `_run_owner/_run_root/_run_model` (kora.py:393-394) с identity-guard в finally (kora.py:402-405). `gate_mode` добавляется четвёртым полем ТЕМ ЖЕ паттерном.
3. **`_gate_decision` (kora.py:507-546)** — чистая функция, категории явные. `docs_only` встаёт после `_is_secret_path` и после in-workspace-резолва: у нас уже есть `resolved` и `ws_resolved` — сужение мутирующих инструментов до whitelist-путей = несколько строк перед `return True`.
4. **Мутирующие file-инструменты** по `_PATH_KEY`: `Write`, `Edit`, `NotebookEdit` (Read/Glob/Grep/LS — читающие, docs_only их НЕ трогает; `_SAFE_META_TOOLS` тоже).
5. **Thread (threads.py:17-26)** уже хранит `stage` (дефолт "collect") и `last_outcome` — UI-4 добавляет поля `request_text`, `last_model`, `archived` + методы переходов; `_load/_persist` расширяются симметрично.
6. **Точки старта запусков в app.py**: `_on_task_committed` (голос, :286) и `_http_task_committed` (:316) — обе зовут `kora_runner.start(task_id, text, RunSpec(...))`. Гейт-запуск — ТРЕТЬЯ точка, минуя ConfirmFlow (двухфазность уже отработана confirm-параметром/читкой ДО гейта): `store.start_task(...)` + `threads.append_task` + `runner.start`.
7. **TaskStore.start_task(task_id, text, status, now)** (state.py:192) и `has_active_task()` (:186) — всё, что нужно гейту; task_id генерится как в confirm.py:24 (`_new_task_id`).
8. **Промпт диспетчера** (prompt.py) — константы + `build_system_prompt(cfg, task_dictionary)`; loop.py:126 зовёт его на каждый ход. Стадийный блок добавляется как параметр `stage_block` (по умолчанию пустой — голос без открытого треда и старые тесты не трогаются).
9. **Инструменты диспетчера** (tools.py): `ALL_SCHEMAS` — 5 схем; dedup-латч и `_VALID_TOOL_NAMES` (loop.py:23) подхватят новые инструменты автоматически, если схемы добавлены в `ALL_SCHEMAS`.
10. **`_handle_question` identity-guard уже чистит `_pending_answer`** (kora.py:630-632) — межзапускный reset-тест (чат-2) проверяет СУЩЕСТВУЮЩЕЕ поведение на новой паре запусков SPEC_PLAN→CODE, кода не требует (если тест упадёт — это находка, чинить в kora.py).
11. **Лента**: `append_feed` пишет любой dict; thread.js рендерит по `KIND_ICONS` — неизвестный kind сейчас падает в дефолт-иконку, gate_card требует отдельной ветки рендера (кнопки).
12. **Модельный allowlist (S34)**: три id из спеки §4 — `claude-opus-4-8`, `claude-sonnet-5`, `claude-fable-5`; серверная константа, НЕ конфиг (конфиговый `kora_model` — дефолт, он в allowlist не обязан входить исторически — валидируем только пришедшее из UI/инструмента).

## Решения план-фазы (закрывают открытые вопросы спеки)

- **Р3 whitelist docs-путей (`docs_only`)**: мутирующий инструмент разрешён, если resolved-путь (уже внутри клетки) попадает в: (а) поддерево `<root>/docs/`, (б) top-level `*.md` файлы корня (`<root>/plan.md`, `<root>/README.md`, …). Всё остальное — deny `docs_only_violation`. Читается весь проект как раньше.
- **Конвенция план-файла (S2)**: `<root>/docs/plans/<thread_id>.md`. SPEC_PLAN-запуску путь диктуется в тексте задачи; гейт-проверка `write_code` — существование ровно этого файла. Тред без проекта — тот же путь внутри дефолт-воркспейса.
- **Текст задач запусков**: SPEC_PLAN → `"Подготовь спеку и план по запросу ниже. План запиши в файл docs/plans/<thread_id>.md (создай директории). Запрос: <request_text>"`. CODE стадийный → `"Реализуй по плану docs/plans/<thread_id>.md. Исходный запрос: <request_text>"`. CODE быстрый (S1) → сам `request_text`.
- **Свод**: новый инструмент диспетчера `propose_request(text)` — единственный переход collect→propose; кладёт `request_text`, пишет gate_card в ленту. [Правки]/`revise` стирают карточку логически (стадия назад; старая карточка в ленте остаётся историей, кнопки мертвы вне своей стадии — рендер сверяет стадию треда).
- **Голосовой гейт**: инструмент `gate_action(action, model?)` бьёт в ту же хост-функцию; двухфазность голоса = правило промпта (зачитка «отправляю вот это — верно?» / «точно пишем код в <проект>?» → только после явного «да» звать инструмент) — паттерн возможности «г» (Р-16). HTTP-двухфазность = `confirm: true` (S5).
- **Дефолт модели (находка E)**: `thread.last_model` (пишется при каждом гейт-запуске) → нет → `cfg.kora_model`.
- **Бейдж «ждёт» (О2)**: 409 от gate при занятом синглтоне; клиент показывает подпись «Кора занята другим тредом — ждёт» на карточке. Авто-старта из очереди нет (v1).
- **Компакт (S10)**: порог — новый конфиг `dispatcher_compact_after` (дефолт 40 сообщений истории). При превышении ПЕРЕД ходом: LLM-выжимка старшей половины истории (только user/assistant реплики) тем же `AnthropicLLMClient` → история = `[{"role":"user","content":"[КОМПАКТ] <выжимка>"}] + хвост`; в ленту — запись `kind:"event"` «контекст сжат». Сырая лента в файле не тронута (необратимого удаления нет).
- **Экран настроек (§2.3) НЕ в этом плане** — дефолт модели меняется конфигом; отдельный мини-слайс после live.

## File Structure

```
synapse/threads.py                     MOD  stage-FSM (set_stage+таблица), request_text/last_model/archived,
                                            bind_project (находка F), rename/archive, авто-title
synapse/bridge/kora.py                 MOD  _run_gate_mode снапшот, docs_only-ветка в _gate_decision
synapse/pipeline/app.py                MOD  build_host: gate_action-хелпер + per-thread gate-lock'и,
                                            стадийный блок в text_loop/voice, wiring новых инструментов
synapse/pipeline/webrtc_server.py      MOD  POST /api/threads/{id}/gate, GET /api/threads/{id},
                                            PATCH /api/threads/{id}, POST /api/threads/{id}/archive,
                                            DELETE /api/projects/{id}, POST /api/threads/{id}/bind-project
synapse/dispatcher/tools.py            MOD  схемы+хендлеры propose_request / gate_action / bind_project
synapse/dispatcher/loop.py             MOD  stage_block в _complete, компакт перед ходом (UI-5)
synapse/prompt.py                      MOD  стадийные правила COLLECT/PROPOSE (пачкой вопросы, S29)
synapse/config.py                      MOD  dispatcher_compact_after
synapse/pipeline/client/thread.js      MOD  чип стадии, рендер gate_card (кнопки+модель+двухфазный тап)
synapse/pipeline/client/thread.html    MOD  чип стадии в шапке
synapse/pipeline/client/app.js         MOD  чип стадии+бейджи в списке тредов, архив-фильтр, «⋯» меню
synapse/pipeline/client/style.css      MOD  чипы/карточки/кнопки
tests/test_stages.py                   NEW  UI-4: FSM, gate_action, docs_only, план-файл, single-flight,
                                            модельный allowlist, привязка, межзапускный reset
tests/test_hygiene.py                  NEW  UI-5: чистый контекст, компакт, rename/авто-title, архив,
                                            удаление проекта
tests/test_ui_client.py                MOD  лексические тесты новых кусков thread.js/app.js
```

---
---

# СЛАЙС UI-4 «стадии»

Живой продукт: в треде виден чип стадии; серьёзный запрос собирается диспетчером, сводится карточкой, уходит Коре на спеку-план в `docs_only`, план-файл открывает кнопку «Пиши код», код пишется вторым запуском с выбранной моделью. Быстрый путь — «сразу код» с явным confirm.

### Task 1: FSM треда в ThreadStore

**Files:** `synapse/threads.py`, `tests/test_stages.py`

**Steps:**
- [ ] Step 1: Тесты `tests/test_stages.py`: (а) легальные переходы collect→propose→spec_plan→code→done, propose/spec_plan→collect (revise), propose→code (быстрый путь); (б) нелегальные (collect→code, done→*, code→collect) → `ValueError`; (в) `set_request` пишет request_text и персистит; (г) `bind_project`: null→значение ок ПОКА `task_ids` пуст, повторная привязка / привязка после запуска / значение→значение → отказ (находка F); (д) `last_model` персистится; (е) рестарт (`_load`) восстанавливает все новые поля.
- [ ] Step 2: `Thread`: поля `request_text: str | None = None`, `last_model: str | None = None`, `archived: bool = False`. `_STAGE_TRANSITIONS: dict[str, frozenset[str]]` на модуле. Методы `set_stage(thread_id, stage)` (валидация + persist), `set_request(thread_id, text)`, `set_last_model(thread_id, model)`, `bind_project(thread_id, project_id) -> bool` (guard находки F: `project_id is None and not task_ids`). `_persist`/`_load` — новые ключи (отсутствующие в старых файлах → дефолты, битые файлы — прежний skip).
- [ ] Step 3: Суита зелёная; коммит `ui-4: thread stage fsm, request/model/archived fields, project binding guard`.

### Task 2: Гейт-режим docs_only в KoraRunner

**Files:** `synapse/bridge/kora.py`, `tests/test_stages.py`

**Steps:**
- [ ] Step 1: Тесты (стаб-раннер без SDK, паттерн test_runspec.py): в `gate_mode="docs_only"` — `Write` в `<ws>/docs/plans/x.md` → allow; `Write` в `<ws>/src/main.py` → deny `docs_only_violation`; `Edit` top-level `<ws>/plan.md` → allow; `Read`/`Grep` по всему проекту → allow; секрет (`<ws>/docs/.env`) → deny `secret_path` (порядок проверок!); `gate_mode="full"` — поведение байт-в-байт прежнее; вне рана (снапшот пуст) — дефолт full. Межзапускный reset (чат-2, факт 10): «SPEC_PLAN-запуск паркует AskUserQuestion → супersede CODE-запуском → `provide_answer` НЕ доставляется в новый ран, awaiting-флаг чист».
- [ ] Step 2: `_run_gate_mode: str | None` четвёртым слотом снапшота (kora.py:345-347): ставится в `_run` рядом с `_run_root` из `spec.gate_mode`, чистится в том же identity-guard finally. Хелпер `_current_gate_mode()` по образцу `_current_root` (дефолт `"full"`).
- [ ] Step 3: `_gate_decision`: константа `_MUTATING_FILE_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})`; после секрет-чека и `is_relative_to(ws_resolved)`, ПЕРЕД `return True`: если `_current_gate_mode() == "docs_only"` и инструмент мутирующий — путь обязан быть `resolved.is_relative_to(ws/"docs")` ИЛИ (`resolved.parent == ws_resolved` и суффикс `.md`), иначе `return False, "docs_only_violation", "docs_only_violation"` (категория-only, без пути — прецедент B21).
- [ ] Step 4: Суита; коммит `ui-4: docs_only gate mode — mutations narrowed to docs tree and top-level md`.

### Task 3: gate_action в build_host + запуск стадий

**Files:** `synapse/pipeline/app.py`, `tests/test_stages.py`

**Steps:**
- [ ] Step 1: Тесты (SimpleNamespace-хост, паттерн `_api_host`): `send_to_kora` из propose → стадия spec_plan, задача в сторе RUNNING, `RunSpec.gate_mode == "docs_only"`, текст задачи содержит request_text и путь план-файла; `send_to_kora` с `fast=true`-карточкой (propose при отсутствии план-требования) требует `confirm` → без него `{"error":"confirm_required"}`; `write_code` без план-файла → `{"error":"no_plan_file"}`, с файлом (tmp_path) → стадия code, `gate_mode == "full"`, модель из аргумента едет в RunSpec и в `last_model`; невалидная модель → `{"error":"invalid_model"}` (S34); занятый синглтон → `{"error":"busy"}` и стадия НЕ сдвинулась (S6); `revise` → collect без запуска; двойной конкурентный вызов на один тред — второй ждёт lock и получает busy (single-flight); CODE-успех → `last_outcome completed` и стадия done (через `_thread_run_finished`).
- [ ] Step 2: В `build_host` (рядом с C-guard-блоком): `_KORA_MODELS = frozenset({"claude-opus-4-8", "claude-sonnet-5", "claude-fable-5"})`; `gate_locks: dict[str, asyncio.Lock]`; асинхронный `gate_action(thread_id, action, model=None, confirm=False) -> dict`. Логика: тред существует → per-thread lock → валидация модели → ветвление:
  - `revise`: `set_stage(collect)` (легальность отдаёт ValueError → `{"error":"illegal_stage"}`), карточка-событие в ленту.
  - `send_to_kora` (из propose): request_text обязателен; **быстрый путь** = `confirm`-требование (двухфазность S1/S5) — семантика кнопки решается на карточке: стадийная карточка шлёт `send_to_kora`, быстрая — `send_to_kora` + `fast: true` + `confirm: true`; fast → стадия code + `gate_mode="full"` + текст = request_text; стадийный → spec_plan + `docs_only` + текст по конвенции.
  - `write_code` (из spec_plan): `confirm` обязателен; проверка план-файла `<root>/docs/plans/<id>.md` (root = проект треда или дефолт-воркспейс раннера); стадия code, `gate_mode="full"`, текст «реализуй по плану…».
  - Общий хвост запуска: `store.has_active_task()` → busy; `task_id = _new_task_id(now)` (импорт из confirm.py); `store.start_task(..., RUNNING, now)`; `threads.append_task`; `set_last_model`; `kora_runner.start(task_id, text, RunSpec(thread_id, project_root, gate_mode, model))`; gate_card-запись «запуск ушёл» в ленту.
- [ ] Step 3: `_thread_run_finished` (существующий колбэк исхода, находка G) дополняется: исход `completed` у CODE-запуска треда в стадии code → `set_stage(done)`.
- [ ] Step 4: `SynapseHost` получает `gate_action` полем; суита; коммит `ui-4: gate_action host helper — stage runs, plan-file check, model allowlist, single-flight`.

### Task 4: HTTP-гейт и API стадий

**Files:** `synapse/pipeline/webrtc_server.py`, `tests/test_stages.py`, `tests/test_text_turn.py` (замороженный `_thread_dict` — намеренное расширение)

**Steps:**
- [ ] Step 1: Тесты (паттерн `_endpoint`+`FakeRequest`): `POST /api/threads/{id}/gate` — CSRF 403; body `{action, model?, confirm?, fast?}` → прокси в `host.gate_action`, `{"error": "busy"}` → HTTP 409, `invalid_model`/`confirm_required`/`no_plan_file`/`illegal_stage` → 400 с телом, успех → свежий `_thread_dict`; неизвестный тред 404. `GET /api/threads/{id}` отдаёт `_thread_dict` (+`request_text`). Расширенный `_thread_dict` содержит `request_text`/`last_model`/`archived`.
- [ ] Step 2: Роуты `api_thread_get`, `api_thread_gate` ДО mount'а; `_thread_dict` расширяется новыми полями (тест UI-3 `test_message_turn_is_thread_scoped_and_persisted` проверяет подмножество ключей — не ломается; если проверяет равенство — правка входит в одобренный перечень).
- [ ] Step 3: Суита; коммит `ui-4: gate endpoint with csrf/409, thread detail api`.

### Task 5: Инструменты диспетчера и стадийный промпт

**Files:** `synapse/dispatcher/tools.py`, `synapse/prompt.py`, `synapse/dispatcher/loop.py`, `synapse/pipeline/app.py`, `tests/test_stages.py`

**Steps:**
- [ ] Step 1: Тесты: `propose_request` (в collect) → `set_request` + стадия propose + gate_card в ленте + dedup-латч работает; `gate_action`-инструмент бьёт в ту же хост-функцию (мок), возвращает её dict; `bind_project` — только имя из ProjectStore (отказ на неизвестное имя), guard находки F пробрасывается; стадийный блок в системном промпте: collect-тред получает COLLECT-правила, propose — PROPOSE, тред в code/done — без блока; голосовой ход без открытого треда — промпт байт-в-байт прежний (регресс-якорь).
- [ ] Step 2: tools.py: схемы `PROPOSE_REQUEST_SCHEMA` (text), `GATE_ACTION_SCHEMA` (action enum + model? + confirm?), `BIND_PROJECT_SCHEMA` (project_name) → в `ALL_SCHEMAS`; `KoraBridge` получает `on_propose`, `on_gate`, `on_bind` колбэки (None → `{"outcome":"unavailable"}`); хендлеры по образцу answer_kora (журналирование + dedup).
- [ ] Step 3: prompt.py: `build_system_prompt(cfg, task_dictionary, stage_block="")`; константы `STAGE_RULES_COLLECT` (копи контекст, вопросы ПАЧКОЙ в одном ходе — S29, лимит уточнений; свод готов → зачитай и по «верно» зови propose_request), `STAGE_RULES_PROPOSE` (изменения → обратно propose_request; «отправляй» → gate_action send_to_kora; «сразу код» → зачитка «точно пишем код в <проект>?» и только после явного да gate_action confirm=true — Р-16-паттерн). Блок вставляется ПОСЛЕ железных правил, ДО [СОСТОЯНИЕ].
- [ ] Step 4: loop.py: `_complete` принимает stage_block через новый колбэк `stage_block_for(thread_id)` (конструктор-параметр, None → ""); app.py прокидывает лямбду по `threads.get(thread_id).stage` и вяжет `on_propose/on_gate/on_bind` на оба бриджа (голосовой тред = `voice_thread["id"]`, HTTP = `current_http_thread["id"]` — паттерн C-guard'а).
- [ ] Step 5: Суита; коммит `ui-4: dispatcher stage tools and collect/propose prompt rules`.

### Task 6: UI стадий — чип, gate-карточки, модель

**Files:** `synapse/pipeline/client/thread.js`, `thread.html`, `app.js`, `style.css`, `tests/test_ui_client.py`

**Steps:**
- [ ] Step 1: Лексические тесты: thread.js содержит `gate_card`, `/gate`, названия стадий-чипа (`СБОР`/`ЗАПРОС`/`СПЕКА·ПЛАН`/`КОД`/`ГОТОВО`), двухфазный маркер (`точно`), селектор модели, `innerHTML` отсутствует; app.js — чип стадии в списке тредов.
- [ ] Step 2: thread.html: `<span id="stage-chip">` в шапке. thread.js: `GET /api/threads/{id}` в полле статуса → чип + бейджи (❓ awaiting из kora-status, ✖/⏹ из last_outcome); рендер записи `kind:"gate_card"`: контейнер с кнопками (текст по стадии карточки), `<select>` из трёх моделей (дефолт — last_model/пусто), кнопки живы ТОЛЬКО когда стадия карточки == текущая стадия треда; первый тап `write_code`/быстрого пути перекрашивает кнопку в «точно пишем код?» — второй тап шлёт с `confirm:true`; 409 → подпись «Кора занята — ждёт».
- [ ] Step 3: app.js: чип стадии рядом с заголовком треда в списке. style.css: чипы/карточки.
- [ ] Step 4: Суита; коммит `ui-4: stage chip and gate cards with model picker, two-phase code button`.

### Task 7: Интеграционный смоук UI-4 + DoD

**Steps:**
- [ ] Step 1: Staging на 7861 (`staging_7861.py`-паттерн: реальный конфиг, ИЗОЛИРОВАННЫЙ `journal_dir`, Кора включена, проект = тестовая tmp-папка): пройти стадийный путь руками через UI/curl — collect-реплики → propose_request → карточка → send_to_kora → живой SPEC_PLAN-запуск в docs_only (проверить gate_deny на Write вне docs в ленте Коры) → план-файл появился → «Пиши код» активна → CODE-запуск → done.
- [ ] Step 2: Быстрый путь и отказы: «сразу код» без confirm → 400; занятый синглтон → 409 и «ждёт».
- [ ] Step 3: **Live-DoD (Теро, телефон)**: голосом собрать запрос в треде → «отправляй» → услышать/увидеть спеку-план → «пиши код» с читкой → код в проекте. ✋ не отмечать без живого прогона.
- [ ] Step 4: Суита + коммит хвостов; parking lot в конец плана.

---
---

# СЛАЙС UI-5 «гигиена»

Живой продукт: тредов много и они не текут друг в друга; длинный тред жмётся сам; треды переименовываются и архивируются; удаление проекта не рвёт треды.

### Task 8: Чистый контекст нового треда (формальный якорь)

**Files:** `tests/test_hygiene.py`

**Steps:**
- [ ] Step 1: Тесты поверх UI-3-механики (без нового кода, если зелёные — фиксация; красные — чинить loop.py): два треда через один `DispatcherTurnLoop` — история Б не содержит реплик А ни в каком виде; регидрация холодного треда не тащит gate_card/event/кора-kinds (только user/assistant); [СОСТОЯНИЕ]-блок остаётся глобальным (один и тот же в обоих тредах).
- [ ] Step 2: Суита; коммит `ui-5: clean-context anchors for per-thread dispatcher isolation`.

### Task 9: Компакт длинного треда (S10)

**Files:** `synapse/dispatcher/loop.py`, `synapse/config.py`, `synapse/pipeline/app.py`, `tests/test_hygiene.py`

**Steps:**
- [ ] Step 1: Тесты (мок-LLM): история длиннее `dispatcher_compact_after` → перед ходом старшая половина заменяется ОДНИМ `[КОМПАКТ]`-сообщением (выжимка = отдельный вызов LLM с промптом «сожми диалог, сохрани решения/имена/пути»), хвост нетронут, следующий ход отвечает с учётом выжимки; лента получает `kind:"event"` «контекст сжат» (через новый опциональный колбэк `on_compact(thread_id)`); сырой feed-файл не изменился; NO-EXFIL — в выжимку не попали кора-kinds (по построению: жмётся история, куда они не входят); tool_use-хвосты (assistant с tool_calls + role:tool) режутся ТОЛЬКО целыми группами — оборванная пара tool_use/tool_result ломает Anthropic API.
- [ ] Step 2: config.py: `dispatcher_compact_after: int = 40` (+ from_env). loop.py: `_maybe_compact(thread_id, history)` в начале `ingest_user_turn`; граница разреза выравнивается по целым turn-группам; компакт-вызов идёт мимо `ALL_SCHEMAS` (tools=[]).
- [ ] Step 3: app.py: `on_compact` → `threads.append_feed(..., {"kind":"event","text":"контекст сжат"})`.
- [ ] Step 4: Суита; коммит `ui-5: dispatcher history compaction per thread with feed notice`.

### Task 10: Заголовки: авто-title и rename (S30)

**Files:** `synapse/threads.py`, `synapse/pipeline/webrtc_server.py`, `synapse/pipeline/app.py`, `synapse/pipeline/client/thread.js`, `tests/test_hygiene.py`

**Steps:**
- [ ] Step 1: Тесты: `maybe_autotitle(thread_id, text)` ставит title из первой реплики (обрезка 80, как create) ТОЛЬКО если title == "новый тред"; message-роут зовёт его на каждом user-ходе (второй ход не переименовывает); `PATCH /api/threads/{id}` `{title}` — CSRF, 404, пустой title → 400, успех персистится; лексический тест кнопки rename в thread.js.
- [ ] Step 2: threads.py `rename`/`maybe_autotitle`; роут `api_thread_patch`; вызовы autotitle в message-роуте и голосовом фиде (точка, где user-реплика уже пишется в ленту). thread.js: тап по заголовку → `prompt()` → PATCH (v1-паттерн проектов).
- [ ] Step 3: Суита; коммит `ui-5: thread auto-title from first message, rename endpoint and ui`.

### Task 11: Архив тредов и удаление проекта (S31)

**Files:** `synapse/threads.py`, `synapse/projects.py`, `synapse/pipeline/webrtc_server.py`, `synapse/pipeline/client/app.js`, `tests/test_hygiene.py`

**Steps:**
- [ ] Step 1: Тесты: `POST /api/threads/{id}/archive` → `archived=true`, тред пропадает из `GET /api/threads`, виден с `?archived=1`; файл и лента на месте; архив живого исполняемого треда → 409 (бейджи/awaiting не осиротеют); `DELETE /api/projects/{id}` → проект удалён из стора, у его тредов `project_id=None` + `kind:"event"` «проект удалён» в лентах, треды живы; CSRF на обоих.
- [ ] Step 2: threads.py `set_archived` + фильтр в `list(include_archived=False)` + `unbind_project(project_id)` (пробег по тредам). projects.py `remove(project_id)` (атомарный rewrite под тем же lock). Роуты `api_thread_archive`, `api_projects_delete`.
- [ ] Step 3: app.js: «⋯»-действие или свайп не строим — маленькая кнопка «архив» в строке треда + «удалить» у проекта с `confirm()`-диалогом; списки фильтруют archived.
- [ ] Step 4: Суита; коммит `ui-5: thread archive, project delete unbinds threads`.

### Task 12: Финальный смоук UI-5 + закрытие плана

**Steps:**
- [ ] Step 1: Staging-прогон: длинный диалог до компакта (порог временно занизить env'ом? нет — конфиг из .env стагинга), rename с телефона/браузера, архив, удаление проекта.
- [ ] Step 2: **Live-DoD (Теро)**: после его слова — рестарт живого сервера на HEAD; стадийный цикл голосом end-to-end на реальном проекте; старые треды/ленты живы после рестарта.
- [ ] Step 3: Обновить память проекта; ✔ чекбоксы; parking lot ниже.

## Parking lot (входящий из UI-1..3 + новое)

- Supersede-окно снапшота RunSpec; голосовой хвост `_on_end_of_turn` вне turn_lock; голосовые реплики не пишутся в ленту треда до B13-grounding; `prompt()`-ввод проекта/раним title.
- Экран настроек §2.3 (дефолт модели из UI, версия сервера).
- Очередь тредов с авто-стартом (v1 — только бейдж «ждёт»); per-thread workspace (S19); голосовое добавление проекта (S11); hotword/PTT (О1).
- `docs_only` не покрывает переименование/удаление через будущие инструменты (mv/rm живут в Bash — Bash denied целиком, риска нет до M1.1-эскалации).
