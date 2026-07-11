"""import app без pyaudio; build_tier_services с фейковыми env-ключами, без сети (item 24)."""
import importlib

import httpx


def test_import_app_module_without_pyaudio():
    # synapse.pipeline.app must not import pipecat.transports.local.audio (or anything else
    # that needs pyaudio/portaudio) at module load time (S4) -- if it did, this import would
    # raise ImportError in any environment without pyaudio installed.
    module = importlib.import_module("synapse.pipeline.app")
    assert hasattr(module, "build_host")
    assert hasattr(module, "build_session_pipeline")
    assert hasattr(module, "run")
    assert hasattr(module, "SynapseHost")
    assert hasattr(module, "SynapseSession")


def test_build_tier_services_with_fake_env_keys_no_network():
    from synapse.cascade.services import build_tier_services
    from synapse.config import SynapseConfig

    cfg = SynapseConfig(
        google_api_key="fake-google-key",
        openrouter_api_key="fake-openrouter-key",
        anthropic_api_key="fake-anthropic-key",
    )
    services, labels = build_tier_services(cfg)
    assert len(services) == 2
    assert len(labels) == 2
    assert [label.endpoint for label in labels] == ["openrouter", "anthropic"]
    assert [label.paid for label in labels] == [True, True]


def test_build_tier_services_sets_request_timeout_on_every_client_no_network():
    # Р-14: without this, the SDK default (httpx.Timeout(600, connect=5.0)) applies and a
    # hung tier hangs the whole turn instead of failing over (research §2 item 6). connect
    # stays at the SDK's own 5.0 default, not cfg.request_timeout_s (critique MINOR).
    from synapse.cascade.services import build_tier_services
    from synapse.config import SynapseConfig

    cfg = SynapseConfig(
        google_api_key="fake-google-key",
        openrouter_api_key="fake-openrouter-key",
        anthropic_api_key="fake-anthropic-key",
        request_timeout_s=10.0,
    )
    services, _ = build_tier_services(cfg)
    expected = httpx.Timeout(10.0, connect=5.0)
    for svc in services:
        assert svc._client.timeout == expected


def test_validate_voice_keys_hard_fails_with_missing_key_list():
    from synapse.config import SynapseConfig

    cfg = SynapseConfig()
    try:
        cfg.validate_voice_keys()
        assert False, "expected RuntimeError for missing keys"
    except RuntimeError as e:
        assert "OPENROUTER_API_KEY" in str(e)
        assert "FISH_REFERENCE_ID" in str(e)


def test_build_pipeline_wires_guard_hooks_around_switcher_and_guarded_aggregator(tmp_path):
    # §3 item 3: GenerationGuard is wired into the live pipeline, not left as a documented
    # gap -- two GenerationStartHooks around llm_switcher (DOWNSTREAM pre, UPSTREAM post) and
    # a guarded (non-flat) assistant aggregator subclass, per research §2.2.
    from pipecat.pipeline.llm_switcher import LLMSwitcher
    from pipecat.processors.aggregators.llm_response_universal import LLMAssistantAggregator
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat.services.fish.tts import FishAudioTTSService

    from synapse.config import SynapseConfig
    from synapse.pipeline.app import build_host, build_session_pipeline
    from synapse.pipeline.context_guard import GenerationStartHook

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
    session = build_session_pipeline(host)
    procs = session.pipeline.processors

    switcher_idx = next(i for i, p in enumerate(procs) if isinstance(p, LLMSwitcher))
    pre_hook, post_hook = procs[switcher_idx - 1], procs[switcher_idx + 1]
    assert isinstance(pre_hook, GenerationStartHook) and pre_hook._direction == FrameDirection.DOWNSTREAM
    assert isinstance(post_hook, GenerationStartHook) and post_hook._direction == FrameDirection.UPSTREAM

    assistant = next(p for p in procs if isinstance(p, LLMAssistantAggregator))
    assert isinstance(assistant, LLMAssistantAggregator)
    assert type(assistant) is not LLMAssistantAggregator  # guarded subclass, not the flat base

    tts = next(p for p in procs if isinstance(p, FishAudioTTSService))
    assert tts._settings.model == cfg.fish_tts_model

    # Regression guard (no-audio bug): assistant_aggregator terminates TextFrame/LLMTextFrame/
    # LLMFullResponse* -- upstream of tts it starves synthesis. Must stay downstream of tts.
    assert procs.index(assistant) > procs.index(tts)


import pytest
from synapse.dispatcher.tools import KoraBridge

def test_pipeline_cascade_events_not_wired_to_journal(tmp_path):
    from synapse.config import SynapseConfig
    from synapse.pipeline.app import build_host, build_session_pipeline

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
    session = build_session_pipeline(host)
    strategy = session.llm_switcher.strategy

    # Check that handlers list is not empty
    assert len(strategy._event_handlers["on_retry"].handlers) > 0
    assert len(strategy._event_handlers["on_all_failed"].handlers) > 0


def test_voice_pipeline_speak_ledger_gap(tmp_path, monkeypatch):
    from synapse.config import SynapseConfig
    from synapse.pipeline.app import build_host

    captured_bridge = []
    original_init = KoraBridge.__init__
    def mock_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        captured_bridge.append(self)
    monkeypatch.setattr(KoraBridge, "__init__", mock_init)

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
    bridge = captured_bridge[0]

    from synapse.bridge.state import KoraEvent, EventClass
    critical_ev = KoraEvent(id="e1", type="task_completed", cls=EventClass.CRITICAL, payload={}, speak_text="ok", ts=0.0)
    host.speak_ledger.register_critical(critical_ev)

    assert "e1" in host.speak_ledger._pending
    bridge.on_speak("ok")
    # In the expected (fixed) code, this should register speak and mark it spoken
    assert host.speak_ledger._pending["e1"].spoken is True
