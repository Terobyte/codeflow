# -*- coding: utf-8 -*-
import asyncio
import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# Check missing deps
pytest.importorskip("aiortc")
pytest.importorskip("cv2")
pytest.importorskip("fastapi")

from synapse.config import SynapseConfig
from synapse.clock import SystemClock
from synapse.bridge.state import TaskStore, TaskStatus, KoraEvent, EventClass
from synapse.journal import TurnJournal
from synapse.pipeline.app import SynapseHost
from synapse.bridge.runspec import RunSpec
from synapse.pipeline.tts_cache import TTSCache, TTSCacheObserver
from pipecat.observers.base_observer import FramePushed
from pipecat.frames.frames import (
    TTSStartedFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
    TTSStoppedFrame,
)
from synapse.pipeline.webrtc_server import build_web_app, _browse_dir
from synapse.bridge.kora import KoraRunner
from synapse.threads import ThreadStore
from synapse.dispatcher.tools import ToolHandlers, ToolCall


# B-PIPE-1 — _run_finished stage transition failure silently swallowed after outcome write
@pytest.mark.asyncio
@pytest.mark.xfail(reason="known-red: отчёт охотника, премиса не верифицирована (решение Теро 2026-07-16)", strict=False)
async def test_b_pipe_1_swallowed_stage_transition_failure(tmp_path):
    clock = SystemClock()
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    try:
        thread = MagicMock()
        thread.stage = "code"
        thread.request_text = "test request"
        threads.get.return_value = thread
        
        # Mock set_stage to raise ValueError, simulating transition failure/race
        threads.set_stage.side_effect = ValueError("Illegal stage transition")
        
        host = SynapseHost(
            clock=clock, cfg=cfg, journal=journal, store=store,
            speak_ledger=MagicMock(), classifier=MagicMock(),
            confirm_flow=MagicMock(), arbiter_policy=MagicMock(),
            bridge=MagicMock(), handlers=MagicMock(), breaker=MagicMock(),
            cost_cap=MagicMock(), threads=threads
        )
        
        # Expected: The ValueError should propagate or roll back the set_outcome.
        # Actual: ValueError is caught, pass is executed, leaving last_outcome="completed"
        # while the stage is still "code".
        with pytest.raises(ValueError, match="Illegal stage transition"):
            host._run_finished("thread_1", "completed", gate_mode="full")
    finally:
        journal.close()


# B-PIPE-2 — kora_runner.start() failure after state mutations leaves zombie run
@pytest.mark.asyncio
async def test_b_pipe_2_zombie_run_on_kora_start_failure(tmp_path):
    clock = SystemClock()
    cfg = SynapseConfig()
    store = TaskStore(clock, journal_dir=tmp_path)
    threads = MagicMock()
    
    thread = MagicMock()
    thread.id = "thread_1"
    thread.request_text = "do something"
    thread.stage = "collect"
    thread.archived = False
    threads.get.return_value = thread
    
    kora_runner = MagicMock()
    kora_runner.start.side_effect = RuntimeError("Failed to initialize workspace/runner")
    
    host = SynapseHost(
        clock=clock, cfg=cfg, journal=MagicMock(), store=store,
        speak_ledger=MagicMock(), classifier=MagicMock(),
        confirm_flow=MagicMock(), arbiter_policy=MagicMock(),
        bridge=MagicMock(), handlers=MagicMock(), breaker=MagicMock(),
        cost_cap=MagicMock(), threads=threads, kora_runner=kora_runner
    )
    
    # Call gate_action. The exception propagates, but the store task remains RUNNING
    # and the stage remains "spec_plan" (the mutations were already applied).
    with pytest.raises(RuntimeError, match="Failed to initialize workspace/runner"):
        await host.gate_action("thread_1", "send_to_kora", user_initiated=True, confirm=True)
        
    # Expected: If the runner fail to start, the state mutations should be rolled back.
    # Actual: store.has_active_task() is True (the task is running but runner failed).
    assert not store.has_active_task()


