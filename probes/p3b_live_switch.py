"""Probe P3b (§14.2.4, живая половина) — Fish voice-switch latency против боевого WS.

Go/no-go для KV-1. §4.1 делает арбитра voice-aware: перед репликой с другим голосом он
пушит `TTSUpdateSettingsFrame(delta=FishAudioTTSService.Settings(voice=<id>))`, затем сам
speak-frame. В pipecat 1.5 `FishAudioTTSService._update_settings` на ЛЮБОЕ изменение
voice/model отвечает `await self._disconnect(); await self._connect()` — полный реконнект
WebSocket (`fish/tts.py:219-236`). Спека (§0.1, риск-строка §9) ставит жёсткий порог:
p95 ≤ 500 мс, иначе KV-1 no-go и дизайн переезжает в отдельный двух-сервисный, а не в
скрытый fallback. Честный NO-GO — нормальный результат; probe не болеет за GO.

Что здесь измеряется и почему именно так:

* РЕАЛЬНЫЙ `FishAudioTTSService` и реальная сеть. Фейка нет намеренно: автоматическая
  половина P3b едва не соврала именно потому, что фейк расходился с боевым сервисом в
  измеряемом параметре. Здесь измеряемое — сама платформа Fish, подделать её нечем.
* ТОЧНАЯ ФОРМА СВИТЧА из §4.1: `TTSUpdateSettingsFrame(delta=...Settings(voice=...))`.
  Приватные методы (`_update_settings`, `_connect`) не трогаются — probe меряет тот путь,
  который поедет в проде, а не более короткий.
* КОНТРОЛЬ (без него число ничего не значит). Абсолютное «speak → первое аудио» — это в
  основном собственный TTFB синтеза Fish: свитч его не создаёт и KV-1 его не починит.
  Поэтому на том же прогоне, том же соединении и том же пуле фраз меряется вторая
  выборка — реплики БЕЗ свитча — и считается дельта. Вердикт выносится по обоим чтениям.
* ДЕЛЬТА СЧИТАЕТСЯ ПОПАРНО, а не как разность двух независимых p95 (см. `paired_deltas`).
* МОДЕЛЬ — параметр (`--model` / `P3B_MODEL`, дефолт `FISH_TTS_MODEL` из `.env`). Дефолтный
  прогон меряет то, что реально крутится в проде; ключ нужен, чтобы прогнать и платный
  тариф, потому что боевой дефолт `s2.1-pro-free` — временный промо-тариф (`config.py:24`).
* `context_id` ПРОВЕРЯЕТСЯ, а не собирается впустую (см. `context_id_correlation`).
* СЕРИАЛИЗАЦИЯ. Арбитр — единственная точка сериализации выдачи в TTS (§4.1), поэтому и
  probe шлёт строго по одной реплике, дожидаясь её конца. Пайплайнить нельзя: измерялось
  бы то, чего продукт никогда не делает. Побочно сериализация даёт однозначную атрибуцию
  аудио к реплике — в каждый момент открыт ровно один audio context.

Run:  .venv/bin/python probes/p3b_live_switch.py
Exit: 0 = GO, 1 = NO-GO, 2 = probe не смог отработать (сеть/ключ) — это НЕ вердикт платформе.
"""

import asyncio
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.fish.tts import FishAudioTTSService
from pipecat.workers.runner import WorkerRunner

# «Спокойный женский голос» (ru) — проверено доступным этому ключу через Fish model API.
# Не секрет, печатается. Переопределяется KORA_FISH_REFERENCE_ID.
KORA_VOICE_DEFAULT = "2a1036d645634680b3cc69aeeb60375b"

ROUNDS = 20  # §14.2.4: 20 чередований disp↔kora. Каждый раунд = 1 switched + 1 baseline.
SWITCH_P95_THRESHOLD_MS = 500.0  # §0.1 / риск-строка §9

