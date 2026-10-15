#!/usr/bin/env python3
"""Iteration 5 BSC Track B screens. Reads journals; writes only to stdout."""

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from iter1_trackb_screens import load_pairs, stats, costed_usd, infer_chain

ROOT = Path(__file__).resolve().parents[1]
CUTOFF = datetime(2026, 9, 28, 16, 24, tzinfo=timezone.utc).timestamp()
FEE_BIAS = 0.0025


def epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def source(e):
    return e.get("source") if e.get("source") is not None else "<missing>"


def number(value):
    if value is None:
        return "NA"
    return f"{value:.3f}" if isinstance(value, (float, int)) else str(value)


def row(label, pairs, slip=.0015):
    s = stats(pairs, slippage=slip)
    if not s["n"]:
        return f"| {label} | 0 | NA | NA | NA | NA | NA |"
    return (f"| {label} | {s['n']} | {s['net']:+.2f} | {number(s['pf'])} | "
            f"{s['wr']:.1%} | {s['exp']:+.4f} | {s['max_dd']:.2f} |")


def pf(s):
    v = s.get("pf")
    return float("inf") if v == "inf" else v


def gate(is_pairs, oos_pairs, base_oos):
    a, b, stress = stats(is_pairs), stats(oos_pairs), stats(oos_pairs, .0045)
    ap, bp, sp = pf(a), pf(b), pf(stress)
    base_pf = pf(stats(base_oos))
    retention = bp / ap if ap not in (None, 0) and bp is not None else None
    gain = bp - base_pf if bp is not None and base_pf is not None else None
    flags = []
    if a["n"] < 100: flags.append("IS<100")
    if b["n"] < 30: flags.append("OOS<30")
    if retention is None or retention < .6: flags.append("retention<60%")
    if gain is None or not (gain >= .25 or (b["n"] >= 30 and b.get("exp", 0) > 0)):
        flags.append("significance")
    if sp is None or sp < 1: flags.append("3x-fee PF<1")
    if b.get("wr", 0) > .9: flags.append("WR>90%")
    if retention is not None and 1-retention > .7: flags.append("OOS decay>70%")
    return (retention, gain, flags)