# B-PIPE-3 — monitor_forever exception handler continues silently, heartbeat checks skipped indefinitely
@pytest.mark.asyncio
@pytest.mark.xfail(reason="known-red: отчёт охотника, премиса не верифицирована (решение Теро 2026-07-16)", strict=False)
async def test_b_pipe_3_monitor_forever_swallows_persistent_exceptions():
    clock = SystemClock()
    cfg = SynapseConfig()
    cfg.heartbeat_interval_s = 0.001
    
    store = MagicMock()
    # Mock store.liveness to persistently raise an exception
    store.liveness.side_effect = RuntimeError("Persistent DB connection failure")
    
    host = SynapseHost(
        clock=clock, cfg=cfg, journal=MagicMock(), store=store,
        speak_ledger=MagicMock(), classifier=MagicMock(),
        confirm_flow=MagicMock(), arbiter_policy=MagicMock(),
        bridge=MagicMock(), handlers=MagicMock(), breaker=MagicMock(),
        cost_cap=MagicMock()
    )
    
    task = asyncio.create_task(host.monitor_forever())
    
    try:
        # Give it a brief moment to run a few iterations.
        # If it propagates, the task will finish with an error.
        await asyncio.wait_for(task, timeout=2.0)
    except TimeoutError:
        # If it timed out, the loop is running indefinitely, swallowing the exceptions.
        pytest.fail("B-PIPE-3: monitor_forever loop continued silently after persistent exceptions")
    except RuntimeError as e:
        assert "Persistent DB connection failure" in str(e)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# B-PIPE-4 — TTSCacheObserver exception handler swallows all cache write failures
@pytest.mark.asyncio
@pytest.mark.xfail(reason="known-red: отчёт охотника, премиса не верифицирована (решение Теро 2026-07-16)", strict=False)
async def test_b_pipe_4_tts_cache_observer_swallows_cache_write_failures(tmp_path):
    cache = TTSCache(tmp_path, "model", "voice")
    # Mock put_pcm to raise an OSError (e.g. disk full)
    cache.put_pcm = MagicMock(side_effect=OSError("No space left on device"))
    
    tts = MagicMock()
    observer = TTSCacheObserver(cache, tts)
    
    await observer.on_push_frame(FramePushed(frame=TTSStartedFrame(), source=tts))
    await observer.on_push_frame(FramePushed(frame=TTSAudioRawFrame(audio=b"123", sample_rate=16000, num_channels=1), source=tts))
    await observer.on_push_frame(FramePushed(frame=TTSTextFrame(text="hello"), source=tts))
    
    # Expected: The OSError should be propagated or escalated.
    # Actual: on_push_frame catches all exceptions and logs them without raising.
    with pytest.raises(OSError, match="No space left on device"):
        await observer.on_push_frame(FramePushed(frame=TTSStoppedFrame(), source=tts))