# Три короткие русские фразы, а не две и не четыре: голос чередуется с периодом 2, фраза —
# с периодом 3, lcm = 6, поэтому КАЖДАЯ фраза за 20 раундов успевает прозвучать обоими
# голосами (нужно для проверки «свитч вообще что-то делает») и каждая пара (голос, текст)
# повторяется (нужно для контроля детерминизма синтеза). При периоде 2 или 4 фраза
# намертво срасталась бы с одним голосом, и сравнивать было бы нечего.
PHRASES = [
    "Задача принята, начинаю работу.",
    "Первый шаг готов, иду дальше.",
    "Проверил результат, всё сходится.",
]

# Реплика ждёт первого аудио не дольше этого. 30 с — это не «медленный свитч», это рваное
# соединение: реконнект Fish укладывается в сотни миллисекунд или не происходит вовсе.
UTTERANCE_TIMEOUT_S = 30.0


class ProbeCannotRun(Exception):
    """Сеть/ключ/подключение. Не вердикт платформе — exit 2, а не NO-GO."""


class UtteranceLost(Exception):
    """Реплика не дала аудио в отведённый срок. Спека считает потерю реплики no-go-условием."""


@dataclass
class Sample:
    kind: str  # "switched" | "baseline"
    round_idx: int  # номер раунда: switched и baseline одного раунда — пара (см. paired_deltas)
    voice_role: str  # "disp" | "kora"
    text: str
    ms: float  # инициация (switch|speak) → первое аудио этой реплики
    context_id: str | None  # из TTSStartedFrame этой реплики
    # Факты для проверки корреляции: какие id стояли на аудио-фреймах ЭТОЙ реплики и на её
    # TTSStoppedFrame. Собираются, чтобы их проверить (context_id_correlation), а не «на всякий».
    audio_context_ids: set[str | None]
    stopped_context_id: str | None
    audio_bytes: bytes


class Tap(FrameProcessor):
    """Штампует время TTS-фреймов в тот момент, когда они покидают Fish-сервис.

    Точка замера выбрана осознанно: аудио сюда попадает уже пройдя audio-context task и
    `push_frame`, т.е. ровно там, где в проде его подхватил бы transport output. Меряется
    путь до реально отдаваемого наружу аудио, а не момент прихода байтов в receive-таск.

    `enable_direct_mode=True` — как у pipecat-овского `QueuedFrameProcessor`: фрейм
    обрабатывается инлайн, без собственной очереди процессора, чтобы в измерение не
    заехала лишняя итерация event loop-а. Штамп ставится ДО `push_frame`.
    """

    def __init__(self, events: asyncio.Queue) -> None:
        super().__init__(enable_direct_mode=True)
        self._events = events

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, (TTSStartedFrame, TTSAudioRawFrame, TTSStoppedFrame, ErrorFrame)):
            self._events.put_nowait((time.perf_counter(), frame))
        await self.push_frame(frame, direction)


def nearest_rank_percentile(values: list[float], pct: float) -> float:
    """Nearest-rank перцентиль: возвращает реально измеренное значение, а не интерполяцию
    между двумя соседними — на n=20 интерполяция придумывает точку, которой не было.

    p95 при n=20 = 19-е по возрастанию значение (`ceil(0.95*20)`).

    NB: nearest-rank здесь скорее ЗАНИЖАЕТ относительно линейной интерполяции (та берёт
    индекс `0.95*(n-1) = 18.05`, т.е. чуть выше 19-го значения), а не завышает — в ранней
    редакции докстринга было написано обратное. На вердикт это не влияло ни разу: порог
    промахивается кратно, а не на единицы миллисекунд. Но если прогон однажды сядет на
    границу, выбор перцентиля станет решающим — и тогда важно, что он консервативен
    В ПОЛЬЗУ GO, а не против него.
    """
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


