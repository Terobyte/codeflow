# Синапс · AI-настройки, голос Коры, файлы и радио

Дата: 2026-07-15.

Статус: **v3 — объединённая спека после архитектурного аудита; implementation
запрещён до закрытия prerequisite checklist и обязательных live probes**.

Область: настройки AI-провайдеров и моделей диспетчера/Коры; реалтайм-
присутствие Коры в голосовом канале (свой голос, живые
реплики, детерминированный роутер ответов и комментариев), TTS-friendly речь
Коры, доставка файлов-результатов в CodeFlow и потоковая озвучка длинных
текстов («радио») голосом литератора через Fish Audio streaming.

Не трогается: STT, права Коры (гейт), approval-контракт. Каскад диспетчера
меняется только в AI-слайсах этой спеки. Документ
`2026-07-14-synapse-ai-provider-settings-design.md` поглощён этой версией и
больше не является самостоятельным источником требований.

## 0. Go/no-go, единый порядок работ и статус

### 0.1. Prerequisite checklist

`OPEN` означает «в репозитории нет достаточного доказательства закрытия»;
это не утверждение, что работа точно не сделана в другой ветке. Перед
implementation-plan владелец плана обязан заменить `OPEN` на ссылку на commit,
тест/артефакт probe и дату. Одного устного подтверждения недостаточно.

| Gate | Статус на 2026-07-15 | Что считается закрытием | Блокирует |
| --- | --- | --- | --- |
| SDK pin `claude-agent-sdk==0.2.116` | **DONE в этой правке** | точный pin в `pyproject.toml`; lock/окружение подтверждается при установке | SDK probes, KV-3b, KV-4 |
| Phase 0 C4 Task 4.2 | **OPEN** | `CostCapBlocked` и исчерпанный `ProviderUnavailable` на voice+HTTP routes дают одну degraded-реплику, HTTP 200 и `degraded: true`; обе feed-записи и тесты | AI-1..AI-3 |
| Phase 0 C5 bearer authn | **OPEN** | fail-closed bearer на control/download/settings routes + `AUTH_FAILURE` tests | AI-1..AI-3, KV-4, KV-5 |
| Phase 0 C6 journal isolation | **OPEN** | journal/artifact root вне доступного Коре project root; секреты не наследуются SDK subprocess; tests | KV-4, KV-5 |
| Probe P1 SDK bidirectional | **OPEN** | артефакт §14.2.1 | KV-3b |
| Probe P2 SDK MCP | **OPEN** | артефакт §14.2.2 | KV-4 |
| Probe P3a Pipecat route bypass | **OPEN** | артефакт §14.2.3: context-frame действительно поглощён до `llm_switcher` | KV-3a |
| Probe P3b Pipecat voice switch/correlation | **OPEN** | артефакт §14.2.4, p95 ≤ 500 мс, exact `context_id ↔ utterance_id`, без потерь | KV-1 |
| Probe P4 Fish MP3 segments | **OPEN** | артефакт §14.2.5 | KV-5 |
| Probe P5 PWA media auth | **OPEN** | артефакт §14.2.6 на iPhone Safari | KV-5 |
| HTTPS staging | **OPEN** | staging origin с TLS, где выполнены P4/P5 | KV-5 |

Жёсткие развилки: сериализация follow-up допустима, потеря follow-up или новый
SDK subprocess — no-go KV-3b; невидимый MCP tool при рабочей permission-
конфигурации — no-go KV-4; неприменимые streaming MP3 или scoped cookie в
Safari — no-go KV-5. Если P3a не доказывает поглощение context-frame до
switcher-а или P3b не даёт exact correlation без FIFO, KV-3a/KV-1 также no-go.
No-go создаёт отдельную transport/design-спеку, а не скрытый fallback в
текущем плане.

### 0.2. Единый dependency graph

| Слайс | Содержание | Входные зависимости | Выход / кого разблокирует |
| --- | --- | --- | --- |
| **P0-C4.2** | route catch: `200 + degraded: true` | C4 Task 4.1 | AI-1 |
| **P0-C5** | bearer authn control plane | Phase 0 | AI-1, KV-4, KV-5 |
| **P0-C6** | journal/artifact isolation | Phase 0 | KV-4, KV-5 |
| **P1/P2** | SDK bidirectional + MCP probes на pinned 0.2.116 | SDK pin | KV-3b / KV-4 |
| **P3a** | Pipecat context-frame route/bypass probe | SDK-independent | KV-3a |
| **P3b** | Pipecat voice-switch + `context_id` correlation probe | SDK-independent | KV-1 |
| **P4/P5** | Fish MP3 + Safari scoped-cookie probes | HTTPS staging | KV-5 |
| **AI-1** | settings store/API/UI, runtime ещё старый | C4.2 + C5 | AI-2 |
| **AI-2** | routed runtime + keyed breaker migration | AI-1 | AI-3 |
| **AI-3** | Google adapter + provider model/test APIs | AI-2 | AI v1 done |
| **KV-1/KV-2** | voice tract / speech contract | P3b / — | KV-3a |
| **KV-3a** | presence + hard router | KV-1 + KV-2 + P3a | KV-3b |
| **KV-3b** | interactive SDK comments | KV-3a + P1 | KV-3 done |
| **KV-4** | `deliver_file` + artifact store | C5 + C6 + P2 | KV-5 |
| **KV-5** | radio | KV-4 + C5 + P4 + P5 + HTTPS | feature done |

Разрешённая параллельность после gates: AI-ветка, KV-1/2/3 и KV-4 могут идти
независимо; KV-5 всегда последний. AI-4/Ollama и Voice Settings M+1 не входят
в acceptance v1.

## 1. Зачем это нужно

Живой прогон 2026-07-15 (задача «найди книгу и порежь главу») показал четыре
дыры разом:

1. **Кора немая и безликая.** Голосом звучат только два шаблона
   (`Задача выполнена: {task_text}` / `не выполнена`, `synapse/bridge/kora.py:207-212`)
   и вопрос `AskUserQuestion` — всё голосом диспетчера. Кнопки Play в ленте
   различаются только title-атрибутом `"TTS · Flow voice"` / `"TTS · Code voice"`
   (`client/app.js:511`), но синтезируют одним и тем же
   `cfg.fish_reference_id` — подпись врёт. Пока задача идёт,
   пользователь в тишине.
2. **Озвучка текста Коры — мусор.** Кора пишет markdown с таблицами, путями и
   идентификаторами; речевого контракта в её системном промпте нет
   (`kora.py:580-594`). Спасает только speakify (Gemini-переписывание,
   `synapse/dispatcher/speakify.py`) — платный LLM-вызов на каждый новый текст
   и лишняя задержка.
3. **Результат-файл не доходит до пользователя.** Кора нашла книгу, порезала
   главу — и положила файл на Desktop, потому что «отправить в CodeFlow» нечем:
   в системе нет ни инструмента доставки, ни роута отдачи файла
   (grep `FileResponse|StaticFiles|as_attachment` по `synapse/pipeline/` пуст;
   единственный роут, касающийся workspace — `/diff`, и тот отдаёт текст).
4. **Слушать длинный текст нельзя.** `POST /api/tts` объявлен в
   `synapse/pipeline/webrtc_server.py:678`, капит текст 4000 символами на `:689`
   и отдаёт один WAV-блоб — глава на
   31 500 слов так не звучит. При этом весь стек уже стоит на стриминговом
   Fish `wss://api.fish.audio/v1/tts/live` (pipecat `fish/tts.py:203`) — есть
   быстрый первый байт, нет только трубы до браузера.

## 2. Зафиксированные продуктовые решения

1. **Три голоса, три роли.** Диспетчер — существующий `FISH_REFERENCE_ID`;
   Кора — сконфигурированный `FISH_VOICE_KORA`, литератор (радио) —
   сконфигурированный `FISH_VOICE_NARRATOR`. Значения `c5e804ba…` и
   `102ea81…` — только рекомендуемые fallback-примеры в `.env.example`, не
   зашитая часть контракта. Голоса — конфиг из `.env` (id голоса — не секрет, но живёт рядом с
   `FISH_REFERENCE_ID` по конвенции). Незаданный голос Коры/литератора →
   fallback на голос диспетчера (фича деградирует, не ломается).
2. **Всё, что произносит Кора, звучит её голосом** — и live-инжекты
   (completion, вопрос, майлстоуны), и Play-кнопка ленты (`role="kora"`).
   Собственный тембр — это ещё и защита от имперсонации: ухо отличает, кто
   говорит, до всякого текста.
3. **Речь Коры — работа промпта, чистота — работа детерминированного фильтра.**
   Системный промпт Коры получает речевой контракт (говорить с пользователем
   разговорно, техника — в thinking/tool-вызовы). Детерминированный
   `speakable()`-фильтр (regex, ноль LLM) решает, чист ли текст; speakify
   остаётся backstop-ом только для грязного текста в Play-пути. В live-пути
   LLM-вызовов нет вообще: грязный текст просто не озвучивается.
4. **Роутер реплик — детерминированный код, не LLM** (принцип
   `docs/dispatcher-kora-ideal-architecture.md`: «LLM никогда не источник
   полномочий»). Жёсткий вопрос `AskUserQuestion`, явное обращение «Кора/Code»
   и короткое окно после реально прозвучавшей реплики Коры направляют следующий
   ход ей; управление и обращение «Флоу/диспетчер» всегда направляют
   диспетчеру. Никакой семантической классификации моделью или embedding-ами.
5. **Р-15 ослабляется точечно и явно: голос — да, контекст — нет.**
   Спикабельные text-блоки Коры (см. §4.3) можно озвучивать её голосом, но они
   по-прежнему никогда не попадают в LLM-контекст диспетчера
   (`TTSSpeakFrame(append_to_context=False)`, регидрация только
   `user`/`assistant` — `dispatcher/loop.py:64-66`). NO-EXFIL как правило о
   контексте не трогается.
6. **`deliver_file` — единственный канал доставки файла в CodeFlow.**
   Кастомный in-process SDK-тул (`create_sdk_mcp_server`; локально установлен
   `claude-agent-sdk 0.2.116`, зависимость закреплена точно в `pyproject.toml`). Успешная
   доставка делает неизменяемый snapshot в серверном artifact-store и создаёт
   карточку `kind="file"` с криптографически случайным `file_id`. Исходный
   абсолютный путь ни в ленту, ни в URL не попадает.
7. **Радио = Fish WS на сервере + ограниченные MP3-сегменты + нативный
   `<audio>`.** Один бесконечный `StreamingResponse` на всю книгу запрещён:
   браузер может выкачать его целиком даже на паузе. Сервер стримит один
   ограниченный сегмент, клиент запрашивает следующий только после `ended`.
   Так сохраняется быстрый первый звук Fish и появляется честный верхний предел
   перерасхода после паузы.
8. **Радио без LLM.** Markdown книги превращается в речевой текст
   детерминированным стриппером (заголовки → строки, таблицы/код-блоки →
   пропуск с пометкой). Прогонять главу через speakify — это десятки
   Gemini-вызовов на книгу; запрещено.
9. **Стоимость Fish под контролем.** Синтез запускается по одному сегменту,
   без prefetch в v1; пауза может досинтезировать только текущий сегмент. Одна
   радио-сессия и не больше одного Fish WS суммарно на хост (voice или radio),
   кап символов документа и сегмента.
10. **Control-роуты — под authn Фазы 0 С5** (bearer + CSRF на мутациях).
    Нативный `<audio>` не умеет послать bearer-заголовок, поэтому media-GET
    получает отдельную короткоживущую HttpOnly SameSite=Strict cookie,
    выпущенную только аутентифицированным POST старта радио и scoped к пути
    конкретной сессии. Токен не попадает в URL.

## 3. Целевая картина

```text
                        ┌────────────────────────────────┐
   голос пользователя   │         VoiceRouter (§4.4)      │  hard question → provide_answer
  ──────────────────────►  адресат + reply-window, 0 LLM ├────────────────────────────► Кора
                        │  control/«Флоу» → диспетчер    │  soft reply → provide_comment
                        └───────────────┬────────────────┘
                                        │ иначе → как сегодня
                                       ▼
                              диспетчер LLM (без изменений)
                                       │ TextFrame
                                       ▼
   Кора: text/completion   ┌───────────────────────────┐    TTSUpdateSettingsFrame(voice)
  ──── speakable()? ──────►│   Арбитр (voice-aware)     ├──────────────────────────────► Fish TTS
        (§4.2, §4.3)       │  QueueItem + voice-метка   │      один сервис, свитч         (WebRTC)
                           └───────────────────────────┘      на границе реплик
   лента CodeFlow:
     kind="file" ──► карточка ──► auth-fetch GET …/files/{file_id} (скачать)
                              └──► POST …/radio-sessions (bearer, выдаёт media-cookie)
                                      └──► GET …/segments/N (один bounded MP3)
                                                ▲
                                      Fish WS ◄── md-стриппер ◄── immutable artifact
```

## 4. Архитектура

### 4.1. Голос Коры в живом тракте (KV-1)

Сегодня: один `FishAudioTTSService` на соединение
(`synapse/pipeline/app.py:1081-1084`), голос задаётся один раз в
`FishAudioTTSService.Settings(model=host.cfg.fish_tts_model,
voice=host.cfg.fish_reference_id)`. pipecat умеет менять голос на лету:
`TTSUpdateSettingsFrame(delta)` → `_update_settings` → reconnect WS
(`.venv/.../pipecat/services/fish/tts.py:219-236`) — свитч не пофреймовый, а
по-реконнектный (~100–300 мс).