def main():
    all_pairs = load_pairs()
    pairs = [p for p in all_pairs if infer_chain(*p) == "bsc"]
    journal = [json.loads(line) for line in open(ROOT / "runs/paper-1h/candidates.jsonl") if line.strip()]
    obs = defaultdict(list)
    veto = defaultdict(list)
    for r in journal:
        key = (r.get("chain"), r.get("mint"))
        if r.get("event") == "candidate" and r.get("ts_epoch"):
            obs[key].append(r)
        if r.get("event") == "slip_veto" and r.get("ts_epoch") and float(r["ts_epoch"]) >= CUTOFF:
            veto[key].append(float(r["ts_epoch"]))
    for values in veto.values():
        values.sort()

    def entry_epoch(e, c):
        wall = epoch(e["ts"])
        rows = obs[(infer_chain(e, c), e.get("mint"))]
        if rows:
            delta, matched = min(((abs(wall-epoch(r["ts"])), r) for r in rows), key=lambda x: x[0])
            if delta <= 1800:
                return wall + (float(matched["ts_epoch"])-epoch(matched["ts"]))
        return wall

    # First touch is based on the full append-order entry stream, not close order.
    import iter1_trackb_screens as base
    with open(Path(base.PROJ) / "runs/paper-1h/trades.jsonl") as handle:
        trades = [json.loads(line) for line in handle if line.strip()]
    first = {}
    for index, t in enumerate(trades):
        if t.get("type") != "entry":
            continue
        day = datetime.fromtimestamp(epoch(t["ts"]), timezone.utc).date()
        key = (t.get("chain"), t.get("mint"), day)
        first.setdefault(key, index)
    # Entry dict identity is lost in the second parse; assign per-key append indices.
    entry_indices = defaultdict(list)
    for index, t in enumerate(trades):
        if t.get("type") == "entry":
            entry_indices[(t.get("chain"), t.get("mint"))].append((index, t))
    # Match duplicate entry records in FIFO order, which is also load_pairs' policy.
    pair_entry_index = {}
    used = defaultdict(int)
    for p in all_pairs:
        e, c = p
        key = (e.get("chain"), e.get("mint"))
        candidates = entry_indices[key]
        while used[key] < len(candidates) and candidates[used[key]][1] != e:
            used[key] += 1
        if used[key] < len(candidates):
            pair_entry_index[id(e)] = candidates[used[key]][0]
            used[key] += 1

    def r3(e, c):
        value = e.get("slip_from_signal_pct")
        return value is None or float(value) <= FEE_BIAS

    def r3s(e, c):
        if not r3(e, c):
            return False
        t = entry_epoch(e, c)
        return not any(v < t for v in veto[(infer_chain(e, c), e.get("mint"))])

    def s1(e, c):
        day = datetime.fromtimestamp(epoch(e["ts"]), timezone.utc).date()
        key = (e.get("chain"), e.get("mint"), day)
        return pair_entry_index.get(id(e)) == first.get(key)

    print(f"BSC FIFO pairs: {len(pairs)}; all-chain pairs: {len(all_pairs)}")
    print(f"IS source counts (first 2/3): {dict(Counter(source(e) for e,c in pairs[:2*len(pairs)//3]))}")
    majority = sorted(Counter(source(e) for e,c in pairs[:2*len(pairs)//3]).items(), key=lambda item: (-item[1], item[0]))[0][0]
    print(f"S2 majority source: {majority}")
    print("\n## Venue buckets (full BSC universe, entry source)")
    print("| Source | n | net $ | PF | WR | exp $ | maxDD $ |\n|---|---:|---:|---:|---:|---:|---:|")
    for src in sorted({source(e) for e,c in pairs}):
        print(row(src, [p for p in pairs if source(p[0]) == src]))

    screens = {"R3": r3, "R3S": r3s, "S1": s1, "S2": lambda e,c: source(e) == majority}
    for fraction, name in ((2/3, "2/3"), (.6, "60/40"), (.7, "70/30")):
        k = 2*len(pairs)//3 if name == "2/3" else int(len(pairs)*fraction)
        is_all, oos_all = pairs[:k], pairs[k:]
        print(f"\n## Split {name}: IS universe {len(is_all)}, OOS universe {len(oos_all)}")
        print("| Cohort | n | net $ | PF | WR | exp $ | maxDD $ |\n|---|---:|---:|---:|---:|---:|---:|")
        print(row("Baseline IS", is_all))
        print(row("Baseline OOS", oos_all))
        print(row("Baseline OOS 3x", oos_all, .0045))
        for label, screen in screens.items():
            a = [p for p in is_all if screen(*p)]
            b = [p for p in oos_all if screen(*p)]
            print(row(f"{label} IS", a))
            print(row(f"{label} OOS", b))
            print(row(f"{label} OOS 3x", b, .0045))
            retention, gain, flags = gate(a, b, oos_all)
            print(f"{label} gate: retention={number(retention)} gain={number(gain)} flags={','.join(flags) or 'none'}")
            top = sorted(b, key=lambda p: costed_usd(*p), reverse=True)[:2]
            drop = [p for p in b if not any(p is winner for winner in top)]
            print(row(f"{label} OOS drop top 2", drop))

    print("\n## Slip mechanism (full BSC universe)")
    print("| Cohort | n | net $ | PF | WR | exp $ | maxDD $ |\n|---|---:|---:|---:|---:|---:|---:|")
    print(row("slip missing", [p for p in pairs if p[0].get("slip_from_signal_pct") is None]))
    print(row("slip <= 0", [p for p in pairs if p[0].get("slip_from_signal_pct") is not None and p[0]["slip_from_signal_pct"] <= 0]))
    print(row("marginal (0, 0.0025]", [p for p in pairs if p[0].get("slip_from_signal_pct") is not None and 0 < p[0]["slip_from_signal_pct"] <= FEE_BIAS]))
    print(row("blocked > 0.0025", [p for p in pairs if p[0].get("slip_from_signal_pct") is not None and p[0]["slip_from_signal_pct"] > FEE_BIAS]))


if __name__ == "__main__":
    main()
