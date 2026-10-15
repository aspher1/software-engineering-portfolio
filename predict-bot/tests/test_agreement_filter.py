import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from same_day_loop import agrees_with_lean, AGREE_LEAN_MIN


def test_constant_is_point_five():
    assert AGREE_LEAN_MIN == 0.5


def test_agreement_yes_kept():
    # side=YES, mid=0.6 -> taken-side price 0.6 >= 0.5: keep
    assert agrees_with_lean("YES", 0.6) is True


def test_contrarian_yes_dropped():
    # side=YES, mid=0.3 -> taken-side price 0.3 < 0.5: drop
    assert agrees_with_lean("YES", 0.3) is False


def test_agreement_no_kept():
    # side=NO, mid=0.4 -> taken-side price 1-0.4=0.6 >= 0.5: keep
    assert agrees_with_lean("NO", 0.4) is True


def test_contrarian_no_dropped():
    # side=NO, mid=0.7 -> taken-side price 1-0.7=0.3 < 0.5: drop
    assert agrees_with_lean("NO", 0.7) is False


def test_boundary_taken_price_exactly_half_kept():
    # taken-side price exactly 0.5 is agreement, not a fade: keep
    assert agrees_with_lean("YES", 0.5) is True
    assert agrees_with_lean("NO", 0.5) is True
