# Синапс · Настройки AI — провайдеры, модели, ручная маршрутизация

Дата: 2026-07-14.

Статус: **v4 — исправлена по результатам аудита против кода**. v2 порезан: аудит
сверил каждый раздел с реальным кодом и с Фазой 0 (`docs/superpowers/plans/
2026-07-14-synapse-dispatcher-kora-phase0.md`), которая уже строит половину
старых AI-0/AI-2 под другими именами.

Область: AI диспетчера и модель Коры. STT, TTS, голос и права Коры не
перенастраиваются этой фичей.

## 0. Что изменилось против v2

Вырезано (с причиной):

| Было в v2 | Почему вырезано |
| --- | --- |
| AI-5: умная маршрутизация, 5 приоритетов | Синапс — один пользователь. Нет трафика для p50-статистики задержек; `quality_score`-каталог — ручная беговая дорожка («качество выглядит объективным» — риск самого v2); min-max-нормализация по динамическому набору кандидатов нестабильна: новый кандидат перемасштабирует всех и может перевернуть победителя — это тихо воюет с собственным требованием детерминизма §4.3 v2 |
| Формула баланса + `formula_version` | Умирает вместе с приоритетами |
| Keychain SecretStore, secret-endpoints, редактирование ключей из UI | Фоновый python-процесс + Keychain = entitlement/prompt-боль; v2 сам признавал, что ключи из UI нельзя выпускать до настоящей authn. Ключи остаются в `.env`; смена ключа — событие частоты рестарта |
| OpenAI как провайдер | Кода нет, потребности не заявлено; OpenRouter уже мета-провайдер поверх моделей OpenAI/Google — v2 этим фактом не пользовался |
| Ollama в v1 | Жёсткое требование диспетчера — tool calling; сперва доказать, что локальная модель его тянет, потом рисовать UI. Выделен в отдельный слайс приватности (AI-4), не в acceptance v1 |
| `max_request_cost_usd`, цены в каталоге | Существующий `CostCap` считает ВЫЗОВЫ, не доллары (`synapse/cascade/services.py:56`); долларовый учёт требует пер-провайдерного token accounting, чей единственный потребитель — вырезанный cost-приоритет |
| Tombstone-механика каталога моделей («удалённая модель видима как недоступна») | Сжато до: проксировать список провайдера по требованию; при сбое — последний известный + ручной `model_id` |

Добавлено (v2 это упускал):

| Добавка | Почему несущая |
| --- | --- |
| Явная зависимость от Фазы 0 (С4/С5) | В рабочем дереве готова только Task 4.1 С4 (`GuardedLLMClient` — общий cost cap, нормализация сбоев). Task 4.2 (роутовый catch с ответом `200 + degraded: true`) и С5 (bearer authn) — обязательные пререквизиты AI-0. Старые AI-0/AI-2 описывали ту же работу как новую |
| Порядок обёрток текстового канала | Единственное важнейшее архитектурное предложение, которого не было в v2: cost cap живёт СНАРУЖИ выбора провайдера — платная попытка считается независимо от того, кто её принял |
| Breaker: владение и пересборка | Сегодня `CircuitBreaker(len(tiers))` строится один раз на старте процесса и индексируется ПОЗИЦИЕЙ tier-а (`synapse/pipeline/app.py:815`); конфигурируемые маршруты ломают оба допущения. Самый рискованный рефактор фичи — v2 упоминал ключ `(provider, model)` одной строкой без владельца |
| Карта LLM-потребителей | `speakify.py` (родился 2026-07-14) ходит в Gemini напрямую и НАМЕРЕННО мимо каскада — раздельная квота и есть его смысл. Спека обязана говорить, кто маршрутизируется, а кто pinned |
| Вход в экран настроек | Роут `#/settings/ai` был, а шестерёнки в сайдбаре не было |
| Тестовые швы | Конвенция репо — `httpx.MockTransport` через DI (`llm_client.py`, `speakify.py`); адаптеры обязаны её держать |

## 1. Зачем это нужно

