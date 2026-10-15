"""Hermetic tests for the Y2 hard stop-loss counterfactual engine.

All trades are synthetic dicts; no journal, network, or bot state touched.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis" / "y2_stoploss"))

from stoploss import (
    stop_outcome,
    counterfactual_net,
    final_portion,
    summarize,
    CONFIG,
)
from replay import net_pnl


def mk(entry=100.0, peak=110.0, exit_=80.0, reason="trailing stop -30% from peak",
       rungs=None, buy_sol=1.0, realized_sol=None, sol_usd=100.0, chain="solana"):
    realized = 0.0 if not entry else (exit_ / entry - 1.0) * buy_sol
    t = {"entry": entry, "peak": peak, "exit": exit_, "reason": reason,
         "rungs": rungs or [], "buy_sol": buy_sol, "sol_usd": sol_usd,
         "chain": chain,
         "realized_sol": realized_sol if realized_sol is not None else realized}
    return t


def test_trail_stop_fires_when_stop_above_exit():
    # Slow bleed: exit 80 < stop 85 -> stop fires, fill 0.85 of entry.
    fires, ratio, cat = stop_outcome(mk(entry=100, peak=105, exit_=80), 15)
    assert fires and cat == "trail-stop-fires"
    assert abs(ratio - 0.85) < 1e-9


def test_trail_before_stop_when_exit_above_stop():
    # Trail fired at 90, stop at 85 -> trailing stop won, unchanged.
    fires, ratio, cat = stop_outcome(mk(entry=100, peak=130, exit_=90,
                                        reason="trailing stop -30.8% from peak"), 15)
    assert not fires and cat == "trail-before-stop"
    assert abs(ratio - 0.90) < 1e-9


def test_gap_trade_gaps_through_stop():
    # Rug gap: exit/peak = 0.2 < 0.4 -> stop cannot improve on the gap fill.
    fires, ratio, cat = stop_outcome(mk(entry=100, peak=200, exit_=40,
                                        reason="trailing stop -80% from peak"), 15)
    assert not fires and cat == "trail-gap-unchanged"
    assert abs(ratio - 0.40) < 1e-9


def test_dump_exit_unchanged_by_default():
    fires, ratio, cat = stop_outcome(mk(entry=100, peak=120, exit_=70,
                                        reason="dump detector -25% in 60s"), 15)
    assert not fires and cat == "dump-unchanged"


def test_dump_flip_sensitivity_fires():
    t = mk(entry=100, peak=120, exit_=70,
           reason="dump detector -25% in 60s")
    _, _, info = counterfactual_net(t, 15, dump_flip=True)
    assert info["fires"] and info["category"] == "dump-stop-fires(sensB)"


def test_stale_exit_inside_level_unchanged():
    fires, _, cat = stop_outcome(mk(entry=100, peak=108, exit_=95,
                                    reason="stale exit: -5% after 30m"), 15)
    assert not fires and cat == "stale-unchanged"


def test_stale_exit_beyond_level_fires():
    fires, ratio, cat = stop_outcome(mk(entry=100, peak=108, exit_=80,
                                        reason="stale exit: -20% after 30m"), 15)
    assert fires and cat == "stale-stop-fires"
    assert abs(ratio - 0.85) < 1e-9


def test_discretionary_unchanged():
    for reason in ("take profit +50%, rotating", "manual rotation - freeing slots",
                   "mystery exit"):
        fires, _, cat = stop_outcome(mk(entry=100, peak=200, exit_=150,
                                        reason=reason), 15)
        assert not fires and cat == "discretionary-unchanged"


def test_unscorable():
    fires, ratio, cat = stop_outcome(mk(entry=0), 15)
    assert not fires and ratio is None and cat == "unscorable"
    t = mk(); t["exit"] = None
    fires, ratio, cat = stop_outcome(t, 15)
    assert cat == "unscorable"


def test_final_portion():
    assert final_portion(mk(rungs=[])) == 1.0
    assert abs(final_portion(mk(rungs=[[0, 102.0]])) - 0.5) < 1e-9
    # rung0 sells 50% of balance, rung1 sells 25% of the remaining 50%.
    assert abs(final_portion(mk(rungs=[[0, 100.0], [1, 200.0]])) - 0.375) < 1e-9


def test_delta_math_exact():
    # buy 1 SOL @100, exit 80, stop 15% -> fill 85 on the full position.
    t = mk(entry=100.0, peak=105.0, exit_=80.0, buy_sol=1.0, sol_usd=100.0)
    base_native, _ = net_pnl(t, CONFIG)
    native, usd, info = counterfactual_net(t, 15)
    assert info["fires"]
    from replay import SWAP_FEE, SLIPPAGE
    k = SWAP_FEE["solana"] + SLIPPAGE
    expected_delta = 1.0 * 1.0 * (85.0 - 80.0) / 100.0 * (1 - k)
    assert abs(native - (base_native + expected_delta)) < 1e-9
    assert abs(usd - native * 100.0) < 1e-9


def test_delta_scales_with_rungs():
    # Half sold at TP: only the remaining half benefits from the stop.
    t = mk(entry=100.0, peak=150.0, exit_=80.0, buy_sol=1.0, sol_usd=100.0,
           rungs=[[0, 100.0]], reason="trailing stop -46.7% from peak")
    base_native, _ = net_pnl(t, CONFIG)
    native, _, info = counterfactual_net(t, 15)
    assert info["fires"] and abs(info["final_portion"] - 0.5) < 1e-9
    from replay import SWAP_FEE, SLIPPAGE
    k = SWAP_FEE["solana"] + SLIPPAGE
    expected_delta = 0.5 * 1.0 * (85.0 - 80.0) / 100.0 * (1 - k)
    assert abs(native - (base_native + expected_delta)) < 1e-9


def test_fill_shock_reduces_saving():
    t = mk(entry=100.0, peak=105.0, exit_=80.0, buy_sol=1.0, sol_usd=100.0)
    n0, _, _ = counterfactual_net(t, 15, fill_shock=0.0)
    n2, _, _ = counterfactual_net(t, 15, fill_shock=0.02)
    assert n2 < n0  # 2% adverse fill -> smaller saving, still fires


def test_stress_increases_cost_drag():
    t = mk(entry=100.0, peak=105.0, exit_=80.0, buy_sol=1.0, sol_usd=100.0)
    n_plain, _, _ = counterfactual_net(t, 15, stress=False)
    n_stress, _, _ = counterfactual_net(t, 15, stress=True)
    assert n_stress < n_plain


def test_no_fire_returns_baseline():
    t = mk(entry=100, peak=130, exit_=90,
           reason="trailing stop -30.8% from peak")
    base_native, base_usd = net_pnl(t, CONFIG)
    native, usd, info = counterfactual_net(t, 25)
    assert not info["fires"]
    assert abs(native - base_native) < 1e-12 and abs(usd - base_usd) < 1e-12


def test_summarize_metrics():
    rows = [{"usd": 10.0}, {"usd": -4.0}, {"usd": -6.0}, {"usd": 20.0}]
    m = summarize(rows)
    assert m["n"] == 4 and abs(m["net"] - 20.0) < 1e-9
    assert abs(m["pf"] - 30.0 / 10.0) < 1e-9
    assert abs(m["wr"] - 0.5) < 1e-9
    # cum: 10, 6, 0, 20 -> peak 10, trough 0 -> dd 10
    assert abs(m["dd"] - 10.0) < 1e-9


def test_summarize_empty_and_all_wins():
    m = summarize([])
    assert m["n"] == 0 and m["net"] == 0.0 and m["wr"] == 0.0
    m2 = summarize([{"usd": 5.0}])
    assert math.isinf(m2["pf"]) and m2["dd"] == 0.0


def test_missing_usd_excluded_not_zeroed():
    m = summarize([{"usd": None}, {"usd": 5.0}])
    assert m["n"] == 1 and m["n_usd_missing"] == 1 and abs(m["net"] - 5.0) < 1e-9
