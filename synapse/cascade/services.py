"""build_tier_services — constructs the two Р-14 cascade tiers as pipecat LLMService
instances, each tagged with a {endpoint, model} label for the journal. Also CostCap
(§11.5): per-PAID-attempt increment+check (R9), not per-turn — both tiers are paid, so a
full-cascade turn (tier1 paid attempt -> tier2 paid attempt) costs up to 2, and an overshoot
past the cap is bounded to <=1 call past the limit (documented in README).

Р-14 request timeout: pipecat's `create_client` doesn't forward a constructor `timeout=` to
AsyncOpenAI (it stops at `default_headers`); AnthropicLLMService does accept `client=` but
building a whole client ourselves duplicates its construction recipe. Simplest correct fix
for both tiers uniformly: set `.timeout` post-construction directly on the SDK client --
both AsyncOpenAI and AsyncAnthropic (openai/anthropic BaseClient) store it as a plain
attribute, read fresh per request, not baked into a per-call kwarg. Without this, the SDK
default (`httpx.Timeout(600, connect=5.0)`) applies, and a hung tier hangs the whole turn
instead of failing over.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.openrouter.llm import OpenRouterLLMService

from synapse.config import SynapseConfig


@dataclass
class TierLabel:
    endpoint: str
    model: str
    paid: bool


def build_tier_services(cfg: SynapseConfig) -> tuple[list, list[TierLabel]]:
    # B-CORE-17: model= депрекейтнут pipecat 0.0.105 в пользу settings=…Settings(model=…)
    tier1 = OpenRouterLLMService(api_key=cfg.openrouter_api_key,
                                 settings=OpenRouterLLMService.Settings(model=cfg.tier1_model))
    tier2 = AnthropicLLMService(api_key=cfg.anthropic_api_key or "unset",
                                settings=AnthropicLLMService.Settings(model=cfg.tier2_model))
    services = [tier1, tier2]

    # connect=5.0 kept at the SDK default explicitly -- a bare `httpx.Timeout(N)` would also
    # tighten connect to N, which is not what request_timeout_s is meant to bound (critique
    # MINOR).
    timeout = httpx.Timeout(cfg.request_timeout_s, connect=5.0)
    for svc in services:
        svc._client.timeout = timeout

    labels = [
        TierLabel(endpoint="openrouter", model=cfg.tier1_model, paid=True),
        TierLabel(endpoint="anthropic", model=cfg.tier2_model, paid=True),
    ]
    return services, labels


class CostCap:
    def __init__(self, max_paid_calls_per_day: int | None, rpd_reset_hour_utc: int = 0) -> None:
        self._max = max_paid_calls_per_day
        self._reset_hour = rpd_reset_hour_utc
        self._count = 0
        self._tripped = False
        self._reset_day: int | None = None  # the day-bucket the current count belongs to

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def count(self) -> int:
        return self._count

    def _day_bucket(self, now: float) -> int:
        # Number of whole days since epoch, with the boundary shifted to rpd_reset_hour_utc so it
        # matches the breaker's RPD reset semantics. B-CASC-1: clip at 0 — for `now` before the
        # reset hour on epoch day the raw value goes negative (e.g. reset_hour=8, now=7h → -1),
        # and a negative bucket makes the `bucket > _reset_day` advance trigger a premature reset
        # within the same calendar day (0 > -1). The rolling semantics only depend on RELATIVE
        # bucket differences, and any real-world `now` is far past epoch, so flooring at 0 keeps
        # the rolling logic correct everywhere it matters without a negative-bucket edge.
        return max(0, int((now - self._reset_hour * 3600) // 86400))

    def maybe_reset(self, now: float) -> bool:
        """B30: a "per day" cap must recover when the reset hour rolls over — otherwise one trip
        hard-blocks the cascade for the whole process lifetime. Establishes the day on first call;
        resets count+trip when the day-bucket advances. Returns True iff it reset.

        The None-path only ANCHORS the day — it must never also clear count/trip. B-CASC-3 argued
        that a restart could leave `_count`/`_tripped` carried over while `_reset_day` was None, and
        cleared them here to avoid a day-long hard block. That state is unreachable: `CostCap` is
        never rehydrated (the sole construction site, `pipeline/app.py:819`, always starts at
        count=0/tripped=False), so nothing survives a restart to recover from. What IS reachable is
        `record_paid_attempt()` called without `now` (a sanctioned path — see
        `tests/test_host_singleton.py:76`), which counts the attempt while leaving `_reset_day` None.
        Clearing here would then wipe a legitimate same-day trip on the next `maybe_reset` tick —
        and `monitor_forever` ticks it every heartbeat — letting paid calls past the daily cap."""
        bucket = self._day_bucket(now)
        if self._reset_day is None:
            # Anchor the day to the first `now` we see. Any count already recorded belongs to THIS
            # bucket, so it is deliberately preserved; only a bucket ADVANCE below may clear it.
            self._reset_day = bucket
            return False
        if bucket > self._reset_day:
            self._count = 0
            self._tripped = False
            self._reset_day = bucket
            return True
        return False

    def record_paid_attempt(self, now: float | None = None) -> bool:
        """Call once per paid-tier attempt. Returns True if this attempt may proceed, False
        if the cap was already tripped before this call (so the overshoot is bounded to the
        single attempt that trips it). Pass `now` so a day rollover un-trips the cap first."""
        if now is not None:
            self.maybe_reset(now)
        if self._max is None:
            return True
        if self._tripped:
            return False
        self._count += 1
        if self._count >= self._max:
            self._tripped = True
        return True

    def reset(self) -> None:
        # B-CORE-8: an explicit reset is a clean slate — restore the SAME state as __init__,
        # including the day anchor. Leaving `_reset_day` set would make reset() disagree with a
        # fresh CostCap; clearing it re-anchors the day on the next maybe_reset tick. (Unlike
        # maybe_reset's None-path, reset() deliberately clears count/trip, so re-anchoring is
        # correct here.) reset() is a test/admin helper — no production path calls it.
        self._count = 0
        self._tripped = False
        self._reset_day = None
