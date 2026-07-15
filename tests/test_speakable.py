"""speakable() — форматный фильтр KV-2 (спека §4.2, тесты §14.1.2). Корпус — реальные тексты
Коры, намайненные из journals/ + journals-staging/ живых прогонов (сами журналы гитигнорены;
здесь только представительные строки, обрезанные и обезличенные — без ключей и без домашних
путей реального пользователя: /Users/terobyte → /Users/someone).

⚠️ Терминология (§4.2 запрещает иное): это ФОРМАТНЫЙ фильтр «прозвучит ли текст как речь, а
не как зачитанный markdown». Не фильтр безопасного содержания, не semantic safety-классификатор:
короткая разговорная prompt-injection-фраза проходит его штатно и это по проекту.
"""
from __future__ import annotations

import pytest

from synapse.config import SynapseConfig
from synapse.dispatcher.speakable import speakable

CAP = SynapseConfig().kora_speak_max_chars  # 350 — дефолт, на нём и живёт Play-путь


def _clean(text: str) -> bool:
    return speakable(text, max_chars=CAP)


# --------------------- корпус: реальные тексты Коры молчат ---------------------

# Строки ниже — дословные фрагменты из живых прогонов (thread feed, kind="text").
REAL_KORA_DIRTY = [
    # инлайн-код — самый частый шум в коротких репликах Коры
    "Нашёл папку `agentx`. Смотрю её содержимое и описание.",
    "Книга успешно обработана: `naik-swapnali-joshi-think-ai`, score 0.97 (ok). "
    "Теперь посмотрю список глав и вытащу первую.",
    'This project — "librarian-cli" (deterministic book-to-markdown pipeline), '
    "CLI команда `lib`. Читаю README чтобы узнать про нарезку глав.",
    # абсолютный путь
    "Нашёл проект: `/Users/someone/Desktop/Projects/Active/scripts/libby`. Смотрю содержимое.",
    "Не могу это выполнить: мне разрешено работать только внутри рабочей директории "
    "`/Users/someone/synapse-kora-workspace` и запрещено обращаться к путям за её пределами.",
    # markdown-заголовок + булеты + bold — терминальный отчёт
    "Готово. Вот что было сделано:\n\n**1. Книга найдена** в `~/Downloads`: `Think AI.pdf`.",
    "## Что это\n**Синапс — голосовой каскад-мост к Коре (M0)**.",
    "- `synapse/` — основной python-пакет\n- `tests/` — тесты",
    # нумерованный список уточняющих вопросов
    "Уточните, пожалуйста, что нужно сделать.\n\n"
    "1. Опечатка при вставке текста, и часть сообщения не отправилась?\n"
    "2. Речь про конкретные файлы в рабочей директории?",
    "1. **O que deseja desenhar?** (ex: uma casa, um personagem, um logotipo)",
]


@pytest.mark.parametrize("text", REAL_KORA_DIRTY)
def test_real_kora_markup_is_not_speakable(text):
    assert _clean(text) is False


# Дословные разговорные реплики Коры из тех же прогонов — они обязаны звучать как есть.
REAL_KORA_CLEAN = [
    "Теперь запускаю ingest книги и вытаскиваю главу 1.",
    "Слышу тебя. Похоже, это строчка, которая отзывается чем-то тяжёлым — чувство, "
    "что не хватило сил кого-то защитить или спасти.",
    "Готово, задача выполнена, все тесты зелёные.",
    "Отлично, это книга, создана через calibre, 214 страниц. Это и есть искомая.",
    "Я готова помочь в любом из этих направлений — хоть довести строчку до полноценного "
    "текста, хоть просто побыть рядом в разговоре.",
    # английский — Кора отвечает на языке пользователя
    "Done. I fixed the failing tests and everything passes now.",
    "I could not find the book in your downloads folder. Where should I look?",
]


@pytest.mark.parametrize("text", REAL_KORA_CLEAN)
def test_short_conversational_speech_is_speakable(text):
    assert _clean(text) is True


# --------------------- отдельные правила ---------------------

@pytest.mark.parametrize("text", [
    "```python\nprint(1)\n```",           # код-фенс
    "~~~\nsome code\n~~~",                # альтернативный фенс
    "вызови `speakable()` тут",           # инлайн-код
    "| файл | статус |\n| --- | --- |",   # markdown-таблица
    "|---|---|",                          # только разделитель
    "col a | col b | col c",              # ряд таблицы без разделителя
    "# Заголовок",
    "### Мелкий заголовок",
    "> цитата",
    "* пункт списка",
    "+ ещё пункт",
    "- пункт списка",
    "2) второй пункт",
    "это **важно** знать",
    "это *важно* знать",
    "поле total_cost_usd выросло",        # snake_case
    "__жирный__ текст",                   # markdown-эмфаза через _
])
def test_markup_is_dirty(text):
    assert _clean(text) is False


@pytest.mark.parametrize("text", [
    "смотри /Users/someone/Desktop и там всё",
    "лежит в ~/Downloads уже",
    "правил synapse/dispatcher/loop.py сегодня",
    "открой https://example.com в браузере",
    "зайди на www.example.com",
    r"файл C:\Windows лежит там",
])
def test_paths_and_urls_are_dirty(text):
    assert _clean(text) is False


