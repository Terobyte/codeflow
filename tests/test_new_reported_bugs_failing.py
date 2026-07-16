# -*- coding: utf-8 -*-
import asyncio
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# Ensure mocked modules for recording exist
mock_sd = MagicMock()
mock_sf = MagicMock()
sys.modules["sounddevice"] = mock_sd
sys.modules["soundfile"] = mock_sf

# Check missing deps
pytest.importorskip("aiortc")
pytest.importorskip("cv2")
pytest.importorskip("fastapi")

from synapse.config import SynapseConfig
from synapse.clock import SystemClock
from synapse.bridge.state import TaskStore, TaskStatus, KoraEvent, EventClass, Liveness
from synapse.dispatcher.loop import DispatcherTurnLoop, history_from_feed
from synapse.dispatcher.tools import ToolHandlers
from synapse.cascade.services import CostCap
from synapse.cascade.breaker import CircuitBreaker
from synapse.journal import TurnJournal
from synapse.runners.record_commands import record_session
from synapse.pipeline.tts_cache import TTSCache
from synapse.bridge.kora import KoraRunner


# B-DISP-1 — history compaction race: concurrent threads corrupt splices
@pytest.mark.asyncio
@pytest.mark.xfail(reason="known-red: отчёт охотника, премиса не верифицирована (решение Теро 2026-07-16)", strict=False)
async def test_b_disp_1_history_compaction_race():
    # ThreadSafeComplete avoids AsyncMock thread-safety violations when called from multiple OS threads
    class ThreadSafeComplete:
        def __init__(self):
            self._lock = threading.Lock()
            self.calls = []
        async def __call__(self, *args, **kwargs):
            with self._lock:
                self.calls.append((args, kwargs))
            return "[КОМПАКТ_ОК]", []
            
    llm = MagicMock()
    llm.complete = ThreadSafeComplete()
    
    handlers = MagicMock()
    confirm_flow = MagicMock()
    store = MagicMock()
    journal = MagicMock()
    clock = SystemClock()
    cfg = SynapseConfig()
    cfg.dispatcher_compact_after = 4
    
    loop = DispatcherTurnLoop(llm, handlers, confirm_flow, store, journal, clock, cfg)
    
    # We populate the history of thread_1
    # 6 elements -> cut is 3
    # older has length 3: [E0, E1, E2]
    # tail has length 3: [E3, E4, E5]
    class RaceyList(list):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.barrier = threading.Barrier(2)
            self.compare_started = threading.Event()
            
        def __getitem__(self, item):
            res = super().__getitem__(item)
            if isinstance(item, slice) and item.stop is not None:
                # We intercept slice check during _maybe_compact
                if threading.current_thread().name == "Thread-A":
                    self.compare_started.set()
                    try:
                        self.barrier.wait(timeout=5.0)
                    except threading.BrokenBarrierError:
                        pass
                    # Let Thread-B complete comparison and write first
                    time.sleep(0.05)
                elif threading.current_thread().name == "Thread-B":
                    self.compare_started.wait()
                    try:
                        self.barrier.wait(timeout=5.0)
                    except threading.BrokenBarrierError:
                        pass
            return res
            
    history = RaceyList([
        {"role": "user", "content": "E0"},
        {"role": "assistant", "content": "E1"},
        {"role": "user", "content": "E2"},
        {"role": "assistant", "content": "E3"},
        {"role": "user", "content": "E4"},
        {"role": "assistant", "content": "E5"},
    ])
    
    # Run two threads that both trigger maybe_compact concurrently
    def run_compact(name):
        threading.current_thread().name = name
        # We run the async loop inside the thread
        l = asyncio.new_event_loop()
        l.run_until_complete(loop._maybe_compact("thread_1", history))
        l.close()
        
    t1 = threading.Thread(target=run_compact, args=("Thread-A",))
    t2 = threading.Thread(target=run_compact, args=("Thread-B",))
    
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    
    # Expected: The tail of history ([E3, E4, E5]) should be preserved after compaction.
    # Actual: Thread-A's late slice assignment overwrote the entire list because
    # it compared before Thread-B's write, resulting in tail truncation.
    assert len(history) > 1, "B-DISP-1: Compaction race resulted in history truncation"


# B-DISP-2 — end_turn() doesn't clear anonymous dedup slot when turn_id=None
@pytest.mark.asyncio
async def test_b_disp_2_end_turn_anonymous_leak():
    bridge = MagicMock()
    bridge.clock = SystemClock()
    bridge.cfg = SynapseConfig()
    handlers = ToolHandlers(bridge, MagicMock())
    
    async def dummy_tool():
        return {"ok": True}
        
    # Populate the anonymous slot
    res, hit = await handlers._guarded("submit_task", {"text": "foo"}, dummy_tool)
    assert not hit
    assert "<anonymous>" in handlers._dedup
    assert "submit_task" in handlers._dedup["<anonymous>"]
    
    # Call end_turn while current turn_id is None
    handlers.end_turn()
    
    # Expected: The anonymous slot should be cleared/popped to prevent unbounded leaks.
    # Actual: It remains intact, caching old tool calls indefinitely.
    assert "<anonymous>" not in handlers._dedup or not handlers._dedup["<anonymous>"], "B-DISP-2: Anonymous slot not cleared"


