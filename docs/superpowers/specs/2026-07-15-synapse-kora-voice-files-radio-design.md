# Синапс · Голос Коры, файлы в CodeFlow, радио-озвучка

Дата: 2026-07-15.

Статус: **v2 — расширенная спека после аудита кода и несущих браузерных
ограничений; до implementation-plan и живых protocol probes**.

Область: реалтайм-присутствие Коры в голосовом канале (свой голос, живые
реплики, детерминированный роутер ответов и комментариев), TTS-friendly речь
Коры, доставка файлов-результатов в CodeFlow и потоковая озвучка длинных
текстов («радио») голосом литератора через Fish Audio streaming.

Не трогается: STT, каскад диспетчера, права Коры (гейт), approval-контракт,
спека AI-настроек (`2026-07-14-synapse-ai-provider-settings-design.md`).

## 1. Зачем это нужно

Живой прогон 2026-07-15 (задача «найди книгу и порежь главу») показал четыре
дыры разом:

1. **Кора немая и безликая.** Голосом звучат только два шаблона
   (`Задача выполнена: {task_text}` / `не выполнена`, `synapse/bridge/kora.py:207-212`)
   и вопрос `AskUserQuestion` — всё голосом диспетчера. Кнопки Play в ленте
   подписаны «Flow voice» / «Code voice» (`client/app.js:511`), но синтезируют
   одним и тем же `cfg.fish_reference_id` — подпись врёт. Пока задача идёт,
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
4. **Слушать длинный текст нельзя.** `POST /api/tts` капит текст 4000 символами
   (`synapse/pipeline/webrtc_server.py:670`) и отдаёт один WAV-блоб — глава на
   31 500 слов так не звучит. При этом весь стек уже стоит на стриминговом
   Fish `wss://api.fish.audio/v1/tts/live` (pipecat `fish/tts.py:203`) — есть
   быстрый первый байт, нет только трубы до браузера.

## 2. Зафиксированные продуктовые решения

1. **Три голоса, три роли.** Диспетчер — существующий `FISH_REFERENCE_ID`;
   Кора — `FISH_VOICE_KORA` = `c5e804ba213c4ca3bcf3fe8160fceef6`; литератор
   (радио) — `FISH_VOICE_NARRATOR` = `102ea81e50c64962b689c44c16931473`.
   Голоса — конфиг из `.env` (id голоса — не секрет, но живёт рядом с
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
   `claude-agent-sdk 0.2.116`, зависимость перед реализацией пинится). Успешная
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
   радио-сессия и один Fish WS на хост, кап символов документа и сегмента.
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
`Settings(voice=cfg.fish_reference_id)`. pipecat умеет менять голос на лету:
`TTSUpdateSettingsFrame(delta)` → `_update_settings` → reconnect WS
(`.venv/.../pipecat/services/fish/tts.py:219-236`) — свитч не пофреймовый, а
по-реконнектный (~100–300 мс).

**Решение: арбитр становится voice-aware.** Арбитр — единственная точка
сериализации выдачи в TTS (`synapse/pipeline/arbiter.py:95-126`), значит
только он может вставить свитч строго на границе реплик:

- вводится `VoiceRole = Literal["disp", "kora"]` и приложение-специфичный
  `SynapseSpeakFrame(TTSSpeakFrame)` с обязательным `voice_role`; голая
  `TTSSpeakFrame` больше не создаётся кодом Синапса;
- `QueueItem` получает обязательное поле `voice_role`; `push_dispatcher_text`
  всегда ставит `disp`, `push_speak(text, voice_role)` требует роль явно;
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

`cascade/strategy.py` тоже перестаёт создавать голую `TTSSpeakFrame` и ставит
`disp`. Лексическое определение роли по тексту запрещено.

Альтернативы отклонены: второй `FishAudioTTSService` в пайплайне — pipecat
ParallelPipeline + фильтры, два WS-коннекта, тяжело; синтез Кориных реплик
через REST вне пайплайна с инжектом raw-аудио — обходит арбитра и
interruption-семантику (barge-in перестаёт работать на этих репликах).

