"""B-SIM-1 (MAJOR, data-integrity) — red proof.

`DispatcherTurnLoop._maybe_compact` (synapse/dispatcher/loop.py, ~419-481) replaces the
OLDER half of a thread's history with a single LLM-generated summary message. The summary
comes from an LLM call that is told to preserve tokens "дословно" (verbatim), but a lossy
summarizer cannot reliably reproduce high-entropy opaque tokens (secrets, hashes, random
IDs) — it can silently drop them from its prose. There is no mechanism that pins exact
literals past the summarizer, so such a token present in the pre-compaction history is
gone from the post-compaction history.

This test builds the loop with the SAME harness pattern as
tests/test_chat_commands.py::_loop (FakeClock + TaskStore + TurnJournal + ConfirmFlow +
ToolHandlers/KoraBridge, DispatcherTurnLoop wired directly — no network, no real LLM), and
uses `force_compact` (tests/test_chat_commands.py::test_force_compact_noop_on_empty_history
exercises the same entrypoint) which calls `_maybe_compact(thread_id, history,
threshold_override=1)` — the exact call this bughunt brief asks for.

Fix-agnostic invariant under test: an exact high-entropy token present in history BEFORE
compaction must still appear verbatim somewhere in history AFTER compaction, even when the
summarizer's own output omits it. On current code this fails — the older half (which
contains the token) is wholly replaced by the summary text, which lacks it.
"""
from __future__ import annotations

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import TaskStore
from synapse.config import SynapseConfig
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.dispatcher.tools import KoraBridge, ToolHandlers
from synapse.journal import TurnJournal


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def now(self):
        return self.t


class LossySummarizerLLM:
    """Simulates a real compaction summarizer: prose summary that DELIBERATELY omits the
    high-entropy token, exactly like a real LLM would silently drop it. complete() is async
    and returns the (summary, tool_calls) 2-tuple _maybe_compact expects."""

    def __init__(self):
        self.seen = []

    async def complete(self, messages, tools):
        self.seen.append(messages)
        return "Пользователь настраивал деплой; обсудили конфиг.", []


def _loop(tmp_path, llm=None):
    # Mirrors tests/test_chat_commands.py::_loop construction pattern exactly.
    clock = FakeClock()
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
                          cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    handlers = ToolHandlers(KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg), journal)
    llm = llm or LossySummarizerLLM()
    return DispatcherTurnLoop(llm, handlers, confirm, store, journal, clock, cfg), llm


async def test_b_sim_1_compaction_drops_high_entropy_token(tmp_path):
    TOKEN = "RC-9F3A-7712"
    loop_obj, llm = _loop(tmp_path)

    # Seed a thread with the token in the OLDEST message so it lands in the older half that
    # _maybe_compact cuts and replaces (cut = len//2, advanced to the next role=="user"; with
    # 4 messages cut==2, so history[:2] — the token message plus its assistant reply — is
    # what gets summarized away).
    hist = loop_obj._history_for("th-secret")
    hist.append({"role": "user", "content": f"мой ключ доступа {TOKEN}, запомни его"})
    hist.append({"role": "assistant", "content": "принято"})
    hist.append({"role": "user", "content": "давай теперь настроим деплой"})
    hist.append({"role": "assistant", "content": "конфиг деплоя обсудили"})

    # Premise guard: token really is present pre-compaction, and there's more than one
    # message so threshold_override=1 will actually force a cut (len(history) > threshold).
    assert any(TOKEN in (m.get("content") or "") for m in hist), "premise broken: token not seeded"
    assert len(hist) > 1, "premise broken: nothing to compact"

    # force_compact is the exact entrypoint the brief calls out: it invokes
    # _maybe_compact(thread_id, history, threshold_override=1) on the live history list.
    await loop_obj.force_compact("th-secret")

    # The summarizer really ran (sanity: this isn't a no-op).
    assert llm.seen, "compaction never called the summarizer — test setup is broken"

    assert any(TOKEN in (m.get("content") or "") for m in hist), (
        f"B-SIM-1: high-entropy token {TOKEN!r} present before compaction is GONE after "
        f"compaction — the lossy summarizer's output replaced the older half wholesale and "
        f"nothing pinned the exact literal. Post-compaction history: {hist!r}"
    )
