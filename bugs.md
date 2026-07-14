# Аудит живого UI-кода (HTML/CSS/JS)

Дата: 2026-07-13 · срез: коммит `8a1b1cc` · 2 хантера (B-CORE: DOM/сеть/состояние app.js + index.html · B-UI: вёрстка/CSS/виджеты), дедуп сеньором.

Это аудит **реального кода** (не спеки). Предыдущий аудит спеки v2 → `bugs-archive-2026-07-13-spec.md`.

Файлы под аудитом: `synapse/pipeline/client/{index.html,style.css,app.js}` · `synapse/pipeline/static/{status-widget.js,logs.html,manifest.webmanifest}`.

## TL;DR

- **3 CRIT** (B-CORE-1/2/3): потеря набранного текста при ошибке сети, зависший UI без таймаута fetch, замерзающая после 500 записей лента. Все три — один корень: фронтенд оптимистичен там, где сеть падает.
- **6 MAJOR в app.js**: молчаливый «успех» добавления проекта, нересетуемый onError голоса, модалки без focus-trap/Escape, гонка pollFeed → дубликаты, нет aria-live, пустой UI при первой неудаче загрузки.
- **6 MAJOR в CSS/виджетах**: контраст `--dimmer` и логов ниже WCAG AA, нет `:focus-visible`/`:active` нигде, нет `prefers-reduced-motion`, status-widget.js загрязняет `window` и дыряво ловит ошибки JSON.
- **Решать в первую очередь:** B-CORE-1, B-CORE-2, B-CORE-3 — это потеря данных и death-состояния; дальше пачка MAJOR про тач-отдачу/доступность (B-UI-3/4) и aria-live (B-CORE-8).

---

## B-CORE · app.js + index.html (DOM, сеть, состояние)

### B-CORE-1 · CRIT · Текст пользователя удаляется из input ДО подтверждения успеха отправки
- **Файл:** app.js:266 (в `sendMessage`)
- **Что:** `input.value = "";` стоит сразу после `if (!text) return;`, **до** любых сетевых вызовов. Дальше `postJSON("/api/threads", …)` (создание треда), проверка `!tRes.ok` (273), `postJSON(/message)` (278). Любой `return`/`throw` по пути оставляет поле пустым.
- **Почему баг:** отказ сети (`catch` 286), ошибка создания треда (273) или `!res.ok` на message (279) — набранный текст бесследно исчезает. На мобиле перепечатывать длинное сообщение дорого. Корневая причина потери данных.
- **Чинить:** чистить `input.value` только после успешного POST `/message` (строка 282, ветка успеха), либо при ошибке восстанавливать `input.value = text`.

### B-CORE-2 · CRIT · `postJSON`/`getJSON` без таймаута → зависший UI навсегда
- **Файл:** app.js:12-20 (хелперы); разносится в 271, 278, 455
- **Что:** `fetch` без `AbortController`/таймаута. В `sendMessage` (260-292) `await postJSON(/message)` блокирует функцию; `finally { msg-send.disabled=false; typing.hidden=true }` (288-291) не выполнится, пока fetch не ответит.
- **Почему баг:** сервер «молчит» (завис, пакет потерян, TCP держит) → `msg-send` остаётся disabled, «думаю…» горит бесконечно, поле ввода заблокировано. Пользователь не может ни переотправить, ни понять, что зависло — только перезагрузка. Классический hung-promise UX.
- **Чинить:** `AbortController` + таймаут на всех клиентских fetch. В коде уже есть `withTimeout` (308-311) для голоса — переиспользовать для текста.

