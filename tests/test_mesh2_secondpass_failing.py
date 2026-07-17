# -*- coding: utf-8-sig -*-
"""Red test proving bug B-M2-10 from bugs.md ("🔁 Багхант МЕШ-2, второй заход, `9f42166`").

B-M2-10 (MAJOR): the MESH-2 commit removed `_propose_for`'s stage guard
(`if th.stage != "collect": return {"outcome": "illegal_stage"}`). Now a `propose_request`
in a post-collect stage (`spec_plan`, after a completed docs run) rewrites `request_text`
via `threads.set_request` — which never resets `last_outcome` and never raises — leaving
`last_outcome == "completed"` intact. `write_code`'s two staleness signals
(`plan_path.exists()` for request R1's on-disk plan + `last_outcome == "completed"`) both
still pass, so it launches CODE against R1's stale plan under the NEW request R2 instead of
refusing with `{"error": "stale_plan"}`. The `revise` branch (app.py:567-573, tagged B07)
proves the intended invariant by calling `threads.set_outcome(thread_id, None)`; the new
propose path omits it.

This test is written so the CORRECT (documented) behavior is the pass condition: after
proposing R2 in `spec_plan`, `write_code` MUST refuse with `stale_plan`. It FAILS at its own
assertion against the current (unfixed) tree — today `write_code` launches CODE and returns
`{"ok": True, "stage": "code"}`. No production code is touched. Real `SynapseHost` via
`build_host()` (fake keys, never dials the network) with `kora_runner` swapped for a stub,
mirroring `tests/test_bugs_0714_gatestate.py::test_B46...` (same shape: propose ->
send_to_kora -> completed docs run -> write_code asserts stale_plan).
"""
from __future__ import annotations

from pathlib import Path

from synapse.bridge.state import TaskStatus
from synapse.config import SynapseConfig


def _fake_cfg(tmp_path) -> SynapseConfig:
    """The standard fake-key cfg pattern used throughout the suite (test_bugs_0714_gatestate.py) --
    build_host() never dials the network with it."""
    return SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path), kora_workspace_dir=str(tmp_path / "ws"),
    )


class _FakeKoraRunner:
    """Stub KoraRunner (same shape as test_bugs_0714_gatestate.py's): records start(...)
    calls, no SDK/network involved."""

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


async def test_b_m2_10_propose_in_spec_plan_leaves_stale_plan_launchable(tmp_path):
    host = _gate_host(tmp_path)

    # (1) collect -> propose(R1). The propose seam is the voice tool's bridge.on_propose
    # (_voice_propose -> the internal _propose_for closure), targeted at this thread by its
    # voice binding -- exactly the surface B-M2-10 identifies as reachable host code.
    t = host.threads.create("x")
    assert host.threads.get(t.id).stage == "collect"  # sanity: fresh thread
    host.voice_thread["id"] = t.id
    r1 = host.bridge.on_propose("запрос R1")
    assert r1.get("outcome") == "proposed"
    assert host.threads.get(t.id).stage == "propose"  # sanity: R1 committed, стадия propose

    # (2) send_to_kora (non-fast) launches the docs/spec_plan run.
    res = await host.gate_action(t.id, "send_to_kora", confirm=True)
    assert res.get("ok") is True and host.threads.get(t.id).stage == "spec_plan"

    # Simulate the docs run COMPLETED the same way test_bugs_0714_gatestate.py does: Kora
    # wrote R1's plan file on disk, the store task completes, and the run-finished callback
    # fires for a docs_only run -> last_outcome="completed", stage stays spec_plan.
    root = Path(host.cfg.kora_workspace_dir)
    (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "plans" / f"{t.id}.md").write_text("план R1", encoding="utf-8")
    host.store.set_task_status(TaskStatus.COMPLETED)
    host._run_finished(t.id, "completed", "docs_only")

    th = host.threads.get(t.id)
    assert th.stage == "spec_plan" and th.last_outcome == "completed"  # setup sanity

    # (3) The user refines the request; the dispatcher commits R2 via propose_request WHILE
    # the thread sits in spec_plan (post-collect). set_request rewrites request_text without
    # touching last_outcome and without raising -- the stale plan file for R1 stays on disk.
    r2 = host.bridge.on_propose("запрос R2")
    assert r2.get("outcome") == "proposed"
    th = host.threads.get(t.id)
    assert th.request_text == "запрос R2" and th.stage == "spec_plan"  # sanity: R2 committed

    # (4) write_code. R1's plan (docs/plans/{id}.md) and R2 (request_text) now disagree.
    # Changing the request summary after a completed docs run MUST invalidate the stale plan
    # (exactly as `revise` does via set_outcome(None)), so write_code refuses with stale_plan
    # until a fresh spec_plan run completes for R2.
    result = await host.gate_action(t.id, "write_code", confirm=True)

    assert result == {"error": "stale_plan"}, (
        "B-M2-10: propose_request(R2) in spec_plan left last_outcome=='completed', so "
        "write_code launched R1's stale on-disk plan under the new request R2 instead of "
        f"refusing with stale_plan. got {result!r} (starts={host.kora_runner.starts!r})"
    )
