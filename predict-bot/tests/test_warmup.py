import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest

from same_day_loop import (
    in_warmup, shrink_midrange, EDGE_THRESHOLD, WINDOW_WARMUP_MIN,
)

WO = 1_760_000_000


def test_default_warmup_is_three_minutes():
    assert WINDOW_WARMUP_MIN == 3.0


@pytest.mark.parametrize("elapsed", [0, 60, 179])
def test_in_warmup_during_first_three_minutes(elapsed):
    assert in_warmup(WO, WO + elapsed) is True


@pytest.mark.parametrize("elapsed", [180, 181, 300, 899])
def test_not_in_warmup_from_180s_on(elapsed):
    assert in_warmup(WO, WO + elapsed) is False


def test_custom_warmup_min_respected():
    assert in_warmup(WO, WO + 299, warmup_min=5) is True
    assert in_warmup(WO, WO + 300, warmup_min=5) is False
    assert in_warmup(WO, WO + 59, warmup_min=1) is True
    assert in_warmup(WO, WO + 60, warmup_min=1) is False


def test_zero_warmup_never_skips():
    assert in_warmup(WO, WO, warmup_min=0) is False


def test_live15_loop_wiring_skips_only_warmup_windows():
    now_ts = WO + 10_000
    live15 = [
        ({"ticker": "KXBTC15M-FRESH"}, "BTC", now_ts - 60, now_ts - 60 + 900),
        ({"ticker": "KXETH15M-SETTLED"}, "ETH", now_ts - 300, now_ts - 300 + 900),
    ]
    evaluated = []
    for m, sym, wo, wc in live15:
        if in_warmup(wo, now_ts):
            continue
        evaluated.append(m["ticker"])
    assert evaluated == ["KXETH15M-SETTLED"]


def test_regression_edge_threshold_unchanged():
    assert EDGE_THRESHOLD == 0.05


def test_regression_shrink_midrange_unchanged():
    assert shrink_midrange(0.50, 0.37) == pytest.approx(0.4675)
    assert shrink_midrange(0.70, 0.37) == 0.70