### B-CORE-3 · CRIT · Лента треда «замерзает» после 500 записей
- **Файл:** app.js:228, 231, 235 (`pollFeed`)
- **Что:** запрос с `?limit=500`. Логика инкрементальная: `const fresh = data.entries.slice(feedCount)`, `feedCount = data.entries.length`. Если на сервере накопилось >500, окно — всегда последние 500, а `feedCount` после первого рендера = 500.
- **Почему баг:** как только `data.entries.length === 500` и `feedCount === 500`, `slice(500)` всегда возвращает `[]` — новые сообщения треда больше **никогда** не появятся в UI. Для долгоживущих сессий это полный death ленты. Дополнительно (см. B-CORE-7): если `feedCount` уменьшится из-за гонки, `slice(feedCount)` вернёт уже показанное → дубликаты.
- **Чинить:** курсорная/`since`-пагинация (передавать серверу последний увиденный id/ts вместо `slice(feedCount)`), либо хотя бы `feedCount = Math.max(feedCount, data.entries.length)` + дедуп по id записи.

### B-CORE-4 · MAJOR · Сетевая ошибка добавления проекта молча закрывает пикер как «успех»
- **Файл:** app.js:453-463 (`picker-choose`)
- **Что:** `const res = await postJSON("/api/projects", {...}).catch(() => null);` → `if (res && !res.ok) { …; return; }` → `picker.hidden = true; loadLists();`. Когда fetch падает (`.catch → null`), условие `res && !res.ok` ложно, код проваливается в ветку «успеха».
- **Почему баг:** пользователь видит нормальное закрытие пикера, думает, что проект добавился. Обнаружит пропажу только когда не найдёт папку в сайдбаре. Молчаливая потеря действия.
- **Чинить:** различать `res === null` (сеть) как отдельную ветку ошибки перед `!res.ok`: `if (res === null) { pickerPath.textContent = "⛔ нет связи"; return; }`.

### B-CORE-5 · MAJOR · Голосовой `onError` не сбрасывает `client` и состояние кнопки
- **Файл:** app.js:334 (`onError` в `connectVoice`)
- **Что:** колбэк только логирует и пишет в `conn-status`. Не обнуляет `client`, не зовёт `setMicState("error")`, не трогает `connecting`. `mic-btn` остаётся в `data-state="on"`, диалог «🎙 слушаю» остаётся активным.
- **Почему баг:** после обрыва голоса (ошибка ICE, падение бота) UI противоречив: статус кричит «⛔ ошибка», кнопка мигает «слушаю». `probeSession` (379-405) не поможет сразу — он ждёт 2 промаха по 5с. Плюс `client` остаётся не-null, следующие нажатия микрофона идут в ветку «отключить» (343-348) на мёртвом клиенте.
- **Чинить:** в `onError` при `client === me`: `client = null; setMicState("error", "соединение прервано");` — симметрично с `onDisconnected`.

### B-CORE-6 · MAJOR · Drawer и Picker — модалки без focus-trap, Escape и scroll-lock
- **Файл:** app.js:408-419 (drawer), 448-463 (picker); index.html:39 (backdrop), 67-76 (picker)
- **Что:** drawer открывается классом `drawer-open` + `backdrop.hidden=false`, закрывается только кликом по `side-close`/`backdrop`. Picker (`#picker`) — вообще без бэкдропа, закрытие только по `picker-cancel`. Никаких `keydown Escape`, перехвата Tab, `aria-modal`/`role="dialog"`. Скролл фона не блокируется.
- **Почему баг:** (1) A11y: пользователь с клавиатуры/скринридером «проваливается» Tab-ом в скрытый контент под модалкой, нет фокус-ловушки, нет Escape. (2) Mobile: на iOS прокрутка фона за модалкой (scroll chaining), особенно в picker при длинном списке папок. (3) Нет `aria-modal`/`aria-live` → скринридер не понимает, что открылся диалог.
- **Чинить:** на открытие — слушать `Escape` → close; перехватывать Tab внутри; `document.body.style.overflow = "hidden"`; `role="dialog" aria-modal="true"` на `#picker-panel` и сайдбар-контейнер.