class LiveSwitchProbe:
    def __init__(self, tts: FishAudioTTSService, worker: PipelineWorker, events: asyncio.Queue):
        self._tts = tts
        self._worker = worker
        self._events = events
        self.connects = 0
        self.connect_errors: list[str] = []

    async def utterance(
        self, text: str, voice_role: str, switch_to: str | None, round_idx: int
    ) -> Sample:
        """Одна реплика. `switch_to=None` — контрольная выборка (голос уже нужный, свитча нет).

        Предусловие (держится вызывающим): предыдущий audio context закрыт и обработка
        фреймов не на паузе, поэтому t0 не включает чужое ожидание.
        """
        t0 = time.perf_counter()
        if switch_to is not None:
            # Точная форма §4.1. Порядок обязателен: сначала свитч, потом реплика.
            await self._worker.queue_frame(
                TTSUpdateSettingsFrame(delta=FishAudioTTSService.Settings(voice=switch_to))
            )
        await self._worker.queue_frame(TTSSpeakFrame(text=text, append_to_context=False))

        first_audio_at: float | None = None
        context_id: str | None = None
        audio_context_ids: set[str | None] = set()
        stopped_context_id: str | None = None
        audio = bytearray()
        deadline = t0 + UTTERANCE_TIMEOUT_S

        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise UtteranceLost(f"нет TTSStoppedFrame за {UTTERANCE_TIMEOUT_S:.0f} с: {text!r}")
            try:
                ts, frame = await asyncio.wait_for(self._events.get(), timeout=remaining)
            except TimeoutError as e:
                raise UtteranceLost(f"нет аудио за {UTTERANCE_TIMEOUT_S:.0f} с: {text!r}") from e

            if isinstance(frame, ErrorFrame):
                raise ProbeCannotRun(f"ErrorFrame из TTS: {frame.error}")
            if isinstance(frame, TTSStartedFrame):
                context_id = frame.context_id
            elif isinstance(frame, TTSAudioRawFrame):
                if first_audio_at is None:
                    first_audio_at = ts
                audio_context_ids.add(frame.context_id)
                audio.extend(frame.audio)
            elif isinstance(frame, TTSStoppedFrame):
                stopped_context_id = frame.context_id
                break

        if first_audio_at is None:
            raise UtteranceLost(f"контекст закрылся без единого аудио-фрейма: {text!r}")

        # Реплика доиграна и её контекст удалён. Возвращаем сервис из паузы ровно тем
        # фреймом, которым это делает BaseOutputTransport в проде (base_output.py:704-706):
        # после TTSSpeakFrame сервис зовёт pause_processing_frames() и без
        # BotStoppedSpeakingFrame следующий TTSUpdateSettingsFrame навсегда застрял бы в
        # очереди (tts_service.py:781, 803). Это не хак probe-а, это контракт пайплайна.
        await self._worker.queue_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)  # resume должен приземлиться ДО следующего t0 — вне замера

        return Sample(
            kind="baseline" if switch_to is None else "switched",
            round_idx=round_idx,
            voice_role=voice_role,
            text=text,
            ms=(first_audio_at - t0) * 1000.0,
            context_id=context_id,
            audio_context_ids=audio_context_ids,
            stopped_context_id=stopped_context_id,
            audio_bytes=bytes(audio),
        )

    async def run(self, disp_voice: str, kora_voice: str) -> list[Sample]:
        voices = {"disp": disp_voice, "kora": kora_voice}
        samples: list[Sample] = []

        # Чередование switched/baseline попарно внутри раунда, а не двумя блоками подряд:
        # сеть и нагрузка Fish дрейфуют за прогон, и два блока сравнивали бы разные
        # минуты, а не разные механики. Внутри пары — один голос, соседние фразы из
        # одного пула: разница между членами пары ровно одна — был свитч или нет.
        # Тексты в паре РАЗНЫЕ намеренно: одинаковый текст подряд рисковал бы поймать
        # серверный кэш Fish и занизить baseline, т.е. раздуть дельту в пользу NO-GO.
        # Пара — это не только порядок прогона, но и ЕДИНИЦА АНАЛИЗА: `round_idx` доезжает
        # до `paired_deltas`, где дельта считается внутри пары.
        for i in range(ROUNDS):
            role = "kora" if i % 2 == 0 else "disp"  # старт с disp (из конструктора) → каждый раунд реально свитчит
            samples.append(await self.utterance(PHRASES[i % 3], role, voices[role], i))
            samples.append(await self.utterance(PHRASES[(i + 1) % 3], role, None, i))
            done = i + 1
            print(f"    раунд {done:2d}/{ROUNDS}  switched={samples[-2].ms:6.1f} мс  "
                  f"baseline={samples[-1].ms:6.1f} мс  дельта={samples[-2].ms - samples[-1].ms:+7.1f} мс  "
                  f"голос={role}", flush=True)
        return samples


