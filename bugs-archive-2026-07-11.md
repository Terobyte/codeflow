# Synapse — список багов

Ревью от 2026-07-11 (параллельный прогон 3 ревью-агентов: webrtc_server / app / tests+config).
Тест-сьют зелёный: **91 passed, 16 warnings in 6.03s** (`.venv/bin/pytest -q`).

Предыдущий список (2026-07-10) почти полностью закрыт коммитами `85c20ef`, `07db99e`,
`fd7e9a8`, `8d04f32`, `d83f1cb`. Ниже — что осталось и что нашлось заново, по убыванию
серьёзности.

---

## ✅ Закрыто из старого списка (для истории)

- Баг 1 (дубль-алерты `check()`) — `85c20ef`
- Баг 2 (Р-15г мёртв в голосе) — задокументирован как гэп + тест
  `test_voice_pipeline_speak_ledger_gap`
- Баг 3 (cascade events не подключены) — `07db99e` подключил события к журналу;
  остаток закодифицирован тестом `test_pipeline_cascade_events_not_wired_to_journal`
- Баг 4 (`on_tier3` → `on_tail_tier`) — `07db99e`
- Баг 5 (потеря `retry_after`) — `85c20ef`
- Баг 6 (`set_task_status` перезаписывает терминальный) — `85c20ef`
- Замечание 7 (`DeprecationWarning` по `model=`) — **ещё живо**, см. Баг 7 ниже

---

## 🔴 Баг 1 — `monitor_forever()` умирает навсегда от любого исключения

**Где:** `synapse/pipeline/app.py:58-66` (тело цикла); запуск `synapse/pipeline/webrtc_server.py:62`,
teardown `webrtc_server.py:66`

Цикл `while True` без `try/except`. Любой разовый сбой `journal.alert()` (`os.fsync()`),
`store.liveness()` (запись `state.json`) или `AlertKind(kind)` пробрасывается наружу и
убивает фоновый таск до конца сессии. Таск запущен через `asyncio.ensure_future(...)` и
в `finally` только `.cancel()`-ится — исключение никто не логирует
(`Task exception was never retrieved`).

Следствие: после одного транзиентного I/O все Р-15г («critical ⇒ paired SPEAK») и
Р-11 (liveness) проверки тихо перестают работать до перезапуска процесса, без какого-либо
сигнала.

**Почему тесты не ловят:** `monitor_forever` не покрыт тестами на отказ; ни один вызов
внутри цикла не мокаится на выброс исключения.

**Фикс:**

```python
# app.py — тело цикла
while True:
    try:
        ...  # speak_ledger.check / journal.alert / store.liveness
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("monitor iteration failed")
    await asyncio.sleep(heartbeat_interval_s)
```

```python
# webrtc_server.py — finally
finally:
    monitor.cancel()
    try:
        await monitor
    except asyncio.CancelledError:
        pass
```

---

## 🟡 Баг 2 — SPEAK из `on_speak` может застрять в очереди политики без `TextFrame`

**Где:** `synapse/pipeline/app.py:92` (`arbiter_policy.push_speak(text)`),
`synapse/arbiter.py:100-113` (flush только на `TextFrame`/`TTSSpeakFrame`)

`on_speak` мутирует очередь `ArbiterPolicy`, но не пушит фрейм. `TTSArbiterProcessor`
дрейнит очередь только внутри `process_frame` на `TextFrame`/`TTSSpeakFrame`; на
`LLMFullResponseEndFrame` и прочих срабатывает ветка `else` → `push_frame` без `_drain()`.

На pure-tool-call терне (где downstream-текста нет) приоритетный SPEAK сидит в очереди
до следующего терна с текстом — задержка operational readback (Р-5) произвольна. В
консольном раннере есть явный `drain_all()` сразу после `push_speak`; в live-пути — нет.

**Почему тесты не ловят:** арбитр тестируется на потоке `TextFrame`; сценарий
«`push_speak` без последующего `TextFrame`» не покрыт.

**Фикс:** дрейнить на контрольных фреймах конца ответа (`LLMFullResponseEndFrame`),
либо прокидывать SPEAK через `push_frame(TTSSpeakFrame(text), DOWNSTREAM)` вместо прямой
мутации очереди.

---

## 🟡 Баг 3 — `active_sessions` растёт без bound (утечка памяти)

**Где:** `synapse/pipeline/webrtc_server.py:45` (декларация), `:85` (запись),
`:95` (чтение)

Словарь только пишется (`active_sessions[session_id] = data.get("body", {})` при каждом
`POST /start`, а RTVI-клиент зовёт `/start` на каждый коннект/рефреш вкладки) и никогда
не чистится — нет `del`/`pop`, нет TTL. Значение при этом нигде не читается
(проверяется только факт наличия ключа). Для долго живущего uvicorn — монотонная
утечка, каждая запись — чистый мусор.

**Фикс:** `active_sessions.pop(session_id, None)` после успешного offer и/или в `finally`
`run_session`. Либо выкинуть структуру совсем, если нужен только readiness-флаг с TTL.

---

## 🟡 Баг 4 — `opencv-python-headless>=5` конфликтует с `pipecat-ai[webrtc]`