### B-CORE-7 · MAJOR · Гонка `pollFeed` рассинхронизирует `feedCount` → дубликаты сообщений
- **Файл:** app.js:224-237 + 285, 469, 472
- **Что:** `pollFeed` зовётся из трёх источников одновременно: `setInterval(pollFeed, 3000)` (469), `sendMessage` через `Promise.all([pollFeed(), …])` (285), `visibilitychange` (472). Каждый — независимый `getJSON`, после — `feedCount = data.entries.length` (235). Поздний ответ с меньшим `data.entries.length` перезапишет больший → следующий poll вызовет `slice(feedCount)` с уменьшенным индексом.
- **Почему баг:** визуальные дубликаты в ленте («привет» дважды). При активном обмене с агентом воспроизводимо. Подрывает доверие к ленте.
- **Чинить:** дедуп по `entry.id` (id должно приходить с сервера): `Set` отрендеренных id и skip. Либо сериализовать pollFeed через мьютекс/`inFlight`-флаг.

### B-CORE-8 · MAJOR · Динамические статусы без `aria-live` — ошибки и «думаю» не анонсируются
- **Файл:** index.html:58 (`conn-status`), 56 (`typing`), 44 (`thread-badge`), 35 (`kora-card-sub`)
- **Что:** все четыре узла меняются через `textContent`/`hidden` (`setConn`, `setKora`, `renderBadge`, toggle typing), но ни у одного нет `aria-live`. `conn-status` — основной канал ошибок («нет связи», «⛔ ошибка»), и он невидим для скринридера.
- **Почему баг:** пользователь с TalkBack/VoiceOver не узнаёт, что отправка упала или голос отвалился — сидит перед «молчащей» кнопкой. WCAG 4.1.3 (Status Messages).
- **Чинить:** `conn-status` → `aria-live="assertive"`; `typing`, `kora-card-sub` → `aria-live="polite"`; `thread-badge` → `role="status"` + `aria-live="polite"`.

### B-CORE-9 · MAJOR · `loadLists` при первой загрузке молча проглатывает ошибку → пустой UI без обратной связи
- **Файл:** app.js:179-194 (особенно 184); init в 466
- **Что:** `loadLists` обёрнут в `try { … } catch { return; }`. На холодном старте при отсутствии сети `threads`/`projects` остаются `[]`, `catch` молча выходит. `render()` отрисует пустой сайдбар, пустые «Недавние», никаких «нет связи» / «повторите».
- **Почему баг:** пользователь видит абсолютно пустое приложение без подсказки. Нет индикатора загрузки, нет состояния ошибки, нет retry. На нестабильном мобильном интернете — типичный сценарий.
- **Чинить:** различать первичную загрузку от фоновой; при первом падении показывать в `view-home`/`conn-status` «не удалось загрузить — потяните для повтора». Минимум — `setConn("нет связи с сервером")` в catch если `threads.length === 0`.

### B-CORE-10 · MINOR · `maybeReload` — низкий порог промахов, может убить unsaved-ввод
- **Файл:** app.js:379-404 (`probeSession`), 372-377 (`maybeReload`)
- **Что:** watchdog считает `aliveMisses >= 2` (2 подряд `active:false` с интервалом 5с = 5-10 секунд). Если авто-реконнект падает, зовётся `maybeReload()`. Порог невысокий, `/session-alive` может кратковременно лгать. Никакой экспоненциальной задержки, никакого подтверждения.
- **Почему баг:** при кратковременной рассинхронизации сервера — внезапный `location.reload()`, теряется набранный текст `msg-input` (он нигде не сохраняется), WebRTC-сессия пересоздаётся. На слабом соединении воспроизводимо.
- **Чинить:** поднять порог до 3-4 промахов (15-20с стабильно «нет сессии»), перед reload сохранять `msg-input.value` в sessionStorage.

### B-CORE-11 · MINOR · `pickerPath` перезаписывается ошибкой, путь теряется
- **Файл:** app.js:458
- **Что:** при ошибке добавления проекта `pickerPath.textContent = "⛔ " + (data.error || …)` перезаписывает текущий путь.
- **Почему баг:** лёгкая дезориентация в пикере после ошибки — список директорий виден, но контекст «где я» заменён на текст ошибки.
- **Чинить:** показывать ошибку в отдельном элементе (`#picker-error`), не трогая `pickerPath`.

