# Архив — Синапс UI v2, слайсы UI-4/UI-5 (Task 1–11 сданы)

Дата: 2026-07-14 · HEAD `dff3478` · живой план: `2026-07-13-synapse-ui-v2-slices-4-5.md`
(в нём остался только Task 7 Step 3–4 и Task 12) · спека: `2026-07-13-synapse-ui-v2-design.md`

> Заменяет собой `2026-07-13-ui4-handoff-archive.md` (тот фиксировал только Task 1–3
> на середине пути) — здесь весь путь Task 1–11 целиком, обе слайса UI-4 и UI-5.

## Что сдано

### СЛАЙС UI-4 «стадии»

| Task | Коммит | Файлы | Что |
|------|--------|-------|-----|
| 1. FSM треда в ThreadStore | `ace68e6` | `threads.py`, `test_stages.py` | `_STAGE_TRANSITIONS`, `set_stage`/`set_request`/`set_last_model`/`bind_project`; поля `request_text`/`last_model`/`archived`; guard находки F (нельзя перепривязать проект после первого запуска) |
| 2. Гейт-режим `docs_only` | `24a307c` | `bridge/kora.py`, `test_stages.py` | Четвёртый слот снапшота `_run_gate_mode`; `_gate_decision` сужает мутации до `docs/` + top-level `*.md`, ПОСЛЕ секрет-денилиста |
| 3. `gate_action` в build_host | `275fcb0` | `pipeline/app.py`, `test_stages.py` | Единая хост-функция `gate_action(thread_id, action, model, confirm)`: revise/send_to_kora/write_code, per-thread lock, busy-чек ДО `set_stage`, план-файл + `last_outcome=="completed"` гейт против stale-плана, модельный allowlist |
| — (housekeeping) | `0900704` | план-файл, `ui4-handoff-archive.md` | Чекбоксы Task 1–3, первый handoff-архив (теперь поглощён этим файлом) |
| 4. HTTP-гейт и API стадий | `e1b7e55` | `pipeline/webrtc_server.py`, `test_stages.py` | `POST /api/threads/{id}/gate` (CSRF, busy→409, invalid_model/confirm_required/no_plan_file/stale_plan/illegal_stage→400), `GET /api/threads/{id}` расширенный `_thread_dict` |
| 5. Инструменты диспетчера + стадийный промпт | `1f1c046`¹ | `dispatcher/tools.py`, `prompt.py`, `dispatcher/loop.py`, `pipeline/app.py` | `propose_request`/`gate_action`/`bind_project` инструменты; `STAGE_RULES_COLLECT/PROPOSE`; голосовой системный промпт (факт 15, Step 3b — сделано, НЕ descope); `_complete(history, thread_id)` |
| 6. UI стадий — чип, gate-карточки, модель | `1f1c046`¹ | `pipeline/client/app.js/index.html/style.css` | `#stage-chip`, gate_card-ветка в `addEntry` с кнопками+select модели+двухфазный тап, чип в `threadCard` сайдбара |
| 7. Смоук UI-4 + DoD (Step 1–2 из 4) | `1f1c046`¹ | — | Живой прогон на staging 7861: стадийный путь collect→propose→docs_only-SPEC_PLAN→CODE→done, быстрый путь и отказы (400/409). **Проверено live 2026-07-14.** Попутно закрыт b13 (contextvar-latch/`asyncio.run`, см. `ui4-handoff-archive.md`). |

¹ Task 5, 6 и 7(Step 1–2) вошли одним коммитом `1f1c046: ui-4: activity staging and b13 bux audit fixes` — исполнитель не резал по границам плана, все три Steps проверены той же прогонённой сюитой.

### СЛАЙС UI-5 «гигиена»

