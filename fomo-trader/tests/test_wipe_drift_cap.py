"""Hermetic arithmetic checks for the BSC wipe-drift ticket cap."""

import pytest

from fomo_trader import wipe_drift_cap_native


@pytest.mark.parametrize("signal,entry,rate,cfg_buy,buy,expected,capped,slip", [
    ({"signal_price_usd": 8}, 0.011, 800, 0.01, 0.02,
     0.005, True, 0.1),
    ({"signal_price_usd": 8}, 0.011, 800, 0.01, 0.004,
     0.004, False, 0.1),
    ({"signal_price_usd": 8}, 0.009, 800, 0.01, 0.02,
     0.02, False, -0.1),
    ({"signal_price_usd": 8}, 0.01, 800, 0.01, 0.02,
     0.02, False, 0.0),
    ({}, 0.011, 800, 0.01, 0.02,
     0.02, False, None),
    ({"signal_price_usd": 0}, 0.011, 800, 0.01, 0.02,
     0.02, False, None),
    ({"signal_price_usd": "invalid"}, 0.011, 800, 0.01, 0.02,
     0.02, False, None),
    ({"signal_price_usd": 8}, 0.011, None, 0.01, 0.02,
     0.02, False, None),
    ({"signal_price_usd": 8}, 0.011, 0, 0.01, 0.02,
     0.02, False, None),
    ({"signal_price_usd": 8}, 0.011, 800, 0, 0.02,
     0.02, False, 0.1),
])
def test_wipe_drift_cap_native(signal, entry, rate, cfg_buy, buy,
                               expected, capped, slip):
    # The helper receives values directly; this test invokes no network or I/O.
    output, was_capped, observed_slip = wipe_drift_cap_native(
        signal, entry, rate, cfg_buy, buy)
    assert output == pytest.approx(expected)
    assert output <= buy
    assert was_capped is capped
    if slip is None:
        assert observed_slip is None
    else:
        assert observed_slip == pytest.approx(slip)
