"""import app без pyaudio; build_tier_services с фейковыми env-ключами, без сети (item 24)."""
import importlib


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


def test_validate_voice_keys_hard_fails_with_missing_key_list():
    from synapse.config import SynapseConfig

    cfg = SynapseConfig()
    try:
        cfg.validate_voice_keys()
        assert False, "expected RuntimeError for missing keys"
    except RuntimeError as e:
        assert "GOOGLE_API_KEY" in str(e)
        assert "FISH_REFERENCE_ID" in str(e)
