import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest

from same_day_loop import shrink_midrange, EDGE_THRESHOLD


def _grid(n=101):
    return [i / (n - 1) for i in range(n)]


@pytest.mark.parametrize("fair", [0.70, 0.30, 0.39, 0.61, 0.0, 1.0, 0.05, 0.95])
@pytest.mark.parametrize("mid", [0.10, 0.37, 0.50, 0.90])
def test_identity_outside_band(fair, mid):
    assert shrink_midrange(fair, mid) == fair


def test_band_edges_are_inclusive():
    assert shrink_midrange(0.40, 0.20) == pytest.approx(0.35)
    assert shrink_midrange(0.60, 0.80) == pytest.approx(0.65)


def test_moves_toward_mid_inside_band():
    assert shrink_midrange(0.50, 0.37, alpha=0.25) == pytest.approx(0.4675)
    assert shrink_midrange(0.50, 0.63, alpha=0.25) == pytest.approx(0.5325)


def test_never_flips_side():
    for fair in _grid():
        for mid in _grid():
            new = shrink_midrange(fair, mid)
            if fair > mid:
                assert new > mid
            elif fair < mid:
                assert new < mid
            else:
                assert new == mid


def test_result_between_fair_and_mid():
    for fair in _grid():
        for mid in _grid():
            new = shrink_midrange(fair, mid)
            assert min(fair, mid) - 1e-12 <= new <= max(fair, mid) + 1e-12


def test_alpha_zero_is_identity():
    for fair in _grid():
        for mid in _grid():
            assert shrink_midrange(fair, mid, alpha=0.0) == fair


def test_result_stays_in_unit_interval():
    for fair in _grid():
        for mid in _grid():
            assert 0.0 <= shrink_midrange(fair, mid) <= 1.0


def test_edge_can_only_shrink():
    for fair in _grid():
        for mid in _grid():
            new = shrink_midrange(fair, mid)
            assert abs(new - mid) <= abs(fair - mid) + 1e-12


def test_edge_threshold_unchanged():
    assert EDGE_THRESHOLD == 0.05
