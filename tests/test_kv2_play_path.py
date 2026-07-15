"""KV-2 Play-путь (спека §4.2): POST /api/tts с role=kora пускает разговорный текст в TTS
КАК ЕСТЬ — мимо speakify (минус платный Gemini-вызов и его задержка), а неразговорный гонит
через speakify ровно как раньше. Хост/фикстуры — паттерн test_api_tts_diff.py (тройной
importorskip, speakify/fish_rest_tts монкипатчатся как модульные символы webrtc_server).

⚠️ speakable() здесь — ФОРМАТНЫЙ фильтр (§4.2 запрещает называть его фильтром безопасного
содержания): решается только «звучит как речь или как зачитанный markdown».
"""
from __future__ import annotations

import io
import wave

import pytest

pytest.importorskip("aiortc")
pytest.importorskip("cv2")
pytest.importorskip("fastapi")

from starlette.testclient import TestClient

from synapse.config import SynapseConfig
from synapse.pipeline import webrtc_server
from synapse.pipeline.tts_cache import TTSCache
from synapse.pipeline.webrtc_server import build_web_app

_CSRF = {"content-type": "application/json", "origin": "http://testserver"}

CLEAN = "Готово, задача выполнена, все тесты зелёные."
DIRTY = "готово: правил app.py:1024, тесты зелёные"


def _wav(pcm: bytes = b"\x01\x02\x03\x04") -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return buf.getvalue()


class _TTSHost:
    def __init__(self, tmp_path, **cfg_kw):
        self.cfg = SynapseConfig(
            fish_audio_api_key="fish-k", fish_reference_id="voice-1",
            google_api_key="g-key", **cfg_kw,
        )
        self.tts_cache = TTSCache(tmp_path / "cache", self.cfg.fish_tts_model,
                                  self.cfg.fish_reference_id or "")


def _client(host):
    return TestClient(build_web_app(host), raise_server_exceptions=False)


@pytest.fixture
def spies(monkeypatch):
    seen = {"speakify": 0, "synth_text": None}

    async def _fake_speakify(text, **k):
        seen["speakify"] += 1
        return "переписанный текст"

    async def _fake_synth(text, **k):
        seen["synth_text"] = text
        return _wav()

    monkeypatch.setattr(webrtc_server, "speakify", _fake_speakify)
    monkeypatch.setattr(webrtc_server, "fish_rest_tts", _fake_synth)
    return seen


def test_clean_kora_text_skips_speakify_and_synthesizes_raw(tmp_path, spies):
    """Сердце слайса: разговорная реплика Коры не платит за Gemini и звучит дословно."""
    resp = _client(_TTSHost(tmp_path)).post(
        "/api/tts", json={"text": CLEAN, "role": "kora"}, headers=_CSRF)
    assert resp.status_code == 200
    assert spies["speakify"] == 0            # платный вызов не сделан
    assert spies["synth_text"] == CLEAN      # в TTS ушёл исходный текст, не пересказ


def test_clean_kora_text_does_not_write_speak_text_cache(tmp_path, spies):
    """Переписывать было нечего — .speak.txt не появляется (кэш не врёт про санитайз)."""
    host = _TTSHost(tmp_path)
    _client(host).post("/api/tts", json={"text": CLEAN, "role": "kora"}, headers=_CSRF)
    assert host.tts_cache.get_speak_text(CLEAN) is None
    assert not host.tts_cache.speak_text_path(CLEAN).exists()


def test_dirty_kora_text_still_runs_speakify(tmp_path, spies):
    """Регрессия на старое поведение: грязный текст идёт через speakify как сегодня."""
    resp = _client(_TTSHost(tmp_path)).post(
        "/api/tts", json={"text": DIRTY, "role": "kora"}, headers=_CSRF)
    assert resp.status_code == 200
    assert spies["speakify"] == 1
    assert spies["synth_text"] == "переписанный текст"


def test_dispatcher_role_never_consults_the_filter(tmp_path, spies):
    """role=disp не меняется: речь диспетчера и так разговорная, фильтр к ней не применяется —
    даже если бы она была «грязной» по формату."""
    resp = _client(_TTSHost(tmp_path)).post(
        "/api/tts", json={"text": DIRTY, "role": "disp"}, headers=_CSRF)
    assert resp.status_code == 200
    assert spies["speakify"] == 0
    assert spies["synth_text"] == DIRTY


def test_cap_comes_from_config_not_a_constant(tmp_path, spies):
    """kora_speak_max_chars реально доезжает из конфига в роут: с капом в 5 символов
    та же разговорная реплика становится «неразговорной» и уходит в speakify."""
    host = _TTSHost(tmp_path, kora_speak_max_chars=5)
    resp = _client(host).post("/api/tts", json={"text": CLEAN, "role": "kora"}, headers=_CSRF)
    assert resp.status_code == 200
    assert spies["speakify"] == 1


def test_clean_kora_text_caches_wav_under_its_own_text(tmp_path, spies):
    """Второй Play того же чистого текста берёт WAV из кэша: ключ — сам текст, без speakify."""
    host = _TTSHost(tmp_path)
    client = _client(host)
    r1 = client.post("/api/tts", json={"text": CLEAN, "role": "kora"}, headers=_CSRF)
    assert r1.headers["x-tts-source"] == "synth"
    r2 = client.post("/api/tts", json={"text": CLEAN, "role": "kora"}, headers=_CSRF)
    assert r2.headers["x-tts-source"] == "cache"
    assert r2.content == r1.content
    assert spies["speakify"] == 0
