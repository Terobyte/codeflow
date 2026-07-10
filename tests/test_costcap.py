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
