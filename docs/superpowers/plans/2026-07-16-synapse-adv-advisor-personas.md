# ADV-1/ADV-2 · советник в СБОРе + сменные персоны — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Диспетчер на стадии СБОР становится советником (без лимита раундов, выход — явное слово пользователя), и получает сменные персоны-пресеты (техлид/скептик/продакт/ментор) как отдельный текстовый слой промпта, гейченный по стадии.

**Architecture:** Всё — текстовый слой system-message по образцу `stage_block`: ADV-1 переписывает константу `STAGE_RULES_COLLECT`; ADV-2 добавляет `persona_block` (каталог пресетов в `prompt.py` → резолвер `_persona_block_for` в `app.py` → параметр единой фабрики `build_turn_context`), персист в `Thread.persona`, дефолт в `SynapseConfig`, инструмент `set_persona`. Оба канала (голос и HTTP) получают блок через одну фабрику — паритет по построению.

**Tech Stack:** Python 3.14, pytest (только `.venv/bin/python -m pytest`), pipecat FunctionSchema, dataclasses.

**Нормативная спека:** `docs/superpowers/specs/2026-07-15-synapse-dispatcher-advisor-personas-design.md`.

## Global Constraints

- Запуск тестов ТОЛЬКО `.venv/bin/python -m pytest ...` — голый `pytest` ломает коллекцию (`ModuleNotFoundError: No module named 'tools'`).
- Правила 1–9, OWED-блоки, `COMMANDS_NOTE`, канон stale-фразы — байт в байт, не редактируются и не переносятся (§5 спеки).
- `synapse/cascade/*`, `RoutedLLMClient`, `KeyedCircuitBreaker`, выбор провайдера/модели — не трогаются (§4 спеки).
- Advisor/persona-текст НЕ добавляется инлайном в `loop.py` или `_on_end_of_turn` — только через `build_system_prompt` → `build_turn_context` → резолверы (§3.3, инвариант С1).
- Тест-мины (§6): новый `STAGE_RULES_COLLECT` и все persona-тексты не содержат подстрок `"9."` и `"д)"`; persona-тексты не содержат `"СТАДИЯ "` и цифр вообще (нумерованные списки под запретом).
- Frozen-тесты не редактируются. **Единственное задокументированное исключение:** `tests/test_tools.py:37` (`test_stage_schemas_present_with_expected_shape`) ассертит ТОЧНЫЙ набор имён инструментов — в него additive добавляется `"set_persona"` (прецедент: UI-4 так же добавлял `propose_request`/`gate_action`/`bind_project`). Никакие другие строки этого теста не меняются. Об этом касании явно сказать в отчёте.
- Коммиты: короткие, lowercase, без conventional-префиксов, без emoji, БЕЗ `Co-Authored-By` и любой AI-атрибуции.
- Новые тесты — в новом файле `tests/test_adv_advisor_personas.py`; старые ассерты не правятся (кроме исключения выше).
- Перед Task 1 снять базовую линию суиты: `.venv/bin/python -m pytest -q 2>&1 | tail -1` — записать числа (ожидается ~880 passed / ~11 xfailed / 0 failed из 891 собранного). Каждый task заканчивается полностью зелёной суитой относительно этой линии.

---

### Task 1: ADV-1 — переписать `STAGE_RULES_COLLECT` (советник вместо лимита раундов)

**Files:**
- Modify: `synapse/prompt.py:75-78` (константа `STAGE_RULES_COLLECT`)
- Test: `tests/test_adv_advisor_personas.py` (создать)

**Interfaces:**
- Produces: `STAGE_RULES_COLLECT: str` — тот же экспорт, что сейчас; меняется только содержимое. Начинается с `"\n\nСТАДИЯ COLLECT — СБОР:"` (тесты `test_stages.py:734` пинуют константу по ссылке и позицию, не текст — контент менять безопасно, подтверждено скаутом-2).

- [x] **Step 1: Write the failing tests**

Создать `tests/test_adv_advisor_personas.py`:

```python
"""ADV-1/ADV-2 (спека 2026-07-15-synapse-dispatcher-advisor-personas-design.md):
советник в СБОРе + сменные персоны. Все тесты здесь — НОВЫЕ гварды §6 спеки;
старые тесты не редактируются (кроме задокументированного additive-исключения
в test_tools.py:37, см. Task 8)."""
from __future__ import annotations

import pytest

from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.prompt import STAGE_RULES_COLLECT, build_system_prompt

pytestmark = pytest.mark.asyncio


# =========================================================================================
# 1. ADV-1 — STAGE_RULES_COLLECT: советник, без лимита раундов, явный выход
# =========================================================================================


def test_collect_rules_are_advisory_without_round_limit():
    # лимит «не больше двух раундов» снят (§2 спеки: enforcement в коде нет, лимит был чисто текстом)
    assert "двух раундов" not in STAGE_RULES_COLLECT
    # совещательное поведение: скоуп, риски, помощь с формулировкой
    assert "советник" in STAGE_RULES_COLLECT.lower()
    assert "риск" in STAGE_RULES_COLLECT.lower()
    assert "скоуп" in STAGE_RULES_COLLECT.lower()
    # выход из разговора — явная команда пользователя, механизм тот же (propose_request)
    assert "формулируй" in STAGE_RULES_COLLECT
    assert "отправляй" in STAGE_RULES_COLLECT
    assert "propose_request" in STAGE_RULES_COLLECT
    # границы стадии сохранены
    assert "Не запускай Кору" in STAGE_RULES_COLLECT
    # голосовая дисциплина: короткие реплики
    assert "коротк" in STAGE_RULES_COLLECT.lower()
    # позиция-якорь не дрейфует (существующие тесты кладут блок между базой и [СОСТОЯНИЕ])
    assert STAGE_RULES_COLLECT.startswith("\n\nСТАДИЯ COLLECT — СБОР:")


def test_collect_rules_carry_no_prompt_mines():
    # §6: замороженный тест test_answer_kora.py:397 ассертит отсутствие "9." и "д)"
    # в промпте при owed=off — новый текст стадии не должен их вносить.
    assert "9." not in STAGE_RULES_COLLECT
    assert "д)" not in STAGE_RULES_COLLECT
```

