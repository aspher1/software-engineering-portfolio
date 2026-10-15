#!/usr/bin/env python3
"""Offline analysis of Type B events skipped by the MAX_EVENT_LEGS cap.

Paper/analysis only, never part of the 5-minute scan. Fetches the Kalshi
listing once with combo_scan's own sweep (same rate limiting, no extra
concurrency), groups markets by event_ticker exactly like screen_type_b, and
for every event with more than MAX_EVENT_LEGS outcomes records:
  - brackets_exhaustive on the listing rows,
  - the listing-ask price screen (event_yes_legs + screen_edge_cents, the
    same optimistic math as screen_type_b, fee multiplier 1),
  - outcome count, strike-type signature, title pattern and series.

No orderbook, event or series calls; no dedupe state, sweep plan or other
cache is written. Prints a human summary and writes a JSON report
(--out, default data/analysis/capped_events_analysis.json).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import combo_scan as cs  # noqa: E402
import scanner_env as env  # noqa: E402

DEFAULT_OUT = os.path.join(env.REPO_ROOT, "data", "analysis", "capped_events_analysis.json")
SIZE_BUCKETS = ((41, 50), (51, 100), (101, 200), (201, None))
TOP_N = 15
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


def capped_events(markets, cap=None):
    """{event_ticker: markets} for events screen_type_b skips as too large."""
    cap = cs.MAX_EVENT_LEGS if cap is None else cap
    by_event = {}
    for m in markets:
        by_event.setdefault(m.get("event_ticker"), []).append(m)
    return {et: ms for et, ms in by_event.items() if et and len(ms) > cap}


def strike_signature(ms):
    return "+".join(sorted({m.get("strike_type") or "none" for m in ms}))


def title_pattern(ms):
    """Most common market title with numbers replaced by '#'."""
    pats = Counter(_NUM_RE.sub("#", (m.get("title") or "").lower()).strip() for m in ms)
    return pats.most_common(1)[0][0] if pats else ""


def _exhaustive(ms):
    try:
        return cs.brackets_exhaustive(ms)
    except (TypeError, ValueError):
        return False


def analyze_event(et, ms):
    legs = cs.event_yes_legs(ms)
    asks = [cs.quote(m)[0] for m in ms]
    rec = {
        "event_ticker": et,
        "series": cs.series_of({"event_ticker": et}),
        "outcomes": len(ms),
        "strike_types": strike_signature(ms),
        "title_pattern": title_pattern(ms),
        "exhaustive": _exhaustive(ms),
        "yes_quoted": sum(1 for a in asks if a is not None),
        "all_quoted": legs is not None,
        "yes_ask_sum_cents": round(sum(a for a in asks if a is not None) / 100, 2),
        "price_screen_pass": False,
    }
    if legs is not None:
        edge = cs.screen_edge_cents(legs)
        rec["screen_edge_cents"] = round(edge, 4)
        rec["price_screen_pass"] = edge > 0
    return rec


def _bucket(n):
    for lo, hi in SIZE_BUCKETS:
        if n >= lo and (hi is None or n <= hi):
            return f"{lo}-{hi}" if hi else f"{lo}+"
    return f"<{SIZE_BUCKETS[0][0]}"


def _group(records, key):
    out = {}
    for r in records:
        g = out.setdefault(r[key], {"events": 0, "exhaustive": 0, "all_quoted": 0,
                                    "price_screen_pass": 0})
        g["events"] += 1
        g["exhaustive"] += r["exhaustive"]
        g["all_quoted"] += r["all_quoted"]
        g["price_screen_pass"] += r["price_screen_pass"]
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["events"]))


def summarize(records):
    counts = sorted(r["outcomes"] for r in records)
    quoted = [r for r in records if r["all_quoted"]]
    return {
        "events": len(records),
        "markets": sum(counts),
        "outcomes_min": counts[0] if counts else None,
        "outcomes_median": counts[len(counts) // 2] if counts else None,
        "outcomes_max": counts[-1] if counts else None,
        "outcome_buckets": {b: n for b, n in sorted(
            Counter(_bucket(n) for n in counts).items(), key=lambda kv: int(kv[0].split("-")[0].rstrip("+")))},
        "exhaustive": sum(r["exhaustive"] for r in records),
        "all_quoted": len(quoted),
        "price_screen_pass": sum(r["price_screen_pass"] for r in records),
        "exhaustive_and_pass": sum(r["exhaustive"] and r["price_screen_pass"] for r in records),
        "by_strike_types": _group(records, "strike_types"),
        "by_bucket_exhaustive": dict(Counter(_bucket(r["outcomes"]) for r in records
                                             if r["exhaustive"])),
        "top_series": dict(list(_group(records, "series").items())[:TOP_N]),
        "top_title_patterns": dict(Counter(r["title_pattern"] for r in records).most_common(TOP_N)),
        "price_screen_passes": sorted(
            ({k: r[k] for k in ("event_ticker", "outcomes", "strike_types", "exhaustive",
                                "screen_edge_cents")} for r in records if r["price_screen_pass"]),
            key=lambda r: -r["screen_edge_cents"]),
        "closest_to_pass": [
            {k: r[k] for k in ("event_ticker", "outcomes", "strike_types", "exhaustive",
                               "screen_edge_cents")}
            for r in sorted(quoted, key=lambda r: -r["screen_edge_cents"])[:10]],
    }


def format_summary(report):
    s = report["summary"]
    lines = [
        f"capped-events analysis {report['as_of']}",
        f"listing: {report['markets_scanned']} markets in {report['sweep_seconds']}s, "
        f"coverage_complete={report['coverage_complete']}, errors={len(report['errors'])}",
        f"events with > {report['max_event_legs']} outcomes: {s['events']} "
        f"({s['markets']} markets; screen_type_b B_too_many_outcomes="
        f"{report['screen_type_b_too_many_outcomes']})",
        f"outcomes min/median/max: {s['outcomes_min']}/{s['outcomes_median']}/{s['outcomes_max']}; "
        f"buckets {s['outcome_buckets']}",
        f"exhaustive: {s['exhaustive']}  all yes asks quoted: {s['all_quoted']}  "
        f"price-screen pass: {s['price_screen_pass']}  exhaustive AND pass: {s['exhaustive_and_pass']}",
        "by strike types (events / exhaustive / all_quoted / pass):",
    ]
    for k, g in s["by_strike_types"].items():
        lines.append(f"  {k}: {g['events']} / {g['exhaustive']} / {g['all_quoted']} / "
                     f"{g['price_screen_pass']}")
    lines.append("top series (events / exhaustive / all_quoted / pass):")
    for k, g in s["top_series"].items():
        lines.append(f"  {k}: {g['events']} / {g['exhaustive']} / {g['all_quoted']} / "
                     f"{g['price_screen_pass']}")
    lines.append("top title patterns:")
    for k, n in s["top_title_patterns"].items():
        lines.append(f"  {n:4d}  {k[:90]}")
    lines.append("price-screen passes:" if s["price_screen_passes"] else "price-screen passes: none")
    for r in s["price_screen_passes"]:
        lines.append(f"  {r['event_ticker']} n={r['outcomes']} {r['strike_types']} "
                     f"exhaustive={r['exhaustive']} edge={r['screen_edge_cents']}c")
    lines.append("closest to passing (fully quoted):")
    for r in s["closest_to_pass"]:
        lines.append(f"  {r['event_ticker']} n={r['outcomes']} {r['strike_types']} "
                     f"exhaustive={r['exhaustive']} edge={r['screen_edge_cents']}c")
    return "\n".join(lines)


def run(out_path, budget_s):
    errors, t0 = [], time.time()
    now = datetime.now(timezone.utc)
    # Read-only on shared caches: the live scan owns the sweep plan.
    saved, cs.save_sweep_plan = cs.save_sweep_plan, lambda *a, **k: False
    try:
        markets = cs.kalshi_open_markets(int(now.timestamp()), errors, t0 + budget_s)
    finally:
        cs.save_sweep_plan = saved
    st = dict(getattr(markets, "stats", None) or {})
    b_stats = {}
    cs.screen_type_b(markets, b_stats)
    records = sorted((analyze_event(et, ms) for et, ms in capped_events(markets).items()),
                     key=lambda r: -r["outcomes"])
    report = {
        "as_of": now.isoformat(),
        "markets_scanned": len(markets),
        "sweep_seconds": st.get("sweep_seconds"),
        "coverage_complete": st.get("coverage_complete"),
        "errors": errors,
        "max_event_legs": cs.MAX_EVENT_LEGS,
        "fee_note": "listing-ask screen, fee multiplier 1 (same optimistic lower bound as screen_type_b)",
        "screen_type_b_too_many_outcomes": b_stats.get("B_too_many_outcomes", 0),
        "summary": summarize(records),
        "events": records,
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=1)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=DEFAULT_OUT, help="JSON report path")
    ap.add_argument("--budget", type=float, default=cs.SWEEP_BUDGET_S,
                    help="listing sweep budget in seconds (default: the scan's)")
    args = ap.parse_args(argv)
    report = run(args.out, args.budget)
    print(format_summary(report))
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
