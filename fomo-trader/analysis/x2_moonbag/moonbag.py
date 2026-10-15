"""X2: moonbag exit-policy paper test (decision support, NOT a ship candidate).

Replays every closed journal trade under a moonbag policy:
  - sell 50% at +100% (fires iff recorded lifetime peak >= 2x entry)
  - remaining 50% rides with a wide trailing stop T% from the recorded peak,
    no take-profit ceiling, no dump detector, no stale exit.

Only (entry, peak, exit) are observed per trade. Every fill between those
points is an assumption -- each one is listed in ASSUMPTIONS below and the
sensitive ones get a dedicated sensitivity column. Nothing here is a fill
forecast.

Cost convention mirrors analysis/replay.py: the baseline is journal
realized minus replay.estimated_cost (same stress layer both policies get),
so the DELTA between policies is apples-to-apples. Moonbag performs two
sells, so one extra fixed leg is charged vs the 2-leg model.
"""

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from replay import (load_journal, metrics, net_pnl, chain_of, SWAP_FEE, SLIPPAGE,
                    BNB_GAS_NATIVE_PER_LEG, SOL_LAMPORTS, CONFIG,
                    SOL_FALLBACK_USD)

TRAILS = (30, 40, 50)
GAP_RATIO = 0.4  # exit/peak below this => gap fill; fill price unobservable

ASSUMPTIONS = [
    "A1: +100% sale fills at exactly 2x entry iff recorded peak >= 2x entry. "
        "Gap-throughs (favorable) and exact wick touches both approximated at 2x.",
    "A2: ordering -- the 2x touch happens before the ultimate peak (peak is the "
        "lifetime max, so the touch precedes or equals it). Intratrade path order "
        "is otherwise unknown.",
    "A3: trailing stop fires at peak*(1-T) iff recorded final exit < that level "
        "and exit/peak >= 0.4 (no gap). Assumes the fall from peak to exit passed "
        "through the trail level.",
    "A4: gap fills (exit/peak < 0.4, the 'rug gap' definition) fill at the "
        "OBSERVED exit (conservative). The true gap fill is unobservable.",
    "A5: if recorded exit >= trail level, the trail never fired -> position still "
        "open at close. Primary: mark-to-market at recorded exit (censored). "
        "Sensitivity: drop censored trades entirely.",
    "A6: pure moonbag per the user's description -- no dump detector, no stale "
        "exit, no hard stop. Dump protection loss is reflected via A4. Sensitivity "
        "column keeps the dump guard (dump-reason trades exit riding half at "
        "recorded exit).",
    "A7: costs use replay.py's variable model on (stake + proceeds) with the same "
        "stress tripling; moonbag is charged one extra fixed leg (two sells). "
        "Paper-quote costs inside journal realized are NOT double-subtracted: the "
        "baseline is journal realized minus estimated_cost, the moonbag leg is "
        "gross proceeds minus estimated_cost minus the extra leg.",
    "A8: USD conversion reuses the journal's own rate convention (sol_usd field; "
        "for BSC that field carries the BNB/USD price).",
]

_config = json.loads(CONFIG.read_text())
_PRIORITY_FEE_SOL = float(_config.get("exit", {}).get("max_priority_fee_lamports", 2_000_000)) / SOL_LAMPORTS
EXTRA_LEG_NATIVE = {"solana": _PRIORITY_FEE_SOL, "bsc": BNB_GAS_NATIVE_PER_LEG}


def _stake(trade):
    return float(trade.get("buy_sol") or trade.get("buy_bnb") or 0.0)