**Где:** `pyproject.toml:20` (`voice` extra)

`voice` extra пинит `opencv-python-headless>=5`. При этом `pipecat-ai[webrtc]` требует
`opencv-python<5,>=4.11.0.86`. `opencv-python` и `opencv-python-headless` — разные
дистрибуции, ставящие один и тот же `cv2`-неймспейс; одновременно владеть им может
только одна.

Сейчас резолвер случайно ставит `opencv-python-headless 5.0.0.93` (потому что `voice`
перечисляет её явно, а `[webrtc]` никто не тянет). Но любой потребитель `[webrtc]` получит
конфликт неймспейса, плюс это major-версия opencv, на которой WebRTC-транспорт не
валидировался.

**Фикс:** `opencv-python-headless>=4.11,<5`, либо убрать явную запись и зависеть от
`pipecat-ai[webrtc]` (он тянет совместимые aiortc + opencv).

---

## 🟢 Баг 5 — Дескриптор файла `TurnJournal` не закрывается в WebRTC-пути

**Где:** `synapse/pipeline/app.py:75` (создание), `synapse/pipeline/webrtc_server.py:47-66`
(жизненный цикл сессии)

`TurnJournal.__init__` открывает файл (`self._path.open("a", ...)`). `console.py` зовёт
`journal.close()`; live-путь — никогда. Каждая WebRTC-сессия течёт одним fd, при
многократных реконнектах упрётся в лимит fd. R2 (durability) держится построчно, но
финальный частичный буфер перед крашем не флашится.

**Фикс:** `voice_pipeline.journal.close()` в `finally` `run_session` (после
`monitor.cancel()`/`await monitor`).

---

## 🟢 Баг 6 — Конкурентный доступ к `TurnJournal` без синхронизации (хрупко)

**Где:** `synapse/pipeline/app.py:61-66` (`monitor_forever`), `:110-124`
(`@strategy.event_handler(...)`)

Каскадные event-хендлеры pipecat запускает как fire-and-forget `asyncio.create_task`,
`monitor_forever` — отдельный таск, и всё это пишет в тот же `TurnJournal`, который
мутирует и flow терна (`begin_turn`/`record_tool_call`/`end_turn`).

Сейчас безопасно **только случайно**: все методы `TurnJournal` полностью синхронны (без
внутреннего `await`), поэтому методы не interleav'ятся на середине. Но:

- `journal.alert()` из фоновой таски может выполниться после `end_turn()`, уже
  обнулившего `self._current` → alert привяжется к `turn_id=None`, теряется связка
  alert↔turn для `on_retry`/`on_tail_tier`;
- любое будущее добавление `await` внутрь метода журнала молча внесёт реальную гонку
  данных; `retry=True` в `_journal_retry` гоняется с `end_turn()`, читающим
  `asdict(self._current)`.

**Фикс:** явно задокументировать инвариант «все методы журнала остаются синхронными»,
либо захватывать turn по значению (id), а не мутировать `journal.current` из
несинхронизированной фоновой таски.

---

## 🟢 Баг 7 — `DeprecationWarning: model=` по всему сьюту (бывшее Замечание 7)

**Где:** `synapse/cascade/services.py:36` (`AnthropicLLMService(..., model=...)`),
`synapse/pipeline/app.py:126` (`DeepgramFluxSTTService(..., model=...)`),
`tests/test_tools.py:104,122` (`OpenAILLMService(..., model=...)`)

pipecat 1.5 депрекейтит `model=` в пользу `settings=Service.Settings(model=...)`.
16 варнингов на прогон. Не критично (работает), но код помечен к удалению в будущей
версии pipecat; лучше переехать сейчас, пока разброс по файлам небольшой.

**Фикс:** везде `settings=Service.Settings(model=cfg.xxx_model)`.

---

## 🟢 Баг 8 — `av` ↔ `cv2` дублирующие нативные `libavdevice` dylibs (macOS)

**Где:** побочный эффект `pyproject.toml:20` (`voice` extra тянет и aiortc→`av`, и
headless cv2)

aiortc→`av` и `opencv-python-headless` тянут разные мажорные FFmpeg `libavdevice` dylib
(61 vs 62) → objc-варнинг `Class AVFFrameReceiver is implemented in both …libavdevice.61…
(av) and …libavdevice.62… (cv2)`, с прямым указанием на возможные «spurious casting
failures and mysterious crashes».

**Фикс:** prefer cv2-сборку без бандлинга ffmpeg; документировать, что `av` из aiortc
должен победить. Зависит от фикса Бага 4.

---

## Порядок починки (рекомендация)

1. **Баг 1** — монитор молча умирает → реальная потеря safety-инвариантов в проде.
   Самый высокий приоритет, фикс локальный.
2. **Баг 2** — зависающий SPEAK → пользовательски наблюдаемый glitch.
3. **Баг 3 + Баг 4** — утечки (память / зависимость). Дёшево, делать вместе.
4. **Баг 5** — закрытие journal fd.
5. **Баг 6** — задокументировать инвариант или захват turn по значению.
6. **Баг 7** — переезд на `settings=`.
7. **Баг 8** — следствие Бага 4.
