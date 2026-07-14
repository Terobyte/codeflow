"""B42/B43/B44/B47 regression -- bugs.md § «2026-07-14 -- hands-on browser + fan-out UI hunt».
Each `xfail(strict=True)` reds on its OWN assertion under `--runxfail`, matching the documented
expected-vs-actual, and keeps the normal suite green (xfailed) until the real fix lands. GREEN
invariant-companions guard contracts the future fix must not break.

- B42 (CRIT, app.py:845-858 `_flush_voice_context` + pipecat's commit-only-on-End-frame,
  `context_guard.py` `push_aggregation`): a dispatcher answer that started streaming (already
  spoken through TTS -- the assistant aggregator sits downstream of TTS, so a populated
  `_aggregation` means the words were already audible) but never reached
  `LLMFullResponseEndFrame` is invisible to `_flush_voice_context` (it only reads COMMITTED
  `context.messages`). `on_client_disconnected` (webrtc_server.py:175-195) calls `flush()` then
  `task.cancel()` -- neither step ever calls `push_aggregation()` on the pending aggregator, so
  the words the user just heard never reach the thread feed. Proven by driving the REAL
  `session.flush_voice_feed` callback (the exact one `on_client_disconnected` calls) against the
  REAL aggregator instance with a populated, uncommitted `_aggregation` -- no full
  aiortc/PipelineTask handshake needed: the loss is already complete before `task.cancel()` ever
  runs, since nothing upstream of it ever commits the pending tail.

- B43 (MAJOR, webrtc_server.py:687): `POST /api/active-thread` rebinds `host.voice_thread["id"]`
  unconditionally, even while a voice connection is live (`host._output_task` bound via
  `bind_output` -- M1 slice 2's own "a connection is live" marker, see app.py `SynapseHost`
  docstring). The full client-side reconnect race (app.js:226-234/898-927, silent
  `client=null` during ICE renegotiation) is not reachable from a pytest route test -- there is
  no aiortc handshake timing here -- but the SERVER-side half of the bug (the route provides no
  liveness gate at all) is directly provable: bind a live output task, then hit the route for a
  different thread and show it rebinds anyway.

- B44 (MAJOR, app.py:832 `context = LLMContext(tools=ALL_SCHEMAS)`): every (re)connect gets a
  fresh EMPTY LLM context, never seeded from the thread's feed history -- contrast
  `dispatcher/loop.py::_history_for`, which rehydrates the HTTP path's history from
  `thread_feed_reader` on a cold cache miss. A thread with prior user/assistant feed entries
  reconnecting to voice starts the dispatcher with total amnesia, even though the feed (and the
  UI showing it) has the whole conversation.

- B47 (MAJOR, app.py:613-626 `_on_task_committed` / 662-669 `_http_task_committed`): neither
  direct-dispatch commit path calls `threads.set_stage`; `_run_finished` (app.py:285-297) only
  ever advances stage `code`->`done`. A thread whose only activity was a direct-dispatch
  `submit_task`/`confirm_task` never leaves stage="collect", even after the task completes --
  observed live by Tero (helloworld.txt task completed, badge stuck at "СБОР").
"""
from __future__ import annotations

import pytest

from test_live_to_chat import _context_of, _voice_host_or_skip


# =============================================================================================
# B42 -- mid-speech (uncommitted) dispatcher answer is lost on hangup teardown
# =============================================================================================


