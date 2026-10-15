"""Hermetic tests for X1 rug-gap forensics. No I/O, no network, no bot imports."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis" / "x1_ruggap"))

from ruggap import (
    anatomy, chain_of, cooldown_counterfactual, gain_veto_split,
    is_dump_exit, is_rug_gap, trigger_ratio, usd_per_entry_unit,
)


def gap_trade(**kw):
    t = {"type": "close", "mint": "M", "chain": "solana", "entry": 1e-6,
         "peak": 2e-6, "exit": 0.2e-6, "buy_sol": 0.087,
         "reason": "dump detector -90.0% in 60s, selling all", "sol_usd": 200.0}
    t.update(kw)
    return t


def test_gap_boundary():
    assert is_rug_gap(gap_trade(exit=0.2e-6, peak=2e-6)) is True       # 0.10 < 0.4
    assert is_rug_gap(gap_trade(exit=0.79e-6, peak=2e-6)) is True      # 0.395 < 0.4
    assert is_rug_gap(gap_trade(exit=0.81e-6, peak=2e-6)) is False     # 0.405 >= 0.4


def test_gap_malformed_never_raises():
    for bad in [None, {}, {"peak": None, "exit": 1}, {"peak": 0, "exit": 1},
                {"peak": "x", "exit": "y"}, {"peak": float("nan"), "exit": 1},
                {"peak": float("inf"), "exit": 1}, {"peak": -5, "exit": 1}]:
        assert is_rug_gap(bad) is False
        assert anatomy(bad) == (0.0, 0.0)


def test_trigger_ratios():
    assert trigger_ratio(gap_trade(reason="dump detector -50% in 60s")) == 0.88
    assert trigger_ratio(gap_trade(reason="venue dump: DexScreener m5 -94%, selling")) == 0.70
    assert trigger_ratio(gap_trade(reason="trailing stop -30.1% from peak")) == 0.70
    assert trigger_ratio({}) == 0.70
    assert trigger_ratio(None) == 0.70


def test_anatomy_splits_lag_and_gap():
    # peak 2, trigger 0.88*2=1.76, exit 0.2 ; stake .087 @ sol_usd 200, entry 1e-6
    lag, gap = anatomy(gap_trade())
    scale = 0.087 * 200.0 / 1e-6
    assert abs(lag - (2e-6 - 1.76e-6) * scale) < 1e-6
    assert abs(gap - (1.76e-6 - 0.2e-6) * scale) < 1e-6
    assert gap > lag  # the forensic headline: gap dominates


def test_anatomy_non_gap_zero():
    assert anatomy(gap_trade(exit=1.5e-6, peak=2e-6)) == (0.0, 0.0)


def test_anatomy_missing_rate_falls_back():
    t = gap_trade(sol_usd=None)
    lag, gap = anatomy(t)
    assert lag > 0 and gap > 0  # solana falls back to 115


def test_anatomy_bsc_without_rate_unscorable():
    t = gap_trade(chain="bsc", mint="0xabc", buy_sol=None, buy_bnb=0.009, sol_usd=None)
    assert anatomy(t) == (0.0, 0.0)


def test_chain_of_heuristics():
    assert chain_of({"mint": "0x123"}) == "bsc"
    assert chain_of({"mint": "So1abc"}) == "solana"
    assert chain_of({"chain": "bsc"}) == "bsc"
    assert chain_of(None) == "solana"
    assert chain_of("nope") == "solana"


def test_cooldown_vetoes_post_dump_reentry():
    close1 = {"type": "close", "mint": "M1", "close_ts": "2026-09-27 14:05:13",
              "reason": "dump detector -22.8% in 60s, selling all"}
    entry2 = {"type": "entry", "mint": "M1", "ts": "2026-09-27 14:18:22"}
    entry3 = {"type": "entry", "mint": "M2", "ts": "2026-09-27 14:19:00"}
    vetoed, kept = cooldown_counterfactual([close1, entry2, entry3])
    assert entry2 in vetoed and entry3 in kept


def test_cooldown_ignores_non_dump_closes():
    close1 = {"type": "close", "mint": "M1", "close_ts": "2026-09-27 14:05:13",
              "reason": "trailing stop -30.1% from peak"}
    entry2 = {"type": "entry", "mint": "M1", "ts": "2026-09-27 14:18:22"}
    vetoed, kept = cooldown_counterfactual([close1, entry2])
    assert vetoed == [] and entry2 in kept


def test_cooldown_malformed_safe():
    vetoed, kept = cooldown_counterfactual(None)
    assert vetoed == [] and kept == []
    vetoed, kept = cooldown_counterfactual([None, "x", 42])
    assert vetoed == [] and kept == []


def test_gain_veto_split():
    ts = [{"signal_gain_pct": 100}, {"signal_gain_pct": 350}, {"signal_gain_pct": None}, {}]
    kept, vetoed = gain_veto_split(ts, 300.0)
    assert len(kept) == 3 and len(vetoed) == 1
    assert vetoed[0]["signal_gain_pct"] == 350


def test_gain_veto_nan_safe():
    kept, vetoed = gain_veto_split([{"signal_gain_pct": float("nan")}])
    assert len(kept) == 1 and vetoed == []


def test_is_dump_exit():
    assert is_dump_exit({"reason": "dump detector -12.8% in 60s, selling all"})
    assert is_dump_exit({"reason": "venue dump: DexScreener m5 -38.9%, selling all"})
    assert not is_dump_exit({"reason": "trailing stop -30.1% from peak"})
    assert not is_dump_exit({})
    assert not is_dump_exit(None)


def test_latency_benchmark():
    trades = [gap_trade() for _ in range(20000)]
    for fn in (is_rug_gap, anatomy):
        start = time.perf_counter()
        for t in trades:
            fn(t)
        dt = (time.perf_counter() - start) / len(trades) * 1000
        assert dt < 1.0, (fn.__name__, dt)  # offline forensics; hot path N/A
    # p99-style: worst single call under 5ms
    start = time.perf_counter()
    anatomy(gap_trade())
    assert (time.perf_counter() - start) * 1000 < 5.0