def _size_evidence(samples: list[Sample]) -> str:
    """Косвенная улика на случай, когда байты сравнивать бессмысленно (см. ниже).

    Идея: если синтез недетерминирован, то повтор той же пары (голос, текст) даёт
    естественный разброс длины — это шум ресинтеза. Расхождение длин МЕЖДУ голосами на
    одном тексте имеет смысл только в сравнении с этим шумом. Это не доказательство
    идентичности говорящего (её измеряет ухо/спектр, не длина), но отделяет «голоса
    реально разные» от «одна и та же озвучка с джиттером».
    """
    sizes: dict[tuple[str, str], list[int]] = defaultdict(list)
    for s in samples:
        sizes[(s.voice_role, s.text)].append(len(s.audio_bytes))

    within = [max(v) - min(v) for v in sizes.values() if len(v) >= 2]
    texts = {t for _, t in sizes}
    gaps = []
    for t in texts:
        d, k = sizes.get(("disp", t)), sizes.get(("kora", t))
        if d and k:
            gaps.append(abs(statistics.fmean(d) - statistics.fmean(k)))
    if not within or not gaps:
        return ""
    verdict = ("расхождение голосов ВЫШЕ шума ресинтеза" if min(gaps) > max(within)
               else "расхождение голосов НЕ отделимо от шума ресинтеза")
    return (f" Длины: шум при повторе того же голоса ≤ {max(within):.0f} B, "
            f"расхождение между голосами {min(gaps):.0f}–{max(gaps):.0f} B → {verdict}.")


def audio_differs_by_voice(samples: list[Sample]) -> tuple[bool | None, str]:
    """Свитч вообще что-то делает? Иначе арбитр реконнектит впустую и KV-1 бессмысленен.

    Ловушка: «байты разные» само по себе НИЧЕГО не доказывает, если синтез Fish
    недетерминирован — тогда и один голос дважды даст разные байты. Поэтому сначала
    контроль детерминизма на естественных повторах (голос, текст), и только он решает,
    что значит межголосовое различие. Возврат None = вопрос не решён этим probe-ом.
    """
    by_voice_text: dict[tuple[str, str], list[bytes]] = defaultdict(list)
    for s in samples:
        by_voice_text[(s.voice_role, s.text)].append(s.audio_bytes)

    repeats = [v for v in by_voice_text.values() if len(v) >= 2]
    deterministic = None
    if repeats:
        deterministic = all(all(a == group[0] for a in group) for group in repeats)

    by_text: dict[str, dict[str, bytes]] = defaultdict(dict)
    for s in samples:
        by_text[s.text].setdefault(s.voice_role, s.audio_bytes)
    pairs = [(t, v["disp"], v["kora"]) for t, v in by_text.items() if "disp" in v and "kora" in v]
    if not pairs:
        return None, "ни один текст не прозвучал обоими голосами — сравнивать нечего"

    diff = [(t, d, k) for t, d, k in pairs if d != k]
    same = [t for t, d, k in pairs if d == k]
    if same:
        return False, (
            f"{len(same)} текст(ов) дали ПОБАЙТОВО ОДИНАКОВОЕ аудио двумя голосами "
            f"({same[0]!r}) — свитч не меняет звук, реконнект впустую"
        )

    sizes = ", ".join(f"{t[:18]!r}: disp {len(d)}B / kora {len(k)}B" for t, d, k in diff[:3])
    if deterministic is True:
        return True, (
            f"синтез детерминирован (повтор той же пары голос+текст даёт байт-в-байт то же), "
            f"поэтому различие байтов на {len(pairs)} общих текстах = различие голосов. {sizes}"
        )
    if deterministic is False:
        return None, (
            f"аудио двух голосов различается на всех {len(pairs)} общих текстах, НО повтор "
            f"той же пары голос+текст тоже даёт разные байты → синтез недетерминирован и "
            f"побайтовое сравнение смену голоса НЕ доказывает.{_size_evidence(samples)} "
            f"Тождество говорящего — аудитивно/спектрально, вне этого probe"
        )
    return None, f"повторов пары (голос, текст) не набралось, детерминизм не проверен. {sizes}"


