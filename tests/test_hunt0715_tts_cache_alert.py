# -*- coding: utf-8 -*-
"""Багхант 2026-07-15, B-PIPE-4 — `TTSCacheObserver` (`synapse/pipeline/tts_cache.py`).

`on_push_frame` заворачивает всё в широкий `except Exception` и просто логирует (R-1:
обсервер сидит в живом push-пути пайплайна и никогда не может пробросить исключение —
это НЕ баг, это инвариант). Баг в том, что при УСТОЙЧИВОМ сбое записи в кэш (диск полон,
права) видимости нет вообще никакой: только logger, в журнал (§8-евиденс) не попадает
ничего, и каждый следующий Play молча платит дорогим REST-ресинтезом.

Желаемый (ещё не реализованный) дизайн: `TTSCacheObserver(cache, tts_source, journal=None)`
— опциональный журнал, на сбой шлёт `journal.alert(AlertKind.TTS_CACHE_DEGRADED, {...})`
РОВНО ОДИН раз за серию сбоев (анти-спам — обсервер вызывается на каждый аудио-фрейм),
не пробрасывая исключение, и при `journal=None` ведёт себя как сегодня.

Антипример (НЕ повторять): `tests/test_reported_bugs_failing.py::
test_b_pipe_4_tts_cache_observer_swallows_cache_write_failures` требует
`pytest.raises(OSError)` — это требование нарушило бы сам R-1 (уронило бы живой звук).
Страж R-1 заморожен в `tests/test_tts_cache.py::test_observer_never_propagates_exception`
— этот файл его не трогает, только зеркалит рядом (тест 3 ниже), чтобы держать связку
"фикс видимости + фикс не должен сломать R-1" в одном месте.

`TTSCacheObserver.__init__` СЕГОДНЯ не принимает `journal=` (конструктор — `(cache,
tts_source)`), поэтому конструирование с этим kwarg заранее обёрнуто в try/except
TypeError с фолбэком на старый 2-арный конструктор — так тест краснеет на содержательном
ASSERT'е (алерта не было), а не в собственном сетапе.
"""
import io
import wave
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import (
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection

from synapse.journal import AlertKind
from synapse.pipeline.tts_cache import TTSCacheObserver


class _FailingCache:
    """Кэш, у которого запись УСТОЙЧИВО падает (диск полон / права) — put_pcm/put_wav
    всегда бросают OSError. wav_path всегда указывает на несуществующий файл, так что
    _finalize каждый раз реально пытается писать (а не молча скипает как «уже в кэше»)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def wav_path(self, text: str, voice_id: str | None = None) -> Path:
        return self.root / "never-written.wav"

    def put_pcm(self, *a, **k):
        raise OSError("No space left on device")

    def put_wav(self, *a, **k):
        raise OSError("No space left on device")


def _audio(pcm: bytes, sr: int = 16000, ch: int = 1) -> TTSAudioRawFrame:
    return TTSAudioRawFrame(audio=pcm, sample_rate=sr, num_channels=ch)


def _pushed(frame, source):
    return FramePushed(source=source, destination=object(), frame=frame,
                       direction=FrameDirection.DOWNSTREAM, timestamp=0)


def _make_observer(cache, tts, journal=None):
    # Конструктор ещё не знает про journal= (это и есть суть баг-репорта) — фолбэк не даёт
    # тесту упасть в сетапе TypeError'ом, а честно доводит его до содержательного assert.
    try:
        return TTSCacheObserver(cache, tts, journal=journal)
    except TypeError:
        return TTSCacheObserver(cache, tts)


async def _run_full_cycle(obs: TTSCacheObserver, tts, text: str, pcm: bytes = b"\x01\x02") -> None:
    for f in (TTSStartedFrame(), _audio(pcm),
              TTSTextFrame(text, aggregated_by="sentence"), TTSStoppedFrame()):
        await obs.on_push_frame(_pushed(f, tts))


def _alert_kinds(journal_mock: MagicMock) -> list:
    """Достаёт `kind` из каждого вызова journal.alert(kind, detail=...), не привязываясь
    к тому, позиционный он или именованный — сигнатура фикса ещё не написана."""
    kinds = []
    for call in journal_mock.alert.call_args_list:
        args, kwargs = call
        if args:
            kinds.append(args[0])
        elif "kind" in kwargs:
            kinds.append(kwargs["kind"])
    return kinds


async def test_b_pipe_4_cache_write_failure_alerts_the_journal(tmp_path):
    cache = _FailingCache(tmp_path)
    tts = object()
    journal = MagicMock()
    obs = _make_observer(cache, tts, journal=journal)

    await _run_full_cycle(obs, tts, "привет")

    # Сегодня journal никуда не подключён (конструктор его не принимает) → алертов ноль.
    # Это и есть баг: устойчивый сбой кэша не оставляет ни одного следа в §8-евиденсе.
    assert AlertKind.TTS_CACHE_DEGRADED in _alert_kinds(journal)


async def test_b_pipe_4_repeated_failures_alert_only_once(tmp_path):
    cache = _FailingCache(tmp_path)
    tts = object()
    journal = MagicMock()
    obs = _make_observer(cache, tts, journal=journal)

    # Несколько полных прогонов синтеза подряд — кэш падает на каждом. Анти-спам: обсервер
    # сидит в аудио-пути, алерт на каждый фрейм/прогон = флуд журнала.
    for i, text in enumerate(["один", "два", "три", "четыре"]):
        await _run_full_cycle(obs, tts, text, pcm=bytes([i, i]))

    degraded = [k for k in _alert_kinds(journal) if k == AlertKind.TTS_CACHE_DEGRADED]
    assert len(degraded) == 1


async def test_b_pipe_4_observer_still_never_propagates(tmp_path):
    """Страж R-1 рядом с фиксом видимости: даже с подключённым (или пока не подключённым)
    журналом устойчивый сбой кэша НЕ должен ронять on_push_frame — это тот самый инвариант,
    который антипример-тест в test_reported_bugs_failing.py пытался (неправильно) сломать.
    Ожидаемо ЗЕЛЁНЫЙ уже сегодня: R-1 уже реализован (широкий except в on_push_frame);
    он тут как анти-регрессия — чтобы будущий фикс видимости (добавление journal.alert)
    случайно не начал пробрасывать исключение из-под try/except."""
    cache = _FailingCache(tmp_path)
    tts = object()
    journal = MagicMock()
    obs = _make_observer(cache, tts, journal=journal)

    for text in ("а", "бэ", "цэ"):
        await _run_full_cycle(obs, tts, text)  # не должно кинуть ни на одном фрейме
