# Диспетчер ↔ Кора: спека имплементации Фазы 0

**Статус:** к исполнению. Источник — `docs/dispatcher-kora-ideal-architecture.md`
(proposal + находки ревью кода CR-1…CR-10, 2026-07-14). Это НЕ ещё один анализ:
каждый слайс ниже — призыв к действию с конкретными файлами, эскизами кода и DoD.

> **For agentic workers:** домашний воркфлоу — каждый слайс = отдельный tero-ран
> (`~/.claude/skills/tero/SKILL.md`). Эскизы кода в спеке — контракты, не готовые
> диффы: точные правки рождаются в план-фазе рана. Замороженные тесты не редактируются.

**Принцип Фазы 0 (одна строка):** LLM интерпретирует намерение и формулирует речь;
факты о выполнении, полномочия и границы доступа переезжают в детерминированный код.

## Скоуп

**Входит:** все 10 пунктов Фазы 0 proposal-а, перегруппированные в 7 слайсов
(С0–С6). Только текущий рантайм: TaskStore-синглтон, callbacks, RunSpec — живут.

**НЕ входит (не обещать в PR-ах Фазы 0):**
- RunManifest / durable outbox / sequence-ack / TaskRegistry — Фаза 1;
- capability broker, изоляция worker-а, git-checkpoint перед run (CR-8),
  per-path lock конкурентных tool-call-ов (CR-10) — Фаза 2;
- сетевой запрет worker→control plane — невозможен, пока Кора живёт в одном
  процессе с хостом; остаётся явным residual risk (см. «Остаточные риски»);
- CR-9 (Кора — доверенный рассказчик, tool-input values в display-ленте) —
  поверхность записана в proposal, лечится `ToolFact`-ами Фазы 1, здесь не трогаем.

## Global Constraints (действуют во всех слайсах)

- Коммиты: короткие, lowercase, по-человечески; **никакой** AI-атрибуции; без `feat:`/`fix:`.
- **NO-EXFIL (Р-15):** сырые кора-шаги никогда не в LLM-контекст диспетчера.
- **Синглтон «одна активная задача»** (TaskStore) в Фазе 0 не меняется.
- Ключи только из `.env` через `SynapseConfig.from_env()`; никогда не хардкод.
- **Живой сервер 7860 не рестартовать без слова Теро**; проверки — тестами и staging 7861 / tailnet :8443.
- Запуск тестов: `.venv/bin/python -m pytest tests/ -q`. База на старте: 566 passed / 1 xfailed (B15) + незакоммиченные `test_b_pipe_bugs.py`/`test_concurrency_race.py` (С0 их коммитит).
- Каждый слайс заканчивается полностью зелёной суитой; замороженные тесты не редактируются — расхождение = сигнал о неверном плане, не о «плохом тесте».

## Карта слайсов

