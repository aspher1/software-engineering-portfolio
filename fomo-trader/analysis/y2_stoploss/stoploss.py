"""Y2: hard stop-loss recalibration via counterfactual replay.

Tests whether a tighter hard stop (-15/-20/-25/-30/-35%) beats the journal
baseline (mixed 40%/35% hard-stop regimes) on the same entries, same TP
ladder, same dump detector.

MODEL (fills at OBSERVED prices except the stop fill itself):
- Baseline per trade = journal realized (native) minus replay.estimated_cost
  (same cost convention as analysis/replay.py and the X2 moonbag track).
- Counterfactual changes ONLY the final exit leg. TP rung legs are identical
  (same entries, same ladder). The delta is applied to the portion of the
  position still held at the final exit.

FIRING RULES by close reason (only entry/peak/exit/reason are recorded; no
intratrade path exists, so every rule is a stated assumption, A1..A4):
- A1 trailing stop: the recorded exit X ~= the trade's minimum (sold when the
  trail fired). If not a gap and S > X, price crossed the stop level S on the
  way down -> stop fires, fill = S. Gap trades (exit/peak < 0.4, the journal's
  rug-gap definition) are gapped through: fill = X, unchanged.
- A2 dump detector / venue dump: unchanged. The 12%/60s dump detector already
  exits faster than a hard stop in crash scenarios; in gaps both get the gap
  fill. Sensitivity B flips this (stop replaces the dump exit when S > X).
- A3 stale exit: fires iff the stale exit price is at/below S (never observed
  for the tested levels; coded generally).
- A4 take profit / manual rotation: unchanged. An intratrade dip below S is
  unobservable -> winners-killed reports these as UNKNOWN, not zero.

COSTS: delta variable cost only on the final leg's changed gross, using
replay.py's fee+slippage convention; fixed legs unchanged (the stop replaces
the final sell 1:1). 3x stress triples the slippage term, as in replay.py.

USD: same rate convention as replay.net_pnl (sol_usd field; BSC rows carry
BNB/USD there; SOL_FALLBACK_USD for early Solana closes without quotes).

Implementation note: built directly in the coordinator's shell. The brief
permits this fallback (Codex CLI's read-only sandbox failed 3 of 4 sibling
wave-1 tracks); this is a pure replay analysis with fully understood inputs.
"""

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from replay import (  # noqa: E402
    load_journal,
    net_pnl,
    chain_of,
    rug_gap,
    SWAP_FEE,
    SLIPPAGE,
    SOL_FALLBACK_USD,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads((ROOT / "runs/paper-1h/config.json").read_text())

LEVELS = [15, 20, 25, 30, 35]
IS_N = 49  # chronological split matching the X2 moonbag track: first 49 IS, rest OOS

# sell_pct of CURRENT balance per rung index. Rung 0 = 50% in every ladder era
# observed; rung 1 = 25% (old [[100,50],[200,25]] ladder). No journal trade
# fired rung index 1, so the era ambiguity is moot.
RUNG_SELL_PCT = {0: 50.0, 1: 25.0}


def final_portion(trade):
    """Fraction of the original stake still held at the final exit."""
    remain = 1.0
    rungs = trade.get("rungs") or []
    for r in sorted(rungs, key=lambda r: r[0] if isinstance(r, (list, tuple)) else r):
        idx = r[0] if isinstance(r, (list, tuple)) else r
        remain *= 1.0 - RUNG_SELL_PCT.get(idx, 0.0) / 100.0
    return remain


def stop_outcome(trade, level_pct):
    """Return (fires, fill_ratio, category) for a hard stop at level_pct.

    fill_ratio is the fill price as a fraction of entry. Non-firing trades
    keep the observed exit ratio.
    """
    entry = float(trade.get("entry") or 0)
    peak = float(trade.get("peak") or 0)
    exit_ = trade.get("exit")
    if entry <= 0 or exit_ is None or float(exit_) < 0:
        return (False, None, "unscorable")
    exit_ = float(exit_)
    stop = entry * (1.0 - level_pct / 100.0)
    reason = str(trade.get("reason") or "").lower()
    gap = rug_gap(trade)

    if "trailing stop" in reason:
        # A1: recorded exit ~= trade minimum; gap -> gapped through at X.
        if gap:
            return (False, exit_ / entry, "trail-gap-unchanged")
        if stop > exit_:
            return (True, stop / entry, "trail-stop-fires")
        return (False, exit_ / entry, "trail-before-stop")
    if "dump" in reason:
        # A2: dump detector / venue dump already the fastest crash exit.
        return (False, exit_ / entry, "dump-unchanged")
    if "stale" in reason:
        # A3: fires only if the stale exit itself is at/below the stop.
        if stop >= exit_:
            return (True, stop / entry, "stale-stop-fires")
        return (False, exit_ / entry, "stale-unchanged")
    # A4: take profit / manual rotation / anything else: unobservable dip.
    return (False, exit_ / entry, "discretionary-unchanged")


def counterfactual_net(trade, level_pct, stress=False, fill_shock=0.0,
                       dump_flip=False):
    """(net_native, net_usd, info) with a hard stop at level_pct.

    fill_shock: adverse fill multiplier on the stop price (sensitivity A:
    0.02 = 2% worse than the stop level, modelling the 2-5s poll delay).
    dump_flip: sensitivity B, stop replaces dump exits when stop > exit
    (non-gap only).
    """
    base_native, base_usd = net_pnl(trade, CONFIG, stress=stress)
    fires, fill_ratio, cat = stop_outcome(trade, level_pct)

    if dump_flip and not fires and fill_ratio is not None:
        reason = str(trade.get("reason") or "").lower()
        if "dump" in reason and not rug_gap(trade):
            entry = float(trade.get("entry"))
            stop = entry * (1.0 - level_pct / 100.0)
            if stop > float(trade.get("exit")):
                fires, fill_ratio = True, stop / entry
                cat = "dump-stop-fires(sensB)"

    info = {"fires": fires, "category": cat, "base_usd": base_usd}
    if not fires or fill_ratio is None:
        return base_native, base_usd, info

    entry = float(trade.get("entry"))
    exit_ = float(trade.get("exit"))
    buy = float(trade.get("buy_bnb") or trade.get("buy_sol") or 0)
    fp = final_portion(trade)
    chain = chain_of(trade)
    k = SWAP_FEE[chain] + SLIPPAGE * (3 if stress else 1)
    stop_price = entry * (1.0 - level_pct / 100.0) * (1.0 - fill_shock)
    # Delta on the final leg only: changed gross minus changed variable cost.
    delta = fp * buy * (stop_price - exit_) / entry * (1.0 - k)
    native = base_native + delta
    rate = trade.get("sol_usd")
    if rate is None and chain == "solana":
        rate = SOL_FALLBACK_USD
    usd = native * float(rate) if rate is not None else None
    info.update({"delta_native": delta, "final_portion": fp,
                 "stop_price": stop_price})
    return native, usd, info


def summarize(rows):
    """rows: list of dicts with 'usd'. Returns metric dict."""
    vals = [r["usd"] for r in rows if r["usd"] is not None]
    n = len(vals)
    net = sum(vals)
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v < 0]
    gross_w = sum(wins)
    gross_l = -sum(losses)
    pf = (gross_w / gross_l) if gross_l > 0 else (math.inf if gross_w > 0 else 0.0)
    wr = len(wins) / n if n else 0.0
    dd, peak = 0.0, 0.0
    cum = 0.0
    for v in vals:
        cum += v
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"n": n, "net": net, "pf": pf, "wr": wr, "dd": dd,
            "n_usd_missing": len(rows) - n}