def moonbag_exit(trade, trail_pct, dump_guard=False):
    """Proceeds-weighted effective exit price under the moonbag policy.

    Returns (X_eff, info). Raises ValueError on malformed (non-positive entry
    or stake, peak below entry).
    """
    E = float(trade["entry"])
    P = float(trade["peak"])
    X = float(trade["exit"])
    if E <= 0:
        raise ValueError("entry must be positive")
    if not all(math.isfinite(v) for v in (E, P, X)):
        raise ValueError("entry/peak/exit must be finite")
    if P < E:
        raise ValueError("peak below entry: malformed trade")
    if not (0 < trail_pct < 100):
        raise ValueError("trail_pct must be between 0 and 100")

    hit = P >= 2 * E
    reason = str(trade.get("reason") or "").lower()
    info = {"hit_100": hit, "trail_pct": trail_pct, "censored": False,
            "gap_fill": False, "trail_fired": False, "dump_guard_exit": False}

    if dump_guard and "dump" in reason:
        # sensitivity: keep the actual dump exit for the riding half
        X2 = X
        info["dump_guard_exit"] = True
    else:
        L = P * (1 - trail_pct / 100)
        if X >= L:
            X2 = X  # trail never fired; still open -> mark to market (A5)
            info["censored"] = True
        elif X / P < GAP_RATIO:
            X2 = X  # gap-through; fill unobservable -> conservative (A4)
            info["gap_fill"] = True
            info["trail_fired"] = True
        else:
            X2 = L
            info["trail_fired"] = True

    info["second_half_price"] = X2
    X_eff = (E + 0.5 * X2) if hit else X2  # 0.5*2E + 0.5*X2
    info["X_eff"] = X_eff
    return X_eff, info


def moonbag_net(trade, trail_pct, stress=False, dump_guard=False):
    """(net_native, net_usd, info) under moonbag. Cost convention per A7/A8."""
    chain = chain_of(trade)
    stake = _stake(trade)
    if stake <= 0:
        raise ValueError("stake must be positive")
    E = float(trade["entry"])
    if E <= 0:
        raise ValueError("entry must be positive")
    X_eff, info = moonbag_exit(trade, trail_pct, dump_guard)

    key = "realized_bnb" if chain == "bsc" else "realized_sol"
    adjusted = dict(trade)
    adjusted[key] = stake * (X_eff / E - 1.0)  # gross proceeds, pre-cost
    adjusted["exit"] = X_eff
    native, usd = net_pnl(adjusted, None, stress)  # subtracts 2-leg estimated_cost
    native -= EXTRA_LEG_NATIVE[chain]  # second sell leg
    rate = adjusted.get("sol_usd")
    if rate is None and chain == "solana":
        rate = SOL_FALLBACK_USD
    if usd is not None and rate is not None:
        usd -= EXTRA_LEG_NATIVE[chain] * float(rate)
    return native, usd, info


def moonbag_metrics(trades, trail_pct, stress=False, dump_guard=False,
                    drop_censored=False):
    """Same dict shape as replay.metrics, plus per-trade rows and counts."""
    rows = []
    for t in trades:
        native, usd, info = moonbag_net(t, trail_pct, stress, dump_guard)
        rows.append((native, usd, chain_of(t), info, t))
    if drop_censored:
        rows = [r for r in rows if not r[3]["censored"]]
    vals = [r[0] for r in rows]
    usd_vals = [r[1] for r in rows if r[1] is not None]
    wins = sum(v > 0 for v in vals)
    gains = sum(v for v in usd_vals if v > 0)
    losses = -sum(v for v in usd_vals if v < 0)
    cumulative = high = drawdown = 0.0
    for v in (r[1] or 0.0 for r in rows):
        cumulative += v
        high = max(high, cumulative)
        drawdown = max(drawdown, high - cumulative)
    return {
        "n": len(rows),
        "usd": sum(usd_vals),
        "usd_n": len(usd_vals),
        "win_rate": wins / len(rows) if rows else 0.0,
        "pf": gains / losses if losses else (math.inf if gains else 0.0),
        "max_dd_usd": drawdown,
        "n_hit_100": sum(r[3]["hit_100"] for r in rows),
        "n_censored": sum(r[3]["censored"] for r in rows),
        "n_gap_fill": sum(r[3]["gap_fill"] for r in rows),
        "n_trail_fired": sum(r[3]["trail_fired"] for r in rows),
        "rows": rows,
    }


def runner_value_usd(stake_usd=7.0, peak_mult=20.0, trail_pct=50):
    """Net USD of one hypothetical runner: +100% sale + trail from peak."""
    # proceeds multiple of stake: 0.5*2 + 0.5*peak_mult*(1-trail)
    mult = 1.0 + 0.5 * peak_mult * (1 - trail_pct / 100)
    gross = stake_usd * (mult - 1.0)
    # rough costs: variable ~0.4%/leg-ish + fixed legs; use replay constants
    variable = stake_usd * mult * (0.0025 + 0.0015) * 2
    return gross - variable - 0.50  # ~fixed legs in USD


