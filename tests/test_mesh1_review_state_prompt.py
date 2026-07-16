"""Red pytest proofs for the МЕШ-1 code review (bugs.md, "Code review 2026-07-16 — МЕШ-1").

Scope of this file: B-BRIDGE-12, B-BRIDGE-13, B-CORE-10 (real, expected RED on current code),
plus a not-test-verifiable marker for B-BRIDGE-14 (latent, unreachable on a single event loop —
do NOT fake a red by forcing OS threads).

TEST-WRITER ONLY: no production code touched, no other test file touched.
"""
from __future__ import annotations

import json

import pytest

from synapse.bridge.state import AwaitingRequest, TaskStatus, TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.prompt import build_system_prompt


def test_b_bridge_12_resync_greeting_leaks_flow_instruction():
    """B-BRIDGE-12 (MAJOR): resync_greeting() delegates its suffix to render_state_template(),
    which for a live schema-1 park returns the raw "[ЗАПРОС КОРЫ]: <flow_instruction>" (+
    "[ФОРМАТ ОТВЕТА]: <answer_format>") lines. That greeting is spoken to the user on WebRTC
    reconnect with NO LLM and NO trust framing — the internal Flow-directed instruction leaks
    to the human instead of a generic "Кора ждёт твоего ответа" line.
    """
    clock = FakeClock(1.0)
    store = TaskStore(clock)
    store.start_task("tk", "работай над проектом", TaskStatus.RUNNING, 1.0)
    store.set_awaiting(
        AwaitingRequest(
            1, "r1", "th", "tk", "code",
            "СЕКРЕТНАЯ инструкция для Flow", "одним словом", 1.0,
        )
    )

    greeting = store.resync_greeting(2.0, 120, 300)

    assert greeting is not None
    # Correct behavior: schema-1 park greets with a generic "ждёт ответа" line, never the raw
    # flow_instruction text nor the literal bracket labels.
    assert "СЕКРЕТНАЯ инструкция для Flow" not in greeting, (
        f"resync_greeting leaked flow_instruction verbatim to the user: {greeting!r}"
    )
    assert "[ЗАПРОС КОРЫ]" not in greeting
    assert "[ФОРМАТ ОТВЕТА]" not in greeting


def test_b_bridge_13_malformed_awaiting_wipes_valid_running_task(tmp_path):
    """B-BRIDGE-13 (MAJOR): TaskStore._load() parses the "awaiting" blob inside the SAME
    try/except that parses "task". A malformed schema-1 awaiting blob (missing a required key)
    raises inside AwaitingRequest(...) construction; the blanket except resets self._task = None
    too, and the S13 zombie-reconcile (RUNNING -> FAILED) never runs. A valid RUNNING task
    silently vanishes and state.json is never healed on next boot.
    """
    clock = FakeClock(1.0)
    store = TaskStore(clock, journal_dir=tmp_path)
    store.start_task("tk", "важная задача", TaskStatus.RUNNING, 1.0)
    store.set_awaiting(
        AwaitingRequest(1, "r1", "th", "tk", "code", "спроси", "ответ: …", 1.0)
    )

    state_path = tmp_path / "state.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["task"]["status"] == "running"
    assert data["awaiting"]["schema"] == 1

    # Corrupt the awaiting blob: drop a required key (task_id) — schema stays 1, so _load()
    # still attempts full AwaitingRequest construction and hits a bare-dict KeyError.
    del data["awaiting"]["task_id"]
    state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    reloaded = TaskStore(FakeClock(2.0), journal_dir=tmp_path)

    # Correct behavior: the malformed awaiting blob is dropped in isolation; the valid RUNNING
    # task survives and is reconciled by S13 (RUNNING at boot -> FAILED, zombie of a dead runner).
    assert reloaded.task is not None, (
        "malformed awaiting blob wiped the valid RUNNING task on boot (task is None)"
    )
    assert reloaded.task.status == TaskStatus.FAILED, (
        "S13 zombie-reconcile did not run — surviving task should be RUNNING->FAILED"
    )
    assert reloaded.awaiting is None


def test_b_core_10_trust_note_stripped_by_owed_killswitch():
    """B-CORE-10 (MINOR): build_system_prompt() appends KORA_REQUEST_TRUST_NOTE only under
    `if cfg.include_owed_prompt_rules`. When that killswitch is False, Flow still receives the
    [ЗАПРОС КОРЫ]/[ФОРМАТ ОТВЕТА] blocks in [СОСТОЯНИЕ] rendering (that mechanism does not check
    the flag) but WITHOUT the "недоверенные данные" trust framing — defense-in-depth silently
    drops with an unrelated operator toggle.
    """
    cfg_off = SynapseConfig(include_owed_prompt_rules=False)
    prompt = build_system_prompt(cfg_off)

    # Correct behavior: trust framing for Kora-sourced blocks must not depend on the owed
    # killswitch — the reply_to_flow/[ЗАПРОС КОРЫ] mechanism itself is unconditional.
    assert "недоверенные данные" in prompt, (
        "KORA_REQUEST_TRUST_NOTE was stripped by include_owed_prompt_rules=False"
    )


@pytest.mark.skip(
    reason=(
        "B-BRIDGE-14 latent: snapshot() (state.py:510-521) re-derives the self.awaiting "
        "property three times instead of capturing once (unlike the sibling _awaiting_lines(), "
        "which does `current = self.awaiting` once). A concurrent clear_awaiting() between the "
        "guard and the body would AttributeError. NOT reachable on a single event loop: "
        "TaskStore is never accessed cross-thread in the real system (only the TTS cache uses "
        "to_thread) and snapshot() has no `await`, so no interleaving point exists. The hunter's "
        "repro forced two real OS threads via sys.setswitchinterval() to manufacture the race — "
        "that reproduces the mechanism, not reachability, and would be a fake red here. Verified "
        "by code inspection only: capture-once hygiene, not a live bug — do not force threads to "
        "manufacture a red for this one."
    )
)
def test_b_bridge_14_snapshot_capture_once_not_test_verifiable():
    pass