def evaluate(levels=LEVELS, stress=False, **kw):
    """Per-level metrics on ALL / IS / OOS plus winners-killed analysis."""
    trades, counts = load_journal()
    splits = {"ALL": trades, "IS": trades[:IS_N], "OOS": trades[IS_N:]}
    out = {"counts": counts, "levels": {}}
    for lvl in levels:
        lvl_out = {}
        for name, chunk in splits.items():
            rows = []
            for t in chunk:
                native, usd, info = counterfactual_net(t, lvl, stress=stress, **kw)
                rows.append({"usd": usd, "native": native, **info, "trade": t})
            m = summarize(rows)
            # winners-killed (charleytrades objection)
            base_winners = [r for r in rows
                            if (r["base_usd"] or 0) > 0]
            killed = [r for r in base_winners
                      if r["fires"] and (r["usd"] or 0) <= 0]
            reduced = [r for r in base_winners
                       if r["fires"] and (r["usd"] or 0) > 0
                       and r["usd"] < r["base_usd"]]
            unknown = [r for r in base_winners
                       if r["category"] == "discretionary-unchanged"]
            fired_total = sum(1 for r in rows if r["fires"])
            m.update({
                "winners_killed": len(killed),
                "winners_reduced": len(reduced),
                "winners_unknown_dip": len(unknown),
                "base_winners": len(base_winners),
                "stops_fired": fired_total,
                "cats": {c: sum(1 for r in rows if r["category"] == c)
                         for c in sorted({r["category"] for r in rows})},
            })
            lvl_out[name] = m
        out["levels"][lvl] = lvl_out
    return out