### B-CORE-12 · MINOR · Нет лоадера/скинера при `browse()` — быстрые тапы плодят гонку
- **Файл:** app.js:427-446
- **Что:** `browse()` ничего не показывает во время fetch. Тап по поддиректории запускает новый `browse`, ответ от предыдущего может прийти позже и перерисовать список на устаревший путь (`pickerDirs.replaceChildren()` 435).
- **Почему баг:** на медленной сети тап «папка A» → «папка B» даёт сначала B, потом внезапно A. Рассинхронизация навигации пикера.
- **Чинить:** счётчик запросов / токен отмены: игнорировать ответ не от последнего `browse`. Плюс индикатор в `pickerPath`.

### B-CORE-13 · MINOR · `render()` на каждый чих шлёт `POST /api/active-thread`
- **Файл:** app.js:65-95 (особ. 88-91); `setActiveProject` (51-56) зовёт `render()`
- **Что:** `render()` завершается безусловным `postJSON("/api/active-thread", …)` без дребезгозащиты. Любой тап по проекту (toggle → `render`) шлёт запрос.
- **Почему баг:** лишний трафик + риск рассинхрона active-thread при быстрых переключениях (последний запрос не обязательно дойдёт последним).
- **Чинить:** дебаунс `postJSON("/api/active-thread", …)`, либо вынести из `render()` и звать только при `hashchange`/явном изменении active project.

### B-CORE-14 · MINOR · Несоответствие значков `last_outcome` между бейджем и карточкой
- **Файл:** app.js:101-103 (`renderBadge`) vs 111 (`threadCard`)
- **Что:** бейдж: `✖` для failed, `⏹` для cancelled, иначе (включая неизвестный outcome, пустую строку, `running`, `queued`) рисует `✓ готово`. Карточка в сайдбаре: `✖` для failed, `✓` только для `completed`, иначе пусто. Один `last_outcome` в двух местах интерпретируется по-разному.
- **Почему баг:** тред в `running`/`queued` на бейдже отмечен «✓ готово» (вводит в заблуждение), в сайдбаре — пусто. Неконсистентность.
- **Чинить:** вынести логику в одну функцию `outcomeLabel(outcome)`, использовать и в `renderBadge`, и в `threadCard`. Явно обработать `running`/`queued`.

### B-CORE-15 · MINOR · Нет `enterkeyhint`/`inputmode` на `msg-input`; iOS-раскладка и IME
- **Файл:** index.html:61; обработчик app.js:294
- **Что:** `<input type="text" …>` без `enterkeyhint="send"`. На iOS Enter показывает «return». Плюс `if (e.key === "Enter") sendMessage()` не различает основной Enter и Shift+Enter/IME composition (мобильный Enter при вводе Кандзи/эмодзи может прийти как `keydown` во время composition).
- **Почему баг:** (1) Пользователь не сразу понимает, что Enter = отправка. (2) Для IME (китайский/японский, эмодзи-клавиатура iOS) Enter-подтверждение composition сработает как send и отправит недописанное.
- **Чинить:** `enterkeyhint="send"`. В keydown проверять `e.isComposing` или `e.keyCode === 229` и игнорировать.

### B-CORE-16 · MINOR · Программный `.focus()` на `msg-input` на iOS может не поднять клавиатуру
- **Файл:** app.js:418 (хендлер `new-thread`)
- **Что:** `$("msg-input").focus()` синхронно в обработчике клика. На iOS Safari программный focus часто не открывает клавиатуру, если ставится не в том же тике жеста (а перед ним `closeDrawer()`, меняющая layout).
- **Почему баг:** жмёт «＋ новый тред», drawer закрывается, но клавиатура не выезжает — приходится тапать по полю вручную.
- **Чинить:** `requestAnimationFrame(() => $("msg-input").focus())` после закрытия drawer.