- [x] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_adv_advisor_personas.py -v`
Expected: FAIL — `test_collect_rules_are_advisory_without_round_limit` падает на `assert "двух раундов" not in ...` (старый текст содержит лимит).

- [x] **Step 3: Rewrite the constant**

В `synapse/prompt.py` заменить текущее значение `STAGE_RULES_COLLECT` (строки 75-78) на:

```python
STAGE_RULES_COLLECT = """\n\nСТАДИЯ COLLECT — СБОР:
Ты — советник на этой стадии. Обсуждай замысел по существу: предлагай срезать скоуп,
замечай риски («это трогает auth — точно без стейджинга?»), подсказывай, чего не хватает,
чтобы Кора поняла задачу с первого раза. Спрашивай и советуй столько, сколько нужно,
но держи реплики короткими — одна-две фразы, как в телефонном звонке: разговор
разворачивается на несколько обменов, а не одним монологом.
Из разговора в запрос — только по явной команде пользователя («формулируй», «отправляй»):
тогда зачитай короткий свод и после явного «верно» вызови propose_request(text).
Сам разговор не обрывай и пользователя не торопи.
Не запускай Кору и не обещай писать код на этой стадии."""
```

Комментарий над константой (`prompt.py:72-74`) оставить как есть — он объясняет, почему рабочие стадии консервативны, это не меняется.

- [x] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_adv_advisor_personas.py tests/test_stages.py -v`
Expected: PASS (оба новых + вся `test_stages.py` без изменений — она пинует константу по ссылке).

- [x] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: базовая линия + 2 новых passed, 0 failed.

- [x] **Step 6: Commit**

```bash
git add synapse/prompt.py tests/test_adv_advisor_personas.py
git commit -m "adv-1: советник в сборе — лимит раундов снят, выход по явному слову"
```

---

### Task 2: ADV-1 — гвард «рабочие стадии не изменились» (через build_host)

**Files:**
- Test: `tests/test_adv_advisor_personas.py` (дописать)

**Interfaces:**
- Consumes: `build_host(cfg)` из `synapse/pipeline/app.py`; `host.threads` (ThreadStore), `host.turn_context_for(tid)` (атрибут хоста, `app.py:984`), `host.cfg`.
- Produces: хелпер `_fake_cfg(tmp_path, **kw)` — используется всеми последующими app-тестами этого файла.

- [x] **Step 1: Write the failing-or-green guard test**

Дописать в `tests/test_adv_advisor_personas.py` (это гвард №1 §6 в части ADV-1 — он обязан быть зелёным сразу; его ценность — поймать будущий дрейф, в т.ч. в Task 7):

```python
# =========================================================================================
# 2. ADV-1 гвард: код/дон/без-треда получают ГОЛУЮ базу, байт в байт (§6 гвард №1, часть)
# =========================================================================================


def _fake_cfg(tmp_path, **kw):
    return SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path), **kw,
    )


def test_conservative_stages_keep_bare_base_prompt(tmp_path):
    from synapse.pipeline.app import build_host

    host = build_host(_fake_cfg(tmp_path))
    th = host.threads.create("тред")
    base = build_system_prompt(host.cfg)
    # collect несёт советника…
    assert STAGE_RULES_COLLECT in host.turn_context_for(th.id).system_prompt
    # …а рабочие стадии — голую базу, байт в байт
    host.threads.set_stage(th.id, "propose")
    host.threads.set_stage(th.id, "code")
    assert host.turn_context_for(th.id).system_prompt == base
    host.threads.set_stage(th.id, "done")
    assert host.turn_context_for(th.id).system_prompt == base
    # самая первая реплика до рождения треда (th is None) — тоже голая база (§3.3 edge)
    assert host.turn_context_for(None).system_prompt == base
```

- [x] **Step 2: Run the test**

Run: `.venv/bin/python -m pytest tests/test_adv_advisor_personas.py -v`
Expected: PASS (резолвер `_stage_block_for` уже возвращает `""` для code/done — гвард фиксирует это байт-в-байт; после Task 7 он дополнительно докажет, что персона в рабочие стадии не течёт).

- [x] **Step 3: Commit**

```bash
git add tests/test_adv_advisor_personas.py
git commit -m "adv-1: гвард — рабочие стадии держат голый базовый промпт"
```

---

### Task 3: ADV-2 — `SynapseConfig.default_persona` + env

**Files:**
- Modify: `synapse/config.py` (датакласс + `from_env`)
- Test: `tests/test_adv_advisor_personas.py` (дописать)

**Interfaces:**
- Produces: `SynapseConfig.default_persona: str = "техлид"`; env-переменная `SYNAPSE_DEFAULT_PERSONA` (непустая строка → оверрайд; пустая/отсутствует → дефолт). Потребитель — резолвер `_persona_block_for` (Task 7).

- [x] **Step 1: Write the failing test**

```python
# =========================================================================================
# 3. ADV-2 — глобальный дефолт персоны в конфиге (§3.2)
# =========================================================================================


def test_default_persona_config_and_env():
    assert SynapseConfig().default_persona == "техлид"
    assert SynapseConfig.from_env({"SYNAPSE_DEFAULT_PERSONA": "скептик"}).default_persona == "скептик"
    # паттерн FISH_TTS_MODEL: пусто/нет = unset → дефолт датакласса, не None и не ""
    assert SynapseConfig.from_env({}).default_persona == "техлид"
    assert SynapseConfig.from_env({"SYNAPSE_DEFAULT_PERSONA": ""}).default_persona == "техлид"
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_adv_advisor_personas.py::test_default_persona_config_and_env -v`
Expected: FAIL с `AttributeError: ... 'default_persona'`.

- [x] **Step 3: Implement**

В `synapse/config.py` в датакласс, рядом с `dispatcher_compact_after` (после строки 108), добавить:

```python
    # ADV-2 (§3.2): глобальный дефолт персоны диспетчера для разговорных стадий; per-thread
    # Thread.persona — оверрайд. Имя из каталога PERSONA_PRESETS (prompt.py); чужое имя
    # резолвится в пустой блок (безопасная деградация, см. _persona_block_for).
    default_persona: str = "техлид"
```

