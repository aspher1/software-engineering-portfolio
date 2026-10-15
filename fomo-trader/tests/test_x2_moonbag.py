"""Hermetic tests for the X2 moonbag replay simulator.

All trades are synthetic. The live journal is never touched: this module must
be importable and testable with zero network, zero files, zero clock.
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis" / "x2_moonbag"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))

from moonbag import (moonbag_exit, moonbag_net, moonbag_metrics, split_is_oos,
                     drought_usd, runner_value_usd, EXTRA_LEG_NATIVE, TRAILS)
from replay import SLIPPAGE


def T(**kw):
    base = {"entry": 1.0, "peak": 1.0, "exit": 1.0, "buy_sol": 0.05,
            "chain": "solana", "sol_usd": 100.0, "reason": "trailing stop -30%",
            "realized_sol": 0.0}
    base.update(kw)
    return base


def test_hit_100_sells_half_at_2x():
    # E=1 P=2.5 X=1.2 T=50 -> L=1.25, X<L, no gap -> X2=1.25, X_eff=1+0.625
    xeff, info = moonbag_exit(T(entry=1.0, peak=2.5, exit=1.2), 50)
    assert info["hit_100"] is True
    assert info["trail_fired"] is True and not info["censored"] and not info["gap_fill"]
    assert xeff == pytest.approx(1.625)


def test_exact_2x_counts_as_hit():
    _, info = moonbag_exit(T(entry=1.0, peak=2.0, exit=1.5), 50)
    assert info["hit_100"] is True


def test_no_hit_full_size_on_trail():
    # E=1 P=1.5 X=0.8 T=30 -> L=1.05 -> X2=1.05, X_eff=1.05
    xeff, info = moonbag_exit(T(entry=1.0, peak=1.5, exit=0.8), 30)
    assert info["hit_100"] is False
    assert xeff == pytest.approx(1.05)


def test_censored_marks_to_market():
    # X >= trail level -> still open, carried at X
    xeff, info = moonbag_exit(T(entry=1.0, peak=2.0, exit=1.8), 50)
    assert info["censored"] is True
    assert info["second_half_price"] == pytest.approx(1.8)
    assert xeff == pytest.approx(1.9)  # hit: 1 + 0.5*1.8


def test_gap_fill_uses_observed_exit():
    # X/P = 0.2 < 0.4 -> conservative fill at X, not at L=5
    xeff, info = moonbag_exit(T(entry=1.0, peak=10.0, exit=2.0), 50)
    assert info["gap_fill"] is True
    assert info["second_half_price"] == pytest.approx(2.0)
    assert xeff == pytest.approx(2.0)  # hit: 1 + 0.5*2


def test_gap_boundary_is_strict():
    # X/P exactly 0.4 -> not a gap -> trail fill at L
    xeff, info = moonbag_exit(T(entry=1.0, peak=10.0, exit=4.0), 50)
    assert info["gap_fill"] is False
    assert info["second_half_price"] == pytest.approx(5.0)


def test_dump_guard_keeps_dump_exit():
    dump = T(entry=1.0, peak=3.0, exit=2.6, reason="dump detector -13.0% in 60s")
    _, plain = moonbag_exit(dump, 50)
    assert plain["censored"] is True  # 2.6 >= L=1.5
    _, guarded = moonbag_exit(dump, 50, dump_guard=True)
    assert guarded["dump_guard_exit"] is True
    assert guarded["second_half_price"] == pytest.approx(2.6)


def test_malformed_trades_raise():
    with pytest.raises(ValueError):
        moonbag_exit(T(entry=0.0), 50)
    with pytest.raises(ValueError):
        moonbag_exit(T(entry=1.0, peak=0.9), 50)
    with pytest.raises(ValueError):
        moonbag_exit(T(entry=float("nan"), peak=2.0, exit=1.0), 50)
    with pytest.raises(ValueError):
        moonbag_exit(T(entry=1.0, peak=float("inf"), exit=1.0), 50)
    with pytest.raises(ValueError):
        moonbag_exit(T(entry=1.0, peak=2.0, exit=1.0), 0)
    with pytest.raises(ValueError):
        moonbag_exit(T(entry=1.0, peak=2.0, exit=1.0), 100)
    with pytest.raises(ValueError):
        moonbag_net(T(entry=1.0, peak=2.0, exit=1.0, buy_sol=0.0,
                       realized_sol=0.0), 50)


def test_extra_sell_leg_is_charged():
    # moonbag native must equal replay-convention net minus exactly one leg
    from replay import net_pnl, chain_of
    tr = T(entry=1.0, peak=2.5, exit=1.2, buy_sol=0.05, realized_sol=0.0)
    native, usd, info = moonbag_net(tr, 50)
    key = "realized_sol"
    adj = dict(tr)
    adj[key] = 0.05 * (info["X_eff"] / 1.0 - 1.0)
    adj["exit"] = info["X_eff"]
    base_native, base_usd = net_pnl(adj, None, False)
    assert native == pytest.approx(base_native - EXTRA_LEG_NATIVE["solana"])
    assert usd == pytest.approx(base_usd - EXTRA_LEG_NATIVE["solana"] * 100.0)


def test_stress_triples_only_slippage():
    tr = T(entry=1.0, peak=2.5, exit=1.2, buy_sol=0.05, realized_sol=0.0)
    n_plain, _, info = moonbag_net(tr, 50, stress=False)
    n_stress, _, _ = moonbag_net(tr, 50, stress=True)
    stake = 0.05
    proceeds = stake * info["X_eff"] / 1.0
    # stress adds 2*SLIPPAGE on (stake + proceeds); fixed legs unchanged
    assert (n_plain - n_stress) == pytest.approx(2 * SLIPPAGE * (stake + proceeds))


def test_bsc_usd_uses_overloaded_rate():
    tr = T(entry=1.0, peak=2.0, exit=1.5, buy_sol=0.009, chain="bsc",
           sol_usd=700.0, realized_sol=None, realized_bnb=0.0)
    native, usd, _ = moonbag_net(tr, 50)
    assert usd == pytest.approx((native) * 700.0, rel=1e-9)


def test_metrics_shape_and_counts():
    trades = [T(entry=1.0, peak=2.5, exit=1.2, reason="x"),
              T(entry=1.0, peak=1.2, exit=0.5, reason="x"),
              T(entry=1.0, peak=3.0, exit=2.9, reason="x")]  # censored at T=50
    m = moonbag_metrics(trades, 50)
    for key in ("n", "usd", "win_rate", "pf", "max_dd_usd"):
        assert key in m
    assert m["n"] == 3
    assert m["n_hit_100"] == 2
    assert m["n_censored"] == 1
    assert 0.0 <= m["win_rate"] <= 1.0
    assert math.isfinite(m["usd"])


def test_drop_censored_removes_open_positions():
    trades = [T(entry=1.0, peak=2.5, exit=1.2),
              T(entry=1.0, peak=3.0, exit=2.9)]  # second is censored at T=50
    m = moonbag_metrics(trades, 50, drop_censored=True)
    assert m["n"] == 1
    assert m["n_censored"] == 0


def test_split_is_oos_chronological():
    trades = [{"ts": i} for i in range(6)]
    is_t, oos_t = split_is_oos(trades)
    assert [t["ts"] for t in is_t] == [0, 1, 2, 3]
    assert [t["ts"] for t in oos_t] == [4, 5]


def test_drought_usd_math():
    assert drought_usd([1.0, 2.0, 3.0, -5.0], 1) == pytest.approx(-2.0)
    assert drought_usd([1.0, 2.0, 3.0, -5.0], 2) == pytest.approx(-4.0)
    assert drought_usd([1.0], 2) is None


def test_runner_value_sanity():
    # a 20x runner must be worth far more than a typical $7 stake's loss
    assert runner_value_usd() > 20.0
    # tighter trail from the peak keeps more of the runner
    assert runner_value_usd(trail_pct=30) > runner_value_usd(trail_pct=50)


def test_trails_swept_are_wide():
    assert min(TRAILS) >= 30  # moonbag premise: wide trail, no ceiling


def test_huge_peak_no_overflow():
    _, usd, info = moonbag_net(T(entry=1e-9, peak=1e-3, exit=1e-9), 50)
    assert math.isfinite(usd)
    assert info["hit_100"] is True


def test_zero_exit_still_defined():
    # total wipeout: X tiny but positive
    xeff, info = moonbag_exit(T(entry=1.0, peak=5.0, exit=1e-12), 50)
    assert info["gap_fill"] is True
    assert xeff == pytest.approx(1.0)  # hit: 1 + 0.5*~0
