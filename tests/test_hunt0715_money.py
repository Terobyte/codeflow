# -*- coding: utf-8-sig -*-
"""Red tests proving B-CASC-5, B-DISP-8, B-DISP-9 from docs/bugs.md ("Hunt 2026-07-15
(вечер) -- Фаза 0: auth + money"). One test per bug ID. Each test is written so the
CORRECT (documented) behavior is the pass condition -- these are expected to FAIL against
the current (unfixed) tree and to flip green once the corresponding bug is fixed, with NO
change to the assertions themselves.

Touches no production code, no other test files, never bugs.md. Fixtures mirror:
- tests/test_hunt0714_a.py / test_hunt0714b_app.py (build_host + build_session_pipeline,
  calling the real strategy/switcher directly -- no live pipecat runtime) for B-CASC-5.
- tests/test_findings_reproduction.py (manual SimpleNamespace host + the real
  webrtc_server route endpoint, mocking only the LLM/provider -- a true externality) for
  B-DISP-8 and B-DISP-9.
"""
from __future__ import annotations

import asyncio
import json
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import TaskStatus, TaskStore
from synapse.config import SynapseConfig
from synapse.dispatcher.llm_client import CostCapBlocked
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.dispatcher.tools import KoraBridge, ToolCall, ToolHandlers
from synapse.journal import TurnJournal
from synapse.threads import ThreadStore


def _fake_cfg(tmp_path) -> SynapseConfig:
    """The standard fake-key cfg pattern used throughout the suite (test_hunt0714_a.py) --
    build_host() never dials the network with it."""
    return SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path), kora_workspace_dir=str(tmp_path / "ws"),
    )


# ---------------------------------------------------------------------------------------
# B-CASC-5 -- a failover-stuck non-zero tier escapes CostCap: only the failover attempt
# itself is counted (strategy._advance counts the SWITCH, not the turns after it); every
# SUCCESSFUL turn afterwards on that same stuck tier is invisible to the daily cap, because
# _CostCountingLLMSwitcher.push_frame only ever counted idx==0 and nothing re-invokes
# _advance without a fresh ErrorFrame.
# location: synapse/pipeline/app.py:555-565 x synapse/cascade/strategy.py:96-124.
#
# A generation == a turn: it advances only when a new LLMContextFrame reaches a
# GenerationStartHook -> GenerationGuard.start_generation() (context_guard.py:44, wired at
# app.py:1129-1130), NEVER on a tier switch. Driving that boundary is what makes this test
# about the STICKY TIER across turns rather than about double-counting inside one turn
# (which is B21, already fixed and asserted inline at turn 1 below).
# ---------------------------------------------------------------------------------------


async def test_b_casc_5_sticky_failover_tier_escapes_cost_cap(tmp_path):
    from pipecat.frames.frames import ErrorFrame, LLMFullResponseEndFrame
    from pipecat.processors.aggregators.llm_context import LLMContext

    from synapse.dispatcher.tools import ALL_SCHEMAS
    from synapse.pipeline.app import build_host, build_session_pipeline

    host = build_host(_fake_cfg(tmp_path))
    session = build_session_pipeline(host)
    switcher = session.llm_switcher
    strategy = switcher.strategy
    guard = session.generation_guard
    services = switcher.services  # [tier0 (paid), tier1 (paid)]
    context = LLMContext(tools=ALL_SCHEMAS)  # mirrors app.py:1036

    def succeed_on(tier_idx: int) -> LLMFullResponseEndFrame:
        """One completed generation on `tier_idx`: the ParallelPipeline filters let only the
        ACTIVE tier's LLMFullResponseEndFrame escape downstream, so one such frame == one
        completed paid attempt (app.py:533-535)."""
        end = LLMFullResponseEndFrame()
        end.processor = services[tier_idx]
        return end

    assert host.cost_cap.count == 0
    assert strategy.active_tier_index() == 0

    # --- Turn 1 (generation 1): tier0 errors -> failover to tier1, which then succeeds. -----
    guard.start_generation(context)
    await strategy.handle_error(ErrorFrame(error="rate limited"))
    assert strategy.active_tier_index() == 1, "setup: handle_error must fail over to tier1"
    assert host.cost_cap.count == 1, (
        "setup: _advance must count the tier1 failover attempt exactly once (strategy.py:103)"
    )
    # tier1 completes THIS SAME generation. _advance already counted this attempt, so the
    # success end-frame must NOT count it again -- that is B21, and this pins it stays fixed.
    await switcher.push_frame(succeed_on(1))
    assert host.cost_cap.count == 1, (
        "B21 regression: a failover-then-success within ONE generation must count exactly "
        f"once, not twice -- got count={host.cost_cap.count!r}; the cap would trip prematurely."
    )

    # --- Turns 2 and 3 (generations 2 and 3): healthy tier1, no ErrorFrame anywhere. --------
    # The ONE switcher built for this WebRTC connection stays on tier1 for the rest of the
    # call (fresh only on reconnect). Nothing re-invokes _advance without a fresh ErrorFrame,
    # and nothing but __init__ / _set_active_if_available (reached solely from _advance) ever
    # assigns _active_service -- no ManuallySwitchServiceFrame is sent by anyone -- so the
    # switcher is structurally stuck on tier1. Each turn below is a REAL billed paid call,
    # exactly what CostCap exists to gate (strategy.py:12: "CostCap gates every paid-tier
    # attempt"). Each starts its OWN generation, so `advanced_this_generation()` is False for
    # it and nothing legitimately pre-counted it.
    for _ in range(2):
        guard.start_generation(context)
        assert not strategy.advanced_this_generation(), (
            "setup: a fresh generation with no failover in it was never pre-counted by _advance"
        )
        await switcher.push_frame(succeed_on(1))

    assert host.cost_cap.count == 3, (
        "B-CASC-5: every paid attempt must count exactly once in its OWN generation -- 1 "
        "failover attempt (gen 1) + 2 healthy turns on the tier the switcher is stuck on "
        f"(gens 2, 3) = 3 -- got count={host.cost_cap.count!r}. Gating the success-side count on "
        "`active_tier_index()==0` counts only the INITIAL tier, so after one failover every "
        "later healthy turn on the sticky non-zero tier is invisible to CostCap and the daily "
        "max_paid_calls_per_day never trips for the rest of the connection (R9 reopened)."
    )