def paired_deltas(samples: list[Sample]) -> list[float]:
    """Цена свитча = (switched − baseline) ВНУТРИ раунда, а не разность двух независимых p95.

    Почему не `sw_p95 - bl_p95` (как считала первая версия): это разность двух независимо
    посчитанных порядковых статистик, и на n=20 каждая из них — ОДНО наблюдение (19-е по
    возрастанию). Один тяжёлый baseline-хвост (Fish иногда даёт TTFB ~580 мс вместо
    типичных ~200) сдвигает bl_p95 на сотни миллисекунд, и «цена свитча» скачет вслед за
    сэмплом, к свитчу отношения не имеющим. Это воспроизведено: на одном и том же коде и
    пороге такая разность давала +726.8, +768.3, +300.3, +401.7 и +256.1 мс — ТРИ значения
    из пяти ниже порога 500, т.е. статистика переворачивала вердикт чтения B, хотя
    измеряемая механика не менялась. Парная дельта на тех же двух последних прогонах:
    +744.0 и +754.1 — стабильно и однозначно NO-GO.
    Пары уже есть в дизайне прогона: switched и baseline одного раунда идут подряд, тем же
    голосом, по тому же соединению, в пределах секунд. Разность ВНУТРИ пары вычитает общий
    дрейф сети/нагрузки Fish, вместо того чтобы сравнивать два независимых хвоста. Тяжёлый
    baseline портит тогда одну пару, а не всю статистику.
    """
    by_round: dict[int, dict[str, float]] = defaultdict(dict)
    for s in samples:
        by_round[s.round_idx][s.kind] = s.ms
    return [
        r["switched"] - r["baseline"]
        for _, r in sorted(by_round.items())
        if "switched" in r and "baseline" in r
    ]


def context_id_correlation(samples: list[Sample]) -> tuple[bool | None, list[str]]:
    """Живая проверка того, на чём держится presence-механизм §14.2.4.

    Автоматическая половина P3b доказала уникальность `context_id` на фейке. Здесь тот же
    вопрос задаётся РЕАЛЬНОМУ сервису: id присутствует, непустой, уникален на все реплики
    прогона, и аудио с `TTSStoppedFrame` каждой реплики несут именно тот id, который
    приехал в её `TTSStartedFrame` (иначе `pop_started()` свяжет окно не с той репликой).

    Границы честности, которые эта проверка НЕ переходит. Протокол Fish не возвращает
    context_id на входящем аудио: `_receive_messages` берёт его из ЛОКАЛЬНОГО курсора
    сервиса — `get_active_audio_context_id()` = `_playing_context_id or _turn_context_id`
    (`fish/tts.py:366-373`, `tts_service.py:1371-1384`, где это прямо названо fallback-ом
    «для сервисов, чей протокол не эхоит context_id»). Поэтому проверка доказывает, что при
    СЕРИАЛИЗОВАННОЙ выдаче курсор ни разу не приписал аудио чужой реплике — и ничего не
    говорит про reordering/drop: на проводе Fish нет id, который можно было бы переставить.
    Требование §14.2.4 «exact correlation при SPEAK reordering/drop» этим probe-ом закрыть
    нельзя в принципе, и подделывать его фейком было бы измерением себя.
    """
    notes: list[str] = []
    n = len(samples)
    missing = [s for s in samples if not s.context_id]
    ids = [s.context_id for s in samples]
    dupes = n - len({*ids})
    mismatched = [
        s for s in samples
        if s.audio_context_ids != {s.context_id} or s.stopped_context_id != s.context_id
    ]

    if missing:
        notes.append(f"у {len(missing)} из {n} реплик context_id пуст/отсутствует")
    if dupes:
        notes.append(f"context_id НЕ уникален: {dupes} повторов на {n} реплик")
    if mismatched:
        s = mismatched[0]
        notes.append(
            f"у {len(mismatched)} из {n} реплик аудио/stopped несут не тот id, что "
            f"TTSStartedFrame (первая: started={s.context_id}, аудио={s.audio_context_ids}, "
            f"stopped={s.stopped_context_id})"
        )
    if notes:
        return False, notes
    return True, [
        f"{n}/{n} реплик: context_id непустой и уникален; аудио и TTSStoppedFrame каждой "
        f"реплики несут id её TTSStartedFrame — на живом WS, не на фейке",
        "границы: атрибуция аудио у Fish идёт по локальному курсору сервиса "
        "(протокол id не эхоит), поэтому reordering/drop этим НЕ покрыт — см. докстринг",
    ]


