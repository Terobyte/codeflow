# -*- coding: utf-8-sig -*-
"""Red tests proving bugs B46, B48, B49 from docs/bugs.md ("2026-07-14 — сбор проблем",
live-test staging findings). One test per bug ID, xfail(strict=True) so the normal suite
stays green while these are open — each xfail flips to an unexpected-pass failure (and
must be un-marked) the moment the corresponding fix lands, exactly like the historical
`tests/test_hunt0714_a.py`/`test_hunt0714_b.py` convention.

Every test is written so the CORRECT (documented) behavior is the pass condition — these
FAIL at their own assertion against the current (unfixed) tree. No production code is
touched. Real `SynapseHost` via `build_host()` (fake keys, never dials the network) with
`kora_runner` swapped for a stub, mirroring `tests/test_hunt0714_a.py::_gate_host`.

- B46 (CRIT): `write_code`'s only freshness signal is `thread.last_outcome=="completed"`
  (app.py:368-369). `_run_finished` (app.py:285-297) sets that flag unconditionally for
  ANY completed run bound to a thread_id — including an unrelated direct-dispatch task
  (`_on_task_committed`/`_http_task_committed`, app.py:613-626/662-669) that happens to
  land in the same thread while a stale plan file from an earlier request sits on disk.
  Repro: propose→send_to_kora completes plan A (plan file + last_outcome=completed) →
  revise (B07 correctly resets last_outcome=None, but plan file A remains on disk) → an
  UNRELATED direct-dispatch task completes in the same thread → last_outcome flips back
  to "completed" with no relation to a spec_plan for the new request → propose(B) →
  write_code passes the guard and launches Kora against plan A's stale file under
  request B.
- B48 (MAJOR): `gate_action` never reads `th.archived` — an archived thread accepts
  `send_to_kora` exactly like a live one (app.py:299-377).
- B49 (MAJOR): the archive route's busy-check (`webrtc_server.py:612-628`) only excludes
  `TaskStatus.RUNNING`, but the canonical "busy" definition `has_active_task()`
  (`state.py:186-190`) also includes `PENDING_CONFIRMATION` — a thread with a task
  awaiting confirmation can still be archived.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from synapse.bridge.state import TaskStatus
from synapse.config import SynapseConfig


def _fake_cfg(tmp_path) -> SynapseConfig:
    """The standard fake-key cfg pattern used throughout the suite (test_hunt0714_a.py) --
    build_host() never dials the network with it."""
    return SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path), kora_workspace_dir=str(tmp_path / "ws"),
        api_token="test-token",  # С5: build_web_app's authn middleware needs a real token
    )


class _FakeKoraRunner:
    """Stub KoraRunner (same shape as test_hunt0714_a.py's _FakeKoraRunner): records
    start(...) calls, no SDK/network involved."""

    def __init__(self) -> None:
        self.starts: list[tuple] = []

    def start(self, task_id, text, spec) -> None:
        self.starts.append((task_id, text, spec))


def _gate_host(tmp_path):
    """Real SynapseHost via build_host (fake keys), kora_runner swapped for a stub so
    gate_action's launches are observable without touching the Claude Agent SDK."""
    from synapse.pipeline.app import build_host

    host = build_host(_fake_cfg(tmp_path))
    host.kora_runner = _FakeKoraRunner()
    return host


# =============================================================================================
# B46 -- an unrelated direct-dispatch task completing in the same thread re-satisfies
# write_code's stale-plan guard (reopens B07 through a different channel).
# =============================================================================================


async def test_B46_unrelated_direct_dispatch_completion_must_not_unstale_the_plan(tmp_path):
    host = _gate_host(tmp_path)
    t = host.threads.create("x")
    host.threads.set_stage(t.id, "propose")
    host.threads.set_request(t.id, "запрос A")

    res = await host.gate_action(t.id, "send_to_kora", confirm=True)
    assert res.get("ok") is True and host.threads.get(t.id).stage == "spec_plan"

    # Request A's spec_plan run finishes successfully: Kora wrote the plan file and the
    # runner's on_run_finished callback fires (mirrors _run_finished/start_task).
    root = Path(host.cfg.kora_workspace_dir)
    (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "plans" / f"{t.id}.md").write_text("план A", encoding="utf-8")
    host.store.set_task_status(TaskStatus.COMPLETED)
    host._run_finished(t.id, "completed", "docs_only")  # B46: вид рана явный — это spec_plan
    assert host.threads.get(t.id).last_outcome == "completed"

    # revise correctly resets last_outcome (B07 fix) -- but the stale plan FILE for
    # request A is left on disk, exactly as B07's repro sets up.
    res = await host.gate_action(t.id, "revise")
    assert res.get("ok") is True and host.threads.get(t.id).stage == "collect"
    assert host.threads.get(t.id).last_outcome is None

    # An UNRELATED direct-dispatch task ("создай helloworld.txt", nothing to do with this
    # thread's plan/spec_plan flow) completes while bound to the SAME thread -- exactly
    # the _on_task_committed/_http_task_committed channel B46 identifies, which shares
    # thread_id with whatever the gate flow used and is entirely un-stage-gated (B47).
    host.store.start_task("unrelated-task-1", "создай helloworld.txt", TaskStatus.RUNNING,
                          host.clock.now())
    host.threads.append_task(t.id, "unrelated-task-1")
    host.store.set_task_status(TaskStatus.COMPLETED)
    host._run_finished(t.id, "completed")

    # The unrelated completion must NOT resurrect write_code's freshness signal for a
    # thread that has no fresh spec_plan for its (new, not-yet-proposed) request.
    assert host.threads.get(t.id).last_outcome is None, (
        "B46: an unrelated direct-dispatch task's completion flipped last_outcome back to "
        f"'completed' ({host.threads.get(t.id).last_outcome!r}) via the shared _run_finished "
        "channel, re-satisfying write_code's only freshness check with no relation to a "
        "fresh spec_plan for this thread's request."
    )

    # Propose a DIFFERENT request B and go straight for write_code -- no fresh spec_plan
    # ever ran for B. The correct behavior is a refusal; today the stale plan_path.exists()
    # (A's file) + the resurrected last_outcome=="completed" together pass the guard.
    host.threads.set_stage(t.id, "propose")
    host.threads.set_request(t.id, "запрос B")

    res = await host.gate_action(t.id, "write_code", confirm=True)
    assert res.get("error") == "stale_plan", (
        "B46: write_code launched request A's stale on-disk plan under the new request B "
        f"instead of refusing with stale_plan, got {res!r} (starts={host.kora_runner.starts!r})"
    )


# =============================================================================================
# B48 -- an archived thread is still fully launchable: gate_action never reads
# th.archived.
# =============================================================================================


async def test_B48_archived_thread_must_refuse_send_to_kora(tmp_path):
    host = _gate_host(tmp_path)
    t = host.threads.create("x")
    host.threads.set_stage(t.id, "propose")
    host.threads.set_request(t.id, "запрос")

    assert host.threads.set_archived(t.id, True) is True
    assert host.threads.get(t.id).archived is True

    res = await host.gate_action(t.id, "send_to_kora", confirm=True)
    assert res.get("error") in ("archived", "thread_archived"), (
        "B48: gate_action must refuse to launch a run on an archived thread, but it "
        f"proceeded (result={res!r}, kora_runner.starts={host.kora_runner.starts!r}) -- "
        "archived is never checked in the send_to_kora/write_code/revise branches."
    )
    assert not host.kora_runner.starts, (
        "B48: an archived thread must never launch Kora, but a run was started: "
        f"{host.kora_runner.starts!r}"
    )


# =============================================================================================
# B49 -- thread archive is allowed while a task is PENDING_CONFIRMATION (the archive
# busy-check only excludes RUNNING).
# =============================================================================================


def test_B49_archive_must_refuse_while_task_pending_confirmation(tmp_path):
    pytest.importorskip("aiortc")
    pytest.importorskip("cv2")
    pytest.importorskip("fastapi")
    try:
        from synapse.pipeline.webrtc_server import build_web_app
    except (ImportError, RuntimeError) as e:
        pytest.skip(f"webrtc deps/prebuilt UI unavailable: {e}")
    from starlette.testclient import TestClient

    host = _gate_host(tmp_path)
    t = host.threads.create("x")
    host.threads.append_task(t.id, "task-1")
    host.store.start_task("task-1", "деструктивный ask", TaskStatus.PENDING_CONFIRMATION,
                          host.clock.now())
    assert host.store.has_active_task() is True  # sanity: canonical busy-check agrees

    app = build_web_app(host)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        f"/api/threads/{t.id}/archive",
        content=b"{}",
        headers={"content-type": "application/json", "origin": "http://testserver",
                 "authorization": "Bearer test-token"},
    )

    assert resp.status_code == 409, (
        "B49: archiving a thread with a PENDING_CONFIRMATION task must be refused (409) "
        f"like a RUNNING task is, got status={resp.status_code!r} body={resp.text!r} -- "
        "the archive route's busy-check only excludes TaskStatus.RUNNING."
    )
    assert host.threads.get(t.id).archived is False, (
        "B49: the thread was archived while its task was still PENDING_CONFIRMATION"
    )
