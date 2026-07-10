from datetime import datetime, timezone

from synapse.cascade.breaker import CircuitBreaker, ErrorKind


def test_rpm_mute_for_configured_duration():
    b = CircuitBreaker(tier_count=3, rpm_mute_s=60.0, rpd_reset_hour_utc=8)
    b.mute(0, ErrorKind.RPM, now=1000.0)
    assert b.is_muted(0, now=1000.0)
    assert b.is_muted(0, now=1059.9)
    assert not b.is_muted(0, now=1060.0)


def test_rpm_mute_uses_retry_after_when_given():
    b = CircuitBreaker(3, 60.0, 8)
    b.mute(0, ErrorKind.RPM, now=0.0, retry_after_s=10.0)
    assert b.is_muted(0, now=9.9)
    assert not b.is_muted(0, now=10.0)


def test_rpd_mute_before_reset_hour_today():
    b = CircuitBreaker(3, 60.0, rpd_reset_hour_utc=8)
    now = datetime(2026, 7, 10, 3, 0, tzinfo=timezone.utc).timestamp()  # before 08:00 UTC
    b.mute(0, ErrorKind.RPD, now=now)
    reset_today = datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc).timestamp()
    assert b.is_muted(0, now=reset_today - 1)
    assert not b.is_muted(0, now=reset_today)


def test_rpd_mute_after_reset_hour_rolls_to_tomorrow():
    b = CircuitBreaker(3, 60.0, rpd_reset_hour_utc=8)
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc).timestamp()  # after 08:00 UTC
    b.mute(0, ErrorKind.RPD, now=now)
    reset_tomorrow = datetime(2026, 7, 11, 8, 0, tzinfo=timezone.utc).timestamp()
    assert b.is_muted(0, now=reset_tomorrow - 1)
    assert not b.is_muted(0, now=reset_tomorrow)


def test_auth_mutes_until_manual_reset():
    b = CircuitBreaker(3, 60.0, 8)
    b.mute(0, ErrorKind.AUTH, now=0.0)
    assert b.is_muted(0, now=10**9)
    b.reset_tier(0)
    assert not b.is_muted(0, now=10**9)


def test_first_available_skips_muted_tiers():
    b = CircuitBreaker(3, 60.0, 8)
    b.mute(0, ErrorKind.RPM, now=0.0)
    assert b.first_available(now=0.0) == 1
    b.mute(1, ErrorKind.AUTH, now=0.0)
    assert b.first_available(now=0.0) == 2
    b.mute(2, ErrorKind.RPM, now=0.0)
    assert b.first_available(now=0.0) is None


def test_mask_reports_auth_timestamp_and_none():
    b = CircuitBreaker(3, 60.0, 8)
    b.mute(0, ErrorKind.RPM, now=0.0)
    b.mute(1, ErrorKind.AUTH, now=0.0)
    mask = b.mask(now=0.0)
    assert mask[0] == 60.0
    assert mask[1] == "AUTH"
    assert mask[2] is None
