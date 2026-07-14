# -*- coding: utf-8-sig -*-
"""Red tests proving bugs B01, B02, B04, B06, B07, B08, B10 from docs/bugs.md
("Hunt 2026-07-14"). One test per bug ID. Every test is written so the CORRECT
(documented) behavior is the pass condition -- these are expected to FAIL against the
current (unfixed) tree and flip green once the corresponding bug is fixed, with no
change to the assertions themselves.

Touches no production code. Mocks only true externalities (network/SDK keys are fake
strings that are never dialed -- build_host()/build_session_pipeline() never make a
network call in these tests).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import EventClass, KoraEvent, TaskStatus, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.dispatcher.tools import KoraBridge, ToolHandlers
from synapse.journal import TurnJournal


def _fake_cfg(tmp_path) -> SynapseConfig:
    """The standard fake-key cfg pattern used throughout the suite (test_stages.py,
    test_pipeline_smoke.py) -- build_host() never dials the network with it."""
    return SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path), kora_workspace_dir=str(tmp_path / "ws"),
    )


class _FakeKoraRunner:
    """Stub KoraRunner (same shape as test_stages.py's _FakeRunner): records start(...)
    calls, no SDK/network involved."""

    def __init__(self) -> None:
        self.starts: list[tuple] = []

    def start(self, task_id, text, spec) -> None:
        self.starts.append((task_id, text, spec))


def _gate_host(tmp_path):
    """Real SynapseHost via build_host (fake keys), kora_runner swapped for a stub so
    gate_action's send_to_kora/write_code launches are observable without touching the
    Claude Agent SDK."""
    from synapse.pipeline.app import build_host

    host = build_host(_fake_cfg(tmp_path))
    host.kora_runner = _FakeKoraRunner()
    return host


# ---------------------------------------------------------------------------------------
# B01 -- speak() marks the SpeakLedger "spoken" before TTS delivery is confirmed; a
# dropped critical (push_speak_frame raises) is never re-alerted.
# ---------------------------------------------------------------------------------------


async def test_B01_failed_speak_injection_must_leave_critical_unspoken(tmp_path):
    from synapse.pipeline.app import build_host

    host = build_host(_fake_cfg(tmp_path))

    class _RaisingOutputTask:
        """Duck-typed live output task whose queue_frame raises -- the exact scenario
        the code's own B9 comment describes (output task torn down mid-emit)."""

        def has_finished(self) -> bool:
            return False

        async def queue_frame(self, frame) -> None:
            raise RuntimeError("output task torn down mid-emit")

    host.bind_output(_RaisingOutputTask())

    ev = KoraEvent(
        id="e1", type="task_completed", cls=EventClass.CRITICAL, payload={},
        speak_text="готово", ts=host.clock.now(),
    )
    host.speak_ledger.register_critical(ev)

    before_tasks = set(asyncio.all_tasks())
    host.speak("готово")
    new_tasks = set(asyncio.all_tasks()) - before_tasks
    assert new_tasks, "speak() should have scheduled the push_speak_frame injection as a task"
    # Drive the scheduled injection (and its done-callback) to completion before asserting.
    await asyncio.gather(*new_tasks, return_exceptions=True)

    assert host.speak_ledger._pending["e1"].spoken is False, (
        "B01: a SPEAK whose injection raised was recorded as delivered anyway "
        f"(spoken={host.speak_ledger._pending['e1'].spoken!r}) -- register_speak_text() marks "
        "the ledger BEFORE delivery is confirmed and the failure callback only logs."
    )
    alerts = host.speak_ledger.check(now=host.clock.now() + 9999, window_s=1.0)
    assert any(
        kind == "CRITICAL_WITHOUT_SPEAK" and detail.get("event_id") == "e1"
        for kind, detail in alerts
    ), f"B01: Р-15г watchdog did not fire for the dropped critical (alerts={alerts!r})"


# ---------------------------------------------------------------------------------------
# B02 -- DispatcherTurnLoop shares one unlocked history list per thread; concurrent turns
# corrupt history and cross-deliver replies.
# ---------------------------------------------------------------------------------------


class _OrderedLLM:
    """Deterministic sync point: the FIRST call to complete() sets `entered_first` (so a
    concurrent second caller can synchronize on it) and blocks on `unblock_first` until the
    test releases it. The SECOND call returns immediately. This forces the exact
    interleaving the ledger reproduces without any sleep-and-hope loop."""

    def __init__(self, replies: list[tuple[str, list]]) -> None:
        self.entered_first = asyncio.Event()
        self.unblock_first = asyncio.Event()
        self.replies = replies
        self.calls = 0

    async def complete(self, messages, tools):
        idx = self.calls
        self.calls += 1
        if idx == 0:
            self.entered_first.set()
            await self.unblock_first.wait()
        return self.replies[idx]


def _make_dispatcher_loop(tmp_path, llm):
    clock = FakeClock()
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(
        store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
        cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s,
    )
    handlers = ToolHandlers(KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg), journal)
    return DispatcherTurnLoop(llm, handlers, confirm, store, journal, clock, cfg)