AI-конфигурация зашита в `.env` и собирается при старте: голос — каскад
OpenRouter→Anthropic (`synapse/cascade/services.py:36-39`), текст — только
Anthropic на `tier2_model` (`synapse/pipeline/app.py:794`), Кора — Claude Agent
SDK со своим выбором модели. Пользователь не может из интерфейса выбрать
провайдера/модель, увидеть кто реально ответил, проверить ключ без рестарта.

Цель — экран `Настройки → AI`, сохранив разделение ролей:

1. **Диспетчер** разговаривает с пользователем; его маршрут (primary + fallback)
   настраивается и читается голосом и текстом из одного snapshot-а.
2. **Кора** исполняет задачи через Claude Agent SDK; её модель настраивается
   отдельно и в маршрутизации диспетчера не участвует.

## 2. Зафиксированные продуктовые решения

1. Провайдеры v1: **OpenRouter, Anthropic, Google AI Studio**. Ollama — слайс
   приватности AI-4 (вне acceptance v1). OpenAI — вне спеки, пока нет причины.
2. Модель — подпункт провайдера; хранится парой `(provider_id, model_id)`.
3. Голосовой и текстовый диспетчер читают одну конфигурацию маршрутизации.
4. Маршрутизация v1 — **только ручная**: основной маршрут + упорядоченный
   резерв. Никакого автоматического выбора модели.
5. Ключи живут только в `.env`. UI показывает `configured / source / маску`,
   read-only. Редактирование ключей из UI — вне v1 целиком.
6. Кора — отдельный блок; провайдер фиксирован: Claude Agent SDK.
7. Snapshot-семантика: текст — на начало хода; голос — на сборку сеанса
   (`build_session_pipeline`); Кора — `RunSpec` на запуск. Живой ход/сеанс/ран
   не переключается скрытно.
8. Authn control plane — **зависимость от Фазы 0 С5** (bearer-токен,
   fail-closed), не работа этой спеки.
9. API-ключи никогда не возвращаются клиенту и не пишутся в JSON/журнал/ленту.

## 3. Экран настроек

Вход: кнопка-шестерёнка в футере сайдбара (сегодня отсутствует — добавить).
Новый SPA-маршрут `#/settings/ai` в существующем hash-роутере.

```text
Настройки AI
│
├── Диспетчер
│   ├── Провайдеры
│   │   ├── OpenRouter      (ключ: статус read-only · модель · проверить)
│   │   ├── Anthropic       (ключ: статус read-only · модель · проверить)
│   │   └── Google AI Studio(ключ: статус read-only · модель · проверить)
│   │
│   └── Маршрут
│       ├── Основной: провайдер + модель
│       └── Резервный: провайдер + модель (необязателен)
│
└── Кора
    ├── Провайдер: Claude Agent SDK (read-only)
    ├── Модель по умолчанию (allowlist сервера)
    └── Ограничения запуска: max turns · бюджет · deadline
```

### 3.1. Карточка провайдера

- название; переключатель `Включён`;
- статус ключа: `настроен (env) · sk-…7f2a` — read-only, без кнопок правки;
- селектор модели: список с провайдерского API + пункт `Указать model_id
  вручную`; при недоступном API — последний успешный список с меткой времени;
- кнопка `Проверить подключение` — минимальный запрос на выбранной модели, без
  пользовательского контекста и без tools;
- краткая ошибка последней проверки.

Состояния: `не настроен` (нет ключа) · `не проверен` · `подключён` ·
`ошибка ключа` (401/403) · `недоступен` (timeout/5xx) · `ограничен` (429).
Ошибка одного провайдера не блокирует сохранение и работу остальных.

### 3.2. Блок Коры

- модель по умолчанию — из серверного allowlist `_KORA_MODELS`
  (`synapse/pipeline/app.py:57`), который отдаётся через API; клиентская копия
  `KORA_MODELS` (`synapse/pipeline/client/app.js:161`) **умирает**;
- `max turns`, бюджет запуска (USD), deadline (сек). `kora_max_budget_usd` —
  существующий лимит одного `RunSpec`; он не связан с вырезанным в §0
  диспетчерским `max_request_cost_usd` и per-request dollar accounting;
- применение: новое значение — дефолт следующего запуска; выбор модели в
  gate-карточке треда переопределяет дефолт на один запуск (уже работает через
  `RunSpec.model`); `RunSpec` — immutable snapshot; активный ран не трогается.

