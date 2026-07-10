"""import app без pyaudio; build_tier_services с фейковыми env-ключами, без сети (item 24)."""
import importlib

import httpx


def test_import_app_module_without_pyaudio():
    # synapse.pipeline.app must not import pipecat.transports.local.audio (or anything else
    # that needs pyaudio/portaudio) at module load time (S4) -- if it did, this import would
    # raise ImportError in any environment without pyaudio installed.
    module = importlib.import_module("synapse.pipeline.app")
    assert hasattr(module, "build_pipeline")
    assert hasattr(module, "run")
    assert hasattr(module, "SynapseVoicePipeline")


def test_build_tier_services_with_fake_env_keys_no_network():
    from synapse.cascade.services import build_tier_services
    from synapse.config import SynapseConfig

    cfg = SynapseConfig(
        google_api_key="fake-google-key",
        openrouter_api_key="fake-openrouter-key",
        anthropic_api_key="fake-anthropic-key",
    )
    services, labels = build_tier_services(cfg)
    assert len(services) == 3
    assert len(labels) == 3
    assert [label.endpoint for label in labels] == ["google-ai-studio", "openrouter", "anthropic"]
    assert [label.paid for label in labels] == [False, True, True]


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
        assert "GOOGLE_API_KEY" in str(e)
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
    from synapse.pipeline.app import build_pipeline
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
    voice_pipeline = build_pipeline(cfg)
    procs = voice_pipeline.pipeline.processors

    switcher_idx = next(i for i, p in enumerate(procs) if isinstance(p, LLMSwitcher))
    pre_hook, post_hook = procs[switcher_idx - 1], procs[switcher_idx + 1]
    assert isinstance(pre_hook, GenerationStartHook) and pre_hook._direction == FrameDirection.DOWNSTREAM
    assert isinstance(post_hook, GenerationStartHook) and post_hook._direction == FrameDirection.UPSTREAM

    assistant = procs[switcher_idx + 2]
    assert isinstance(assistant, LLMAssistantAggregator)
    assert type(assistant) is not LLMAssistantAggregator  # guarded subclass, not the flat base

    tts = next(p for p in procs if isinstance(p, FishAudioTTSService))
    assert tts._settings.model == cfg.fish_tts_model
