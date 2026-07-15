"""Red guards for Phase 0 findings.

These tests intentionally describe the security contracts, not today's partial
implementation.  They must stay red until the corresponding production fixes
exist; turning them green by weakening the assertions is not acceptable.
"""
from __future__ import annotations

import asyncio

import pytest

from synapse.cascade.services import CostCap
from synapse.clock import FakeClock
from synapse.config import SynapseConfig
from synapse.dispatcher import llm_client
from synapse.pipeline.app import build_host


@pytest.mark.asyncio
async def test_text_cap_reserves_the_last_slot_before_network_io():
    """A parallel second text request must be blocked before it reaches the provider."""
    guarded_cls = getattr(llm_client, "GuardedLLMClient", None)
    blocked_exc = getattr(llm_client, "CostCapBlocked", None)
    assert guarded_cls is not None, "C4 needs GuardedLLMClient; raw text requests bypass CostCap"
    assert blocked_exc is not None, "C4 needs a distinct CostCapBlocked fallback signal"

    class _BlockingProvider:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, messages, tools):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return "ok", []

    provider = _BlockingProvider()
    client = guarded_cls(provider, CostCap(1), FakeClock(0.0))
    first = asyncio.create_task(client.complete([], []))
    await provider.started.wait()

    try:
        # A dirty check-then-await implementation lets this call enter the
        # provider and hang behind `release`; bound it so the regression is a
        # normal assertion failure rather than a stuck test process.
        with pytest.raises(blocked_exc):
            await asyncio.wait_for(client.complete([], []), timeout=0.05)
        assert provider.calls == 1
    finally:
        provider.release.set()
        await first


def test_text_and_voice_channels_share_the_same_cost_cap(tmp_path):
    """build_host must not replace the cap after wiring GuardedLLMClient."""
    cfg = SynapseConfig(
        google_api_key="fake",
        openrouter_api_key="fake",
        anthropic_api_key="fake",
        deepgram_api_key="fake",
        fish_audio_api_key="fake",
        fish_reference_id="fake",
        journal_dir=str(tmp_path),
        max_paid_calls_per_day=1,
    )
    host = build_host(cfg)

    assert host.text_loop._llm._cost_cap is host.cost_cap
