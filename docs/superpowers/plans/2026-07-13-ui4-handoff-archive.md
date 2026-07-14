# Архив UI-4 — что сдано и где остановились

Дата: 2026-07-13 · HEAD `275fcb0` · план: `2026-07-13-synapse-ui-v2-slices-4-5.md`

> Этот файл — handoff-якорь. План не закрыт (Task 4–12 впереди); здесь зафиксировано
> сданное состояние, принятые решения и блокировки, чтобы продолжить без раскопок.

## Сдано (Task 1–3, закоммичено на main)

| Task | Коммит | Файлы | Что |
|------|--------|-------|-----|
| 1 | `ace68e6` | `synapse/threads.py`, `tests/test_stages.py` | FSM стадий: `_STAGE_TRANSITIONS`, `set_stage`/`set_request`/`set_last_model`/`bind_project`; поля `request_text`/`last_model`/`archived`; guard находки F |
| 2 | `24a307c` | `synapse/bridge/kora.py`, `tests/test_stages.py` | `docs_only` gate mode: `_run_gate_mode` слот снапшота (4-й), `_current_gate_mode()`, `_MUTATING_FILE_TOOLS`, ветка `docs_only_violation` в `_gate_decision` внутри `is_relative_to` |
| 3 | `275fcb0` | `synapse/pipeline/app.py`, `tests/test_stages.py` | `gate_action` (МЕТОД `SynapseHost`, не closure), `_launch_run`, `_run_finished` обёртка (code→done), `_KORA_MODELS` allowlist, holder `_on_run_finished`, `_GATE_TASK_SEQ` |

## Отдельный коммит (багфикс, не UI-4)

`500cfd5` — `bugfix layer`: робастность classify google-429; `contextvars` dedup-latch + LRU в tools.py;
`OrderedDict` `_histories` LRU в loop.py; webrtc monitor lifecycle (startup/cancel). FakeRequest
получил same-origin default под усиленный CSRF. **Известная незавершённость:**
`test_bughunt_w3.py::test_b13_voice_end_of_turn_arms_turn_latch` теперь RED — contextvar-latch
не переживает `asyncio.run` в тесте. Чинится отдельно, не в рамках UI-4/UI-5.

## Тестовая база

- `tests/test_stages.py` — НОВЫЙ файл, **35 тестов**: Task 1 (FSM/fields/guard/load) + Task 2
  (docs_only/full/secret/snapshot-reset) + Task 3 (gate_action: send_to_kora/fast/write_code/
  stale_plan/busy/single-flight/revise/run_finished). Все зелёные.
- **Baseline:** `pytest tests/ -q --ignore=tests/test_bugs_audit.py --ignore=tests/test_b_pipe_bugs.py --ignore=tests/test_concurrency_race.py` = 378 passed (минус `test_b13` — bughunt).
- Полный `pytest tests/ -q` = **11 failed**: 10 × `test_bugs_audit.py` (B-UX — намеренно-красные
  failing-on-buggy audits Теро) + 1 × `test_b13` (незавершённость contextvars, см. выше).
  `test_b_pipe_bugs.py` / `test_concurrency_race.py` — тоже failing-on-buggy audits (B-PIPE / race).

## Принятые решения (действуют для Task 4–12)

1. **Task 5 Step 3b (голосовой системный промпт) — ДЕЛАТЬ.** Выбрана рекомендация, не descope.
   Сегодня голосовой `LLMContext` строится БЕЗ system-сообщения (факт 15) → stage_block и двухфазность
   на голос не доходят. Без Step 3b голос-DoD (Task 7 Step 3, Task 12 Step 2) липовые. Точка впрыска
   в pipecat-агрегаторах НЕ очевидна — если неясна из кода, **гонять живым probe, не гадать**.
2. **gate_action = МЕТОД `SynapseHost`**, не build_host-closure (отклонение от плана ради
   тестируемости). `build_host` его не переопределяет; роут/инструмент зовут `host.gate_action(...)`.
3. **`_on_run_finished` — holder-паттерн** (как on_speak): kora_runner строится до host, замыкание
   читает `_h["host"]` в runtime.
4. **Коммиты:** короткие lowercase, по-человечески; **НИКАКОЙ AI-атрибуции**; без `feat:`/`fix:`.
5. **CSRF `_csrf_ok`** обязателен на новых мутирующих роутах; **XSS: только textContent, 0 innerHTML.**
6. **Живой сервер 7860 НЕ рестартовать** без слова Теро. Проверки — staging 7861 / тесты.

## Где остановились → что дальше

**Task 4 (HTTP-гейт) — СЛЕДУЮЩИЙ.** Файлы: `synapse/pipeline/webrtc_server.py`, `tests/test_stages.py`.
- `_csrf_ok` уже на :420 (багфикс усилил — требует Origin/Referer).
- `_thread_dict` на :431 — добавь ТОЛЬКО `request_text`/`last_model`/`archived` (stage там уже есть).
- Роуты `api_thread_get`/`api_thread_gate` ДО `app.mount` (:523).
- gate → HTTP: busy=409, invalid_model/confirm_required/no_plan_file/stale_plan/illegal_stage=400,
  unknown_thread=404. Шаблон теста: `FakeRequest`+`_endpoint` из `test_text_turn.py`.

**Task 5–12 — по плану, строго по Steps.**

## Нюансы, добытые в работе (не в плане)

- `FakeClock` — это `synapse.clock.FakeClock`, НЕ `Clock.fixed` (такого нет).
- `asyncio_mode = "auto"` в `pyproject.toml` — async-тесты запускаются автоматически; НЕ звать
  `asyncio.get_event_loop().run_until_complete` в sync-тесте (Python 3.14 кидает RuntimeError).
- `cfg.kora_workspace_dir` по умолчанию `None` — тестовый cfg обязан его задать, иначе `Path(None)`
  падает в write_code/`_resolve_root_for`.
- `itertools` надо импортировать в app.py (`_GATE_TASK_SEQ = itertools.count(1)`).
- `docs/plans/*.md` с hex-именами — тестовые артефакты прогонов `test_write_code_*` (не Commit'ить).

## Parking lot (пополнить в конце плана)

- Голосовой системный промпт — НОВОЕ, load-bearing (Task 5 Step 3b).
- b13 contextvar-latch — незавершённость багфикса (чинится отдельно).
- B-UX / B-PIPE / race audits (`test_bugs_audit.py`/`test_b_pipe_bugs.py`/`test_concurrency_race.py`)
  — намеренно-красные, фиксируют найденные Теро баги для будущей работы.
