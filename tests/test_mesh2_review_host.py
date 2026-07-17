"""МЕШ-2 review — red tests for host/consult defects B-M2-1/6/8/9.

Each test asserts the DESIRED behavior and fails on current code AT ITS OWN ASSERTION
(not import/fixture/signature). Offline, duck-typed fakes; no production code touched.

Harness mirrors tests/test_full_mesh_m2.py:
  - build_host(cfg, FakeClock(1.0)) with a FAKE Runner assigned to host.kora_runner
  - ThreadStore(FakeClock(1.0), tmp_path/"threads") for the store-only cases.
"""
from __future__ import annotations

import pytest

from synapse.bridge.state import AwaitingRequest, TaskStatus
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.threads import ThreadStore


def _cfg(tmp_path) -> SynapseConfig:
    """Same shape as test_host_consult_starts_and_resumes_without_moving_fsm."""
    return SynapseConfig(
        google_api_key="g", openrouter_api_key="o", anthropic_api_key="a",
        deepgram_api_key="d", fish_audio_api_key="f", fish_reference_id="r",
        journal_dir=str(tmp_path), kora_workspace_dir=str(tmp_path / "ws"),
        kora_cli_path="/bin/echo",
    )


class _Runner:
    """Duck-typed KoraRunner stand-in (copied from the existing host test)."""

    active_run_kind = None

    def __init__(self, host):
        self._host = host
        self.started = None
        self.delivered = None

    def start(self, task_id, text, spec):
        self.active_run_kind = spec.run_kind
        self.started = (task_id, text, spec)

    def provide_answer(self, request_id, text):
        self.delivered = (request_id, text)
        self._host.store.clear_awaiting(request_id)
        return "answer_delivered"


# --- B-M2-1 -----------------------------------------------------------------------------
async def test_b_m2_1_consult_refused_when_task_pending_confirmation(tmp_path):
    """A consult launched while a destructive task is staged (PENDING_CONFIRMATION) must
    return `busy` and MUST NOT overwrite the singleton task slot. Current busy-check is
    `active.status == RUNNING` only, so PENDING_CONFIRMATION falls through → begin_task
    clobbers the staged task."""
    from synapse.pipeline.app import build_host

    host = build_host(_cfg(tmp_path), FakeClock(1.0))
    thread = host.threads.create("idea")
    host.threads.set_stage(thread.id, "propose")

    # A destructive task is staged and awaiting the user's confirmation, occupying the slot.
    host.store.stage_task("destructive-1", "rm -rf стенд", {"task_id": "destructive-1"}, 1.0)
    assert host.store.task.status == TaskStatus.PENDING_CONFIRMATION  # precondition

    host.kora_runner = _Runner(host)

    result = await host.consult_kora(thread.id, "обсуди идею")

    # DESIRED: singleton is busy (RUNNING ∪ PENDING_CONFIRMATION) → refuse, keep the slot.
    assert result["outcome"] == "busy", (
        f"consult clobbered a PENDING_CONFIRMATION task instead of refusing busy; got {result}"
    )
    assert host.store.task.id == "destructive-1"
    assert host.store.task.status == TaskStatus.PENDING_CONFIRMATION


# --- B-M2-6 -----------------------------------------------------------------------------
def test_b_m2_6_read_only_consult_does_not_disable_project_binding(tmp_path):
    """A read-only consult must not permanently disable bind_project. begin_aux_run appends
    the aux task_id to t.task_ids, and bind_project refuses once task_ids is non-empty, so a
    single consult silently burns the thread's ability to bind a project forever."""
    threads = ThreadStore(FakeClock(1.0), tmp_path / "threads")
    thread = threads.create("idea")  # unbound: project_id is None

    threads.begin_aux_run(thread.id, "consult-1")

    # DESIRED: a read-only consult must not consume the one-shot bind.
    assert threads.bind_project(thread.id, "proj-1") is True, (
        "consult begin_aux_run wrote to t.task_ids → bind_project sees non-empty task_ids "
        "and refuses (returns False) forever"
    )
    assert threads.get(thread.id).project_id == "proj-1"


# --- B-M2-8 -----------------------------------------------------------------------------
def test_b_m2_8_read_case_survives_corrupt_non_utf8_file(tmp_path):
    """read_case is best-effort → empty string on failure. It catches OSError but a corrupt
    (non-UTF-8) case file raises UnicodeDecodeError (a ValueError), which currently escapes
    and crashes the tool turn."""
    threads = ThreadStore(FakeClock(1.0), tmp_path / "threads")
    thread = threads.create("idea")
    threads.case_path(thread.id).write_bytes(b"\xff\xfe\x00 bad bytes \x83")

    try:
        out = threads.read_case(thread.id)
    except UnicodeDecodeError:
        pytest.fail(
            "B-M2-8: read_case raised UnicodeDecodeError instead of returning '' "
            "on a corrupt case file"
        )
    assert out == ""


# --- B-M2-9 -----------------------------------------------------------------------------
async def test_b_m2_9_answer_kora_parked_consult_records_briefing_to_case(tmp_path):
    """Answering a parked consult via answer_kora must also record the briefing to the durable
    case (П-3 §6: the case file is the source of truth). consult_kora's resume path calls
    append_case; the answer_kora consult branch delivers to Kora but skips append_case, so the
    durable case loses this briefing turn."""
    from synapse.pipeline.app import build_host

    host = build_host(_cfg(tmp_path), FakeClock(1.0))
    host.kora_runner = _Runner(host)
    thread = host.threads.create("idea")
    host.threads.set_stage(thread.id, "propose")

    # Park the store on a RUNNING consult awaiting the user's answer.
    host.store.start_task("consult-1", "brief", TaskStatus.RUNNING, 1.0)
    host.store.set_awaiting(AwaitingRequest(
        1, "r1", thread.id, "consult-1", "consult", "уточни нагрузку", "нагрузка: …", 1.0,
    ))

    res = host.answer_kora(thread.id, "нагрузка 500 rps")

    # Delivery happened (sanity — this half already works)...
    assert res["outcome"] == "answer_delivered"
    # DESIRED: the briefing is also recorded to the durable case.
    assert "нагрузка 500 rps" in host.threads.read_case(thread.id), (
        "answer_kora consult branch delivered to Kora but never called append_case → "
        "the durable case does not contain the briefing turn"
    )