# B-DISP-3 — _guarded dedup dict comparison fragile for nested args
@pytest.mark.asyncio
@pytest.mark.xfail(reason="known-red: отчёт охотника, премиса не верифицирована (решение Теро 2026-07-16)", strict=False)
async def test_b_disp_3_guarded_nested_args_comparison():
    bridge = MagicMock()
    bridge.clock = SystemClock()
    bridge.cfg = SynapseConfig()
    handlers = ToolHandlers(bridge, MagicMock())
    handlers.begin_turn("turn_1")
    
    call_count = 0
    async def dummy_tool():
        nonlocal call_count
        call_count += 1
        return {"ok": True}
        
    # Clarification regarding list element order:
    # While list order differences are technically ordered, in many API tool call designs
    # (e.g. bulk deleting an unordered set of file paths or ids), the elements represent
    # a set, and their order is semantically irrelevant. The simple dict equality checks
    # treat these as different call signatures, causing false misses.
    args1 = {"nested_list": [1, 2], "nested_dict": {"a": 1, "b": 2}}
    res1, hit1 = await handlers._guarded("submit_task", args1, dummy_tool)
    assert not hit1
    assert call_count == 1
    
    # Order-independent semantically identical list
    args2 = {"nested_list": [2, 1], "nested_dict": {"a": 1, "b": 2}}
    
    # Expected: Tool call with semantically identical nested arguments should hit the deduplication slot.
    # Actual: Python's standard dict comparison treats them as different, resulting in a cache miss.
    res2, hit2 = await handlers._guarded("submit_task", args2, dummy_tool)
    assert hit2, "B-DISP-3: Fragile nested args comparison caused a false dedup miss"


# B-DISP-4 — start_task doesn't reset store-level _last_event_ts, false UNREACHABLE
def test_b_disp_4_start_task_liveness_ts_leak():
    clock = SystemClock()
    store = TaskStore(clock, journal_dir=None)
    
    store.start_task("task_A", "text", TaskStatus.RUNNING, 100.0)
    ev = KoraEvent(id="ev_1", type="kora_system", cls=EventClass.NARRATABLE, payload={}, speak_text=None, ts=100.0)
    store.apply_event(ev)
    assert store._last_event_ts == 100.0
    
    store.set_task_status(TaskStatus.COMPLETED)
    
    # Start task B at 200.0, set status FAILED
    store.start_task("task_B", "text", TaskStatus.RUNNING, 200.0)
    store.set_task_status(TaskStatus.FAILED)
    
    # Expected: Starting a new task should clear or reset the store-level last event timestamp.
    # Actual: It keeps task A's old timestamp (100.0), returning UNREACHABLE at 300.0.
    res = store.liveness(now=300.0, stale_after_s=50.0, unreachable_after_s=100.0)
    assert res != Liveness.UNREACHABLE, "B-DISP-4: Stale _last_event_ts from previous task caused false UNREACHABLE"


# B-DISP-5 — zombie reconciliation appends event but doesn't update _last_event_ts
@pytest.mark.xfail(reason="known-red: отчёт охотника, премиса не верифицирована (решение Теро 2026-07-16)", strict=False)
def test_b_disp_5_zombie_reconcile_liveness_ts_leak(tmp_path):
    clock = SystemClock()
    state_path = tmp_path / "state.json"
    data = {
        "task": {
            "id": "task_1",
            "text": "run",
            "status": "running",
            "started_ts": 100.0,
            "last_event_ts": 100.0,
            "events": []
        },
        "last_event_ts": 100.0,
        "staged": None
    }
    state_path.write_text(json.dumps(data), encoding="utf-8")
    
    clock.now = MagicMock(return_value=500.0)
    store = TaskStore(clock, journal_dir=tmp_path)
    
    # Expected: Zombie reconciliation on boot should update the store's last event timestamp to the current boot time.
    # Actual: It retains the old crash-time timestamp (100.0), resulting in UNREACHABLE at 600.0.
    res = store.liveness(now=600.0, stale_after_s=150.0, unreachable_after_s=200.0)
    assert res != Liveness.UNREACHABLE, "B-DISP-5: Zombie reconciliation failed to update store _last_event_ts"