# B-PIPE-5 — run_session finally block cleanup racing with state assignment leaves stale current/bind
@pytest.mark.asyncio
@pytest.mark.xfail(reason="known-red: отчёт охотника, премиса не верифицирована (решение Теро 2026-07-16)", strict=False)
async def test_b_pipe_5_run_session_finally_race(tmp_path):
    from synapse.pipeline.webrtc_server import build_web_app
    
    host = MagicMock()
    host.clock = SystemClock()
    host.cfg = SynapseConfig()
    
    app = build_web_app(host)
    
    # Intercept run_session and active_sessions from build_web_app closure
    routes = [r for r in app.routes if getattr(r, "path", "") == "/sessions/{session_id}/api/offer"]
    assert len(routes) > 0
    
    # Seed the session "abc" via start endpoint
    start_ep = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/start")
    start_req = MagicMock()
    start_req.body = AsyncMock(return_value=b'{"body": {}}')
    start_resp = await start_ep(start_req)
    sid = start_resp["sessionId"]
    
    # We find the run_session closure by inspecting the offer endpoint
    captured_callback = None
    async def mock_handle(self_or_req, **kwargs):
        nonlocal captured_callback
        captured_callback = kwargs.get("webrtc_connection_callback")
        return MagicMock()
    
    from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler
    offer_ep = routes[0].endpoint
    bg = MagicMock()
    with patch.object(SmallWebRTCRequestHandler, "handle_web_request", mock_handle):
        await offer_ep(session_id=sid, request=MagicMock(), background_tasks=bg)
        
    assert captured_callback is not None
    await captured_callback(MagicMock())
    run_session_fn = bg.add_task.call_args[0][0]
    
    # Now we simulate the race.
    # Connection A starts and immediately enters finally because its PipelineRunner raises/exits.
    # Connection B starts concurrently with the same session_id.
    # We patch PipelineRunner.run to allow simulating the sequence.
    from pipecat.pipeline.runner import PipelineRunner
    
    a_in_finally = asyncio.Event()
    b_can_start = asyncio.Event()
    b_finished = asyncio.Event()
    
    original_run = PipelineRunner.run
    
    # We lock execution order inside the finally block.
    # A will acquire the lock first, but we mock the lock in A's run_session
    # to yield and let B start and acquire the lock.
    # Set up mocks
    with patch("synapse.pipeline.webrtc_server.build_session_pipeline", return_value=MagicMock()), \
         patch("synapse.pipeline.webrtc_server.SmallWebRTCTransport", return_value=MagicMock()), \
         patch("synapse.pipeline.webrtc_server.Pipeline", return_value=MagicMock()), \
         patch("synapse.pipeline.webrtc_server.PipelineTask", return_value=MagicMock()):
             
        # Mock PipelineRunner.run to yield for Connection A
        async def mock_run(self, task):
            # Connection A exits immediately to enter finally
            return
                
        with patch.object(PipelineRunner, "run", mock_run):
            # We run Connection A. It completes instantly and enters finally.
            # To avoid global contamination of asyncio.Lock, we mock the Lock class constructor local to build_web_app calls.
            original_lock = asyncio.Lock
            lock_count = 0
            
            class MockedLock(original_lock):
                async def __aenter__(self):
                    nonlocal lock_count
                    lock_count += 1
                    if lock_count == 2: # This is A's finally block lock acquisition
                        a_in_finally.set()
                        try:
                            await asyncio.wait_for(b_can_start.wait(), timeout=5.0)
                        except asyncio.TimeoutError:
                            pass
                    return await super().__aenter__()
                    
            with patch("asyncio.Lock", MockedLock):
                # Start Connection A in background
                task_a = asyncio.create_task(run_session_fn(MagicMock(), session_id=sid))
                try:
                    await asyncio.wait_for(a_in_finally.wait(), timeout=5.0)
                    
                    # Connection B now starts with the same session_id.
                    task_b = asyncio.create_task(run_session_fn(MagicMock(), session_id=sid))
                    try:
                        await asyncio.sleep(0.01) # let B acquire lock and set state
                        
                        # Let A resume and acquire the lock in finally
                        b_can_start.set()
                        
                        await asyncio.wait_for(task_a, timeout=5.0)
                        await asyncio.wait_for(task_b, timeout=5.0)
                    finally:
                        task_b.cancel()
                        try:
                            await task_b
                        except asyncio.CancelledError:
                            pass
                finally:
                    task_a.cancel()
                    try:
                        await task_a
                    except asyncio.CancelledError:
                        pass
                    
        # Expected: B's session_id should still be in active_sessions because B is the active connection.
        # Actual: A's finally block popped sid because current["task"] is not task_A,
        # so it takes the else/preempted branch which can pop the session.
        # Check active_sessions inside the build_web_app closure (we can check if it 404s now)
        resp = await offer_ep(session_id=sid, request=MagicMock(), background_tasks=MagicMock())
        assert resp.status_code != 404, "B-PIPE-5: session_id was popped during finally race"


# B-PIPE-6 — _browse_dir null-byte ValueError silently falls back to home, attacker can probe filesystem
def test_b_pipe_6_browse_dir_null_byte_probe(tmp_path):
    # Expected: Path containing a null byte returns None (resulting in 400 Bad Request).
    # Actual: It falls back to base (home directory) listing, leaking home directory paths.
    res = _browse_dir("/etc\x00/passwd", tmp_path)
    assert res is None, "B-PIPE-6: browse_dir fallback to home on invalid path with null byte"


# B-BRIDGE-1 — TaskStore._persist race: concurrent writes lost or corrupted
def test_b_bridge_1_task_store_persist_race(tmp_path):
    clock = SystemClock()
    store = TaskStore(clock, journal_dir=tmp_path)
    store.start_task("task_1", "test task", TaskStatus.RUNNING, clock.now())
    
    from pathlib import Path
    original_write_text = Path.write_text
    
    barrier = threading.Barrier(2)
    
    def slow_write_text(self, text, *args, **kwargs):
        if "state.json" in str(self):
            data = json.loads(text)
            status = data.get("task", {}).get("status")
            try:
                barrier.wait(timeout=5.0)
            except threading.BrokenBarrierError:
                pass
            if status == "running":
                # Stale snapshot: write last
                time.sleep(0.05)
            elif status == "completed":
                # Fresh snapshot: write first
                pass
        return original_write_text(self, text, *args, **kwargs)
        
    with patch.object(Path, "write_text", slow_write_text):
        ev = KoraEvent(id="ev_1", type="kora_system", cls=EventClass.NARRATABLE, payload={}, speak_text=None, ts=clock.now())
        t1 = threading.Thread(target=store.apply_event, args=(ev,))
        t2 = threading.Thread(target=store.set_task_status, args=(TaskStatus.COMPLETED,))
        
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        
    new_store = TaskStore(clock, journal_dir=tmp_path)
    # Expected: The store status should be COMPLETED, and the event should be persisted.
    # Actual: Thread 1's write clobbered Thread 2's write because there is no synchronization.
    assert new_store.task.status == TaskStatus.COMPLETED, "B-BRIDGE-1: Task status write was lost"
    assert len(new_store.task.events) == 1, "B-BRIDGE-1: Event append write was lost"