**Решение: арбитр становится voice-aware.** Арбитр — единственная точка
сериализации выдачи в TTS (`synapse/pipeline/arbiter.py:95-126`), значит
только он может вставить свитч строго на границе реплик:

- вводится `VoiceRole = Literal["disp", "kora"]` и приложение-специфичный
  `SynapseSpeakFrame(TTSSpeakFrame)` с обязательными `voice_role` и
  `utterance_id`; голая `TTSSpeakFrame` больше не создаётся кодом Синапса;
- `QueueItem` получает обязательные `voice_role` и `utterance_id`;
  `push_dispatcher_text` всегда ставит `disp` и создаёт внутренний id
  `disp:<generation_id>:<sentence_seq>`, а
  `push_speak(text, voice_role, utterance_id)` требует метаданные явно;
- `TTSArbiterProcessor` получает отображение role → reference id и держит
  `current_voice_id`; перед item-ом с другим id пушит
  `TTSUpdateSettingsFrame(delta=FishAudioTTSService.Settings(voice=<id>))`,
  затем сам frame. Это точная форма API установленного pipecat 1.5;
- после Interruption / очистки очереди bookkeeping сбрасывается в `UNKNOWN`,
  не в `disp`: реальный Fish-сервис мог остаться на любом голосе, поэтому
  следующий item обязан безусловно выставить свой id;
- `host.speak()` / `push_speak_frame` (`app.py:210-252`) прокидывают
  voice-метку **явно по источнику**: реплики Коры (completion, вопрос,
  майлстоун) — `"kora"`; системные и диспетчерские реплики — `"disp"`.
  Дефолта в app-level API нет намеренно.

Текущий общий callback `on_speak(text)` разделяется по месту wiring-а, а не по
эвристике текста:

| Источник | Роль |
| --- | --- |
| dispatcher tools / approval-readback / degraded fallback | `disp` |
| reconnect greeting и системные status-фразы | `disp` |
| Kora `AskUserQuestion`, milestone, final и completion-fallback | `kora` |
| replay критического Kora-event из `SpeakLedger` | `kora` |

Аудит охватывает все три текущих site-а создания голой `TTSSpeakFrame`:
`synapse/cascade/strategy.py:130`, `synapse/pipeline/arbiter.py:123` и
`synapse/pipeline/app.py:223`. Все они переходят на приложение-специфичный
frame с явной ролью и id; strategy/fallback ставят `disp`. Лексическое
определение роли по тексту запрещено. Замечание про `strategy.py:130`: этот
site пушит `ALL_TIERS_FAILED_PHRASE` напрямую в `self._services[0]`, минуя
`host.speak()`, поэтому `utterance_id` для него генерируется локально в самом
call-site-е как `disp:all-tiers-failed:<sentence_seq>` (а не непредсказуемый
host-id); роль жёстко `disp`, callback не нужен. То же относится к
fallback-пути `arbiter.py:123`, который идёт через арбитраж, но без Kora-id.

Альтернативы отклонены: второй `FishAudioTTSService` в пайплайне — pipecat
ParallelPipeline + фильтры, два WS-коннекта, тяжело; синтез Кориных реплик
через REST вне пайплайна с инжектом raw-аудио — обходит арбитра и
interruption-семантику (barge-in перестаёт работать на этих репликах).

**Кэш обязан стать voice-aware.** Сейчас `TTSCache` хранит `_voice` один раз в
конструкторе, а `get/put_wav/put_pcm/assemble/wav_path` не принимают голос. Одного
«observer отслеживает update-frame» недостаточно. Контракт меняется полностью:

- `key`, `wav_path`, `get`, `put_wav`, `put_pcm`, `assemble` принимают
  `voice_id`; старый dispatcher-id остаётся только compatibility-default на
  время миграции тестов;
- observer замечает `TTSUpdateSettingsFrame` **до** фильтра
  `data.source is self._tts` (frame пушит арбитр, а Fish его поглощает), хранит
  `current_voice_id` и snapshot-ит его в `_open` на `TTSStartedFrame`;
- `_finalize` пишет `put_pcm(..., voice_id=run["voice_id"])`; поздний
  `TTSTextFrame` не может случайно взять уже переключившийся голос;
- `/api/tts` вычисляет `voice_id` до lookup и использует его во всех трёх
  ступенях cache → assemble → synth.

**Play-кнопка становится честной.** `POST /api/tts` принимает только роли
`disp|kora` (неизвестная → 400), выбирает reference id и использует его и для
кэша, и для `fish_rest_tts`. Если Корин id не задан, он резолвится в дисп-id
до обращения к кэшу: одинаковый реальный звук закономерно делит ключ.

### 4.2. Речевой контракт Коры (KV-2)

В `_system_prompt` (`kora.py:580-594`) добавляется абзац:

> Твои текстовые сообщения пользователь слышит голосом. Пиши их разговорно,
> одним-двумя короткими абзацами: без markdown-разметки, таблиц, код-блоков,
> путей и идентификаторов. Все технические выкладки, списки файлов и команды
> держи в thinking и в tool-вызовах — пользователю говори итог и смысл.

Детерминированный фильтр `speakable(text) -> bool` (новый модуль
`synapse/dispatcher/speakable.py`, чистые regex, ноль сети): текст грязный,
если содержит markdown-таблицы (`|---`), код-фенсы, заголовки/буллеты
разметкой, инлайн-код, URL/абсолютные пути, длинные идентификаторы; плюс кап
длины (конфиг `kora_speak_max_chars`, default 350).

`speakable()` — только форматный фильтр, не semantic safety-классификатор.
Обычная короткая фраза из prompt-injection может пройти его. Безопасность
держится не на regex, а на отдельном голосе Коры, отсутствии записи этой речи в
контекст диспетчера и неизменном approval/gate-контракте. В документации и
тестах запрещено называть `speakable()` «фильтром безопасного содержания».

Потребители фильтра:

- **live-путь (§4.3)**: только `speakable()`-текст озвучивается; грязный
  остаётся в ленте молча — ноль LLM в live;
- **Play-путь** (`webrtc_server.py:678-710`): `speakable()`-текст идёт в TTS
  как есть (минус Gemini-вызов и задержка), грязный — через speakify как
  сегодня. speakify остаётся pinned-потребителем по карте §4.8 этой спеки.

### 4.3. Реалтайм-присутствие Коры (KV-3)

Сегодня mid-run наружу звучит только вопрос `AskUserQuestion`
(`kora.py:826-827`), терминально — два шаблона; собственный текст Коры не
звучит никогда — спик структурно lifecycle-only (`kora.py:389-397`).

Ослабление (решение №5) реализуется так:

- `_message_to_events` (`kora.py:221-289`) при завершённом `TextBlock`
  дополнительно создаёт **narratable** `kora_said` с полным display-текстом в
  ленте и `speak_text` только при `speakable(text)`. Token-delta из
  `StreamEvent` в v1 не озвучивается: иначе пользователь услышит обрывки и
  повторы после финального `AssistantMessage`;
- стабильный `utterance_id = sha256(task_id|message_id|block_index|text)[:24]`
  проходит в событие, ленту и transient speech-state. Повтор доставки того же
  SDK message не озвучивается дважды;
- **политика майлстоунов** (`KoraPresencePolicy`, чистое решение над
  snapshot-ом) разрешает звук, только если задача всё ещё RUNNING, live output
  привязан к треду-владельцу, `VoiceOutputState.is_idle()` (нет активного
  dispatcher generation, ожидающего TTS item или проигрываемого audio), с прошлого
  майлстоуна прошло ≥ `kora_milestone_min_gap_s` и этот utterance ещё не
  spoken. Проверки одной длины очереди недостаточно: арбитр дренит item до
  того, как динамик закончил его играть;
- запрет сейчас означает «оставить в ленте молча», а не отложить: старый
  прогресс не должен внезапно прозвучать через минуту вне контекста;
- **инвариант против тишины:** если RUNNING-задача имеет live output, диспетчер
  не говорит и с последней реально начавшейся Kora-реплики прошло
  `kora_presence_max_silence_s`, watchdog произносит короткий детерминированный
  NO-EXFIL heartbeat («Я продолжаю работу.»), даже если все новые TextBlock-и
  отсеяны `speakable()`. Heartbeat подчиняется idle/owner/dedupe, не открывает
  reply-window и не чаще одного раза за max-silence interval. Чистый milestone
  имеет приоритет и сбрасывает таймер. Поэтому prompt-контракт улучшает речь,
  но не является единственной гарантией отсутствия многоминутной тишины;
- **терминальная реплика**: последний чистый TextBlock звучит на ResultMessage,
  если он не был озвучен как milestone; если тот же TextBlock уже прозвучал,
  terminal speech считается выполненным и второй шаблон не добавляется. Только
  когда чистого финального текста нет, звучит существующий NO-EXFIL-шаблон
  `Задача выполнена: {task_text}` / failure-шаблон. Один `utterance_id` —
  максимум одна live-реплика;
- начавший звучать вопрос или milestone открывает transient
  `KoraReplyWindow(task_id, thread_id, utterance_id, expires_at)`; окно
  создаётся не по `push_speak_frame=True`, а по коррелированному
  `TTSStartedFrame` после acceptance. FIFO-сопоставление запрещено.
  `SynapseSpeakFrame.utterance_id` проходит через `QueueItem` после всех
  reordering/drop-решений арбитра. На входе в TTS адаптер регистрирует
  `TTSStartedFrame.context_id → utterance_id`; `context_id` создаётся Pipecat
  отдельно для каждого `TTSSpeakFrame` и уже присутствует в started/stopped
  frames. Observer открывает окно только по exact lookup этой пары. Drop удаляет
  запись без callback, interruption очищает все незапущенные пары, а неизвестный
  или повторный `context_id` fail-closed не открывает окно. Dispatcher-run не
  может забрать Kora-id даже при reordering;
  ⚠️ **весь presence-механизм (reply-window, dedupe, terminal-vs-milestone)
  держится на допущении, что установленная версия Pipecat создаёт уникальный
  `context_id` на каждый `TTSSpeakFrame` и кладёт его в `TTSStartedFrame`.** Это
  single-point-of-failure: если версия не даёт unique-per-frame `context_id`
  (переиспользует, None, не пробрасывает в started), reply-window не открывается
  ни для какой реплики — вся interactive-Kora-функциональность KV-3b молча
  деградирует до «только hard question». Поэтому probe P3b проверяет именно
  presence+uniqueness `context_id` на 20 чередованиях; невыполнение — no-go
  KV-1/KV-3b, а не скрытый FIFO-fallback;
- контекст диспетчера не меняется: `SynapseSpeakFrame` наследует
  `append_to_context=False`, регидрация по-прежнему берёт только
  `user`/`assistant` (`loop.py:64-66`). `kind="text"`, `kind="file"` и
  user-entry с `to:"kora"` туда не попадают.

`KoraReplyWindow` не персистится: после рестарта нет живой SDK-сессии, которой
можно ответить. Окно закрывается по timeout, terminal/cancel/supersede задачи,
началу речи диспетчера или успешной маршрутизации одного пользовательского
хода. Новый milestone заменяет предыдущее окно.

Риск инъекции (workspace-контент → речь Коры) принят осознанно. Форматный
фильтр, кап и троттлинг снижают шум, но не доказывают смысловую безопасность;
несущие защиты — отдельный тембр, нулевой доступ речи к dispatcher context и
неизменные полномочия approval/gate.

### 4.4. VoiceRouter — детерминированный роутер реплик (KV-3)

Сегодня голосовая реплика **всегда** уходит в LLM диспетчера, но не из
`_on_end_of_turn`. Этот callback (`app.py:931-983`) — side-channel
bookkeeping: journal, system context и feed; кадров в пайплайн он не пушит.
Реальный запуск диспетчера задаёт frame-граф из 8 узлов
`stt → user_aggregator → pre_hook → llm_switcher → post_hook → arbiter → tts →
assistant_aggregator` (`app.py:1101-1112`, `pre_hook` на `:1105`,
`llm_switcher` на `:1106`). Поэтому решение роутера внутри `_on_end_of_turn`
не способно остановить генерацию и запрещено.

**Решение для voice — frame-gate, для HTTP — прямой fast-path.** В voice-граф
между `user_aggregator` и `pre_hook` встаёт async `VoiceRouteProcessor`;
итоговый порядок:

```text
stt → RoutableUserAggregator → VoiceRouteProcessor → pre_hook → llm_switcher
```

`RoutableUserAggregator` добавляет к выходному context-frame одноразовый
`VoiceTurnEnvelope(turn_id, transcript, user_message_index)`. Processor сначала
идемпотентно выполняет сегодняшний turn-bookkeeping (journal/approval/feed и
обновление system message), затем строит snapshot и вызывает чистый
`VoiceRouter.route(text, RouteSnapshot)`. `_on_end_of_turn` после этого не
мутирует routing/context/feed: он остаётся только STT telemetry hook или
удаляется. Таким образом нет скрытого предположения о порядке callback ↔ flush.

- `dispatcher`: envelope снимается, обычный `LLMContextFrame` ровно один раз
  идёт дальше; `pre_hook` и `llm_switcher` работают как сегодня;
