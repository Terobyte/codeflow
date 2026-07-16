"""Bughunt 2026-07-16 (МЕШ-1 code review) — B-PIPE-7 (CRIT, authority-bypass).

`SynapseHost.answer_kora` (synapse/pipeline/app.py) has a "task not running" fallback branch
that reads the RAW `self.store.awaiting` (not liveness-gated via `_awaiting_active()`) and, when
the guarded task's status isn't literally "running", calls `KoraRunner.provide_answer(request_id,
text)` DIRECTLY — skipping the AnswerApprovalService stage -> new-user-turn -> affirm -> digest
two-key cycle entirely, for ANY text, regardless of `user_initiated`.

`TaskStore.request_cancel()` synchronously flips a RUNNING task to CANCEL_REQUESTED, but the
parked `reply_to_flow` handler's `finally` (which clears `_pending_answer`/`_pending_request_id`
and `store.awaiting`) only runs later via async SDK teardown. In that window `store.awaiting`
still returns the live schema-1 `AwaitingRequest` and the runner's pending future is still live,
so the fallback branch's direct `provide_answer` call resolves it with unapproved text — an
unapproved summary is delivered into the still-running code/docs SDK session with no approval
gate at all.

This test reproduces exactly that window using the same `build_host` / `_arm` / reply_to_flow
parking pattern as `tests/test_full_mesh_m1.py::test_host_stages_then_delivers_same_summary_after_affirm`,
except it flips the task to CANCEL_REQUESTED (instead of leaving it RUNNING) before calling
`answer_kora`, with NO prior `stage()` and NO new user turn / affirm.
"""
from __future__ import annotations

import asyncio

import pytest

from synapse.bridge.state import TaskStatus, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig


def _arm(runner, store, *, task_id="tk", thread_id="th", run_kind="code"):
    store.start_task(task_id, "работай", TaskStatus.RUNNING, 1.0)
    runner._run_owner = task_id
    runner._run_thread_id = thread_id
    runner._run_kind = run_kind


def _args(**kw):
    return {
        "speak_text": "Нужно выбрать хранилище.",
        "flow_instruction": "Выясни требования к консистентности.",
        "answer_format": "выбор: …; причина: …",
        "final": False,
        **kw,
    }


@pytest.mark.asyncio
async def test_b_pipe_7_answer_kora_cancel_window_bypasses_approval(tmp_path):
    from synapse.pipeline.app import build_host

    cfg = SynapseConfig(
        google_api_key="g", openrouter_api_key="o", anthropic_api_key="a",
        deepgram_api_key="d", fish_audio_api_key="f", fish_reference_id="r",
        journal_dir=str(tmp_path), kora_workspace_dir=str(tmp_path / "ws"),
        kora_cli_path="/bin/echo",
    )
    host = build_host(cfg, FakeClock(1.0))
    thread = host.threads.create("mesh")
    runner = host.kora_runner
    _arm(runner, host.store, task_id="tk", thread_id=thread.id)

    # Park a real schema-1 reply_to_flow request (R1): live future, live _pending_request_id,
    # live store.awaiting — exactly what a code/docs run in-flight on a question looks like.
    tool = runner._build_reply_tool("tk")
    parked = asyncio.create_task(tool.handler(_args()))
    await asyncio.sleep(0)

    request = host.store.awaiting
    assert request is not None and request.task_id == "tk"
    assert runner._pending_answer is not None
    assert runner._pending_request_id == request.request_id

    # Simulate the in-flight cancel window: request_cancel() synchronously flips the task's
    # status to CANCEL_REQUESTED. The parked handler's `finally` (which would clear
    # store.awaiting / _pending_answer / _pending_request_id) has NOT run yet — it only runs
    # later via async SDK teardown, exactly as B-PIPE-7 describes.
    assert host.store.request_cancel() is True
    assert host.store.task.status == TaskStatus.CANCEL_REQUESTED
    assert not parked.done()  # the parked handler's finally genuinely has not executed

    # No prior AnswerApprovalService.stage(), no new user turn, no affirm — and user_initiated
    # is explicitly False, so this must NOT be treated as an already-approved delivery.
    result = host.answer_kora(thread.id, "unapproved summary", user_initiated=False)

    # CORRECT (documented) behavior: an unapproved summary must never reach the parked SDK
    # session without going through AnswerApprovalService's stage -> new-turn -> affirm -> digest
    # cycle, even inside the cancel window. On current (buggy) code the fallback branch calls
    # KoraRunner.provide_answer() directly, resolving the future with the unapproved text and
    # reporting "answer_delivered" — this assertion documents that defect and goes red on it.
    assert result["outcome"] != "answer_delivered", (
        "B-PIPE-7: answer_kora delivered an UNAPPROVED summary during the cancel window "
        f"(outcome={result!r}) — the 'task not running' fallback branch bypassed "
        "AnswerApprovalService entirely instead of requiring stage->new-turn->affirm->digest."
    )
    assert not parked.done(), (
        "B-PIPE-7: the parked reply_to_flow call resolved with an unapproved summary during "
        "the cancel window."
    )

    parked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parked