## 4. Маршрутизация

### 4.1. Ручной маршрут

Пользователь выбирает основную пару `(provider, model)` и необязательную
резервную. Каждый ход начинается с основной. Дефолт после миграции — текущий
каскад: OpenRouter `google/gemini-3.5-flash` → Anthropic `claude-haiku-4-5`.
Primary и fallback обязаны ссылаться на разные `provider_id`; два вызова одного
провайдера с разными моделями в одном LLM-pass в v1 запрещены и отклоняются API.

### 4.2. Fallback-политика (наследуется из v2 — здоровая часть)

Переключение на резерв разрешено для: timeout, connection error, HTTP 429,
HTTP 500–599. Запрещено для: некорректного запроса (400), schema/tool contract
error, отказа политики безопасности, исчерпанного дневного лимита
(`CostCapBlocked` не fallback-ится в платный резерв). 401/403 помечает
провайдера `ошибка ключа` и не ретраится на нём. Один провайдер вызывается не
более одного раза на один LLM-pass. Выбранный маршрут фиксируется на ход;
параллельно сохранённые настройки применяются со следующего.

## 5. Карта LLM-потребителей

Все места, где Синапс зовёт LLM, и их отношение к маршруту:

| Потребитель | Сегодняшний код | Маршрутизируется? |
| --- | --- | --- |
| Голосовой диспетчер | pipecat-каскад, `build_session_pipeline` (`app.py:889`) | **да** — snapshot на сборку сеанса |
| Текстовый диспетчер | `GuardedLLMClient(AnthropicLLMClient)` (`app.py:794`) | **да** — snapshot на начало хода |
| Компакт истории диспетчера | тот же текстовый клиент (`dispatcher_compact_after`) | **да**, автоматически — едет на клиенте хода |
| speakify (озвучка ленты) | direct Gemini (`synapse/dispatcher/speakify.py`) | **нет — pinned.** Раздельная квота от каскада и есть его смысл; маршрутизация его бы уничтожила. Если маршрут когда-нибудь поедет на direct Gemini — пересмотреть отдельно |
| Кора | Claude Agent SDK, `RunSpec.model` | **нет** — отдельный блок настроек, не шестой провайдер |

## 6. Целевая архитектура

```text
            ┌─────────────────────┐
            │  Настройки → AI UI  │  (#/settings/ai, шестерёнка в сайдбаре)
            └──────────┬──────────┘
                       │ Settings API (authn С5 + CSRF)
            ┌──────────▼──────────┐
            │   AISettingsStore   │  ai-settings.json · revision CAS · atomic
            └──────────┬──────────┘
                       │ immutable snapshot
        ┌──────────────┼───────────────────┐
        │              │                   │
┌───────▼────────┐ ┌───▼───────────────┐ ┌─▼──────────────────┐
│ build_session_ │ │ RoutedLLMClient   │ │ Kora settings      │
│ pipeline(voice)│ │ (text, за Guarded)│ │ → RunSpec на launch│
└───────┬────────┘ └───┬───────────────┘ └────────────────────┘
        │              │
   ┌────▼──────────────▼────┐
   │    ProviderRegistry    │  адаптер = text tools + voice service
   ├────────────────────────┤
   │ OpenRouter · Anthropic │
   │ · Google AI Studio     │
   └────────────────────────┘
```

### 6.1. `AISettingsStore`

Несекретные данные в `<journal_dir>/ai-settings.json`:

```json
{
  "revision": 4,
  "providers": {
    "openrouter": {"enabled": true, "selected_model": "google/gemini-3.5-flash"},
    "anthropic":  {"enabled": true, "selected_model": "claude-haiku-4-5"},
    "google":     {"enabled": false, "selected_model": null}
  },
  "routing": {
    "primary":  {"provider": "openrouter", "model": "google/gemini-3.5-flash"},
    "fallback": {"provider": "anthropic",  "model": "claude-haiku-4-5"}
  },
  "kora": {
    "default_model": "claude-sonnet-5",
    "max_turns": 40,
    "max_budget_usd": 1.0,
    "deadline_s": 900
  }
}
```

