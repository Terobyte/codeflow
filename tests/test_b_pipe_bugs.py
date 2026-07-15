# -*- coding: utf-8-sig -*-
import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from pathlib import Path

from synapse.config import SynapseConfig
from synapse.clock import SystemClock
from synapse.bridge.state import TaskStore
from synapse.journal import TurnJournal
from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.dispatcher.tools import ToolCall, ToolHandlers, KoraBridge
from synapse.dispatcher.loop import DispatcherTurnLoop

# Propagate/Skip if webrtc deps are missing
pytest.importorskip("aiortc")
pytest.importorskip("cv2")
pytest.importorskip("fastapi")

from synapse.pipeline.webrtc_server import build_web_app

class FakeClock:
    def __init__(self, t=0.0):
        self.t = t
    def now(self):
        return self.t

def _setup_test_loop(tmp_path):
    clock = FakeClock()
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(
        store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
        cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s
    )
    handlers = ToolHandlers(KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg), journal)
    llm = MagicMock()
    loop = DispatcherTurnLoop(
        llm, handlers, confirm, store, journal, clock, cfg
    )
    return loop, llm, journal

async def _extract_run_session(host, session_id="test-session-id"):
    """Build a real app, seed a session via /start, then extract the run_session closure
    by intercepting SmallWebRTCRequestHandler.handle_web_request during the offer call.
    Returns (app, run_session_fn, connection_mock)."""
    from synapse.pipeline.webrtc_server import SmallWebRTCRequestHandler

    app = build_web_app(host)

    # Seed a session in the closure's active_sessions via the /start endpoint
    start_ep = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/start")
    start_req = MagicMock()
    start_req.body = AsyncMock(return_value=b'{}')
    start_resp = await start_ep(start_req)
    # We need to use the real session_id minted by /start — but tests want a known id,
    # so we'll call the offer endpoint with that id after manually inserting it.
    # Instead: just use the minted session_id.
    real_sid = start_resp["sessionId"] if isinstance(start_resp, dict) else session_id

    # Capture the run_session function via the offer endpoint
    bg = MagicMock()
    connection = MagicMock()

    captured_callback = None
    async def mock_handle(self_or_req, **kwargs):
        nonlocal captured_callback
        # Called as handler.handle_web_request(request=..., webrtc_connection_callback=...)
        captured_callback = kwargs.get("webrtc_connection_callback")
        return MagicMock()

    offer_ep = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/sessions/{session_id}/api/offer")

    with patch.object(SmallWebRTCRequestHandler, "handle_web_request", mock_handle):
        await offer_ep(session_id=real_sid, request=MagicMock(), background_tasks=bg)

    assert captured_callback is not None
    await captured_callback(connection)
    assert bg.add_task.called
    run_session_fn = bg.add_task.call_args[0][0]
    return app, run_session_fn, connection, real_sid


# B-PIPE-1 MAJOR — setup WebRTC-сессии вне try → утечка соединения при ошибке
@pytest.mark.asyncio
async def test_b_pipe_1_setup_webrtc_failure_leaks_session():
    host = MagicMock()

    with patch("synapse.pipeline.webrtc_server.build_session_pipeline", side_effect=ValueError("Setup failed")):
        app, run_session_fn, connection, sid = await _extract_run_session(host)

        # The session was seeded by /start — it should exist before run_session
        offer_ep = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/sessions/{session_id}/api/offer")

        # run_session should raise but clean up active_sessions in finally
        with pytest.raises(ValueError, match="Setup failed"):
            await run_session_fn(connection, session_id=sid)

        # After cleanup, the session_id should be gone — a second offer should 404
        from fastapi.responses import Response as FResponse
        resp = await offer_ep(session_id=sid, request=MagicMock(), background_tasks=MagicMock())
        assert getattr(resp, "status_code", None) == 404, \
            "B-PIPE-1: session was leaked in active_sessions on setup failure"


# B-PIPE-2 MAJOR — monitor_forever спавнится на каждое соединение → дубли alerts
@pytest.mark.asyncio
async def test_b_pipe_2_monitor_forever_spawned_per_connection():
    host = MagicMock()

    # monitor_forever must return a coroutine that blocks forever (stays pending)
    async def _never_ending():
        await asyncio.Event().wait()

    host.monitor_forever.side_effect = lambda: _never_ending()

    with patch("synapse.pipeline.webrtc_server.build_session_pipeline", return_value=MagicMock()), \
         patch("synapse.pipeline.webrtc_server.SmallWebRTCTransport", return_value=MagicMock()), \
         patch("synapse.pipeline.webrtc_server.Pipeline", return_value=MagicMock()), \
         patch("synapse.pipeline.webrtc_server.PipelineTask", return_value=MagicMock()), \
         patch("synapse.pipeline.webrtc_server.PipelineRunner") as runner_cls:

        runner_cls.return_value.run = AsyncMock()

        # First run
        app1, run1, conn1, sid1 = await _extract_run_session(host)
        await run1(conn1, session_id=sid1)

        # Second run — reuse the same app so the closure's _monitor dict is shared.
        # We can't call _extract_run_session again (it builds a NEW app with a fresh _monitor).
        # Instead, seed another session via /start and run again.
        start_ep = next(r.endpoint for r in app1.routes if getattr(r, "path", "") == "/start")
        start_req = MagicMock()
        start_req.body = AsyncMock(return_value=b'{}')
        resp2 = await start_ep(start_req)
        sid2 = resp2["sessionId"]

        await run1(conn1, session_id=sid2)

        assert host.monitor_forever.call_count <= 1, \
            "B-PIPE-2: monitor_forever was spawned multiple times (per connection)"


