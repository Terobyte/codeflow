# -*- coding: utf-8-sig -*-
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import TaskStore
from synapse.clock import Clock
from synapse.config import SynapseConfig
from synapse.dispatcher.llm_client import CostCapBlocked, ProviderUnavailable
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.dispatcher.tools import KoraBridge, ToolHandlers
from synapse.journal import TurnJournal
from synapse.threads import ThreadStore
from tools.bench_llm_providers import TASKS, Provider, Task, run_task


def _webrtc_or_skip():
    pytest.importorskip("aiortc")
    pytest.importorskip("cv2")
    pytest.importorskip("fastapi")
    try:
        from synapse.pipeline import webrtc_server
        return webrtc_server
    except (ImportError, RuntimeError) as e:
        pytest.skip(f"webrtc deps unavailable: {e}")


def _endpoint(app, name):
    return next(r.endpoint for r in app.routes if getattr(getattr(r, "endpoint", None), "__name__", "") == name)


class FakeRequest:
    def __init__(self, body=None, json_ct=True, origin="http://testserver", host="testserver"):
        self._body = body or {}
        self.headers = {"content-type": "application/json" if json_ct else "text/plain",
                        "host": host}
        if origin:
            self.headers["origin"] = origin
    async def json(self): return self._body


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t
    def now(self):
        return self.t


class ScriptedLLM:
    def __init__(self):
        self.seen = []
    async def complete(self, messages, tools):
        self.seen.append(messages)
        users = [m for m in messages if m["role"] == "user"]
        return f"ok:{users[-1]['content']}:{len(users)}", []


def _loop(tmp_path, feed_reader=None):
    clock = FakeClock()
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
                          cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    handlers = ToolHandlers(KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg), journal)
    llm = ScriptedLLM()
    return DispatcherTurnLoop(llm, handlers, confirm, store, journal, clock, cfg,
                              thread_feed_reader=feed_reader), llm


def _api_host(tmp_path):
    clock = FakeClock()
    threads = ThreadStore(clock, tmp_path / "threads")
    loop_obj, llm = _loop(tmp_path, feed_reader=threads.read_feed)
    from synapse.projects import ProjectStore
    _stub_journal = SimpleNamespace(
        close=lambda: None,
        end_turn=lambda: None,
        check_grounding=lambda *a, **k: None,
        alert=MagicMock(),
    )
    return SimpleNamespace(
        clock=clock, store=loop_obj._store, threads=threads,
        projects=ProjectStore(tmp_path / "projects.json"),
        text_loop=loop_obj, turn_lock=asyncio.Lock(),
        current_http_thread={"id": None}, voice_thread={"id": None},
        voice_project={"id": None},
        journal=_stub_journal,
        http_handlers=SimpleNamespace(end_turn=lambda: None),
    )


# --- [P1] Cost cap и сбой провайдера по-прежнему превращаются в HTTP 500 ---

@pytest.mark.asyncio
async def test_p1_cost_cap_blocked_degraded_response(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("тред")
    ep = _endpoint(app, "api_thread_message")
    
    # Mock ingest_user_turn to raise CostCapBlocked
    host.text_loop.ingest_user_turn = AsyncMock(side_effect=CostCapBlocked())
    
    # In the fixed implementation, this should return a 200 JSONResponse with reply and degraded=True
    resp = await ep(th.id, FakeRequest({"text": "привет"}))
    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data.get("degraded") is True
    assert "Дневной лимит платных запросов исчерпан" in data.get("reply")
    
    # Feed should contain the user and assistant fallback text
    feed = host.threads.read_feed(th.id)
    assert len(feed) >= 2
    assert feed[-2]["kind"] == "user"
    assert feed[-1]["kind"] == "assistant"
    assert "Дневной лимит платных запросов исчерпан" in feed[-1]["text"]
    
    # Alert should be written to journal
    host.journal.alert.assert_any_call("COST_CAP", {"channel": "http"})


@pytest.mark.asyncio
async def test_p1_provider_unavailable_degraded_response(tmp_path):
    webrtc_server = _webrtc_or_skip()
    host = _api_host(tmp_path)
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("тред")
    ep = _endpoint(app, "api_thread_message")
    
    # Mock ingest_user_turn to raise ProviderUnavailable
    host.text_loop.ingest_user_turn = AsyncMock(side_effect=ProviderUnavailable("Timeout"))
    
    # In the fixed implementation, this should return a 200 JSONResponse with reply and degraded=True
    resp = await ep(th.id, FakeRequest({"text": "привет"}))
    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data.get("degraded") is True
    assert "Связь с мозгом потеряна" in data.get("reply")
    
    # Feed should contain the user and assistant fallback text
    feed = host.threads.read_feed(th.id)
    assert len(feed) >= 2
    assert feed[-2]["kind"] == "user"
    assert feed[-1]["kind"] == "assistant"
    assert "Связь с мозгом потеряна" in feed[-1]["text"]
    
    # Alert should be written to journal
    host.journal.alert.assert_any_call("ALL_TIERS_FAILED", {"channel": "http", "reason": "provider"})


# --- [P2] Пустой ответ считается успешной доступностью ---

class EmptyResponseProvider(Provider):
    name = "test_provider"
    model = "test_model"
    def request(self, prompt: str, timeout_s: float) -> tuple[int, str]:
        return 200, "" # HTTP 200 but empty response text


def test_p2_run_task_empty_response_not_ok():
    task = Task("t01", "test", "prompt", lambda text: True)
    provider = EmptyResponseProvider()
    
    result = run_task(provider, task, attempt=1, retries=0, timeout_s=5.0)
    assert result.ok is False, "Empty response should be marked as ok=False"
    assert result.error_kind == "invalid_response", "Should have an error kind"


# --- [P2] Валидаторы benchmark не проверяют заявленные контракты ---

def test_p2_benchmark_validators_are_strict():
    task_map = {t.id: t for t in TASKS}
    
    # t01: Reply with exactly ACK-01
    assert not task_map["t01"].validator("Here is ACK-01")
    
    # t02: Reply with only the number
    assert not task_map["t02"].validator("The result is 391")
    
    # t03: Explain DNS in one sentence
    assert not task_map["t03"].validator("DNS переводит имена. Также он делает запросы.") # Two sentences
    
    # t04: JSON city/country exact structure
    assert not task_map["t04"].validator('{"city":"London"}')
    
    # t05: Translate to Russian
    assert not task_map["t05"].validator("The build finished successfully.") # English
    
    # t06: One word sentiment
    assert not task_map["t06"].validator("It is positive") # Not one word
    
    # t07: Code under 20 chars with last item
    assert not task_map["t07"].validator("def get_last(lst):\n    return lst[-1]\n") # Too long / not expression
    
    # t08: Reply as H:MM
    assert not task_map["t08"].validator("The time was 2:27")
    
    # t09: Exactly three comma-separated codes
    assert not task_map["t09"].validator("400") # Only one
    
    # t10: Reply with one word
    assert not task_map["t10"].validator("The answer is green.")
