"""ADV-1/ADV-2: советник в СБОРе и сменные персоны."""
from __future__ import annotations

import pytest

from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.prompt import STAGE_RULES_COLLECT, build_system_prompt

def _fake_cfg(tmp_path, **kw):
    return SynapseConfig(
        google_api_key="fake", openrouter_api_key="fake", anthropic_api_key="fake",
        deepgram_api_key="fake", fish_audio_api_key="fake", fish_reference_id="fake",
        journal_dir=str(tmp_path), **kw,
    )


def test_collect_rules_are_advisory_without_round_limit():
    assert "двух раундов" not in STAGE_RULES_COLLECT
    assert "советник" in STAGE_RULES_COLLECT.lower()
    assert "риск" in STAGE_RULES_COLLECT.lower()
    assert "скоуп" in STAGE_RULES_COLLECT.lower()
    assert "формулируй" in STAGE_RULES_COLLECT
    assert "отправляй" in STAGE_RULES_COLLECT
    assert "propose_request" in STAGE_RULES_COLLECT
    assert "Не запускай Кору" in STAGE_RULES_COLLECT
    assert "коротк" in STAGE_RULES_COLLECT.lower()
    assert STAGE_RULES_COLLECT.startswith("\n\nСТАДИЯ COLLECT — СБОР:")


def test_collect_rules_carry_no_prompt_mines():
    assert "9." not in STAGE_RULES_COLLECT
    assert "д)" not in STAGE_RULES_COLLECT


def test_confab_hardening_precedes_collect_and_persona_layers():
    from synapse.prompt import (
        CANON_PHRASE_STALE_KORA,
        CONFAB_HARDENING_NOTE,
        build_persona_block,
    )

    persona = build_persona_block("скептик")
    prompt = build_system_prompt(
        SynapseConfig(), stage_block=STAGE_RULES_COLLECT, persona_block=persona
    )

    assert "Я этого не говорил." in CONFAB_HARDENING_NOTE
    assert "В [СОСТОЯНИЕ] этого результата нет." in CONFAB_HARDENING_NOTE
    assert "временная метка «Начата» не означает «только что»" in CONFAB_HARDENING_NOTE
    assert "Я этого не умею." in CONFAB_HARDENING_NOTE
    assert "Не знаю: данных о прогрессе и сроках нет." in CONFAB_HARDENING_NOTE
    assert f"«{CANON_PHRASE_STALE_KORA}. Подожди или попробуй позже.»" in CONFAB_HARDENING_NOTE
    assert CONFAB_HARDENING_NOTE.lower().count("не вызывай инструменты") == 3
    assert "ответь РОВНО" in CONFAB_HARDENING_NOTE
    assert prompt.index(CONFAB_HARDENING_NOTE) < prompt.index(STAGE_RULES_COLLECT)
    assert prompt.index(STAGE_RULES_COLLECT) < prompt.index(persona)


def test_status_tool_schema_does_not_override_false_attribution_correction():
    from synapse.dispatcher.tools import GET_TASK_STATUS_SCHEMA

    description = GET_TASK_STATUS_SCHEMA.description.lower()
    assert "приписывает" in description
    assert "не вызывай инструмент" in description
    assert "confab-шаблону" in description


def test_false_attribution_classifier_is_narrow():
    from synapse.prompt import is_false_attribution

    assert is_false_attribution("Ты же говорил, что файл готов")
    assert is_false_attribution("ТЫ СКАЗАЛ: сервер перезапущен")
    assert not is_false_attribution("Скажи, готов ли файл")
    assert not is_false_attribution("Что ты говорил про архитектуру?")


@pytest.mark.asyncio
async def test_false_attribution_turn_hides_tools_then_restores_them(tmp_path):
    from synapse.dispatcher.tools import ALL_SCHEMAS
    from synapse.pipeline.app import build_host

    class CapturingLLM:
        def __init__(self):
            self.tool_names = []

        async def complete(self, messages, tools):
            self.tool_names.append([tool.name for tool in tools])
            return "Я этого не говорил. Дополнительный неподтверждённый хвост.", []

    host = build_host(_fake_cfg(tmp_path))
    llm = CapturingLLM()
    host.text_loop._llm = llm
    th = host.threads.create("тред")

    _, correction = await host.text_loop.ingest_user_turn(
        "Ты же говорил, что файл готов", thread_id=th.id
    )
    await host.text_loop.ingest_user_turn("Какой сейчас статус?", thread_id=th.id)

    assert llm.tool_names[0] == []
    assert correction == "Я этого не говорил. В [СОСТОЯНИЕ] этого результата нет."
    assert set(llm.tool_names[1]) == {schema.name for schema in ALL_SCHEMAS}


