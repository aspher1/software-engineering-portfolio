#!/usr/bin/env python3
"""Backtest of the Tier 3 model-edge entry rule on Kalshi-settled signals.

Reads a graded-signals file, signal_grades.jsonl (--grades, else
COMBO_SCANNER_GRADES_PATH, else data/signal_grades.jsonl; not shipped with
the repo; read-only, nothing is written anywhere) and replays the rule with
honest fills:
  - rows: graded from Kalshi settlement (proxy == "kalshi", not
    excluded_from_accuracy), scored (correct is not None), with market_mid
    and edge recorded;
  - taken_mid = market_mid (YES) or 1 - market_mid (NO);
    fair_taken = taken_mid + edge (YES) or taken_mid - edge (NO);
  - fill at the ASK: ask = taken_mid + spread/2 (grade rows store no spread,
    so --spread is assumed, default 4c);
  - contracts = floor(stake / ask); Kalshi taker fee
    round up(0.07 x C x P x (1-P)) to the whole cent (schedule effective
    2026-07-07, same conservative rounding as combo_scan.py);
  - rule: agreement (taken_mid >= 0.5) AND fair_taken - ask >= --threshold.
Net $/$ = total P&L / total outlay (asks + fees).

The threshold is chosen on the first half (by graded_at_utc) and reported on
both halves. The placebo is the rule's own takes with zero true edge and the
same fills: its expected $/$ is minus the mean cost drag
(spread/2 x contracts + fee) / stake.
No network. Exits 0.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

import scanner_env as env

DEFAULT_GRADES = env.env_path("COMBO_SCANNER_GRADES_PATH",
                              os.path.join(env.REPO_ROOT, "data", "signal_grades.jsonl"))
KALSHI_TAKER_RATE = 0.07
SWEEP_THRESHOLDS = (3.0, 5.0, 7.0, 10.0)


def taker_fee_dollars(price, contracts, multiplier=1.0):
    """Kalshi taker fee in dollars, rounded UP to the whole cent (conservative)."""
    if contracts <= 0 or not 0 < price < 1:
        return 0.0
    cents = KALSHI_TAKER_RATE * multiplier * contracts * price * (1 - price) * 100
    return math.ceil(cents - 1e-9) / 100


def load_rows(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("proxy") != "kalshi" or r.get("excluded_from_accuracy"):
                continue
            if r.get("correct") is None or r.get("market_mid") is None or r.get("edge") is None:
                continue
            if r.get("side") not in ("YES", "NO"):
                continue
            rows.append(r)
    rows.sort(key=lambda r: r.get("graded_at_utc") or "")
    return rows


def take(r, spread, stake):
    """(taken_mid, fair_taken, ask, contracts, fee, outlay) for one row."""
    mid, edge = float(r["market_mid"]), float(r["edge"])
    taken = mid if r["side"] == "YES" else 1 - mid
    fair_taken = taken + (edge if r["side"] == "YES" else -edge)
    ask = taken + spread / 200
    contracts = math.floor(stake / ask + 1e-9) if 0 < ask < 1 else 0
    fee = taker_fee_dollars(ask, contracts)
    return taken, fair_taken, ask, contracts, fee, contracts * ask + fee


def passes(r, spread, stake, threshold):
    taken, fair_taken, ask, contracts, _, _ = take(r, spread, stake)
    return taken >= 0.5 and (fair_taken - ask) * 100 >= threshold - 1e-9 and contracts > 0


def simulate(rows, spread, stake, threshold):
    n = wins = 0
    pnl = outlay = 0.0
    for r in rows:
        if not passes(r, spread, stake, threshold):
            continue
        _, _, _, contracts, _, cost = take(r, spread, stake)
        n += 1
        wins += bool(r["correct"])
        pnl += (contracts if r["correct"] else 0) - cost
        outlay += cost
    return {"n": n, "wins": wins, "pnl": pnl, "outlay": outlay,
            "net_per_dollar": pnl / outlay if outlay else None}


def placebo_drag(rows, spread, stake):
    """Mean (spread/2 x contracts + fee) / stake over every row's take."""
    drags = []
    for r in rows:
        _, _, ask, contracts, fee, _ = take(r, spread, stake)
        if contracts > 0:
            drags.append((spread / 200 * contracts + fee) / stake)
    return sum(drags) / len(drags) if drags else None


