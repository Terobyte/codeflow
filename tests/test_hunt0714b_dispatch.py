"""Hunt 2026-07-14b — failing tests that PROVE B20 and B22.

Both tests assert CORRECT behavior, so each is RED on the current tree and flips
GREEN once the ledgered defect is fixed (assertions untouched).

- B20 (concurrency): history compaction mutates the SHARED per-thread list across an
  `await`, dropping a concurrent same-thread turn's committed (user,assistant) pair.
  The interleaving is forced with explicit asyncio.Event sync points — no sleep-and-hope.

- B22 (structural): a multi-tool assistant turn must map to a SINGLE user message that
  coalesces every tool_result block (the canonical Anthropic Messages shape, per the
  parallel-tool-use contract "return all tool_result blocks in a single user message").
  The current `_to_anthropic_messages` emits N consecutive user messages instead.
"""
import asyncio

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import TaskStore
from synapse.config import SynapseConfig
from synapse.dispatcher.llm_client import _to_anthropic_messages
from synapse.dispatcher.loop import DispatcherTurnLoop
from synapse.dispatcher.tools import KoraBridge, ToolHandlers
from synapse.journal import TurnJournal


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def now(self):
        return self.t


# ---------------------------------------------------------------------------------------
# B20 -- compaction rewrites the SHARED history list from a pre-await snapshot, silently
# overwriting a concurrent same-thread turn's committed (user,assistant) pair.
# ---------------------------------------------------------------------------------------


class _CompactionBlockingLLM:
    """Deterministic sync points. The FIRST compaction call (tools==[]) sets
    `compaction_entered` and blocks on `resume_compaction` until the test releases it;
    that is turn A, suspended inside `_maybe_compact` *after* it snapshotted `tail`.
    Every other call returns immediately, so turn B runs its whole turn (including its
    own quick compaction) and commits its (user,assistant) pair to the shared history
    while A is parked. When A resumes it rebinds the shared list from its stale `tail`."""

    def __init__(self):
        self.compaction_entered = asyncio.Event()
        self.resume_compaction = asyncio.Event()
        self._blocked_once = False

    async def complete(self, messages, tools):
        is_compaction = not tools
        if is_compaction and not self._blocked_once:
            self._blocked_once = True
            self.compaction_entered.set()
            await self.resume_compaction.wait()
            return "[СЖАТО A]", []
        if is_compaction:
            return "[СЖАТО B]", []
        users = [m for m in messages if m["role"] == "user"]
        last = users[-1]["content"] if users else ""
        return f"reply:{last}", []


def _make_dispatcher_loop(tmp_path, llm):
    clock = FakeClock()
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(
        store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
        cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s,
    )
    handlers = ToolHandlers(KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg), journal)
    return DispatcherTurnLoop(llm, handlers, confirm, store, journal, clock, cfg)


async def test_B20_compaction_across_await_drops_concurrent_turn_commit(tmp_path):
    llm = _CompactionBlockingLLM()
    loop = _make_dispatcher_loop(tmp_path, llm)
    cfg = SynapseConfig()
    threshold = cfg.dispatcher_compact_after  # default 40
    assert threshold > 0

    # Pre-fill the SHARED thread history above the compaction threshold so
    # `_maybe_compact` triggers on the very next turn. Alternating user/assistant so the
    # mechanical `cut` lands on a user boundary (as the compactor requires).
    history = loop._history_for("thread-1")
    for i in range(threshold + 2):
        role = "user" if i % 2 == 0 else "assistant"
        history.append({"role": role, "content": f"{role}-{i}"})

    async def turn_a():
        _, reply = await loop.ingest_user_turn("msg A", thread_id="thread-1")
        return reply

    async def turn_b():
        # Start only once A is provably parked inside its compaction LLM call.
        await llm.compaction_entered.wait()
        _, reply = await loop.ingest_user_turn("msg B", thread_id="thread-1")
        return reply

    task_a = asyncio.create_task(turn_a())
    task_b = asyncio.create_task(turn_b())

    reply_b = await task_b            # B fully commits while A is parked
    llm.resume_compaction.set()       # let A's compaction resume and rebind
    reply_a = await task_a

    assert reply_a == "reply:msg A" and reply_b == "reply:msg B", (
        f"harness sanity: replies crossed (a={reply_a!r}, b={reply_b!r})"
    )

    final = loop._history_for("thread-1")
    contents = [m.get("content") for m in final]
    assert any(m["role"] == "user" and m["content"] == "msg B" for m in final), (
        "B20: concurrent turn B committed its user message to the shared history, but "
        "turn A's compaction rebound the list from a pre-await snapshot and dropped it "
        f"(final contents={contents!r})"
    )
    assert "reply:msg B" in contents, (
        "B20: turn B's committed assistant reply was overwritten by A's stale-tail "
        f"compaction rebind (final contents={contents!r})"
    )


# ---------------------------------------------------------------------------------------
# B22 -- a multi-tool assistant turn must coalesce all tool_result blocks into ONE user
# message (canonical Anthropic Messages shape). Current code emits N consecutive user
# messages, one tool_result each. Structural test, no live API.
# ---------------------------------------------------------------------------------------


def test_B22_parallel_tool_results_coalesce_into_single_user_message():
    # The exact shape loop.py produces for a turn with 2 parallel tool_use blocks:
    # one assistant announce carrying both tool_use, then one {"role":"tool"} message
    # per call.
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "сделай два дела"},
        {
            "role": "assistant",
            "content": "запускаю",
            "tool_calls": [
                {"id": "tu_1", "name": "get_task_status", "arguments": {}},
                {"id": "tu_2", "name": "bind_project", "arguments": {"id": "p"}},
            ],
        },
        {"role": "tool", "tool_call_id": "tu_1", "name": "get_task_status",
         "content": "{\"status\": \"running\"}"},
        {"role": "tool", "tool_call_id": "tu_2", "name": "bind_project",
         "content": "{\"outcome\": \"bound\"}"},
    ]

    _system, out = _to_anthropic_messages(messages)
    roles = [m["role"] for m in out]

    ai = roles.index("assistant")
    # The assistant turn itself must carry both tool_use blocks.
    tool_use_ids = {b["id"] for b in out[ai]["content"] if isinstance(b, dict) and b.get("type") == "tool_use"}
    assert tool_use_ids == {"tu_1", "tu_2"}, f"harness sanity: assistant tool_use ids={tool_use_ids!r}"

    # Canonical contract: the message immediately after the assistant turn is ONE user
    # message carrying every tool_result block.
    assert ai + 1 < len(out) and out[ai + 1]["role"] == "user", (
        f"B22: expected a user message right after the assistant turn, got roles={roles!r}"
    )
    coalesced = out[ai + 1]["content"]
    result_ids = {
        b["tool_use_id"] for b in coalesced
        if isinstance(b, dict) and b.get("type") == "tool_result"
    }
    assert result_ids == {"tu_1", "tu_2"}, (
        "B22: the two tool_result blocks must coalesce into the SINGLE user message "
        "following the assistant turn (canonical Anthropic parallel-tool-use shape); "
        f"instead that message carries {result_ids!r} and the shape is roles={roles!r}"
    )

    # And there must NOT be a second consecutive user message carrying a leftover
    # tool_result (the exact non-canonical split the ledger describes).
    trailing_result_msgs = [
        m for m in out[ai + 2:]
        if m["role"] == "user" and isinstance(m.get("content"), list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
    ]
    assert not trailing_result_msgs, (
        "B22: tool_result blocks are split across multiple consecutive user messages "
        f"(non-canonical); extra tool_result-bearing user messages={trailing_result_msgs!r}"
    )