### B-CORE-17 · MINOR · `DeprecationWarning: model=` в LLM/STT-сервисах (перенесён из архива 07-11, всё ещё жив)
- **Файл:** synapse/cascade/services.py:35-36 (OpenRouterLLMService, AnthropicLLMService), synapse/pipeline/app.py:436 (DeepgramFluxSTTService)
- **Что:** pipecat депрекейтит `model=` в конструкторах сервисов в пользу `settings=Service.Settings(model=…)`. Три сайта передают `model=` позиционно/ключом.
- **Почему баг:** варнинги спамят лог (16/прогон), API помечено к удалению в будущей версии pipecat — после апгрейта молча сломается.
- **Чинить:** `settings=Service.Settings(model=cfg.xxx_model)` (Service — класс настроек конкретного сервиса).

---

## B-UI · style.css + status-widget.js + logs.html + manifest.webmanifest

### B-UI-1 · MAJOR · `--dimmer` (#5c7089) не проходит WCAG AA для мелкого текста
- **Файл:** style.css:10 (использование: :25 `h2`, :79 `.tc-meta`, :132 `#feed-list:empty::before`)
- **Что:** `--dimmer: #5c7089` даёт **3.78:1 на `#0b0f14`** и **3.45:1 на `--surface`**. WCAG AA требует ≥4.5:1. Применяется к `h2`, `.tc-meta`, плейсхолдеру «пока пусто».
- **Почему баг:** метаданные треда и заголовки «Проекты / Без проекта / Недавние» теряются на тёмном фоне; при дневном свете на iPhone (блики) 12px текст по сути нечитаем.
- **Чинить:** поднять до ~`#6b7e98` / `#7388a0` (≈4.6:1 на bg).

### B-UI-2 · MAJOR · logs.html: мета/результат/системный текст (#6b7683) ниже AA, ещё и в 11px
- **Файл:** logs.html:22 (`.meta`), :27 (`.kind-tool_result .text`), :29 (`.kind-system .text`), :30 (`#empty`)
- **Что:** `#6b7683` на `#0b0f14` = **4.16:1** (под 4.5:1). `.meta` имеет `font-size: 11px`. Тот же цвет несёт метку времени и `kind` каждой записи, плюс весь класс `tool_result`/`system` (целые классы ленты, не редкие).
- **Почему баг:** страница «Размышления Коры» по сути состоит из `thinking/tool_use/tool_result` — одна из основных цветовых категорий нечитаема. 11px моношейп-метка на мобиле неразборчива.
- **Чинить:** `.meta` → 12-13px; `#6b7683` → `#8795a5`+ (≈5:1). Для `kind-system/tool_result` — отдельный более светлый серо-голубой.

### B-UI-3 · MAJOR · Нет ни одного `:focus-visible` / `:active` на тач-таргетах
- **Файл:** style.css (нет ни одного правила); затронуты `#new-thread`, `#msg-send`, `#mic-btn`, `#add-project`, `.thread-card`, `.project-row`, `#picker-dirs li`, `#picker-actions button`, `#kora-card`, `#side-close`, `#menu-btn`
- **Что:** единственный focus-стиль — `#msg-input:focus { outline: none; … }` (style.css:187), причём `outline: none` без замены focus-ring. Все кнопки/карточки имеют только `:hover` (`.thread-card:hover` :74, `.project-row:hover` :58, `#picker-dirs li:hover` :229), без `:active` и `:focus-visible`.
- **Почему баг:** (1) На iPhone нет hover — тап по карточке/пункту пикера не даёт мгновенной отдачи, только задержка до перерисовки. (2) Блютуз-клавиатура / VoiceOver / Switch Control на iOS не видят, где фокус. (3) `outline:none` на инпуте без кольца.
- **Чинить:** `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }` для интерактивных; `:active { opacity: .7; }` (или background-свап) для тач-отдачи.

