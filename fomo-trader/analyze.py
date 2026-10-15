#!/usr/bin/env python3
"""Paper-trading analyzer for the FOMO memecoin bot.

Reads the trade journal + state, prints a concise summary, and writes a
dated report to analysis/YYYY-MM-DD.md. Stdlib only, no network calls.

P&L accounting is chain-aware and USD-denominated: per-close P&L comes
from the journaled realized_usd (present on every BSC close); legacy
SOL-era closes that predate USD stamping are priced as
realized_sol x stamped/fallback SOL price. Closes that cannot be priced
are EXCLUDED from all stats (counted and reported) -- they carry no P&L
information and must never be counted as $0 phantoms, which would dilute
win rate and profit factor and let the tuner act on a partial book.

Improvement rule: <10 closed trades -> insufficient data, no tuning.
Otherwise AT MOST ONE conservative parameter change per run, with the
exact stat that motivates it. Never weakens the rug guard, never raises
position size or trade frequency.

With --apply (used by the daily cron), the single proposed tweak is
applied to runs/paper-1h/config.json automatically, but ONLY when:
  - config dry_run is true (auto-tune never touches live-money config),
  - the tweak is on the conservative allowlist with safe direction/value
    bounds (validated again right before writing),
  - no tweak was applied in the last 24h AND at least 5 new closed trades
    exist since the last tweak (anti-thrash cooldown),
  - the last applied tweak has not made things worse (otherwise it is
    reverted instead of stacking a new change on top).
Every application/revert is logged to analysis/tweaks.jsonl, backed up to
runs/paper-1h/config_backups/, recorded in CHANGELOG.md, and followed by
a graceful bot restart so the new values take effect (skipped if
halt.flag is present).
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
RUNDIR = os.path.join(BASE, "runs", "paper-1h")
JOURNAL = os.path.join(RUNDIR, "trades.jsonl")
STATE = os.path.join(RUNDIR, "state.json")
CONFIG = os.path.join(RUNDIR, "config.json")
BACKUP_DIR = os.path.join(RUNDIR, "config_backups")
ANALYSIS_DIR = os.path.join(BASE, "analysis")
TWEAKS_LOG = os.path.join(ANALYSIS_DIR, "tweaks.jsonl")
CHANGELOG = os.path.join(BASE, "CHANGELOG.md")

CONF_CLOSED_TRADES = 30
CONF_PROFIT_FACTOR = 1.2
MIN_TRADES_FOR_TUNING = 10
MIN_NEW_TRADES_AFTER_TWEAK = 5   # anti-thrash: need this many new closes
TWEAK_COOLDOWN_SEC = 24 * 3600   # ...and this much time before next tweak


def f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def usd(s):
    return ("$%+.2f" % s) if s is not None else "n/a"


MAX_UNPRICED_FRAC = 0.20  # tuner refuses to act if more than this share
                          # of closes cannot be priced in USD (fail closed)


def close_pnl_usd(c, fallback_usd=None):
    """Per-close realized P&L in USD, chain-aware.

    Prefers the journaled realized_usd (present on every BSC close).
    Legacy SOL-era closes predate USD stamping: price realized_sol at the
    stamped sol_usd, else the --sol-usd fallback. Returns None when the
    close cannot be priced -- callers must EXCLUDE it from stats, never
    count it as $0.
    """
    try:
        return float(c.get("realized_usd"))
    except (TypeError, ValueError):
        pass
    try:
        native = float(c.get("realized_sol"))
    except (TypeError, ValueError):
        return None
    try:
        price = float(c.get("sol_usd"))
    except (TypeError, ValueError):
        price = None
    price = price or fallback_usd
    if not price:
        return None
    return native * price


def priced_data_ok(n, n_unpriced):
    """Fail-closed coverage guard: the tuner needs a priced majority."""
    total = n + n_unpriced
    return not (n_unpriced > 0 and total > 0 and
                n_unpriced / total > MAX_UNPRICED_FRAC)


def enrich_closes(closes, entries, fallback_usd=None):
    """Join closes to entries and price each close in USD.

    Returns (enriched, n_unpriced). Closes that cannot be priced are
    excluded from `enriched` and counted in `n_unpriced`.
    `enriched[i]["realized"]` is USD. Every tuning rule is unit-free
    (signs, ratios, counts), so no rule threshold changes meaning.
    """
    entry_by_mint = {}
    for e in entries:
        mint = e.get("mint")
        if mint:
            entry_by_mint.setdefault(mint, []).append(e)
    for lst in entry_by_mint.values():
        lst.sort(key=lambda e: e.get("ts", ""))

    enriched = []
    n_unpriced = 0
    for c in closes:
        pnl = close_pnl_usd(c, fallback_usd)
        if pnl is None:
            n_unpriced += 1
            continue
        mint = c.get("mint")
        match = None
        cts = parse_ts(c.get("ts"))
        for e in reversed(entry_by_mint.get(mint, [])):
            if cts and parse_ts(e.get("ts")) and parse_ts(e.get("ts")) <= cts:
                match = e
                break
        hold_min = None
        if match:
            ets = parse_ts(match.get("ts"))
            if ets and cts:
                hold_min = (cts - ets).total_seconds() / 60.0
        enriched.append({
            "close": c,
            "realized": pnl,          # USD; every tuning rule is unit-free
            "realized_usd": pnl,
            "reason": classify_reason(c.get("reason")),
            "source": (match.get("source") if match else None) or "unknown",
            "sig_gain": (match.get("signal_gain_pct") if match else None),
            "hold_min": hold_min,
        })
    return enriched, n_unpriced


def load_journal(path):
    events = []
    if not os.path.exists(path):
        return events
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
    return events


def parse_ts(ts):
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def classify_reason(reason):
    r = (reason or "").lower()
    if "take profit" in r or "tp" in r:
        return "take-profit"
    if "hard stop" in r:
        return "hard-stop"
    if "trailing" in r:
        return "trailing-stop"
    return "other"


def gain_bucket(g):
    if g is None:
        return "unknown"
    if g < 150:
        return "<150%"
    if g <= 300:
        return "150-300%"
    return ">300%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="auto-apply the single proposed tweak (paper mode "
                         "only, with validation, backup, changelog, restart)")
    ap.add_argument("--sol-usd", type=float, default=None,
                    help="fallback SOL/USD for journal records that predate "
                         "USD stamping (stamped records use their own price)")
    args = ap.parse_args()
    fallback_usd = args.sol_usd

    events = load_journal(JOURNAL)
    entries = [e for e in events if e.get("type") == "entry"]
    closes = [e for e in events if e.get("type") == "close"]

    # P&L is USD and chain-aware (see enrich_closes); unpriced closes are
    # excluded from stats, never counted as $0.
    enriched, n_unpriced = enrich_closes(closes, entries, fallback_usd)

    n = len(enriched)
    wins = [c for c in enriched if c["realized"] > 0]
    losses = [c for c in enriched if c["realized"] <= 0]
    total = sum(c["realized"] for c in enriched)
    avg = total / n if n else 0.0
    gross_win = sum(c["realized"] for c in wins)
    gross_loss = abs(sum(c["realized"] for c in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    win_rate = len(wins) / n * 100 if n else 0.0
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = -gross_loss / len(losses) if losses else 0.0
    best = max(enriched, key=lambda c: c["realized"]) if enriched else None
    worst = min(enriched, key=lambda c: c["realized"]) if enriched else None

    # All enriched closes are priced in USD (unpriced ones were excluded
    # above), so the USD aggregates are the primary figures.
    usd_total = sum(c["realized"] for c in enriched) if enriched else None
    usd_avg = usd_total / n if (usd_total is not None and n) else None
    uw = [c["realized"] for c in wins]
    ul = [c["realized"] for c in losses]
    usd_avg_win = sum(uw) / len(uw) if uw else None
    usd_avg_loss = sum(ul) / len(ul) if ul else None
    priced_note = (f"{n_unpriced} unpriced close(s) excluded from stats"
                   if n_unpriced else "all closes priced in USD")

    # Exit-reason breakdown
    reasons = {}
    for c in enriched:
        r = c["reason"]
        d = reasons.setdefault(r, {"n": 0, "total": 0.0})
        d["n"] += 1
        d["total"] += c["realized"]

    # Source breakdown
    sources = {}
    for c in enriched:
        s = str(c["source"]).lower()
        d = sources.setdefault(s, {"n": 0, "wins": 0, "total": 0.0})
        d["n"] += 1
        d["total"] += c["realized"]
        if c["realized"] > 0:
            d["wins"] += 1

    # Signal-gain buckets
    buckets = {"<150%": [], "150-300%": [], ">300%": [], "unknown": []}
    for c in enriched:
        g = c["sig_gain"]
        g = f(g) if g is not None else None
        buckets[gain_bucket(g)].append(c)

    holds = [c["hold_min"] for c in enriched if c["hold_min"] is not None]
    avg_hold = sum(holds) / len(holds) if holds else None

    # Open positions from state
    open_pos, deployed = [], 0.0
    state_ok = True
    try:
        with open(STATE) as fh:
            st = json.load(fh)
        for mint, p in (st.get("positions") or {}).items():
            if isinstance(p, dict):
                open_pos.append({"mint": mint, "name": p.get("name", mint[:8]),
                                 "buy_sol": f(p.get("buy_sol"))})
                deployed += f(p.get("buy_sol"))
    except (OSError, json.JSONDecodeError):
        state_ok = False

    # Confidence bar
    bar = [
        ("30 closed trades", f"{n}/{CONF_CLOSED_TRADES}", n >= CONF_CLOSED_TRADES),
        ("profit factor > 1.2", f"{pf:.2f}" if pf != float("inf") else "inf",
         pf > CONF_PROFIT_FACTOR),
        ("total realized > 0 USD", usd(usd_total),
         usd_total is not None and usd_total > 0),
    ]

    # ---- improvement rule ----
    cfg = {}
    try:
        with open(CONFIG) as fh:
            cfg = json.load(fh)
    except (OSError, json.JSONDecodeError):
        pass
    proposal = None
    if n < MIN_TRADES_FOR_TUNING:
        proposal_text = (f"insufficient data (N={n}<{MIN_TRADES_FOR_TUNING})"
                         " - no tuning proposed")
    elif not priced_data_ok(n, n_unpriced):
        proposal_text = (f"insufficient priced data ({n_unpriced} of "
                         f"{n + n_unpriced} closes unpriced) - no tuning "
                         "proposed")
    else:
        proposal = propose_tuning(enriched, wins, losses, avg_win, avg_loss,
                                  reasons, buckets, sources, pf, cfg)
        proposal_text = (proposal["text"] if proposal
                         else "no tuning proposed - stats do not support a "
                              "conservative change")

    # ---- auto-apply (daily cron) ----
    action_text = "no action (--apply not set; proposal only)"
    if args.apply:
        tweaks = load_tweaks()
        if n < MIN_TRADES_FOR_TUNING:
            action_text = (f"no action - insufficient data "
                           f"(N={n}<{MIN_TRADES_FOR_TUNING})")
        elif not priced_data_ok(n, n_unpriced):
            action_text = (f"no action - insufficient priced data "
                           f"({n_unpriced} of {n + n_unpriced} closes unpriced)")
        else:
            revert_note = maybe_revert(cfg, enriched, tweaks)
            if revert_note:
                action_text = revert_note
            elif proposal is None:
                action_text = "no action - stats do not support a change"
            else:
                cooling, why = cooldown_active(tweaks, n)
                if cooling:
                    action_text = f"no action - cooling down ({why})"
                else:
                    action_text = apply_tweak(
                        cfg, proposal,
                        {"n": n, "pf": pf if pf != float("inf") else 999.0})

    # ---- console summary ----
    L = []
    L.append("FOMO paper-trader analysis")
    L.append(f"closed trades: {n} | wins: {len(wins)} | losses: {len(losses)} | "
             f"win rate: {win_rate:.1f}%")
    L.append(f"total realized: {usd(usd_total)} | "
             f"avg/trade: {usd(usd_avg)}")
    L.append(f"avg win: {usd(usd_avg_win)} | "
             f"avg loss: {usd(usd_avg_loss)} | "
             f"profit factor: {pf:.2f}" if pf != float("inf") else
             f"avg win: {usd(usd_avg_win)} | "
             f"avg loss: {usd(usd_avg_loss)} | "
             "profit factor: inf")
    if best:
        L.append(f"best: {best['close'].get('name')} {usd(best['realized'])} "
                 f"({best['reason']})")
        L.append(f"worst: {worst['close'].get('name')} {usd(worst['realized'])} "
                 f"({worst['reason']})")
    L.append(f"P&L in USD, chain-aware; {priced_note}")
    if avg_hold is not None:
        L.append(f"avg hold time: {avg_hold:.1f} min")
    L.append(f"open positions: {len(open_pos)} | deployed: {deployed:.4f} SOL "
             f"(mark-to-market unavailable - no live prices fetched)")
    L.append("confidence bar: " + "; ".join(
        f"[{'x' if ok else ' '}] {label} ({val})" for label, val, ok in bar))
    L.append("tuning: " + proposal_text)
    if args.apply:
        L.append("auto-tune: " + action_text)
    print("\n".join(L))

    # ---- dated report ----
    os.makedirs(ANALYSIS_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(ANALYSIS_DIR, f"{today}.md")
    R = []
    R.append(f"# FOMO paper-trader analysis - {today}")
    R.append("")
    R.append("## Overall")
    R.append(f"- closed trades: {n} (wins {len(wins)}, losses {len(losses)}, "
             f"win rate {win_rate:.1f}%)")
    R.append(f"- total realized: {usd(usd_total)}, "
             f"avg per trade {usd(usd_avg)}")
    R.append(f"- avg win {usd(usd_avg_win)}, "
             f"avg loss {usd(usd_avg_loss)}")
    R.append(f"- P&L in USD, chain-aware; {priced_note}")
    R.append(f"- profit factor: {pf:.2f}" if pf != float("inf") else "- profit factor: inf")
    if best:
        R.append(f"- best: {best['close'].get('name')} {usd(best['realized'])} "
                 f"({best['reason']})")
        R.append(f"- worst: {worst['close'].get('name')} {usd(worst['realized'])} "
                 f"({worst['reason']})")
    if avg_hold is not None:
        R.append(f"- avg hold time: {avg_hold:.1f} min "
                 f"({len(holds)}/{n} closes matched to entries)")
    R.append("")
    R.append("## Exit reasons")
    for rname, d in sorted(reasons.items()):
        a = d["total"] / d["n"] if d["n"] else 0.0
        R.append(f"- {rname}: {d['n']} trades, total {usd(d['total'])}, "
                 f"avg {usd(a)}")
    if not reasons:
        R.append("- none (no closed trades)")
    R.append("")
    R.append("## By signal source")
    for sname, d in sorted(sources.items()):
        wr = d["wins"] / d["n"] * 100 if d["n"] else 0.0
        R.append(f"- {sname}: {d['n']} trades, win rate {wr:.1f}%, "
                 f"total {usd(d['total'])}")
    if not sources:
        R.append("- none")
    R.append("")
    R.append("## By signal_gain_pct bucket")
    for bname, bl in buckets.items():
        if not bl:
            continue
        bt = sum(c["realized"] for c in bl)
        bw = sum(1 for c in bl if c["realized"] > 0)
        R.append(f"- {bname}: {len(bl)} trades, win rate {bw/len(bl)*100:.1f}%, "
                 f"total {usd(bt)}")
    R.append("")
    R.append("## Open positions")
    if not state_ok:
        R.append("- state.json unreadable")
    elif not open_pos:
        R.append("- none")
    else:
        R.append(f"- {len(open_pos)} open, {deployed:.4f} SOL deployed")
        for p in open_pos:
            R.append(f"  - {p['name']} ({p['mint'][:8]}...), {p['buy_sol']:.4f} SOL")
        R.append("- mark-to-market unavailable (no live prices fetched)")
    R.append("")
    R.append("## Confidence bar")
    for label, val, ok in bar:
        R.append(f"- [{'x' if ok else ' '}] {label}: {val}")
    R.append("")
    R.append("## Tuning proposal")
    R.append(f"- {proposal_text}")
    R.append("")
    R.append("## Auto-tune action")
    R.append(f"- {action_text if args.apply else 'not run (proposal only)'}")
    R.append("")
    with open(path, "w") as fh:
        fh.write("\n".join(R))
    print(f"report written: {path}")


def propose_tuning(enriched, wins, losses, avg_win, avg_loss,
                   reasons, buckets, sources, pf, cfg):
    """At most ONE conservative change as a machine-readable tweak dict.

    All stats are USD-denominated (see enrich_closes); every rule below is
    unit-free (signs, ratios, counts), so no threshold changes meaning.

    Returns None, {"skip": reason}, or
    {"path": dotted.config.path, "new": value, "text": human rationale}.
    The "new" value is computed relative to the CURRENT config so a stale
    proposal can never be applied blindly (apply re-validates anyway).
    """
    stats = lambda name: (reasons.get(name) or {"n": 0, "total": 0.0})

    def cur(path, default=None):
        node = cfg
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # 1) Hard stops bleeding: most losses come from hard stops with no TP.
    hs = stats("hard-stop")
    if hs["n"] >= 3 and hs["total"] < 0:
        ladder = cur("exit.take_profits") or []
        if ladder and ladder[0][0] > 60:
            new_gain = max(60, ladder[0][0] - 20)
            return {"path": "exit.take_profits", "new": [new_gain, ladder[0][1]],
                    "text": ("lower exit.take_profits rung 1 %s -> %s "
                             f"(hard-stop closes: {hs['n']}, total {usd(hs['total'])} - "
                             "coins fade before the first rung; banking half earlier preserves capital)")
                    % (ladder[0], [new_gain, ladder[0][1]])}

    # 2) Trailing stops giving back too much: losses >> wins in size.
    ts = stats("trailing-stop")
    if avg_loss < 0 and avg_win > 0 and abs(avg_loss) > 1.5 * avg_win and ts["n"] >= 3:
        old = cur("exit.trailing_stop_pct", 30)
        if old > 20:
            new = old - 5
            return {"path": "exit.trailing_stop_pct", "new": new,
                    "text": (f"tighten exit.trailing_stop_pct {old} -> {new} "
                             f"(avg loss {usd(avg_loss)} vs avg win {usd(avg_win)}: "
                             "trailing exits give back too much from peak)")}

    # 3) Low-conviction signals losing: the <150% bucket has negative expectancy.
    low = buckets.get("<150%", [])
    if len(low) >= 3:
        lt = sum(c["realized"] for c in low)
        if lt < 0:
            old = cur("hunter.min_m15_gain_pct", 10)
            if old < 40:
                new = min(50, old + 15)
                return {"path": "hunter.min_m15_gain_pct", "new": new,
                        "text": (f"raise hunter.min_m15_gain_pct {old} -> {new} "
                                 f"(<150% signal bucket: {len(low)} trades, {usd(lt)} - "
                                 "weak signals have negative expectancy)")}

    # 4) A signal source is systematically worse.
    if len(sources) >= 2:
        ranked = sorted(sources.items(),
                        key=lambda kv: kv[1]["total"] / kv[1]["n"] if kv[1]["n"] else 0)
        worst_name, wd = ranked[0]
        if wd["n"] >= 3 and wd["total"] < 0:
            if worst_name == "dexscreener":
                old = cur("hunter.dexscreener.min_m5_gain_pct", 5)
                if old < 25:
                    new = min(30, old + 10)
                    return {"path": "hunter.dexscreener.min_m5_gain_pct",
                            "new": new,
                            "text": (f"raise hunter.dexscreener.min_m5_gain_pct {old} -> {new} "
                                     f"(dexscreener-sourced: {wd['n']} trades, {usd(wd['total'])} - "
                                     "its weaker 5m signals underperform GT)")}
            old = cur("hunter.min_buy_sell_ratio", 1.5)
            if old < 3.0:
                new = round(min(3.0, old + 0.5), 2)
                return {"path": "hunter.min_buy_sell_ratio", "new": new,
                        "text": (f"raise hunter.min_buy_sell_ratio {old} -> {new} "
                                 f"({worst_name}-sourced: {wd['n']} trades, {usd(wd['total'])} - "
                                 "tighten buy pressure requirement)")}

    # 5) Generally losing: shrink risk per trade (never raise it).
    total = sum(c["realized"] for c in enriched)
    if total < 0 and pf < 1.0:
        old = cur("risk.buy_sol_per_trade", 0.087)
        if old > 0.02:
            new = round(max(0.02, old * 0.6), 4)
            return {"path": "risk.buy_sol_per_trade", "new": new,
                    "text": (f"reduce risk.buy_sol_per_trade {old} -> {new} "
                             f"(total {usd(total)}, PF {pf:.2f} - shrink exposure while "
                             "the edge is unproven)")}

    return None


# ---------------------------------------------------------------------------
# Auto-apply machinery (--apply)
# ---------------------------------------------------------------------------

# Conservative allowlist: path -> (min, max, direction) where direction is
# "down" (may only decrease), "up" (may only increase), or "ladder" for the
# special TP-rung-1 case. Anything not listed here is rejected outright.
ALLOWLIST = {
    "exit.take_profits": ("ladder",),
    "exit.trailing_stop_pct": (15, 45, "down"),
    "hunter.min_m15_gain_pct": (10, 50, "up"),
    "hunter.dexscreener.min_m5_gain_pct": (5, 30, "up"),
    "hunter.min_buy_sell_ratio": (1.0, 3.0, "up"),
    "risk.buy_sol_per_trade": (0.02, None, "down"),
}


def get_path(cfg, path):
    node = cfg
    for part in path.split("."):
        node = node[part]
    return node


def set_path(cfg, path, value):
    parts = path.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def validate_tweak(cfg, prop):
    """Return (ok, old_value, error). Re-validates against the live config."""
    path, new = prop["path"], prop["new"]
    if path not in ALLOWLIST:
        return False, None, "not on conservative allowlist"
    try:
        old = get_path(cfg, path)
    except (KeyError, TypeError):
        return False, None, "path missing from config"
    rule = ALLOWLIST[path]
    if rule[0] == "ladder":
        # new = [new_gain, sell_pct] for rung 1; only lowering the gain
        # threshold is allowed, never below 60, never touching rung 2+.
        if (not isinstance(old, list) or not old or
                not isinstance(new, list) or len(new) != 2):
            return False, None, "malformed ladder tweak"
        old_gain = old[0][0]
        if not (60 <= new[0] < old_gain):
            return False, None, "rung-1 gain must move down within [60, old)"
        if new[1] != old[0][1]:
            return False, None, "rung-1 sell pct must not change"
        return True, [list(r) for r in old], None
    lo, hi, direction = rule
    if not isinstance(new, (int, float)):
        return False, None, "non-numeric value"
    if lo is not None and new < lo:
        return False, None, "below minimum bound"
    if hi is not None and new > hi:
        return False, None, "above maximum bound"
    if direction == "down" and not (new < old):
        return False, None, "must strictly decrease"
    if direction == "up" and not (new > old):
        return False, None, "must strictly increase"
    return True, old, None


def load_tweaks():
    out = []
    if os.path.exists(TWEAKS_LOG):
        with open(TWEAKS_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def log_tweak(entry):
    os.makedirs(ANALYSIS_DIR, exist_ok=True)
    with open(TWEAKS_LOG, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def changelog_add(lines):
    with open(CHANGELOG, "a") as fh:
        fh.write("\n" + "\n".join(lines) + "\n")


def restart_bot():
    """Graceful SIGTERM then relaunch. Respects halt.flag (no restart then)."""
    if os.path.exists(os.path.join(RUNDIR, "halt.flag")):
        return "halt.flag present - config updated but bot NOT restarted (kill switch)"
    pidfile = os.path.join(RUNDIR, "bot.pid")
    try:
        with open(pidfile) as fh:
            pid = int(fh.read().strip())
        os.kill(pid, signal.SIGTERM)
        for _ in range(40):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except OSError:
                break
        else:
            os.kill(pid, signal.SIGKILL)
            time.sleep(2)
    except (OSError, ValueError):
        pass  # wasn't running; watchdog/daily check will start it
    log_path = os.path.join(RUNDIR, "bot.log")
    with open(log_path, "ab") as lf:
        subprocess.Popen(
            [os.path.join(BASE, ".venv", "bin", "python"),
             os.path.join(BASE, "fomo_trader.py"),
             "--config", CONFIG],
            stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=RUNDIR)
    time.sleep(10)
    try:
        with open(pidfile) as fh:
            new_pid = int(fh.read().strip())
        os.kill(new_pid, 0)
        return f"bot restarted (pid {new_pid}) with new config"
    except (OSError, ValueError):
        return "RESTART FAILED - check bot.log; watchdog will retry"


def apply_tweak(cfg, prop, stats_ctx):
    """Validate, back up, write, log, changelog, restart. Returns action str."""
    if not cfg.get("dry_run"):
        return "SKIPPED: dry_run is not true - auto-tune refuses live config"
    ok, old, err = validate_tweak(cfg, prop)
    if not ok:
        return f"SKIPPED: tweak failed validation ({err})"
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(CONFIG, os.path.join(BACKUP_DIR, f"config.{stamp}.json"))
    if prop["path"] == "exit.take_profits":
        ladder = [list(r) for r in cfg["exit"]["take_profits"]]
        ladder[0] = [prop["new"][0], prop["new"][1]]
        cfg["exit"]["take_profits"] = ladder
    else:
        set_path(cfg, prop["path"], prop["new"])
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, CONFIG)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_tweak({"ts": now, "action": "apply", "path": prop["path"],
               "old": old, "new": prop["new"], "rationale": prop["text"],
               "n_trades": stats_ctx["n"], "pf": stats_ctx["pf"],
               "reverted": False})
    changelog_add([
        f"## auto-tune — {now}",
        f"- Applied `{prop['path']}`: {old} -> {prop['new']}",
        f"- Rationale: {prop['text']}",
        f"- Context: {stats_ctx['n']} closed trades, PF {stats_ctx['pf']:.2f}. "
        "Auto-applied by the daily self-improvement loop (paper mode only).",
    ])
    restart_note = restart_bot()
    return f"APPLIED {prop['path']}: {old} -> {prop['new']}. {restart_note}."


def maybe_revert(cfg, enriched, tweaks):
    """Revert the last tweak if trades since it are meaningfully worse.

    Compares avg realized/trade before vs after the tweak (min 3 closes
    each side). Returns an action string, or None if no revert warranted.
    """
    reverted_ts = {t.get("reverts") for t in tweaks
                   if t.get("action") == "revert"}
    cands = [t for t in tweaks if t.get("action") == "apply"
             and t.get("ts") not in reverted_ts]
    if not cands:
        return None
    last = cands[-1]
    tts = parse_ts(last.get("ts"))
    if not tts:
        return None
    before = [c["realized"] for c in enriched
              if parse_ts(c["close"].get("ts")) and
              parse_ts(c["close"].get("ts")) < tts]
    after = [c["realized"] for c in enriched
             if parse_ts(c["close"].get("ts")) and
             parse_ts(c["close"].get("ts")) >= tts]
    if len(before) < 3 or len(after) < MIN_NEW_TRADES_AFTER_TWEAK:
        return None
    avg_b = sum(before) / len(before)
    avg_a = sum(after) / len(after)
    if avg_b <= 0:
        return None  # it was already losing; nothing to revert on
    if avg_a >= 0.5 * avg_b:
        return None  # not meaningfully worse
    # Revert: restore the old value.
    if not cfg.get("dry_run"):
        return "SKIPPED revert: dry_run is not true - refusing live config"
    path, old = last["path"], last["old"]
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(CONFIG, os.path.join(BACKUP_DIR, f"config.{stamp}.json"))
    if path == "exit.take_profits":
        cfg["exit"]["take_profits"] = [list(r) for r in old]
    else:
        set_path(cfg, path, old)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, CONFIG)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_tweak({"ts": now, "action": "revert", "path": path,
               "old": last["new"], "new": old,
               "rationale": (f"regression: avg/trade after tweak {usd(avg_a)} "
                             f"vs {usd(avg_b)} before ({len(after)} closes)"),
               "n_trades": len(enriched),
               "reverts": last.get("ts")})
    changelog_add([
        f"## auto-tune revert — {now}",
        f"- Reverted `{path}`: {last['new']} -> {old}",
        f"- Reason: avg realized/trade fell to {usd(avg_a)} from {usd(avg_b)} "
        f"over {len(after)} closes after the {last.get('ts')} tweak.",
    ])
    restart_note = restart_bot()
    return (f"REVERTED {path}: {last['new']} -> {old} (tweak made things "
            f"worse). {restart_note}.")


def cooldown_active(tweaks, n):
    """True if a tweak was applied recently or too few new trades exist."""
    active = [t for t in tweaks if t.get("action") in ("apply", "revert")]
    if not active:
        return False, ""
    last = active[-1]
    lts = parse_ts(last.get("ts"))
    if lts and (datetime.now() - lts).total_seconds() < TWEAK_COOLDOWN_SEC:
        return True, f"last tweak {last.get('ts')} <24h ago"
    if n - int(last.get("n_trades", 0)) < MIN_NEW_TRADES_AFTER_TWEAK:
        return True, (f"only {n - int(last.get('n_trades', 0))} new closes "
                      f"since last tweak (need {MIN_NEW_TRADES_AFTER_TWEAK})")
    return False, ""


if __name__ == "__main__":
    main()
