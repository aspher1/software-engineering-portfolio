"""Hermetic checks for the offline paper exit counterfactual."""
from dataclasses import replace

import pytest

from analysis import exit_sweep as sweep


BASE = sweep.Params()


def trade(reason="trailing stop -25.0% from peak", entry=100, peak=150, exit=112.5):
    return {"entry": entry, "peak": peak, "exit": exit, "reason": reason,
            "entry_ts": "2026-01-01 00:00:00", "close_ts": "2026-01-01 00:30:00",
            "buy_sol": .1, "realized_sol": .01, "chain": "solana", "sol_usd": 100}


def test_tp_threshold_and_partial_remainder():
    t = trade()
    p = replace(BASE, take_profits=((40, 50), (60, 50)), trailing_stop_pct=25,
                trail_after_tp_pct=25)
    assert sweep.modeled_price(t, p) == pytest.approx((.5 * 140 + .5 * 112.5) / 100)
    assert sweep.modeled_price(t, replace(p, take_profits=((60, 100),))) == pytest.approx(1.125)
    assert sweep.modeled_price(t, replace(p, take_profits=((40, 100),))) == pytest.approx(1.4)


def test_trail_tighter_wider_and_after_tp():
    t = trade()
    assert sweep.modeled_price(t, replace(BASE, take_profits=(), trailing_stop_pct=10)) == pytest.approx(1.35)
    assert sweep.modeled_price(t, replace(BASE, take_profits=(), trailing_stop_pct=40)) == pytest.approx(1.125)
    assert sweep.modeled_price(t, replace(BASE, take_profits=((40, 50),),
                                                trailing_stop_pct=30, trail_after_tp_pct=8)) == pytest.approx((70 + .5 * 138) / 100)


def test_hard_stop_and_rug_gap_floor():
    t = trade("hard stop -50% from entry", peak=105, exit=50)
    assert sweep.modeled_price(t, replace(BASE, take_profits=(), hard_stop_pct=20)) == pytest.approx(.8)
    rug = trade("dump detector -80.0% in 60s, selling all", peak=200, exit=20)
    p = replace(BASE, take_profits=(), hard_stop_pct=20, trailing_stop_pct=10, dump_drop_pct=8)
    assert sweep.modeled_price(rug, p) == pytest.approx(.2)


def test_stale_timing_and_gain_eligibility_without_invented_fill():
    t = trade("stale exit: +5.0% after 30m", peak=110, exit=105)
    early = replace(BASE, take_profits=(), stale_exit_min=15, stale_exit_max_gain_pct=10)
    assert sweep.stale_eligible_at_close(t, early)
    assert not sweep.stale_eligible_at_close(t, replace(early, stale_exit_min=45))
    assert not sweep.stale_eligible_at_close(t, replace(early, stale_exit_max_gain_pct=5))
    assert not sweep.stale_eligible_at_close(t, replace(early, stale_exit_min=0))
    assert sweep.modeled_price(t, early) == sweep.modeled_price(t, replace(early, stale_exit_min=45))


def test_dump_recorded_window_only():
    t = trade("dump detector -18.0% in 60s, selling all", peak=100, exit=82)
    p = replace(BASE, take_profits=(), hard_stop_pct=50, dump_drop_pct=8)
    assert sweep.modeled_price(t, p) == pytest.approx(.92)
    assert sweep.modeled_price(t, replace(p, dump_drop_pct=20)) == pytest.approx(.82)
    assert sweep.modeled_price(t, replace(p, dump_window_sec=30)) == pytest.approx(.82)
    assert sweep.modeled_price(t, replace(p, dump_drop_pct=0)) == pytest.approx(.82)
    older_peak = trade("dump detector -18.0% in 60s, selling all", peak=200, exit=82)
    assert sweep.modeled_price(older_peak, p) == pytest.approx(.92)


def test_recorded_baseline_anchor_and_costs():
    t = trade()
    cfg = {"exit": {"max_priority_fee_lamports": 2_000_000}}
    row = sweep.candidate_trade(t, BASE, BASE, cfg)
    assert row["realized_sol"] == t["realized_sol"]
    assert row["exit"] == t["exit"]
    tighter = sweep.candidate_trade(t, replace(BASE, take_profits=(), trailing_stop_pct=10),
                                    replace(BASE, take_profits=()), cfg)
    assert tighter["realized_sol"] > t["realized_sol"]
    assert sweep.scored([t], BASE, BASE, cfg, True)["usd"] < sweep.scored([t], BASE, BASE, cfg)["usd"]


def metrics(usd, n=10, win=.5):
    return {"usd": usd, "n": n, "win_rate": win}


def test_validation_retention_and_flags():
    groups = {"chain": {"a": 2, "b": 2}, "liquidity": {"lo": 1, "hi": 1},
              "reason": {"trail": 1, "dump": 1}}
    args = (metrics(0), metrics(10), metrics(0), metrics(6), metrics(1), groups, True)
    assert sweep.validation(*args)["ships"]
    assert "edge retention below 60%" in sweep.validation(*args[:3], metrics(5), *args[4:])["flags"]
    assert "win rate above 90%" in sweep.validation(*args[:3], metrics(6, win=.91), *args[4:])["flags"]
    concentrated = dict(groups, chain={"a": 9, "b": 1})
    assert "edge concentrated in chain" in sweep.validation(*args[:5], concentrated, True)["flags"]
    assert "OOS 3x slippage net nonpositive" in sweep.validation(*args[:4], metrics(-1), *args[5:])["flags"]
    assert "no broad IS plateau" in sweep.validation(*args[:-1], False)["flags"]


def test_plateau_prefers_broad_neighbors_over_sharp_peak():
    base = replace(BASE, take_profits=(), trailing_stop_pct=25)
    p = replace(base, trailing_stop_pct=15, hard_stop_pct=25)
    dims = {"trailing_stop_pct": (10, 15, 20, 25),
            "hard_stop_pct": (20, 25, 30, 35)}
    assert sweep.plateau_support(p, 10, base, dims, lambda _: 6) == list(dims)
    def cliff(q):
        return 1 if q.trailing_stop_pct != p.trailing_stop_pct else 6
    assert sweep.plateau_support(p, 10, base, dims, cliff) == ["hard_stop_pct"]


def test_grid_covers_requested_axes():
    d = sweep.dimensions()
    assert ((30, 100),) in d["take_profits"]
    assert ((30, 50), (40, 50)) in d["take_profits"]
    assert d["trailing_stop_pct"] == (10, 15, 20, 25, 30, 40)
    assert d["hard_stop_pct"] == (20, 25, 30, 35, 45, 50)