def fmt(res):
    if not res["n"]:
        return "n=0 (no takes)"
    return (f"n={res['n']:3d}  wins={res['wins']:3d}  win={res['wins'] / res['n']:6.1%}  "
            f"net={res['net_per_dollar']:+.3f} $/$  (P&L ${res['pnl']:+.2f} on ${res['outlay']:.2f})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--grades", default=DEFAULT_GRADES, help="signal_grades.jsonl (read-only)")
    ap.add_argument("--spread", type=float, default=4.0, help="assumed spread in cents (default 4)")
    ap.add_argument("--stake", type=float, default=15.0, help="dollars per take (default 15)")
    ap.add_argument("--threshold", type=float, default=5.0,
                    help="min fair_taken - ask in cents (default 5)")
    args = ap.parse_args(argv)

    try:
        rows = load_rows(args.grades)
    except OSError as e:
        print(f"cannot read grades: {e}")
        return 0
    print(f"Model-edge backtest — {args.grades}")
    print(f"Kalshi-settled scored rows with mid+edge: {len(rows)}"
          + (f" ({rows[0]['graded_at_utc'][:19]} .. {rows[-1]['graded_at_utc'][:19]} UTC)"
             if rows else ""))
    print(f"Fills: ask = taken mid + {args.spread / 2:g}c (assumed {args.spread:g}c spread), "
          f"${args.stake:g} stake, Kalshi taker fee rounded up per leg")
    if not rows:
        return 0
    half = len(rows) // 2
    train, test = rows[:half], rows[half:]

    print(f"\nRule: agree (taken mid >= 0.5) AND fair - ask >= {args.threshold:g}c")
    print(f"  all    {fmt(simulate(rows, args.spread, args.stake, args.threshold))}")

    print(f"\nThreshold sweep on the first half (n={len(train)} rows):")
    sweep = {t: simulate(train, args.spread, args.stake, t) for t in SWEEP_THRESHOLDS}
    for t, res in sweep.items():
        print(f"  {t:4.0f}c  {fmt(res)}")
    scored = [(t, r) for t, r in sweep.items() if r["n"] and r["net_per_dollar"] is not None]
    chosen = max(scored, key=lambda x: x[1]["net_per_dollar"])[0] if scored else args.threshold
    print(f"  chosen on first half: {chosen:g}c (shipped rule: {args.threshold:g}c)")

    print(f"\nTime split at {chosen:g}c (halves by graded_at_utc):")
    print(f"  first  {fmt(simulate(train, args.spread, args.stake, chosen))}")
    print(f"  second {fmt(simulate(test, args.spread, args.stake, chosen))}")

    taken_rows = [r for r in rows if passes(r, args.spread, args.stake, args.threshold)]
    agree_rows = [r for r in rows if take(r, args.spread, args.stake)[0] >= 0.5]
    print("\nPlacebo (zero true edge, same fills; expected $/$ = -mean cost drag / stake):")
    for label, rs in (("rule's takes", taken_rows), ("all agreeing rows", agree_rows)):
        drag = placebo_drag(rs, args.spread, args.stake)
        if drag is not None:
            print(f"  {label:<18} n={len(rs):3d}  drag {drag:.3f} -> expected {-drag:+.3f} $/$")
    print("\nCaveats: grade rows carry no spread (assumed); small n; one market regime.")
    return 0


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (0, None):
            print(f"bad arguments: exit {e.code}")
    sys.exit(0)