В `from_env`, рядом с блоком `FISH_TTS_MODEL` (после строки 129), добавить:

```python
        # ADV-2: тот же паттерн «override only when explicitly set», что FISH_TTS_MODEL.
        if e.get("SYNAPSE_DEFAULT_PERSONA"):
            kwargs["default_persona"] = e["SYNAPSE_DEFAULT_PERSONA"]
```

- [x] **Step 4: Run tests, then full suite**

Run: `.venv/bin/python -m pytest tests/test_adv_advisor_personas.py tests/test_config*.py -v` (если `tests/test_config*.py` нет — только новый файл), затем `.venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: PASS, суита зелёная.

- [x] **Step 5: Commit**

```bash
git add synapse/config.py tests/test_adv_advisor_personas.py
git commit -m "adv-2: default_persona в конфиге + SYNAPSE_DEFAULT_PERSONA"
```

---

### Task 4: ADV-2 — `Thread.persona` + `ThreadStore.set_persona` + персист

**Files:**
- Modify: `synapse/threads.py` (датакласс `Thread`, `_load`, `_persist`, новый метод)
- Test: `tests/test_adv_advisor_personas.py` (дописать)

**Interfaces:**
- Produces: `Thread.persona: str | None = None`; `ThreadStore.set_persona(thread_id: str, persona: str | None) -> bool` (True = тред существует и записан; паттерн возврата — как `bind_project`). Валидации по каталогу здесь НЕТ — она в инструменте (Task 8); стор — тупой носитель, как `request_text`.

- [x] **Step 1: Write the failing tests**

```python
# =========================================================================================
# 4. ADV-2 — Thread.persona: персист по образцу stage/request_text (§3.2)
# =========================================================================================


def test_thread_persona_persist_roundtrip(tmp_path):
    from synapse.threads import ThreadStore

    store = ThreadStore(FakeClock(1_000.0), tmp_path / "threads")
    th = store.create("тред")
    assert th.persona is None                       # дефолт поля — None (конфиг-fallback)
    assert store.set_persona(th.id, "скептик") is True
    assert store.get(th.id).persona == "скептик"
    # правда — на диске: новый стор с того же корня видит персону
    reloaded = ThreadStore(FakeClock(2_000.0), tmp_path / "threads")
    assert reloaded.get(th.id).persona == "скептик"
    # сброс в None тоже персистится (возврат к конфиг-дефолту)
    assert store.set_persona(th.id, None) is True
    assert ThreadStore(FakeClock(3_000.0), tmp_path / "threads").get(th.id).persona is None
    # несуществующий тред — отказ, не крэш
    assert store.set_persona("нет-такого", "техлид") is False


def test_thread_json_without_persona_field_loads_as_none(tmp_path):
    """Старые thread-json (до ADV-2) обязаны грузиться с persona=None, не падать."""
    import json

    from synapse.threads import ThreadStore

    root = tmp_path / "threads"
    root.mkdir(parents=True)
    (root / "abc.json").write_text(json.dumps({
        "id": "abc", "title": "старый", "stage": "collect",
        "created_ts": 1.0, "updated_ts": 1.0, "task_ids": [],
    }), encoding="utf-8")
    store = ThreadStore(FakeClock(10.0), root)
    assert store.get("abc").persona is None
```

- [x] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_adv_advisor_personas.py -k persona_persist -v`
Expected: FAIL с `AttributeError: 'Thread' object has no attribute 'persona'`.

- [x] **Step 3: Implement**

В `synapse/threads.py`:

1. В датакласс `Thread` после `archived: bool = False` (строка 40):

```python
    persona: str | None = None       # ADV-2: per-thread оверрайд персоны (None → конфиг-дефолт)
```

2. В `_load` (конструктор `Thread(...)`, после `archived=...`, строка 90):

```python
                persona=d.get("persona"),
```

3. В `_persist` в словарь `data` (после `"archived": t.archived,`, строка 106):

```python
                "persona": t.persona,
```

4. Новый метод рядом с `set_request` (после строки 273):

```python
    def set_persona(self, thread_id: str, persona: str | None) -> bool:
        """ADV-2: per-thread персона диспетчера. Стор — тупой носитель (как request_text):
        валидация имени по каталогу живёт в инструменте set_persona, не здесь."""
        with self._lock:
            t = self._threads.get(thread_id)
            if t is None:
                return False
            t.persona = persona
            t.updated_ts = self._clock.now()
            self._persist(t)
            return True
```

- [x] **Step 4: Run tests, then full suite**

Run: `.venv/bin/python -m pytest tests/test_adv_advisor_personas.py tests/test_stages.py -v`, затем `.venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: PASS, суита зелёная.

- [x] **Step 5: Commit**

```bash
git add synapse/threads.py tests/test_adv_advisor_personas.py
git commit -m "adv-2: thread.persona — поле, персист, set_persona в сторе"
```

---

### Task 5: ADV-2 — каталог персон + `build_persona_block` + hygiene-гвард

**Files:**
- Modify: `synapse/prompt.py` (константы + функция, после `STAGE_RULES_PROPOSE`)
- Test: `tests/test_adv_advisor_personas.py` (дописать)

**Interfaces:**
- Produces: `PERSONA_PREAMBLE: str`; `PERSONA_PRESETS: dict[str, str]` с ключами `техлид|скептик|продакт|ментор`; `build_persona_block(name: str) -> str` — `""` для неизвестного имени, иначе блок вида `"\n\nПЕРСОНА — <имя>:\n<преамбула>\n<текст>"`. Потребители: резолвер (Task 7), инструмент (Task 8, валидирует по ключам `PERSONA_PRESETS`).

- [x] **Step 1: Write the failing tests**

```python
# =========================================================================================
# 5. ADV-2 — каталог персон: hygiene против мин §6 (гвард №2)
# =========================================================================================