# B-DISP-6 — history_from_feed crashes on non-dict entries with AttributeError
def test_b_disp_6_history_from_feed_type_error():
    entries = [
        {"kind": "user", "text": "hello"},
        "malformed_non_dict_entry",
        {"kind": "assistant", "text": "hi"}
    ]
    # Expected: Safely skips or filters out non-dictionary entries from the feed.
    # Actual: Throws AttributeError trying to access .get() on the string.
    try:
        res = history_from_feed(entries)
        assert len(res) == 2
    except AttributeError:
        pytest.fail("B-DISP-6: history_from_feed raised AttributeError on non-dict entry")


# B-CASC-1 — Negative day buckets corrupt cost cap tracking before reset hour
def test_b_casc_1_negative_day_buckets():
    cap = CostCap(max_paid_calls_per_day=3, rpd_reset_hour_utc=8)
    # Expected: The computed day bucket must be non-negative.
    # Actual: It returns -1 for timestamps before the reset hour on epoch day.
    bucket = cap._day_bucket(7 * 3600)
    assert bucket >= 0, "B-CASC-1: _day_bucket returned negative day bucket"


# B-CASC-2 — CostCap allows exactly max calls but docs imply < max
@pytest.mark.xfail(reason="known-red: отчёт охотника, премиса не верифицирована (решение Теро 2026-07-16)", strict=False)
def test_b_casc_2_cost_cap_allows_max_inclusive():
    cap = CostCap(max_paid_calls_per_day=3)
    assert cap.record_paid_attempt()
    assert cap.record_paid_attempt()
    # Expected: 3rd paid attempt should be blocked if max is exclusive upper bound.
    # Actual: 3rd call is allowed, meaning max is inclusive.
    assert not cap.record_paid_attempt(), "B-CASC-2: 3rd call was allowed despite max limit of 3"


# B-CASC-3 — Day bucket fails to reset when reset_day=None, permanent money-blocking
@pytest.mark.xfail(reason="known-red: отчёт охотника, премиса не верифицирована (решение Теро 2026-07-16)", strict=False)
def test_b_casc_3_day_bucket_reset_day_none_permanent_blocking():
    cap = CostCap(max_paid_calls_per_day=3)
    cap._count = 3
    cap._tripped = True
    cap._reset_day = None  # simulating uninitialized day bucket on restart
    
    cap.maybe_reset(100000.0)
    # Expected: Cap count and tripped state are reset to 0/False when maybe_reset runs on a fresh day.
    # Actual: It just sets reset_day to the current bucket and returns False, keeping the cap tripped permanently.
    assert not cap.tripped, "B-CASC-3: CostCap remained tripped after reset_day was None"


# B-CASC-4 — RPD reset mutes tier for 24h when failure at reset hour
def test_b_casc_4_rpd_reset_mutes_24h():
    cb = CircuitBreaker(tier_count=2, rpm_mute_s=10.0, rpd_reset_hour_utc=8)
    
    # Failure exactly at the reset hour (8:00 AM UTC)
    now = datetime(2026, 7, 15, 8, 0, 0, tzinfo=timezone.utc).timestamp()
    until = cb._next_rpd_reset(now)
    
    # Expected: Next reset should unmute immediately or within minutes.
    # Actual: It rolls to tomorrow's 8:00 AM, muting the tier for 24 hours.
    assert until < now + 86400, "B-CASC-4: Next reset rolled to tomorrow, muting tier for 24h"


# B-CORE-1 — TurnJournal fd leaks on exception during initialization
def test_b_core_1_journal_fd_leak_on_init_exception():
    # Expected: TurnJournal implements context manager __enter__/__exit__ to prevent fd leaks.
    # Actual: It lacks support for with-statements.
    assert hasattr(TurnJournal, "__enter__") and hasattr(TurnJournal, "__exit__"), "B-CORE-1: TurnJournal has no context manager support"