**Кэш обязан стать voice-aware.** Сейчас `TTSCache` хранит `_voice` один раз в
конструкторе, а все `get/put/assemble/wav_path` не принимают голос. Одного
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
- **Play-путь** (`webrtc_server.py:678-685`): `speakable()`-текст идёт в TTS
  как есть (минус Gemini-вызов и задержка), грязный — через speakify как
  сегодня. speakify остаётся pinned-потребителем по карте AI-спеки (§5 там).

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
- **терминальная реплика**: последний чистый TextBlock звучит на ResultMessage,
  если он не был озвучен как milestone; иначе звучит существующий
  NO-EXFIL-шаблон `Задача выполнена: {task_text}` / failure-шаблон. Один
  `utterance_id` — максимум одна live-реплика;
- успешно прозвучавший вопрос или milestone открывает transient
  `KoraReplyWindow(task_id, thread_id, utterance_id, expires_at)`; окно
  создаётся только после принятия frame живым output task, не при одном лишь
  появлении текста в ленте;
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

Сегодня голосовая реплика **всегда** уходит в LLM диспетчера
(`app.py:931-983`, пре-LLM ветки нет), и доставка ответа Коре держится на том,
что модель догадается позвать `answer_kora` — гарантия вероятностная, не
структурная (это же зафиксировано в архитектурном доке как слабость B13-класса:
вопрос Коры инжектится `append_to_context=False`, LLM его не видит).

**Решение.** Перед диспетчеризацией в `_on_end_of_turn` и симметрично в
`POST /api/threads/{id}/message` встаёт чистый
`VoiceRouter.route(text, RouteSnapshot) -> RouteDecision`. Snapshot содержит
только status/owner/awaiting/reply-window, текущий thread id и clock; I/O и LLM
в роутере нет.

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
  успешной записи turn в SDK transport;
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

**Гейт + snapshot доставки.** Ограничение старого черновика «только current
run root» не покрывает живой кейс: книга лежит в `~/Downloads`, скрипт — в
другом проекте, результат Кора может создать в workspace/output. Новый
контракт совпадает с правом чтения Коры, но строже по типу:

- path резолвится относительно `_current_root`; `_is_secret_path` проверяется
  до открытия; secret/resolve/missing/directory/too_large возвращают только
  категорию, без абсолютного пути;
- источник может лежать вне run root, если не секретный: доставка идёт тому же
  аутентифицированному владельцу машины, а UI ничего не скачивает автоматически;
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
- ответ `201` несёт display metadata, `total_segments`, `start_segment` и
  границы manifest, но не текст книги;
- тем же ответом ставится случайная media-cookie:
  `synapse_radio=<token>; HttpOnly; SameSite=Strict; Path=/api/radio/{session_id}; Max-Age=…`.
  `Secure` обязателен на HTTPS и снимается только в explicit insecure-dev.

**Media-роут** `GET /api/radio/{session_id}/segments/{segment_index}`:

- нативный `<audio>` автоматически посылает scoped cookie; handler constant-time
  сверяет её с session token. Bearer не требуется именно на этом route, потому
  что `<audio src>` не умеет выставить заголовок; cookie является узкой
  capability-authn, выпущенной только bearer-authenticated control-route;
- segment должен быть текущим ожидаемым индексом; повтор уже завершённого
  допускается один раз для browser retry, прыжок вперёд → 409;
- `StreamingResponse(audio/mpeg)` открывает Fish WS, шлёт `start` с narrator
  voice/model/`format=mp3`, text/flush и немедленно отдаёт приходящие audio
  bytes. На `finish`, disconnect или error WS закрывается в `finally`;
- только успешный `finish` двигает current segment. Ошибка Fish → оборванный
  stream; повтор того же segment разрешён новым GET;
- в один момент не больше одного Fish WS. Отмена/expiry инвалидирует cookie
  session; текущий generator получает cancellation и посылает `stop` best effort.

**Control-роут стопа** `DELETE /api/radio-sessions/{session_id}` — bearer +
CSRF, идемпотентно закрывает active session/WS и истекает media-cookie. Natural
completion делает то же после последнего сегмента.