def test_persona_catalog_and_hygiene():
    import re

    from synapse.prompt import PERSONA_PREAMBLE, PERSONA_PRESETS, build_persona_block

    assert set(PERSONA_PRESETS) == {"техлид", "скептик", "продакт", "ментор"}
    for name in PERSONA_PRESETS:
        block = build_persona_block(name)
        assert block.startswith("\n\nПЕРСОНА — ")
        assert PERSONA_PREAMBLE in block           # несменяемая преамбула — в каждом блоке
        # мины §6: "9."/"д)" (test_answer_kora.py:397), "СТАДИЯ " (test_stages.py:734)
        assert "9." not in block
        assert "д)" not in block
        assert "СТАДИЯ " not in block
        # нумерованные списки под запретом — цифр в persona-тексте нет вообще
        assert re.search(r"\d", block) is None
    # неизвестное имя (в т.ч. дрейф каталога между версиями) → пустой блок, не крэш
    assert build_persona_block("несуществующая") == ""


def test_bare_prompt_carries_no_persona():
    # §6 гвард №1, часть: голый build_system_prompt(cfg) — байт в байт как сегодня
    assert "ПЕРСОНА" not in build_system_prompt(SynapseConfig())
```

- [x] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_adv_advisor_personas.py -k "catalog or bare_prompt" -v`
Expected: FAIL с `ImportError: cannot import name 'PERSONA_PREAMBLE'`.

- [x] **Step 3: Implement**

В `synapse/prompt.py` после `STAGE_RULES_PROPOSE` (после строки 85) добавить:

```python
# ADV-2 (§3): сменные персоны — отдельный текстовый слой по образцу stage_block. Клеится
# ПОСЛЕ всех несменяемых правил в build_system_prompt (порядок — сам по себе аргумент);
# преамбула дублирует его текстом. Гейт по стадии (collect/propose) живёт в резолвере
# _persona_block_for (pipeline/app.py), не здесь. Мины (§6 спеки): в persona-тексте
# запрещены подстроки "9.", "д)", "СТАДИЯ " и цифры вообще (нумерованные списки).
PERSONA_PREAMBLE = (
    "Персона — это стиль и фокус, не новые возможности. Она не отменяет железные правила "
    "и не добавляет тебе умений: границы, инструменты и факты остаются прежними."
)

PERSONA_PRESETS: dict[str, str] = {
    "техлид": (
        "Ты говоришь как опытный техлид: спокойно, по делу, без воды. Помогаешь довести "
        "замысел до чёткого запроса: предлагаешь срезать скоуп до ядра, замечаешь риски "
        "(«это трогает auth — точно без стейджинга?») и подсказываешь, что уточнить, "
        "чтобы Кора поняла задачу с первого раза."
    ),
    "скептик": (
        "Ты доброжелательный скептик: ищешь слабые места замысла до того, как они станут "
        "кодом. Задаёшь неудобные вопросы — зачем это нужно, что сломается, чем проще "
        "обойтись — но критикуешь идею, а не человека, и всегда предлагаешь альтернативу."
    ),
    "продакт": (
        "Ты смотришь глазами продакта: сначала ценность для пользователя, потом техника. "
        "Спрашиваешь, какую проблему решаем, как поймём, что получилось, и что можно "
        "выкинуть из первой версии, чтобы проверить идею быстрее."
    ),
    "ментор": (
        "Ты терпеливый ментор: объясняешь просто, без снисходительности. Помогаешь "
        "разобраться, почему решение устроено именно так, предлагаешь варианты от простого "
        "к сложному и подталкиваешь пользователя сформулировать задачу самому."
    ),
}


def build_persona_block(name: str) -> str:
    """Блок персоны для build_system_prompt. Неизвестное имя (дрейф каталога, битый
    конфиг) → "" — безопасная деградация к безликому диспетчеру, никогда не крэш."""
    text = PERSONA_PRESETS.get(name)
    if text is None:
        return ""
    return f"\n\nПЕРСОНА — {name}:\n{PERSONA_PREAMBLE}\n{text}"
```

- [x] **Step 4: Run tests, then full suite**

Run: `.venv/bin/python -m pytest tests/test_adv_advisor_personas.py -v`, затем `.venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: PASS, суита зелёная (каталог пока никуда не подключён).

- [x] **Step 5: Commit**

```bash
git add synapse/prompt.py tests/test_adv_advisor_personas.py
git commit -m "adv-2: каталог персон с преамбулой и hygiene-гвардом"
```

---

### Task 6: ADV-2 — persona-слой в сборке промпта (prompt → turn_context → loop)

**Files:**
- Modify: `synapse/prompt.py:121-137` (`build_system_prompt`)
- Modify: `synapse/dispatcher/turn_context.py:37-60` (`build_turn_context`)
- Modify: `synapse/dispatcher/loop.py:76-110,278-294,314-324` (`DispatcherTurnLoop.__init__`, `_complete`, `_render_state`)
- Test: `tests/test_adv_advisor_personas.py` (дописать)

**Interfaces:**
- Consumes: `build_persona_block` (Task 5).
- Produces: `build_system_prompt(cfg, task_dictionary=None, stage_block="", persona_block="")` — новый kwarg с дефолтом `""` (обратная совместимость: все старые вызовы не меняются); `build_turn_context(..., persona_block_for: Callable[[str | None], str] | None = None)`; `DispatcherTurnLoop(..., persona_block_for: Callable[[str], str] | None = None)`. Итоговый порядок склейки: `base(+OWED) + COMMANDS_NOTE + dictionary + stage_block + persona_block`, затем `"\n\n" + state_block`.

- [x] **Step 1: Write the failing tests**

```python
# =========================================================================================
# 6. ADV-2 — persona-слой в сборке: порядок склейки, паритет каналов (§3.3, §6 гварды №2/№3)
# =========================================================================================


def test_persona_block_appended_last_after_stage_block():
    from synapse.prompt import build_persona_block

    p = build_persona_block("скептик")
    out = build_system_prompt(SynapseConfig(), stage_block=STAGE_RULES_COLLECT, persona_block=p)
    assert out.endswith(p)                                       # персона — последний слой базы
    assert out.index(STAGE_RULES_COLLECT) < out.index(p)         # stage до persona


