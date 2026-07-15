from synapse.cascade.services import CostCap


def test_cost_cap_none_disables_the_cap():
    cap = CostCap(None)
    for _ in range(1000):
        assert cap.record_paid_attempt() is True
    assert cap.tripped is False


def test_cost_cap_trips_at_max_but_allows_the_tripping_call():
    cap = CostCap(3)
    assert cap.record_paid_attempt() is True  # 1
    assert cap.record_paid_attempt() is True  # 2
    assert cap.tripped is False
    assert cap.record_paid_attempt() is True  # 3rd call trips it but is itself allowed
    assert cap.tripped is True
    # overshoot is bounded to <=1 call past the limit:
    assert cap.record_paid_attempt() is False
    assert cap.record_paid_attempt() is False


def test_cost_cap_reset_reopens_the_gate():
    cap = CostCap(1)
    cap.record_paid_attempt()
    assert cap.tripped is True
    cap.reset()
    assert cap.tripped is False
    assert cap.count == 0
    assert cap.record_paid_attempt() is True


def test_maybe_reset_anchoring_the_day_never_clears_a_same_day_trip():
    """A trip recorded before the cap ever saw a clock must survive the first maybe_reset.

    `record_paid_attempt()` without `now` is a sanctioned path (see test_host_singleton.py:76 —
    a paid call can happen before any session exists), and it counts the attempt while leaving
    `_reset_day` unset. `monitor_forever` then ticks `maybe_reset(now)` every heartbeat. If the
    None-path treated "counted but no anchor" as restart state to recover from, that first tick
    would wipe a legitimate same-day trip and let paid calls past the daily cap — a money bug.
    Only a day-bucket ADVANCE may clear the count.
    """
    cap = CostCap(1, rpd_reset_hour_utc=8)
    assert cap.record_paid_attempt() is True  # trips at max=1, no clock passed
    assert cap.tripped is True

    assert cap.maybe_reset(now=1000.0) is False, "anchoring the day is not a reset"
    assert cap.tripped is True, "the same-day trip was wiped by the first clocked tick"
    assert cap.count == 1
    assert cap.record_paid_attempt(now=1001.0) is False, "paid call got past a tripped daily cap"


def test_maybe_reset_still_clears_on_a_real_day_rollover():
    """The guard above must not cost the cap its actual purpose (B30): once the day-bucket
    advances, count and trip do clear."""
    cap = CostCap(1, rpd_reset_hour_utc=0)
    day0 = 86400.0 * 100
    assert cap.record_paid_attempt(now=day0) is True
    assert cap.tripped is True
    assert cap.record_paid_attempt(now=day0 + 3600) is False  # same day, still blocked

    assert cap.maybe_reset(now=day0 + 86400) is True, "day rollover must reset"
    assert cap.tripped is False
    assert cap.count == 0
    assert cap.record_paid_attempt(now=day0 + 86400) is True