**Клиент: мини-плеер.** Кнопка «Озвучить» сначала вызывает control-POST, затем
ставит `audio.src` на первый segment URL. `ended` сохраняет `to_paragraph`
текущего segment в `localStorage` и только тогда ставит URL следующего. Плашка
в шапке треда показывает play/pause, точный «фрагмент N из M», stop и
«продолжить». В v1 нет prefetch: возможная короткая пауза между сегментами —
осознанная цена честного cost-bound. `nowPlaying` остаётся единым для Play и
radio; старт одного останавливает другое.

После рестарта server-session потеряна: клиент создаёт новую через control-POST
с сохранённым `from_paragraph`. Immutable blob + детерминированный stripper
делают продолжение стабильным.

**Сосуществование с голосовым сеансом.** Открытый мик + радио в колонки =
STT слушает книгу. Детерминированное клиентское правило v1: радио и живой мик
взаимоисключающие — тап на мик ставит радио на паузу; старт радио при живом
звонке сначала вешает звонок (существующий `disconnectVoice`,
`app.js:1000-1017`) и ждёт подтверждённый disconnect, только затем делает
control-POST. Оба правила — client-side; сервер всё равно запрещает вторую
radio session как backstop.

### 4.7. Конфиг

Новые поля `SynapseConfig` (все по конвенции `from_env`, `config.py:100-152`;
малформ → дефолт, не краш — паттерн B4):

```text
fish_voice_kora: str | None = None        FISH_VOICE_KORA      (нет → голос диспетчера)
fish_voice_narrator: str | None = None    FISH_VOICE_NARRATOR  (нет → голос диспетчера, §2.1)
kora_speak_max_chars: int = 350           KORA_SPEAK_MAX_CHARS
kora_milestone_min_gap_s: float = 20.0    KORA_MILESTONE_MIN_GAP_S
kora_reply_window_s: float = 15.0         KORA_REPLY_WINDOW_S
kora_comment_queue_max: int = 3           KORA_COMMENT_QUEUE_MAX
deliver_file_max_mb: int = 50             DELIVER_FILE_MAX_MB
radio_max_chars: int = 200_000            RADIO_MAX_CHARS
radio_segment_max_chars: int = 2_500      RADIO_SEGMENT_MAX_CHARS
radio_session_ttl_s: int = 28_800         RADIO_SESSION_TTL_S
```

`.env.example` дополняется этими именами с реальными id голосов как
подсказками-значениями. Наборы `router_control_words`,
`dispatcher_vocatives`, `kora_vocatives` — typed `frozenset` в config с
дефолтами §4.4; env-редактирование словаря в v1 не нужно.

## 5. API — сводка изменений

```text
POST /api/tts                                — role выбирает reference_id (disp/kora); контракт ответа не меняется
GET  /api/threads/{id}/files/{file_id}       — новый: отдача доставленного файла (attachment)
POST /api/threads/{id}/radio-sessions        — новый: manifest + scoped media-cookie
GET  /api/radio/{session_id}/segments/{n}    — новый: один bounded streaming MP3
DELETE /api/radio-sessions/{session_id}      — новый: stop/cleanup
```

Требования: bearer С5 на control/download-роутах; scoped HttpOnly-cookie на
media-GET (§4.6); неизвестный `file_id` → 404; blob исчез/повреждён → 410;
вторая радио-сессия → 409; `from` за пределами → 416; не-текстовый файл →
415; malformed UTF-8/пустой нормализованный текст → 422.

`POST /api/threads/{id}/message` сохраняет URL и response schema, но получает
новый fast-path: при `RouteDecision=kora_*` возвращает
`{"routed_to":"kora","accepted":true,"reply":null}` вместо вызова
dispatcher LLM. Клиент добавляет уже сохранённый server-side user-entry и не
рисует фиктивную assistant-реплику.

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
8. Кора зовёт `deliver_file` на secret/directory/гигантский/исчезнувший файл →
   deny категорией; вне run root само по себе не deny. Частичный snapshot
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

## 7. Скоуп реализации