async def test_B42_mid_speech_answer_survives_hangup_flush(tmp_path):
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMAssistantAggregator,
        LLMUserAggregator,
    )
    from pipecat.utils.string import TextPartForConcatenation

    host, session, stt, handler = _voice_host_or_skip(tmp_path)

    await handler(stt, "первый вопрос")
    tid = host.voice_thread["id"]
    assert tid is not None

    aggregator = next(
        p
        for p in session.pipeline.processors
        if isinstance(p, LLMAssistantAggregator) and not isinstance(p, LLMUserAggregator)
    )

    # Simulate: TTS already streamed these words downstream (aggregator._aggregation reflects
    # what was really spoken -- see context_guard.py's own module docstring), but
    # LLMFullResponseEndFrame never arrived (hangup interrupted the answer mid-stream) --
    # push_aggregation() is NEVER called for this generation. Contrast the B25 test
    # (test_bugs_0714_voiceflush.py), which drives push_aggregation() to prove the commit-time
    # flush; here we deliberately do NOT commit, matching a real mid-speech hangup.
    aggregator._aggregation = [
        TextPartForConcatenation("этот ответ юзер уже слышал", includes_inter_part_spaces=True)
    ]

    # Setup sanity: the words are genuinely NOT committed to the live context yet -- exactly
    # the state a real mid-speech hangup leaves the aggregator in.
    ctx_msgs = _context_of(session).get_messages()
    assert not any(m.get("role") == "assistant" for m in ctx_msgs), (
        "precondition: the partial answer must not be committed to context yet"
    )

    # Drive the REAL teardown call `on_client_disconnected` makes BEFORE `task.cancel()`
    # (webrtc_server.py:180-185) -- exactly `session.flush_voice_feed`, no more, no less.
    session.flush_voice_feed()

    # B42 DESIRED behaviour: words the user already heard must survive the hangup -- the fix
    # flushes the aggregator's pending tail (push_aggregation() or equivalent) around teardown.
    # Today nothing in the disconnect path ever touches the pending (uncommitted) aggregation,
    # so it is silently discarded forever.
    feed = host.threads.read_feed(tid)
    assert any(
        e["kind"] == "assistant" and e.get("text") == "этот ответ юзер уже слышал" for e in feed
    ), (
        "B42: a dispatcher answer already spoken (TTS-streamed) before hangup must reach the "
        f"thread feed via disconnect teardown -- feed still holds only "
        f"{[e['kind'] for e in feed]!r}"
    )


async def test_B42_invariant_fully_committed_answer_survives_hangup_flush(tmp_path):
    """GREEN companion: the disconnect flush mechanism ITSELF is not broken -- an answer that
    DID fully commit (normal end-of-turn, no interrupt) before disconnect must still reach the
    feed. B42 is scoped to the uncommitted-tail case only; a fix must not regress this."""
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMAssistantAggregator,
        LLMUserAggregator,
    )
    from pipecat.utils.string import TextPartForConcatenation

    host, session, stt, handler = _voice_host_or_skip(tmp_path)

    await handler(stt, "первый вопрос")
    tid = host.voice_thread["id"]

    aggregator = next(
        p
        for p in session.pipeline.processors
        if isinstance(p, LLMAssistantAggregator) and not isinstance(p, LLMUserAggregator)
    )
    aggregator._aggregation = [
        TextPartForConcatenation("ответ полностью сказан", includes_inter_part_spaces=True)
    ]
    committed = await aggregator.push_aggregation()
    assert committed == "ответ полностью сказан"

    session.flush_voice_feed()

    feed = host.threads.read_feed(tid)
    assert any(
        e["kind"] == "assistant" and e.get("text") == "ответ полностью сказан" for e in feed
    ), "a fully committed answer must survive the disconnect flush"


# =============================================================================================
# B43 -- /api/active-thread rebinds voice_thread while a voice connection is live
# =============================================================================================


def _webrtc_route_deps_or_skip():
    pytest.importorskip("aiortc")
    pytest.importorskip("cv2")
    pytest.importorskip("fastapi")
    try:
        from synapse.pipeline.webrtc_server import build_web_app
    except (ImportError, RuntimeError) as e:
        pytest.skip(f"webrtc deps/prebuilt UI unavailable: {e}")
    return build_web_app