# B-PIPE-3 MAJOR — _histories в DispatcherTurnLoop растёт без bounds (утечка RAM)
@pytest.mark.asyncio
async def test_b_pipe_3_histories_grow_unbounded(tmp_path):
    loop, _, _ = _setup_test_loop(tmp_path)
    
    for i in range(1000):
        loop._history_for(f"thread_{i}").append({"role": "user", "content": "hi"})
        
    assert len(loop._histories) < 100, "B-PIPE-3: _histories dictionary grows unbounded (memory leak)"


# B-PIPE-4 MAJOR — ingest_user_turn без try/finally → открытый journal turn + битая история при падении LLM
@pytest.mark.asyncio
async def test_b_pipe_4_ingest_user_turn_failure_retains_user_history(tmp_path):
    loop, llm, journal = _setup_test_loop(tmp_path)
    
    llm.complete = AsyncMock(side_effect=RuntimeError("Anthropic API is down"))
    
    with pytest.raises(RuntimeError, match="Anthropic API is down"):
        await loop.ingest_user_turn("hello user message", thread_id="test_thread")
        
    assert journal.current is None, "B-PIPE-4: journal current turn was left open after LLM call failure"
    
    history = loop._history_for("test_thread")
    assert not any(msg["role"] == "user" and msg["content"] == "hello user message" for msg in history),         "B-PIPE-4: user message was retained in history after LLM failure"


# B-PIPE-5 MAJOR — turn_lock держится минуты во время LLM-вызова → голос блокируется
@pytest.mark.asyncio
async def test_b_pipe_5_turn_lock_held_during_llm_call(tmp_path):
    loop, llm, _ = _setup_test_loop(tmp_path)
    lock = asyncio.Lock()
    host = MagicMock()
    host.turn_lock = lock
    host.text_loop = loop
    host.current_http_thread = {"id": None}
    host.threads = MagicMock()
    host.threads.get.return_value = MagicMock()
    host.clock = FakeClock()
    
    llm_started = asyncio.Event()
    llm_finish = asyncio.Event()
    
    async def complete_slow(messages, tools):
        llm_started.set()
        await llm_finish.wait()
        return "response", []
        
    llm.complete = complete_slow
    
    app = build_web_app(host=host)
    ep = next(r.endpoint for r in app.routes if getattr(r.endpoint, "__name__", "") == "api_thread_message")
    
    request = MagicMock()
    request.headers = {"content-type": "application/json", "host": "localhost", "origin": "http://localhost"}
    request.json = AsyncMock(return_value={"text": "hello slow LLM"})
    
    print("DEBUG: host.turn_lock before run:", host.turn_lock)
    loop_task = asyncio.create_task(ep("thread_1", request))
    
    await llm_started.wait()
    print("DEBUG: host.turn_lock inside LLM run:", host.turn_lock, "locked:", host.turn_lock.locked())
    is_locked = host.turn_lock.locked()
    
    llm_finish.set()
    await loop_task
    
    assert not is_locked, "B-PIPE-5: turn_lock is held during LLM completion (blocks other clients)"


# B-PIPE-6/7 MINOR — CSRF пропускает без Origin; _dispatch_tool падает на кривых аргументах LLM
@pytest.mark.asyncio
async def test_b_pipe_6_csrf_bypass_without_origin_and_referer():
    host = MagicMock()
    host.projects = MagicMock()
    host.projects.add = AsyncMock(return_value={})
    
    app = build_web_app(host)
    ep = next(r.endpoint for r in app.routes if getattr(r.endpoint, "__name__", "") == "api_projects_add")
    
    class FakeRequest:
        def __init__(self):
            self.headers = {
                "content-type": "application/json",
                "host": "localhost"
            }
        async def json(self):
            return {"name": "test", "path": "/valid"}
            
    req = FakeRequest()
    resp = await ep(req)
    
    assert resp.status_code == 403, "B-PIPE-6: Request without Origin/Referer bypassed CSRF check"


@pytest.mark.asyncio
async def test_b_pipe_7_dispatch_tool_handles_invalid_arguments_gracefully(tmp_path):
    loop, _, _ = _setup_test_loop(tmp_path)
    bad_call = ToolCall(name="submit_task", arguments={"wrong_arg": "value"}, id="call_1")
    history = []
    
    try:
        await loop._dispatch_tool(bad_call, history)
    except TypeError as e:
        pytest.fail(f"B-PIPE-7: _dispatch_tool crashed with TypeError on bad LLM arguments: {e}")
        
    assert len(history) == 1
    assert "error" in history[0]["content"], "B-PIPE-7: Expected error message in tool result when arguments are invalid"