| Слайс | Содержание | Зависимости |
| --- | --- | --- |
| **KV-1 — голос Коры в тракте** | `SynapseSpeakFrame`, обязательная role-метка в `QueueItem`, voice-aware arbiter/cache API+observer, `/api/tts` role→reference_id | — |
| **KV-2 — речевой контракт** | абзац в `_system_prompt`, модуль `speakable.py` + тесты на корпусе реальных кор-текстов, Play-путь: чистый текст мимо speakify | — |
| **KV-3a — присутствие + hard-router** | `kora_said`, dedupe/presence policy, живая terminal-фраза, reply-window, hard `AskUserQuestion` fast-path в voice+HTTP | KV-1, KV-2 |
| **KV-3b — interactive comments** | real CLI probe, persistent SDK reader, bounded comment queue, pending-results FSM, explicit vocatives/reply-window router, `to:"kora"` feed + journal | KV-3a; probe — go/no-go |
| **KV-4 — deliver_file** | SDK MCP probe, trusted handler, immutable artifact snapshot/blob store, random per-thread ids, download auth-fetch, file-card | Фаза 0 С5 + С6 |
| **KV-5 — радио** | Fish WS-клиент, deterministic manifest, bounded segment stream, radio-session FSM, scoped media-cookie, mini-player, mic↔radio rules | KV-4, С5, HTTPS staging |

Каждый слайс — отдельный tero-ран, зелёная суита, замороженные тесты не
редактируются. KV-1/KV-2 независимы; KV-3a после них; KV-3b только после
protocol probe; KV-4 независим после С5/С6; KV-5 последним.

## 8. Не входит в v1

- семантический (LLM/embedding) роутер реплик — только детерминированные
  правила §4.4;
- комментарий Коре без активной задачи; такой ход всегда отвечает диспетчер;
- озвучка thinking-блоков и tool-результатов Коры;
- второй одновременный TTS-поток (радио живёт вне голосового пайплайна);
- выбор голосов из UI (голоса — `.env`; экран настроек — чужая спека);
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
| Речь Коры — social-engineering канал из workspace | `speakable()` не объявляется safety-фильтром; distinct voice обязателен, речь не входит в dispatcher context и не получает полномочий; шаблон остаётся fallback-ом |
| Reply-window уводит фразу не тому собеседнику | Одно короткое окно только после реально прозвучавшей Коры; «Флоу/диспетчер» и control first-token имеют приоритет; feed явно показывает `to:kora` |
| Интерактивный comment ломает lifecycle SDK | Отдельный real CLI go/no-go probe; один reader, pending-results FSM, bounded queue, identity guards |
| MCP-тул зашадовлен `allowed_tools=[]` | Probe на фактической pinned SDK; trusted handler сам authority независимо от hook/allowlist |
| Download-роут = новый экспорт-канал | Только immutable artifact snapshot, random per-thread id, bearer auth-fetch, без листинга/исходного пути; С5+С6 — prerequisites |
| Fish-счёт на книгах | Bounded segment вместо недоказуемого pause/backpressure; no prefetch, caps, одна session/WS, journal по каждому segment |
| Media-cookie создаёт новую auth-поверхность | HttpOnly + SameSite=Strict + path scope + TTL; выпускается только bearer+CSRF POST; token не в URL; media GET read-only |
| Радио в мик → STT слушает книгу | Взаимоисключение мик↔радио на клиенте (§4.6) |
| iOS/PWA: MP3 chunks, cookie и фон ведут себя иначе | Нативный `<audio>` выбран ради media-session; обязательный live-DoD Safari/iPhone + Chromium до принятия KV-5 |

## 10. Приёмка

1. Реплики Коры (вопрос, completion, майлстоун) звучат голосом
   `c5e804ba…`, диспетчер — прежним; Play-кнопки ленты честны по ролям.
2. Кора в живом прогоне говорит разговорно; markdown-мусор в озвучку не
   попадает: чистый текст — напрямую (без Gemini-вызова), грязный — speakify
   (Play) / молчание (live).
