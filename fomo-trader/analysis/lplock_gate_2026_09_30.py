"""Track B 2026-09-30: LP-lock enforce-gate validation on measure-mode data.

Pre-registered methodology (from CHANGELOG 2026-09-29 iter-4):
  - enforce semantics: veto entries with lp_lock_verdict == "unlocked";
    "burned"/"no_pair"/"unknown" enter (fail open), exactly as iter-2 designed.
  - ship gates: OOS >= 30 trades, OOS edge retention >= 60% of IS edge,
    OOS net > 0 under 3x-fee stress, kept win rate <= 90%,
    edge not concentrated in a single regime (BSC-only note: the whole
    measure dataset is BSC by construction).
  - costs: replay cost model (0.25% swap fee + 0.15% slippage per leg,
    0.00002 BNB gas per leg); stress triples slippage.
  - IS/OOS: chronological append order, first 30 = IS, last 30 = OOS.
    (IS >= 100 significance bar cannot be met on the 60-trade measure
    dataset; the gate's own pre-registered minimum was OOS >= 30.)

Read-only: never writes trades.jsonl/state.json/bot.log/config. Prints a
verdict. ship_recommend=false is the default outcome.
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analysis.replay import load_journal, chain_of, estimated_cost  # noqa: E402


def costed_usd(trade, stress=False):
    """Journal paper USD minus modeled execution costs (never invented)."""
    base = float(trade.get("realized_usd") or 0.0)
    rate = trade.get("sol_usd")  # on BSC rows this carries the cached BNB/USD rate
    if rate is None:
        return base  # missing rate: report journal value, never invent
    cost_native = estimated_cost(trade, stress=stress)
    return base - cost_native * float(rate)


def summarize(trades, stress):
    vals = [costed_usd(t, stress) for t in trades]
    n = len(vals)
    net = sum(vals)
    wins = sum(v > 0 for v in vals)
    gains = sum(v for v in vals if v > 0)
    losses = -sum(v for v in vals if v < 0)
    cum = high = dd = 0.0
    for v in vals:
        cum += v
        high = max(high, cum)
        dd = max(dd, high - cum)
    return {
        "n": n, "net": net, "edge": net / n if n else 0.0,
        "wr": wins / n if n else 0.0,
        "pf": gains / losses if losses else (float("inf") if gains else 0.0),
        "dd": dd,
    }


def main():
    trades, counts = load_journal()
    measured = [t for t in trades
                if not t.get("unmatched_close")
                and t.get("entry_record") is not None
                and t["entry_record"].get("lp_lock_verdict") is not None]
    print(f"journal: {len(trades)} paired trades; measured LP-lock entries with close: {len(measured)}")

    v = Counter(t["entry_record"]["lp_lock_verdict"] for t in measured)
    burn = [t["entry_record"].get("lp_lock_burn_pct") for t in measured
            if t["entry_record"].get("lp_lock_burn_pct") is not None]
    print(f"verdicts: {dict(v)}; burn_pct present on {len(burn)}/{len(measured)}")
    if burn:
        print(f"burn_pct: min={min(burn):.2f} max={max(burn):.2f}")

    # Measurement audit: does "unlocked" predict wipe-like closes?
    by_v = {}
    for verdict in sorted(v):
        sub = [t for t in measured if t["entry_record"]["lp_lock_verdict"] == verdict]
        reasons = Counter(str(t.get("reason") or "")[:40] for t in sub)
        by_v[verdict] = (len(sub), sum(costed_usd(t) for t in sub), dict(reasons.most_common(4)))
    print("\nverdict -> (n, costed-base net USD, top close reasons):")
    for verdict, row in by_v.items():
        print(f"  {verdict}: {row}")

    # Counterfactual gate: veto "unlocked"; burned/no_pair/unknown enter (fail open)
    kept = [t for t in measured if t["entry_record"]["lp_lock_verdict"] != "unlocked"]
    vetoed = [t for t in measured if t["entry_record"]["lp_lock_verdict"] == "unlocked"]
    print(f"\ngate would veto {len(vetoed)}/{len(measured)} ({100.0*len(vetoed)/max(1,len(measured)):.1f}%)")

    if len(measured) < 60:
        print(f"only {len(measured)} measured closes; primary split needs 60 (30/30).")
    is_set, oos_set = measured[:30], measured[30:60]
    is_k = [t for t in is_set if t in kept]
    oos_k = [t for t in oos_set if t in kept]
    print(f"IS: {len(is_set)} -> kept {len(is_k)} | OOS: {len(oos_set)} -> kept {len(oos_k)}")

    report = {}
    for name, full, sub in (("IS", is_set, is_k), ("OOS", oos_set, oos_k)):
        base_f, stress_f = summarize(full, False), summarize(full, True)
        base_k, stress_k = summarize(sub, False), summarize(sub, True)
        report[name] = {"full": base_f, "full_stress": stress_f,
                        "kept": base_k, "kept_stress": stress_k}
        print(f"\n{name} full: n={base_f['n']} net=${base_f['net']:.2f} "
              f"edge=${base_f['edge']:.3f} wr={base_f['wr']:.1%} pf={base_f['pf']:.2f} dd=${base_f['dd']:.2f}")
        print(f"{name} full 3x: net=${stress_f['net']:.2f} edge=${stress_f['edge']:.3f}")
        print(f"{name} kept: n={base_k['n']} net=${base_k['net']:.2f} "
              f"edge=${base_k['edge']:.3f} wr={base_k['wr']:.1%} pf={base_k['pf']:.2f} dd=${base_k['dd']:.2f}")
        print(f"{name} kept 3x: net=${stress_k['net']:.2f} edge=${stress_k['edge']:.3f}")

    is_edge = report["IS"]["kept"]["edge"]
    oos_edge = report["OOS"]["kept"]["edge"]
    is_full_edge = report["IS"]["full"]["edge"]
    retention = oos_edge / is_edge if is_edge else float("nan")
    gates = {
        "oos_n>=30": len(oos_k) >= 30,
        "kept_oos_stress_net>0": report["OOS"]["kept_stress"]["net"] > 0,
        "retention>=60%": (retention >= 0.6) if retention == retention else False,
        "kept_wr<=90%": report["OOS"]["kept"]["wr"] <= 0.9,
        "is_edge_positive": is_edge > 0,
        "is_n>=30 (note: >=100 significance bar unmet)": len(is_k) >= 30,
    }
    print("\nship gates:")
    for g, ok in gates.items():
        print(f"  {'PASS' if ok else 'FAIL'} {g}")
    print(f"  retention (kept OOS edge / kept IS edge): {retention:.1%}" if retention == retention else "  retention: n/a")
    print(f"  IS full edge (no gate): ${is_full_edge:.3f}/trade")
    ship = all(gates.values())
    print(f"\nverdict: {'SHIP (all gates pass)' if ship else 'DO NOT SHIP; ship_recommend=false'}")


if __name__ == "__main__":
    main()
