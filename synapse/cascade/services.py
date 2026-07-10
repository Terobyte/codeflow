"""build_tier_services — constructs the three Р-14 cascade tiers as pipecat LLMService
instances, each tagged with a {endpoint, model} label for the journal. Also CostCap
(§11.5): per-PAID-attempt increment+check (R9), not per-turn — a full-cascade turn (tier1
free miss -> tier2 paid attempt -> tier3 paid attempt) costs up to 2, and an overshoot past
the cap is bounded to <=1 call past the limit (documented in README).
"""
from __future__ import annotations

from dataclasses import dataclass

from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openrouter.llm import OpenRouterLLMService

from synapse.config import SynapseConfig


@dataclass
class TierLabel:
    endpoint: str
    model: str
    paid: bool


def build_tier_services(cfg: SynapseConfig) -> tuple[list, list[TierLabel]]:
    tier1 = OpenAILLMService(api_key=cfg.google_api_key, base_url=cfg.tier1_base_url, model=cfg.tier1_model)
    tier2 = OpenRouterLLMService(api_key=cfg.openrouter_api_key, model=cfg.tier2_model)
    tier3 = AnthropicLLMService(api_key=cfg.anthropic_api_key or "unset", model=cfg.tier3_model)
    services = [tier1, tier2, tier3]
    labels = [
        TierLabel(endpoint="google-ai-studio", model=cfg.tier1_model, paid=False),
        TierLabel(endpoint="openrouter", model=cfg.tier2_model, paid=True),
        TierLabel(endpoint="anthropic", model=cfg.tier3_model, paid=True),
    ]
    return services, labels


class CostCap:
    def __init__(self, max_paid_calls_per_day: int | None) -> None:
        self._max = max_paid_calls_per_day
        self._count = 0
        self._tripped = False

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def count(self) -> int:
        return self._count

    def record_paid_attempt(self) -> bool:
        """Call once per paid-tier attempt. Returns True if this attempt may proceed, False
        if the cap was already tripped before this call (so the overshoot is bounded to the
        single attempt that trips it)."""
        if self._max is None:
            return True
        if self._tripped:
            return False
        self._count += 1
        if self._count >= self._max:
            self._tripped = True
        return True

    def reset(self) -> None:
        self._count = 0
        self._tripped = False
