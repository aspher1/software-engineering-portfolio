#!/usr/bin/env python3
"""Latency bench for the observation-mode hooks on the entry/close path.

  - maybe_tag_research: flags off, and flags on (registry stat + tag), over
    >= 10,000 realistic enrichment dicts; p50/p99 vs the 50 ms budget.
  - maybe_research_dashboard with flags off (the default close path).
  - obs_dashboard.update on a copy of the live journal (read-only) and on a
    synthetic 1,000-close journal; writes only to a temp directory.

    .venv/bin/python analysis/obs/bench_research_hooks.py [iterations]
"""
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import fomo_trader  # noqa: E402
import obs_dashboard  # noqa: E402

BUDGET_MS = 50.0

ENRICHMENT = {
    "signal_price_usd": 0.000281651338074074,
    "commit_price_native": 3.0043897788176485e-07,
    "commit_price_usd": 0.00023368744577599435,
    "slip_from_signal_pct": -0.17029527580467307,
    "holder_top1_pct": None, "holder_top5_pct": None,
    "lp_burn_pct": None, "lp_locked": None, "liquidity_usd": 84318,
    "entry_latency_ms": 2373, "m15_buys": 92, "m15_sells": 24,
    "m15_volume_usd": 49429, "mcap_usd": 314186,
}


def pct(samples_ns, q):
    s = sorted(samples_ns)
    return s[min(len(s) - 1, int(q * len(s)))] / 1e6


def bench(fn, make_arg, n):
    samples = []
    for _ in range(n):
        arg = make_arg()
        t0 = time.perf_counter_ns()
        fn(arg)
        samples.append(time.perf_counter_ns() - t0)
    return {"n": n, "p50_ms": round(pct(samples, 0.50), 6),
            "p99_ms": round(pct(samples, 0.99), 6),
            "max_ms": round(max(samples) / 1e6, 6)}


def synthetic_journal(path, closes):
    with open(path, "w") as f:
        for i in range(closes):
            mint = "M%06d" % i
            f.write(json.dumps({"ts": "2026-09-27 12:%02d:%02d" % (i // 60 % 60, i % 60),
                                "type": "entry", "mint": mint, "entry": 1e-7,
                                "buy_sol": 0.06, "slip_from_signal_pct": 0.05,
                                "entry_latency_ms": 1500}) + "\n")
            f.write(json.dumps({"ts": "2026-09-27 12:%02d:%02d" % (i // 60 % 60, i % 60),
                                "type": "close", "mint": mint, "entry": 1e-7,
                                "peak": 1.3e-7, "exit": 0.3e-7 if i % 5 == 0 else 1.1e-7,
                                "buy_sol": 0.06, "realized_usd": 1.2 if i % 4 == 0 else -0.9,
                                "research_question": "Q%d" % (1 + i % 8)}) + "\n")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    n = max(10_000, int(argv[0])) if argv else 10_000
    off = {"research": {"enabled": False, "tag_trades": False,
                        "question_id": None, "dashboard": False,
                        "field_audit": False}}
    on = {"research": {"enabled": True, "tag_trades": True,
                       "question_id": "Q1", "dashboard": False,
                       "field_audit": False}}
    results = {
        "tag_absent": bench(lambda r: fomo_trader.maybe_tag_research(r, {}),
                            lambda: dict(ENRICHMENT), n),
        "tag_off": bench(lambda r: fomo_trader.maybe_tag_research(r, off),
                         lambda: dict(ENRICHMENT), n),
        "tag_on": bench(lambda r: fomo_trader.maybe_tag_research(r, on),
                        lambda: dict(ENRICHMENT), n),
        "tag_on_close": bench(
            lambda r: fomo_trader.maybe_tag_research(
                r, on, {"research_question": "Q1"}),
            lambda: {"type": "close"}, n),
        "dashboard_hook_off": bench(
            lambda s: fomo_trader.maybe_research_dashboard(off, s),
            lambda: "/nonexistent/state.json", n),
    }
    tmp = tempfile.mkdtemp(prefix="z3-bench-")
    try:
        out = os.path.join(tmp, "kill_dashboard.json")
        live = os.path.join(ROOT, "runs", "paper-1h", "trades.jsonl")
        if os.path.exists(live):
            copy = os.path.join(tmp, "live_copy.jsonl")
            shutil.copyfile(live, copy)
            results["dashboard_update_live_journal"] = bench(
                lambda p: obs_dashboard.update(p, out), lambda: copy, 200)
        synth = os.path.join(tmp, "synthetic.jsonl")
        synthetic_journal(synth, 1000)
        results["dashboard_update_1000_closes"] = bench(
            lambda p: obs_dashboard.update(p, out), lambda: synth, 100)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    worst = max(r["p99_ms"] for k, r in results.items() if k.startswith("tag"))
    results["tag_budget"] = {"budget_ms": BUDGET_MS, "worst_p99_ms": worst,
                             "within_budget": worst < BUDGET_MS}
    print(json.dumps(results, indent=2))
    return 0 if worst < BUDGET_MS else 1


if __name__ == "__main__":
    sys.exit(main())