- `kora_*`: processor await-ит доставку. При успехе он удаляет ровно user-entry
  с `user_message_index` из live `LLMContext`, пишет `to:"kora"` и **поглощает**
  context-frame; ни `pre_hook`, ни `llm_switcher` его не видят;
- при отказе/гонке доставки processor не откатывает user-entry и пересылает тот
  же frame диспетчеру. Потеря хода и двойная доставка запрещены;
- несовпавший index/text, повторный envelope или ошибка bookkeeping —
  fail-closed в dispatcher, с audit event; «угадать» элемент контекста по
  последней строке запрещено.

HTTP не использует frame-граф: тот же router вызывается в
`POST /api/threads/{id}/message` непосредственно **до** прямого
`host.text_loop.ingest_user_turn()` (`webrtc_server.py:640`). Snapshot содержит
только status/owner/awaiting/reply-window, текущий thread id и clock; I/O и LLM
в роутере нет. P3a обязан доказать call-counter-ом, что поглощённый voice-frame
не достигает fake `llm_switcher`; без этого KV-3a — no-go.

```text
1. пустая/невалидная реплика                                      → dispatcher
2. первое слово ∈ control_words                                  → dispatcher
   default: стоп, отмена, статус, compact, clear
3. первое слово ∈ dispatcher_vocatives                           → dispatcher
   default: флоу, flow, диспетчер
4. первое слово ∈ kora_vocatives AND RUNNING task/owner совместим → kora_comment
   default: кора, код, code
5. awaiting_answer AND owner-thread совпадает                     → kora_answer
6. живо reply-window AND task/thread/utterance совпадают          → kora_comment
7. иначе                                                          → dispatcher
```

Пунктуация вокруг **первого токена** снимается, token casefold-ится; вся
остальная исходная строка доставляется дословно. Слова управления имеют
приоритет над окном. «Нет, стоп на третьем варианте» остаётся ответом Коре,
потому что control проверяется только первым токеном; одиночное «стоп» всегда
достаётся диспетчеру.

Owner-совместимость строгая для HTTP: current thread обязан совпасть с тредом
активной задачи. Для voice сохраняется существующее правило `_voice_answer`:
явный другой voice-thread запрещён, а `voice_thread=None` во время hard
`awaiting_answer` считается неопределённостью и доставляется owner-треду. Для
soft comment после reconnect `None` не разрешается: reply-window всё равно
закрывается при disconnect. Серверные `/compact` и `/clear` обрабатываются до
VoiceRouter и никогда не уходят Коре.

Ветки доставки:

- `kora_answer` → существующий `provide_answer` для parked
  `AskUserQuestion`;
- `kora_comment` → новый async
  `KoraRunner.provide_comment(text, task_id, thread_id) -> bool`: envelope с
  ack-future кладётся в bounded queue, а `True` возвращается только после
  успешной записи turn в SDK transport. Process await-ит этот future под
  **явным delivery deadline** `kora_comment_delivery_timeout_s` (default 2.0,
  новое bootstrap-поле): voice-пайплайн данного соединения сериализован, и без
  bound-а медленная/зависшая SDK-запись блокирует весь тракт (STT молчит, пока
  ждём ack). При таймауте process считается, что доставка не подтверждена,
  атомарно закрывает reply-window и пересылает тот же ход диспетчеру — то же
  поведение, что при полной queue (case 6); очередь при этом не теряет ход
  (снятая reservation не удаляет уже поставленный envelope, но late-результат
  его игнорирует по identity-guard). Латентность доставки входит в go/no-go
  KV-3a: p95 ack ≤ 500 мс, иначе асинхронная delivery-модель вместо in-line
  await-а;
- успех пишет user-feed ровно один раз с `to:"kora"`, `task_id` и
  `utterance_id` (если было окно), journal `route_to_kora` с причиной
  `hard_question|reply_window|explicit_vocative`; dispatcher LLM не вызывается;
- доставка вернула False из-за гонки terminal/supersede/closed queue → окно
  атомарно закрывается, и тот же текст тем же ходом идёт диспетчеру;
- тул `answer_kora` не удаляется: явные сложные просьбы «передай Коре…» вне
  reply-window остаются fallback-ом dispatcher LLM.

**Интерактивный транспорт Коры.** Предыдущий черновик ошибочно считал
`client.query()` во время ответа неопределённым. Локальный
`claude-agent-sdk 0.2.116` прямо определяет `ClaudeSDKClient` как bidirectional:
`query()` можно вызывать после `connect()` и параллельно читать
`receive_messages()`. Реализация переходит с one-response convenience-loop на
одного владельца SDK-client + один reader + bounded comment queue:

- initial query и каждый enqueue-reserved comment увеличивают
  `pending_queries` под одним interaction-lock до возможного terminal result;
  каждый `ResultMessage` уменьшает его;
- result при зарезервированном, но ещё не записанном comment сохраняется как
  `deferred_result`, а не terminalize-ит TaskStore. Успешная запись продолжает
  сессию; write failure снимает reservation, завершает deferred result и даёт
  router-у `False` для dispatcher-fallback;
- промежуточный `ResultMessage` не terminalize-ит TaskStore, пока есть
  принятый comment; только result при `pending_queries == 0` создаёт
  `task_completed/failed`;
- один reader — единственный потребитель `receive_messages`; HTTP/voice код
  никогда не пишет в transport напрямую, только в очередь;
- queue cap = 3; переполнение → `False` и dispatcher-fallback, без потери хода;
- cancel/supersede сначала закрывает reply-window и queue, затем отменяет
  active runner; late comment по identity-guard не попадает преемнику.

До implementation-plan обязателен protocol probe реального CLI: отправить
comment во время tool-heavy ответа и зафиксировать порядок `ResultMessage`,
сохранение session context, работу PreToolUse hooks и отсутствие второго
subprocess. Если CLI фактически сериализует follow-up только после первого
result — это допустимо; если теряет его или открывает новый процесс, KV-3
блокируется, а не подменяется LLM-роутером.

### 4.5. `deliver_file` — доставка файла в CodeFlow (KV-4)

**Инструмент.** In-process SDK MCP-сервер (`create_sdk_mcp_server`,
локальный SDK 0.2.116) с одним тулом:

```text
deliver_file(path: str, title: str | None) -> {"delivered": bool, "reason"?: str}
```

Регистрируется в `_build_options` через `mcp_servers`. Власть лежит **внутри
trusted handler-а**: даже если SDK permission-layer разрешил вызов, handler
повторно валидирует файл и может вернуть deny. ⚠️ До implementation-plan нужен
probe: точное имя (`mcp__synapse__deliver_file` или иное), видимость при
`allowed_tools=[]`, прохождение `PreToolUse` hook и отсутствие shadowing. Если
нужен allowlist, разрешается ровно фактическое имя, но hook остаётся
аудит-слоем, не authority.

**Гейт + snapshot доставки.** Ограничение «любой читаемый файл машины»
отклонено: prompt-injection не должен превращать `deliver_file` в общий канал
экспорта. Живой кейс книги в `~/Downloads` сохраняется через узкий allowlist
корней. Допустимый resolved source обязан лежать либо в immutable
`RunSpec.project_root`, либо под одним из bootstrap
`deliver_file_extra_roots` (default: только `~/Downloads`). Сетевой root,
`~`, `/`, пустой root и root, содержащий artifact/journal store, невалидны при
старте процесса. Расширение списка — осознанная server-admin операция, не
аргумент tool-вызова и не runtime-настройка Коры.

- relative path резолвится относительно `RunSpec.project_root`; после resolve
  проверяются allowed root **и** secret policy до открытия. Категории
  `outside_allowed_roots/secret/resolve/missing/directory/too_large` не содержат
  абсолютного пути;
- secret policy — отдельная authority-boundary, а не неформальная ссылка на
  существующий helper. Все сравнения casefolded. Deny: любой сегмент
  `.ssh/.aws/.gnupg/.kube/.docker/.git/.config/Keychains`; `.env`, `*.env` и
  `.env.*`, кроме `.example/.sample/.template/.dist/.md`; exact names
  `credentials*`, `.netrc`, `.git-credentials`, `.npmrc`, `.pypirc`,
  `.dockercfg`, `.htpasswd`, `.envrc`, `secrets.{yaml,yml,json,toml}`,
  `token(s).txt`, `apikey.txt`, `api_key.txt`, `service-account.json`,
  `.pgpass`, shell rc/history, `.claude.json`; private-key stems `id_rsa`,
  `id_dsa`, `id_ecdsa`, `id_ed25519` и `*_sk`; suffixes
  `.pem/.key/.p12/.pfx/.keystore/.jks`. Реализация выносит это в общий typed
  `SecretPathPolicy`, используемый Kora read-gate и delivery, чтобы списки не
  разъехались;
- policy version журналируется безопасным id (`secret_policy_version`), но
  denied path не логируется. Неизвестная ошибка policy или невозможность
  подтвердить принадлежность allowed root → deny, не allow;
- файл открывается без follow финального symlink-а (`O_NOFOLLOW`, где доступен),
  затем `fstat` подтверждает regular file; чтение идёт чанками с hard cap, а не
  по доверенному предварительному `stat`;
- одновременно считаются sha256 и размер; bytes атомарно кладутся в
  `<journal_dir>/artifacts/blobs/<sha256>` (tmp + replace). Это immutable
  snapshot: исходник можно удалить/изменить, карточка продолжает отдавать ровно
  доставленные байты;
- `journal_dir`/artifact-store должен быть вне доступного Коре project root —
  пререквизит hardening-слайса С6. API-token и исходные registry paths в env
  SDK-subprocess не передаются.

**Лента и отдача.** Успех → `log_sink`-запись `kind="file"`:

```json
{"kind": "file", "ts": …, "task_id": "…", "name": "Think AI — Глава 1.md",
 "title": "…", "size": 812345, "mime": "text/markdown",
 "file_id": "a3f9…", "text": "📎 Think AI — Глава 1.md"}
```

`file_id = secrets.token_urlsafe(24)` — opaque id с достаточной энтропией
(не самостоятельная авторизация),
не hash пути и не hash содержимого. В **server-side реестре треда** хранится
`file_id → {blob_sha256, name, title, size, mime, created_ts, task_id}`. Реестр
персистится атомарно рядом с метаданными `ThreadStore`; исходного пути там уже
нет. Повторный `deliver_file` того же blob в том же треде идемпотентно
возвращает существующую карточку, но другой тред получает другой `file_id`.

Новый роут `GET /api/threads/{thread_id}/files/{file_id}`:

- `file_id` ищется только в реестре указанного треда; blob обязан существовать,
  иметь ожидаемый размер и жить под private artifact-store;
- отдача `FileResponse` с `content-disposition: attachment`;
- authn С5; это read-роут — CSRF не нужен, но и **никакого листинга**: только
  точечный opaque id.

**Клиент.** `addEntry` (`app.js:554-605`) учится `kind="file"`: карточка-вложение
(имя, размер, кнопка «Скачать» → `fetch` с Authorization → Blob → временный
object URL; обычный `<a href>` bearer не посылает). Для `text/*` — кнопка
«Озвучить» → радио §4.6). Кора узнаёт про инструмент из системного промпта:
«результат-файл отправляй пользователю инструментом deliver_file, а не копией
на Desktop».

Artifact живёт вместе с тредом и переживает рестарт. Архивация треда его не
удаляет; garbage collection и quota-store — отдельный M+1. В v1 общий объём
ограничивается только per-file cap и свободным диском; ошибка записи диска
возвращает `storage_error`, карточка не создаётся.

### 4.6. Радио — потоковая озвучка литератором (KV-5)

**Серверное плечо: тонкий Fish WS-клиент** `synapse/pipeline/radio.py` по
протоколу, который уже реализует pipecat (`ormsgpack`: `start` c
`reference_id`, серия `text`, `flush`/`stop`; модель — HTTP-заголовок
хендшейка — `fish/tts.py:290-314`). Наш клиент независим от pipecat-сервиса
(тот заточен под пайплайн-фреймы), формат выхода — `mp3` (Fish WS его
поддерживает; экономия полосы против PCM и нативная воспроизводимость
`<audio>`).

**Подготовка текста — детерминированный md-стриппер** (`radio.py`, ноль LLM):
заголовки → обычные строки, списки → предложения, таблицы и код-фенсы →
пропуск с врезкой «таблица пропущена» / «код пропущен», сноски/ссылки →
текст без URL. Разбивка на параграфы, внутри — `default_sentence_splitter`
(`arbiter.py:28-46`). После нормализации предложения детерминированно пакуются
в сегменты ≤ `radio_segment_max_chars`; один слишком длинный sentence режется
по clause/word boundary. Manifest содержит для каждого сегмента
`index/from_paragraph/to_paragraph/chars` и считается до первого Fish-вызова.

**Почему не один stream на главу.** `<audio>` сам решает, сколько буферизовать;
`pause()` не обязан остановить network read. Поэтому TCP-backpressure не может
быть биллинговым инвариантом. V1 синтезирует ровно один bounded-сегмент на GET;
следующий GET появляется только после `ended`. Максимальный перерасход после
паузы — остаток одного сегмента.

**Control-роут старта**
`POST /api/threads/{thread_id}/radio-sessions` с
`{"file_id":"…","from_paragraph":0}`:

