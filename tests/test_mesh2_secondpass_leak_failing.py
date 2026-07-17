"""B-M2-11 (МЕШ-2 second pass): the consult teardown leaks _consult_followup_requests.

FAILING test that proves the bug. `SynapseHost.on_consult_parked` registers a parked
consult's request_id in `host._consult_followup_requests` (app.py:257). The consult
teardown branch of `_run_finished` (`gate_mode == "consult"`, app.py:480-490) pops the two
sibling per-session structures — `_consult_session_threads` and `_consult_budget_remaining`
— but never removes the request_id from `_consult_followup_requests`, so the marker lingers
in the set forever after the session ends (monotonic memory / stale membership).

Fix-agnostic assertion: after teardown the known request_id must NOT be in the set. It is
currently still present -> RED now, GREEN once teardown clears it symmetrically.
"""
from __future__ import annotations

import asyncio

import pytest

from synapse.bridge.state import AwaitingRequest
from synapse.clock import FakeClock
from synapse.config import SynapseConfig


class _NoLLMTextLoop:
    """Neutralizes the ONLY real externality: `on_consult_parked` schedules
    `asyncio.create_task(self._run_consult_followup(request))`, which awaits
    `text_loop.ingest_autonomous_turn(...)` (a real dispatcher LLM turn). Here it is an
    async no-op, so the scheduled follow-up runs nothing real. Being non-None also lets
    `on_consult_parked` pass its `self.text_loop is None` guard and reach the `.add`."""

    def __init__(self) -> None:
        self.calls: list = []

    async def ingest_autonomous_turn(self, instruction, thread_id):
        self.calls.append((instruction, thread_id))


class _StubRunner:
    """Minimal kora_runner: `consult_kora`'s start path only needs `.start` (records the
    spec) — mirrors the Runner stub in test_full_mesh_m2's host-consult test."""

    active_run_kind = None

    def start(self, task_id, text, spec):
        self.active_run_kind = spec.run_kind
        self.started = (task_id, text, spec)

    def provide_answer(self, *args, **kwargs):
        return "answer_delivered"


@pytest.mark.asyncio
async def test_b_m2_11_consult_followup_request_id_released_on_teardown(tmp_path):
    from synapse.pipeline.app import build_host

    cfg = SynapseConfig(
        google_api_key="g", openrouter_api_key="o", anthropic_api_key="a",
        deepgram_api_key="d", fish_audio_api_key="f", fish_reference_id="r",
        journal_dir=str(tmp_path), kora_workspace_dir=str(tmp_path / "ws"),
        kora_cli_path="/bin/echo",
    )
    host = build_host(cfg, FakeClock(1.0))
    host.kora_runner = _StubRunner()
    # Neutralize the real follow-up LLM turn before any consult can schedule one.
    host.text_loop = _NoLLMTextLoop()

    thread = host.threads.create("idea")
    host.threads.set_stage(thread.id, "propose")

    # (1) Start a real consult session: consult_kora begins a RUNNING task and sets
    #     _consult_budget_remaining[task_id] > 0 and _consult_session_threads[task_id].
    started = await host.consult_kora(thread.id, "сравни варианты кеша")
    assert started["outcome"] == "consult_started"
    task = host.store.task
    task_id = task.id
    assert task.status.value == "running"
    assert host._consult_budget_remaining.get(task_id, 0) > 0
    assert host._consult_session_threads.get(task_id) == thread.id

    # (2) A parked consult reply (schema-1 identity). on_consult_parked ADDS its request_id
    #     to _consult_followup_requests and schedules the (now no-op) follow-up turn.
    request_id = "r-parked-b-m2-11"
    request = AwaitingRequest(
        1, request_id, thread.id, task_id, "consult", "уточни нагрузку", "нагрузка: …", 1.0
    )
    host.on_consult_parked(request)
    # Drain the scheduled follow-up deterministically (grabbed before any await, so the
    # done-callback has not discarded it yet); it is a no-op LLM stub.
    pending = list(host._consult_followup_tasks)
    if pending:
        await asyncio.gather(*pending)

    # Precondition: the park actually registered the id -> the leak source is live.
    assert request_id in host._consult_followup_requests

    # (3) Consult session ends -> the teardown branch of _run_finished (gate_mode="consult").
    host._run_finished(thread.id, "completed", "consult")

    # Teardown DID release the two sibling per-session structures...
    assert task_id not in host._consult_session_threads
    assert task_id not in host._consult_budget_remaining
    # ...and the correct, symmetric behavior is to release this per-session marker too.
    assert request_id not in host._consult_followup_requests