3. Во время рана при молчащем диспетчере Кора рассказывает прогресс не чаще
   раза в `kora_milestone_min_gap_s`; финал — её собственная фраза, если
   чистая, иначе шаблон; один SDK TextBlock не звучит дважды.
4. При `awaiting_answer` голосовой ответ доставляется Коре без единого
   LLM-вызова (журнал: `route_to_kora`, ноль dispatcher-pass); «стоп» при
   этом уходит диспетчеру и отменяет задачу.
5. После реально прозвучавшего milestone короткий ответ и явное «Кора, …»
   попадают в тот же SDK-process через comment queue без dispatcher LLM;
   «Флоу, …» отвечает диспетчер. Comment до terminal result выполняется до
   terminalization задачи.
6. `deliver_file` для не-секретного regular file (в том числе из Downloads)
   создаёт карточку и immutable blob; скачивание после удаления исходника
   byte-for-byte совпадает с snapshot. secret/directory/сверх-кап — deny
   категорией без пути.
7. Кнопка «Озвучить» на md-файле стартует радио литератором `102ea81…`:
   первый звук ≤ 2 с на живом прогоне; после pause не синтезируется следующий
   segment, перерасход ≤ segment cap; «продолжить» идёт с сохранённого
   paragraph; вторая session → 409.
8. Control/download API недоступен без bearer С5; media GET недоступен без
   scoped cookie, а cookie нельзя получить без bearer+CSRF start. API token,
   media token и исходные пути отсутствуют в URL/feed/deny-текстах.
9. Полная суита зелёная; замороженные тесты не тронуты; NO-EXFIL-тесты
   регидрации (`loop.py:64-66`) остаются якорем — новые kinds (`file`,
   `to:"kora"`) в LLM-историю не попадают.
10. Live-матрица проходит на desktop Chromium и iPhone Safari: смена голосов,
    background native audio, scoped cookie, stop/resume и мик↔радио.

## 11. Контракты новых типов и функций

Ниже — публичная поверхность модулей. Имена могут механически уточниться в
implementation-plan, но входы, выходы и authority-boundaries фиксированы.

### 11.1. Голос и речевая пригодность

```python
VoiceRole = Literal["disp", "kora"]

@dataclass(kw_only=True)
class SynapseSpeakFrame(TTSSpeakFrame):
    voice_role: VoiceRole

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
    def push_speak(self, text: str, voice_role: VoiceRole) -> None: ...
    def has_pending(self, source: str) -> bool: ...

class VoiceOutputState:
    def note_dispatcher_generation(self, active: bool) -> None: ...
    def note_item_enqueued(self, voice_role: VoiceRole) -> None: ...
    def note_tts_stopped(self) -> None: ...
    def interrupt(self) -> None: ...
    def is_idle(self) -> bool: ...

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

class KoraRunner:
    async def provide_comment(self, text: str, *, task_id: str,
                              thread_id: str) -> bool: ...
```

Reply-window mutable state живёт в отдельном `KoraInteractionState`, не в
persisted `TaskStore`: `open_window`, `consume_window`, `close_for_task` — все
синхронны и identity-guarded. `VoiceRouter` его не мутирует. Мутация происходит
только после успешной доставки решения, чтобы failed delivery могла честно
уйти диспетчеру тем же ходом.

`provide_comment` не принимает comment для чужого task/thread, terminal task,
закрытой SDK-сессии или полной queue. Текст не нормализуется и не обрезается;
верхний cap пользовательского хода применяется общим HTTP/STT входом до
router-а.

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
    async def publish(self, source: Path, *, thread_id: str, task_id: str,
                      title: str | None) -> FileArtifact: ...
    def resolve(self, thread_id: str, file_id: str) -> tuple[FileArtifact, Path] | None: ...
