"""Wave-2 bug-hunt regression tests — cascade money/breaker layer.

FAILING (red) tests that pin the intended POST-FIX behavior for three Wave-2 defects
documented in bugs.md (Wave 2 section):

- B32  breaker.py:39  — `Retry-After: 0` (or negative) leaves the dead tier instantly
       selectable (`until == now`), causing a failover-to-self livelock. Fix floors the
       mute to a minimum window.
- B33  classify.py:40 / breaker.py:38-39 — a benign non-rate-limit 4xx (400/404/413) is
       classified as tier-health ERROR and mutes the tier for `rpm_mute_s`. Fix: a
       deterministic client error is fatal-to-turn but must NOT mute the tier.
- B30  services.py:80 / strategy.py:95-98 — a tripped CostCap never recovers (no
       clock-driven reset), permanently hard-muting every paid tier for the process
       lifetime despite the `_per_day` naming. Fix adds a day-boundary recovery.

Each test asserts POST-FIX behavior, so each fails RED against the current tree.
"""
from __future__ import annotations

from datetime import datetime, timezone

from synapse.cascade.breaker import CircuitBreaker, ErrorKind
from synapse.cascade.classify import classify_error
from synapse.cascade.services import CostCap


# --------------------------------------------------------------------------------------
# B32 — a zero/negative Retry-After must still mute the dead tier (floor the mute window)
# --------------------------------------------------------------------------------------
def test_b32_zero_or_negative_retry_after_still_mutes_the_dead_tier():
    now = 1000.0

    # Provider hands back `Retry-After: 0`. Pre-fix: until = now + 0.0 == now, and
    # is_muted checks `now < until` (1000.0 < 1000.0 -> False) => the just-failed tier is
    # instantly selectable again => handle_error re-picks tier 0 => failover-to-self
    # livelock draining the cost cap. Post-fix floors the mute to a minimum window.
    b = CircuitBreaker(tier_count=2, rpm_mute_s=60.0, rpd_reset_hour_utc=8)
    b.mute(0, ErrorKind.RPM, now=now, retry_after_s=0.0)

    assert b.is_muted(0, now=now)  # RED pre-fix: until == now => not muted
    assert b.first_available(now=now) != 0  # must not re-select the tier that just failed
    assert b.first_available(now=now) == 1  # should fail over to the healthy tier instead

    # A NEGATIVE retry-after (clock skew / a malformed provider header parsed to < 0) must
    # not un-mute the tier either -- pre-fix until = now - 5.0 < now => not muted.
    b_neg = CircuitBreaker(tier_count=2, rpm_mute_s=60.0, rpd_reset_hour_utc=8)
    b_neg.mute(0, ErrorKind.RPM, now=now, retry_after_s=-5.0)
    assert b_neg.is_muted(0, now=now)


# --------------------------------------------------------------------------------------
# B33 — a benign non-rate-limit 4xx must NOT mute the tier
# --------------------------------------------------------------------------------------
def test_b33_benign_4xx_does_not_mute_the_tier():
    now = 1000.0

    # Drive the real production path handle_error() uses: classify_error(status) -> mute().
    # A 400 (bad request) or 404 is a deterministic client error -- retrying it on the SAME
    # tier and muting the tier for rpm_mute_s (60s, blinding a HEALTHY tier) are both wrong.
    # Pre-fix classify_error(400) -> (ERROR, None); breaker.mute(ERROR) -> until = now + 60
    # => the tier is muted for a benign, deterministic request error.
    for status in (400, 404, 413):
        b = CircuitBreaker(tier_count=2, rpm_mute_s=60.0, rpd_reset_hour_utc=8)
        kind, retry_after = classify_error(status, body={}, headers={})
        b.mute(0, kind, now=now, retry_after_s=retry_after)

        assert not b.is_muted(0, now=now), f"{status} muted the tier at now (B33)"
        assert not b.is_muted(0, now=now + 1.0), f"{status} muted the tier past now (B33)"
        # A benign client error must never hard-mute the tier either.
        assert not b.is_muted(0, now=now + 10**9), f"{status} hard-muted the tier (B33)"


# --------------------------------------------------------------------------------------
# B30 — a tripped CostCap must recover after a day boundary passes
# --------------------------------------------------------------------------------------
def test_b30_costcap_recovers_after_day_boundary():
    # Trip the cap on a known day (pass `now` so the count is attributed to that day-bucket —
    # the entry point the daily-reset fix uses).
    day0 = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc).timestamp()
    cap = CostCap(max_paid_calls_per_day=1)
    assert cap.record_paid_attempt(now=day0) is True  # the tripping call is itself allowed
    assert cap.tripped is True  # confirm: cap is tripped and now blocks all paid calls
    assert cap.record_paid_attempt(now=day0) is False  # ... and it currently stays blocked

    # A day boundary passes. Post-fix, CostCap must expose a clock-driven recovery so a
    # monitor can un-trip the "per day" cap when the reset hour rolls over (mirroring the
    # breaker's RPD reset) -- the intended hook is `maybe_reset(now)`. Pre-fix there is NO
    # such recovery (reset()/reset_tier() have zero non-test callers), so the cap is stuck
    # tripped for the whole process lifetime == the B30 bug.
    day_later = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc).timestamp()  # past next reset
    recovered = False
    if hasattr(cap, "maybe_reset"):
        cap.maybe_reset(day_later)
        recovered = not cap.tripped

    assert recovered, "CostCap never recovers after a day boundary (B30): no clock-driven reset"
    assert cap.record_paid_attempt() is True  # paid calls are allowed again after the reset