### B-UI-4 · MAJOR · Нет `prefers-reduced-motion` — две infinite-анимации и переход drawer'а
- **Файл:** style.css:161 (`@keyframes blink` infinite), :196 (`#mic-btn[data-state="connecting"]` → `pulse` infinite :199), :206 (`transition: transform .22s`)
- **Что:** «думаю…» мигает точками (`blink 1.2s infinite`) всё время ответа Коры; `pulse` пульсирует mic-кнопкой в connecting. Никакого `@media (prefers-reduced-motion: reduce)` нигде нет.
- **Почему баг:** для пользователей с «Уменьшить движение» (вестибулярные расстройства, мигрень) мигание — триггер. Плюс infinite-анимация постоянно гоняет compositor на iOS → батарея/прокрутка.
- **Чинить:** обернуть в `@media (prefers-reduced-motion: no-preference) { …blink/pulse… }`, либо в `@media (prefers-reduced-motion: reduce) { animation: none; transition: none; }`.

### B-UI-5 · MAJOR · status-widget.js: нет IIFE, загрязняет `window` хост-страницы
- **Файл:** status-widget.js:9 (`COLORS`), :11 (`dot`), :29 (`poll`)
- **Что:** скрипт (по комментарию — injectится «поверх prebuilt UI», т.е. в чужую страницу) объявляет `COLORS`, `dot`, `poll` как top-level `const`/function. В classic-скрипте это глобальные биндинги, попадают в `window`-scope и **могут конфликтовать** с переменными приложения хоста (имя `poll` — крайне частое).
- **Почему баг:** встраиваемый виджет должен быть self-contained. Любое совпадение имён = `SyntaxError: Identifier 'poll' has already been declared`, который валит ВЕСЬ скрипт виджета (молчаливо — юзер видит серую точку «неизвестно» навсегда, без диагностики).
- **Чинить:** обернуть всё в `(() => { … })();` (или `;(() => { ... })()` для безопасности от чужого кода выше).

### B-UI-6 · MAJOR · status-widget.js: `res.json()` не защищён try/catch — обещанная обработка ошибок дырявая
- **Файл:** status-widget.js:30-45
- **Что:** `try/catch` ловит только `fetch` (:32). Дальше `await res.json()` (:41) и `COLORS[data.color]` — вне try. Если сервер вернёт 200 с мусором/обрезанным телом (таймаут прокси, partial response), `res.json()` бросит `SyntaxError`, который вылетит в `setInterval`-обработчик → этот полл упадёт, но `setInterval` продолжит стрелять каждые 3с и падать, спамя консоль.
- **Почему баг:** комментарий в шапке обещает «Ошибка сети = серый "неизвестно"» — но поломанное тело не считается ошибкой по этой реализации. В tailnet с rc-буферами/прокси это реальный сценарий.
- **Чинить:** перенести всю обработку ответа внутрь try, или отдельный try вокруг `res.json()`.

### B-UI-7 · MINOR · status-widget.js: `location.href = "./logs"` — относительный путь ломается вне корня
- **Файл:** status-widget.js:24-26
- **Что:** клик по точке ведёт на `"./logs"`, разрешается **относительно URL внедряющей страницы**. Маршрут сервера — `/client/logs`. Если виджет встроен на страницу с путём не `/client/`, `./logs` уведёт на несуществующий путь → 404.
- **Почему баг:** для self-contained виджета относительная навигация хрупкая, при смене пути хост-страницы молча ломается.
- **Чинить:** абсолютный `/client/logs` (classic-скрипт — просто хардкод).

### B-UI-8 · MAJOR · logs.html и виджет: poll-интервалы не ставятся на паузу в фоновой вкладке
- **Файл:** logs.html:77 (`setInterval(poll, 2000)`), status-widget.js:48 (`setInterval(poll, 3000)`); `visibilitychange` (:78, :49) только триггерит немедленный poll, не останавливает интервал
- **Что:** оба крутят `setInterval` бесконечно. iOS Safari в standalone PWA при уходе вкладки/экрана в фон дросселирует таймеры (до 1Гц, потом может убить), но не мгновенно — каждые 2-3с летит `fetch("./kora-log")`, пока страница жива. На lock screen PWA может висеть часами.
- **Почему баг:** непрерывные сетевые poll'ы разряжают батарею и держат network-radio активным. `visibilitychange`-хендлер есть, но используется только для мгновенного ре-полла, не для паузы.
- **Чинить:** в `visibilitychange` дополнительно `clearInterval(timer)` при `document.hidden` и `setInterval`/первый poll при возврате.