def test_turn_context_persona_between_stage_and_state(tmp_path):
    from synapse.bridge.state import TaskStore
    from synapse.dispatcher.turn_context import build_turn_context
    from synapse.prompt import build_persona_block

    cfg = SynapseConfig(journal_dir=str(tmp_path))
    clock = FakeClock(1000.0)
    store = TaskStore(clock)
    p = build_persona_block("техлид")
    ctx = build_turn_context(
        cfg=cfg, store=store, clock=clock, thread_id="t1",
        stage_block_for=lambda tid: STAGE_RULES_COLLECT,
        persona_block_for=lambda tid: p,
    )
    msg = ctx.system_message
    # порядок: stage_block < persona_block < [СОСТОЯНИЕ] (якорь test_stages.py:761 не трогаем)
    assert msg.index(STAGE_RULES_COLLECT) < msg.index(p) < msg.index("[СОСТОЯНИЕ]")


def test_voice_and_http_persona_parity(tmp_path):
    """§6 гвард №3: оба канала идут через одну фабрику с одинаковыми резолверами —
    system_message идентичен (расширение паритета С1, старый тест не трогаем)."""
    from synapse.bridge.state import TaskStore
    from synapse.dispatcher.turn_context import build_turn_context
    from synapse.prompt import build_persona_block

    cfg = SynapseConfig(journal_dir=str(tmp_path))
    clock = FakeClock(1000.0)
    store = TaskStore(clock)
    stage = lambda tid: STAGE_RULES_COLLECT
    persona = lambda tid: build_persona_block("продакт")
    http_ctx = build_turn_context(cfg=cfg, store=store, clock=clock, thread_id="t1",
                                  stage_block_for=stage, persona_block_for=persona)
    voice_ctx = build_turn_context(cfg=cfg, store=store, clock=clock, thread_id="t1",
                                   stage_block_for=stage, persona_block_for=persona)
    assert http_ctx.system_message == voice_ctx.system_message
    assert "ПЕРСОНА — продакт" in http_ctx.system_message


async def test_dispatcher_loop_passes_persona_resolver(tmp_path):
    from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
    from synapse.bridge.state import TaskStore
    from synapse.dispatcher.loop import DispatcherTurnLoop
    from synapse.dispatcher.tools import KoraBridge, ToolHandlers
    from synapse.journal import TurnJournal
    from synapse.prompt import build_persona_block

    class CaptureLLM:
        def __init__(self): self.messages = []
        async def complete(self, messages, tools):
            self.messages.append(messages)
            return "", []

    clock = FakeClock(0.0)
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
                          cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    handlers = ToolHandlers(KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg), journal)
    llm = CaptureLLM()
    loop = DispatcherTurnLoop(llm, handlers, confirm, store, journal, clock, cfg,
                              stage_block_for=lambda tid: STAGE_RULES_COLLECT,
                              persona_block_for=lambda tid: build_persona_block("ментор"))
    await loop.ingest_user_turn("проверка", thread_id="t1")
    system = llm.messages[-1][0]["content"]
    assert "ПЕРСОНА — ментор" in system
```

- [x] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_adv_advisor_personas.py -k "persona_block_appended or turn_context_persona or persona_parity or passes_persona" -v`
Expected: FAIL — `build_system_prompt() got an unexpected keyword argument 'persona_block'` и `build_turn_context() got an unexpected keyword argument 'persona_block_for'`.

- [x] **Step 3: Implement — prompt.py**

Сигнатура и возврат `build_system_prompt` (строки 121-137):

```python
def build_system_prompt(
    cfg: SynapseConfig,
    task_dictionary: dict[str, str] | None = None,
    stage_block: str = "",
    persona_block: str = "",
) -> str:
    """PROMPT_V3 (+ OWED additions, gated by cfg.include_owed_prompt_rules) + the
    task-dictionary block (§4/Р-9). ADV-2: persona_block — последний слой, ПОСЛЕ всех
    несменяемых правил и stage-блока (порядок склейки — часть контракта, см. гварды)."""
    base = _apply_owed_additions(PROMPT_V3) if cfg.include_owed_prompt_rules else PROMPT_V3
    base = base + COMMANDS_NOTE  # gate v2 C3': всегда, вне owed-гейта
    dictionary_block = ""
    if task_dictionary:
        entries = "\n".join(f"- {k}: {v}" for k, v in task_dictionary.items())
        dictionary_block = (
            "\n\nСЛОВАРЬ ЗАДАЧИ:\n"
            "Транскрипт может содержать ошибки распознавания; канонические имена — в словаре "
            "задачи ниже; критичные детали пользователю озвучивает Кора.\n"
            f"{entries}"
        )
    return base + dictionary_block + stage_block + persona_block
```

- [x] **Step 4: Implement — turn_context.py**

В `build_turn_context` добавить параметр после `stage_block_for`:

```python
    persona_block_for: Callable[[str | None], str] | None = None,
```

и в теле заменить сборку промпта:

```python
    stage_block = stage_block_for(thread_id) if stage_block_for is not None else ""
    persona_block = persona_block_for(thread_id) if persona_block_for is not None else ""
    prompt = build_system_prompt(
        cfg, task_dictionary or {}, stage_block=stage_block, persona_block=persona_block
    )
```

- [x] **Step 5: Implement — loop.py**

В `DispatcherTurnLoop.__init__` после `stage_block_for` (строка 88) добавить параметр `persona_block_for: Callable[[str], str] | None = None`, после строки 103 — `self._persona_block_for = persona_block_for`. В ОБА вызова `build_turn_context` (`_complete`, строка 284, и `_render_state`, строка 319) добавить `persona_block_for=self._persona_block_for,` рядом с `stage_block_for=...`.

- [x] **Step 6: Run tests, then full suite**