- bearer auth + CSRF; file берётся из immutable registry §4.5 и допускаются
  только `text/plain`, `text/markdown` и UTF-8-декодируемые файлы;
- строится manifest; пустой документ → 422, `from` вне диапазона → 416,
  документ после нормализации > `radio_max_chars` → 413;
- одна active session на host: вторая → 409 с id без токена; session хранит
  случайный id, thread/file/blob digest, manifest, current segment, expiry;
- до создания session control-route атомарно берёт общий
  `FishSessionLease(kind="radio")`. Если lease удерживает live voice pipeline,
  ответ `409 fish_busy_voice`; если radio — `409 radio_active`. Lease нельзя
  получить проверкой bool + последующей записью: это один lock/CAS;
- ответ `201` несёт display metadata, `total_segments`, `start_segment` и
  границы manifest, но не текст книги;
- тем же ответом ставится случайная media-cookie:
  `synapse_radio=<token>; HttpOnly; SameSite=Strict; Path=/api/radio/{session_id}; Max-Age=…`.
  `Secure` обязателен на HTTPS и снимается только в explicit insecure-dev.
  `Max-Age` равен `radio_session_ttl_s` (28800): cookie и session истекают
  одновременно, поэтому истёкшая cookie не может авторизовать продление
  просроченной session (case 12); продление — только новый bearer+CSRF
  control-POST с сохранённого paragraph.

**Media-роут** `GET /api/radio/{session_id}/segments/{segment_index}`:

- нативный `<audio>` автоматически посылает scoped cookie; handler constant-time
  сверяет её с session token. Bearer не требуется именно на этом route, потому
  что `<audio src>` не умеет выставить заголовок; cookie является узкой
  capability-authn, выпущенной только bearer-authenticated control-route;
- segment должен быть текущим ожидаемым индексом; повтор уже завершённого
  допускается один раз для browser retry, прыжок вперёд → 409;
- нативный `<audio>` массово шлёт `Range:`-запросы при буферизации и seeking.
  Live-сегмент — это chunked stream без известной длины и без seek-гранул, к
  которому byte-Range неприменим семантически. Поэтому media-роут игнорирует
  `Range` (отдаёт весь сегмент с `200` и `Content-Type: audio/mpeg`, без
  `Accept-Ranges`/`Content-Range`), либо возвращает `416` на явный Range-запрос
  — точное поведение обязано зафиксировать probe P5 на целевых браузерах, чтобы
  избежать пере-запроса/зависания элемента. `Content-Length` намеренно не
  выставляется (streaming, длина неизвестна до Fish-finish); клиентский плеер
  обязан корректно играть chunked MP3 без длины;
- `StreamingResponse(audio/mpeg)` открывает Fish WS, шлёт `start` с narrator
  voice/model/`format=mp3`, text/flush и немедленно отдаёт приходящие audio
  bytes. На `finish`, disconnect или error WS закрывается в `finally`;
- только успешный `finish` двигает current segment. Ошибка Fish → оборванный
  stream; повтор того же segment разрешён новым GET;
- в один момент не больше одного Fish WS суммарно. Voice pipeline берёт тот же
  `FishSessionLease(kind="voice")` перед connect и освобождает после
  подтверждённого disconnect; radio открывает WS только владея своим lease.
  Отмена/expiry инвалидирует cookie/session; текущий generator получает
  cancellation, посылает `stop` best effort и ровно один раз освобождает lease.

**Control-роут стопа** `DELETE /api/radio-sessions/{session_id}` — bearer +
CSRF, идемпотентно закрывает active session/WS и истекает media-cookie. Natural
completion делает то же после последнего сегмента.

**Клиент: мини-плеер.** Кнопка «Озвучить» сначала вызывает control-POST, затем
ставит `audio.src` на первый segment URL. `ended` сохраняет `to_paragraph`
текущего segment в `localStorage` под ключом `(thread_id,file_id)` и только
тогда ставит URL следующего. Плашка
в шапке треда показывает play/pause, точный «фрагмент N из M», stop и
«продолжить». В v1 нет prefetch: возможная короткая пауза между сегментами —
осознанная цена честного cost-bound. `nowPlaying` остаётся единым для Play и
radio; старт одного останавливает другое.

После рестарта server-session потеряна: клиент создаёт новую через control-POST
с сохранённым `from_paragraph`. Immutable blob + детерминированный stripper
делают продолжение стабильным **для того же `file_id`**. Новый контент всегда
получает новый `file_id`, даже при том же имени/title: старую paragraph-закладку
нельзя безопасно переносить на другую версию. UI сохраняет её у старой карточки,
а у новой явно показывает «Новая версия — начать сначала»; тихое применение
старого paragraph mapping запрещено.

**Сосуществование с голосовым сеансом.** Открытый мик + радио в колонки =
STT слушает книгу. Детерминированное клиентское правило v1: радио и живой мик
взаимоисключающие — тап на мик ставит радио на паузу; старт радио при живом
звонке сначала вешает звонок (существующий `disconnectVoice`,
`app.js:1000-1017`) и ждёт подтверждённый disconnect, только затем делает
control-POST. Это UX-правило; `FishSessionLease` — server-side backstop против
гонки, второго клиента и обхода UI. Поэтому буквальный инвариант «один Fish WS
на хост» включает и voice TTS, и radio.

### 4.7. Единый контракт конфигурации

Двух равноправных runtime-конфигов нет. Владение разделено по типу данных:

- `SynapseConfig` — immutable **bootstrap** на процесс: секреты, пути, порты,
  low-level operational limits и seed первого запуска. Он читается один раз из
  env/defaults и не является пользовательским runtime-store;
- `AISettingsStore` — единственный source of truth для изменяемых в UI
  AI-настроек: провайдеры, модели, routing и дефолты запуска Коры. Runtime
  читает только immutable snapshot store-а на границе хода/voice-session/run;
- API keys остаются только в env/`SynapseConfig`, никогда не пишутся в store;
- voice reference ids и radio/presence caps остаются bootstrap в v1, потому
  что UI этой версии не управляет TTS. **Владелец продолжения зафиксирован:**
  `Settings → Voice` в M+1 переносит `fish_voice_*` и настраиваемые caps в
  отдельную секцию того же settings store. До M+1 UI честно показывает
  `configured from env`, read-only; dead end «навсегда в .env» запрещён.

Bootstrap-поля `SynapseConfig` (малформ → дефолт, не crash — паттерн B4):

```text
fish_voice_kora: str | None = None        FISH_VOICE_KORA      (нет → голос диспетчера)
fish_voice_narrator: str | None = None    FISH_VOICE_NARRATOR  (нет → голос диспетчера, §2.1)
kora_speak_max_chars: int = 350           KORA_SPEAK_MAX_CHARS
kora_milestone_min_gap_s: float = 20.0    KORA_MILESTONE_MIN_GAP_S
kora_presence_max_silence_s: float = 45.0 KORA_PRESENCE_MAX_SILENCE_S
kora_reply_window_s: float = 15.0         KORA_REPLY_WINDOW_S
kora_comment_queue_max: int = 3           KORA_COMMENT_QUEUE_MAX
kora_comment_delivery_timeout_s: float = 2.0 KORA_COMMENT_DELIVERY_TIMEOUT_S
deliver_file_max_mb: int = 50             DELIVER_FILE_MAX_MB
deliver_file_extra_roots: tuple[Path, ...] DELIVER_FILE_EXTRA_ROOTS (default: ~/Downloads)
radio_max_chars: int = 200_000            RADIO_MAX_CHARS
radio_segment_max_chars: int = 2_500      RADIO_SEGMENT_MAX_CHARS
radio_session_ttl_s: int = 28_800         RADIO_SESSION_TTL_S
```

`.env.example` дополняется этими именами с реальными id голосов как
подсказками-значениями. Наборы `router_control_words`,
`dispatcher_vocatives`, `kora_vocatives` — typed `frozenset` в config с
дефолтами §4.4; env-редактирование словаря в v1 не нужно.

Несекретные runtime-данные живут в `<journal_dir>/ai-settings.json`. Значения
моделей ниже — иллюстративные; **точные model id в seed обязаны совпадать с
реальными SKU провайдера**, иначе `/test` вернёт 404, а route упадёт на
несуществующей модели. Текущий bootstrap (`tier1_model="google/gemini-3.5-flash"`,
`tier2_model="claude-haiku-4-5"`, `speakify_model="gemini-3.5-flash"`,
`config.py:15-16,31`) содержит имена, не соответствующие реальным SKU
(`gemini-1.5-flash`/`gemini-2.0-flash`, `claude-3-5-haiku` и т.п.);
implementation-plan обязан зафиксировать проверенные id до миграции и добавить
валидность seed в prerequisite checklist.

```json
{
  "schema_version": 1,
  "revision": 4,
  "providers": {
    "openrouter": {"enabled": true, "selected_model": "google/gemini-3.5-flash"},
    "anthropic": {"enabled": true, "selected_model": "claude-haiku-4-5"},
    "google": {"enabled": false, "selected_model": null}
  },
  "routing": {
    "primary": {"provider": "openrouter", "model": "google/gemini-3.5-flash"},
    "fallback": {"provider": "anthropic", "model": "claude-haiku-4-5"}
  },
  "kora": {
    "default_model": "claude-sonnet-5",
    "max_turns": 40,
    "max_budget_usd": 1.0,
    "deadline_s": 900
  }
}
```

Запись: unique temp в той же директории → fsync → `os.replace`; update несёт
ожидаемую `revision`. `providers[id].selected_model` — значение селектора и
`/test`, а `routing.*.model` — реально исполняемый маршрут; они могут
расходиться явно, но UI при таком расхождении показывает предупреждение.

**Первый запуск и конец zombie-полей.** Если файла нет, store один раз
создаётся из bootstrap: OpenRouter/`tier1_model` → Anthropic/`tier2_model` и
текущих `kora_*`. После успешного atomic commit пишется событие
`ai_settings_migrated`; с этого момента `tier1_model`/`tier2_model` никогда не
читаются runtime-кодом и не участвуют в повторной миграции. Они остаются
deprecated rollback seed только до конца v1 и удаляются в следующей schema
version. Изменение env после созданного store не меняет маршрут; UI показывает
этот факт. `speakify_model` остаётся отдельным pinned bootstrap-потребителем.

**Model-list cache.** Последний успешный список хранится сервером в
`<journal_dir>/model-cache/{provider_id}.json`, атомарно, без ключей и тел
ошибок. Fresh TTL = 6 часов, stale-if-error = 30 дней. Успешный refresh
заменяет cache и ставит `fetched_at`; ручной refresh обходит fresh TTL, но
rate-limited. Смена/исчезновение env-key инвалидирует только fresh-состояние,
не удаляя stale список. После 30 дней upstream failure API возвращает пустой
список + ручной `model_id`, а не заведомо устаревший каталог. Выбранная модель
никогда не удаляется из settings автоматически.

**409 в UI.** Клиент не делает auto-merge. При CAS conflict он сохраняет
локальный draft в памяти, повторно делает `GET`, показывает «Настройки изменены
на другом устройстве» и diff `server vs draft`, затем предлагает
`Применить мой вариант` (явный новый PUT с новой revision) или `Принять
серверный`. Фоновый retry и last-write-wins запрещены.

### 4.8. AI settings и ручная маршрутизация

Экран `#/settings/ai` открывается шестерёнкой в футере сайдбара и содержит:

```text
Настройки AI
├── Диспетчер
│   ├── OpenRouter / Anthropic / Google AI Studio
│   │   └── enabled · key status read-only · model · test
│   └── Маршрут: primary + optional fallback
├── Кора
│   └── Claude Agent SDK read-only · model · max turns · budget · deadline
└── Voice (v1 read-only)
    └── Dispatcher / Kora / Narrator: configured from env · M+1 owner
```

Провайдеры v1: OpenRouter, Anthropic, Google AI Studio. Модель хранится парой
`(provider_id, model_id)`. Primary/fallback ручные; одинаковый provider в обеих
позициях запрещён даже с разными моделями. Обоснование: fallback существует для
провайдер-level отказов (5xx инфраструктуры, 401 key, региональный outage) и
только для них; модель-специфичные 429/timeout при том же провайдере не
считаются independent failure, поэтому резервный маршрут обязан идти на другой
provider-аккаунт/инфраструктуру. Это сознательное сужение полезного
fallback-паттерна (`openrouter/gemini → openrouter/claude` при 429 на одной
модели) в обмен на честность «fallback = другой провайдер»; UI показывает это
правило явно. Ключи показываются только как
`configured/source/mask`, не возвращаются и не изменяются API. Кора остаётся
Claude Agent SDK с server-side allowlist; клиентская `KORA_MODELS` удаляется.

Fallback разрешён для timeout, connection error, 429 и 5xx. 400,
`ProviderContractError`, policy refusal и `CostCapBlocked` не fallback-ятся.
401/403 ставят key-error без retry. Один provider вызывается максимум один раз
на LLM-pass. Snapshot фиксируется: text — на начало хода, voice — на
`build_session_pipeline`, Kora — в immutable `RunSpec`.

Карта потребителей:

| Потребитель | Маршрут |
| --- | --- |
| Voice dispatcher | settings snapshot `[primary, fallback]` на reconnect |
| Text dispatcher + compact | тот же snapshot через `RoutedLLMClient` |
| speakify Play-backstop | pinned direct Gemini; не маршрутизируется |
| Kora | отдельная settings-секция → `RunSpec`; не provider dispatcher-а |