Запись атомарная: временный файл в той же директории → `os.replace`. Каждый
update несёт ожидаемую `revision`; конфликт → 409 (телефон + ноутбук — реальный
сценарий двух клиентов). Ключей в файле нет по построению — нечего утекать.

`providers[id].selected_model` — дефолт карточки провайдера для селектора и
`/test`; `routing.primary.model` и `routing.fallback.model` — модели, реально
используемые в ходе. Эти значения могут законно расходиться.

### 6.2. Ключи

Только ключи читаются из `.env` через `SynapseConfig.from_env()` — как сегодня.
`tier1_model`, `tier2_model` и `speakify_model` из env сейчас не читаются: их
исходные значения — dataclass defaults в `SynapseConfig`. `GET /api/settings/ai`
отдаёт по провайдеру: `configured: bool`, `source: "env" | "none"`, необратимую
маску (`sk-…7f2a`). Ничего мутирующего. Смена ключа = правка `.env` + рестарт —
честно и названо в UI подсказкой.

### 6.3. Порядок обёрток текстового канала

Task 4.1 Фазы 0 С4 уже создала шов
`GuardedLLMClient → AnthropicLLMClient`
(`synapse/dispatcher/llm_client.py:110`). Task 4.2 ещё обязана научить роут ловить
`CostCapBlocked`/`ProviderUnavailable` и возвращать детерминированную degraded-
реплику; это пререквизит AI-0, не работа AI-2. После обеих задач С4 маршрутизация
встраивается так:

```text
роут (webrtc_server: ловит CostCapBlocked / ProviderUnavailable → degraded-реплика)
  └─ RoutedLLMClient          — на начало хода берёт snapshot, строит цепочку
       ├─ GuardedLLMClient(адаптер primary)   — cost cap + нормализация сбоев
       └─ GuardedLLMClient(адаптер fallback)  — при ProviderUnavailable primary
```

Исчерпанная цепочка пере-бросает `ProviderUnavailable` последнего адаптера —
новых типов исключений роут не учит. Реализованный как пререквизит AI-0 в
Task 4.2 catch С4 переиспользуется без дополнительных правок в AI-2.

Инварианты:

- **cost cap снаружи выбора провайдера**: каждая платная попытка резервируется
  до сетевого вызова независимо от того, какой провайдер её принял (семантика
  С4 сохраняется дословно);
- `CostCapBlocked` обрывает цепочку — платный резерв после лимита запрещён;
- `ProviderUnavailable` от primary → одна попытка fallback → если и он упал,
  роут отдаёт degraded-реплику (200 + `degraded: true`) через обязательный catch
  Task 4.2 С4;
- полиморфизм `LLMClient.complete()` сохраняется — loop и консоль разницы не видят.

### 6.4. Голосовой канал

`build_session_pipeline` (`app.py:889`) уже пересобирает pipecat-сервисы на
каждый reconnect — snapshot читается там, вместо жёсткой пары из
`build_tier_services(cfg)`. Существующая машинерия каскада (LLMSwitcher,
strategy, Р-14 failover) не меняется — меняется только источник списка
сервисов: порядок = `[primary, fallback]` из snapshot-а.

### 6.5. Breaker — самый рискованный рефактор фичи

Сегодня: `CircuitBreaker(len(_tier_probe), …)` строится один раз в `build_host`
(`app.py:815`), живёт на хосте, индексируется позицией tier-а. Конфигурируемые
маршруты ломают оба допущения (число маршрутов меняется на лету; «позиция 0»
после смены настроек — другая модель).

Целевое состояние: breaker хранит состояние по ключу `(provider_id, model_id)`,
регистрирует ключ лениво при первом использовании, живёт на хосте и **один на
оба канала** — голос, затриповавший OpenRouter, обязан быть виден тексту, иначе
текст продолжит долбить мёртвый маршрут. Cooldown/RPM-окна (`rpm_mute_s`,
`rpd_reset_hour_utc`) сохраняют текущую семантику.

### 6.6. `ProviderRegistry`

Описание провайдера + фабрики двух адаптеров: text completion с tools и
voice LLM service (pipecat). Провайдерская специфика не выходит за адаптер.