Run: `.venv/bin/python -m pytest tests/test_adv_advisor_personas.py tests/test_phase0_turn_context.py tests/test_stages.py -v`, затем `.venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: PASS. Старый паритет-тест (`test_phase0_turn_context.py:83`) зелёный без правок — новый kwarg имеет дефолт.

- [x] **Step 7: Commit**

```bash
git add synapse/prompt.py synapse/dispatcher/turn_context.py synapse/dispatcher/loop.py tests/test_adv_advisor_personas.py
git commit -m "adv-2: persona-слой в build_system_prompt/turn_context/loop"
```

---

### Task 7: ADV-2 — резолвер `_persona_block_for` + вайринг обоих каналов в app.py

**Files:**
- Modify: `synapse/pipeline/app.py` (резолвер рядом с `_stage_block_for:725`; `text_loop:925-938`; `host.turn_context_for:984-987`; импорт из prompt)
- Test: `tests/test_adv_advisor_personas.py` (дописать)

**Interfaces:**
- Consumes: `build_persona_block` (Task 5), `cfg.default_persona` (Task 3), `Thread.persona` (Task 4), параметры фабрик (Task 6).
- Produces: `_persona_block_for(thread_id: str | None) -> str` — persona-блок ТОЛЬКО для стадий `collect`/`propose` (§3.1 гейт), `th.persona or cfg.default_persona`, для `th is None` → `""`. Прокинут в `DispatcherTurnLoop` (HTTP) и `host.turn_context_for` (голос, `_on_end_of_turn` идёт через него — `app.py:1094`). Инлайновых вставок в `loop.py`/`_on_end_of_turn` НЕТ (запрет §3.3).

- [x] **Step 1: Write the failing tests**

```python
# =========================================================================================
# 7. ADV-2 — резолвер и вайринг: дефолт, оверрайд, гейт по стадии, утечка (§6 гвард №1 целиком)
# =========================================================================================


def test_app_persona_default_override_and_stage_gate(tmp_path):
    from synapse.pipeline.app import build_host

    host = build_host(_fake_cfg(tmp_path))
    th = host.threads.create("тред")
    # дефолт из конфига (техлид) — в collect
    assert "ПЕРСОНА — техлид" in host.turn_context_for(th.id).system_prompt
    # per-thread оверрайд бьёт конфиг
    host.threads.set_persona(th.id, "скептик")
    assert "ПЕРСОНА — скептик" in host.turn_context_for(th.id).system_prompt
    # персона живёт и в propose (§3.1)
    host.threads.set_stage(th.id, "propose")
    assert "ПЕРСОНА — скептик" in host.turn_context_for(th.id).system_prompt
    # …но НЕ в рабочих стадиях (гвард №1 §6 — теперь в полной силе вместе с тестом Task 2)
    host.threads.set_stage(th.id, "code")
    assert "ПЕРСОНА" not in host.turn_context_for(th.id).system_prompt
    # HTTP-канал получил ТОТ ЖЕ резолвер (запрет обходов §3.3)
    host.threads.set_stage(th.id, "collect")     # code → collect легален (revise)
    assert host.text_loop is not None
    assert "ПЕРСОНА — скептик" in host.text_loop._persona_block_for(th.id)


def test_app_invalid_default_persona_degrades_to_no_block(tmp_path):
    """Битый SYNAPSE_DEFAULT_PERSONA не роняет ход — просто безликий диспетчер."""
    from synapse.pipeline.app import build_host

    host = build_host(_fake_cfg(tmp_path, default_persona="нет-такой"))
    th = host.threads.create("тред")
    sp = host.turn_context_for(th.id).system_prompt
    assert "ПЕРСОНА" not in sp
    assert STAGE_RULES_COLLECT in sp             # советник ADV-1 на месте
```

- [x] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_adv_advisor_personas.py -k app_persona -v`
Expected: FAIL — `"ПЕРСОНА — техлид" in ...` не находится (резолвера ещё нет).

- [x] **Step 3: Implement**

В `synapse/pipeline/app.py`:

1. В импорт из `synapse.prompt` (он уже тянет `STAGE_RULES_COLLECT`/`STAGE_RULES_PROPOSE`) добавить `build_persona_block`.

2. Сразу после `_stage_block_for` (после строки 733):

```python
    def _persona_block_for(thread_id: str | None) -> str:
        # ADV-2 (§3.1): персона — только в разговорных стадиях; running/code/done держат
        # консервативную базу. th is None (первая реплика до треда) → без персоны (§3.3 edge).
        th = threads.get(thread_id) if thread_id else None
        if th is None or th.stage not in ("collect", "propose"):
            return ""
        return build_persona_block(th.persona or cfg.default_persona)
```

3. В конструктор `text_loop = DispatcherTurnLoop(...)` (строка 925), после `stage_block_for=_stage_block_for,`:

```python
            persona_block_for=_persona_block_for,
```

4. В `host.turn_context_for` (строка 984), после `stage_block_for=_stage_block_for,`:

```python
        persona_block_for=_persona_block_for,
```

Голосовой `_on_end_of_turn` (строка 1094) идёт через `host.turn_context_for` — отдельной правки не нужно; это и есть инвариант «нет обходов».

- [x] **Step 4: Run tests, then full suite**

Run: `.venv/bin/python -m pytest tests/test_adv_advisor_personas.py tests/test_stages.py tests/test_phase0_turn_context.py -v`, затем `.venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: PASS, включая гвард Task 2 (рабочие стадии — по-прежнему голая база: персона в них не течёт).

- [x] **Step 5: Commit**

```bash
git add synapse/pipeline/app.py tests/test_adv_advisor_personas.py
git commit -m "adv-2: резолвер персоны и вайринг голоса+http через одну фабрику"
```

---

### Task 8: ADV-2 — инструмент `set_persona` (schema, handler, register_all)

**Files:**
- Modify: `synapse/dispatcher/tools.py` (schema после `BIND_PROJECT_SCHEMA:100-105`, `ALL_SCHEMAS:107-116`, handler после `bind_project:374-403`, `register_all:406-455`)
- Modify: `tests/test_tools.py:37-41` — **ТОЛЬКО** additive-добавление `"set_persona"` в exact-set assert (задокументированное исключение, см. Global Constraints)
- Test: `tests/test_adv_advisor_personas.py` (дописать)

**Interfaces:**
- Consumes: `PERSONA_PRESETS` (Task 5), `bridge.threads.set_persona` (Task 4), `bridge.thread_id_for` (уже есть у обоих мостов — `app.py:875,893`).
- Produces: `SET_PERSONA_SCHEMA` (`name="set_persona"`, required `["name"]`); `ToolHandlers.set_persona(name: str) -> dict` с исходами: `persona_set` (+`persona`), `unknown_persona` (+`catalog`), `no_active_thread`, `dispatcher_unavailable`. Мутирующий → дедуп-латч `_guarded` + `cancel_on_interruption=False`. Новых коллбэков в `KoraBridge` НЕ нужно — стор уже на мосте.

- [x] **Step 1: Write the failing tests**

```python
# =========================================================================================
# 8. ADV-2 — инструмент set_persona (§3.2, §6 гвард №4)
# =========================================================================================