⚠️ **Residual cost-поверхность.** `speakify` — единственный LLM-вызов в скоупе
этой спеки, не посаженный под дневной cost cap и не проходящий через
`RoutedLLMClient`: он бьёт напрямую в Google Gemini (`speakify.py:14`,
`speakify_model`, `config.py:31`) на каждый грязный Play-текст. Для v1 это
допустимо — вызов срабатывает только по ручному тапу Play на грязном тексте,
а не автоматически в live-пути. Тем не менее это сознательно оставленная
неограниченная LLM-трата; явный владелец её ограничения зафиксирован на M+1:
посадить speakify под тот же `GuardedLLMClient` cost cap, что и text-канал. До
M+1 UI Play-пути не обещает «бесплатной» озвучки.

Порядок текстовых обёрток:

```text
route catch C4.2 → 200/degraded
  └─ RoutedLLMClient (snapshot + fallback policy)
       ├─ GuardedLLMClient(primary adapter)  — reserve cost before call
       └─ GuardedLLMClient(fallback adapter) — only ProviderUnavailable
```

Каждая платная попытка резервирует cost независимо. `CostCapBlocked` обрывает
цепочку; исчерпанный `ProviderUnavailable` доходит до уже готового C4.2 catch.

**Breaker migration — отдельный атомарный шаг AI-2.** Новый
`KeyedCircuitBreaker` живёт на host и хранит состояние по
`(provider_id, model_id)`, общее для voice/text. Миграция идёт не in-place:

1. ввести keyed API и characterization tests старого Р-14 поведения;
2. подключить voice/text через compatibility adapter, сохранив cooldown/RPM/RPD;
3. на deploy не переносить позиционные open-states (fail-open один раз), но
   записать `breaker_state_reset reason=key_migration`; это безопаснее неверно
   приписать trip другой модели;
4. parity + frozen Р-14 suite доказывают failover; только затем удалить
   `CircuitBreaker(len(tiers))` и `_tier_probe`.

`ProviderRegistry` даёт для каждого provider фабрики text-tools adapter и
Pipecat voice service. Text adapters принимают `httpx.MockTransport`,
нормализуют общий completion/tool shape и поднимают
`ProviderContractError` отдельно от транзиентного `ProviderUnavailable`.

| Provider | Text | Voice | Model source |
| --- | --- | --- | --- |
| OpenRouter | OpenAI-compatible httpx adapter | `OpenRouterLLMService` | OpenRouter models API |
| Anthropic | существующий `AnthropicLLMClient` | `AnthropicLLMService` | `/v1/models` |
| Google AI Studio | direct Gemini adapter с tools | Pipecat Google service | Google models API |

`ModelInfo` ограничен полями `provider_id, model_id, display_name,
supports_tools, available`; pricing/quality scoring в v1 нет. `/test` делает
минимальный запрос без пользовательского контекста/tools, rate-limited;
upstream body санитизируется. Неизвестный provider → 404, invalid field/model
→ 400, CAS → 409, test upstream timeout/unavailable → 502/504. Модель без
`supports_tools` нельзя выбрать в dispatcher route.

На каждый LLM-pass журналируется только безопасная метаинформация:

```json
{"provider":"openrouter","model":"google/gemini-3.5-flash",
 "fallback_index":0,"latency_ms":820,"settings_revision":4,"outcome":"ok"}
```

Ключи, user text и upstream request/response не журналируются. UI ответа
показывает компактно provider/model/latency. Смена settings во время хода,
voice session или Kora run применяется только к следующему snapshot.

## 5. API — сводка изменений

```text
POST /api/tts                                — role выбирает reference_id (disp/kora); контракт ответа не меняется
GET  /api/settings/ai                        — providers/key status/routing/Kora/voice read-only status
PUT  /api/settings/ai/routing                — primary/fallback + expected revision
PUT  /api/settings/ai/kora                   — defaults + expected revision
PUT  /api/settings/ai/providers/{provider}   — enabled/selected_model + expected revision
POST /api/settings/ai/providers/{provider}/test
GET  /api/settings/ai/providers/{provider}/models
GET  /api/threads/{id}/files/{file_id}       — новый: отдача доставленного файла (attachment)
POST /api/threads/{id}/radio-sessions        — новый: manifest + scoped media-cookie
GET  /api/radio/{session_id}/segments/{n}    — новый: один bounded streaming MP3
DELETE /api/radio-sessions/{session_id}      — новый: stop/cleanup
```

Требования: bearer С5 на settings/control/download-роутах, CSRF на мутациях;
scoped HttpOnly-cookie на
media-GET (§4.6); неизвестный `file_id` → 404; blob исчез/повреждён → 410;
вторая радио-сессия → 409; `from` за пределами → 416; не-текстовый файл →
415; malformed UTF-8/пустой нормализованный текст → 422.

`POST /api/threads/{id}/message` сохраняет URL и тип существующего поля
`reply: string`. Новый fast-path при `RouteDecision=kora_*` возвращает
`{"reply":"","routed_to":"kora","accepted":true}` вместо вызова
dispatcher LLM: поля `routed_to/accepted` additive, `reply` не становится
nullable. Клиент считает `accepted && routed_to == "kora"` успешной доставкой,
подтягивает уже сохранённый server-side user-entry и не рисует assistant-entry
для пустого `reply`. Legacy-клиент также не должен рисовать пустой bubble;
контракт закрепляется route+client тестом.

## 6. Крайние случаи

1. Свитч голоса посреди barge-in: Interruption чистит очередь арбитра →
   bookkeeping становится `UNKNOWN`; следующий item любой роли безусловно
   выставляет свой голос. Сброс в `disp` запрещён: он соврал бы, если реальный
   Fish WS остался на Корином голосе.
2. Кора и диспетчер говорят «одновременно»: арбитраж уже решён — SPEAK прыгает
   вперёд хвоста диспетчера (`arbiter.py:69-80`), свитч только удорожает
   границу (~100–300 мс WS-реконнект). Майлстоуны в очередь при живом
   диспетчер-хвосте вообще не встают (§4.3-политика).
3. `FISH_VOICE_KORA`/narrator не заданы → соответствующая роль резолвится в
   голос диспетчера,
   поведение = сегодняшнее (фича выключена без флага).
4. Кэш WAV: старые записи под старыми ключами остаются валидными (дисп-голос
   не менялся); корины тексты после включения фичи синтезируются заново под
   новым ключом — деградации нет, только холодный кэш.
5. `provide_answer` False после решения роутера «kora» (Кора перестала ждать
   в этот самый момент) → реплика в тот же ход уходит диспетчеру; пользователь
   разницы не видит.
6. `provide_comment` принят до terminal result → result не terminalize-ит
   задачу, пока follow-up не получит свой result; comment после закрытия queue
   → dispatcher-fallback. Identity-guard не даёт late comment попасть в новый
   task.
7. Реплика «стоп» во время `awaiting_answer` или reply-window → диспетчер,
   который штатно зовёт `request_cancel` — отмена не может быть съедена
   роутером.
8. Кора зовёт `deliver_file` на secret, файл вне project/extra roots,
   directory/гигантский/исчезнувший файл → deny категорией. Частичный snapshot
   остаётся только tmp и удаляется.
9. Исходник удалён после доставки → скачивание всё равно работает из immutable
   blob. Сам blob исчез/не совпал размер → 410, не попытка читать старый путь.
10. Радио: browser pause может дочитать текущий segment, но следующий GET не
    создаётся; cost-overshoot ≤ `radio_segment_max_chars`.
11. Fish WS упал посреди segment → current index не двигается; retry повторяет
    этот segment. Возможен повтор последних миллисекунд звука, пропуск текста
    запрещён.
12. Media-cookie истекла во время pause → следующий segment даёт 401; клиент
    делает новый authenticated control-POST с сохранённого paragraph, не
    показывает prompt API-token повторно без фактического bearer-401.
13. Одновременно Play-кнопка и радио: существующий `nowPlaying`-синглтон
    (`app.js:497`) расширяется на радио — старт любого аудио стопит прежнее.
14. Милстоун-текст с plain-text инъекцией может пройти `speakable()` и
    прозвучать голосом Коры. Это не «без последствий» для social engineering,
    поэтому distinct voice — обязательный DoD; технических полномочий речь не
    получает, критические действия остаются через approval-контракт.
15. Два одинаковых файла в разных тредах делят blob по sha256, но имеют разные
    случайные `file_id`; знание id из треда A не даёт download через URL треда B.
16. Radio-session natural-complete, explicit stop, disconnect и expiry сходятся
    в один idempotent cleanup; semaphore и active-session slot освобождаются
    ровно один раз.
17. Settings CAS conflict → UI reload+diff, без auto-merge; stale model cache
    старше 30 дней не выдаётся как каталог; ручной `model_id` остаётся.
18. Breaker deploy сбрасывает старые positional open-states с audit event;
    состояние не может переехать на другую `(provider, model)`.
19. Второй клиент стартует радио при живом voice WS → общий Fish lease даёт
    409 до создания session/WS; подтверждённый voice disconnect освобождает
    lease и повторный control-POST проходит.

## 7. Скоуп реализации

| Слайс | Содержание | Зависимости |
| --- | --- | --- |
| **KV-1 — голос Коры в тракте** | `SynapseSpeakFrame`, обязательные role/utterance id в `QueueItem`, voice-aware arbiter/cache, exact TTS `context_id` registry, `/api/tts` role→reference_id | P3b |
| **KV-2 — речевой контракт** | абзац в `_system_prompt`, модуль `speakable.py` + тесты на корпусе реальных кор-текстов, Play-путь: чистый текст мимо speakify | — |
| **KV-3a — присутствие + hard-router** | `kora_said`, dedupe/presence + max-silence heartbeat, reply-window, `RoutableUserAggregator` + frame-gate voice и прямой HTTP fast-path | KV-1, KV-2, P3a |
| **KV-3b — interactive comments** | real CLI probe, persistent SDK reader, bounded comment queue, pending-results FSM, explicit vocatives/reply-window router, `to:"kora"` feed + journal | KV-3a; probe — go/no-go |
| **KV-4 — deliver_file** | SDK MCP probe, project/extra-root allowlist + shared secret policy, immutable artifact snapshot/blob store, random per-thread ids, download auth-fetch, file-card | Фаза 0 С5 + С6 |
| **KV-5 — радио** | Fish WS-клиент, shared voice/radio lease, deterministic manifest, bounded segment stream, radio-session FSM, scoped media-cookie, mini-player | KV-4, С5, HTTPS staging |
| **AI-1..AI-3** | store/UI → routed runtime/keyed breaker → Google adapter | строго по единой таблице §0.2 |

Каждый слайс — отдельный tero-ран, зелёная суита, замороженные тесты не
редактируются. KV-1/KV-2 независимы; KV-3a после них; KV-3b только после
protocol probe; KV-4 независим после С5/С6; KV-5 последним.

**Фазирование для качества реализации.** Совокупная площадь поверхности велика
(6 KV-слайсов + AI-1…3 + breaker-migration + 3 adapter-а + 5 живых probe +
С5/С6). Чтобы не просесть на последних слайсах, рекомендуется явная разбивка
контура приёмки:

- **Контур A (v1-core):** KV-1, KV-2, KV-3a, AI-1, AI-2 — голос Коры, речевой
  контракт, детерминированный роутер, settings store + routed runtime. Этого
  достаточно, чтобы закрыть дыры 1-3 живого прогона (§1) без Files/Радио.
- **Контур B (v1-files):** KV-3b, KV-4 — interactive comments + deliver_file.
  Зависит от положительных probe P1/P2 и Контур A.
- **Контур C (v1-radio):** KV-5, AI-3 — радио + Google adapter. Зависит от
  Контур B, probe P4/P5, HTTPS staging.

Приёмка §10 не различает «обязательно для релиза v1-core» от «обязательно для
полного v1-контура»; implementation-plan обязан пометить acceptance-пункты 1-6,
9, 11-14 как Контур A, 4-5 (comment-часть) + deliver_file acceptance (6) как
Контур B, радио-acceptance (7) + Google adapter как Контур C. Релиз v1-core без
B/C допустим и не нарушает приёмку, если B/C явно отнесены к следующему контуру.

## 8. Не входит в v1

- семантический (LLM/embedding) роутер реплик — только детерминированные
  правила §4.4;
- комментарий Коре без активной задачи; такой ход всегда отвечает диспетчер;
- озвучка thinking-блоков и tool-результатов Коры;
- второй одновременный TTS-поток (радио живёт вне голосового пайплайна);
- изменение голосов из UI в v1; read-only статус входит сюда, редактор имеет
  явного владельца `Settings → Voice` M+1 (§4.7);
- просмотр/скачивание произвольных файлов машины из UI (только immutable
  artifacts, созданные `deliver_file`);
- очередь/плейлист радио, скорость воспроизведения, точный highlight
  параграфа;
- пуш-канал сервер→клиент (SSE/WS) для ленты — лента остаётся на поллинге.

## 9. Риски