| Провайдер | Text-адаптер | Voice-адаптер | Источник моделей |
| --- | --- | --- | --- |
| OpenRouter | OpenAI-совместимый httpx-клиент (паттерн `llm_client.py`) | `OpenRouterLLMService` (есть) | API моделей OpenRouter |
| Anthropic | `AnthropicLLMClient` (есть) | `AnthropicLLMService` (есть) | `/v1/models` |
| Google AI Studio | direct Gemini httpx-клиент (паттерн `speakify.py`, + tools) | pipecat Google service | API моделей Google |

`ModelInfo` сжат до необходимого: `provider_id, model_id, display_name,
supports_tools, available`. Цен, quality- и latency-оценок нет — их потребители
вырезаны.

Каждый text-адаптер нормализует провайдерский ответ в общий completion/tool
shape и при нарушении этого shape поднимает отдельный `ProviderContractError`.
`GuardedLLMClient` переводит в `ProviderUnavailable` только разрешённые §4.2
транзиентные сетевые/HTTP-сбои и пропускает `ProviderContractError` без
переклассификации, поэтому contract error не запускает fallback.

Тестовый шов: каждый адаптер принимает `transport=` (httpx.MockTransport DI) —
конвенция репо (`llm_client.py:73`, `speakify.py:29`). Живое сравнение
провайдеров — существующий `tools/bench_llm_providers.py`.

## 7. Встраивание в текущий код

1. `SynapseConfig` остаётся immutable bootstrap (пути, порты, стартовая
   миграция из bootstrap-конфига). Runtime-код настроек читает только snapshot-ы
   store-а.
2. **Миграция первого старта**: нет `ai-settings.json` → создать из bootstrap-
   конфига: primary = OpenRouter/`tier1_model`, fallback =
   Anthropic/`tier2_model`. Эти две модели сегодня берутся из dataclass defaults,
   а не из env. Параметры Коры (`kora_model`/`kora_max_turns`/
   `kora_max_budget_usd`/`kora_deadline_s`) допускают существующие env-overrides
   через `from_env()`. `.env` не изменяется → rollback на старую версию безопасен.
   После AI-2 `tier1_model`/`tier2_model` не управляют runtime-маршрутом, но
   остаются migration seed и rollback-якорем v1; `speakify_model` продолжает
   использоваться pinned-путём §5. Удалять эти поля в v1 не надо.
3. `build_session_pipeline` (`app.py:889`): сервисы из Registry по snapshot-у
   вместо `build_tier_services(cfg)`.
4. Текстовый канал (`app.py:794`): `RoutedLLMClient` вместо прямого
   `GuardedLLMClient(AnthropicLLMClient(cfg.tier2_model))`.
5. Breaker: пересадка на ключ `(provider_id, model_id)` (§6.5); `_tier_probe`
   -счётчик (`app.py:815`) умирает.
6. Кора: `RunSpec.model` и gate-валидация `invalid_model` (`app.py:370`) не
   меняются; `_KORA_MODELS` отдаётся клиенту через `GET /api/settings/ai`;
   `KORA_MODELS` в `app.js:161` удаляется.
7. Шестерёнка в футере сайдбара → `#/settings/ai`.
8. speakify и его `speakify_model`/`google_api_key` не трогаются (pinned, §5).

## 8. API

```text
GET  /api/settings/ai                              — всё: провайдеры (+статус ключей), маршрут, кора, allowlist моделей Коры
PUT  /api/settings/ai/routing                      — primary/fallback (+ revision)
PUT  /api/settings/ai/kora                         — default_model/max_turns/budget/deadline (+ revision)
PUT  /api/settings/ai/providers/{provider_id}      — enabled/selected_model (+ revision)
POST /api/settings/ai/providers/{provider_id}/test — проверка подключения на выбранной модели
GET  /api/settings/ai/providers/{provider_id}/models — прокси списка моделей провайдера
```

Требования:

- неизвестный `provider_id` → 404; невалидная модель/поле → 400; конфликт
  `revision` → 409; недоступный upstream при проверке → 502/504 с
  нормализованной ошибкой;