@pytest.mark.asyncio
async def test_voice_false_attribution_hides_tools_then_restores_them(tmp_path):
    from openai import NOT_GIVEN
    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator
    from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
    from synapse.pipeline.app import build_host, build_session_pipeline

    session = build_session_pipeline(build_host(_fake_cfg(tmp_path)))
    stt = next(
        processor
        for processor in session.pipeline.processors
        if isinstance(processor, DeepgramFluxSTTService)
    )
    context = next(
        processor.context
        for processor in session.pipeline.processors
        if isinstance(processor, LLMUserAggregator)
    )
    handler = stt._event_handlers["on_end_of_turn"].handlers[0]

    await handler(stt, "Ты же говорил, что файл готов")
    assert context.tools is NOT_GIVEN

    await handler(stt, "Какой сейчас статус?")
    assert context.tools is not NOT_GIVEN


def test_conservative_stages_keep_bare_base_prompt(tmp_path):
    from synapse.pipeline.app import build_host

    host = build_host(_fake_cfg(tmp_path))
    th = host.threads.create("тред")
    base = build_system_prompt(host.cfg)
    assert STAGE_RULES_COLLECT in host.turn_context_for(th.id).system_prompt
    host.threads.set_stage(th.id, "propose")
    host.threads.set_stage(th.id, "code")
    assert host.turn_context_for(th.id).system_prompt == base
    host.threads.set_stage(th.id, "done")
    assert host.turn_context_for(th.id).system_prompt == base
    assert host.turn_context_for(None).system_prompt == base


def test_default_persona_config_and_env():
    assert SynapseConfig().default_persona == "техлид"
    assert SynapseConfig.from_env({"SYNAPSE_DEFAULT_PERSONA": "скептик"}).default_persona == "скептик"
    assert SynapseConfig.from_env({}).default_persona == "техлид"
    assert SynapseConfig.from_env({"SYNAPSE_DEFAULT_PERSONA": ""}).default_persona == "техлид"


def test_thread_persona_persist_roundtrip(tmp_path):
    from synapse.threads import ThreadStore

    store = ThreadStore(FakeClock(1_000.0), tmp_path / "threads")
    th = store.create("тред")
    assert th.persona is None
    assert store.set_persona(th.id, "скептик") is True
    assert store.get(th.id).persona == "скептик"
    assert ThreadStore(FakeClock(2_000.0), tmp_path / "threads").get(th.id).persona == "скептик"
    assert store.set_persona(th.id, None) is True
    assert ThreadStore(FakeClock(3_000.0), tmp_path / "threads").get(th.id).persona is None
    assert store.set_persona("нет-такого", "техлид") is False


def test_thread_json_without_persona_field_loads_as_none(tmp_path):
    import json
    from synapse.threads import ThreadStore

    root = tmp_path / "threads"
    root.mkdir(parents=True)
    (root / "abc.json").write_text(json.dumps({
        "id": "abc", "title": "старый", "stage": "collect",
        "created_ts": 1.0, "updated_ts": 1.0, "task_ids": [],
    }), encoding="utf-8")
    assert ThreadStore(FakeClock(10.0), root).get("abc").persona is None


def test_persona_catalog_and_hygiene():
    import re
    from synapse.prompt import PERSONA_PREAMBLE, PERSONA_PRESETS, build_persona_block

    assert set(PERSONA_PRESETS) == {"техлид", "скептик", "продакт", "ментор"}
    for name in PERSONA_PRESETS:
        block = build_persona_block(name)
        assert block.startswith("\n\nПЕРСОНА — ")
        assert PERSONA_PREAMBLE in block
        assert "9." not in block
        assert "д)" not in block
        assert "СТАДИЯ " not in block
        assert re.search(r"\d", block) is None
    assert build_persona_block("несуществующая") == ""


def test_bare_prompt_carries_no_persona():
    assert "ПЕРСОНА" not in build_system_prompt(SynapseConfig())


def test_persona_block_appended_last_after_stage_block():
    from synapse.prompt import build_persona_block

    p = build_persona_block("скептик")
    out = build_system_prompt(SynapseConfig(), stage_block=STAGE_RULES_COLLECT, persona_block=p)
    assert out.endswith(p)
    assert out.index(STAGE_RULES_COLLECT) < out.index(p)


