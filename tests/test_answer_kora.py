"""M1 slice 3 (E5) — answer_kora: Кора переспрашивает уточнение mid-task.

The whole E5 loop runs with NO network / NO SDK CLI: the interactive gate branch
(`KoraRunner._handle_question`) is driven by calling the PreToolUse hook
`_pretool_hook({"tool_name": "AskUserQuestion", "tool_input": ...}, None, None)` directly and
resolving the parked future via `provide_answer` — exactly the shape slice-1's live probe proved
(§2b). Slice-4 re-homed the gate from `can_use_tool` (which never fired for read-only tools)
onto the PreToolUse hook, so AskUserQuestion now returns a hook dict whose
`hookSpecificOutput.updatedInput.answers` carries the verbatim reply. No subprocess/API.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.kora import KoraRunner
from synapse.bridge.state import (
    EventClass,
    KoraEvent,
    Liveness,
    SpeakLedger,
    TaskStatus,
    TaskStore,
)
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.dispatcher.tools import (
    ALL_SCHEMAS,
    ANSWER_KORA_SCHEMA,
    KoraBridge,
    ToolHandlers,
    register_all,
)
from synapse.journal import TurnJournal
from synapse.prompt import build_system_prompt


# --- helpers -------------------------------------------------------------------------------


def make_runner(tmp_path):
    clock = FakeClock(0.0)
    ws = tmp_path / "ws"
    cfg = SynapseConfig(kora_workspace_dir=str(ws))
    store = TaskStore(clock)  # journal_dir=None → no state.json
    ledger = SpeakLedger()
    journal = TurnJournal(str(tmp_path / "journal"), clock, session_id="s")
    speaks: list[str] = []
    runner = KoraRunner(cfg, store, ledger, clock, journal, speaks.append)
    return runner, store, journal, ws, speaks


def make_answer_handlers(tmp_path, on_answer):
    clock = FakeClock(0.0)
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path), clock, session_id="s")
    classifier = KeywordClassifier(cfg.destructive_keywords)
    confirm_flow = ConfirmFlow(
        store, clock, classifier, journal, cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s
    )
    bridge = KoraBridge(store=store, confirm_flow=confirm_flow, clock=clock, cfg=cfg, on_answer=on_answer)
    handlers = ToolHandlers(bridge, journal)
    return handlers, store, journal


def _journal_rows(journal):
    return [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _one_question(q="Какой формат?", labels=("JSON", "HTML")):
    return {"questions": [{"question": q, "header": "формат", "options": [{"label": x} for x in labels], "multiSelect": False}]}


def _ask(runner, tool_input):
    # slice-4: AskUserQuestion flows through the PreToolUse hook (not can_use_tool). Returns the
    # coroutine so callers await it or wrap it in a task, exactly as they did with `_gate`.
    return runner._pretool_hook({"tool_name": "AskUserQuestion", "tool_input": tool_input}, None, None)


# =========================================================================================
# 1. Gate interactive branch — parks the stream, speaks the question, resolves verbatim
# =========================================================================================


async def test_gate_question_speaks_sets_awaiting_blocks_then_resolves_verbatim(tmp_path):
    runner, store, journal, ws, speaks = make_runner(tmp_path)
    store.start_task("tk", "создай файл", TaskStatus.RUNNING, 0.0)

    gate = asyncio.create_task(_ask(runner, _one_question()))
    await asyncio.sleep(0)  # let _handle_question run up to `await fut`

    # spoken prompt carries the question text + labels + the free-form invitation (on_speak ONLY).
    assert len(speaks) == 1
    assert "Какой формат?" in speaks[0]
    assert "JSON" in speaks[0] and "HTML" in speaks[0]
    assert "своими словами" in speaks[0].lower()
    # flag set, stream blocked, future parked.
    assert store.awaiting_answer is True
    assert not gate.done()
    assert runner._pending_answer is not None

    # off-menu free-form reply (§2b: CLI does not validate answer ∈ labels) → delivered verbatim.
    assert runner.provide_answer("простой текст") is True
    assert store.awaiting_answer is False  # R5: cleared SYNCHRONOUSLY, before set_result

    result = await gate
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["updatedInput"]["answers"]["Какой формат?"] == "простой текст"
    assert hso["updatedInput"]["questions"] == _one_question()["questions"]
    assert runner._pending_answer is None  # finally nulled the slot


async def test_question_journaled_keys_only_no_text(tmp_path):
    runner, store, journal, ws, speaks = make_runner(tmp_path)
    store.start_task("tk", "t", TaskStatus.RUNNING, 0.0)

    gate = asyncio.create_task(_ask(runner, _one_question(q="СЕКРЕТНЫЙ-ВОПРОС")))
    await asyncio.sleep(0)
    runner.provide_answer("ответ")
    await gate

    rows = _journal_rows(journal)
    asked = next(r for r in rows if r["type"] == "kora_question_asked")
    assert asked["payload"] == {"num_questions": 1}
    assert asked["has_speak"] is False
    # the question text NEVER hits the journal (Р-8/Р-15, MAJOR-R4).
    assert all("СЕКРЕТНЫЙ-ВОПРОС" not in json.dumps(r, ensure_ascii=False) for r in rows)


async def test_answer_applied_to_all_question_keys(tmp_path):
    runner, store, journal, ws, speaks = make_runner(tmp_path)
    store.start_task("tk", "t", TaskStatus.RUNNING, 0.0)
    tool_input = {
        "questions": [
            {"question": "q1", "options": [{"label": "a"}]},
            {"question": "q2", "options": [{"label": "b"}]},
        ]
    }

    gate = asyncio.create_task(_ask(runner, tool_input))
    await asyncio.sleep(0)
    runner.provide_answer("один ответ")
    result = await gate

    assert result["hookSpecificOutput"]["updatedInput"]["answers"] == {"q1": "один ответ", "q2": "один ответ"}


# =========================================================================================
# 2. Run-scoped cleanup — identity guard (MAJOR-C1)
# =========================================================================================


async def test_cancel_of_parked_question_clears_own_flag(tmp_path):
    runner, store, journal, ws, speaks = make_runner(tmp_path)
    store.start_task("tk", "t", TaskStatus.RUNNING, 0.0)

    gate = asyncio.create_task(_ask(runner, _one_question()))
    await asyncio.sleep(0)
    assert store.awaiting_answer is True

    gate.cancel()  # deadline/cancel/supersede: CancelledError propagates through `await fut`
    with pytest.raises(asyncio.CancelledError):
        await gate

    assert store.awaiting_answer is False  # own finally cleaned up
    assert runner._pending_answer is None


async def test_superseded_run_finally_does_not_clobber_successor(tmp_path):
    runner, store, journal, ws, speaks = make_runner(tmp_path)
    store.start_task("tk", "t", TaskStatus.RUNNING, 0.0)

    first = asyncio.create_task(_ask(runner, _one_question(q="q1")))
    await asyncio.sleep(0)
    fut1 = runner._pending_answer

    # a successor run parks its own question, overwriting the slot (the superseding-run scenario).
    second = asyncio.create_task(_ask(runner, _one_question(q="q2")))
    await asyncio.sleep(0)
    fut2 = runner._pending_answer
    assert fut1 is not fut2
    assert store.awaiting_answer is True

    # the superseded run dies — its finally must NOT touch the successor's future/flag (identity guard).
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert runner._pending_answer is fut2  # successor slot intact
    assert store.awaiting_answer is True  # successor's flag intact

    assert runner.provide_answer("ответ на q2") is True
    result = await second
    assert result["hookSpecificOutput"]["updatedInput"]["answers"]["q2"] == "ответ на q2"
    assert store.awaiting_answer is False


# =========================================================================================
# 3. provide_answer — no pending → False
# =========================================================================================


def test_provide_answer_no_pending_returns_false(tmp_path):
    runner, store, *_ = make_runner(tmp_path)
    assert runner.provide_answer("никто не спрашивал") is False
    assert store.awaiting_answer is False


async def test_provide_answer_idempotent_second_call_false(tmp_path):
    runner, store, journal, ws, speaks = make_runner(tmp_path)
    store.start_task("tk", "t", TaskStatus.RUNNING, 0.0)
    gate = asyncio.create_task(_ask(runner, _one_question()))
    await asyncio.sleep(0)

    assert runner.provide_answer("первый") is True
    assert runner.provide_answer("второй") is False  # already resolved
    result = await gate
    assert result["hookSpecificOutput"]["updatedInput"]["answers"]["Какой формат?"] == "первый"


# =========================================================================================
# 4. state.py — liveness / render / snapshot / transience
# =========================================================================================


def test_liveness_ok_while_awaiting_even_when_stale(tmp_path):
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    store.heartbeat(0.0)
    # far past the unreachable threshold — would be UNREACHABLE if not for awaiting (MAJOR-R1).
    assert store.liveness(10_000.0, 120, 300) == Liveness.UNREACHABLE
    store.set_awaiting()
    assert store.liveness(10_000.0, 120, 300) == Liveness.OK


def test_render_state_awaiting_marker_is_redacted(tmp_path):
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    store.start_task("tk", "секретная задача", TaskStatus.RUNNING, 0.0)
    store.set_awaiting()
    rendered = store.render_state(1.0, 120, 300)
    assert "ждёт твоего ответа" in rendered
    assert "детали озвучены голосом" in rendered


def test_render_state_template_awaiting_phrase(tmp_path):
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    store.start_task("tk", "t", TaskStatus.RUNNING, 0.0)
    store.heartbeat(0.0)
    store.set_awaiting()
    assert store.render_state_template(0.0, 120, 300) == "Кора ждёт твоего ответа на свой вопрос."


def test_snapshot_awaiting_bool_gated_running(tmp_path):
    clock = FakeClock(0.0)
    store = TaskStore(clock)
    store.start_task("tk", "t", TaskStatus.RUNNING, 0.0)
    store.set_awaiting()
    assert store.snapshot(1.0, 120, 300)["awaiting_answer"] is True

    # non-RUNNING task → gated off even if the flag is (spuriously) still set (R6).
    store.apply_event(KoraEvent("e", "task_completed", EventClass.NARRATABLE, {}, "готово", 2.0))
    assert store.task.status == TaskStatus.COMPLETED
    assert store.snapshot(3.0, 120, 300)["awaiting_answer"] is False


def test_awaiting_is_transient_not_persisted(tmp_path):
    clock = FakeClock(0.0)
    store = TaskStore(clock, journal_dir=str(tmp_path))
    store.start_task("t1", "задача", TaskStatus.RUNNING, 0.0)
    store.set_awaiting()
    assert store.awaiting_answer is True

    # a restart (dead runner) must NOT strand a stale «ждёт ответа» — the flag is never written.
    store2 = TaskStore(FakeClock(0.0), journal_dir=str(tmp_path))
    assert store2.task is not None and store2.task.id == "t1"
    assert store2.awaiting_answer is False


# =========================================================================================
# 5. tools.py — answer_kora handler / dedup / schema / registration
# =========================================================================================


async def test_answer_kora_delivered_when_pending(tmp_path):
    handlers, store, journal = make_answer_handlers(tmp_path, on_answer=lambda t: True)
    handlers.begin_turn("t1")
    res = await handlers.answer_kora(text="ответ")
    assert res == {"outcome": "answer_delivered"}


async def test_answer_kora_no_pending_when_bridge_says_false(tmp_path):
    handlers, store, journal = make_answer_handlers(tmp_path, on_answer=lambda t: False)
    handlers.begin_turn("t1")
    res = await handlers.answer_kora(text="ответ")
    assert res == {"outcome": "no_pending_question"}


async def test_answer_kora_no_pending_when_unwired(tmp_path):
    handlers, store, journal = make_answer_handlers(tmp_path, on_answer=None)
    handlers.begin_turn("t1")
    res = await handlers.answer_kora(text="ответ")
    assert res == {"outcome": "no_pending_question"}


async def test_answer_kora_verbatim_reaches_bridge(tmp_path):
    seen: list[str] = []
    handlers, store, journal = make_answer_handlers(tmp_path, on_answer=lambda t: (seen.append(t) or True))
    handlers.begin_turn("t1")
    await handlers.answer_kora(text="ЗЮЗЯБЛИК-7788")
    assert seen == ["ЗЮЗЯБЛИК-7788"]  # verbatim, no rewriting (§2.9)


async def test_answer_kora_dedup_within_turn(tmp_path):
    calls: list[str] = []
    handlers, store, journal = make_answer_handlers(tmp_path, on_answer=lambda t: (calls.append(t) or True))
    record = journal.begin_turn("реплика")
    handlers.begin_turn(record.turn_id)

    r1 = await handlers.answer_kora(text="раз")
    # B14: a genuine same-turn retry re-issues the SAME text → deduped, on_answer NOT called again.
    # (A different answer in the same turn — a correction — must NOT dedup; see test_b14.)
    r2 = await handlers.answer_kora(text="раз")

    assert r1 == r2
    assert calls == ["раз"]
    # P8: record_tool_call carries the deduped flag (False first, True on the repeat).
    ak = [tc for tc in record.tool_calls if tc["name"] == "answer_kora"]
    assert [tc["result"]["deduped"] for tc in ak] == [False, True]


async def test_answer_kora_dedup_resets_new_turn(tmp_path):
    calls: list[str] = []
    handlers, store, journal = make_answer_handlers(tmp_path, on_answer=lambda t: (calls.append(t) or True))
    handlers.begin_turn("t1")
    await handlers.answer_kora(text="раз")
    handlers.begin_turn("t2")
    await handlers.answer_kora(text="два")  # new turn → executes again
    assert calls == ["раз", "два"]


def test_all_schemas_has_answer_kora_with_description():
    names = {s.name for s in ALL_SCHEMAS}
    assert "answer_kora" in names
    assert ANSWER_KORA_SCHEMA.required == ["text"]
    assert ANSWER_KORA_SCHEMA.description  # non-empty (P7)
    assert "text" in ANSWER_KORA_SCHEMA.properties


def test_register_all_answer_kora_no_cancel_on_interruption():
    from pipecat.services.openai.llm import OpenAILLMService

    class _Handlers:
        async def submit_task(self, **kw):
            return {}

        async def confirm_task(self, **kw):
            return {}

        async def get_task_status(self):
            return {}

        async def request_cancel(self):
            return {}

        async def answer_kora(self, **kw):
            return {}

    llm = OpenAILLMService(api_key="fake", model="gpt-4.1")
    register_all(llm, _Handlers())
    # S5: barge-in must NOT drop the answer.
    assert llm._functions["answer_kora"].cancel_on_interruption is False


# =========================================================================================
# 6. prompt.py — «д» + routing rule 9 (with carve-out), gated by owed
# =========================================================================================


def test_prompt_answer_kora_capability_and_routing_when_owed_on():
    prompt = build_system_prompt(SynapseConfig(include_owed_prompt_rules=True))
    assert "answer_kora" in prompt
    assert "д)" in prompt
    assert "9." in prompt  # routing rule
    # cancel/status carve-out (MAJOR-R2): both remain reachable mid-question.
    assert "request_cancel" in prompt
    assert "get_task_status" in prompt
    # R4: do not re-read the question — Кора voiced it.
    assert "не пересказывай" in prompt


def test_prompt_answer_kora_gated_off_by_owed_killswitch():
    prompt = build_system_prompt(SynapseConfig(include_owed_prompt_rules=False))
    assert "answer_kora" not in prompt
    assert "9." not in prompt
    assert "д)" not in prompt


# =========================================================================================
# 7. app.py — on_answer wired to KoraRunner.provide_answer
# =========================================================================================


def test_app_wires_on_answer_to_provide_answer(tmp_path):
    from synapse.pipeline.app import build_host

    cfg = SynapseConfig(
        google_api_key="fake-google-key",
        openrouter_api_key="fake-openrouter-key",
        anthropic_api_key="fake-anthropic-key",
        deepgram_api_key="fake-deepgram-key",
        fish_audio_api_key="fake-fish-key",
        fish_reference_id="fake-fish-ref",
        journal_dir=str(tmp_path),
    )
    host = build_host(cfg)
    assert host.kora_runner is not None
    # bound-method equality: same __self__/__func__.
    assert host.bridge.on_answer == host.kora_runner.provide_answer