- одинаковый `provider_id` у primary и fallback → 400, даже если модели разные;
- **все роуты — под authn Фазы 0 С5** (bearer) + существующий CSRF на мутациях;
- `GET` не возвращает ключ ни в каком виде, кроме необратимой маски;
- тест подключения rate-limited, не использует текст пользователя, upstream
  response body санитизируется;
- невалидная модель Коры отбрасывается сервером (существующий контракт
  `invalid_model` сохраняется).

## 9. Наблюдаемость

На каждый LLM-pass — безопасная журнальная запись:

```json
{"provider": "openrouter", "model": "google/gemini-3.5-flash",
 "fallback_index": 0, "latency_ms": 820, "settings_revision": 4, "outcome": "ok"}
```

Не журналируются: ключи, полные upstream request/response, текст пользователя.
В UI у ответа — компактная строка `OpenRouter · gemini-3.5-flash · 0.82 s`.

## 10. Ошибки и крайние случаи

1. Включён провайдер без ключа → `не настроен`, в маршрут не выбирается.
2. Выбранная primary-модель исчезла из списка провайдера → карточка показывает
   `модель недоступна`, ход автоматически идёт в fallback без настроечной реплики;
   если fallback отсутствует или тоже недоступен → degraded-путь Task 4.2 С4.
3. Настройки изменены во время хода → ход доезжает на старом snapshot-е.
4. Настройки изменены во время запуска Коры → `RunSpec` не меняется.
5. Два клиента сохраняют одновременно → второй получает 409 по revision.
6. Model-list API недоступен → последний кэш + ручной `model_id`.
7. 429 с `Retry-After` → маршрут в cooldown до указанного времени.
8. 401/403 → `ошибка ключа`, без бесконечных повторов.
9. Primary и fallback оба недоступны → одна degraded-реплика через обязательный
   роутовый catch Task 4.2 С4, не каскад 500.
10. Провайдер вернул ответ без требуемого completion/tool-контракта → адаптер
    поднимает `ProviderContractError`; `GuardedLLMClient` не превращает его в
    `ProviderUnavailable`, fallback не запускается.
11. Рестарт сервера → настройки восстановлены из файла; отсутствие файла на
    первом старте → миграция из bootstrap-конфига (§7.2).

## 11. Скоуп реализации

| Слайс | Содержание | Зависимости |
| --- | --- | --- |
| **AI-0 — пререквизит, не работа** | Фаза 0 С5 (authn) и С4 целиком влиты: Task 4.1 (`GuardedLLMClient`) + Task 4.2 (роут ловит `CostCapBlocked`/`ProviderUnavailable`, возвращает `200 + degraded: true` и пишет обе feed-записи) | Фаза 0 |
| **AI-1 — стор и экран (read-mostly)** | `AISettingsStore` (revision CAS, атомарная запись), миграция §7.2, `GET/PUT` API, шестерёнка + `#/settings/ai`: 3 карточки (ключи read-only), выбор модели, ручной primary/fallback, блок Коры; allowlist Коры с сервера, `KORA_MODELS` из клиента удалён. Старый runtime ещё не читает store | AI-0 |
| **AI-2 — маршрут в runtime** | `RoutedLLMClient` (текст, snapshot на ход), `build_session_pipeline` по snapshot-у (голос), breaker на ключ `(provider, model)` на хосте, журнальная строка маршрута; Кора: `_launch_run` резолвит дефолт модели/лимитов из snapshot-а вместо `cfg.kora_*` (`RunSpec.model=None` → settings, не config); parity-тест: голос и текст при одном snapshot-е дают один маршрут | AI-1 |
| **AI-3 — Google-адаптер** | direct Gemini text-адаптер с tools + voice service, `/test` и `/models` для всех трёх провайдеров, capability-фильтр `supports_tools` | AI-2 |
| **AI-4 — приватность (по желанию, вне acceptance v1)** | Ollama (loopback-only URL — SSRF-правило из v2 сохраняется), режим «Приватность» = только локальная модель, без облачного fallback; privacy-тест сетевыми spy-транспортами: ноль облачных вызовов | AI-3 |

Каждый слайс заканчивается зелёной суитой; замороженные тесты не редактируются.

## 12. Не входит в v1

