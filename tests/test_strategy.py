"""§3 item 7 (Plan v2): unwrap of a list-shaped Google OpenAI-compat 429 body in
`_extract_http_info`. Live-confirmed defect (Senior probes, scratchpad/probe_output.txt): a
real 429 body is `[{"error": {...}}]`, not a mapping -- the openai SDK's own unwrap leaves a
non-mapping body as the whole list in `e.body`, and the pre-fix `_extract_http_info` treated
any non-dict body as absent, blinding `classify_error` to RPD (a whole-day quota exhaustion)
and RPM's `retryDelay` alike."""
from synapse.cascade.breaker import ErrorKind
from synapse.cascade.classify import classify_error
from synapse.cascade.strategy import _extract_http_info


class FakeHTTPException(Exception):
    """Stand-in for the openai/anthropic SDK exception shape `_extract_http_info` reads:
    `.status_code`, `.response` (for headers), `.body`."""

    def __init__(self, status_code, body, headers=None):
        super().__init__("fake http error")
        self.status_code = status_code
        self.body = body
        self.response = _FakeResponse(headers) if headers is not None else None


class _FakeResponse:
    def __init__(self, headers):
        self.headers = headers


# A live Google OpenAI-compat 429 body -- see Senior probes -- is a LIST containing one
# {"error": {...}} object, with both a QuotaFailure (PerDay here) and a RetryInfo detail.
_LIST_BODY_RPD = [
    {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
                    ],
                },
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"},
            ],
        }
    }
]


def test_extract_http_info_unwraps_list_body_to_inner_error_dict():
    exc = FakeHTTPException(429, _LIST_BODY_RPD, headers={})
    status, body, headers = _extract_http_info(exc)
    assert status == 429
    assert body == _LIST_BODY_RPD[0]  # unwrapped to the {"error": {...}} dict inside
    assert headers == {}


def test_extract_http_info_still_handles_a_plain_dict_body():
    # Regression: the already-supported dict-body form (e.g. OpenRouter, or a Google error
    # that for some reason arrives unwrapped) must keep working unchanged.
    dict_body = _LIST_BODY_RPD[0]
    exc = FakeHTTPException(429, dict_body)
    status, body, headers = _extract_http_info(exc)
    assert status == 429
    assert body == dict_body


def test_extract_http_info_drops_a_body_that_is_neither_dict_nor_list_of_dicts():
    exc = FakeHTTPException(429, "not a mapping or a list of mappings")
    _, body, _ = _extract_http_info(exc)
    assert body is None

    exc_empty_list = FakeHTTPException(429, [])
    _, body_empty, _ = _extract_http_info(exc_empty_list)
    assert body_empty is None


def test_google_429_list_body_classifies_as_rpd_via_extract_and_classify():
    """The end-to-end path handle_error() drives: a live list-shaped 429 body must still
    classify as RPD (a whole-day mute), not silently degrade to the RPM header-fallback."""
    exc = FakeHTTPException(429, _LIST_BODY_RPD, headers={})
    status, body, headers = _extract_http_info(exc)

    kind, retry_after = classify_error(status, body, headers)
    assert kind == ErrorKind.RPD
    assert retry_after == 34.0


def test_control_pre_fix_would_have_blinded_classify_error_to_rpm_fallback():
    """Control: without the unwrap, `_extract_http_info` would hand classify_error a `None`
    body (list is not a dict), which falls through to the generic RPM/Retry-After-header
    fallback -- exactly the pre-fix bug (Senior probes): RPD exhaustion re-classified as a
    60s RPM mute, re-hitting the guaranteed 429 all day. Simulate the pre-fix body here
    directly (not by calling the fixed function) to pin down what the bug looked like."""
    pre_fix_body = None  # what the old `if not isinstance(body, dict): body = None` gave
    kind, retry_after = classify_error(429, pre_fix_body, {})
    assert kind == ErrorKind.RPM
    assert retry_after is None


import asyncio

import pytest
from pipecat.processors.frame_processor import FrameProcessor
from synapse.cascade.breaker import CircuitBreaker
from synapse.cascade.services import CostCap, TierLabel
from synapse.cascade.strategy import SynapseFailoverStrategy
from synapse.clock import FakeClock
from synapse.pipeline.context_guard import GenerationGuard
from synapse.journal import AlertKind
from pipecat.frames.frames import ErrorFrame

class MockService(FrameProcessor):
    def __init__(self, name="mock_service"):
        super().__init__(name=name)  # FrameProcessor.name is a read-only property; set via ctor

@pytest.mark.asyncio
async def test_strategy_tail_tier_event_naming():
    clock = FakeClock(0.0)
    service1 = MockService("svc1")
    service2 = MockService("svc2")
    services = [service1, service2]
    
    breaker = CircuitBreaker(tier_count=2, rpm_mute_s=60.0, rpd_reset_hour_utc=8)
    labels = [
        TierLabel(endpoint="openrouter", model="m1", paid=True),
        TierLabel(endpoint="anthropic", model="m2", paid=True),
    ]
    cost_cap = CostCap(max_paid_calls_per_day=3)
    generation_guard = GenerationGuard()
    
    strategy = SynapseFailoverStrategy(
        services,
        breaker=breaker,
        labels=labels,
        cost_cap=cost_cap,
        generation_guard=generation_guard,
        clock=clock
    )
    
    events_called = []
    
    try:
        @strategy.event_handler("on_tail_tier")
        async def _on_tail_tier(strat):
            events_called.append("on_tail_tier")
    except ValueError:
        # If registering on_tail_tier raises ValueError (because it's not a registered event on the strategy)
        # then the test should fail as expected.
        pass
        
    exc_429 = FakeHTTPException(429, {}, {"Retry-After": "10"})
    error_frame = ErrorFrame(error="429 from svc1", exception=exc_429, processor=service1)
    
    await strategy.handle_error(error_frame)
    # pipecat dispatches event handlers as fire-and-forget tasks (is_sync=False), so the
    # on_tail_tier side effect lands on the next loop tick -- yield once before asserting.
    await asyncio.sleep(0)
    assert "on_tail_tier" in events_called