def fmt(v, digits=2):
    if v is None:
        return "n/a"
    if isinstance(v, float) and math.isinf(v):
        return "inf"
    return f"{v:+.{digits}f}" if isinstance(v, float) else str(v)


def main():
    res = evaluate()
    res_stress = evaluate(stress=True)
    res_shock = evaluate(fill_shock=0.02)
    res_dumpflip = evaluate(dump_flip=True)
    print("stop | split | net_usd | PF | WR | maxDD | fired | killed/red/unkn")
    for lvl in LEVELS:
        for name in ("ALL", "IS", "OOS"):
            m = res["levels"][lvl][name]
            s = res_stress["levels"][lvl][name]
            print(f"{lvl:>4}% | {name:>3} | {m['net']:+8.2f} | {m['pf']:.3f} | "
                  f"{m['wr']*100:4.1f}% | {m['dd']:7.2f} | {m['stops_fired']:>3} | "
                  f"{m['winners_killed']}/{m['winners_reduced']}/{m['winners_unknown_dip']} "
                  f"| 3x:{s['net']:+8.2f}")
    print("\nSensitivities (ALL net_usd):")
    for lvl in LEVELS:
        a = res["levels"][lvl]["ALL"]["net"]
        b = res_shock["levels"][lvl]["ALL"]["net"]
        c = res_dumpflip["levels"][lvl]["ALL"]["net"]
        print(f"  {lvl}%: base {a:+8.2f} | fill-2% {b:+8.2f} | dump-flip {c:+8.2f}")
    # baseline (journal actuals) for reference
    trades, _ = load_journal()
    for name, chunk in (("ALL", trades), ("IS", trades[:IS_N]), ("OOS", trades[IS_N:])):
        rows = []
        for t in chunk:
            _, usd = net_pnl(t, CONFIG)
            rows.append({"usd": usd})
        m = summarize(rows)
        print(f"BASELINE {name}: net {m['net']:+.2f} PF {m['pf']:.3f} "
              f"WR {m['wr']*100:.1f}% DD {m['dd']:.2f} n={m['n']}")
    Path(__file__).with_name("y2_results.json").write_text(
        json.dumps({"levels": LEVELS, "is_n": IS_N,
                    "main": _jsonable(res),
                    "stress3x": _jsonable(res_stress),
                    "fill_shock_2pct": _jsonable(res_shock),
                    "dump_flip": _jsonable(res_dumpflip)}, indent=1))


def _jsonable(res):
    out = {}
    for lvl, splits in res["levels"].items():
        out[str(lvl)] = {}
        for name, m in splits.items():
            d = dict(m)
            d["pf"] = None if math.isinf(d["pf"]) else d["pf"]
            out[str(lvl)][name] = d
    return out


if __name__ == "__main__":
    main()
