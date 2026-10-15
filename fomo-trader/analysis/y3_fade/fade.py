"""Y3 fade-the-signal diagnostic.

Two questions:
  1. DIAGNOSTIC: is our 100%+/15m breakout entry signal backwards? I.e., do
     tokens systematically mean-revert right after we buy? Measured from
     journal-derivable forward proxies: peak/entry, exit/entry, give-back,
     and the (untradeable) short-mirror return.
  2. TRADEABLE VARIANT: pullback entry -- enter at entry*(1-X) only if the
     price touches that level after the signal. The journal has no intraday
     price path, so fills are PROVABLE only when exit <= entry*(1-X) (the
     price demonstrably got there). Every provable fill is therefore a loser
     by construction; winners' fills are unprovable. We report the provable
     lower bound and the impossible best-case upper bound (every trade fills).

Pure stdlib, offline, never raises on malformed rows (fail-open: skips row,
counts it in `skipped`).
"""

import json
import math
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JOURNAL = ROOT / "runs/paper-1h/trades.jsonl"
SOL_FALLBACK_USD = 115.0
SWAP_FEE = {"solana": 0.0025, "bsc": 0.0025}
SLIPPAGE = 0.0015
STRESS_MULT = 3


def chain_of(row):
    return row.get("chain") or ("bsc" if str(row.get("mint", "")).startswith("0x") else "solana")