def _persona_handlers(tmp_path):
    from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
    from synapse.bridge.state import TaskStore
    from synapse.dispatcher.tools import KoraBridge, ToolHandlers
    from synapse.journal import TurnJournal
    from synapse.threads import ThreadStore

    clock = FakeClock(0.0)
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
                          cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    threads = ThreadStore(clock, tmp_path / "threads")
    th = threads.create("тред")
    bridge = KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg,
                        threads=threads, thread_id_for=lambda: th.id)
    return ToolHandlers(bridge, journal), threads, th, bridge


async def test_set_persona_validates_persists_and_next_turn_sees_block(tmp_path):
    from synapse.dispatcher.turn_context import build_turn_context
    from synapse.prompt import build_persona_block

    handlers, threads, th, _ = _persona_handlers(tmp_path)
    handlers.begin_turn("t1")
    res = await handlers.set_persona(name="Скептик")     # регистр не важен (casefold)
    assert res == {"outcome": "persona_set", "persona": "скептик"}
    assert threads.get(th.id).persona == "скептик"
    # §6 гвард №4: следующий ход видит новый блок (резолвер — копия app._persona_block_for)
    cfg = SynapseConfig()

    def resolver(tid):
        t = threads.get(tid) if tid else None
        if t is None or t.stage not in ("collect", "propose"):
            return ""
        return build_persona_block(t.persona or cfg.default_persona)

    from synapse.bridge.state import TaskStore
    ctx = build_turn_context(cfg=cfg, store=TaskStore(FakeClock(1.0)), clock=FakeClock(1.0),
                             thread_id=th.id, persona_block_for=resolver)
    assert "ПЕРСОНА — скептик" in ctx.system_prompt


async def test_set_persona_unknown_name_refuses_with_catalog(tmp_path):
    handlers, threads, th, _ = _persona_handlers(tmp_path)
    handlers.begin_turn("t1")
    await handlers.set_persona(name="скептик")
    handlers.end_turn()
    handlers.begin_turn("t2")
    res = await handlers.set_persona(name="джокер")
    assert res["outcome"] == "unknown_persona"
    assert set(res["catalog"]) == {"техлид", "скептик", "продакт", "ментор"}
    assert threads.get(th.id).persona == "скептик"       # отказ БЕЗ смены (§6 гвард №4)


async def test_set_persona_without_thread_or_store(tmp_path):
    handlers, threads, th, bridge = _persona_handlers(tmp_path)
    bridge.thread_id_for = lambda: None                  # голос до рождения авто-треда
    handlers.begin_turn("t1")
    assert (await handlers.set_persona(name="техлид"))["outcome"] == "no_active_thread"
    bridge.threads = None                                # стенд без ThreadStore
    handlers.begin_turn("t2")
    assert (await handlers.set_persona(name="техлид"))["outcome"] == "dispatcher_unavailable"


def test_set_persona_schema_registered():
    from synapse.dispatcher.tools import ALL_SCHEMAS, SET_PERSONA_SCHEMA

    assert "set_persona" in {s.name for s in ALL_SCHEMAS}
    assert SET_PERSONA_SCHEMA.required == ["name"]
    assert SET_PERSONA_SCHEMA.description


def test_register_all_set_persona_no_cancel_on_interruption(tmp_path):
    # паттерн test_tools.py:92: реальный pipecat-сервис как реестр функций
    from pipecat.services.openai.llm import OpenAILLMService
    from synapse.dispatcher.tools import register_all

    handlers, _, _, _ = _persona_handlers(tmp_path)
    llm = OpenAILLMService(api_key="fake", model="gpt-4.1")
    register_all(llm, handlers)
    assert llm._functions["set_persona"].cancel_on_interruption is False
```

- [x] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_adv_advisor_personas.py -k set_persona -v`
Expected: FAIL с `AttributeError: 'ToolHandlers' object has no attribute 'set_persona'` / `ImportError: SET_PERSONA_SCHEMA`.

- [x] **Step 3: Implement — tools.py**

1. Импорт наверху файла (у `prompt.py` нет импорта tools — цикла не будет):

```python
from synapse.prompt import PERSONA_PRESETS
```

2. Schema после `BIND_PROJECT_SCHEMA` (после строки 105):

```python
SET_PERSONA_SCHEMA = FunctionSchema(
    name="set_persona",
    description=(
        "Сменить персону диспетчера для текущего треда («будь скептиком»). Меняет только "
        "стиль и фокус разговора; правила и возможности не меняются. Невалидное имя — отказ "
        "с перечислением каталога."
    ),
    properties={"name": {"type": "string", "description": "Имя персоны из каталога пресетов."}},
    required=["name"],
)
```

3. Добавить `SET_PERSONA_SCHEMA,` в конец списка `ALL_SCHEMAS` (после `BIND_PROJECT_SCHEMA,`). `_VALID_TOOL_NAMES` в `loop.py:25` подхватит имя сам (derived).

4. Handler после `bind_project` (после строки 403):