def _voice_route_host(tmp_path):
    build_web_app = _webrtc_route_deps_or_skip()
    from synapse.clock import FakeClock
    from synapse.config import SynapseConfig
    from synapse.pipeline.app import build_host

    cfg = SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path / "j"), kora_workspace_dir=str(tmp_path / "ws"),
        kora_enabled=False,
    )
    host = build_host(cfg, clock=FakeClock(0.0))
    app = build_web_app(host)
    return host, app


class _FakeLiveOutputTask:
    """Duck-typed per-connection output task (M1 slice 2, see `SynapseHost`'s class docstring
    in app.py): `has_finished()` is the ONLY surface the host actually touches. A task bound via
    `host.bind_output(...)` with `has_finished() == False` IS the host's own "a voice connection
    is live" marker -- the same one `push_speak_frame`/`speak` consult."""

    def has_finished(self) -> bool:
        return False


@pytest.mark.xfail(
    reason="B43: active-thread rebinds voice_thread even while a voice call is live", strict=True
)
def test_B43_active_thread_must_not_rebind_while_voice_session_live(tmp_path):
    from starlette.testclient import TestClient

    host, app = _voice_route_host(tmp_path)
    t1 = host.threads.create("звонок")
    t2 = host.threads.create("другой тред")
    host.voice_thread["id"] = t1.id
    host.bind_output(_FakeLiveOutputTask())  # a real voice connection is live, mid-call

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/active-thread",
        json={"id": t2.id},
        headers={"content-type": "application/json", "origin": "http://testserver"},
    )

    assert resp.status_code == 200  # setup sanity: the request itself is well-formed/accepted
    assert host.voice_thread["id"] == t1.id, (
        "B43: /api/active-thread must not rebind the live voice call to another thread -- "
        f"voice_thread rebound to {host.voice_thread['id']!r} while a live output task was "
        "bound (webrtc_server.py:687 rebinds unconditionally, with no liveness gate at all)"
    )


def test_B43_invariant_active_thread_rebinds_when_no_voice_session_live(tmp_path):
    """GREEN companion: the normal (no live call) case must keep working -- navigating threads
    from the home screen with nothing connected must still rebind voice_thread. A fix gating on
    liveness must not break this, the overwhelmingly common path."""
    from starlette.testclient import TestClient

    host, app = _voice_route_host(tmp_path)
    t2 = host.threads.create("другой тред")
    assert host.voice_thread["id"] is None  # setup sanity: no live call bound at construction

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/active-thread",
        json={"id": t2.id},
        headers={"content-type": "application/json", "origin": "http://testserver"},
    )

    assert resp.status_code == 200
    assert host.voice_thread["id"] == t2.id


# =============================================================================================
# B44 -- reconnect builds a fresh, empty LLM context; never seeded from thread feed history
# =============================================================================================


async def test_B44_reconnect_seeds_context_from_thread_history(tmp_path):
    pytest.importorskip("pipecat.services.deepgram.flux.stt")
    from synapse.config import SynapseConfig
    from synapse.pipeline.app import build_host, build_session_pipeline

    cfg = SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path),
    )
    host = build_host(cfg)

    # A conversation that happened BEFORE this connection (an earlier call segment, or the HTTP
    # chat channel writing into the same thread) -- what the user experiences as "continuing the
    # same conversation" on reconnect.
    th = host.threads.create("тред с историей")
    host.threads.append_feed(th.id, {"ts": 0.0, "kind": "user", "text": "какая сейчас стадия"})
    host.threads.append_feed(
        th.id, {"ts": 0.0, "kind": "assistant", "text": "сейчас идёт сбор требований"}
    )
    host.voice_thread["id"] = th.id  # reconnecting INTO this already-conversed thread

    session = build_session_pipeline(host)
    ctx_msgs = _context_of(session).get_messages()

    assert any(
        m.get("role") == "user" and "какая сейчас стадия" in str(m.get("content"))
        for m in ctx_msgs
    ), (
        "B44: (re)connect must seed the LLM context from the thread's prior feed history "
        "(dispatcher/loop.py::_history_for does exactly this for the HTTP path) -- got a fresh "
        f"empty context instead: messages={ctx_msgs!r}"
    )


