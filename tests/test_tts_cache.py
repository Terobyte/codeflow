"""TTS-кэш ленты (tero 2026-07-14): ключ по содержимому, WAV-roundtrip, атомарность,
сборка из посентенсовых кусков и обсервер-стейт-машина (оба порядка Text/Stopped,
интеррапт-сброс, чужой source, не-пробрасывающее исключение). Фейки фреймов — реальные
pipecat-классы, FramePushed конструируется вручную."""
import io
import os
import wave
from pathlib import Path

import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import (
    InterruptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection

from synapse.pipeline.tts_cache import TTSCache, TTSCacheObserver


def _wav_frames(wav_bytes: bytes):
    w = wave.open(io.BytesIO(wav_bytes), "rb")
    return (w.getnchannels(), w.getsampwidth(), w.getframerate()), w.readframes(w.getnframes())


def _audio(pcm: bytes, sr: int = 16000, ch: int = 1) -> TTSAudioRawFrame:
    return TTSAudioRawFrame(audio=pcm, sample_rate=sr, num_channels=ch)


def _pushed(frame, source):
    return FramePushed(source=source, destination=object(), frame=frame,
                       direction=FrameDirection.DOWNSTREAM, timestamp=0)


# ---------- TTSCache ----------

def test_key_is_stable_and_strip_normalized(tmp_path):
    cache = TTSCache(tmp_path, "m", "v")
    assert cache.key("hi") == cache.key("  hi  ")
    assert cache.key("hi") != cache.key("bye")
    # ключ несёт model|voice — другой голос = другой ключ
    assert cache.key("hi") != TTSCache(tmp_path, "m", "OTHER").key("hi")


def test_put_pcm_get_roundtrip(tmp_path):
    cache = TTSCache(tmp_path, "m", "v")
    pcm = b"\x01\x02" * 100  # 100 фреймов, mono 16-bit
    cache.put_pcm("привет", pcm, 22050, 1)
    wav = cache.get("привет")
    assert wav is not None
    (nch, sw, fr), frames = _wav_frames(wav)
    assert (nch, sw, fr) == (1, 2, 22050)
    assert frames == pcm
    assert cache.get("нет такого") is None


def test_put_wav_idempotent_and_atomic(tmp_path):
    cache = TTSCache(tmp_path, "m", "v")
    cache.put_wav("t", b"FIRST")
    cache.put_wav("t", b"SECOND")  # идемпотентно: второй раз не переписывает
    assert cache.get("t") == b"FIRST"
    # никаких *.tmp-хвостов (os.listdir видит и dot-файлы)
    assert [f for f in os.listdir(cache.root) if f.endswith(".tmp")] == []


def test_assemble_happy_concatenates_and_writes_full_key(tmp_path):
    cache = TTSCache(tmp_path, "m", "v")
    cache.put_pcm("A", b"\xaa\xaa", 16000, 1)
    cache.put_pcm("B", b"\xbb\xbb", 16000, 1)
    out = cache.assemble("AB", lambda t: ["A", "B"])
    assert out is not None
    (params, frames) = _wav_frames(out)
    assert params == (1, 2, 16000)
    assert frames == b"\xaa\xaa\xbb\xbb"
    # полный ключ записан — повторный get отдаёт кэш
    assert cache.get("AB") == out


def test_assemble_partial_miss_returns_none(tmp_path):
    cache = TTSCache(tmp_path, "m", "v")
    cache.put_pcm("A", b"\xaa\xaa", 16000, 1)  # "B" отсутствует
    assert cache.assemble("AB", lambda t: ["A", "B"]) is None


def test_assemble_mixed_sample_rate_returns_none(tmp_path):
    cache = TTSCache(tmp_path, "m", "v")
    cache.put_pcm("A", b"\xaa\xaa", 16000, 1)
    cache.put_pcm("B", b"\xbb\xbb", 22050, 1)
    assert cache.assemble("AB", lambda t: ["A", "B"]) is None


def test_assemble_single_sentence_returns_none(tmp_path):
    cache = TTSCache(tmp_path, "m", "v")
    cache.put_pcm("A", b"\xaa\xaa", 16000, 1)
    assert cache.assemble("A", lambda t: ["A"]) is None


def test_speak_text_cache_roundtrip_and_key_ignores_model_voice(tmp_path):
    cache = TTSCache(tmp_path, "m", "v")
    assert cache.get_speak_text("код app.py:1024") is None
    cache.put_speak_text("код app.py:1024", "готово")
    assert cache.get_speak_text("код app.py:1024") == "готово"
    # ключ санитайза — по исходному тексту, без model|voice
    assert TTSCache(tmp_path, "OTHER", "OTHER").get_speak_text("код app.py:1024") == "готово"


# ---------- TTSCacheObserver ----------

async def test_observer_text_before_stopped_finalizes(tmp_path):
    cache = TTSCache(tmp_path, "m", "v")
    tts = object()
    obs = TTSCacheObserver(cache, tts)
    for f in (TTSStartedFrame(), _audio(b"\x01\x02"), _audio(b"\x03\x04"),
              TTSTextFrame("привет", aggregated_by="sentence"), TTSStoppedFrame()):
        await obs.on_push_frame(_pushed(f, tts))
    wav = cache.get("привет")
    assert wav is not None
    _params, frames = _wav_frames(wav)
    assert frames == b"\x01\x02\x03\x04"


async def test_observer_late_text_after_stopped_finalizes(tmp_path):
    cache = TTSCache(tmp_path, "m", "v")
    tts = object()
    obs = TTSCacheObserver(cache, tts)
    # текст приходит ПОСЛЕ Stopped (word-timestamps порядок)
    for f in (TTSStartedFrame(), _audio(b"\x05\x06"), TTSStoppedFrame(),
              TTSTextFrame("поздний", aggregated_by="sentence")):
        await obs.on_push_frame(_pushed(f, tts))
    wav = cache.get("поздний")
    assert wav is not None
    _params, frames = _wav_frames(wav)
    assert frames == b"\x05\x06"


async def test_observer_interruption_resets_open_run(tmp_path):
    cache = TTSCache(tmp_path, "m", "v")
    tts = object()
    obs = TTSCacheObserver(cache, tts)
    for f in (TTSStartedFrame(), _audio(b"\x01\x02"),
              TTSTextFrame("прерван", aggregated_by="sentence"),
              InterruptionFrame(), TTSStoppedFrame()):
        await obs.on_push_frame(_pushed(f, tts))
    # интеррапт сбросил open — Stopped уже не финализирует
    assert cache.get("прерван") is None


async def test_observer_new_started_discards_textless_pending(tmp_path):
    cache = TTSCache(tmp_path, "m", "v")
    tts = object()
    obs = TTSCacheObserver(cache, tts)
    # первый прогон без текста → pending; новый Started обязан его выбросить
    for f in (TTSStartedFrame(), _audio(b"\x01\x02"), TTSStoppedFrame()):
        await obs.on_push_frame(_pushed(f, tts))
    assert obs._pending is not None
    await obs.on_push_frame(_pushed(TTSStartedFrame(), tts))
    assert obs._pending is None and obs._open is not None


async def test_observer_ignores_foreign_source(tmp_path):
    cache = TTSCache(tmp_path, "m", "v")
    tts = object()
    other = object()
    obs = TTSCacheObserver(cache, tts)
    await obs.on_push_frame(_pushed(TTSStartedFrame(), tts))
    await obs.on_push_frame(_pushed(_audio(b"\xff\xff"), other))  # чужой source — игнор
    await obs.on_push_frame(_pushed(_audio(b"\x07\x08"), tts))
    await obs.on_push_frame(_pushed(TTSTextFrame("свой", aggregated_by="sentence"), tts))
    await obs.on_push_frame(_pushed(TTSStoppedFrame(), tts))
    _params, frames = _wav_frames(cache.get("свой"))
    assert frames == b"\x07\x08"  # чужой \xff\xff не попал в кэш


async def test_observer_never_propagates_exception(tmp_path):
    class _FailingCache:
        root = Path(tmp_path)
        def wav_path(self, text):
            return self.root / "missing.wav"  # .exists() == False → пойдём в put_pcm
        def put_pcm(self, *a, **k):
            raise RuntimeError("disk full")

    tts = object()
    obs = TTSCacheObserver(_FailingCache(), tts)
    # полный финализирующий прогон — put_pcm упадёт, но on_push_frame НЕ пробрасывает (R-1)
    for f in (TTSStartedFrame(), _audio(b"\x01\x02"),
              TTSTextFrame("x", aggregated_by="sentence"), TTSStoppedFrame()):
        await obs.on_push_frame(_pushed(f, tts))  # не должно кинуть