def _fnum(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def load_trades(path=JOURNAL):
    """Chronological (journal-append-order) list of dicts with entry/peak/exit."""
    from collections import defaultdict, deque
    pending = defaultdict(deque)
    trades = []
    skipped = 0
    for line in Path(path).open(encoding="utf-8"):
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            skipped += 1
            continue
        if not isinstance(row, dict):
            skipped += 1
            continue
        kind = row.get("type")
        if kind == "entry":
            pending[row.get("mint")].append(row)
        elif kind == "close":
            q = pending[row.get("mint")]
            entry = q.popleft() if q else None
            e = _fnum((entry or {}).get("entry"))
            if e is None:
                e = _fnum(row.get("entry"))  # unmatched close: close record carries entry price
            p = _fnum(row.get("peak"))
            x = _fnum(row.get("exit"))
            if e is None or e <= 0 or p is None or x is None:
                skipped += 1
                continue
            merged = dict(entry or {})
            merged.update(row)
            merged["entry_ts"] = (entry or {}).get("ts")
            merged["close_ts"] = row.get("ts")
            merged["entry_f"] = e
            merged["peak_f"] = p
            merged["exit_f"] = x
            merged["chain"] = chain_of(merged)
            trades.append(merged)
    return trades, skipped


def estimated_cost_usd(trade, entry_price, stress=False):
    """Round-trip cost in USD for a hypothetical entry at entry_price."""
    ch = chain_of(trade)
    stake = _fnum(trade.get("buy_bnb") or trade.get("buy_sol")) or 0.0
    x = trade["exit_f"]
    price_ratio = max(0.0, x / entry_price) if entry_price > 0 else 0.0
    gross_out = stake * price_ratio
    variable = (stake + gross_out) * (SWAP_FEE[ch] + SLIPPAGE * (STRESS_MULT if stress else 1))
    native = variable  # fixed legs are dust at these stakes; variable dominates
    rate = _fnum(trade.get("sol_usd")) or (SOL_FALLBACK_USD if ch == "solana" else 0.0)
    return native * rate


def trade_net_usd(trade, entry_price, stress=False):
    ch = chain_of(trade)
    stake = _fnum(trade.get("buy_bnb") or trade.get("buy_sol")) or 0.0
    x = trade["exit_f"]
    rate = _fnum(trade.get("sol_usd")) or (SOL_FALLBACK_USD if ch == "solana" else 0.0)
    gross = (x / entry_price - 1.0) * stake * rate
    return gross - estimated_cost_usd(trade, entry_price, stress)


def forward_diagnostic(trades):
    """Journal-derivable post-entry drift proxies."""
    peaks = [t["peak_f"] / t["entry_f"] for t in trades]
    exits = [t["exit_f"] / t["entry_f"] for t in trades]
    giveback = [(t["peak_f"] - t["exit_f"]) / t["peak_f"] for t in trades if t["peak_f"] > 0]
    fade_mirror = [1.0 - v for v in exits]  # gross short-at-entry return; NOT tradeable
    wins = [t for t in trades if t["exit_f"] > t["entry_f"]]

    def dist(v):
        s = sorted(v)
        n = len(s)
        return {"n": n, "min": s[0], "p25": s[n // 4], "med": statistics.median(s),
                "mean": statistics.mean(s), "p75": s[3 * n // 4], "max": s[-1]} if n else {}

    return {
        "peak_mult": dist(peaks),
        "exit_mult": dist(exits),
        "giveback": dist(giveback),
        "fade_mirror_gross": dist(fade_mirror),
        "p_win": len(wins) / len(trades) if trades else 0,
        "p_peak_ge_1_10": sum(1 for v in peaks if v >= 1.10) / len(peaks) if peaks else 0,
        "p_peak_ge_2_00": sum(1 for v in peaks if v >= 2.00) / len(peaks) if peaks else 0,
        "winners_med_peak": statistics.median([t["peak_f"] / t["entry_f"] for t in wins]) if wins else None,
    }


def pullback_bounds(trades, x, stress=False):
    """Lower bound: only provably-filled trades (exit <= entry*(1-x)), all losers.
    Upper bound: impossible best case -- every trade fills at the retrace."""
    e_key = lambda t: t["entry_f"] * (1.0 - x)
    provable = [t for t in trades if t["exit_f"] <= e_key(t)]
    lower = [trade_net_usd(t, e_key(t), stress) for t in provable]
    upper = [trade_net_usd(t, e_key(t), stress) for t in trades]
    winners_unprovable = sum(1 for t in trades
                             if t["exit_f"] > t["entry_f"] and t["exit_f"] > e_key(t))
    return {
        "x": x, "stress": stress,
        "provable_fills": len(provable),
        "provable_all_losers": all(t["exit_f"] <= t["entry_f"] for t in provable),
        "winners_with_unprovable_fill": winners_unprovable,
        "lower_net_usd": sum(lower),
        "upper_net_usd": sum(upper),
    }


def split_stats(usds):
    n = len(usds)
    wins = [v for v in usds if v > 0]
    losses = [-v for v in usds if v < 0]
    pf = sum(wins) / sum(losses) if losses else float("inf")
    dd = 0.0
    peak = 0.0
    run = 0.0
    for v in usds:
        run += v
        peak = max(peak, run)
        dd = max(dd, peak - run)
    return {"n": n, "net": sum(usds), "pf": pf,
            "wr": len(wins) / n if n else 0.0, "max_dd": dd}


def evaluate(trades, xs=(0.10, 0.15, 0.20, 0.30), is_n=49):
    """Chronological IS/OOS evaluation of the provable-fill pullback."""
    out = {"diagnostic": forward_diagnostic(trades), "variants": []}
    for split, sub in (("IS", trades[:is_n]), ("OOS", trades[is_n:])):
        base = split_stats([trade_net_usd(t, t["entry_f"]) for t in sub])
        row = {"split": split, "baseline": {k: round(v, 4) for k, v in base.items()
                                            if not isinstance(v, float) or math.isfinite(v)}}
        for x in xs:
            for stress in (False, True):
                b = pullback_bounds(sub, x, stress)
                fills = [t for t in sub if t["exit_f"] <= t["entry_f"] * (1.0 - x)]
                s = split_stats([trade_net_usd(t, t["entry_f"] * (1.0 - x), stress) for t in fills])
                row[f"x{int(x * 100)}{'_3x' if stress else ''}"] = {
                    "fills": len(fills),
                    "net": round(s["net"], 2), "pf": round(s["pf"], 4),
                    "wr": round(s["wr"], 4), "max_dd": round(s["max_dd"], 2),
                    "impossible_upper_net": round(b["upper_net_usd"], 2),
                }
        out["variants"].append(row)
    return out