async def test_B44_invariant_fresh_thread_with_no_history_gets_no_injected_turns(tmp_path):
    """GREEN companion: a brand-new thread with NO prior feed history must not have anything
    fabricated into its context -- the future seeding fix must only rehydrate REAL history, not
    inject placeholder turns for a thread that never had any."""
    pytest.importorskip("pipecat.services.deepgram.flux.stt")
    from synapse.config import SynapseConfig
    from synapse.pipeline.app import build_host, build_session_pipeline

    cfg = SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path),
    )
    host = build_host(cfg)
    th = host.threads.create("свежий тред")
    host.voice_thread["id"] = th.id

    session = build_session_pipeline(host)
    ctx_msgs = _context_of(session).get_messages()

    assert not any(m.get("role") in ("user", "assistant") for m in ctx_msgs), (
        f"a fresh thread with no feed history must not get fabricated turns: {ctx_msgs!r}"
    )


# =============================================================================================
# B47 -- direct-dispatch commit path never advances thread.stage past "collect"
# =============================================================================================


class ResultMessage:
    """Fake SDK terminal message -- the class NAME (exactly "ResultMessage") is what
    `_message_to_events` duck-types on (`type(msg).__name__`), never isinstance. Convention
    duplicated from tests/test_kora.py, no cross-import of test fixtures per repo precedent."""

    def __init__(self, is_error: bool) -> None:
        self.is_error = is_error
        self.num_turns = 1
        self.total_cost_usd = 0.001


class _FakeCompletingClient:
    """Minimal fake async-context SDK client that immediately yields one successful terminal
    message -- enough to drive a real `KoraRunner._run` to TaskStatus.COMPLETED with no network."""

    def __init__(self, opts) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt, session_id="default"):
        pass

    def receive_response(self):
        async def gen():
            yield ResultMessage(is_error=False)

        return gen()


@pytest.mark.xfail(
    reason="B47: direct-dispatch commit never advances thread.stage past 'collect'",
    strict=True,
)
async def test_B47_direct_dispatch_commit_never_advances_stage(tmp_path):
    from synapse.bridge.state import TaskStatus
    from synapse.clock import FakeClock
    from synapse.config import SynapseConfig
    from synapse.pipeline.app import build_host

    cfg = SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path / "j"), kora_workspace_dir=str(tmp_path / "ws"),
    )
    host = build_host(cfg, clock=FakeClock(0.0))
    assert host.kora_runner is not None  # setup sanity: kora_enabled defaults True
    host.kora_runner._client_factory = _FakeCompletingClient

    # Real direct-dispatch entry point (dispatcher tool `submit_task`, voice channel): a
    # non-destructive request commits straight to RUNNING and fires `_on_task_committed`
    # (app.py:613-626) -- the exact path B47 names.
    host.handlers.begin_turn("t1")
    res = await host.handlers.submit_task(text="создай helloworld.txt")
    assert res["outcome"] == "committed"  # setup sanity: non-destructive text commits directly

    await host.kora_runner._active  # drain the fire-and-forget run to completion

    tid = host.voice_thread["id"]
    assert tid is not None  # setup sanity: direct-dispatch auto-created the voice thread
    th = host.threads.get(tid)
    assert host.store.task.status == TaskStatus.COMPLETED  # setup sanity: the task really finished
    assert th.last_outcome == "completed"  # setup sanity: _run_finished really fired

    assert th.stage != "collect", (
        "B47: a completed direct-dispatch task must advance thread.stage past 'collect' -- "
        f"stage stuck at {th.stage!r} even though the task COMPLETED (bug: "
        "_on_task_committed/_run_finished never call set_stage for the direct-dispatch path, "
        "only the gated code->done transition does)"
    )