async def test_cost_counting_switcher_keeps_synapse_clock_after_pipecat_clock_attach(tmp_path):
    """Pipecat owns FrameProcessor._clock; billing must not reuse that attribute name."""
    from pipecat.frames.frames import LLMFullResponseEndFrame

    from synapse.pipeline.app import build_host, build_session_pipeline

    host = build_host(_fake_cfg(tmp_path))
    session = build_session_pipeline(host)
    switcher = session.llm_switcher

    # Live PipelineTask startup replaces FrameProcessor._clock with Pipecat's media clock,
    # whose API is get_time(), not Synapse Clock.now().  Simulate that ownership boundary.
    switcher._clock = object()
    end = LLMFullResponseEndFrame()
    end.processor = switcher.services[0]

    await switcher.push_frame(end)

    assert host.cost_cap.count == 1


# ---------------------------------------------------------------------------------------
# Shared fixture for B-DISP-8 / B-DISP-9: a manual (non-build_host) host stand-in -- same
# shape as tests/test_findings_reproduction.py's `_api_host` -- wiring a REAL
# DispatcherTurnLoop/TaskStore/ConfirmFlow/ToolHandlers/ThreadStore together, with only the
# LLM (the true network/provider externality) swapped for a scripted fake. The route
# (webrtc_server.api_thread_message) itself runs for real, unmodified.
# ---------------------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t


def _webrtc_or_skip():
    import pytest

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

    async def json(self):
        return self._body


def _build_api_host(tmp_path, llm):
    """Returns (host, store, committed). `committed` records every (task_id, text) pair
    ToolHandlers.submit_task's on_task_committed callback fires with -- the observable proof
    that a mutating tool call's effect landed for real, independent of the real KoraRunner
    (never constructed here -- kept out to avoid touching the Claude Agent SDK/subprocess
    layer; `on_task_committed` is the one true externality boundary submit_task crosses)."""
    from synapse.projects import ProjectStore

    clock = _FakeClock()
    cfg = _fake_cfg(tmp_path)
    threads = ThreadStore(clock, tmp_path / "threads")
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
                          cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    committed: list[tuple[str, str]] = []
    bridge = KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg,
                        on_task_committed=lambda task_id, text: committed.append((task_id, text)))
    handlers = ToolHandlers(bridge, journal)
    loop_obj = DispatcherTurnLoop(llm, handlers, confirm, store, journal, clock, cfg,
                                  thread_feed_reader=threads.read_feed)
    _stub_journal = SimpleNamespace(
        close=lambda: None, end_turn=lambda: None,
        check_grounding=lambda *a, **k: None, alert=MagicMock(),
    )
    host = SimpleNamespace(
        clock=clock, store=store, threads=threads,
        projects=ProjectStore(tmp_path / "projects.json"),
        text_loop=loop_obj, turn_lock=asyncio.Lock(),
        current_http_thread={"id": None}, voice_thread={"id": None},
        voice_project={"id": None},
        journal=_stub_journal, http_handlers=SimpleNamespace(end_turn=lambda: None),
    )
    return host, store, committed


