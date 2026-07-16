import pytest

from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
from synapse.bridge.state import TaskStore
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.dispatcher.tools import (
    ALL_SCHEMAS,
    CONFIRM_TASK_SCHEMA,
    GET_TASK_STATUS_SCHEMA,
    REQUEST_CANCEL_SCHEMA,
    SUBMIT_TASK_SCHEMA,
    KoraBridge,
    ToolHandlers,
    register_all,
)
from synapse.journal import TurnJournal


def make_handlers(tmp_path):
    clock = FakeClock(0.0)
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path), clock, session_id="s")
    classifier = KeywordClassifier(cfg.destructive_keywords)
    confirm_flow = ConfirmFlow(
        store, clock, classifier, journal, cfg.affirm_words, cfg.deny_words,
        cfg.max_rereadbacks, cfg.confirm_timeout_s,
    )
    speaks: list[str] = []
    bridge = KoraBridge(store=store, confirm_flow=confirm_flow, clock=clock, on_speak=speaks.append, cfg=cfg)
    handlers = ToolHandlers(bridge, journal)
    return handlers, store, journal, speaks


def test_stage_schemas_present_with_expected_shape():
    names = {s.name for s in ALL_SCHEMAS}
    assert names == {
        "submit_task", "confirm_task", "get_task_status", "request_cancel", "answer_kora",
        "propose_request", "gate_action", "bind_project", "set_persona",
    }
    assert SUBMIT_TASK_SCHEMA.required == ["text"]
    assert CONFIRM_TASK_SCHEMA.properties["decision"]["enum"] == ["confirm", "deny"]
    assert GET_TASK_STATUS_SCHEMA.required == []
    assert REQUEST_CANCEL_SCHEMA.required == []


@pytest.mark.asyncio
async def test_dedup_latch_makes_same_turn_retry_a_noop(tmp_path):
    handlers, store, journal, speaks = make_handlers(tmp_path)
    handlers.begin_turn("t1")
    r1 = await handlers.submit_task(text="удали старое")
    # B14: a real intra-turn cascade retry re-invokes the SAME tool call with the SAME args — that
    # is the case the dedup latch exists for, and it must return the first result as a no-op.
    # (A DIFFERENT-args same-name call must NOT dedup — proven separately in test_b14.)
    r2 = await handlers.submit_task(text="удали старое")
    assert r1 == r2
    assert sum(1 for s in speaks if "удали старое" in s) == 1


@pytest.mark.asyncio
async def test_dedup_latch_resets_on_new_turn(tmp_path):
    handlers, store, journal, speaks = make_handlers(tmp_path)
    calls: list[int] = []
    original = store.request_cancel

    def counting_request_cancel():
        calls.append(1)
        return original()

    store.request_cancel = counting_request_cancel  # type: ignore[method-assign]

    handlers.begin_turn("t1")
    await handlers.request_cancel()
    await handlers.request_cancel()  # same turn -- deduped, must not call the store again
    assert len(calls) == 1

    handlers.begin_turn("t2")
    await handlers.request_cancel()  # new turn -- latch resets, executes again
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_get_task_status_marks_grounding_via_journal(tmp_path):
    handlers, store, journal, speaks = make_handlers(tmp_path)
    record = journal.begin_turn("как дела?")
    handlers.begin_turn(record.turn_id)
    await handlers.get_task_status()
    assert any(tc["name"] == "get_task_status" for tc in record.tool_calls)


def test_register_all_sets_cancel_on_interruption_flags():
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

    llm = OpenAILLMService(api_key="fake", model="gpt-4.1")
    register_all(llm, _Handlers())
    assert llm._functions["submit_task"].cancel_on_interruption is False
    assert llm._functions["confirm_task"].cancel_on_interruption is False
    assert llm._functions["request_cancel"].cancel_on_interruption is False
    assert llm._functions["get_task_status"].cancel_on_interruption is True


@pytest.mark.asyncio
async def test_register_all_integration_with_real_pipecat_function_call_params(tmp_path):
    """A2: register_all on a real pipecat LLMService, a manually assembled
    FunctionCallParams with a fake result_callback -- exercises the pipecat-facing wrapper
    end to end (not just the pure handlers)."""
    from pipecat.services.llm_service import FunctionCallParams
    from pipecat.services.openai.llm import OpenAILLMService

    handlers, store, journal, speaks = make_handlers(tmp_path)
    handlers.begin_turn("t1")
    llm = OpenAILLMService(api_key="fake", model="gpt-4.1")
    register_all(llm, handlers)

    results = []

    async def fake_result_callback(result, *, properties=None):
        results.append(result)

    item = llm._functions["submit_task"]
    params = FunctionCallParams(
        function_name="submit_task",
        tool_call_id="tc1",
        arguments={"text": "скачай книгу"},
        llm=llm,
        pipeline_worker=None,
        context=None,
        result_callback=fake_result_callback,
    )
    await item.handler(params)
    assert len(results) == 1
    assert results[0]["outcome"] == "committed"
    assert store.task is not None
    assert store.task.text == "скачай книгу"
