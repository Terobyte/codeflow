# -*- coding: utf-8 -*-
"""Red tests for bug B48's RESIDUAL write-paths (docs/bugs.md, "2026-07-14 — сбор проблем"):
"архивный тред остаётся полностью запускаемым/мутируемым: archived не проверяется ни в
одном write-пути".

The gate_action path is already pinned red in
tests/test_bugs_0714_gatestate.py::test_B48_archived_thread_must_refuse_send_to_kora.
This file covers the two remaining write-paths, one test per path, each red at its OWN
assertion against the current (unfixed) tree:

- propose path: `_propose_for` (app.py ~530, reached via the voice tool's
  `bridge.on_propose` = `_voice_propose`) never reads `th.archived` — proposing into an
  archived thread moves its stage collect→propose. Post-fix it must refuse with
  `{"outcome": "thread_archived"}` and leave the stage untouched.
- voice direct-dispatch commit path: `_on_task_committed` (app.py ~613, reached via
  `bridge.on_task_committed`) never reads `th.archived` — a stale archived voice binding
  appends the task into the archived thread and launches Kora there. Post-fix design: the
  stale archived binding degrades to a FRESH non-archived thread (same pattern as the
  existing "битый project_id тихо деградирует" branch) and the voice binding moves there.

Both tests use a real SynapseHost via build_host() (fake keys, never dials the network),
mirroring tests/test_bugs_0714_gatestate.py::_gate_host — plus the
tests/test_threads.py::_fake_host trick of patching `start` ON the original KoraRunner
instance, because the `_on_task_committed` closure captured that instance (swapping
`host.kora_runner` alone would leave the closure launching the real runner). No production
code is touched; xfail(strict=True) keeps the suite green while B48 is open and flips to a
loud unexpected-pass the moment the fix lands.
"""
from __future__ import annotations

import pytest

from synapse.config import SynapseConfig


def _fake_cfg(tmp_path) -> SynapseConfig:
    """The standard fake-key cfg pattern used throughout the suite (test_hunt0714_a.py) --
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
    """Real SynapseHost via build_host (fake keys), Kora launches stubbed BOTH ways:
    `host.kora_runner` is swapped for the stub (covers host.gate_action's launches) AND
    `start` is patched on the ORIGINAL runner instance (covers the `_on_task_committed`
    closure, which captured that instance at build time -- test_threads.py::_fake_host)."""
    from synapse.pipeline.app import build_host

    host = build_host(_fake_cfg(tmp_path))
    fake = _FakeKoraRunner()
    if host.kora_runner is not None:
        host.kora_runner.start = fake.start  # the closure's reference
    host.kora_runner = fake                  # the host-attribute reference
    return host


# =============================================================================================
# B48 residual path 1 -- propose_request into an archived thread must refuse (and must not
# advance the archived thread's stage).
# =============================================================================================


def test_B48_propose_on_archived_thread_must_refuse(tmp_path):
    host = _gate_host(tmp_path)
    t = host.threads.create("x")
    assert host.threads.get(t.id).stage == "collect"  # sanity: fresh thread

    assert host.threads.set_archived(t.id, True) is True
    assert host.threads.get(t.id).archived is True

    # The voice tool's seam: bridge.on_propose is _voice_propose (wired in build_host),
    # which routes through the internal _propose_for closure with the voice binding's id.
    host.voice_thread["id"] = t.id
    res = host.bridge.on_propose("свод запроса")

    assert res.get("outcome") == "thread_archived", (
        "B48: propose_request must refuse to commit a request summary into an archived "
        f"thread with outcome='thread_archived', but it proceeded (result={res!r}) -- "
        "_propose_for never checks th.archived."
    )
    assert host.threads.get(t.id).stage == "collect", (
        "B48: proposing into an archived thread moved its stage to "
        f"{host.threads.get(t.id).stage!r}; an archived thread must stay immutable."
    )


# =============================================================================================
# B48 residual path 2 -- a voice direct-dispatch commit with a stale ARCHIVED voice binding
# must not append the task into / launch Kora inside the archived thread; post-fix it
# degrades to a fresh thread (the existing dead-project_id degradation pattern).
# =============================================================================================


def test_B48_voice_commit_into_archived_binding_must_degrade_to_fresh_thread(tmp_path):
    host = _gate_host(tmp_path)
    t = host.threads.create("x")
    assert host.threads.set_archived(t.id, True) is True
    host.voice_thread["id"] = t.id  # stale binding: the bound thread got archived

    # The real wired callback (_on_task_committed, app.py ~613) -- exactly what a
    # non-destructive submit_task's COMMITTED outcome fires on the voice channel.
    host.bridge.on_task_committed("task-x1", "создай файл")

    assert "task-x1" not in host.threads.get(t.id).task_ids, (
        "B48: the direct-dispatch commit appended the task into the ARCHIVED thread "
        f"(task_ids={host.threads.get(t.id).task_ids!r}) -- _on_task_committed never "
        "checks th.archived."
    )
    th2 = host.threads.thread_for_task("task-x1")
    assert th2 is not None and th2.id != t.id and th2.archived is False, (
        "B48: the task must degrade to a FRESH non-archived thread (the dead-project_id "
        f"degradation pattern), got thread={th2!r} for the committed task."
    )
    assert host.voice_thread["id"] == th2.id, (
        "B48: the voice binding must move off the archived thread to the fresh one, "
        f"but it is still {host.voice_thread['id']!r} (expected {th2.id!r})."
    )
