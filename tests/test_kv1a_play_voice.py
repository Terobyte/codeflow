"""KV-1a §4.1 «Play-кнопка перестаёт врать»: роль disp|kora на POST /api/tts выбирает
reference id, и он идёт во ВСЕ ступени (кэш → сборка → синтез → put), а не только в
fish_rest_tts. Половина KV-1 про живой тракт (voice-aware арбитр) НЕ строится: probe P3b
дал NO-GO (свитч голоса в живом WS = реконнект, +707…+754 мс парной дельты). REST-путь
Play свитча не платит — голос там просто параметр.

Тесты проверяют наблюдаемое поведение: какой voice долетел до синтеза, какое аудио вернул
роут, разъехались ли ключи кэша. Паттерн монкипатча/CSRF — как в test_api_tts_diff.py.
"""
from __future__ import annotations

import hashlib
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

DISP_ID = "voice-disp"
KORA_ID = "voice-kora"


def _wav(pcm: bytes, sr: int = 16000, ch: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


class _TTSHost:
    """Реальные TTSCache + cfg, без Kora/пайплайна (как _TTSHost в test_api_tts_diff)."""

    def __init__(self, tmp_path, kora_voice: str | None = KORA_ID):
        self.cfg = SynapseConfig(
            fish_audio_api_key="fish-k",
            fish_reference_id=DISP_ID,
            kora_fish_reference_id=kora_voice,
        )
        self.tts_cache = TTSCache(tmp_path / "cache", self.cfg.fish_tts_model, DISP_ID)


def _recording_synth(monkeypatch, audio_by_voice: dict[str, bytes] | None = None):
    """Подменяет fish_rest_tts, записывая voice каждого вызова. Возвращает разное аудио на
    разные голоса — так тест ловит не «параметр прокинут», а «вернулся чужой звук»."""
    seen: list[dict] = []

    async def _fake(text, **k):
        seen.append({"text": text, "voice": k.get("voice")})
        if audio_by_voice is not None:
            return audio_by_voice[k["voice"]]
        return _wav(b"\x01\x02")

    monkeypatch.setattr(webrtc_server, "fish_rest_tts", _fake)
    return seen


def _client(host):
    return TestClient(build_web_app(host), raise_server_exceptions=False)


# --------------------------- роль как контракт роута ---------------------------

def test_unknown_role_is_400_and_synthesizes_nothing(tmp_path, monkeypatch):
    """Раньше любая строка молча становилась disp. §4.1: неизвестная → 400."""
    seen = _recording_synth(monkeypatch)
    resp = _client(_TTSHost(tmp_path)).post(
        "/api/tts", json={"text": "Привет.", "role": "narrator"}, headers=_CSRF
    )
    assert resp.status_code == 400
    assert seen == []  # ничего не синтезировали


def test_missing_role_still_defaults_to_disp(tmp_path, monkeypatch):
    """Сегодняшнее поведение, на которое опирается клиент: роли нет → голос диспетчера."""
    seen = _recording_synth(monkeypatch)
    resp = _client(_TTSHost(tmp_path)).post(
        "/api/tts", json={"text": "Привет."}, headers=_CSRF
    )
    assert resp.status_code == 200
    assert [s["voice"] for s in seen] == [DISP_ID]


def test_role_kora_synthesizes_with_koras_voice(tmp_path, monkeypatch):
    """Ядро бага: подпись «TTS · Code voice» обязана звучать голосом Коры."""
    seen = _recording_synth(monkeypatch)
    resp = _client(_TTSHost(tmp_path)).post(
        "/api/tts", json={"text": "Готово.", "role": "kora"}, headers=_CSRF
    )
    assert resp.status_code == 200
    assert [s["voice"] for s in seen] == [KORA_ID]


def test_role_disp_unaffected(tmp_path, monkeypatch):
    seen = _recording_synth(monkeypatch)
    host = _TTSHost(tmp_path)
    client = _client(host)
    r1 = client.post("/api/tts", json={"text": "Одно предложение.", "role": "disp"},
                     headers=_CSRF)
    assert r1.status_code == 200
    assert r1.headers["x-tts-source"] == "synth"
    assert [s["voice"] for s in seen] == [DISP_ID]
    # повтор — из кэша, синтез не зовётся (дисп-путь не потерял кэш)
    r2 = client.post("/api/tts", json={"text": "Одно предложение.", "role": "disp"},
                     headers=_CSRF)
    assert r2.headers["x-tts-source"] == "cache"
    assert len(seen) == 1
    assert r2.content == r1.content


# --------------------------- ловушка: коллизия ключей ---------------------------

def test_two_voices_same_text_do_not_collide_in_cache(tmp_path, monkeypatch):
    """ЛОВУШКА KV-1a. Прокинуть voice только в fish_rest_tts мало: аудио Коры легло бы под
    дисп-ключ, и следующий дисп-Play того же текста вернул бы голос Коры. Тест красный
    ровно на этой ошибке — сверяется САМ ЗВУК, не параметр."""
    disp_wav, kora_wav = _wav(b"\xaa\xaa"), _wav(b"\xbb\xbb")
    seen = _recording_synth(monkeypatch, {DISP_ID: disp_wav, KORA_ID: kora_wav})
    host = _TTSHost(tmp_path)
    client = _client(host)
    text = "Один и тот же текст."

    r_kora = client.post("/api/tts", json={"text": text, "role": "kora"}, headers=_CSRF)
    assert r_kora.status_code == 200
    assert r_kora.content == kora_wav

    r_disp = client.post("/api/tts", json={"text": text, "role": "disp"}, headers=_CSRF)
    assert r_disp.status_code == 200
    assert r_disp.content == disp_wav  # НЕ голос Коры из чужого кэша
    assert r_disp.headers["x-tts-source"] == "synth"  # промах кэша, а не чужой хит
    assert [s["voice"] for s in seen] == [KORA_ID, DISP_ID]  # оба синтеза реально были

    # и каждый голос переиграл из СВОЕЙ записи
    assert client.post("/api/tts", json={"text": text, "role": "kora"},
                       headers=_CSRF).content == kora_wav
    assert client.post("/api/tts", json={"text": text, "role": "disp"},
                       headers=_CSRF).content == disp_wav
    assert len(seen) == 2  # больше синтезов не понадобилось


def test_unset_kora_voice_shares_the_dispatcher_cache_key(tmp_path, monkeypatch):
    """§4.1: незаданный id Коры резолвится в дисп-id ДО обращения к кэшу, поэтому
    одинаковый реальный звук закономерно делит один ключ (а не синтезируется дважды)."""
    seen = _recording_synth(monkeypatch, {DISP_ID: _wav(b"\xcc\xcc")})
    host = _TTSHost(tmp_path, kora_voice=None)
    client = _client(host)
    text = "Общая фраза."

    r_disp = client.post("/api/tts", json={"text": text, "role": "disp"}, headers=_CSRF)
    assert r_disp.headers["x-tts-source"] == "synth"
    r_kora = client.post("/api/tts", json={"text": text, "role": "kora"}, headers=_CSRF)
    assert r_kora.headers["x-tts-source"] == "cache"  # тот же ключ
    assert r_kora.content == r_disp.content
    assert [s["voice"] for s in seen] == [DISP_ID]  # второго синтеза не было


# --------------------------- кэш: voice-aware + обратная совместимость ---------------------------

def test_default_voice_id_key_is_byte_identical_to_pre_change(tmp_path):
    """Обратная совместимость несущая: дефолт обязан дать ТОТ ЖЕ ключ, что до KV-1a, иначе
    осиротеет весь кэш на диске и случится массовый ре-синтез. Ключ считается тем же
    способом, что и в старом коде: sha256(model|voice|text.strip())[:40]."""
    cache = TTSCache(tmp_path, "m", "v")
    old = hashlib.sha256("m|v|привет".encode("utf-8")).hexdigest()[:40]
    assert cache.key("привет") == old
    assert cache.key("  привет  ") == old  # strip как раньше
    assert cache.key("привет", None) == old
    assert cache.key("привет", "v") == old  # явный конструкторный голос = дефолт
    assert cache.wav_path("привет").name == f"{old}.wav"


def test_cache_roundtrip_is_voice_scoped(tmp_path):
    cache = TTSCache(tmp_path, "m", DISP_ID)
    cache.put_wav("t", b"DISP-AUDIO")
    cache.put_wav("t", b"KORA-AUDIO", KORA_ID)
    assert cache.get("t") == b"DISP-AUDIO"
    assert cache.get("t", KORA_ID) == b"KORA-AUDIO"
    assert cache.key("t") != cache.key("t", KORA_ID)


def test_assemble_is_voice_scoped(tmp_path):
    """Сборка из посентенсовых кусков не должна склеивать чужой голос: куски Коры не
    собираются в дисп-Play и наоборот."""
    cache = TTSCache(tmp_path, "m", DISP_ID)
    cache.put_pcm("A", b"\xaa\xaa", 16000, 1, KORA_ID)
    cache.put_pcm("B", b"\xbb\xbb", 16000, 1, KORA_ID)
    split = lambda t: ["A", "B"]

    assert cache.assemble("AB", split) is None  # дисп-голосом кусков нет → честный промах
    out = cache.assemble("AB", split, KORA_ID)
    assert out is not None
    with wave.open(io.BytesIO(out), "rb") as w:
        assert w.readframes(w.getnframes()) == b"\xaa\xaa\xbb\xbb"
    # результат осел под ключ Коры, а не диспетчера
    assert cache.get("AB", KORA_ID) == out
    assert cache.get("AB") is None


def test_put_pcm_defaults_to_constructor_voice(tmp_path):
    """Обсервер живого тракта зовёт put_pcm позиционно и без voice_id — дефолт обязан
    остаться конструкторным голосом (KV-1a не трогает живой тракт)."""
    cache = TTSCache(tmp_path, "m", DISP_ID)
    cache.put_pcm("t", b"\x01\x02", 16000, 1)
    assert cache.get("t") is not None
    assert cache.get("t", KORA_ID) is None


# --------------------------- конфиг ---------------------------

def test_kora_reference_id_from_env():
    cfg = SynapseConfig.from_env({"KORA_FISH_REFERENCE_ID": "k-1"})
    assert cfg.kora_fish_reference_id == "k-1"


def test_unset_kora_reference_id_is_none_and_not_a_required_key():
    cfg = SynapseConfig.from_env({})
    assert cfg.kora_fish_reference_id is None
    # id голоса — не секрет и опционален: отсутствие не валит voice-пайплайн из-за него
    cfg2 = SynapseConfig(
        openrouter_api_key="a", anthropic_api_key="b", deepgram_api_key="c",
        fish_audio_api_key="d", fish_reference_id="e",
    )
    cfg2.validate_voice_keys()  # не кидает, хотя kora_fish_reference_id не задан


class _ProdShapedHost:
    """Хост, собранный ТОЧНО как прод (app.py:906): кэш конструируется выражением
    `cfg.fish_reference_id or ""`. Именно это выражение роут обязан повторить для disp."""

    def __init__(self, tmp_path, disp_voice: str | None):
        self.cfg = SynapseConfig(fish_audio_api_key="fish-k", fish_reference_id=disp_voice)
        self.tts_cache = TTSCache(
            tmp_path / "cache", self.cfg.fish_tts_model, self.cfg.fish_reference_id or ""
        )


@pytest.mark.parametrize("disp_voice", [None, "", DISP_ID])
def test_play_finds_wav_that_the_live_tract_cached_so_disk_cache_never_orphans(
    tmp_path, monkeypatch, disp_voice
):
    """Play обязан найти WAV, который положил в кэш ЖИВОЙ тракт. Это и есть тест на осиротение.

    Живой путь (`TTSCacheObserver._finalize`) пишет позиционно, без voice_id → ключ считается
    конструкторным голосом (`app.py:906`: `cfg.fish_reference_id or ""`). Роут после KV-1a
    считает ключ САМ, из роли. Разъедься эти два выражения — весь накопленный на диске кэш
    диспетчера станет недостижим и пересинтезируется за реальные деньги.

    Судья доказал, что дыра была открыта: он сломал `or ""` в резолве роута, и ни один тест
    не покраснел — все фикстуры задавали fish_reference_id, а незаданный тут и опасен.

    Почему именно так, а не «два Play подряд»: два Play сравнивали бы роут сам с собой —
    оба промахнулись бы одинаково и одинаково же попали во второй раз, а тест бы позеленел
    на разъехавшемся ключе. Писать обязана СТОРОНА КОНСТРУКТОРА, читать — роут.
    """
    seen = _recording_synth(monkeypatch)
    host = _ProdShapedHost(tmp_path, disp_voice)
    text = "Фраза диспетчера."
    live_wav = _wav(b"\xdd\xdd")
    host.tts_cache.put_wav(text, live_wav)  # позиционно — ровно как _finalize живого тракта

    resp = _client(host).post("/api/tts", json={"text": text, "role": "disp"}, headers=_CSRF)

    assert resp.status_code == 200
    assert resp.headers["x-tts-source"] == "cache", (
        "Play пересинтезировал текст, уже лежащий в кэше: ключ роута разошёлся с ключом "
        "конструктора — на диске это осиротевший кэш и повторная оплата синтеза"
    )
    assert resp.content == live_wav
    assert seen == [], "синтез позван при живом кэш-хите"
