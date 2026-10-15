#!/usr/bin/env python3
"""Daily analysis for the paper trader. Reads trades.jsonl and state.json,
computes per-sleeve performance stats, drawdown, and scan throughput, and
writes a dated report.

HARD RULE: analyze.py NEVER modifies config.json itself - or any live state.
It proposes at most ONE concrete tweak with the evidence numbers inline (or
none at all), and the human operator applies it by hand and logs the change
in CHANGELOG.md. Proposals are text only. This script performs no trades.

Usage: python analyze.py [--days N]   (default: all history)
Report: analysis/YYYY-MM-DD.md
"""
import argparse
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RUN = os.path.dirname(os.path.abspath(__file__))
JOURNAL = os.path.join(RUN, "trades.jsonl")
STATE_PATH = os.path.join(RUN, "state.json")
OUTDIR = os.path.join(RUN, "analysis")


def load_trades():
    trades = []
    if not os.path.exists(JOURNAL):
        return trades
    with open(JOURNAL) as f:
        for line in f:
            try:
                trades.append(json.loads(line))
            except ValueError:
                pass
    return trades


def load_state():
    """Read state.json; return {} when missing or unreadable."""
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def stats(closes):
    n = len(closes)
    if not n:
        return None
    pnls = [c["pnl"] for c in closes]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return {
        "n": n,
        "win_rate": len(wins) / n * 100,
        "avg_win": sum(wins) / len(wins) if wins else 0,
        "avg_loss": sum(losses) / len(losses) if losses else 0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "expectancy": sum(pnls) / n,
        "total": sum(pnls),
        "best": max(pnls),
        "worst": min(pnls),
    }


def propose(cfg_note, all_stats, sleeve_stats, max_drawdown_pct=None):
    """At most ONE concrete proposal (text only), conservative, only with
    >=10 closed trades overall. `cfg_note` is kept for call-site
    compatibility; it does not gate proposals. Rules are ordered by priority
    (risk first, edge problems second, scaling notes last) and only the
    first firing rule is returned."""
    candidates = []
    total_n = sum(s["n"] for s in sleeve_stats.values() if s)
    if total_n < 10:
        return ["No tuning yet: only %d closed trades (need >= 10)." % total_n]

    mdd = (max_drawdown_pct if isinstance(max_drawdown_pct, (int, float))
           else 0.0)

    # 1. drawdown-aware risk rule
    if mdd > 10:
        candidates.append(
            "Max drawdown %.1f%% > 10%% -> tighten risk first: smaller trade "
            "size or wider stops; do NOT widen entries or scale size while "
            "drawdown is elevated." % mdd)

    # 2. overall edge rule (uses the previously ignored all_stats)
    if all_stats:
        o = all_stats
        pf = o.get("profit_factor") or 0
        if pf < 1.0:
            candidates.append(
                "Overall PF %.2f < 1.0 over %d closed trades (win %.1f%%, "
                "expectancy $%.2f/trade, avg win $%.2f vs avg loss $%.2f) -> "
                "review exits: TP/SL/trailing widths; the leak looks like "
                "exits, not entries." % (pf, o["n"], o["win_rate"],
                                         o["expectancy"], o["avg_win"],
                                         o["avg_loss"]))

    # 3. existing sleeve rules (unchanged semantics)
    for sleeve, s in sleeve_stats.items():
        if not s or s["n"] < 5:
            continue
        if s["profit_factor"] < 0.8 and s["win_rate"] < 40:
            candidates.append(
                "%s: PF %.2f, win %.0f%% over %d trades -> consider widening "
                "stop or tightening entry filter; do NOT shrink size on noise."
                % (sleeve, s["profit_factor"], s["win_rate"], s["n"]))
        elif s["profit_factor"] > 1.5 and s["n"] >= 15:
            candidates.append(
                "%s: PF %.2f over %d trades -> edge holding; consider +25%% "
                "size or looser entry to scale." % (sleeve, s["profit_factor"],
                                                   s["n"]))
        elif s["avg_loss"] != 0 and abs(s["avg_win"] / s["avg_loss"]) < 1.0:
            candidates.append(
                "%s: avg win $%.2f < avg loss $%.2f -> exits cut winners too "
                "early or stops too tight; consider wider TP/ladder."
                % (sleeve, s["avg_win"], s["avg_loss"]))

    # 4. bar-approach note: healthy edge plus healthy drawdown
    if all_stats:
        pf = all_stats.get("profit_factor") or 0
        if pf > 1.2 and mdd < 10:
            candidates.append(
                "Overall PF %.2f > 1.2 over %d trades with max drawdown %.1f%% "
                "(< 10%%) -> validation bar being approached; hold params, "
                "no tweak." % (pf, all_stats["n"], mdd))

    if not candidates:
        return ["No change warranted: stats within healthy bands."]
    return candidates[:1]


def fmt_pct(v):
    return "%.2f%%" % v if isinstance(v, (int, float)) else "n/a"


def fmt_money(v):
    return "$%.2f" % v if isinstance(v, (int, float)) else "n/a"


def scan_stats_for_today(state, today):
    """Pull today's scan_stats from state.json. Tolerates a flat dict with a
    'date' key, a per-date nested dict, or a list of dated rows."""
    ss = state.get("scan_stats")
    if not ss:
        return None
    if isinstance(ss, dict):
        if ss.get("date") == today:
            return ss
        day = ss.get(today)
        if isinstance(day, dict):
            return day
        return None
    if isinstance(ss, list):
        for row in reversed(ss):
            if isinstance(row, dict) and row.get("date") == today:
                return row
    return None