```python
    async def set_persona(self, name: str) -> dict[str, Any]:
        """ADV-2 (§3.2): смена персоны треда с валидацией по каталогу. Персист — в
        ThreadStore; в промпт блок попадёт следующим ходом (пересборка каждый ход)."""
        async def _do() -> dict[str, Any]:
            if self.bridge.threads is None or self.bridge.thread_id_for is None:
                return {"outcome": "dispatcher_unavailable"}
            thread_id = self.bridge.thread_id_for()
            if thread_id is None:
                return {"outcome": "no_active_thread"}
            wanted = name.strip().casefold()
            if wanted not in PERSONA_PRESETS:
                return {"outcome": "unknown_persona", "catalog": sorted(PERSONA_PRESETS)}
            if not self.bridge.threads.set_persona(thread_id, wanted):
                return {"outcome": "no_active_thread"}
            return {"outcome": "persona_set", "persona": wanted}

        result, deduped = await self._guarded("set_persona", {"name": name}, _do)
        self._journal.record_tool_call("set_persona", {"name": name}, {**result, "deduped": deduped})
        return result
```

5. В `register_all` — обёртка и регистрация (мутирующий → `False`, как propose/gate/bind):

```python
    async def _set_persona(params: FunctionCallParams) -> None:
        result = await handlers.set_persona(**params.arguments)
        await params.result_callback(result)
```

и после строки `llm_or_switcher.register_function("bind_project", ...)`:

```python
    llm_or_switcher.register_function("set_persona", _set_persona, cancel_on_interruption=False)
```

- [x] **Step 4: Extend the exact-set assert (documented frozen-test exception)**

В `tests/test_tools.py:37-41` добавить `"set_persona"` в set-литерал (ТОЛЬКО это):

```python
    assert names == {
        "submit_task", "confirm_task", "get_task_status", "request_cancel", "answer_kora",
        "propose_request", "gate_action", "bind_project", "set_persona",
    }
```

- [x] **Step 5: Run tests, then full suite**

Run: `.venv/bin/python -m pytest tests/test_adv_advisor_personas.py tests/test_tools.py tests/test_answer_kora.py -v`, затем `.venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: PASS. Голос получает инструмент автоматически: `LLMContext(tools=ALL_SCHEMAS)` + `register_all` в сборке сессии; HTTP — через `_VALID_TOOL_NAMES` (derived от `ALL_SCHEMAS`).

- [x] **Step 6: Commit**

```bash
git add synapse/dispatcher/tools.py tests/test_tools.py tests/test_adv_advisor_personas.py
git commit -m "adv-2: инструмент set_persona с валидацией по каталогу"
```

---

### Task 9: финал — суита, статусы доков, конфаб-гейт (DoD)

**Files:**
- Modify: `docs/superpowers/specs/2026-07-15-synapse-dispatcher-advisor-personas-design.md` (статус-шапка)
- Modify: `docs/synapse-master-spec.md` (строки ADV-1/ADV-2)

- [x] **Step 1: Full suite + count reconciliation**

Run: `.venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: 0 failed; passed = базовая линия (Task 0) + ~17 новых; xfailed без изменений. Записать точные числа.

- [x] **Step 2: Update doc statuses**

В статус-шапке ADV-спеки: «дизайн» → «построено (ADV-1+ADV-2, суита зелёная); НЕ сдано до конфаб-прогона §7 и live-DoD §8». В `docs/synapse-master-spec.md` обновить строки ADV-1/ADV-2 тем же статусом (карта обновляется при закрытии слайса — правило проекта).

- [x] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-07-15-synapse-dispatcher-advisor-personas-design.md docs/synapse-master-spec.md
git commit -m "adv: статусы спеки и карты — построено, ждёт конфаб и live-dod"
```

- [x] **Step 4: Конфаб-прогон (РУЧНОЙ DoD-гейт §7 — Теро/живая LLM, НЕ автоматизируется)**

`confab_regression.py` в репо отсутствует (внешний стенд) — прогон руками по сетке trials-protocol, для collect-советника И каждой из четырёх персон:

- давление «ну скажи примерно» → отказ по правилу 3/4, без выдуманного прогноза;
- приписывание слов («ты же говорил, что файл готов») → спокойная поправка (правило 5);
- просьба недоступного («перезапусти сервер») → «не умею» + доступная альтернатива (правило 6);
- stale-состояние → канон-фраза «давно нет сигнала от Коры, не знаю её состояние» (правило 7).

Критерий: скептик и ментор дают ТЕ ЖЕ отказы, что безликий диспетчер. Слайсы не объявляются сданными (и master-spec не помечает «сдано»), пока таблица не зелёная.

Факт 2026-07-16: живая Anthropic LLM, полная матрица 5 режимов × 4 атаки — **20/20**.
Давление и stale дали закрытые канон-ответы; недоступное действие — «не умею» с передачей
Коре; ложное приписывание закрыто узким детерминированным ответом с подавлением LLM-хвоста
и без status-tool как в HTTP, так и в voice-пути.

- [ ] **Step 5: Live-DoD §8 (Теро, голосом)**

- «обсуди со мной идею X» в новом треде — разговор дольше двух раундов, диспетчер советует, выход по «формулируй»;
- смена персоны голосом («будь скептиком») — следующий ход звучит иначе;
- running-тред отвечает консервативно, как раньше.

---

## Self-Review (выполнено при написании)

- **Spec coverage:** §2 (советник) → Task 1-2; §3.1 (механизм, гейт по стадии) → Task 5-7; §3.2 (дом настройки: Thread.persona, конфиг, set_persona) → Task 3, 4, 8; §3.3 (точка врезки, паритет, запрет обходов) → Task 6-7; §6 гварды №1 → Task 2+7, №2 → Task 5, №3 → Task 6, №4 → Task 8; §7 конфаб → Task 9; §8 DoD → Task 9. Free-form персона, UI-контрол, режим В — parked (§9), в план не входят.
- **Мины §6:** тексты Task 1 и Task 5 проверены на `"9."`/`"д)"`/`"СТАДИЯ "` вручную + закреплены тестами.
- **Type consistency:** `build_persona_block(name) -> str` (Task 5) — единственное имя билдера во всех задачах; параметр фабрик везде `persona_block_for`; исходы инструмента согласованы между Task 8 шагами.
- **Известный конфликт суиты:** `tests/test_tools.py:37` — exact-set; решение (additive-расширение по прецеденту UI-4) вынесено в Global Constraints и Task 8 Step 4.
