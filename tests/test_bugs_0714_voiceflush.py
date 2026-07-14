"""B25 regression — dispatcher answer must reach the thread feed at answer-COMMIT, not one turn
late / only on disconnect. Formerly a RED xfail-strict repro; the B25 fix (2026-07-14) turned it
GREEN and the marker was removed.

Root cause (docs/bugs.md B25): `_flush_voice_context` was event-wise attached only to the START
of the NEXT `_on_end_of_turn` (context diff) and to `flush_voice_feed` on disconnect — never to
the moment the answer is committed. pipecat's `LLMAssistantAggregator.push_aggregation` puts
`{"role":"assistant","content":<str>}` into the live context exactly when the answer is fully
spoken (the aggregator sits downstream of TTS). The fix adds a post-commit `on_commit` callback
to GuardedAssistantAggregator that calls `_flush_voice_context` right there.

This test drives the REAL guarded assistant aggregator to a commit — the same code path prod
uses — WITHOUT a following user turn and WITHOUT `flush_voice_feed()`, then asserts the answer is
already in the thread feed at commit time.

Reuses the harness in test_live_to_chat.py (`_voice_host_or_skip`, `_context_of`): real
build_host + build_session_pipeline, real STT/handler, one shared live LLMContext.
"""
from __future__ import annotations

from test_live_to_chat import _context_of, _voice_host_or_skip


async def test_dispatcher_answer_reaches_feed_at_commit(tmp_path):
    # importorskip lives in _voice_host_or_skip; import pipecat symbols only after it ran.
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMAssistantAggregator,
        LLMUserAggregator,
    )
    from pipecat.utils.string import TextPartForConcatenation

    host, session, stt, handler = _voice_host_or_skip(tmp_path)

    # First voice turn: D1' eager-creates the thread and writes the user transcript.
    await handler(stt, "первый вопрос")
    tid = host.voice_thread["id"]
    assert tid is not None
    assert [e["kind"] for e in host.threads.read_feed(tid)] == ["user"]

    # The guarded assistant aggregator is the assistant half of the pair (a subclass of
    # LLMAssistantAggregator; LLMUserAggregator is a sibling class, so exclude it).
    aggregator = next(
        p
        for p in session.pipeline.processors
        if isinstance(p, LLMAssistantAggregator) and not isinstance(p, LLMUserAggregator)
    )

    # Drive the REAL commit path: seed the running aggregation and push it. push_aggregation()
    # runs `self._context.add_message({"role":"assistant","content":<str>})` — byte-for-byte the
    # prod commit — and returns the committed text. (push_context_frame/timestamp pushes log a
    # benign "StartFrame not received" outside a running pipeline; the commit itself succeeds.)
    aggregator._aggregation = [
        TextPartForConcatenation("вот мой ответ", includes_inter_part_spaces=True)
    ]
    committed = await aggregator.push_aggregation()

    # Setup sanity — the answer really committed to the live context via the prod path.
    assert committed == "вот мой ответ"
    ctx_msgs = _context_of(session).get_messages()
    assert any(
        m.get("role") == "assistant" and m.get("content") == "вот мой ответ" for m in ctx_msgs
    ), "precondition: answer must be in the live LLM context after push_aggregation"

    # B25 DESIRED behaviour: NO next handler() call, NO flush_voice_feed() — the answer must
    # already be in the thread feed at commit time (≤3s / next pollFeed). Today it is absent.
    feed = host.threads.read_feed(tid)
    assert any(
        e["kind"] == "assistant" and e.get("text") == "вот мой ответ" for e in feed
    ), (
        "B25: dispatcher answer must land in the thread feed at answer-commit — feed still "
        f"holds only {[e['kind'] for e in feed]!r}"
    )
