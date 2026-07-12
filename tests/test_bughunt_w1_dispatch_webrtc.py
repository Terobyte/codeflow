"""Bug-hunt wave-1 RED regression tests — dispatcher + WebRTC teardown/leak.

Frozen tree `a8dd919` (see bugs.md). One test per bug ID; each asserts the *post-fix*
behavior, so all three FAIL RED against the current (unfixed) code. They are written to fail
at their OWN assertion, not on import/collection/fixture.

  B5 (dispatcher/loop.py `_dispatch_tool`): tool dispatch by `getattr(self._handlers, name)`
     bypasses the ALL_SCHEMAS allowlist. A hallucinated/adversarial ToolCall whose name
     collides with a real, non-tool ToolHandlers method (`begin_turn`) resolves to that method
     → it is invoked (turn state mutated) and then crashes (`await` on its None return).
     Post-fix: unknown/non-allowlisted name → clean `{"error": ...}` result, method never run.

  B7 (pipeline/webrtc_server.py disconnect): the WebRTC disconnect handler (and `run_session`
     finally) cancels only the pipeline task, never `host.kora_runner.request_cancel()`. A
     tab-close mid-task orphans the Claude CLI child burning budget. Post-fix: disconnect /
     teardown cancels Kora.

  B8 (pipeline/webrtc_server.py `active_sessions`): a bare `POST /start` with no follow-up
     offer inserts an entry that is only ever popped in `run_session`'s finally (reached only
     via a completed offer). Unpaired starts (tab close, ICE fail, curl loop) leak forever.
     Post-fix: `active_sessions` is bounded (capped/evicted), not 1:1 with requests.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.dispatcher.mock_llm import MockLLM
from synapse.dispatcher.tools import ALL_SCHEMAS, KoraBridge, ToolCall, ToolHandlers
from synapse.journal import TurnJournal


# --------------------------------------------------------------------------------------------
# B5 — tool dispatch bypasses the ALL_SCHEMAS allowlist (dispatcher/loop.py `_dispatch_tool`)
# --------------------------------------------------------------------------------------------
def _make_loop(journal_dir: str) -> tuple[DispatcherTurnLoop, ToolHandlers]:
    cfg = SynapseConfig()
    clock = FakeClock(start=0.0)
    journal = TurnJournal(journal_dir, clock, session_id="b5")
    store = TaskStore(clock, journal_dir=None)
    classifier = KeywordClassifier(cfg.destructive_keywords)
    confirm_flow = ConfirmFlow(
        store, clock, classifier, journal,
        cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s,
    )
    bridge = KoraBridge(store=store, confirm_flow=confirm_flow, clock=clock, cfg=cfg)
    handlers = ToolHandlers(bridge, journal)
    loop = DispatcherTurnLoop(MockLLM(), handlers, confirm_flow, store, journal, clock, cfg)
    return loop, handlers


async def test_b5_unknown_tool_name_colliding_with_real_method_is_rejected(tmp_path):
    loop, handlers = _make_loop(str(tmp_path))

    # `begin_turn` is a real ToolHandlers method but is NOT one of the four+one dispatcher tools.
    tool_names = {schema.name for schema in ALL_SCHEMAS}
    assert "begin_turn" not in tool_names, "premise: begin_turn must not be a real tool"

    # Establish a known, legit turn id via the real begin_turn so we can detect a hijack.
    handlers.begin_turn("turn-legit")
    baseline_turn = handlers._current_turn_id

    # A hallucinated/adversarial tool call whose name collides with the real method.
    call = ToolCall(name="begin_turn", arguments={"turn_id": "HIJACK"})

    crashed: Exception | None = None
    result = None
    try:
        result = await loop._dispatch_tool(call)
    except Exception as exc:  # current code: begin_turn() runs, then `await None` → TypeError
        crashed = exc

    # POST-FIX: a name not in the ALL_SCHEMAS allowlist is rejected cleanly, never dispatched.
    assert crashed is None, (
        f"dispatch of a non-tool method must not crash; got {crashed!r}"
    )
    assert isinstance(result, dict) and "error" in result, (
        f"expected an unknown-tool error result, got {result!r}"
    )
    # ...and the real begin_turn() must never have run (turn state not hijacked).
    assert handlers._current_turn_id == baseline_turn, (
        "real ToolHandlers.begin_turn was invoked via getattr — turn state hijacked"
    )


# --------------------------------------------------------------------------------------------
# Shared closure introspection for the webrtc_server tests. `run_session` / `active_sessions`
# are closures local to build_web_app; we reach them the way the fix would have to preserve.
# --------------------------------------------------------------------------------------------
def _endpoint(app, name):
    for route in app.routes:
        ep = getattr(route, "endpoint", None)
        if ep is not None and getattr(ep, "__name__", None) == name:
            return ep
    raise AssertionError(f"route endpoint {name!r} not found")


def _cells(fn) -> dict:
    return dict(zip(fn.__code__.co_freevars, [c.cell_contents for c in (fn.__closure__ or ())]))


# B7 (disconnect → cancel Kora) was REJECTED by senior review as by-design, not a bug: §2.7 /
# slice-0 intent is "task survives a reconnect (drop tab → reconnect → same task)". Cancelling
# Kora on disconnect would terminalize the logical task to FAILED and break that v1 DoD. The
# orphaned-budget concern is real but bounded (kora_deadline_s + max_budget_usd); a grace-based
# cancel-if-no-reconnect is the M1.1 refinement (needs the reconnect-reattach path). No test.


# --------------------------------------------------------------------------------------------
# B8 — active_sessions must not grow unbounded on bare /start (pipeline/webrtc_server.py)
# --------------------------------------------------------------------------------------------
async def test_b8_bare_start_does_not_leak_active_sessions():
    pytest.importorskip("aiortc")
    pytest.importorskip("cv2")
    pytest.importorskip("fastapi")
    try:
        from synapse.pipeline import webrtc_server
    except (ImportError, RuntimeError) as e:
        pytest.skip(f"webrtc deps/prebuilt UI unavailable: {e}")

    app = webrtc_server.build_web_app(host=object())
    start_bot = _endpoint(app, "start_bot")
    active = _cells(start_bot).get("active_sessions")
    if active is None:
        # A fix that dropped the structure entirely also satisfies "does not leak".
        return

    class FakeRequest:
        async def json(self):
            return {}

    n = 1000
    for _ in range(n):
        await start_bot(FakeRequest())  # bare /start, never followed by an offer

    # POST-FIX: bounded (cap/evict), NOT one retained entry per unpaired /start.
    assert len(active) < n, (
        f"active_sessions grew unbounded: {len(active)} entries after {n} unpaired /start calls"
    )