def throughput_lines(ss):
    """Report lines for one day's scan_stats (paper_trader writes
    scans / signals / entered / crypto_skipped)."""
    if not ss:
        return ["no scan_stats in state.json - throughput data unavailable\n"]
    scans = ss.get("scans", 0) or 0
    found = ss.get("signals", ss.get("signals_found", 0)) or 0
    entered = ss.get("entered", ss.get("signals_entered", 0)) or 0
    skipped = ss.get("crypto_skipped", 0) or 0
    sps = found / scans if scans else 0
    out = ["scans %d | signals found %d | signals entered %d | "
           "signals/scan %.2f | crypto scans skipped (no data) %d"
           % (scans, found, entered, sps, skipped)]
    approvals = ss.get("ai_approvals", 0) or 0
    vetoes = ss.get("ai_vetoes", 0) or 0
    if approvals or vetoes:
        out.append("AI gate: %d approved / %d vetoed (%.0f%% pass rate)"
                   % (approvals, vetoes,
                      100 * approvals / (approvals + vetoes)))
    if scans and skipped >= scans / 2:
        out.append("verdict: crypto data feed failing on %d/%d scans - fix "
                   "data before judging entry filters\n" % (skipped, scans))
    elif scans >= 20 and found == 0:
        out.append("verdict: no signals firing - check market conditions "
                   "before loosening entry filters\n")
    elif found > 0 and entered == 0:
        out.append("verdict: signals found but none entered - entry guards "
                   "(size / caps / cooldown) may be too tight\n")
    else:
        out.append("verdict: throughput looks normal\n")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0)
    args = ap.parse_args()
    trades = load_trades()
    closes = [t for t in trades if t.get("type") == "close"]
    entries = [t for t in trades if t.get("type") == "open" or t.get("type") == "entry"]

    sleeves = {}
    for sleeve in ("crypto", "stocks"):
        sleeves[sleeve] = stats([c for c in closes if c.get("sleeve") == sleeve])
    overall = stats(closes)

    # per-symbol table
    by_sym = {}
    for c in closes:
        by_sym.setdefault(c.get("symbol", "?"), []).append(c["pnl"])

    state = load_state()
    max_dd = state.get("max_drawdown_pct")

    today = datetime.now(ET).strftime("%Y-%m-%d")
    out = []
    out.append("# Paper trader analysis - %s\n" % today)
    out.append("Entries journaled: %d | closed: %d\n" % (len(entries), len(closes)))
    if overall:
        o = overall
        out.append("## Overall")
        out.append("trades %d | win rate %.1f%% | PF %.2f | expectancy $%.2f/trade | total %+.2f"
                   % (o["n"], o["win_rate"], o["profit_factor"], o["expectancy"], o["total"]))
        out.append("best %+.2f | worst %.2f\n" % (o["best"], o["worst"]))
    for sleeve, s in sleeves.items():
        out.append("## %s" % sleeve)
        if not s:
            out.append("no closed trades yet\n")
            continue
        out.append("trades %d | win %.1f%% | PF %.2f | exp $%.2f | total %+.2f"
                   % (s["n"], s["win_rate"], s["profit_factor"], s["expectancy"], s["total"]))
        out.append("avg win $%.2f | avg loss $%.2f\n" % (s["avg_win"], s["avg_loss"]))
    if by_sym:
        out.append("## Per symbol")
        for sym, pnls in sorted(by_sym.items(), key=lambda kv: sum(kv[1])):
            out.append("%s: %d trades, %+.2f" % (sym, len(pnls), sum(pnls)))
        out.append("")

    out.append("## Drawdown")
    if not state:
        out.append("no state.json - drawdown data unavailable\n")
    else:
        out.append("peak equity %s | current drawdown %s | max drawdown %s"
                   % (fmt_money(state.get("peak_equity")),
                      fmt_pct(state.get("drawdown_pct")),
                      fmt_pct(max_dd)))
        halted = state.get("entries_halted")
        if halted is None:
            out.append("entries kill switch: n/a (not tracked in state.json)\n")
        else:
            out.append("entries kill switch: %s\n"
                       % ("ENGAGED" if halted else "not engaged"))

    out.append("## Throughput")
    out.extend(throughput_lines(scan_stats_for_today(state, today)))

    gate = state.get("ai_gate") if state else None
    if gate and gate.get("date") == today and gate.get("calls"):
        out.append("## AI gate")
        out.append("calls %d | approved %d | judgment vetoes %d | "
                   "quota vetoes %d"
                   % (gate.get("calls", 0), gate.get("approvals", 0),
                      gate.get("vetoes", 0), gate.get("quota_vetoes", 0)))
        qv = gate.get("quota_vetoes", 0)
        if qv and qv >= gate.get("calls", 0) / 2:
            out.append("note: gate mostly blind today - quota outages, "
                       "not AI judgment, blocked entries\n")

    out.append("## Proposal")
    for p in propose("", overall, sleeves, max_dd):
        out.append("- " + p)

    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, today + ".md")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")
    print("\n".join(out))
    print("\nwrote " + path)


if __name__ == "__main__":
    main()