def test_turn_context_persona_between_stage_and_state(tmp_path):
    from synapse.bridge.state import TaskStore
    from synapse.dispatcher.turn_context import build_turn_context
    from synapse.prompt import build_persona_block

    cfg = SynapseConfig(journal_dir=str(tmp_path))
    clock = FakeClock(1000.0)
    p = build_persona_block("техлид")
    ctx = build_turn_context(
        cfg=cfg, store=TaskStore(clock), clock=clock, thread_id="t1",
        stage_block_for=lambda tid: STAGE_RULES_COLLECT, persona_block_for=lambda tid: p,
    )
    assert ctx.system_message.index(STAGE_RULES_COLLECT) < ctx.system_message.index(p) < ctx.system_message.rindex("[СОСТОЯНИЕ]")


def test_voice_and_http_persona_parity(tmp_path):
    from synapse.bridge.state import TaskStore
    from synapse.dispatcher.turn_context import build_turn_context
    from synapse.prompt import build_persona_block

    cfg = SynapseConfig(journal_dir=str(tmp_path))
    clock = FakeClock(1000.0)
    store = TaskStore(clock)
    stage = lambda tid: STAGE_RULES_COLLECT
    persona = lambda tid: build_persona_block("продакт")
    http = build_turn_context(cfg=cfg, store=store, clock=clock, thread_id="t1", stage_block_for=stage, persona_block_for=persona)
    voice = build_turn_context(cfg=cfg, store=store, clock=clock, thread_id="t1", stage_block_for=stage, persona_block_for=persona)
    assert http.system_message == voice.system_message
    assert "ПЕРСОНА — продакт" in http.system_message


def test_first_voice_turn_gets_collect_advisor_and_default_persona(tmp_path):
    """Новый voice-тред должен существовать до сборки system message первого хода."""
    import asyncio

    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator
    from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
    from synapse.pipeline.app import build_host, build_session_pipeline

    host = build_host(_fake_cfg(tmp_path))
    session = build_session_pipeline(host)
    stt = next(
        p for p in session.pipeline.processors if isinstance(p, DeepgramFluxSTTService)
    )
    context = next(
        p.context for p in session.pipeline.processors if isinstance(p, LLMUserAggregator)
    )
    handler = stt._event_handlers["on_end_of_turn"].handlers[0]

    assert host.voice_thread["id"] is None
    asyncio.run(handler(stt, "обсуди со мной идею нового приложения"))

    system = context.get_messages()[0]["content"]
    assert host.voice_thread["id"] is not None
    assert STAGE_RULES_COLLECT in system
    assert "ПЕРСОНА — техлид" in system


@pytest.mark.asyncio
async def test_dispatcher_loop_passes_persona_resolver(tmp_path):
    from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
    from synapse.bridge.state import TaskStore
    from synapse.dispatcher.loop import DispatcherTurnLoop
    from synapse.dispatcher.tools import KoraBridge, ToolHandlers
    from synapse.journal import TurnJournal
    from synapse.prompt import build_persona_block

    class CaptureLLM:
        def __init__(self): self.messages = []
        async def complete(self, messages, tools):
            self.messages.append(messages)
            return "", []

    clock = FakeClock(0.0)
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
                          cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    handlers = ToolHandlers(KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg), journal)
    llm = CaptureLLM()
    loop = DispatcherTurnLoop(llm, handlers, confirm, store, journal, clock, cfg,
                              stage_block_for=lambda tid: STAGE_RULES_COLLECT,
                              persona_block_for=lambda tid: build_persona_block("ментор"))
    await loop.ingest_user_turn("проверка", thread_id="t1")
    assert "ПЕРСОНА — ментор" in llm.messages[-1][0]["content"]


def test_app_persona_default_override_and_stage_gate(tmp_path):
    from synapse.pipeline.app import build_host

    host = build_host(_fake_cfg(tmp_path))
    th = host.threads.create("тред")
    assert "ПЕРСОНА — техлид" in host.turn_context_for(th.id).system_prompt
    host.threads.set_persona(th.id, "скептик")
    assert "ПЕРСОНА — скептик" in host.turn_context_for(th.id).system_prompt
    host.threads.set_stage(th.id, "propose")
    assert "ПЕРСОНА — скептик" in host.turn_context_for(th.id).system_prompt
    host.threads.set_stage(th.id, "code")
    assert "ПЕРСОНА" not in host.turn_context_for(th.id).system_prompt
    host.threads.set_stage(th.id, "collect")
    assert host.text_loop is not None
    assert "ПЕРСОНА — скептик" in host.text_loop._persona_block_for(th.id)