def split_is_oos(trades):
    """Chronological split: first 2/3 IS, rest OOS (journal append order)."""
    cut = len(trades) * 2 // 3
    return trades[:cut], trades[cut:]


def drought_usd(nets, k):
    """Total USD after removing the top-k nets. Nets: iterable of floats."""
    ranked = sorted(nets)
    if len(ranked) < k:
        return None
    return sum(ranked) - sum(ranked[-k:])


def main():
    trades, counts = load_journal()
    is_trades, oos_trades = split_is_oos(trades)
    parts = {"IS": is_trades, "OOS": oos_trades, "ALL": trades}
    print(f"Journal: {len(trades)} closes ({counts.get('close', '?')}), "
          f"IS={len(parts['IS'])} OOS={len(parts['OOS'])}")
    print("peak>=2x entry:", sum(float(t["peak"]) >= 2 * float(t["entry"]) for t in trades))

    out = {"assumptions": ASSUMPTIONS, "parts": {}}
    for pname, part in parts.items():
        base = metrics(part)
        base_s = metrics(part, None, True)
        entry = {"baseline": base, "baseline_stress": base_s, "moonbag": {}}
        for T in TRAILS:
            m = moonbag_metrics(part, T)
            m_s = moonbag_metrics(part, T, stress=True)
            m_dg = moonbag_metrics(part, T, dump_guard=True)
            m_nc = moonbag_metrics(part, T, drop_censored=True)
            # runner drought on moonbag nets
            nets = [(r[1] or 0.0) for r in m["rows"] if r[1] is not None]
            drought = {f"drop_top_{k}": drought_usd(nets, k) for k in (1, 2, 3)}
            entry["moonbag"][T] = {
                "plain": {k: v for k, v in m.items() if k != "rows"},
                "stress3x": {k: v for k, v in m_s.items() if k != "rows"},
                "dump_guard": {k: v for k, v in m_dg.items() if k != "rows"},
                "drop_censored": {k: v for k, v in m_nc.items() if k != "rows"},
                "runner_drought_usd": drought,
            }
        out["parts"][pname] = entry

    # headline table
    for pname in ("IS", "OOS", "ALL"):
        e = out["parts"][pname]
        b = e["baseline"]
        print(f"\n=== {pname} (n={b['n']}) ===")
        print(f"  baseline ladder : usd {b['usd']:+.2f}  PF {b['pf']:.3f}  "
              f"WR {b['win_rate']:.1%}  DD ${b['max_dd_usd']:.2f}  "
              f"(3x stress {e['baseline_stress']['usd']:+.2f})")
        for T in TRAILS:
            mb = e["moonbag"][T]["plain"]
            st = e["moonbag"][T]["stress3x"]["usd"]
            dr = e["moonbag"][T]["runner_drought_usd"]
            print(f"  moonbag T={T:>2}%   : usd {mb['usd']:+.2f}  PF {mb['pf']:.3f}  "
                  f"WR {mb['win_rate']:.1%}  DD ${mb['max_dd_usd']:.2f}  "
                  f"(3x {st:+.2f}) hit100={mb['n_hit_100']} cens={mb['n_censored']} "
                  f"gap={mb['n_gap_fill']} | drought: "
                  + " ".join(f"-top{k}={v:+.2f}" for k, v in
                              ((kk, vv) for kk, vv in
                               ((1, dr["drop_top_1"]), (2, dr["drop_top_2"]), (3, dr["drop_top_3"]))
                               if vv is not None)))

    rv = runner_value_usd()
    total_mb = out["parts"]["ALL"]["moonbag"][50]["plain"]["usd"]
    print(f"\nOne hypothetical 20x runner (T=50) ≈ ${rv:+.2f} net.")
    if total_mb < 0:
        print(f"Moonbag T=50 ALL deficit ${total_mb:+.2f} needs "
              f"~{math.ceil(-total_mb / rv)} such runners to break even.")

    Path(__file__).parent.joinpath("moonbag_results.json").write_text(
        json.dumps(out, indent=1, default=str))
    print("\nwrote analysis/x2_moonbag/moonbag_results.json")


if __name__ == "__main__":
    main()