async def test_B02_concurrent_turns_same_thread_corrupt_shared_history(tmp_path):
    llm = _OrderedLLM([("reply-A", []), ("reply-B", [])])
    loop = _make_dispatcher_loop(tmp_path, llm)

    async def turn_a():
        _, reply = await loop.ingest_user_turn("msg A", thread_id="thread-1")
        return reply

    async def turn_b():
        # Deterministic sync point: only start once A is provably blocked inside its LLM
        # call (call #1) -- not a sleep-based guess.
        await llm.entered_first.wait()
        _, reply = await loop.ingest_user_turn("msg B", thread_id="thread-1")
        return reply

    task_a = asyncio.create_task(turn_a())
    task_b = asyncio.create_task(turn_b())

    reply_b = await task_b  # completes without ever touching unblock_first (call #2 doesn't block)
    llm.unblock_first.set()
    reply_a = await task_a

    history = loop._history_for("thread-1")
    roles = [m["role"] for m in history]

    assert reply_a == "reply-A" and reply_b == "reply-B", (
        f"B02: a caller received the other turn's reply (reply_a={reply_a!r}, reply_b={reply_b!r})"
    )
    assert roles == ["user", "assistant", "user", "assistant"], (
        "B02: concurrent turns on the same thread corrupted the shared history -- expected "
        f"strict user/assistant alternation, got roles={roles!r} (history={history!r})"
    )


# ---------------------------------------------------------------------------------------
# B04 -- CostCap.record_paid_attempt is only reached on the error path, so a successful
# paid-tier turn never counts against max_paid_calls_per_day.
# ---------------------------------------------------------------------------------------


async def test_B04_successful_paid_turn_must_count_against_cost_cap(tmp_path):
    from pipecat.frames.frames import LLMFullResponseEndFrame

    from synapse.pipeline.app import build_host, build_session_pipeline

    host = build_host(_fake_cfg(tmp_path))
    session = build_session_pipeline(host)
    switcher = session.llm_switcher
    active = switcher.strategy.active_service  # tier1 (OpenRouter), paid=True by default

    assert host.cost_cap.count == 0

    # Simulate the active (paid) tier1 service finishing ONE turn successfully -- no
    # ErrorFrame anywhere in this flow, exactly the real "the common case" path the ledger
    # describes. LLMFullResponseEndFrame is the real frame pipecat's LLM services push
    # (downstream) when a generation completes without error.
    end_frame = LLMFullResponseEndFrame()
    end_frame.processor = active
    await switcher.push_frame(end_frame)

    assert host.cost_cap.count == 1, (
        "B04: a successful paid-tier1 turn was not counted against CostCap (R9) -- "
        f"count={host.cost_cap.count!r}. record_paid_attempt() is reachable only via "
        "handle_error()/_advance(), never on the success path."
    )


# ---------------------------------------------------------------------------------------
# B06 -- send_to_kora/write_code gate branches skip the stage guard `revise` has: an
# illegal-transition ValueError escapes gate_action uncaught.
# ---------------------------------------------------------------------------------------


async def test_B06_illegal_stage_transition_must_not_escape_as_valueerror(tmp_path):
    host = _gate_host(tmp_path)
    t = host.threads.create("x")
    host.threads.set_request(t.id, "запрос")
    # Thread stays at its default stage "collect". send_to_kora's non-fast target stage is
    # "spec_plan", which is NOT a legal transition from "collect" (only "propose" is) --
    # `revise` guards this exact failure mode with try/except ValueError, send_to_kora doesn't.
    try:
        res = await host.gate_action(t.id, "send_to_kora", confirm=True)
    except ValueError as exc:
        pytest.fail(
            "B06: an illegal stage transition raised an uncaught ValueError out of "
            f"gate_action instead of returning a structured error dict: {exc!r}"
        )
    assert res == {"error": "illegal_stage"}, f"B06: expected illegal_stage error, got {res!r}"