def test_app_invalid_default_persona_degrades_to_no_block(tmp_path):
    from synapse.pipeline.app import build_host

    host = build_host(_fake_cfg(tmp_path, default_persona="нет-такой"))
    th = host.threads.create("тред")
    sp = host.turn_context_for(th.id).system_prompt
    assert "ПЕРСОНА" not in sp
    assert STAGE_RULES_COLLECT in sp


def _persona_handlers(tmp_path):
    from synapse.bridge.confirm import ConfirmFlow, KeywordClassifier
    from synapse.bridge.state import TaskStore
    from synapse.dispatcher.tools import KoraBridge, ToolHandlers
    from synapse.journal import TurnJournal
    from synapse.threads import ThreadStore

    clock = FakeClock(0.0)
    cfg = SynapseConfig()
    store = TaskStore(clock)
    journal = TurnJournal(str(tmp_path / "j"), clock)
    confirm = ConfirmFlow(store, clock, KeywordClassifier(cfg.destructive_keywords), journal,
                          cfg.affirm_words, cfg.deny_words, cfg.max_rereadbacks, cfg.confirm_timeout_s)
    threads = ThreadStore(clock, tmp_path / "threads")
    th = threads.create("тред")
    bridge = KoraBridge(store=store, confirm_flow=confirm, clock=clock, cfg=cfg,
                        threads=threads, thread_id_for=lambda: th.id)
    return ToolHandlers(bridge, journal), threads, th, bridge


@pytest.mark.asyncio
async def test_set_persona_validates_persists_and_next_turn_sees_block(tmp_path):
    from synapse.bridge.state import TaskStore
    from synapse.dispatcher.turn_context import build_turn_context
    from synapse.prompt import build_persona_block

    handlers, threads, th, _ = _persona_handlers(tmp_path)
    handlers.begin_turn("t1")
    assert await handlers.set_persona(name="Скептик") == {"outcome": "persona_set", "persona": "скептик"}
    assert threads.get(th.id).persona == "скептик"
    cfg = SynapseConfig()

    def resolver(tid):
        t = threads.get(tid) if tid else None
        if t is None or t.stage not in ("collect", "propose"):
            return ""
        return build_persona_block(t.persona or cfg.default_persona)

    clock = FakeClock(1.0)
    ctx = build_turn_context(cfg=cfg, store=TaskStore(clock), clock=clock,
                             thread_id=th.id, persona_block_for=resolver)
    assert "ПЕРСОНА — скептик" in ctx.system_prompt


@pytest.mark.asyncio
async def test_set_persona_unknown_name_refuses_with_catalog(tmp_path):
    handlers, threads, th, _ = _persona_handlers(tmp_path)
    handlers.begin_turn("t1")
    await handlers.set_persona(name="скептик")
    handlers.end_turn()
    handlers.begin_turn("t2")
    res = await handlers.set_persona(name="джокер")
    assert res["outcome"] == "unknown_persona"
    assert set(res["catalog"]) == {"техлид", "скептик", "продакт", "ментор"}
    assert threads.get(th.id).persona == "скептик"


@pytest.mark.asyncio
async def test_set_persona_without_thread_or_store(tmp_path):
    handlers, _, _, bridge = _persona_handlers(tmp_path)
    bridge.thread_id_for = lambda: None
    handlers.begin_turn("t1")
    assert (await handlers.set_persona(name="техлид"))["outcome"] == "no_active_thread"
    bridge.threads = None
    handlers.begin_turn("t2")
    assert (await handlers.set_persona(name="техлид"))["outcome"] == "dispatcher_unavailable"


def test_set_persona_schema_registered():
    from synapse.dispatcher.tools import ALL_SCHEMAS, SET_PERSONA_SCHEMA

    assert "set_persona" in {s.name for s in ALL_SCHEMAS}
    assert SET_PERSONA_SCHEMA.required == ["name"]
    assert SET_PERSONA_SCHEMA.description


def test_register_all_set_persona_no_cancel_on_interruption(tmp_path):
    from pipecat.services.openai.llm import OpenAILLMService
    from synapse.dispatcher.tools import register_all

    handlers, _, _, _ = _persona_handlers(tmp_path)
    llm = OpenAILLMService(api_key="fake", model="gpt-4.1")
    register_all(llm, handlers)
    assert llm._functions["set_persona"].cancel_on_interruption is False