| Риск | Решение |
| --- | --- |
| WS-реконнект на каждый свитч голоса рвёт темп диалога | Свитч только на границе реплик; майлстоуны троттлятся; live-probe меряет switch latency. Go/no-go KV-1: p95 ≤ 500 мс, иначе отдельный двух-сервисный дизайн, не скрытый префикс |
| Отравление TTS-кэша при двух голосах | Все cache APIs получают реальный `voice_id`; observer snapshot-ит id на `TTSStartedFrame`; матрица late-text + switch доказывает два ключа |
| `on_end_of_turn` решил Kora, но frame уже запустил LLM | Роутинг живёт в `VoiceRouteProcessor` перед `pre_hook/llm_switcher`; успешный Kora-route поглощает context-frame; P3a считает вызовы fake LLM |
| Reordering приписал reply-window чужой реплике | `utterance_id` идёт в frame/QueueItem, а TTS `context_id` даёт exact mapping; FIFO запрещён, unknown id fail-closed |
| Markdown-heavy run снова оставляет минуты тишины | Детерминированный heartbeat по `kora_presence_max_silence_s` при idle; чистый milestone сбрасывает таймер |
| Речь Коры — social-engineering канал из workspace | `speakable()` не объявляется safety-фильтром; distinct voice обязателен, речь не входит в dispatcher context и не получает полномочий; шаблон остаётся fallback-ом |
| Reply-window уводит фразу не тому собеседнику | Одно короткое окно только после реально прозвучавшей Коры; «Флоу/диспетчер» и control first-token имеют приоритет; feed явно показывает `to:kora` |
| Интерактивный comment ломает lifecycle SDK | Отдельный real CLI go/no-go probe; один reader, pending-results FSM, bounded queue, identity guards |
| MCP-тул зашадовлен `allowed_tools=[]` | Probe на фактической pinned SDK; trusted handler сам authority независимо от hook/allowlist |
| Download-роут = новый экспорт-канал | Source только project root + admin-configured extra roots; versioned secret policy; immutable snapshot, random per-thread id, bearer auth-fetch; С5+С6 — prerequisites |
| Fish-счёт на книгах | Bounded segment вместо недоказуемого pause/backpressure; no prefetch, caps, одна session/WS, journal по каждому segment |
| Media-cookie создаёт новую auth-поверхность | HttpOnly + SameSite=Strict + path scope + TTL; выпускается только bearer+CSRF POST; token не в URL; media GET read-only |
| Радио в мик → STT слушает книгу / два Fish WS | UX-взаимоисключение на клиенте + общий server-side `FishSessionLease` для voice/radio (§4.6) |
| `<audio>` Range-запросы ломают chunked-сегмент | Media-роут игнорирует `Range` (200 без `Accept-Ranges`/`Content-Length`) или 416; точное поведение фиксирует probe P5 на Chrome+Safari; зависший элемент блокирует радио |
| Voice-пайплайн заблокирован ожиданием доставки Kora | `process_voice_turn` await-ит ack под `kora_comment_delivery_timeout_s` (2.0); таймаут → dispatcher-fallback; p95 ack ≤ 500 мс в go/no-go KV-3a, иначе асинхронная delivery |
| `context_id` не уникален/отсутствует → presence молча деградирует | Single-point-of-failure: probe P3b проверяет presence+uniqueness на 20 чередованиях; невыполнение — no-go KV-1/KV-3b, не FIFO-fallback |
| iOS/PWA: MP3 chunks, cookie и фон ведут себя иначе | Нативный `<audio>` выбран ради media-session; обязательный live-DoD Safari/iPhone + Chromium до принятия KV-5 |
| Два источника runtime-конфига | Жёсткое владение §4.7: bootstrap/secrets в `SynapseConfig`, UI runtime только в `AISettingsStore`; one-shot migration |
| Breaker-рефактор ломает Р-14 | Characterization/frozen tests, compatibility adapter и явный fail-open reset positional state (§4.8) |
| 409 затирает настройки второго клиента | Reload+diff+explicit overwrite; auto-merge и silent retry запрещены |
| Model API недоступен или cache протух | 6h fresh, 30d stale-if-error, timestamp и ручной model id |

## 10. Приёмка

1. Реплики Коры (вопрос, completion, майлстоун) звучат сконфигурированным
   `FISH_VOICE_KORA`, диспетчер — прежним; Play-кнопки ленты честны по ролям.
2. Кора в живом прогоне говорит разговорно; markdown-мусор в озвучку не
   попадает: чистый текст — напрямую (без Gemini-вызова), грязный — speakify
   (Play) / молчание (live).
3. Во время рана при молчащем диспетчере Кора рассказывает прогресс не чаще
   раза в `kora_milestone_min_gap_s`; если спикабельного текста нет, RUNNING-run
   не остаётся без Kora-audio дольше `kora_presence_max_silence_s` (+ scheduler
   tolerance) благодаря heartbeat. Финал — её собственная фраза, если чистая,
   иначе шаблон; один SDK TextBlock не звучит дважды.
4. При `awaiting_answer` голосовой ответ доставляется Коре без единого
   LLM-вызова: route processor поглощает context-frame до `pre_hook`, журнал
   содержит `route_to_kora`, fake/real dispatcher-pass = 0; «стоп» при этом
   уходит диспетчеру и отменяет задачу.
5. После реально прозвучавшего milestone короткий ответ и явное «Кора, …»
   попадают в тот же SDK-process через comment queue без dispatcher LLM;
   «Флоу, …» отвечает диспетчер. Comment до terminal result выполняется до
   terminalization задачи.
6. `deliver_file` для не-секретного regular file из project root или явно
   разрешённого extra root (default Downloads)
   создаёт карточку и immutable blob; скачивание после удаления исходника
   byte-for-byte совпадает с snapshot. secret/outside-root/directory/сверх-кап
   — deny категорией без пути; secret-policy corpus проходит целиком.
7. Кнопка «Озвучить» на md-файле стартует радио сконфигурированным
   `FISH_VOICE_NARRATOR`:
   первый звук ≤ 2 с на живом прогоне; после pause не синтезируется следующий
   segment, перерасход ≤ segment cap; «продолжить» идёт с сохранённого
   paragraph; вторая session → 409. При живом voice WS radio также получает
   409 и не открывает второй Fish WS; после disconnect повтор проходит.
   Сегмент доигрывается до конца в Chrome и iPhone Safari без зависания
   элемента на Range/буферизации.
8. Control/download API недоступен без bearer С5; media GET недоступен без
   scoped cookie, а cookie нельзя получить без bearer+CSRF start. API token,
   media token и исходные пути отсутствуют в URL/feed/deny-текстах.
9. Полная суита зелёная; замороженные тесты не тронуты; NO-EXFIL-тесты
   регидрации (`loop.py:64-66`) остаются якорем — новые kinds (`file`,
   `to:"kora"`) в LLM-историю не попадают.
10. Live-матрица проходит на desktop Chromium и iPhone Safari: смена голосов,
    background native audio, scoped cookie, stop/resume и мик↔радио.
11. Settings UI показывает OpenRouter/Anthropic/Google и отдельную Кору;
    voice ids видны read-only как env-backed. Ключи нигде не возвращаются,
    кроме необратимой маски.
12. Первый запуск атомарно мигрирует bootstrap seed; после этого изменение
    `tier1_model`/`tier2_model` не меняет runtime. Voice/text с одним snapshot
    выбирают один primary/fallback маршрут.
13. Timeout/429/5xx дают один fallback; 400/contract/policy/CostCapBlocked —
    нет. Исчерпанная цепочка проходит через C4.2 как `200 + degraded: true`.
14. CAS conflict показывает server-vs-draft; рестарт восстанавливает store;
    breaker trip по ключу, созданный voice-каналом, виден text-каналу.

## 11. Контракты новых типов и функций

Ниже — публичная поверхность модулей. Имена могут механически уточниться в
implementation-plan, но входы, выходы и authority-boundaries фиксированы.

### 11.1. Голос и речевая пригодность

```python
VoiceRole = Literal["disp", "kora"]

@dataclass(kw_only=True)
class SynapseSpeakFrame(TTSSpeakFrame):
    voice_role: VoiceRole
    utterance_id: str

def resolve_voice_id(role: VoiceRole, cfg: SynapseConfig) -> str: ...
def speakable(text: str, *, max_chars: int) -> bool: ...
```

`resolve_voice_id` — единственная таблица role → Fish reference id. Пустой
Kora-id возвращает dispatcher-id; неизвестная роль — `ValueError`, не fallback.
`SynapseSpeakFrame` в `__post_init__` фиксирует
`append_to_context=False`; передать True — programmer error. `kw_only=True`
нужен, потому что у базового dataclass уже есть default-поле.
`speakable` pure/total: любой input превращается в bool без исключения и без
I/O; пустая/whitespace строка всегда False.

```python
class ArbiterPolicy:
    def push_dispatcher_text(self, text: str) -> None: ...
    def push_speak(self, text: str, voice_role: VoiceRole,
                   utterance_id: str) -> None: ...
    def has_pending(self, source: str) -> bool: ...

class VoiceOutputState:
    def note_dispatcher_generation(self, active: bool) -> None: ...
    def note_item_enqueued(self, voice_role: VoiceRole) -> None: ...
    def note_tts_stopped(self) -> None: ...
    def interrupt(self) -> None: ...
    def is_idle(self) -> bool: ...

class SynapseHost:
    def speak(self, text: str, *, voice_role: VoiceRole,
              utterance_id: str | None = None,
              on_started: Callable[[], None] | None = None) -> None: ...
    async def push_speak_frame(self, text: str, *,
                               voice_role: VoiceRole,
                               utterance_id: str) -> bool: ...

class TTSCorrelationRegistry:
    def bind(self, context_id: str, utterance_id: str) -> None: ...
    def pop_started(self, context_id: str) -> str | None: ...
    def discard(self, context_id: str) -> None: ...
    def interrupt(self) -> None: ...

class TTSCache:
    def get(self, text: str, *, voice_id: str) -> bytes | None: ...
    def put_wav(self, text: str, wav: bytes, *, voice_id: str) -> None: ...
    def put_pcm(self, text: str, pcm: bytes, sr: int, channels: int,
                *, voice_id: str) -> None: ...
    def assemble(self, text: str, splitter, *, voice_id: str) -> bytes | None: ...
```

`VoiceOutputState` хранит FIFO ролей для item-ов между арбитром и
`TTSStoppedFrame`; interruption атомарно чистит FIFO. `is_idle` истинно только
при закрытом dispatcher generation и пустом FIFO. Это transient telemetry для
presence-policy, не новый источник порядка аудио: порядок по-прежнему задаёт
арбитр/TTS.

`SynapseHost.speak` может сгенерировать opaque id для системной/dispatcher
реплики без callback; вопрос, milestone и final всегда передают стабильный
Kora `utterance_id` явно. `push_speak_frame=True` означает только acceptance и
не открывает окно.
TTS adapter bind-ит `context_id`, созданный Pipecat для конкретного
`TTSSpeakFrame`, к `SynapseSpeakFrame.utterance_id` до старта audio context.
Observer получает `context_id` прямо из `TTSStartedFrame` и делает exact
`pop_started`; общей FIFO id нет. `on_started` вызывается ровно один раз и
никогда для sync/offline fallback, exception, silent drop или interruption до
старта.
Unknown/duplicate `context_id` журналируется и fail-closed не открывает окно.
Это подтверждает начало server-side audio-run, но не физическое восприятие
человеком при WebRTC loss; абсолютного playback-ack текущий transport не даёт.
Поэтому окно короткое, control/vocative правила имеют приоритет, а метрика
разделяет `accepted` и `started`.

### 11.2. Presence и маршрутизация

```python
@dataclass(frozen=True)
class KoraReplyWindow:
    task_id: str
    thread_id: str
    utterance_id: str
    opened_ts: float
    expires_ts: float

@dataclass(frozen=True)
class RouteSnapshot:
    channel: Literal["voice", "http"]
    task_id: str | None
    task_status: TaskStatus | None
    owner_thread_id: str | None
    current_thread_id: str | None
    awaiting_answer: bool
    reply_window: KoraReplyWindow | None
    now: float

@dataclass(frozen=True)
class RouteDecision:
    target: Literal["dispatcher", "kora_answer", "kora_comment"]
    reason: str
    utterance_id: str | None = None

class VoiceRouter:
    def route(self, text: str, snapshot: RouteSnapshot) -> RouteDecision: ...

@dataclass(kw_only=True)
class VoiceTurnEnvelope:
    turn_id: str
    transcript: str
    user_message_index: int

class VoiceRouteProcessor(FrameProcessor):
    async def process_voice_turn(self, frame: LLMContextFrame,
                                 envelope: VoiceTurnEnvelope) -> None: ...

class KoraRunner:
    async def provide_comment(self, text: str, *, task_id: str,
                              thread_id: str) -> bool: ...
```

`KoraRunner` (`kora.py:402-867`) уже существует с `start`/`request_cancel`/
`_run`/`_stream`/`_build_options`/`_gate_decision`/`_pretool_hook`/
`_handle_question` и существующим `provide_answer` (`kora.py:474`).
Единственный **новый** метод этой спеки — `provide_comment`; контракт ниже
показывает только его, а не весь класс.

```python
```

Reply-window и `last_clean_utterance` живут в отдельном transient
`KoraInteractionState`, не в persisted `TaskStore`: `note_utterance`,
`mark_spoken`, `open_window`, `consume_window`, `close_for_task` — все
синхронны и identity-guarded. `VoiceRouter` state не мутирует. Окно
consume-ится только после успешной доставки решения, чтобы failed delivery
могла честно уйти диспетчеру тем же ходом.

