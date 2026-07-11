"""SynapseFailoverStrategy — Р-14 cascade failover, subclassing pipecat's
ServiceSwitcherStrategyFailover.

pipecat's `ServiceSwitcher.__init__` instantiates the strategy as `strategy_type(services)`
— exactly one positional arg — so this class cannot take extra constructor args directly.
Use `build_strategy_type(...)` below (a `functools.partial`) as the `strategy_type` passed
to `LLMSwitcher(llms, strategy_type=...)`.

Selection is breaker-aware `first_available()`, not pipecat's default "just next" (Р-14);
classify + mute the failing tier; mark the in-flight generation aborted BEFORE switching
(S1, via GenerationGuard — covers both orders of the ErrorFrame/LLMFullResponseEndFrame
race); CostCap gates every paid-tier attempt (R9); on_retry/on_tail_tier/on_all_failed are
exposed as pipecat event handlers so app.py can wire journal + arbiter without this module
depending on either.
"""
from __future__ import annotations

import functools
from typing import Any

from pipecat.frames.frames import ErrorFrame, TTSSpeakFrame
from pipecat.pipeline.service_switcher import ServiceSwitcherStrategyFailover
from pipecat.processors.frame_processor import FrameProcessor

from synapse.cascade.breaker import CircuitBreaker
from synapse.cascade.classify import classify_error
from synapse.cascade.services import CostCap, TierLabel
from synapse.clock import Clock
from synapse.pipeline.context_guard import GenerationGuard

ALL_TIERS_FAILED_PHRASE = "связь с мозгом потеряна"


class SynapseFailoverStrategy(ServiceSwitcherStrategyFailover):
    def __init__(
        self,
        services: list[FrameProcessor],
        *,
        breaker: CircuitBreaker,
        labels: list[TierLabel],
        cost_cap: CostCap,
        generation_guard: GenerationGuard,
        clock: Clock,
    ) -> None:
        super().__init__(services)
        self._breaker = breaker
        self._labels = labels
        self._cost_cap = cost_cap
        self._generation_guard = generation_guard
        self._clock = clock
        self._register_event_handler("on_retry")
        self._register_event_handler("on_tail_tier")
        self._register_event_handler("on_all_failed")

    def active_tier_index(self) -> int | None:
        if self._active_service is None:
            return None
        return self._services.index(self._active_service)

    async def handle_error(self, error: ErrorFrame) -> FrameProcessor | None:
        now = self._clock.now()
        current_idx = self.active_tier_index()

        # S1: mark the in-flight generation aborted BEFORE switching tiers, covering both
        # orders of the ErrorFrame/LLMFullResponseEndFrame race (see context_guard.py).
        self._generation_guard.mark_aborted(self._generation_guard.current_generation)

        status, body, headers = _extract_http_info(getattr(error, "exception", None))
        kind, retry_after = classify_error(status, body, headers)
        if current_idx is not None:
            self._breaker.mute(current_idx, kind, now, retry_after)

        return await self._advance(now)

    async def _advance(self, now: float) -> FrameProcessor | None:
        next_idx = self._breaker.first_available(now)
        if next_idx is None:
            await self._fail_all()
            return None

        if self._labels[next_idx].paid:
            allowed = self._cost_cap.record_paid_attempt()
            if not allowed:
                await self._fail_all("cost_cap")
                return None

        result = await self._set_active_if_available(self._services[next_idx])
        if result is None:
            return None

        await self._call_event_handler("on_retry", next_idx)
        if next_idx == len(self._services) - 1:
            # Tail tier (Haiku, Р-14): alert only, never spoken — not an error for the ear.
            await self._call_event_handler("on_tail_tier")
        if self._cost_cap.tripped:
            for idx, label in enumerate(self._labels):
                if label.paid:
                    self._breaker.hard_mute(idx)
        return result

    async def _fail_all(self, reason: str | None = None) -> None:
        await self._call_event_handler("on_all_failed", reason)
        if self._services:
            await self._services[0].push_frame(
                TTSSpeakFrame(ALL_TIERS_FAILED_PHRASE, append_to_context=False)
            )


def build_strategy_type(
    breaker: CircuitBreaker,
    labels: list[TierLabel],
    cost_cap: CostCap,
    generation_guard: GenerationGuard,
    clock: Clock,
) -> Any:
    """A `functools.partial` suitable as `LLMSwitcher(llms, strategy_type=...)` — see the
    class docstring for why this indirection is required."""
    return functools.partial(
        SynapseFailoverStrategy,
        breaker=breaker,
        labels=labels,
        cost_cap=cost_cap,
        generation_guard=generation_guard,
        clock=clock,
    )


def _extract_http_info(exception: BaseException | None) -> tuple[int | None, dict | None, dict | None]:
    if exception is None:
        return None, None, None
    status = getattr(exception, "status_code", None)
    response = getattr(exception, "response", None)
    headers = dict(response.headers) if response is not None and hasattr(response, "headers") else None
    body = getattr(exception, "body", None)
    # A live Google OpenAI-compat 429 body is `[{"error": {...}}]`, not a mapping -- the
    # openai SDK's own unwrap (`data = body.get("error", body) if is_mapping(body) else
    # body`, openai/_client.py:674) leaves a non-mapping body as the whole list, so `e.body`
    # here is that list, not the inner error dict. Unwrap it before the isinstance check
    # below, or classify_error() sees `body=None` and RPD (a whole-day mute) degrades to the
    # RPM fallback (60s) forever -- confirmed against a live 429 (Senior probes).
    if isinstance(body, list) and body and isinstance(body[0], dict):
        body = body[0]
    if not isinstance(body, dict):
        body = None
    return status, body, headers