def report(samples: list[Sample], connects: int) -> bool:
    switched = [s.ms for s in samples if s.kind == "switched"]
    baseline = [s.ms for s in samples if s.kind == "baseline"]
    deltas = paired_deltas(samples)

    def stats(v: list[float]) -> tuple[float, float, float, float]:
        return (
            statistics.median(v),
            nearest_rank_percentile(v, 95),
            max(v),
            statistics.fmean(v),
        )

    sw_p50, sw_p95, sw_max, sw_mean = stats(switched)
    bl_p50, bl_p95, bl_max, bl_mean = stats(baseline)
    d_p50, d_p95, d_max, d_mean = stats(deltas)
    naive_delta_p95 = sw_p95 - bl_p95  # см. paired_deltas: печатается справочно, вердикт не несёт

    print("\n  распределения (инициация → первое аудио реплики), мс:")
    print(f"    {'выборка':<28} {'n':>3} {'p50':>8} {'p95':>8} {'max':>8} {'mean':>8}")
    print(f"    {'switched (свитч+speak)':<28} {len(switched):>3} {sw_p50:>8.1f} {sw_p95:>8.1f} "
          f"{sw_max:>8.1f} {sw_mean:>8.1f}")
    print(f"    {'baseline (speak, без свитча)':<28} {len(baseline):>3} {bl_p50:>8.1f} "
          f"{bl_p95:>8.1f} {bl_max:>8.1f} {bl_mean:>8.1f}")
    print(f"    {'ПАРНАЯ ДЕЛЬТА (цена свитча)':<28} {len(deltas):>3} {d_p50:>+8.1f} {d_p95:>+8.1f} "
          f"{d_max:>+8.1f} {d_mean:>+8.1f}")
    print(f"    (парная = switched−baseline внутри раунда; разность независимых p95 дала бы "
          f"{naive_delta_p95:+.1f} — справочно, в вердикт не идёт: см. докстринг paired_deltas)")

    # Механика §4.1 наощупь: если бы свитч не реконнектил (или реконнектил лишний раз на
    # baseline-репликах), счётчик не сошёлся бы. 1 стартовый коннект + по одному на свитч.
    expected_connects = len(switched) + 1
    ok_connects = connects == expected_connects
    print(f"\n  реконнектов WS (on_connected): {connects}, ожидалось {expected_connects} "
          f"(1 стартовый + по одному на каждый из {len(switched)} свитчей) → "
          f"{'сходится: свитч реконнектит, baseline — нет' if ok_connects else 'НЕ СХОДИТСЯ — механика §4.1 не подтверждена'}")

    ok_diff, diff_detail = audio_differs_by_voice(samples)
    mark = {True: "OK ", False: "FAIL", None: "??? "}[ok_diff]
    print(f"\n  [{mark}] свитч меняет звук: {diff_detail}")

    ok_corr, corr_notes = context_id_correlation(samples)
    print(f"\n  [{'OK ' if ok_corr else 'FAIL'}] context_id ↔ реплика (живой WS):")
    for note in corr_notes:
        print(f"         {note}")

    # Двусмысленность, которую probe НЕ решает молча: спека пишет «p95 reconnect-to-audio
    # ≤ 500 мс», и это буквально абсолютное чтение (switched p95). Но абсолютное число на
    # 90+% состоит из TTFB синтеза Fish, который свитч не создаёт и KV-1 не устранит —
    # дельта отвечает на вопрос «сколько стоит реконнект», ради которого риск-строка §9 и
    # написана. Оба чтения печатаются; вердикт — по буквальному (абсолютному), как в тексте
    # спеки, а расхождение выносится наверх явно.
    abs_go = sw_p95 <= SWITCH_P95_THRESHOLD_MS
    delta_go = d_p95 <= SWITCH_P95_THRESHOLD_MS
    print(f"\n  чтение A (буквальное, текст спеки): switched p95 = {sw_p95:.1f} мс "
          f"{'≤' if abs_go else '>'} {SWITCH_P95_THRESHOLD_MS:.0f} мс → {'GO' if abs_go else 'NO-GO'}")
    print(f"  чтение B (цена свитча, смысл риск-строки §9): ПАРНАЯ дельта p95 = {d_p95:+.1f} мс "
          f"{'≤' if delta_go else '>'} {SWITCH_P95_THRESHOLD_MS:.0f} мс → {'GO' if delta_go else 'NO-GO'}")
    if abs_go != delta_go:
        print("  ⚠ чтения РАСХОДЯТСЯ — порог решает исход. Это находка для владельца спеки, "
              "а не деталь: §0.1 обязан зафиксировать, какое чтение нормативно.")
    else:
        print("  чтения совпадают — трактовка порога на исход не влияет.")

    verdict = abs_go and delta_go and ok_diff is not False and ok_corr is not False
    print(f"\nВЕРДИКТ P3b (живая половина) для KV-1: {'GO' if verdict else 'NO-GO'}")
    if ok_diff is False:
        print("  блокер: два голоса дают одинаковое аудио — реконнект ни за чем.")
    if ok_corr is False:
        print("  блокер: exact-корреляция context_id ↔ реплика не держится на живом сервисе.")
    return verdict