# B-BRIDGE-2 — KoraRunner.provide_answer race: InvalidStateError on cancelled future
@pytest.mark.asyncio
async def test_b_bridge_2_provide_answer_cancelled_future_race():
    cfg = SynapseConfig()
    clock = SystemClock()
    store = MagicMock()
    
    runner = KoraRunner(cfg, store, MagicMock(), clock, MagicMock(), None)
    
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    runner._pending_answer = fut
    
    original_set_result = fut.set_result
    def mock_set_result(val):
        fut.cancel()
        original_set_result(val)
        
    fut.set_result = mock_set_result
    
    # Expected: provide_answer returns False gracefully if the future is cancelled/done.
    # Actual: raises uncaught InvalidStateError.
    res = runner.provide_answer("hello")
    assert res is False, "B-BRIDGE-2: provide_answer should return False on cancelled future"


# B-BRIDGE-3 — apply_event race: second event lost when first's _persist in flight
def test_b_bridge_3_apply_event_lost_race(tmp_path):
    clock = SystemClock()
    store = TaskStore(clock, journal_dir=tmp_path)
    store.start_task("task_1", "test task", TaskStatus.RUNNING, clock.now())
    
    from pathlib import Path
    original_write_text = Path.write_text
    original_replace = Path.replace
    
    barrier = threading.Barrier(2)
    e2_replace_done = threading.Event()
    
    def slow_write_text(self, text, *args, **kwargs):
        if "state.json" in str(self):
            data = json.loads(text)
            events = data.get("task", {}).get("events", [])
            event_ids = [e["id"] for e in events]
            
            if len(event_ids) == 1 and event_ids[0] == "ev_1":
                try:
                    barrier.wait(timeout=5.0)
                except threading.BrokenBarrierError:
                    pass
                e2_replace_done.wait(timeout=5.0)
            elif len(event_ids) == 2:
                try:
                    barrier.wait(timeout=5.0)
                except threading.BrokenBarrierError:
                    pass
                return original_write_text(self, text, *args, **kwargs)
        return original_write_text(self, text, *args, **kwargs)
        
    def mock_replace(self, target):
        res = original_replace(self, target)
        if "state.json" in str(self):
            e2_replace_done.set()
        return res
        
    with patch.object(Path, "write_text", slow_write_text), \
         patch.object(Path, "replace", mock_replace):
        ev1 = KoraEvent(id="ev_1", type="kora_system", cls=EventClass.NARRATABLE, payload={}, speak_text=None, ts=clock.now())
        ev2 = KoraEvent(id="ev_2", type="kora_system", cls=EventClass.NARRATABLE, payload={}, speak_text=None, ts=clock.now())
        
        t1 = threading.Thread(target=store.apply_event, args=(ev1,))
        t2 = threading.Thread(target=store.apply_event, args=(ev2,))
        
        t1.start()
        time.sleep(0.01)
        t2.start()
        
        t1.join()
        t2.join()
        
    new_store = TaskStore(clock, journal_dir=tmp_path)
    event_ids = [e.id for e in new_store.task.events]
    assert "ev_1" in event_ids, "B-BRIDGE-3: Event 1 was lost"
    assert "ev_2" in event_ids, "B-BRIDGE-3: Event 2 was lost"