# B-CORE-2 — Thread never joined in record_commands.py on early exit
def test_b_core_2_thread_never_joined(tmp_path):
    phrases_file = tmp_path / "phrases.txt"
    phrases_file.write_text("hello\n", encoding="utf-8")
    
    original_thread = threading.Thread
    spawned_threads = []
    
    class MockThread(original_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._joined = False
            spawned_threads.append(self)
        def join(self, *args, **kwargs):
            self._joined = True
            return super().join(*args, **kwargs)
            
    with patch("sounddevice.InputStream", MagicMock()), \
         patch("soundfile.write", MagicMock()), \
         patch("builtins.input", return_value=""), \
         patch("threading.Thread", MockThread):
        record_session(str(phrases_file), str(tmp_path), bg="тихая", resume=False)
        
    assert len(spawned_threads) > 0
    # Expected: Spawned waiter daemon thread was joined properly.
    # Actual: Thread is left dangling.
    for t in spawned_threads:
        assert t._joined, "B-CORE-2: Spawned waiter thread was not joined"


# B-CORE-3 — TurnJournal._write fsync exception leaves _file inconsistent
def test_b_core_3_journal_write_fsync_exception(tmp_path):
    clock = SystemClock()
    journal = TurnJournal(tmp_path, clock)
    try:
        with patch("os.fsync", side_effect=OSError("Disk full")):
            journal.alert("STATUS_WITHOUT_GROUNDING")
            
        # Expected: TurnJournal sets self._closed = True to shut down gracefully after write failure.
        # Actual: It remains open and attempts to write to the corrupted file handle.
        assert journal._closed, "B-CORE-3: Journal stayed open after fsync error"
    finally:
        journal.close()


# B-CORE-4 — TTS cache tmp file cleanup races with process exit
def test_b_core_4_tts_cache_tmp_leak(tmp_path):
    tmp_file = tmp_path / ".stale_temp_file.uuid.tmp"
    tmp_file.write_bytes(b"temp")
    
    # Initialize cache
    TTSCache(tmp_path, "model", "voice")
    
    # Expected: TTSCache initialization scans root and cleans up any orphaned .tmp files.
    # Actual: Stale temp files remain on disk.
    assert not tmp_file.exists(), "B-CORE-4: Stale tmp files not cleaned on startup"


@pytest.mark.asyncio
async def test_b_core_5_subprocess_not_killed_on_cancelled_error(tmp_path):
    from synapse.pipeline.webrtc_server import build_web_app
    
    host = MagicMock()
    host.clock = SystemClock()
    host.cfg = SynapseConfig()
    
    # threads.get returns a thread mock
    mock_thread = MagicMock()
    host.threads.get.return_value = mock_thread
    # resolver (resolve_thread_root) returns a valid directory path
    host.resolve_thread_root.return_value = str(tmp_path)
    
    app = build_web_app(host)
    route = next(r for r in app.routes if getattr(r, "path", "") == "/api/threads/{thread_id}/diff")
    endpoint = route.endpoint
    
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(side_effect=asyncio.CancelledError())
    
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        try:
            await endpoint(thread_id="thread_1")
        except asyncio.CancelledError:
            pass
            
    # Expected: Subprocess is killed on non-TimeoutError exception (e.g. CancelledError).
    # Actual: Subprocess remains running as a zombie.
    assert mock_proc.kill.called, "B-CORE-5: Subprocess was not killed on CancelledError"


# B-CORE-6 — KoraRunner._active task leaks on RuntimeError during start()
def test_b_core_6_runner_active_task_leak_on_runtime_error():
    cfg = SynapseConfig()
    clock = SystemClock()
    store = MagicMock()
    
    runner = KoraRunner(cfg, store, MagicMock(), clock, MagicMock(), None)
    
    mock_active = MagicMock()
    mock_active.done.return_value = False
    runner._active = mock_active
    
    with patch("asyncio.create_task", side_effect=RuntimeError("No running loop")):
        runner.start("task_1", "text")
        
    # Expected: The reference to cancelled old active task is cleared or set to None.
    # Actual: runner._active still references the leaked mock task object.
    assert runner._active is None, "B-CORE-6: runner._active task reference was not cleared"


# B-CORE-8 — CostCap.reset() does not clear _reset_day
def test_b_core_8_cost_cap_reset_does_not_clear_reset_day():
    cap = CostCap(max_paid_calls_per_day=3)
    cap.record_paid_attempt(100000.0)
    assert cap._reset_day is not None
    cap.reset()
    # Expected: reset() should restore a clean state where _reset_day = None.
    # Actual: _reset_day remains set.
    assert cap._reset_day is None, "B-CORE-8: CostCap.reset() did not clear _reset_day"


# B-CORE-9 — _dispatch_tool json.dumps raises TypeError on non-serializable tool results
@pytest.mark.asyncio
async def test_b_core_9_dispatch_tool_raises_type_error_on_non_serializable():
    from synapse.dispatcher.tools import ToolCall
    bridge = MagicMock()
    bridge.clock = SystemClock()
    bridge.cfg = SynapseConfig()
    handlers = ToolHandlers(bridge, MagicMock())
    
    async def mock_submit(*args, **kwargs):
        return Path("/some/path")
        
    handlers.submit_task = mock_submit
    
    loop = DispatcherTurnLoop(MagicMock(), handlers, MagicMock(), MagicMock(), MagicMock(), SystemClock(), SynapseConfig())
    
    call = ToolCall(id="call_1", name="submit_task", arguments={"text": "hello"})
    history = []
    
    # Expected: _dispatch_tool should not crash with TypeError when serializing.
    # Actual: json.dumps raises TypeError on Path object.
    try:
        await loop._dispatch_tool(call, history)
        assert len(history) == 1
        assert "content" in history[0]
    except TypeError:
        pytest.fail("B-CORE-9: _dispatch_tool raised TypeError on non-serializable tool result")

