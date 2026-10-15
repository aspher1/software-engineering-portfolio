#!/usr/bin/env python3
"""Offline combo-picker demo on SYNTHETIC legs (no network, no setup).

Loads hand-written legs from demo/fixtures/legs.json, runs the real Tier 4
picker (combo_picker.build_combo_picks) on them, and prints the top pick the
way the Kalshi app phrases a combo: "$10 pays $X". Nothing is fetched, logged
or placed; the output is a MODEL ESTIMATE on invented numbers.

Run from the repo root:  python3 demo/run_demo.py
"""

from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(HERE, "fixtures", "legs.json")

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
try:
    import combo_picker as cp
except ImportError as e:
    sys.exit(f"demo: cannot import the combo picker ({e}).\n"
             "Run from a checkout with dependencies installed:\n"
             "  python3 -m venv .venv && . .venv/bin/activate && "
             "pip install -r requirements.txt")


def load_legs(path, now_ts):
    with open(path) as f:
        rows = json.load(f)["legs"]
    legs = []
    for r in rows:
        r = dict(r)
        r["expiry_ts"] = now_ts + int(r.pop("expires_in_s"))
        r["game_key"] = cp.game_key(r["event_ticker"])
        legs.append(cp.PickLeg(**r))
    return legs


def main():
    now_ts = int(time.time())
    legs = load_legs(FIXTURE, now_ts)
    print("Combo picker demo — SYNTHETIC fixture, not real market data\n")
    print(f"{len(legs)} candidate legs (side @ ask, model fair, edge):")
    for leg in legs:
        print(f"  {leg.ticker:<18} {leg.side:<3} @ {leg.ask_cents:g}c  "
              f"fair {leg.fair_cents:g}c  edge {cp.leg_edge_cents(leg):+g}c  "
              f"[{leg.fair_source}]")

    by_ticker = {leg.ticker: leg for leg in legs}
    try:
        cp.price_combo([by_ticker["SYNTH-ELECTION-A"], by_ticker["SYNTH-ELECTION-B"]])
    except cp.PickRejected as e:
        print(f"\nCorrelation rule: SYNTH-ELECTION-A + SYNTH-ELECTION-B rejected ({e}): "
              "legs from one event are never priced as independent.")

    picks = cp.build_combo_picks(legs, now_ts)
    print(f"\n{len(picks)} passing combo(s) "
          f"(bar: >= {cp.COMBO_MIN_EDGE_CENTS * cp.PICK_REF_STAKE_DOLLARS:g}c expected edge "
          f"on ${cp.PICK_REF_STAKE_DOLLARS}, asks +{cp.MAKER_MARGIN_GUARD_CENTS:g}c/leg guard, "
          "taker fees rounded up).")
    if not picks:
        print("No pick passes — an empty result is the normal outcome on live data.")
        return 0

    p = picks[0]
    print("\nTop pick:")
    for leg in p.legs:
        print(f"  {leg.ticker} {leg.side} @ {leg.ask_cents:g}c")
    print(f"\n  ${cp.PICK_REF_STAKE_DOLLARS} pays ${p.payout_ask_10:g} "
          f"(model fair ${p.payout_fair_10:.2f}, edge {p.edge_cents_10:+.1f}c)")
    print(f"  parlay price {p.p_combo_cents:.2f}c, {p.contracts_10} contracts, "
          f"fees {p.fee_cents_10:g}c, joint fair {100 * p.joint_fair:.1f}%, "
          f"paper stake ${p.paper_stake_dollars:g}")
    print(f"\n  {p.guarantee}.")
    print("  Synthetic inputs; paper/analysis only — no orders are placed. See DISCLAIMER.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