# B-BRIDGE-4 — ThreadStore.append_feed race: lost entries or corrupted ring-buffer rewrite
def test_b_bridge_4_append_feed_race(tmp_path):
    clock = SystemClock()
    # feed_max = 5 -> rewrite is triggered when count > 6
    store = ThreadStore(clock, tmp_path, feed_max=5)
    thread = store.create("test thread")
    
    # Seed 6 entries sequentially so count is 6
    for i in range(6):
        store.append_feed(thread.id, {"ts": clock.now(), "text": f"seed {i}"})
        
    from pathlib import PosixPath
    original_open = Path.open
    original_read_text = Path.read_text
    original_replace = Path.replace
    t1_read_done = threading.Event()
    t2_replace_done = threading.Event()
    
    def mock_read_text(self, *args, **kwargs):
        res = original_read_text(self, *args, **kwargs)
        if ".feed.jsonl" in str(self) and not str(self).endswith(".tmp"):
            if threading.current_thread().name == "Thread-A":
                t1_read_done.set()
                t2_replace_done.wait(timeout=5.0)
        return res
        
    def mock_open(self, *args, **kwargs):
        if ".feed.jsonl" in str(self) and not str(self).endswith(".tmp"):
            if threading.current_thread().name == "Thread-B":
                t1_read_done.wait(timeout=5.0)
        res = original_open(self, *args, **kwargs)
        return res
        
    def slow_replace(self, target):
        if ".feed.jsonl.tmp" in str(self):
            if threading.current_thread().name == "Thread-B":
                try:
                    return original_replace(self, target)
                finally:
                    t2_replace_done.set()
            elif threading.current_thread().name == "Thread-A":
                return original_replace(self, target)
        return original_replace(self, target)
        
    with patch.object(Path, "open", mock_open), \
         patch.object(PosixPath, "open", mock_open), \
         patch.object(Path, "read_text", mock_read_text), \
         patch.object(PosixPath, "read_text", mock_read_text), \
         patch.object(Path, "replace", slow_replace), \
         patch.object(PosixPath, "replace", slow_replace):
        t1 = threading.Thread(target=store.append_feed, args=(thread.id, {"ts": clock.now(), "text": "concurrent A"}), name="Thread-A")
        t2 = threading.Thread(target=store.append_feed, args=(thread.id, {"ts": clock.now(), "text": "concurrent B"}), name="Thread-B")
        
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        
    feed = store.read_feed(thread.id, limit=20)
    texts = [entry["text"] for entry in feed]
    print("DEBUG FEED TEXTS:", texts)
    # Expected: Both concurrent entries should be in the feed.
    # Actual: One concurrent entry was clobbered by the other during the rewrite.
    assert "concurrent A" in texts, "B-BRIDGE-4: Entry A was lost"
    assert "concurrent B" in texts, "B-BRIDGE-4: Entry B was lost"


# B-BRIDGE-5 — ToolHandlers cross-turn dedup collision: late tools share anonymous slot
@pytest.mark.asyncio
async def test_b_bridge_5_tool_handlers_cross_turn_dedup_collision():
    cfg = SynapseConfig()
    clock = SystemClock()
    
    bridge = MagicMock()
    bridge.clock = clock
    bridge.cfg = cfg
    
    handlers = ToolHandlers(bridge, MagicMock())
    
    call_event = asyncio.Event()
    resume_event = asyncio.Event()
    
    submit_calls = 0
    def mock_submit(text, ts, thread_id=None):
        nonlocal submit_calls
        submit_calls += 1
        res = MagicMock()
        res.outcome = MagicMock()
        res.readback_text = None
        res.reject_text = None
        res.task_id = "task_1"
        return res
        
    bridge.confirm_flow.submit = mock_submit
    
    original_guarded = handlers._guarded
    async def mock_guarded(name, args, fn):
        if name == "submit_task":
            call_event.set()
            await resume_event.wait()
        return await original_guarded(name, args, fn)
        
    handlers._guarded = mock_guarded
    
    # 1. Turn A begins and ends
    handlers.begin_turn("turn_A")
    handlers.end_turn()
    
    # 2. Late tool from Turn A runs (no active turn)
    task_a = asyncio.create_task(handlers.submit_task("foo"))
    try:
        await asyncio.wait_for(call_event.wait(), timeout=5.0)
        
        # 3. Turn B begins and ends
        handlers.begin_turn("turn_B")
        handlers.end_turn()
        
        # 4. Late tool from Turn B runs (no active turn)
        task_b = asyncio.create_task(handlers.submit_task("foo"))
        try:
            # Resume A
            resume_event.set()
            await asyncio.wait_for(task_a, timeout=5.0)
            await asyncio.wait_for(task_b, timeout=5.0)
        finally:
            task_b.cancel()
            try:
                await task_b
            except asyncio.CancelledError:
                pass
    finally:
        task_a.cancel()
        try:
            await task_a
        except asyncio.CancelledError:
            pass
            
    # Expected: submit is called twice because the late tool calls are from different turns.
    # Actual: both are assigned to the "<anonymous>" slot, so Turn B's call is deduped.
    assert submit_calls == 2, "B-BRIDGE-5: Cross-turn dedup collision occurred"