`provide_comment` не принимает comment для чужого task/thread, terminal task,
закрытой SDK-сессии или полной queue. Текст не нормализуется и не обрезается;
верхний cap пользовательского хода применяется общим HTTP/STT входом до
router-а. Возврат происходит не позднее
`kora_comment_delivery_timeout_s` (bootstrap): True только при подтверждённой
записи в transport, False при таймауте/гонке/полной queue. False влечёт
dispatcher-fallback тем же ходом (case 6).

`VoiceRouteProcessor` — единственная voice authority, решающая, пройдёт ли
user context-frame дальше. На успешном Kora-route он проверяет, что
`context.messages[user_message_index]` — точная user-запись envelope, удаляет
её и не вызывает `push_frame`. На dispatcher/fallback снимает служебный
envelope и пушит исходный frame ровно один раз. Вызов router-а из одного лишь
STT event callback контракту не соответствует.

### 11.3. Artifact delivery

```python
@dataclass(frozen=True)
class FileArtifact:
    file_id: str
    thread_id: str
    task_id: str
    blob_sha256: str
    name: str
    title: str | None
    size: int
    mime: str
    created_ts: float

class ArtifactStore:
    async def publish(self, source: Path, *, project_root: Path,
                      extra_roots: tuple[Path, ...], thread_id: str, task_id: str,
                      title: str | None) -> FileArtifact: ...
    def resolve(self, thread_id: str, file_id: str) -> tuple[FileArtifact, Path] | None: ...
```

`publish` выполняет blocking file I/O через `asyncio.to_thread`, но commit
registry/feed идёт в event-loop после готового blob. Транзакционный порядок:
validate/open → copy+hash temp → fsync/atomic blob install → atomic registry
persist → append feed. До registry commit файл пользователю не виден. Ошибка
feed append после registry commit чинится идемпотентным replay карточки при
следующем чтении треда; blob не теряется. Reconciliation-алгоритм: при чтении
треда `ThreadStore` сравнивает множество `file_id` из registry этого треда с
`kind="file"` entry в feed; любой registry-artifact без соответствующей feed
entry достраивается in-memory карточкой из registry-метаданных и не дублирует
уже существующую feed entry. Feed остаётся append-only, registry — источник
правды по существованию артефакта.

MCP handler получает `thread_id/task_id` из immutable run snapshot, никогда из
tool arguments. `name = Path(source).name` санитайзится для Content-Disposition;
CR/LF, slash и control chars удаляются. `title` — display-only, cap 160.
`project_root/extra_roots` также приходят из trusted bootstrap/`RunSpec`, не из
tool arguments. Общий `SecretPathPolicy` проверяется после resolve и повторно
по фактически открытому fd/path там, где ОС даёт такую проверку.

### 11.4. Radio

```python
@dataclass(frozen=True)
class RadioSegment:
    index: int
    text: str                 # server-only
    from_paragraph: int
    to_paragraph: int
    chars: int

@dataclass
class RadioSession:
    id: str
    token_digest: bytes       # raw token не персистится/не логируется
    thread_id: str
    file_id: str
    blob_sha256: str
    segments: tuple[RadioSegment, ...]
    current_index: int
    expires_ts: float
    state: Literal["ready", "streaming", "stopped", "completed", "expired"]

def normalize_radio_text(raw: str) -> list[str]: ...       # paragraphs
def build_radio_manifest(paragraphs: list[str], max_chars: int) -> tuple[RadioSegment, ...]: ...

class FishRadioClient:
    async def stream_mp3(self, text: str, *, voice_id: str,
                         model: str) -> AsyncIterator[bytes]: ...

class FishSessionLease:
    async def acquire(self, kind: Literal["voice", "radio"], owner_id: str) -> bool: ...
    async def release(self, kind: Literal["voice", "radio"], owner_id: str) -> bool: ...

class RadioSessionManager:
    def start(...) -> RadioSession: ...
    def authorize_media(self, session_id: str, cookie: str) -> RadioSession | None: ...
    async def stream_segment(self, session_id: str, index: int) -> AsyncIterator[bytes]: ...
    async def stop(self, session_id: str, reason: str) -> bool: ...
```

`normalize_radio_text` и manifest pure/total для валидного Unicode. Session и
raw media-token transient: рестарт требует новый control-POST. В cookie хранится
raw random token, в памяти session — только HMAC/sha256 digest; сравнение
constant-time. Radio text и MP3 bytes не пишутся в journal.

## 12. Машины состояний и атомарность

### 12.1. Интерактивная Kora-сессия

| Событие | Условие | Переход / эффект |
| --- | --- | --- |
| stable `kora_said` принят в live TTS | presence policy allow | open/replace reply-window |
| user route = `kora_comment` | окно/explicit vocative + RUNNING | reserve pending query, consume window |
| SDK writer ack | transport write ok | caller получает True, ждём следующий result |
| SDK writer fail | есть deferred result | снять reservation, finalize deferred result, caller False |
| `ResultMessage` | pending после decrement > 0 | сохранить/обновить deferred result, не terminalize |
| `ResultMessage` | pending после decrement == 0 | один terminal KoraEvent, закрыть window/queue |
| cancel/supersede/deadline | любой active state | закрыть window/queue, cancel SDK, terminalize identity-guarded |

Приоритет close-триггеров reply-window (в порядке убывания): (1)
cancel/supersede/deadline; (2) terminal result при `pending_queries == 0`;
(3) начало речи диспетчера; (4) успешная маршрутизация одного пользовательского
хода; (5) TTL `kora_reply_window_s`. В окне пересечения result-driven close
(2) приоритетнее TTL (5): даже если wall-clock окна ещё не истёк,
`pending_queries == 0`-result закрывает его, и опоздавший комментарий уходит
диспетчеру как dispatcher-fallback (case 6), а не в закрытую SDK-сессию. Это
зафиксированный случай теста §14.1.4 (late result).

Все `pending_queries/deferred_result/queue_open` переходы защищены одним
`asyncio.Lock`. Запрещено держать lock во время network/SDK await; reservation
делается под lock, запись — снаружи, commit/fail — снова под lock.

### 12.2. Artifact publish

`FileArtifact` появляется только после полного blob. Blob content-addressed и
может остаться orphan при crash между install и registry commit; это безопасная
утечка диска, которую будущий GC удалит. Обратное состояние (registry указывает
на неполный blob) запрещено порядком commit. Registry write — unique tmp +
`os.replace`, как TTS cache; один общий `ArtifactStore` сериализует registry
commit lock-ом.

### 12.3. Radio session

```text
READY --GET current--> STREAMING --Fish finish--> READY(next)
  |                         |                        |
  | stop/ttl                | error/disconnect       | last segment
  v                         v                        v
STOPPED/EXPIRED         READY(same index)         COMPLETED
```

Только `STREAMING → READY(next)` двигает позицию. Повторный concurrent GET
текущего segment → 409. Cleanup идемпотентен и через `finally` освобождает
global WS semaphore. TTL проверяется на каждом control/media действии и
периодическим reap без отдельного долгоживущего background task в тестах.

## 13. Наблюдаемость и приватность

Новые journal events (payload без raw user text, абсолютных путей, токенов и
radio content):

| Event | Поля |
| --- | --- |
| `kora_milestone_spoken` | task_id, thread_id, utterance_id, chars |
| `kora_milestone_dropped` | task_id, utterance_id, reason=`dirty|throttled|dispatcher_busy|no_output|duplicate` |
| `kora_reply_window_opened/closed` | task_id, utterance_id, reason |
| `route_to_kora` | task_id, thread_id, reason, utterance_id? |
| `kora_comment_sent/failed` | task_id, reason, queue_depth |
| `artifact_published/denied/downloaded` | task_id, thread_id, file_id?, size?, category? |
| `radio_session_started/stopped` | session_id, file_id, start paragraph, segments, reason? |
| `radio_segment_started/completed/failed` | session_id, index, chars, audio_bytes?, latency_ms?, reason? |

`AUTH_FAILURE` остаётся событием С5. Cookie value, API bearer, blob path/hash и
исходный path не логируются. `blob_sha256` является server-internal metadata и
не идёт в feed/API. Метрики для live-DoD: voice-switch latency p50/p95, Kora
milestone accepted/dropped, route counts по reason, Fish time-to-first-byte,
chars completed per segment и artifact storage errors.

## 14. Верификация и protocol probes

### 14.1. Автоматические тесты

1. **Voice/cache unit:** role обязателен; exact Pipecat update-frame идёт перед
   speak frame; interruption + следующий Kora item снова ставит Kora voice;
   late `TTSTextFrame` после switch пишет под voice snapshot; одинаковый текст
   в двух голосах даёт два WAV; acceptance без `TTSStartedFrame` не открывает
   окно; `context_id ↔ utterance_id` переживает reordering dispatcher/Kora,
   а unknown/duplicate id не открывает его; `on_started` вызывается ровно один
   раз и не вызывается на fallback/drop/exception/interruption-before-start.
2. **Speakable corpus:** реальные строки из приложенного прогона (таблицы,
   Markdown, paths, ids) молчат; короткие разговорные русские/английские фразы
   проходят; fuzz Unicode/пустые inputs не бросают исключений.
3. **Presence/router/frame-gate:** hard question, живое/истёкшее/чужое reply-window,
   explicit vocatives, first-token control, terminal race, dispatcher busy,
   duplicate utterance, markdown-only run > max silence. Для каждого — target,
   journal reason и ровно одна feed entry. Отдельный pipeline test доказывает:
   successful Kora delivery удаляет exact user context entry и не вызывает
   pre-hook/fake LLM; failed delivery сохраняет entry и вызывает fake LLM один
   раз; `_on_end_of_turn` не нужен для правильного ordering. **Feed-dedup:**
   router пишет user-feed, processor пишет journal/context — две ownership-
   границы; тест доказывает ровно одну `to:"kora"` feed entry на ход даже при
   retry/переотправке, и отсутствие feed-записи при поглощённом frame без
   доставки. HTTP fast-path сохраняет `reply` строкой (`""`), добавляет routing
   fields и клиент не рисует пустую assistant-entry.
4. **Interactive fake SDK:** comment до первого result; два comments; queue
   full; writer failure с deferred result; cancel/supersede; late result.
   Проверка: один reader, terminal event ровно один, чужой task не мутирован.
5. **Artifact unit/API:** relative/absolute source, project/Downloads source,
   outside-root, каждый secret-dir/name/stem/suffix (включая case variants),
   env-template allow cases, final symlink, directory, file growing past cap, duplicate content,
   cross-thread id, restart registry, deleted source, corrupt/missing blob,
   malicious filename, policy exception. Download требует bearer и совпадает
   byte-for-byte; deny не содержит path, policy exception fail-closed.
   **Feed-reconciliation:** registry-artifact без feed-entry достраивается
   in-memory при чтении треда (симуляция упавшего feed-append), уже
   существующая feed-entry не дублируется.
6. **Radio pure/client/API:** Markdown normalization golden corpus; segment cap
   и stable manifest; media GET без/с wrong cookie → 401; cookie path/flags;
   concurrent/out-of-order/retry; pause означает отсутствие следующего GET;
   Fish mock error не двигает index; stop/ttl освобождает slot/lease; live voice
   lease блокирует radio до disconnect; конкурентные voice/radio acquire дают
   ровно одного победителя и максимум один mock WS.
7. **NO-EXFIL regression:** `kora_said`, file cards, `to:kora`, manifests и
   radio events не появляются в `history_from_feed` / dispatcher messages.
8. Полная существующая суита запускается без редактирования frozen tests.
9. **AI settings/store:** one-shot migration, atomic recovery, CAS conflict UI
   contract, secret absence, model-cache TTL/stale expiry/invalidation.
   **Seed validity:** bootstrap seed (`tier1/tier2_model`) мигрируется только
   если model id проходит базовую shape-проверку и не является известным
   фиктивным SKU (`gemini-3.5-flash`, `claude-haiku-4-5`); фиктивный seed →
   empty `selected_model` + warning в journal, чтобы первый `/test` не упал
   молча на 404.
10. **Routing/breaker:** provider failure matrix, shared keyed state voice↔text,
    deploy reset audit, snapshot isolation и Р-14 characterization/frozen suite.

Сетевые Fish/SDK тесты — только через fake transport/WS server; unit suite не
требует ключей и не несёт расхода.

### 14.2. Обязательные живые probes до implementation-plan

1. **SDK bidirectional:** pinned version + реальный CLI, comment во время
   tool-heavy response; зафиксировать message/result ordering, hooks, session id
   и число subprocess. Это go/no-go KV-3b.
2. **SDK MCP:** `deliver_file` виден и вызывается при текущей permission config;
   фактическое tool name проходит PreToolUse; handler deny не обходится
   `allowed_tools`. Это go/no-go KV-4.
3. **Pipecat route bypass:** `RoutableUserAggregator` поверх реального
   `LLMUserAggregator` + instrumented `VoiceRouteProcessor` + fake switcher.
   Hard-answer и comment после
   TTS-window дают delivery, context rollback и ноль вызовов switcher; failed
   delivery даёт ровно один вызов. Тест повторяется с намеренно задержанным
   `_on_end_of_turn`: результат не меняется. Это go/no-go KV-3a.