```

`publish` выполняет blocking file I/O через `asyncio.to_thread`, но commit
registry/feed идёт в event-loop после готового blob. Транзакционный порядок:
validate/open → copy+hash temp → fsync/atomic blob install → atomic registry
persist → append feed. До registry commit файл пользователю не виден. Ошибка
feed append после registry commit чинится идемпотентным replay карточки при
следующем чтении треда; blob не теряется.

MCP handler получает `thread_id/task_id` из immutable run snapshot, никогда из
tool arguments. `name = Path(source).name` санитайзится для Content-Disposition;
CR/LF, slash и control chars удаляются. `title` — display-only, cap 160.

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
   в двух голосах даёт два WAV.
2. **Speakable corpus:** реальные строки из приложенного прогона (таблицы,
   Markdown, paths, ids) молчат; короткие разговорные русские/английские фразы
   проходят; fuzz Unicode/пустые inputs не бросают исключений.
3. **Presence/router table:** hard question, живое/истёкшее/чужое reply-window,
   explicit vocatives, first-token control, terminal race, dispatcher busy,
   duplicate utterance. Для каждого — target, journal reason и ровно одна
   feed entry.
4. **Interactive fake SDK:** comment до первого result; два comments; queue
   full; writer failure с deferred result; cancel/supersede; late result.
   Проверка: один reader, terminal event ровно один, чужой task не мутирован.
5. **Artifact unit/API:** relative/absolute source, Downloads source, secret,
   final symlink, directory, file growing past cap, duplicate content,
   cross-thread id, restart registry, deleted source, corrupt/missing blob,
   malicious filename. Download требует bearer и совпадает byte-for-byte.
6. **Radio pure/client/API:** Markdown normalization golden corpus; segment cap
   и stable manifest; media GET без/с wrong cookie → 401; cookie path/flags;
   concurrent/out-of-order/retry; pause означает отсутствие следующего GET;
   Fish mock error не двигает index; stop/ttl освобождает slot.
7. **NO-EXFIL regression:** `kora_said`, file cards, `to:kora`, manifests и
   radio events не появляются в `history_from_feed` / dispatcher messages.
8. Полная существующая суита запускается без редактирования frozen tests.

Сетевые Fish/SDK тесты — только через fake transport/WS server; unit suite не
требует ключей и не несёт расхода.

### 14.2. Обязательные живые probes до implementation-plan

1. **SDK bidirectional:** pinned version + реальный CLI, comment во время
   tool-heavy response; зафиксировать message/result ordering, hooks, session id
   и число subprocess. Это go/no-go KV-3b.
2. **SDK MCP:** `deliver_file` виден и вызывается при текущей permission config;
   фактическое tool name проходит PreToolUse; handler deny не обходится
   `allowed_tools`. Это go/no-go KV-4.
3. **Pipecat voice switch:** 20 чередований disp↔kora, p95 reconnect-to-audio,
   barge-in и cache-key. Порог KV-1 — p95 ≤ 500 мс без потерянной реплики.
4. **Fish MP3:** каждый самостоятельный bounded segment, полученный через live
   WS, проигрывается до конца Chrome/Safari до получения полного body; TTFB ≤ 2
   с на staging.
5. **PWA media auth:** iPhone Safari посылает scoped SameSite cookie из
   `<audio>`, играет в фоне/лок-скрине, stop очищает session; insecure-dev
   явно работает без Secure только на localhost.

### 14.3. Зафиксированные блокеры

- `claude-agent-sdk` сейчас не закреплён прямой зависимостью в `pyproject.toml`,
  хотя окружение содержит 0.2.116. KV-3/KV-4 не начинаются до pin-а версии,
  прошедшей оба SDK probe.
- С5 bearer-auth и С6 изоляция journal/artifact directory должны быть
  реализованы раньше KV-4/KV-5.
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
  speakify pinned (карта LLM-потребителей §5 там; KV-2 сужает его роль, не
  переносит);
- `synapse/pipeline/arbiter.py` — точка voice-свитча (KV-1).
- `synapse/pipeline/tts_cache.py` — ключ кэша + REST-путь Play (KV-1).
- `synapse/bridge/kora.py` — системный промпт, события, гейт (KV-2/3/4).
- `synapse/dispatcher/speakify.py` — backstop грязного текста (KV-2).
- pipecat `services/fish/tts.py` — `_update_settings`-реконнект (KV-1) и
  ormsgpack-протокол Fish WS (образец для радио, KV-5).