# ---------------------------------------------------------------------------------------
# B-DISP-8 -- a degenerate/empty provider response (llm_client.py:99's literal ("", [])
# return on a content array with no text/tool_use blocks) reaches the HTTP route as a plain
# 200 {"reply": ""} with NO "degraded" key -- indistinguishable from a genuine answer, unlike
# the sibling CostCapBlocked/ProviderUnavailable paths right next to it (webrtc_server.py:
# 670-687 vs 703-705).
# ---------------------------------------------------------------------------------------


async def test_b_disp_8_empty_provider_response_not_silently_ok(tmp_path):
    webrtc_server = _webrtc_or_skip()

    class _EmptyProviderLLM:
        async def complete(self, messages, tools):
            # AnthropicLLMClient.complete's literal return (llm_client.py:99) when `content`
            # has no non-empty text block and no tool_use block.
            return "", []

    host, _store, _committed = _build_api_host(tmp_path, _EmptyProviderLLM())
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("тред")
    ep = _endpoint(app, "api_thread_message")

    resp = await ep(th.id, FakeRequest({"text": "привет"}))

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data.get("reply") == "", (
        "setup: the provider's degenerate response must reach the route as an empty reply"
    )
    assert data.get("degraded") is True, (
        "B-DISP-8: an empty/degenerate provider response was returned as a plain 200 "
        f"indistinguishable from a real answer (got {data!r}). P2's own principle (commit "
        "e5dd0a4: 'пустой ответ провайдера не даёт ok=True') was applied only to "
        "tools/bench_llm_providers.py, never to the production dispatcher route -- the "
        "sibling CostCapBlocked/ProviderUnavailable branches right next to this one "
        "(webrtc_server.py:670-687) both set degraded=True; this one (:703-705) sets neither."
    )


# ---------------------------------------------------------------------------------------
# B-DISP-9 -- pass 1 of ingest_user_turn dispatches a mutating tool call (submit_task) whose
# effect commits synchronously and for real (TaskStore -> RUNNING, on_task_committed fires).
# Pass 2's _complete then raises (GuardedLLMClient reserves/trips the cost cap on EVERY
# complete(), not just the first) -- the exception propagates straight past the already-
# committed effect (loop.py:236 dispatch precedes loop.py:237's raise) into the route's
# CostCapBlocked handler, which answers with the SAME context-free "nothing happened" cost-
# cap message regardless of what already committed this very turn.
# ---------------------------------------------------------------------------------------


async def test_b_disp_9_late_pass_failure_after_committed_tool_call_lies_about_state(tmp_path):
    webrtc_server = _webrtc_or_skip()

    class _CommitThenCostCapLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                # Pass 1: the model calls the mutating tool -- this is dispatched (and its
                # effect committed) by loop.py:236 BEFORE pass 2 below ever runs.
                return "", [ToolCall(name="submit_task", arguments={"text": "почини баг"}, id="c1")]
            # Pass 2 (loop.py:237): the SAME GuardedLLMClient reserves/checks the cost cap on
            # EVERY complete() call (llm_client.py:132), not just the turn's first -- a cap
            # that trips between passes surfaces here.
            raise CostCapBlocked()

    host, store, committed = _build_api_host(tmp_path, _CommitThenCostCapLLM())
    app = webrtc_server.build_web_app(host=host)
    th = host.threads.create("тред")
    ep = _endpoint(app, "api_thread_message")

    resp = await ep(th.id, FakeRequest({"text": "почини баг, пожалуйста"}))

    # Setup/premise checks -- these hold both before AND after any real fix; they establish
    # that the contradiction below is real, not a fixture artifact.
    assert committed, "setup: pass 1's submit_task must have actually committed a real task"
    assert store.task is not None and store.task.status == TaskStatus.RUNNING, (
        "setup: the committed task must genuinely be RUNNING when pass 2 fails"
    )
    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data.get("degraded") is True

    assert data.get("reply") != "Дневной лимит платных запросов исчерпан. Попробуйте позже.", (
        "B-DISP-9: the route answered with the SAME context-free 'daily limit exhausted, "
        f"nothing happened' message (got {data!r}) even though pass 1 of this very turn "
        f"already committed and started a real task (committed={committed!r}, "
        f"store.task.status={store.task.status!r}). The response contradicts the real state "
        "it is supposed to reflect -- a naive retry runs straight into {'error':'busy'} "
        "(app.py:416-417) with no link back to this 'failed' turn."
    )