- редактирование/удаление API-ключей из UI (и Keychain целиком);
- автоматический выбор модели, приоритеты, quality/latency/cost-scoring;
- OpenAI как провайдер;
- смена executor-провайдера Коры; автоматический выбор модели Коры;
- скрытое переключение модели внутри активного запуска Коры;
- пользовательские удалённые Ollama URL (только loopback в AI-4);
- синхронизация настроек между устройствами (revision CAS достаточно);
- настройка STT, TTS, Deepgram, Fish Audio, speakify на этом экране.

## 13. Риски

| Риск | Решение |
| --- | --- |
| Settings API расширяет control plane | Все роуты под authn С5; secret-мутаций нет вовсе |
| Голос и текст расходятся | Один snapshot, один Registry, parity-тест в AI-2 |
| Breaker-рефактор ломает Р-14 | Отдельный таск в AI-2; замороженные тесты каскада — регрессионный якорь |
| Hot reload ломает активный ход | Snapshot на начало хода/сеанса; store никогда не читается из середины хода |
| Кора смешивается с диспетчером | Отдельный блок, отдельный ключ в JSON, `RunSpec` immutable |
| Два источника моделей Коры | Allowlist только на сервере; клиентская копия удаляется в AI-1 |
| Gemini text-адаптер: tool-shape отличается от Anthropic/OpenRouter | Адаптер нормализует shape и поднимает `ProviderContractError`; `GuardedLLMClient` не переклассифицирует его в сетевой fallback (§6.6, §10.10) |

## 14. Приёмка

1. Экран показывает три провайдера диспетчера и отдельный блок Коры;
   вход — шестерёнка в сайдбаре.
2. Модель выбирается внутри карточки провайдера; ручной `model_id` доступен.
3. Ключи нигде не видны, кроме необратимой маски; в `ai-settings.json`,
   API-ответах и журнале ключей нет (тест).
4. Env-ключи работают как раньше; `.env` не изменяется фичей; rollback
   на предыдущую версию безопасен.
5. OpenRouter, Anthropic, Google проходят независимую проверку подключения.
6. Голосовой и текстовый ход при одном snapshot-е выбирают один маршрут
   (parity-тест).
7. До начала AI-1/AI-2 полностью закрыта С4 Task 4.2: `CostCapBlocked` и
   исчерпанный `ProviderUnavailable` возвращают 200 + `degraded: true`, не 500.
8. Ход всегда начинается с primary; timeout/429/5xx переключают на fallback;
   400/contract/security — нет; `CostCapBlocked` не уходит в платный резерв.
9. API запрещает одинаковый provider у primary/fallback; один провайдер
   вызывается не более раза на LLM-pass.
10. Изменение настроек не меняет активный ход, активный голосовой сеанс и
   активный `RunSpec`.
11. Gate-карточка переопределяет модель Коры на один запуск; невалидная модель
    отбрасывается сервером.
12. Конкурентное сохранение → 409, без потери данных; рестарт восстанавливает
    настройки; первый старт без файла мигрирует из bootstrap-конфига §7.2.
13. Breaker, затриповавший маршрут в голосе, виден тексту (общий ключ).
14. Полная тестовая суита остаётся зелёной; замороженные тесты не тронуты.

## 15. Связи

- `docs/superpowers/plans/2026-07-14-synapse-dispatcher-kora-phase0.md` —
  С4 (cost cap/fallback текста — шов §6.3) и С5 (authn — пререквизит API).
- `docs/superpowers/specs/2026-07-13-synapse-ui-v2-design.md` — экран настроек,
  модель Коры, `RunSpec`.
- `docs/dispatcher-kora-ideal-architecture.md` — разделение Dispatcher/Kora.
- `synapse/config.py` — bootstrap: env для ключей/Коры, dataclass defaults для
  `tier1_model`/`tier2_model`; источник миграции §7.2.
- `synapse/cascade/services.py` — текущий каскад и `CostCap` (считает вызовы).
- `synapse/dispatcher/llm_client.py` — `GuardedLLMClient`, шов обёрток.
- `synapse/dispatcher/speakify.py` — pinned LLM-потребитель (§5).
- `synapse/bridge/runspec.py` — immutable launch-параметры Коры.
- `tools/bench_llm_providers.py` — живое сравнение провайдеров.