# ---------------------------------------------------------------------------------------
# B07 -- `revise` doesn't reset last_outcome/plan file, so write_code can launch a stale
# plan (from request A) against a newly-proposed, different request (B).
# ---------------------------------------------------------------------------------------


async def test_B07_write_code_refuses_stale_plan_after_revise_and_new_propose(tmp_path):
    host = _gate_host(tmp_path)
    t = host.threads.create("x")
    host.threads.set_stage(t.id, "propose")
    host.threads.set_request(t.id, "запрос A")

    res = await host.gate_action(t.id, "send_to_kora", confirm=True)
    assert res.get("ok") is True and host.threads.get(t.id).stage == "spec_plan"

    # Simulate request A's spec_plan run finishing successfully: Kora wrote the plan file
    # and the runner's on_run_finished callback fires (mirrors `_run_finished`/`start_task`).
    root = Path(host.cfg.kora_workspace_dir)
    (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "plans" / f"{t.id}.md").write_text("план A", encoding="utf-8")
    host.store.set_task_status(TaskStatus.COMPLETED)
    host._run_finished(t.id, "completed")
    assert host.threads.get(t.id).last_outcome == "completed"

    # Правки → сбор (revise), then propose a DIFFERENT request B. No new spec_plan run has
    # happened for B -- the on-disk plan is still A's, and last_outcome is stale w.r.t. B.
    res = await host.gate_action(t.id, "revise")
    assert res.get("ok") is True and host.threads.get(t.id).stage == "collect"
    host.threads.set_stage(t.id, "propose")
    host.threads.set_request(t.id, "запрос B")

    res = await host.gate_action(t.id, "write_code", confirm=True)
    assert res.get("error") == "stale_plan", (
        "B07: write_code launched request A's stale plan under the new request B instead of "
        f"refusing with stale_plan, got {res!r}"
    )


# ---------------------------------------------------------------------------------------
# B08 -- turn_lock releases before ingest_user_turn runs, so a concurrent turn's begin_turn
# steals TurnJournal._current from an in-flight turn (single shared "current" slot).
# ---------------------------------------------------------------------------------------


def test_B08_concurrent_begin_turn_must_not_steal_the_voice_turn_record(tmp_path):
    clock = FakeClock()
    journal = TurnJournal(str(tmp_path / "j"), clock)

    voice_record = journal.begin_turn("voice transcript")
    # The window the ledger describes: turn_lock is released before ingest_user_turn runs,
    # so an HTTP begin_turn can land here, after the voice turn started but before its
    # tool-call tail runs.
    http_record = journal.begin_turn("http transcript")
    # The voice turn's own tool-call tail, still logically part of the voice turn.
    journal.record_tool_call("get_task_status", {}, {"ok": True})

    assert voice_record.tool_calls == [
        {"name": "get_task_status", "arguments": {}, "result": {"ok": True}}
    ], (
        "B08: the voice turn's tool call landed on the wrong TurnRecord -- "
        f"voice_record.tool_calls={voice_record.tool_calls!r}, "
        f"http_record.tool_calls={http_record.tool_calls!r} (TurnJournal._current is a "
        "single shared slot with no per-task isolation)"
    )


# ---------------------------------------------------------------------------------------
# B10 -- mutating /api/* POST routes call `await request.json()` unguarded -> 500 on
# malformed JSON instead of the diagnosable 400 pattern `/start` already uses.
# ---------------------------------------------------------------------------------------


def test_B10_malformed_json_post_returns_400_not_500(tmp_path):
    pytest.importorskip("aiortc")
    pytest.importorskip("cv2")
    pytest.importorskip("fastapi")
    try:
        from synapse.pipeline.webrtc_server import build_web_app
    except (ImportError, RuntimeError) as e:
        pytest.skip(f"webrtc deps/prebuilt UI unavailable: {e}")
    from starlette.testclient import TestClient

    host = MagicMock()
    app = build_web_app(host)
    # raise_server_exceptions=False: observe the real HTTP status the server would send a
    # client instead of pytest re-raising the escaped JSONDecodeError as a Python exception.
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/api/projects",
        content=b"{not valid json",
        # CSRF-satisfying headers: JSON content-type + Origin whose netloc matches Host
        # (TestClient's default Host is "testserver").
        headers={"content-type": "application/json", "origin": "http://testserver"},
    )

    assert resp.status_code == 400, (
        "B10: malformed JSON body on a mutating /api/* route must 400 like /start does, "
        f"got status={resp.status_code!r} body={resp.text!r}"
    )