| # | Слайс | Пункты proposal | Tero-тир | Зависит от |
|---|-------|-----------------|----------|------------|
| С0 | Дренаж | Ф0.10, CR-1, CR-4, CR-5, CR-6 | S/M | — |
| С1 | TurnContext: единый контекст хода | Ф0.1 | M | — |
| С2 | Жизненный цикл хода в журнале | Ф0.2 | M | С1 (соседний код) |
| С3 | ApprovalService для gate_action | Ф0.3 | **L** (полномочия) | С2 (note_user_turn провода) |
| С4 | Текстовый канал: cost cap + fallback | Ф0.4 + Ф0.9 (CR-7) | M | — |
| С5 | Authn для /api/* | Ф0.5 | **L** (auth) | — |
| С6 | Кора-hardening: env, journal_dir, deadline | Ф0.6 + Ф0.7 (CR-2) + Ф0.8 (CR-3) | **L** (гейт) | — |

Рекомендуемый порядок: С0 → С1 → С2 → С3, дальше С4/С6/С5 в любом порядке
(независимы). С0 обязан идти первым: он чинит мину, на которую наступает любой
следующий слайс, трогающий `SynapseHost`.

---

# С0 — Дренаж (CR-1, CR-4, CR-5, CR-6)

Мелкий, но обязательный: убирает мины из-под всех остальных слайсов.

### Task 0.1: сеттер `current_http_thread` перестаёт инициализировать чужие поля (CR-1)

Сеттер (`synapse/pipeline/app.py:159-170`) содержит инициализацию `_output_task`
и `_gate_locks`; комментарий в `__init__` обрывается на полуслове (`app.py:153`).
Работает случайно — присваивание в `__init__` дёргает сеттер один раз. Любое
повторное `host.current_http_thread = …` молча отвяжет живой `PipelineTask`
(тихий дроп SPEAK, класс B17) и сбросит все per-thread гейт-локи; такое
присваивание уже есть в `tests/test_b_pipe_bugs.py:180`.

**Правка** (`app.py`):

```python
# __init__, после self.turn_lock = ...:
self.current_http_thread = current_http_thread if current_http_thread is not None else {"id": None}
# M1 slice 2 (the one NON-long-lived field, see class docstring): the currently live
# per-connection PipelineTask, or None when no client is connected.
self._output_task: Any = None
# UI-4: per-thread гейт-локи — single-flight двух конкурентных гейт-вызовов на один тред.
self._gate_locks: dict[str, asyncio.Lock] = {}

@current_http_thread.setter
def current_http_thread(self, value: dict | None) -> None:
    if isinstance(value, TaskLocalThreadDict):
        self._current_http_thread = value
        return
    self._current_http_thread = TaskLocalThreadDict()
    if isinstance(value, dict) and "id" in value:
        self._current_http_thread["id"] = value["id"]
```

⚠️ Порядок в `__init__`: присваивание `current_http_thread` дёргает сеттер, который
больше НЕ создаёт `_output_task`/`_gate_locks` — их инициализация обязана стоять в
`__init__` независимо (иначе первый же `bind_output` падает AttributeError).

**Тест:** повторное `host.current_http_thread = {"id": "x"}` при забинденном
`_output_task` и непустых `_gate_locks` — оба переживают присваивание.

### Task 0.2: асимметрия answer-гвардов — осознанность фиксируется (CR-4)

`_voice_answer` пропускает ответ при `voice_thread=None` (`app.py:629`),
`_http_answer` строг (`app.py:637`). Решение: **оставить асимметрию** —
голос — канал дома, вопрос Коры прозвучал вслух; строгий гвард сломал бы ответ
после реконнекта, когда `voice_thread` ещё `None`. Но осознанность фиксируется:

- комментарий у `_voice_answer` («асимметрия с _http_answer намеренная: …»);
- якорь-тест: `voice_thread=None` + awaiting → голосовой ответ доставляется;
  HTTP-ответ из чужого треда → `no_pending_question`.

### Task 0.3: busy-check гейта становится структурным (CR-5)

`gate_action` (`app.py:327-409`) корректен только потому, что между
`store.has_active_task()` и `_launch_run` нет ни одного await (гейт-локи —
per-thread, а стор — глобальный синглтон). Появится await — два треда запустят
две Коры. Конвенцию «нет await» превращаем в структурный инвариант:

```python
# SynapseHost.__init__:
self._launch_lock = asyncio.Lock()   # CR-5: busy-check и launch атомарны на ХОСТЕ

# gate_action, ветка запусков:
is_run = action in ("send_to_kora", "write_code")
if is_run:
    async with self._launch_lock:
        if self.kora_runner is not None and self.store.has_active_task():
            return {"error": "busy"}
        ...  # обе ветки send_to_kora / write_code, до _launch_run включительно
```

Дедлок-анализ: единственный порядок — thread-lock → launch-lock; обратного пути
нет; внутри лока сегодня ни одного await (`_launch_run` синхронный), лок дешёв.

**Тест:** monkeypatch `threads.set_stage` на await-ящую версию, два конкурентных
`gate_action` на два разных треда → ровно один `kora_runner.start`, второй
получает `busy`. Без лока этот тест ловит регрессию, с локом — детерминирован.

### Task 0.4: закоммитить бесхозные регрессионные якоря (CR-6)

`tests/test_b_pipe_bugs.py`, `tests/test_concurrency_race.py` — регрессионные
якоря уже закоммиченных фиксов, живут без истории. `git add` + коммит вместе с
Task 0.1-0.3 (сеттер-фикс делает `test_b_pipe_bugs.py:180` корректным по построению).

**Развилка для Теро:** судьба `staging_7861.py` / `staging_bverify.py` /
`staging_smoke.py` — рекомендация: закоммитить в `tools/staging/` (они — рабочий
станок staging-канала), альтернатива — удалить как одноразовые.

**DoD С0:** суита зелёная; новые якоря в истории; повторное присваивание
`current_http_thread` безвредно; конкурентный гейт-тест зелёный.

---

# С1 — TurnContext: единый контекст хода (Ф0.1)

**Мотив.** HTTP-ход собирает `system_prompt + "\n\n" + state_block`
(`synapse/dispatcher/loop.py:238`), голосовой `_on_end_of_turn` — только
`build_system_prompt(cfg, stage_block=…)` (`synapse/pipeline/app.py:844`):
голос не видит `[СОСТОЯНИЕ]` вообще. Роутинг `answer_kora` в голосе держится на
догадке модели — вероятностная гарантия вместо структурной.

### Task 1.1: фабрика `build_turn_context()`

Новый модуль `synapse/dispatcher/turn_context.py` — извлечение текущей инлайн-сборки
из `DispatcherTurnLoop._complete` (`loop.py:231-241`) + `_render_state` (`loop.py:261-270`):

```python
@dataclass(frozen=True)
class TurnContext:
    system_prompt: str   # база промпта + stage-правила
    state_block: str     # [СОСТОЯНИЕ] snapshot

    @property
    def system_message(self) -> str:
        return self.system_prompt + "\n\n" + self.state_block


def build_turn_context(
    *, cfg: SynapseConfig, store: TaskStore, clock: Clock, thread_id: str | None,
    task_dictionary: dict[str, str] | None = None,
    stage_block_for: Callable[[str | None], str] | None = None,
    owner_thread_for: Callable[[str], str | None] | None = None,
) -> TurnContext:
    stage_block = stage_block_for(thread_id) if stage_block_for is not None else ""
    prompt = build_system_prompt(cfg, task_dictionary or {}, stage_block=stage_block)
    task = store.task
    hide = (task is not None and owner_thread_for is not None
            and should_hide_task(task, thread_id, owner_thread_for(task.id)))
    state = store.render_state(clock.now(), cfg.stale_after_s,
                               cfg.unreachable_after_s, hide_task=hide)
    return TurnContext(system_prompt=prompt, state_block=state)
```

### Task 1.2: оба канала через фабрику

- `DispatcherTurnLoop._complete` → один вызов `build_turn_context(...)`;
  `_render_state` умирает (его тело переехало в фабрику).
- `build_host` вешает на хост резолвер:
  `host.turn_context_for = lambda tid: build_turn_context(cfg=cfg, store=store,
  clock=clock, thread_id=tid, stage_block_for=_stage_block_for,
  owner_thread_for=<та же лямбда, что у text_loop>)`.
- Голосовой `_on_end_of_turn` (`app.py:844-850`):

```python
voice_system = host.turn_context_for(host.voice_thread["id"]).system_message
context.set_messages([
    {"role": "system", "content": voice_system},
    *(m for m in context.get_messages() if m.get("role") != "system"),
])
```

Свежесть симметрична HTTP: снапшот на старте хода. `should_hide_task`-скоуп
(терминальная задача чужого треда) теперь работает и в голосе — раньше голос
вообще не видел состояния, теперь видит правильно отскоупленное.

**Вне скоупа слайса (parking):** вопрос Коры по-прежнему инжектится как
`TTSSpeakFrame(append_to_context=False)` (`app.py:207`) — класть ли сам вопрос в
голосовой контекст, это отдельное решение с Р-8-напряжением (текст вопроса =
вывод Коры). `[СОСТОЯНИЕ] awaiting_answer=true` закрывает роутинг структурно.

**Тесты:**
- голосовой system message содержит `[СОСТОЯНИЕ]` (сегодня RED — сам факт дыры);
- parity: при одинаковых входах system-строка голоса == system-строке HTTP;
- hide-скоуп в голосе: терминальная задача чужого треда спрятана;
- awaiting: `[СОСТОЯНИЕ]` в голосовом промпте несёт `awaiting_answer` → LLM видит
  основание позвать `answer_kora` без догадок.

**DoD С1:** «в каждом voice и HTTP вызове один и тот же `[СОСТОЯНИЕ]` snapshot»
— дословный критерий Ф0.1 закрыт тестом parity.

---

# С2 — Жизненный цикл хода в журнале (Ф0.2)

**Мотив — проверенный факт (ревью 2026-07-14, сильнее формулировки proposal):**
`journal.end_turn()` на happy-path зовёт только консоль (`synapse/runners/console.py:107`)
и exception-путь `loop.py:214`. Ни HTTP-роут, ни голос ход не закрывают. B08-бэкстоп
(`journal.py:93` — открытый ход возвращается как есть) превращает это в слияние:
ВСЕ войс/HTTP-ходы процесса пишутся в одну вечно открытую turn-запись, turn-строки
в JSONL не появляются вовсе (только alert-строки), `turn_id` не растёт. Плюс
`ToolHandlers._last_turn_id` (`tools.py:198-202`) — глобальный fallback: поздний
tool-хвост приписывается чужому ходу.

### Task 2.1: HTTP закрывает ход

`POST /api/threads/{id}/message` (`webrtc_server.py:585-589`) добавляет
`host.journal.end_turn()` в `finally` после `ingest_user_turn` (у HTTP нет
tts_texts, ждать нечего). Exception-путь уже закрывает сам (`loop.py:214`) —
`end_turn` идемпотентен (`journal.py:157`: `_current is None → return`), двойной
вызов безвреден.

### Task 2.2: голос закрывает ход на коммите ответа

Закрытие — в `_flush_voice_context` через on_commit-хук guarded-агрегатора
(момент, когда ответ реально зафиксирован, — то самое «remaining grounding-wiring
work» из B13-коммента `app.py:826-834`) и в `_flush_voice_final` (teardown).
Перед `end_turn` — `journal.check_grounding(record, had_active_task)`: голосовой
ход наконец получает grounding-проверку, как консольный.

### Task 2.3: убить глобальный `_last_turn_id`-fallback

Целевое состояние (полный `OperationContext` через command handler) — Фаза 1.
Минимум Фазы 0, убирающий misattribution:

- `ToolHandlers.end_turn()` — новый метод: `_last_turn_id = None`;
  хост зовёт его в тех же точках, что `journal.end_turn()`;
- поздний tool-хвост ПОСЛЕ конца хода получает честный `turn_id=""`
  (`tools.py:219` уже поддерживает), а не id следующего хода.

**Тесты:**
- JSONL содержит turn-строку на каждый HTTP-ход (сегодня RED);
- два последовательных войс-хода → разные `turn_id` в записях;
- late tool call после end_turn не приписан следующему ходу (turn_id пуст);
- замороженные: `test_journal.py` (B08-семантика внутри хода) и
  `test_B08_concurrent_begin_turn_must_not_steal_the_voice_turn_record`
  (`tests/test_hunt0714_a.py:283`) не меняются.

**Граница слайса (честно, вместо буквы Ф0.2).** Слайс закрывает
ПОСЛЕДОВАТЕЛЬНОЕ слияние — сегодняшнюю вечно открытую запись, в которую текут
все ходы подряд. ПАРАЛЛЕЛЬНОЕ перекрытие остаётся: `ingest_user_turn` намеренно
бежит вне `turn_lock` (`webrtc_server.py:579-583`, B-PIPE-5 — медленный клиент
не должен блокировать остальных), и при перекрытии B08-бэкстоп отдаёт второму
ходу запись первого — это ЗАМОРОЖЕННЫЙ инвариант (тест выше), а не баг.
Буквальное «два параллельных хода не делят record» из DoD Фазы 0 proposal-а в
этом слайсе недостижимо без ломки B08 (pipecat-хирургия полной сериализации —
парковый residual); формулировку DoD в proposal нужно скорректировать так же.

**DoD С2:** каждый ПОСЛЕДОВАТЕЛЬНЫЙ ход имеет свою begin/end-пару и свой
`turn_id`, записи не текут в следующий ход; параллельное перекрытие — явный
принятый residual (B08) до `OperationContext` Фазы 1.

---

# С3 — ApprovalService: `confirm=true` от LLM перестаёт быть властью (Ф0.3)

**Мотив.** `gate_action` доверяет булеву `confirm` из tool call
(`synapse/dispatcher/tools.py`, схема `gate_action`; `app.py:365,387` —
`if not confirm: return {"error": "confirm_required"}`). Модель может выставить
его сама — self-approval запуска кода. Двухключевой контракт уже существует и
проверен — `ConfirmFlow` (`synapse/bridge/confirm.py:103`): intervening user turn
+ детерминированный affirm/deny классификатор. Обобщаем ЕГО, не пишем с нуля.

### Task 3.1: вынести классификатор в общий модуль

`_normalize/_words/_classify_response` (`confirm.py:28-45`) → `synapse/bridge/affirm.py`;
`ConfirmFlow` импортирует оттуда. Поведение бит-в-бит, замороженные тесты
ConfirmFlow не меняются.

### Task 3.2: `ApprovalService` (новый `synapse/bridge/approvals.py`)

```python
@dataclass(frozen=True)
class Approval:
    approval_id: str      # "apr-{ms}-{seq}", одноразовый
    thread_id: str
    action: str           # "send_to_kora" | "write_code"
    digest: str           # sha256(request_text | action | model | fast | th.stage)
    issued_at: float
    expires_at: float

class ApprovalService:
    """Double-key контракт ConfirmFlow, обобщённый на gate_action:
    (a) между readback и consume обязан пройти user turn;
    (b) транскрипт этого turn-а обязан пройти affirm-проверку (affirm.py);
    (c) digest свода в момент consume обязан совпасть со staged. digest несёт
        И СТАДИЮ треда: любое движение стадии между stage() и consume()
        инвалидирует pending структурно, по несовпадению, — без обратной
        зависимости threads→bridge и без перечисления всех set_stage-путей.
    confirm=true из tool call не читается вообще — власть только здесь."""

    def stage(self, thread_id: str, action: str, digest: str, now: float) -> str: ...
        # запоминает pending, возвращает readback-текст для озвучки
    def note_user_turn(self, transcript: str, now: float) -> None: ...
        # та же точка входа, что ConfirmFlow.note_user_turn
    def invalidate(self, thread_id: str) -> None: ...
        # явная инвалидация с call site-ов app.py (смена СВОДА);
        # смену СТАДИИ ловит digest сам — invalidate для неё не нужен
    def consume(self, thread_id: str, action: str, digest: str, now: float) -> Approval | None: ...
        # None ⇔ нет staged / не было user turn / не affirm / digest сменился / TTL истёк
        # успех — одноразовый: pending гасится
```

TTL = `cfg.confirm_timeout_s`. Хранение v1 — in-memory: рестарт теряет pending,
пользователь подтверждает заново (принято; персист — вместе с audit-хранилищем Фазы 1).

### Task 3.3: вайринг в `gate_action`

Каналы различаются источником подтверждения:

- **HTTP `/api/threads/{id}/gate`** — `confirm:true` несёт клик живого пользователя
  по кнопке: роут передаёт `user_initiated=True`, ApprovalService не требуется
  (клик = сам пользователь и есть второй ключ).
- **Голосовой tool-путь** (`_voice_gate` → `gate_action`) — `user_initiated=False`:

```python
# gate_action, ветка запусков, под _launch_lock (С0):
if not user_initiated:
    digest = _gate_digest(th.request_text, action, model, fast, th.stage)
    approval = self.approvals.consume(th.id, action, digest, self.clock.now())
    if approval is None:
        readback = self.approvals.stage(th.id, action, digest, self.clock.now())
        return {"error": "confirm_required", "readback": readback}
    # approval_id уходит в журнал вместе с фактом запуска
```

`confirm`-поле в схеме инструмента остаётся (совместимость промпта), но на
голосовом пути игнорируется как authority. `note_user_turn` — рядом с
существующими вызовами `confirm_flow.note_user_turn` (`loop.py:160`,
`app.py:840`): один fan-out на оба сервиса.

**Вайринг `invalidate` — по слоям, не «где-то в threads»:** явные вызовы живут
на call site-ах в `app.py` — `_propose_for` после `threads.set_request`
(`app.py:582`) и ветка `revise` (`app.py:351`). НЕ внутри `ThreadStore` —
обратная зависимость threads→bridge запрещена. Диспозиция остальных двух точек
`set_stage`: `_launch_run` (`app.py:422`) идёт ПОСЛЕ `consume` — безвреден;
`_run_finished` (`app.py:323`) сегодня недостижим при pending только благодаря
B46-гварду `request_text is not None` (`app.py:313`) — на этот гвард НЕ
полагаемся: стадия в digest закрывает путь структурно, даже если он когда-нибудь
ослабнет. Архив: `set_archived` pending не гасит — покрыто archived-гвардом
`gate_action` ДО consume (`app.py:340`) плюс TTL; фиксируется тестом.

**Тесты (ядро — из «Первого implementation slice» proposal-а):**
- self-approval: `gate_action(confirm=true)` голосом без intervening turn → `confirm_required`;
- happy: readback → юзер «да» → повторный gate_action → запуск, approval одноразов;
- deny: «нет» → consume None; unclear → `confirm_required` заново;
- invalidation: смена `request_text` между stage и consume → `confirm_required`;
- смена СТАДИИ между stage и consume → `confirm_required` (digest несёт stage);
- архивный тред с pending approval → `{"error": "archived"}` до consume,
  approval не потребляется;
- TTL истёк → `confirm_required`;
- HTTP-клик путь не регрессирует (user_initiated=True без ApprovalService);
- замороженные: тесты ConfirmFlow и стадийного гейта UI-4 не меняются.

**DoD С3:** «LLM не может самостоятельно подтвердить запуск» — критерий Ф0
закрыт тестом self-approval; появился одноразовый `approval_id` в журнале запуска.

---

# С4 — Текстовый канал: request-time cost cap + детерминированный fallback (Ф0.4 + Ф0.9, CR-7)

**Мотив.** `text_loop` работает на голом `AnthropicLLMClient` (`app.py:716-727`)
мимо `CostCap`; `complete()` делает `resp.raise_for_status()` (`llm_client.py:87`);
`ingest_user_turn` перевыбрасывает (`loop.py:210-215`); роут оборачивает в
`try/finally` без `except` (`webrtc_server.py:585-589`) → 500 без ответа.
На голосе есть `ALL_TIERS_FAILED_PHRASE` (`cascade/strategy.py:31`) +
`render_state_template`; на тексте — ничего.

### Task 4.1: `GuardedLLMClient` (в `synapse/dispatcher/llm_client.py`)

```python
class CostCapBlocked(RuntimeError): ...
class ProviderUnavailable(RuntimeError): ...

class GuardedLLMClient:
    """Обёртка текстового канала: request-time блокировка cost cap ДО сетевого
    вызова (Ф0.4) + нормализация провайдер-сбоев в один тип для fallback-а (CR-7).
    Считает КАЖДЫЙ complete — tool-пассы и компакт-вызовы тоже платные."""

    def __init__(self, inner: AnthropicLLMClient, cost_cap: CostCap, clock: Clock): ...

    async def complete(self, messages, tools):
        now = self._clock.now()
        self._cost_cap.maybe_reset(now)
        if self._cost_cap.tripped:            # request-time: запрос НЕ уходит в paid tier
            raise CostCapBlocked()
        try:
            out = await self._inner.complete(messages, tools)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            raise ProviderUnavailable(str(exc)) from exc
        self._cost_cap.record_paid_attempt(now)
        return out
```

`build_host` (`app.py:718-719`): `AnthropicLLMClient(...)` оборачивается в
`GuardedLLMClient(..., cost_cap, clock)` — тот же `cost_cap`-синглтон, что у
голосового каскада: **один дневной лимит на оба канала**, а не два раздельных.
Примечание: `cost_cap` сегодня строится ниже text_loop (`app.py:735`) — сборку
переупорядочить (cap не зависит ни от чего из середины `build_host`).

`_maybe_compact` уже глотает любой сбой компакта (`loop.py:315-316`) —
блокированный cap просто скипает компакт, ход идёт дальше. Это правильная
деградация, отдельного кода не нужно.

### Task 4.2: детерминированный fallback в роуте (CR-7)

Ловим в роуте (`webrtc_server.py:585-596`), НЕ внутри loop — loop переиспользуется
консолью, у которой своя обработка:

```python
try:
    record, reply = await host.text_loop.ingest_user_turn(text, thread_id=thread_id)
    degraded = False
except CostCapBlocked:
    reply = ("Дневной лимит платных запросов исчерпан. "
             + host.store.render_state_template(now, cfg.stale_after_s, cfg.unreachable_after_s))
    host.journal.alert(AlertKind.COST_CAP, {"channel": "http"})
    degraded = True
except ProviderUnavailable:
    reply = ("Связь с мозгом потеряна. "
             + host.store.render_state_template(now, cfg.stale_after_s, cfg.unreachable_after_s))
    host.journal.alert(AlertKind.ALL_TIERS_FAILED, {"channel": "http", "reason": "provider"})
    degraded = True
finally:
    async with host.turn_lock:
        host.current_http_thread["id"] = None
...
return JSONResponse({"reply": reply, "degraded": degraded})
```

Статус — 200 с `degraded: true`, не 500: клиент показывает реплику как обычный
ответ. Обе feed-записи (user + assistant-fallback) пишутся как на happy-path —
лента остаётся правдой того, что пользователь видел. `render_state_template`
(`state.py:369`) — уже существующий детерминированный путь без LLM, включая
awaiting-приоритет; фраза честная при любом состоянии задачи.

**Тесты:**
- httpx-транспорт, отдающий 500 → роут возвращает 200 + `degraded` + фраза
  начинается «Связь с мозгом потеряна»; feed несёт обе записи;
- tripped cap → сетевой вызов НЕ делается (mock-transport assert: 0 запросов),
  фраза про лимит; alert COST_CAP в журнале;
- happy-path инкрементит `cost_cap.count` (по разу на каждый complete, включая tool-пасс);
- голосовой каскад не тронут (его тесты заморожены).

**DoD С4:** падение/таймаут провайдера и исчерпанный лимит дают детерминированную
реплику, не 500; ни один текстовый запрос не уходит в paid tier после лимита.

---

# С5 — Настоящая authn для /api/* (Ф0.5)

**Мотив.** `_csrf_ok` (`webrtc_server.py:456-465`) сравнивает `Origin`/`Referer`
с `Host` — оба контролирует вызывающий; это не идентичность. Read-роуты, включая
обход файловой системы `GET /api/browse` (`webrtc_server.py:485-490`), не требуют
даже этого. До настоящей идентичности control plane открыт любому, кто дотянулся
до порта.

### Task 5.1: bearer-токен

- `SynapseConfig.api_token: str | None` ← `SYNAPSE_API_TOKEN` (`.env`).
  **Fail-closed:** токен не задан → сервер не стартует (RuntimeError при
  `build_web_app`), кроме явного `SYNAPSE_API_TOKEN=insecure-dev` для локальной
  разработки. Иначе «иллюзия защиты» возвращается через забытый env.
- Один хелпер, constant-time:

```python
def _authed(request: Request) -> bool:
    supplied = request.headers.get("authorization", "")
    return hmac.compare_digest(supplied, f"Bearer {cfg.api_token}")
```

- Применить ко **всем** `/api/*` (read и write: browse, projects, threads, feed,
  message, gate, offer, active-thread) и служебным read-роутам с данными:
  `/client/kora-log`, `/client/kora-status`, `/client/session-alive`.
  Статика `/client/*` (html/js/css/иконки) остаётся открытой — bootstrap PWA.
- 401 + `journal.alert(AlertKind.AUTH_FAILURE, …)` (kind уже существует,
  `journal.py:34`) на отказ.
- `_csrf_ok` остаётся на мутирующих роутах (belt+suspenders: JSON content-type
  отсекает HTML-формы).

### Task 5.2: клиент

PWA (`app.js`): fetch-обёртка добавляет `Authorization` из `localStorage`;
на 401 — один prompt «токен доступа», сохранение, retry. WebRTC-offer идёт через
те же роуты — тот же заголовок. Токен по tailnet-HTTPS, в URL не попадает.

**Развилка для Теро:** localStorage+header (рекомендация: просто, без новой CSRF-
поверхности) vs HttpOnly-cookie (удобнее, но cookie шлётся автоматически —
CSRF-поверхность растёт, `_csrf_ok` становится несущим). Спека исходит из первого.

**Тесты:**
- без заголовка: `GET /api/browse` → 401, `POST …/message` → 401;
- с токеном → 200; сравнение через `hmac.compare_digest` (лексический якорь);
- `/client/` (статика) открыт без токена;
- AUTH_FAILURE-alert пишется.

**Live-DoD (Теро, staging 7861):** телефон с токеном — голос и чат работают;
запрос без токена с ноутбука — 401.

**DoD С5:** «control API требует настоящую идентичность» — ни один data-роут не
отвечает без токена; `Origin`/`Host` больше нигде не единственная проверка.

---

# С6 — Кора-hardening: env-allowlist, journal_dir, deadline (Ф0.6-0.8, CR-2, CR-3)

Три независимые правки в `synapse/bridge/kora.py`, один слайс — один файл, один ран.

### Task 6.1: env-allowlist SDK-subprocess (Ф0.6)

**Мотив.** `run()` делает `load_dotenv()` (`app.py:997`) → ВСЕ ключи `.env`
(FISH, DEEPGRAM, OPENROUTER, TG…) живут в `os.environ` хоста; SDK-subprocess
наследует его целиком; Bash Коры открыт → `env` печатает всё одной командой.

```python
# kora.py, module-level:
_KORA_ENV_ALLOWLIST = ("HOME", "PATH", "SHELL", "TMPDIR", "LANG", "LC_ALL",
                       "TERM", "USER", "ANTHROPIC_API_KEY")

# KoraRunner:
def _worker_env(self) -> dict[str, str]:
    return {k: os.environ[k] for k in _KORA_ENV_ALLOWLIST if k in os.environ}

# _build_options: ClaudeAgentOptions(..., env=self._worker_env())
```

⚠️ **Верификационный чекпойнт план-фазы:** поле `env` у `ClaudeAgentOptions`
нашей версии `claude-agent-sdk` — проверить сигнатуру до кода. Нет поля →
запасной путь: `cli_path` указывает на обёртку-скрипт с `exec env -i …`.
`ANTHROPIC_API_KEY` остаётся — им ходит сам SDK CLI; это ключ, который Кора и
так представляет, не чужой секрет.

**Тест:** fake `client_factory` захватывает opts → `set(opts.env) ⊆ allowlist`;
`FISH_AUDIO_API_KEY`/`DEEPGRAM_API_KEY`/`TG_BOT_TOKEN` отсутствуют.

### Task 6.2: `journal_dir` и репозиторий Синапса в deny-списке гейта (Ф0.7, CR-2)

**Мотив.** Gate v3 «писать везде, кроме секретов» не закрывает `journal_dir`
(state.json, ленты тредов, журнал) и репозиторий Синапса: один `Write` — и
исполнитель выставляет себе `task_completed`, подтирает `gate_deny`-записи или
подкидывает реплики в ленту, из которой регидрируется контекст диспетчера.

```python
# KoraRunner:
def _protected_roots(self) -> tuple[Path, ...]:
    roots = [Path(self._cfg.journal_dir).resolve()]      # journal_dir относительный —
    repo = Path(__file__).resolve().parents[2]           # резолв от cwd процесса, один раз
    if self._current_root().resolve() != repo:           # развилка: см. ниже
        roots.append(repo)
    return tuple(roots)

# _gate_decision, мутирующие ветки (внутри И вне workspace), ПОСЛЕ секрет-чека:
if tool_name in _MUTATING_FILE_TOOLS and any(
        resolved.is_relative_to(r) for r in self._protected_roots()):
    return False, "protected_path", "protected_path"
```

- Новая категория `protected_path` — в контракт категорий докстринга
  `_gate_decision` (`kora.py:637-651`).
- Bash-скан: `str(Path(cfg.journal_dir).resolve())` добавляется в проверку
  команды (абсолютный путь — низкий false-positive; generic-слово «journals» НЕ
  добавляем). Bash-обход через relative-cd остаётся принятым residual-ом, как и
  весь Bash-скан («аудит, не граница»).
- **Решение (зафиксировано, Теро может отменить):** deny репозитория Синапса
  снимается, когда `project_root` рана == сам репозиторий — явный биндинг проекта
  «работать над Синапсом» = осознанный выбор пользователя (иначе Кора никогда не
  сможет кодить Синапс). `journal_dir` закрыт **безусловно** — писать в свой
  аудит легитимного повода нет. Самоэскалация через правку гейта при
  project_root==repo — принятый residual до изоляции Фазы 2.

**Тесты:** `Write journals/state.json` → deny/protected_path (и при
project_root==repo тоже); `Write <repo>/synapse/bridge/kora.py` при чужом
project_root → deny; при project_root==repo → allow; Bash `echo x > <abs journal_dir>/state.json`
→ deny; чтение journal_dir — allowed (deny только на мутации).

### Task 6.3: дедлайн не тикает на парковке AskUserQuestion (Ф0.8, CR-3)

**Мотив.** `asyncio.wait_for(self._stream(…), kora_deadline_s)` (`kora.py:499`)
оборачивает и `await fut` внутри `_handle_question` (`kora.py:829`): поздний
ответ человека убивает ран таймаутом, ответ падает в `no_pending_question`.

Замена `wait_for` на вотчдог, который тратит бюджет только вне
`store.awaiting_answer` (`state.py:188` — флаг уже есть):

```python
_WATCHDOG_TICK_S = 1.0   # период ре-чека парковки; module-level, тесты монкипатчат

async def _run(self, task_id, text, spec=None):
    ...
    stream_task = asyncio.create_task(self._stream(task_id, text))
    try:
        await self._watch_deadline(stream_task, self._cfg.kora_deadline_s)
    except Exception as exc:  # включая TimeoutError вотчдога; CancelledError — мимо
        self._journal.alert(AlertKind.KORA_RUN_FAILED, {"task_id": task_id, "error": repr(exc)})
    finally:
        if not stream_task.done():
            stream_task.cancel()   # supersede/request_cancel: отмена ДОЛЖНА дойти до
                                   # async-with клиента → teardown CLI-subprocess (RISK-B2)
            with contextlib.suppress(BaseException):
                await stream_task  # добрать CancelledError/исключение — не оставлять
                                   # «Task exception was never retrieved» в суите
        ...  # снапшот/terminalize/on_run_finished — без изменений

async def _watch_deadline(self, stream_task, budget_s: float) -> None:
    """CR-3: wall-clock бюджет рана; время парковки на AskUserQuestion его не тратит.

    ⚠️ Бюджет меряется ТОЛЬКО по asyncio.get_running_loop().time() — тем же
    таймером, которым ждёт asyncio.wait. self._clock здесь ЗАПРЕЩЁН: в тестах
    это FakeClock (synapse/clock.py:24), он тикает только ручным advance(), а
    asyncio.wait(timeout=…) его не двигает → remaining не убывал бы никогда и
    вотчдог зависал бы вечным циклом. Замороженный
    test_watchdog_timeout_is_terminalized (tests/test_kora.py:329) зелёный
    именно на реальном времени петли — семантику сохраняем. self._clock.now()
    остаётся только для журнальных меток."""
    loop = asyncio.get_running_loop()
    remaining = budget_s
    while True:
        parked = self._store.awaiting_answer
        started = loop.time()
        done, _ = await asyncio.wait(
            {stream_task},
            timeout=_WATCHDOG_TICK_S if parked else min(_WATCHDOG_TICK_S, remaining),
        )
        if done:
            stream_task.result()   # пробросить исключение стрима в except _run-а
            return
        if not parked:
            remaining -= loop.time() - started
            if remaining <= 0:
                stream_task.cancel()
                raise TimeoutError(f"kora deadline {budget_s}s exhausted (awaiting excluded)")
```

Тонкости, без которых слайс ломает суиту (суита offline — «no real sleeps»,
докстринг FakeClock):

- реальных секундных снов тик НЕ добавляет: `asyncio.wait` просыпается сразу,
  как только stream_task завершился (тик — лишь верхняя граница ре-чека
  парковки), а не-парковочная ветка ограничена `min(_WATCHDOG_TICK_S, remaining)`
  — существующий тест с `deadline_s=0.02` ждёт те же ~0.02с, что и сегодняшний
  `wait_for`;
- точность бюджета ±тик на переходе unparked→parked (уже начатое окно wait
  тарифицируется целиком) — принято, это вотчдог, а не биллинг;
- `_WATCHDOG_TICK_S` — module-level константа, тест парковки при нужде её
  монкипатчит.

Сохранённые инварианты — проговорить в тестах, они несущие:
- `CancelledError` самого `_run` (supersede/`request_cancel`) НЕ ловится `except
  Exception` и добегает до `finally` → `stream_task.cancel()` → async-with
  клиента рвёт CLI-subprocess. Семантика `request_cancel` не меняется.
- `finally` `_run`-а нетронут: анти-зомби terminalize структурный, как был.
- Исключение стрима (ошибка SDK) по-прежнему даёт `KORA_RUN_FAILED` + FAILED.

**Тесты:** (а) fake-клиент паркуется на вопросе дольше `kora_deadline_s` → ран
жив, `provide_answer` → задача завершается COMPLETED; (б) **замороженный**
`test_watchdog_timeout_is_terminalized` (`tests/test_kora.py:329`) остаётся
зелёным БЕЗ правок — семантика реального времени петли сохранена, это и есть
регрессионный якорь замены `wait_for`; (в) `request_cancel` во время парковки
→ subprocess-teardown, слот свободен, terminalize отработал.

**DoD С6:** SDK-subprocess не получает лишних credentials; Кора не может писать
в `journal_dir`; время раздумий человека не убивает ран.

---

## Остаточные риски Фазы 0 (проговорены, не лечатся здесь)

1. **Worker→control plane:** Кора в одном процессе с хостом; сетевого запрета
   нет и в Фазе 0 не обещается (нужна изоляция Фазы 2). Токен С5 Коре в env не
   передаётся (allowlist С6) — но локальный процесс есть локальный процесс.
2. **Bash = полный egress + обход файлового гейта** — принято приказом Теро;
   лексический скан остаётся аудитом, не границей.
3. **CR-8:** cancel/timeout не откатывает записанные файлы — workspace замирает
   в промежуточном состоянии. Git-checkpoint перед run — Фаза 2, п.5.
4. **CR-10:** параллельные tool-call-ы одного `UserMessage` решаются гейтом
   изолированно, per-path блокировки нет — Фаза 2, п.5.
5. **CR-9:** `assistant_text` Коры не проверяется, `/client/kora-log` пишет
   значения tool-инпутов (`kora.py:324-330`, осознанное исключение) — `ToolFact`
   Фазы 1.
6. **Resume отсутствует:** краш mid-run → zombie-реконсил в FAILED
   (`state.py:441-458`) — Фаза 3.
7. **`_last_turn_id` заменён минимально** (С2): полный `OperationContext` на
   границе handlers — Фаза 1, вместе с command handler-ом.
8. **Параллельное перекрытие ходов делит journal record** (B08-бэкстоп,
   замороженный тест `test_hunt0714_a.py:283`; `ingest_user_turn` вне
   `turn_lock` — B-PIPE-5). С2 закрывает только последовательное слияние;
   полная пер-ходовая сериализация/`OperationContext` — Фаза 1.

## Развилки, требующие слова Теро

| # | Вопрос | Рекомендация спеки |
|---|--------|--------------------|
| 1 | Судьба `staging_*.py` (С0) | закоммитить в `tools/staging/` |
| 2 | Deny репо Синапса: безусловный или снимается при `project_root==repo` (С6) | снимается при явном биндинге |
| 3 | Токен: localStorage+header vs cookie (С5) | localStorage+header |
| 4 | `SYNAPSE_API_TOKEN` обязателен (fail-closed старт) (С5) | обязателен, `insecure-dev` для локалки |

## Definition of done Фазы 0 (сшивка с proposal)

Регрессионные тесты доказывают: voice получает state (С1); последовательные
ходы получают свои begin/end-пары и не сливаются — параллельное перекрытие
остаётся принятым B08-residual-ом до Фазы 1 (С2); LLM не может сам подтвердить
запуск (С3); текстовый канал не уходит
в paid tier после лимита и не отвечает 500 на сбой провайдера (С4); control API
требует настоящую идентичность (С5); SDK-subprocess не получает лишних
credentials, Кора не пишет в `journal_dir`, парковка не тратит дедлайн (С6).
Сетевой запрет worker→control plane в этой фазе НЕ обещан — явный residual risk.

## Parking lot

- Голосовой вопрос Коры в контекст диспетчера (`append_to_context=False`,
  `app.py:207`) — Р-8-напряжение, решить отдельно (всплыло в С1).
- Персист pending-approval через рестарт — вместе с audit-хранилищем Фазы 1.
- `task_dictionary` де-факто пустой (`loop.py:80,93,236`) — либо начать наполнять,
  либо выпилить параметр; сейчас только вводит в заблуждение.
- Latency-бюджет голоса (у голоса нет SLA, только `request_timeout_s`) —
  продуктовый бюджет, отдельный разговор.