| Task | Коммит | Файлы | Что |
|------|--------|-------|-----|
| 8. Чистый контекст нового треда | `364988a` | `test_hygiene.py` | Формальный якорь: два треда через один `DispatcherTurnLoop` не делят историю; регидрация холодного треда не тащит `gate_card`/`event`/кора-kinds; `[СОСТОЯНИЕ]` остаётся глобальным |
| 9. Компакт длинного треда (S10) | `6b22c8f` | `dispatcher/loop.py`, `config.py`, `pipeline/app.py` | `dispatcher_compact_after=40`; `_maybe_compact` режет историю ТОЛЬКО по целым tool-группам (cut продвигается вперёд до ближайшего `role:"user"`), in-place мутация `self._histories[thread_id]` (не ребинд); `on_compact` → `event` в ленту |
| 10. Заголовки: авто-title и rename (S30) | `2343810` | `threads.py`, `webrtc_server.py`, `pipeline/app.py`, `app.js` | `maybe_autotitle` только для сентинеля `"новый тред"` (композерные треды); `PATCH /api/threads/{id}` rename; тап по `#view-title` → `prompt()` |
| 11. Архив тредов и удаление проекта (S31) | `dff3478` | `threads.py`, `projects.py`, `webrtc_server.py`, `app.js`, `test_hygiene.py` | `set_archived`/`list(include_archived)`; `unbind_project` (треды переживают удаление проекта, только теряют `project_id` + событие в ленте); `projects.remove`; роуты `POST /api/threads/{id}/archive` (per-thread busy-чек, НЕ глобальный), `DELETE /api/projects/{id}`; кнопки «архив»/«×» в UI с `confirm()` |
| — (сопутствующее) | `5f0558e` | `test_kora_status_ui.py`, `test_ui_client.py` | Добить тестовое покрытие уже отгруженного ранее (thread-контекст в `kora_status`, `resizeMessageInput`/`thread_stage` в лексическом тесте) — найдено при прогоне полной сюиты перед Task 11 |

## Отдельный коммит (не из плана, шёл параллельно)

`500cfd5` — bugfix layer: робастность classify google-429; `contextvars` dedup-latch + LRU в
tools.py; `OrderedDict` `_histories` LRU в loop.py; webrtc monitor lifecycle. Дал регресс b13
(починен внутри `1f1c046`, см. выше).

## Тестовая база на момент архивации

`.venv/bin/python -m pytest tests/ -q --ignore=tests/test_hunt0714_a.py --ignore=tests/test_hunt0714_b.py --ignore=tests/test_b_pipe_bugs.py --ignore=tests/test_concurrency_race.py`
→ **450 passed**. Игнор-лист — это отдельный, ещё не сданный багхант 2026-07-14 (B01–B14,
намеренно-красные находки), не относится к UI-4/UI-5.

## Принятые решения (действовали на Task 1–11, продолжают действовать)

1. **Task 5 Step 3b (голосовой системный промпт) выбран, НЕ descope** — голосовой
   `LLMContext` теперь получает `build_system_prompt(cfg, task_dictionary, stage_block=...)`,
   освежается на каждый ход. Это разблокировало голосовой live-DoD (Task 7/12) — он больше
   не «липовый».
2. `gate_action` — метод `SynapseHost`, не closure (тестируемость).
3. `_on_run_finished`/`on_compact` — holder-паттерн, как `on_speak`.
4. Коммиты: короткие lowercase, по-человечески, без AI-атрибуции, без `feat:`/`fix:`.
5. CSRF `_csrf_ok` — на всех новых мутирующих роутах (gate/archive/patch/delete). XSS —
   только `textContent`/`el()`, ноль `innerHTML`.
6. Живой сервер 7860 не рестартовался без слова Тео на всём протяжении Task 1–11 —
   проверки шли через staging 7861 / полную сюиту.
7. «Живой тред» для 409-детекта — **per-thread**, не глобальный `has_active_task()`
   (иначе архив одного треда ложно блокируется занятостью другого).

## Нюансы, добытые в работе

- `FakeClock` — `synapse.clock.FakeClock`, не `Clock.fixed`.
- `asyncio_mode = "auto"` в `pyproject.toml` — не звать `run_until_complete` в sync-тесте.
- `cfg.kora_workspace_dir` по умолчанию `None` — тестовый cfg обязан задавать явно.
- Компакт: `_history_for` отдаёт живую ссылку на `self._histories[thread_id]` — ребинд
  локальной переменной не долетает до следующего хода, нужна in-place мутация.
- thread.js/thread.html удалены (`7424c3e`) — весь UI живёт в SPA-шелле `index.html`+`app.js`.

## Что НЕ вошло в архив (осталось в живом плане)

- **Task 7 Step 3–4**: live-DoD с телефона (Тео, голос) + финальный коммит хвостов.
- **Task 12**: staging-прогон гигиены целиком + live-DoD рестарта живого сервера + закрытие
  плана (память, чекбоксы, parking lot).
- Parking lot — не изменился, актуален.
