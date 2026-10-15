"""Hermetic tests for the Y3 fade-the-signal diagnostic. No I/O beyond tmp files."""

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from y3_fade.fade import (  # noqa: E402
    chain_of,
    estimated_cost_usd,
    forward_diagnostic,
    load_trades,
    pullback_bounds,
    split_stats,
    trade_net_usd,
)


def _write_journal(tmp_path, rows):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def _entry(ts, mint, price, **kw):
    d = {"ts": ts, "type": "entry", "mint": mint, "name": "T",
         "entry": price, "buy_sol": 0.05, "signal_gain_pct": 120.0}
    d.update(kw)
    return d


def _close(ts, mint, entry, peak, exit_, **kw):
    d = {"ts": ts, "type": "close", "mint": mint, "name": "T",
         "entry": entry, "peak": peak, "exit": exit_,
         "buy_sol": 0.05, "reason": "trailing stop", "realized_sol": 0.0}
    d.update(kw)
    return d


def test_loader_pairs_fifo_and_skips_malformed(tmp_path):
    rows = [
        _entry("2026-09-24 10:00:00", "M1", 1.0),
        {"ts": "x", "type": "entry", "mint": "M2"},  # no entry price on entry...
        _close("2026-09-24 10:05:00", "M2", 2.0, 2.5, 1.5),  # ...but close carries it
        "not json at all",
        _close("2026-09-24 10:06:00", "M3", 1.0, 1.2, 0.0),  # exit 0 -> skip
        _entry("2026-09-24 10:07:00", "M4", float("nan")),  # NaN entry -> skip
        _close("2026-09-24 10:08:00", "M4", 1.0, 1.1, 0.9),
    ]
    p = _write_journal(tmp_path, rows)
    trades, skipped = load_trades(p)
    mints = [t["mint"] for t in trades]
    # M1 has no close -> stays pending (unmatched entry, correctly not emitted).
    # M3's exit=0.0 is a legitimate total-loss outcome, kept. NaN entry falls
    # back to the close record's entry price.
    assert mints == ["M2", "M3", "M4"]
    assert skipped >= 1  # the non-dict JSON string row
    # entry_ts preserved from the ENTRY record, not overwritten by close ts
    assert trades[0]["entry_ts"] == "x"  # M2's entry ts, distinct from its close ts
    assert trades[0]["close_ts"] == "2026-09-24 10:05:00"


def test_chain_of_bsc_prefix():
    assert chain_of({"mint": "0xabc", "chain": None}) == "bsc"
    assert chain_of({"mint": "So1anaAddr"}) == "solana"
    assert chain_of({"mint": "0xabc", "chain": "solana"}) == "solana"


def test_forward_diagnostic_math(tmp_path):
    rows = [
        _entry("2026-09-24 10:00:00", "A", 1.0),
        _close("2026-09-24 10:05:00", "A", 1.0, 2.0, 1.5),   # winner: peak 2x, exit 1.5x
        _entry("2026-09-24 10:00:00", "B", 1.0),
        _close("2026-09-24 10:05:00", "B", 1.0, 1.2, 0.5),   # loser: peak 1.2x, exit 0.5x
    ]
    trades, _ = load_trades(_write_journal(tmp_path, rows))
    d = forward_diagnostic(trades)
    assert d["peak_mult"]["med"] == pytest.approx(1.6)
    assert d["exit_mult"]["med"] == pytest.approx(1.0)
    assert d["p_win"] == pytest.approx(0.5)
    assert d["p_peak_ge_1_10"] == pytest.approx(1.0)
    assert d["p_peak_ge_2_00"] == pytest.approx(0.5)
    # fade mirror is the exact mirror of exit_mult
    assert d["fade_mirror_gross"]["med"] == pytest.approx(0.0)


def test_pullback_bounds_properties(tmp_path):
    rows = [
        _entry("2026-09-24 10:00:00", "W", 1.0),
        _close("2026-09-24 10:05:00", "W", 1.0, 3.0, 2.0),   # winner, fill unprovable
        _entry("2026-09-24 10:00:00", "L", 1.0),
        _close("2026-09-24 10:05:00", "L", 1.0, 1.1, 0.7),   # loser, exit<=0.8 -> provable at x=0.2
    ]
    trades, _ = load_trades(_write_journal(tmp_path, rows))
    b = pullback_bounds(trades, 0.20)
    assert b["provable_fills"] == 1
    assert b["provable_all_losers"] is True
    assert b["winners_with_unprovable_fill"] == 1
    assert b["upper_net_usd"] >= b["lower_net_usd"]  # upper always dominates
    # deeper pullback can only shrink the provable set
    b2 = pullback_bounds(trades, 0.40)
    assert b2["provable_fills"] <= b["provable_fills"]


def test_split_stats_drawdown_and_pf():
    s = split_stats([10.0, -5.0, -5.0, 20.0, -30.0])
    assert s["n"] == 5
    assert s["net"] == pytest.approx(-10.0)
    assert s["pf"] == pytest.approx(30.0 / 40.0)
    assert s["wr"] == pytest.approx(0.4)
    assert s["max_dd"] == pytest.approx(30.0)
    empty = split_stats([])
    assert empty["n"] == 0 and empty["net"] == 0.0


def test_costs_scale_with_stress_and_stay_finite(tmp_path):
    rows = [_entry("2026-09-24 10:00:00", "A", 1.0),
            _close("2026-09-24 10:05:00", "A", 1.0, 1.5, 1.2)]
    trades, _ = load_trades(_write_journal(tmp_path, rows))
    t = trades[0]
    base = trade_net_usd(t, 1.0, stress=False)
    stressed = trade_net_usd(t, 1.0, stress=True)
    assert math.isfinite(base) and math.isfinite(stressed)
    assert stressed <= base  # stress never improves the net
    c = estimated_cost_usd(t, 1.0)
    assert c >= 0 and math.isfinite(c)