async def main() -> int:
    import importlib.metadata as md

    # Явный путь, а не поиск от cwd: probe обязан читать ключи репозитория независимо от
    # того, откуда его запустили.
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    api_key = os.environ.get("FISH_AUDIO_API_KEY")
    disp_voice = os.environ.get("FISH_REFERENCE_ID")
    prod_model = os.environ.get("FISH_TTS_MODEL")
    kora_voice = os.environ.get("KORA_FISH_REFERENCE_ID", KORA_VOICE_DEFAULT)

    # Модель — параметр, но дефолт — БОЕВОЙ: прогон без аргументов меряет то, что реально
    # крутится в проде (`config.py:26`), а не то, что удобнее probe-у. Ручка существует
    # потому, что боевой дефолт `s2.1-pro-free` — временный промо-тариф (бесплатен до конца
    # июля 2026, `config.py:24`), а бесплатные тарифы принято ограничивать на уровне
    # инфраструктуры — а значит стоимость реконнекта на платном тарифе может отличаться, и
    # это надо ИЗМЕРИТЬ, а не оговорить. Вердикт всё равно выносится по боевой модели.
    argv = sys.argv[1:]
    model = os.environ.get("P3B_MODEL") or prod_model  # env-ручка, дефолт — боевая модель
    if "--model" in argv:  # явный флаг бьёт env: он виден в команде прогона рядом с числом
        i = argv.index("--model")
        if i + 1 >= len(argv):
            print("usage: p3b_live_switch.py [--model <fish-model-id>]")
            return 2
        model = argv[i + 1]

    if not api_key or not disp_voice or not model:
        print("probe не может отработать: в .env нет FISH_AUDIO_API_KEY / FISH_REFERENCE_ID / "
              "FISH_TTS_MODEL")
        return 2
    if disp_voice == kora_voice:
        print("probe не может отработать: голоса диспетчера и Коры совпадают — свитча не будет")
        return 2

    print(f"probe P3b — живая половина: Fish voice-switch latency · pipecat {md.version('pipecat-ai')}")
    is_prod = model == prod_model
    print(f"  модель: {model}" + ("  ← боевая (FISH_TTS_MODEL): вердикт выносится по ней"
                                  if is_prod else
                                  f"  ← НЕ боевая (в проде {prod_model}): справочный прогон, "
                                  f"вердикт по боевой модели"))
    print(f"  голос disp (FISH_REFERENCE_ID):        {disp_voice}")
    print(f"  голос kora (KORA_FISH_REFERENCE_ID):   {kora_voice}")
    print(f"  план: {ROUNDS} чередований disp↔kora, каждое со своей baseline-парой "
          f"= {ROUNDS * 2} реплик, строго по одной")
    print("  (ключ не печатается и никуда не пишется)\n")

    events: asyncio.Queue = asyncio.Queue()
    # Конструируется ровно как в проде (app.py:1123-1126): те же kwargs, никаких
    # probe-специфичных послаблений. stop_frame_timeout_s остаётся дефолтным (3 с) — он
    # определяет только длительность прогона, но менять его значило бы мерить не тот сервис.
    tts = FishAudioTTSService(
        api_key=api_key,
        settings=FishAudioTTSService.Settings(model=model, voice=disp_voice),
    )
    tap = Tap(events)
    worker = PipelineWorker(Pipeline([tts, tap]), cancel_on_idle_timeout=False)
    probe = LiveSwitchProbe(tts, worker, events)

    @tts.event_handler("on_connected")
    async def _on_connected(_service):
        probe.connects += 1

    @tts.event_handler("on_connection_error")
    async def _on_connection_error(_service, error):
        probe.connect_errors.append(str(error))

    runner = WorkerRunner()
    await runner.add_workers(worker)
    runner_task = asyncio.create_task(runner.run())

    rc = 2
    samples: list[Sample] = []
    try:
        # Ждём ФАКТА стартового коннекта, а не «подождём полсекунды, наверное успел».
        # tts.start() поднимает WS внутри обработки StartFrame, и если первая реплика
        # уедет раньше — стартовый коннект попадёт внутрь её замера и первый switched
        # окажется завышен на всё подключение. Это не гипотеза: на прогоне с sleep(0.5)
        # раунд 1 стоил 2120 мс против 861 мс у раунда 2.
        deadline = time.perf_counter() + 20.0
        while probe.connects < 1 and time.perf_counter() < deadline:
            if probe.connect_errors:
                raise ProbeCannotRun(f"стартовый коннект не поднялся: {probe.connect_errors[0]}")
            await asyncio.sleep(0.05)
        if probe.connects < 1:
            raise ProbeCannotRun("стартовый коннект к Fish не поднялся за 20 с")
        print(f"  подключено к Fish, sample_rate={tts.sample_rate} Гц, коннектов={probe.connects}\n")
        samples = await probe.run(disp_voice, kora_voice)
        rc = 0 if report(samples, probe.connects) else 1
    except ProbeCannotRun as e:
        print(f"\nPROBE НЕ СМОГ ОТРАБОТАТЬ (это не вердикт платформе): {e}")
    except UtteranceLost as e:
        # Потерянная реплика — no-go-условие спеки («без потерь»), но на живой сети её
        # причина неотличима от обрыва. Печатаем факт и то, сколько успело набраться.
        print(f"\nРЕПЛИКА ПОТЕРЯНА после {len(samples)} успешных: {e}")
        print("  спека считает потерю реплики no-go-условием KV-1; на живой сети причина "
              "(платформа vs сеть) этим probe-ом не различается — перезапустить и сверить")
        rc = 1
    except Exception as e:  # probe, который упал, — это probe, который провалился
        print(f"\nPROBE НЕ СМОГ ОТРАБОТАТЬ (это не вердикт платформе): {type(e).__name__}: {e}")
    finally:
        if probe.connect_errors:
            print(f"\n  ошибки коннекта за прогон: {len(probe.connect_errors)} "
                  f"(первая: {probe.connect_errors[0]})")
        await worker.queue_frame(EndFrame())
        try:
            await asyncio.wait_for(runner_task, timeout=15.0)
        except (TimeoutError, asyncio.CancelledError):
            runner_task.cancel()
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