### B-UI-9 · MINOR · manifest: иконка 512 без `purpose: "maskable"` — обрезка на Android
- **Файл:** manifest.webmanifest:11
- **Что:** только `purpose: "any"`. Android (Chrome) для adaptive-icon применяет `maskable`, при отсутствии берёт `any` и **обрезает по кругу/сквидам** — если логотип не по центру с safe-zone, обрежется. iOS это не касается.
- **Почему баг:** installable-опыт на Android подпорчен. Спека iOS-first прощает.
- **Чинить:** `"purpose": "any maskable"` для icon-512 (лучше — отдельная maskable-иконка с safe-zone).

### B-UI-10 · MINOR · manifest: нет `description` / `display_override` — слабее installability-метаданные
- **Файл:** manifest.webmanifest (весь)
- **Что:** отсутствуют `description`, `display_override`, `categories`, `lang`, `dir`, `id`. Lighthouse PWA пометит `description` как warning. `display_override: ["standalone","minimal-ui"]` дал бы fallback.
- **Почему баг:** не ломает функциональность, снижает installability score и качество установки.
- **Чинить:** добавить `description`, `lang: "ru"`, при желании `display_override`.

### B-UI-11 · MINOR · logs.html: моношейп в основном font-stack — мёртвый код
- **Файл:** logs.html:12 (`font: 14px/1.5 -apple-system, "SF Mono", Menlo, monospace;`)
- **Что:** первый `-apple-system` (пропорциональный), но `"SF Mono"`/`Menlo`/`monospace` в fallback подозрительны — при сбое наследования текст станет моношейпом. Никакого `font-family: monospace`-переопределения в коде нет — моно-часть стека мёртвая.
- **Почему баг:** мёртвый код; если кто-то уберёт `-apple-system` (или на не-Apple платформе) — UI превратится в сплошной моношейп.
- **Чинить:** `font: 14px/1.5 -apple-system, system-ui, sans-serif;` (моно вынести в отдельный класс для `pre`/кода).

### B-UI-12 · MINOR · `.tc-meta` / `.meta` (logs) — 12px и 11px: под порогом читабельности на мобиле
- **Файл:** style.css:79 (`.tc-meta { font-size: 12px; }`), logs.html:22 (`.meta { font-size: 11px; }`), style.css:144 (`.feed-system { font-size: 13px; }`), :168 (`#conn-status { font-size: 13.5px; }`)
- **Что:** ряд вторичных текстов 11-13px. Apple HIG / Material рекомендуют ≥13-14px. Метка времени в 11px моношейп на retina-мобиле с подсветкой — на грани.
- **Почему баг:** метаданные (время, kind) прочесть без zoom'а тяжело, особенно на iPhone SE.
- **Чинить:** `.meta` → 12px, `.tc-meta` → 13px.

### B-UI-13 · MINOR · `#hero { margin: 10vh auto 24px; }` — на landscape iPhone съедает экран
- **Файл:** style.css:122
- **Что:** hero отступает 10vh сверху. В landscape на iPhone SE высота viewport ~320px; на iPhone 14+ landscape ~393px, с safe-area может остаться меньше, и `#hero` + `#home-recent` не влезут без скролла (`.view` имеет `overflow-y:auto`, не ломается, но «первый экран» пустой).
- **Почему баг:** при повороте hero уезжает вверх, контент «Недавних» теряется.
- **Чинить:** `margin: clamp(24px, 6vh, 80px) auto 24px;` или media-query на landscape.