@pytest.mark.parametrize("text", [
    "правил app.py и всё",
    "смотри README.md там",
    "открой index.html в браузере",
])
def test_filenames_are_dirty(text):
    assert _clean(text) is False


def test_frozen_play_path_sample_is_dirty():
    """ЖЁСТКИЙ контракт: ровно этот текст гоняет замороженный
    tests/test_api_tts_diff.py::test_tts_kora_role_runs_speakify_and_caches_sanitized и
    требует speakify == 1. Сочтём его чистым — сломаем замороженный тест."""
    assert speakable("готово: правил app.py:1024, тесты зелёные", max_chars=350) is False


@pytest.mark.parametrize("text", [
    "правил app.py:1024 сегодня",         # файл:строка
    "упал на loop.py:88 опять",
])
def test_file_line_refs_are_dirty(text):
    assert _clean(text) is False


@pytest.mark.parametrize("text", [
    "id naik-swapnali-joshi-think-ai готов",              # 2+ дефиса, длинный
    "сессия 1fe43e08-bd63-48e2-b165-e10db8296afb упала",  # uuid
    "модель gemini-3.5-flash отвечает",                   # буквы+цифры
    "смотри getUserProfileById там",                      # camelCase
])
def test_long_identifiers_are_dirty(text):
    assert _clean(text) is False


# --------------------- граница «слово vs идентификатор» ---------------------

@pytest.mark.parametrize("text", [
    # длинные ОБЫЧНЫЕ слова: одна форма, без цифр и внутренней пунктуации — читаются голосом
    "достопримечательность и неопределённость",
    "This is an incomprehensibility, honestly.",
    # короткие технические токены живут ниже порога длины и проходят
    "версия v1 уже готова",
    "проект M0 закрыт",
    "слайс KV-2 сдан",
    "снял на iPhone вчера",
    "смотри на YouTube потом",
    "пишу на JavaScript сейчас",
])
def test_ordinary_long_words_and_short_tokens_stay_speakable(text):
    assert _clean(text) is True


@pytest.mark.parametrize("text", [
    "и/или он/она — это нормальная речь",   # один слэш путём не считается
    "встретимся в 10:30, счёт был 2:1",     # время/счёт — не файл:строка
    "оценка 0.97, это хорошо",              # десятичная дробь — не имя файла
    "выпуск 2026-07-15 состоялся вчера",    # дата: дефисы без букв — не идентификатор
    "это т.д. и т.п. в общем",              # кириллические сокращения — не имена файлов
    "например e.g. вот так",                # односимвольный стем — не имя файла
    "минус 5 градусов, -5 это холодно",     # дефис без пробела — не буллет
])
def test_speech_lookalikes_are_not_false_positives(text):
    assert _clean(text) is True


# --------------------- кап длины ---------------------

def test_length_cap_is_exclusive_boundary():
    assert speakable("а" * 350, max_chars=350) is True
    assert speakable("а" * 351, max_chars=350) is False


def test_length_cap_measured_after_strip():
    """Обрамляющие пробелы не съедают бюджет: кап — про произносимый текст."""
    assert speakable("  " + "а" * 350 + "  ", max_chars=350) is True


def test_cap_zero_silences_everything():
    assert speakable("да", max_chars=0) is False


# --------------------- pure/total (§11.1) ---------------------

@pytest.mark.parametrize("text", ["", "   ", "\n\n", "\t \r\n"])
def test_empty_or_whitespace_is_never_speakable(text):
    assert speakable(text, max_chars=350) is False


@pytest.mark.parametrize("text", [
    "\ud800",                    # одинокий суррогат
    "\x00\x01\x02",              # управляющие символы
    "🔥" * 50,                    # эмодзи
    "🏳️‍🌈 ZWJ-последовательность",
    "‮​﻿",        # bidi/zero-width/BOM
    "ﷺ",                          # лигатура
    "𝐁𝐨𝐥𝐝 математические буквы",
    "ｆｕｌｌｗｉｄｔｈ",
    "a" * 100000,                # длинная простыня: кап отсекает до regex-прогона
    "`" * 5000,
    "|" * 5000,
    "-" * 5000,
    "/" * 5000,
    "́" * 100,              # комбинирующие диакритики
    "؀؁؂",                        # арабские спецсимволы
    "\N{THAI CHARACTER KO KAI}" * 30,
])
def test_fuzz_unicode_never_raises_and_returns_bool(text):
    assert isinstance(speakable(text, max_chars=350), bool)


def test_non_str_input_is_total_not_a_crash():
    """Total (§11.1): «любой input превращается в bool без исключения». Вызывающий —
    live-путь и Play-путь — не обязан доказывать тип, чтобы не уронить озвучку."""
    for junk in (None, 123, b"bytes", [], {}, object()):
        assert speakable(junk, max_chars=350) is False  # type: ignore[arg-type]


def test_is_pure_no_io_and_stable():
    """Ноль сети, ноль I/O: одинаковый вход даёт одинаковый выход, вход не мутируется."""
    text = "Готово, задача выполнена."
    assert speakable(text, max_chars=350) is speakable(text, max_chars=350)
    assert text == "Готово, задача выполнена."
