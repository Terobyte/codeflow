"""Роуты Play-озвучки и Diff (tero 2026-07-14): POST /api/tts и GET
/api/threads/{id}/diff, поверх build_web_app через starlette.testclient.TestClient
(паттерн test_bugs_0714_routes.py). fish_rest_tts/speakify монкипатчатся как модульные
символы webrtc_server (route-уровень); тройной importorskip — конвенция webrtc-тестов."""
from __future__ import annotations

import io
import subprocess
import wave

import pytest

pytest.importorskip("aiortc")
pytest.importorskip("cv2")
pytest.importorskip("fastapi")

from starlette.testclient import TestClient

from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.pipeline import webrtc_server
from synapse.pipeline.tts_cache import TTSCache
from synapse.pipeline.webrtc_server import build_web_app
from synapse.threads import ThreadStore

_CSRF = {"content-type": "application/json", "origin": "http://testserver"}


def _wav(pcm: bytes = b"\x01\x02\x03\x04", sr: int = 16000, ch: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


class _TTSHost:
    """Хост для /api/tts: реальные TTSCache + cfg (ключи проставлены), без Kora/пайплайна."""

    def __init__(self, tmp_path, google_key=None):
        self.cfg = SynapseConfig(
            fish_audio_api_key="fish-k",
            fish_reference_id="voice-1",
            google_api_key=google_key,
        )
        self.tts_cache = TTSCache(tmp_path / "cache", self.cfg.fish_tts_model,
                                  self.cfg.fish_reference_id or "")


# --------------------------- POST /api/tts ---------------------------

def test_tts_missing_origin_is_403(tmp_path):
    app = build_web_app(_TTSHost(tmp_path))
    client = TestClient(app, raise_server_exceptions=False)
    # без Origin/Referer — только content-type; _csrf_ok отвергает
    resp = client.post("/api/tts", json={"text": "hi"}, headers={"content-type": "application/json"})
    assert resp.status_code == 403


def test_tts_stub_host_without_cache_is_503(tmp_path):
    app = build_web_app(host=object())
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/tts", json={"text": "hi"}, headers=_CSRF)
    assert resp.status_code == 503


def test_tts_empty_text_is_400(tmp_path):
    app = build_web_app(_TTSHost(tmp_path))
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/tts", json={"text": "   "}, headers=_CSRF)
    assert resp.status_code == 400


def test_tts_cache_hit_does_not_synthesize(tmp_path, monkeypatch):
    host = _TTSHost(tmp_path)
    wav = _wav(b"\xaa\xbb\xcc\xdd")
    host.tts_cache.put_wav("Привет мир", wav)  # пре-сид под ключ disp-текста

    calls = {"n": 0}
    async def _fake_synth(*a, **k):
        calls["n"] += 1
        return b"SHOULD-NOT-BE-CALLED"
    monkeypatch.setattr(webrtc_server, "fish_rest_tts", _fake_synth)

    app = build_web_app(host)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/tts", json={"text": "Привет мир", "role": "disp"}, headers=_CSRF)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.headers["x-tts-source"] == "cache"
    assert resp.content == wav
    assert calls["n"] == 0  # синтез не звался


def test_tts_miss_synthesizes_then_caches(tmp_path, monkeypatch):
    host = _TTSHost(tmp_path)
    calls = {"n": 0}
    async def _fake_synth(text, **k):
        calls["n"] += 1
        return _wav(b"\x10\x20\x30\x40")
    monkeypatch.setattr(webrtc_server, "fish_rest_tts", _fake_synth)

    app = build_web_app(host)
    client = TestClient(app, raise_server_exceptions=False)
    r1 = client.post("/api/tts", json={"text": "Одно предложение.", "role": "disp"}, headers=_CSRF)
    assert r1.status_code == 200
    assert r1.headers["x-tts-source"] == "synth"
    assert calls["n"] == 1
    # второй вызов того же текста — из кэша, синтез больше не зовётся
    r2 = client.post("/api/tts", json={"text": "Одно предложение.", "role": "disp"}, headers=_CSRF)
    assert r2.status_code == 200
    assert r2.headers["x-tts-source"] == "cache"
    assert calls["n"] == 1
    assert r2.content == r1.content


def test_tts_kora_role_runs_speakify_and_caches_sanitized(tmp_path, monkeypatch):
    host = _TTSHost(tmp_path, google_key="g-key")
    seen = {"speakify": 0, "arg": None}
    async def _fake_speakify(text, **k):
        seen["speakify"] += 1
        seen["arg"] = text
        return "человеческий текст"
    async def _fake_synth(text, **k):
        return _wav(b"\x77\x88")
    monkeypatch.setattr(webrtc_server, "speakify", _fake_speakify)
    monkeypatch.setattr(webrtc_server, "fish_rest_tts", _fake_synth)

    app = build_web_app(host)
    client = TestClient(app, raise_server_exceptions=False)
    raw = "готово: правил app.py:1024, тесты зелёные"
    resp = client.post("/api/tts", json={"text": raw, "role": "kora"}, headers=_CSRF)
    assert resp.status_code == 200
    assert seen["speakify"] == 1
    assert seen["arg"] == raw
    # санитайз закэширован под исходным текстом (.speak.txt)
    assert host.tts_cache.get_speak_text(raw) == "человеческий текст"
    assert host.tts_cache.speak_text_path(raw).exists()


# --------------------------- GET /api/threads/{id}/diff ---------------------------

class _DiffHost:
    def __init__(self, threads, root):
        self.threads = threads
        self._root = root

    def resolve_thread_root(self, th):
        return self._root


def _threads(tmp_path):
    return ThreadStore(FakeClock(), str(tmp_path / "threads"))


def test_diff_unknown_thread_is_404(tmp_path):
    host = _DiffHost(_threads(tmp_path), str(tmp_path))
    app = build_web_app(host)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/threads/nope/diff")
    assert resp.status_code == 404


def test_diff_stub_host_is_503(tmp_path):
    app = build_web_app(host=object())
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/threads/whatever/diff")
    assert resp.status_code == 503


def test_diff_non_repo_root_returns_repo_false(tmp_path):
    threads = _threads(tmp_path)
    th = threads.create("t")
    plain = tmp_path / "plain"
    plain.mkdir()
    host = _DiffHost(threads, str(plain))
    app = build_web_app(host)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(f"/api/threads/{th.id}/diff")
    assert resp.status_code == 200
    assert resp.json()["repo"] is False


def test_diff_git_repo_reports_files_and_plus_line(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                    capture_output=True, env={**__import__("os").environ, **env})
    run("init", "-q")
    (repo / "file.txt").write_text("line1\n", encoding="utf-8")
    run("add", "file.txt")
    run("commit", "-q", "-m", "init")
    (repo / "file.txt").write_text("line1\nline2\n", encoding="utf-8")  # tracked-модификация

    threads = _threads(tmp_path)
    th = threads.create("t")
    host = _DiffHost(threads, str(repo))
    app = build_web_app(host)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(f"/api/threads/{th.id}/diff")
    assert resp.status_code == 200
    data = resp.json()
    assert data["repo"] is True
    assert data["files"]  # status --porcelain непуст
    assert any(f["path"] == "file.txt" for f in data["files"])
    assert "+line2" in data["diff"]
