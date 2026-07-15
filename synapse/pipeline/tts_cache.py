"""TTS-кэш ленты (tero run 2026-07-14): реалтайм-озвученное аудио персистится на диск, а Play
в UI отдаёт кэш либо синтезирует on-demand. Точка тапа в пайплайне — TTSCacheObserver поверх
pipecat BaseObserver.on_push_frame; off-pipeline REST-синтез (для того, чего в кэше нет) —
fish_rest_tts.

У записей ленты нет стабильного id, поэтому ключуем по содержимому:
sha256(model|voice|text.strip())[:40]. Формат на диске — WAV (sampwidth=2), общий для
телефона/десктопа (кэш на сервере, не в браузере).
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import uuid
import wave
from pathlib import Path
from typing import Callable

import httpx
from pipecat.frames.frames import (
    InterruptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

logger = logging.getLogger(__name__)

_FISH_REST_URL = "https://api.fish.audio/v1/tts"


class TTSCache:
    """Файловый кэш озвученного текста. Ключ — по содержимому (у feed-записей нет id).
    Реалтайм-пайплайн пишет посентенсовые WAV (арбитр озвучивает по предложениям); Play
    полного текста собирает их через assemble либо синтезирует on-demand."""

    def __init__(self, root: Path, model: str, voice: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._model = model
        self._voice = voice

    def key(self, text: str) -> str:
        raw = f"{self._model}|{self._voice}|{text.strip()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]

    def wav_path(self, text: str) -> Path:
        return self.root / f"{self.key(text)}.wav"

    def get(self, text: str) -> bytes | None:
        p = self.wav_path(text)
        return p.read_bytes() if p.exists() else None

    def _atomic_write(self, path: Path, data: bytes) -> None:
        # R-2: уникальное tmp-имя в той же директории + os.replace — две сессии на одном
        # ключе не перезапишут чужой tmp и не оставят половинный replace.
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def put_wav(self, text: str, wav: bytes) -> None:
        p = self.wav_path(text)
        if p.exists():
            return  # идемпотентно: реалтайм-повтор той же фразы не переписывает файл
        self._atomic_write(p, wav)

    def put_pcm(self, text: str, pcm: bytes, sample_rate: int, num_channels: int) -> None:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(num_channels)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(pcm)
        self.put_wav(text, buf.getvalue())

    def assemble(self, text: str, splitter: Callable[[str], list[str]]) -> bytes | None:
        """Сборка полного WAV из посентенсовых кусков в кэше (тот же splitter, что арбитр,
        инвариант join==text). Любой промах / разнобой в (nchannels,sampwidth,framerate) →
        None (честный fallback на REST-синтез). Один сегмент → None (нечего собирать)."""
        sentences = splitter(text)
        if len(sentences) < 2:
            return None
        params: tuple[int, int, int] | None = None
        chunks: list[bytes] = []
        for s in sentences:
            p = self.wav_path(s)
            if not p.exists():
                return None
            with wave.open(str(p), "rb") as w:
                pr = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                if params is None:
                    params = pr
                elif pr != params:
                    return None
                chunks.append(w.readframes(w.getnframes()))
        if params is None:
            return None
        nchannels, sampwidth, framerate = params
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(nchannels)
            w.setsampwidth(sampwidth)
            w.setframerate(framerate)
            w.writeframes(b"".join(chunks))
        result = buf.getvalue()
        self.put_wav(text, result)
        return result

    # --- санитайз-кэш: ключ по ИСХОДНОМУ тексту (без model|voice), иначе недетерминизм
    #     Gemini на каждый Play ломал бы посентенсовый WAV-ключ ниже по потоку ---
    def _speak_key(self, text: str) -> str:
        return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:40]

    def speak_text_path(self, text: str) -> Path:
        return self.root / f"{self._speak_key(text)}.speak.txt"

    def get_speak_text(self, text: str) -> str | None:
        p = self.speak_text_path(text)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def put_speak_text(self, text: str, spoken: str) -> None:
        p = self.speak_text_path(text)
        if p.exists():
            return  # детерминизм: первый санитайз фиксируется, повтор его не мутирует
        self._atomic_write(p, spoken.encode("utf-8"))


class TTSCacheObserver(BaseObserver):
    """Тап на выходе TTS: собирает PCM одного прогона синтеза (Started→Audio*→Text→Stopped)
    и пишет WAV в кэш под ключ прозвученного текста. Порядок TTSTextFrame относительно
    TTSStoppedFrame у Fish не гарантирован (word-timestamps), поэтому стейт-машина
    толерантна к обоим: поздний текст финализирует «pending»-прогон."""

    def __init__(self, cache: TTSCache, tts_source) -> None:
        self._cache = cache
        self._tts = tts_source
        self._open: dict | None = None      # текущий открытый прогон
        self._pending: dict | None = None   # аудио собрано, ждём поздний TTSTextFrame

    async def on_push_frame(self, data: FramePushed) -> None:
        # R-1: обсервер НИКОГДА не пробрасывает — исключение здесь летит в push-путь
        # пайплайна и могло бы уронить живое аудио.
        try:
            await self._handle(data)
        except Exception:
            logger.exception("TTSCacheObserver.on_push_frame failed; ignoring")

    async def _handle(self, data: FramePushed) -> None:
        frame = data.frame
        # Прерывание (любой source) сбрасывает всё — недособранный прогон это мусор.
        if isinstance(frame, InterruptionFrame):
            self._open = None
            self._pending = None
            return
        if data.source is not self._tts:
            return
        if isinstance(frame, TTSStartedFrame):
            self._pending = None  # новый прогон: pending-без-текста больше не финализируется
            self._open = {"audio": [], "text": None, "sr": None, "ch": None}
        elif isinstance(frame, TTSAudioRawFrame):
            if self._open is not None:
                self._open["audio"].append(frame.audio)
                self._open["sr"] = frame.sample_rate
                self._open["ch"] = frame.num_channels
        elif isinstance(frame, TTSTextFrame):
            if self._open is not None:
                self._open["text"] = frame.text
            elif self._pending is not None:
                run = self._pending
                run["text"] = frame.text
                self._pending = None
                await self._finalize(run)
        elif isinstance(frame, TTSStoppedFrame):
            if self._open is not None:
                run = self._open
                self._open = None
                if run["text"]:
                    await self._finalize(run)
                else:
                    self._pending = run  # текст ещё не пришёл — ждём поздний TTSTextFrame

    async def _finalize(self, run: dict) -> None:
        audio, text = run["audio"], run["text"]
        if not audio or not text:
            return
        if self._cache.wav_path(text).exists():
            return  # уже в кэше — не пишем повторно
        await asyncio.to_thread(
            self._cache.put_pcm, text, b"".join(audio), run["sr"], run["ch"]
        )


async def fish_rest_tts(
    text: str,
    *,
    api_key: str,
    model: str,
    voice: str,
    timeout_s: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    """Off-pipeline REST-синтез Fish Audio (POST /v1/tts) — путь Play для текста вне кэша.
    Паттерн httpx как в AnthropicLLMClient (transport-DI для тестов). non-2xx → RuntimeError."""
    headers = {
        "authorization": f"Bearer {api_key}",
        "model": model,
        "content-type": "application/json",
    }
    payload = {"text": text, "reference_id": voice, "format": "wav"}
    async with httpx.AsyncClient(transport=transport, timeout=timeout_s) as client:
        resp = await client.post(_FISH_REST_URL, json=payload, headers=headers)
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"fish REST TTS failed: HTTP {resp.status_code}")
        return resp.content