4. **Pipecat voice switch/correlation:** 20 чередований disp↔kora, p95
   reconnect-to-audio, barge-in, cache-key и exact
   `TTSStartedFrame.context_id ↔ utterance_id` при SPEAK reordering/drop. Порог
   KV-1 — p95 ≤ 500 мс без потерянной/неверно коррелированной реплики.
5. **Fish MP3:** каждый самостоятельный bounded segment, полученный через live
   WS, проигрывается до конца Chrome/Safari до получения полного body; TTFB ≤ 2
   с на staging.
6. **PWA media auth:** iPhone Safari посылает scoped SameSite cookie из
   `<audio>`, играет в фоне/лок-скрине, stop очищает session; insecure-dev
   явно работает без Secure только на localhost. Отдельно фиксируется поведение
   `Range`-запросов: как нативный `<audio>` (Chrome + iPhone Safari) реагирует
   на chunked MP3 без `Content-Length`/`Accept-Ranges` — доигрывает ли сегмент
   до конца, не зависает ли на буферизации, не пере-запрашивает ли Range.
   Подтверждение выбранной Range-стратегии (200 ignore или 416) — часть go/no-go
   KV-5; зависший элемент на Safari блокирует радио целиком.

### 14.3. Зафиксированные блокеры

- `claude-agent-sdk==0.2.116` закреплён прямой зависимостью в `pyproject.toml`
  этой правкой. Это закрывает только pin; KV-3b/KV-4 всё ещё не начинаются до
  успешных P1/P2 на установленном окружении с этой точной версией.
- С5 bearer-auth и С6 изоляция journal/artifact directory должны быть
  реализованы раньше KV-4/KV-5.
- P3a/P3b закрываются до implementation-plan KV-3a/KV-1: callback-only router
  и FIFO-сопоставление started frames не являются допустимыми временными
  реализациями.
- Если native Safari не принимает streaming MP3 segment или scoped cookie,
  KV-5 останавливается для отдельного transport-design; полный blob и
  неограниченный chapter-stream не являются допустимым fallback.

## 15. Связи

- `docs/dispatcher-kora-ideal-architecture.md` — принцип детерминизма
  (роутер §4.4), Р-15/NO-EXFIL и его точечное ослабление (§2.5).
- `docs/superpowers/plans/2026-07-14-synapse-dispatcher-kora-phase0.md` —
  С5 authn (пререквизит новых роутов), С6 kora-hardening (гейт-семья для
  `deliver_file`).
- `docs/superpowers/specs/2026-07-14-synapse-ai-provider-settings-design.md` —
  поглощённый документ; оставлен только redirect на эту каноническую спеку;
- `synapse/pipeline/arbiter.py` — точка voice-свитча (KV-1).
- `synapse/pipeline/tts_cache.py` — ключ кэша + REST-путь Play (KV-1).
- `synapse/bridge/kora.py` — системный промпт, события, гейт (KV-2/3/4).
- `synapse/dispatcher/speakify.py` — backstop грязного текста (KV-2).
- pipecat `services/fish/tts.py` — `_update_settings`-реконнект (KV-1) и
  ormsgpack-протокол Fish WS (образец для радио, KV-5).
- `prototypes/codeflow-night-theme/` — утверждённый визуальный контракт §16.

## 16. Редизайн CodeFlow (Night Atlas / Hero Drive)

Добавлено 2026-07-15 после того, как прототип был построен и проверен живьём.
Раздел описывает **оболочку**, в которую садятся §4.5 (файлы), §4.6 (радио) и
§4.8 (AI-настройки). Он не меняет ни одного сетевого контракта выше и не
является дополнительным gate: редизайн-слайсы R1/R2 идут независимо от
prerequisite checklist §0.1, потому что не трогают ни AI-стор, ни SDK, ни Fish.

### 16.1. Источник и статус

Источник — `prototypes/codeflow-night-theme/` (не импортируется, не собирается,
в проде не участвует). Прототип построен как **1:1 функциональное зеркало**
живого клиента: каждый его контрол соответствует реальной фиче
`synapse/pipeline/client/`, и наоборот. Поверх зеркала в нём проверены три
экрана этой спеки — настройки, файловая карточка, радио.

Проверено живьём в браузере 2026-07-15 (Playwright, статик-сервер): свич тем,
запрет одинакового провайдера в primary/fallback с блокировкой Save, save →
bump ревизии, Revert, warning расхождения `routing.model` vs
`providers.selected_model`, недоступность модели без `supports_tools` в
маршруте, Test-кнопка, полный цикл радио с закладкой и все три правила
аудио-эксклюзива. Демо-данные прототипа — фейковые маски (`sk-or-…f4a2`), не
реальные ключи и не реальные `reference_id` голосов.

Статус: прототип — **утверждённый визуальный контракт**, не код. Прод-клиент
получает его портом по правилам §16.5.

### 16.2. Две темы и один свич

`:root` в `styles.css` — токен-контракт темы **Night Atlas** (тёмная,
по умолчанию). `.hero-mode` — оверрайды темы **Hero Drive**. Обе темы делят
одну геометрию компонентов: тема меняет цвет, тень, шрифтовую пару и акцент,
но никогда не меняет разметку, размеры и порядок элементов. Это и есть условие
того, что свич не может «сломать экран» — переключается только палитра.
Брейкпоинты 1050 px (сайдбар → drawer) и 760 px (сетки → одна колонка).

**Тема — свойство устройства, не воркспейса.** Она персистится в
`localStorage` и НЕ едет в `ai-settings.json` (§4.7). Обоснование: телефон в
тёмной комнате и мак на столе законно расходятся; CAS-ревизия, 409-диалог и
`server vs draft` за смену цвета — абсурдная цена. `AISettingsStore` остаётся
стором про AI. Следствие, которое UI обязан показывать честно: смена темы **не
поднимает ревизию** и не показывает save bar — она применяется мгновенно.

Свич живёт в двух местах: секция `Appearance` экрана настроек (канон) и
кнопка в топбаре (быстрый доступ). Оба обязаны читать и писать одно состояние —
второй источник правды запрещён.

### 16.3. Экран настроек: `Appearance` приезжает раньше AI

Экран `#/settings/ai` из §4.8 создаётся редизайном (слайс R2) с **одной**
секцией `Appearance`, потому что она client-only и ничего не ждёт. Секции
`Диспетчер`/`Кора`/`Voice` приходят в AI-1 и садятся в тот же экран без его
переписывания:

```text
Settings
├── Appearance           ← R2 (редизайн, client-only, localStorage)
│   └── Night Atlas ⇄ Hero Drive
├── Диспетчер            ← AI-1 (§4.8, требует C4.2 + C5)
├── Кора                 ← AI-1
└── Voice (read-only)    ← AI-1, редактор — Settings → Voice M+1
```

Запрещено: рисовать в проде AI-секции заглушками, disabled-контролами или
демо-данными до AI-1. Экран, который показывает провайдера, которого не
существует в сторе, врёт пользователю. Пока секции нет — её нет.

### 16.4. Якоря для файлов и радио

Редизайн закладывает DOM-места и правила, но не включает фичи:

| Место | Кто владеет | Когда включается |
| --- | --- | --- |
| `kind="file"` в `addEntry` | §4.5 | KV-4 |
| `#radio-bar` в шапке треда | §4.6 | KV-5 |
| `#/settings/ai` AI-секции | §4.8 | AI-1 |

**Правило одного звука.** У клиента ровно один арбитр аудио: Play-кнопка ленты
(`nowPlaying`), радио и живой звонок взаимоисключающи. Тап мика ставит радио на
паузу; старт радио вешает живой звонок, **оставаясь на треде** (навигация в
чат звонка — поведение ручного hang-up, не побочный эффект старта радио); Play
и радио глушат друг друга. Правило клиентское: сервер про него не знает, и это
осознанно — двух одновременных звуков в одном браузере не бывает физически,
серверный лизинг здесь ничего не добавляет. Серверные лизы радио (§4.6,
voice-lease) остаются отдельным механизмом и решают другую задачу — конкуренцию
за Fish WS, а не за уши.

Пауза радио **переигрывает текущий фрагмент** с начала, а не продолжает с
середины: закладка сохраняется только по концу фрагмента (§4.6 двигает позицию
только на `STREAMING → READY(next)`). Пользователь не теряет текст — он слышит
последние секунды дважды. Это дешевле, чем хранить offset внутри сегмента.

### 16.5. Контракт порта прототип → прод-клиент

1. **DOM-id прод-клиента — несущие.** `app.js` адресует ~60 узлов по id;
   редизайн меняет разметку вокруг них, но ни один id не переименовывается.
   Переименование id — отдельный шаг с правкой `app.js`, не «заодно».
2. **Прототипный `app.js` — не источник.** Он стоит на демо-массивах вместо
   `/api/*`. Портируются `index.html` (структура) и `styles.css` (тема);
   поведение остаётся продовым — реальный поллинг, реальные роуты, реальные
   гварды (`feedKey`, `listsSeq`, `feedInFlight`, identity-guard голоса).
   Копирование прототипных хендлеров в прод запрещено.
3. **Ноль изменений сетевых контрактов.** Ни один `fetch` не появляется, не
   исчезает и не меняет форму. Редизайн, требующий нового роута, — не редизайн.
4. **Инварианты, которые легко потерять портом** (каждый — реальный баг, если
   сломать):
   - `#view-thread` остаётся scroll-контейнером: `nearBottom()` читает его
     `scrollHeight/scrollTop/clientHeight`. Вложенный скроллер сломает
     авто-прокрутку ленты;
   - `loadDiff` делает `replaceChildren` на `#view-diff` — обёртка внутри него
     будет стёрта первым же рендером диффа; либо её нет, либо `loadDiff`
     учится про неё явно;
   - `$("live-mute").textContent = …` затирает содержимое кнопки: у неё либо
     нет иконки, либо у подписи отдельный узел и `app.js` пишет в него;
   - `#view-title` законно снимается из DOM inline-редактором rename — любой
     новый писатель заголовка обязан пережить `null` (`setViewTitle`);
   - мик **никогда** не получает `disabled` (R2): `dispOn=false` — только
     визуальный dim, иначе теряется единственный hang-up.
5. **Ассеты.** `/client` отдаётся точными роутами, а не mount-ом (S24: все
   exact-роуты регистрируются ДО mount `/client/dev`). Новые файлы темы
   (SVG-иллюстрации) требуют роута с whitelist-именами; молча положить файл в
   директорию недостаточно — он не отдастся.
6. **`prefers-reduced-motion`** покрывает каждую новую анимацию темы. Звёздное
   поле, орбиты и wave — декоративные; при reduce они замирают, а не исчезают.
7. **`100vh` запрещён** (`100dvh`): iOS Safari режет вьюпорт адресной строкой.

### 16.6. Тесты: что переживает редизайн, а что он отменяет

Переживают **немодифицированными** (редизайн обязан подстроиться под них, а не
наоборот): `test_ui_client.py` (структурные id, SPA-роутер, XSS-дисциплина,
mount-order, wiring голоса/Play/Diff), `test_bugs_audit.py`,
`test_kora_status_ui.py`, `test_slice5_pwa.py`, `test_ui_hunt_0714.py` и вся
остальная суита.

Отменяется ровно один файл: **`test_ui_redesign.py`** прибивает Organic-канон
UI v4 (`Caprasimo`, `Figtree`, `#c67139`, `#f5ead8`, `color-scheme: light`,
`.bg-blob`, `logo-wave`) — то есть ровно ту тему, которую редизайн заменяет.
Он переписывается на Night Atlas-канон с сохранением **смысла** каждой
проверки: токены темы существуют, `prefers-reduced-motion` покрывает новые
анимации, `100vh` не вернулся, композер — mic/input/send, `live-status` имеет
`aria-live`, `innerHTML` не появился, мик не получает `disabled`. Проверки,
которые редизайн не отменяет, а просто перекрашивает (значения токенов),
меняют значения; проверки поведения не трогаются вовсе.

`manifest.webmanifest` перекрашивается вместе с темой (`theme_color`/
`background_color` → Night Atlas), и `test_ui_redesign.py` следует за ним.
PWA-иконки остаются старыми до отдельного шага — перерисовка растровых иконок
не входит в порт и не блокирует его; расхождение иконки и темы фиксируется
здесь явно, чтобы не выглядеть недосмотром.

### 16.7. Слайсы

| Слайс | Содержание | Зависимости | DoD |
| --- | --- | --- | --- |
| **R1** | тема и оболочка: `index.html` + `style.css` + токены, топбар-свич, шапка треда, stage-rail | — | суита зелёная, `test_ui_redesign.py` переписан, живой прогон на телефоне |
| **R2** | экран `#/settings/ai` + секция `Appearance` | R1 | свич из настроек и из топбара — одно состояние; тема переживает reload |
| **R3** | карточка `kind="file"` | R2 + KV-4 | §4.5 |
| **R4** | `#radio-bar` + правило одного звука | R3 + KV-5 | §4.6 |

R1/R2 не ждут §0.1 — они не касаются AI/SDK/Fish. R3/R4 ждут свои фичи и не
существуют раньше них.

### 16.8. Не входит

Перерисовка PWA-иконок; анимационный пасс; редактор голосов (Settings → Voice
M+1); темы сверх этих двух; серверная персистентность темы; кастомные токены
пользователя.