### B-UI-14 · MINOR · `#kora-card` — `<a href="./logs">`, но визуально карточка; нет аффорданса и focus
- **Файл:** style.css:82-87; index.html:31
- **Что:** `#kora-card` — это `<a>`, стилизованная как блок. Имеет `:hover { background }` (:87), но нет `:focus-visible`, нет `:active`. На тач-устройстве непонятно, что это кликабельная навигация (нет chevron, курсор не работает).
- **Почему баг:** discoverability — переход на важную страницу спрятан за неявно-кликабельным блоком.
- **Чинить:** добавить визуальный аффорданс (chevron `›`) и `:active`/`:focus-visible` стейты.

---

## Проверено и чисто

**app.js / index.html (B-CORE):**
- **XSS / injection:** все серверные данные (имена тредов, имена/пути проектов, имена директорий пикера, текст ленты, task_text Коры) вставляются только через `textContent` (`el()`, прямые присваивания) или `className`. `innerHTML` с серверными данными нет. `eval`/`Function` тоже нет.
- **Identity-guard в голосовом клиенте:** колбэки PipecatClient корректно захватывают `me` и проверяют `if (client === me)` (324-334) — поздние колбэки от старой сессии не гасят новую после реконнекта.
- **Сброс состояния ленты при переключении треда:** `feedThread`/`feedCount` переустанавливаются в `render()` (75-79), проверяются в `pollFeed` (230) после await — поздний ответ от треда A не дорисуется в открытый тред B.
- **Двойной submit / спам send:** `msg-send.disabled = true` (265) до сети, Enter при пустом уходит по `if (!text) return` (263). Защита адекватна (исключение — потеря текста, B-CORE-1).
- **Утечки слушателей:** все top-level обработчики навешиваются один раз на статические элементы; динамические узлы создаются через `replaceChildren()`, старые GC'ятся вместе со слушателями — накопления нет.
- **WebRTC / websocket lifecycle:** ручное и авто-отключение зовёт `disconnect().catch(() => {})`, `connecting`-флаг блокирует повторный вход в `connectVoice`, `probeSession` не стартует пока `connecting` — реентерабельность соблюдена.
- **`nearBottom`-стик при авто-скролле ленты:** корректно разделяет «первая загрузка» и «рядом со дном» (233), не дёргает скролл при чтении истории.

**style.css / виджеты / manifest (B-UI):**
- **color-scheme: dark + theme_color #0b0f14** — консистентно между `:root` style.css:3, `meta theme-color` index.html:12/logs.html:9, manifest строки 7-8.
- **env(safe-area-inset-*)** — корректно в `#sidebar` padding-top (:39), `#kora-card` padding-bottom (:85), `#topbar` padding-top (:101), `#composer` padding-bottom (:171), виджет top (:14), logs header/feed (:15, :20). Под notch/home-bar контент не уезжает.
- **100dvh** в `#shell` (:32) — корректнее `100vh` на iOS (решает shrinking адресной строки).
- **XSS в logs.html и status-widget.js** — действительно только `textContent` (logs.html:54, 57; виджет только `style`/`title`/`textContent` через `data.*`). Сырой HTML нигде. Осознанный выбор.
- **`#feed-list:empty::before`** (style.css:130) — `:empty` работает: app.js делает `replaceChildren()` (78), при пустом фиде селектор матчится. Не orphan.
- **Селекторы style.css vs DOM** — сверены все id/классы с index.html и app.js: `.msg-user`/`.msg-bot` (app.js:200), `.feed-*` kinds из `bridge/kora.py`, `.project-row`/`.thread-card`/`.tc-*` (app.js:109, 125), picker-узлы (app.js:422+). Orphan-классов нет.
- **`overscroll-behavior: none`** (style.css:20) — правильно для PWA, предотвращает pull-to-refresh поверх чата.
- **`#proj-chip` ellipsis + `white-space:nowrap`** (:179) — длинный путь проекта обрезается многоточием, не ломает композер.
- **drawer на мобиле** (style.css:203-213) — `width: min(300px, 84vw)` корректно для SE; `transform: translateX(-102%)` исключает «призрак» border'а.
- **z-index виджета (2147483647)** — легитимный max-int для overlay над чужим UI; не конфликтует с picker'ом (z-index:20), т.к. виджет на отдельной странице.
