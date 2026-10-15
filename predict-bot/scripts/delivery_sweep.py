#!/usr/bin/env python3
"""Delivery sweep for the Profit Kalshi Bot: backup delivery path for alerts.

The main scan worker is slow (140s+ per scan), so its chat handoff can arrive
7-9 minutes late -- by which time fresh signals are stale and get suppressed,
which looks like "nothing scanning". This script is FAST (no Kalshi calls):
a 2-minute cron runs it, it diffs data/same_day_loop_state.json against
data/delivery_ledger.json, emits pre-formatted, pre-checked alerts as JSON,
and marks emitted items delivered in the ledger (file-locked).

Suppression rules (mirror the standing delivery preferences):
- entries: skip if already delivered, if exit_ts is set, or if the window
  closes in under 2 minutes / already closed.
- exits: skip if already delivered, if the entry never reached chat (orphan),
  or if the exit is older than 30 minutes.
- exit framing always compares taken-side now vs entry first:
  favorable -> "exit to bank the favorable move.",
  adverse -> "exit to cut the loss.", flat -> "exit flat."

Paper/analysis only. Never places orders. Read-only w.r.t. the scan state;
only the ledger is written.
"""
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
STATE = os.path.join(DATA, "same_day_loop_state.json")
LEDGER = os.path.join(DATA, "delivery_ledger.json")
GRADES = os.path.join(DATA, "signal_grades.jsonl")
EXITS = os.path.join(DATA, "exit_events.jsonl")
OUTBOX = os.path.join(DATA, "alert_outbox.jsonl")

ENTRY_MIN_SECS_LEFT = 120
EXIT_MAX_AGE_SECS = 1800

DISCLAIMER = (
    "Model-estimated edges on Kalshi 15-min markets (paper only), not guarantees \u2014 "
    "the model is a Kraken-spot zero-drift Gaussian/Brownian probability model with "
    "validated mid-range shrinkage and can be wrong on news-driven moves."
)


def coin_from_ticker(t):
    m = re.match(r"KX([A-Z]+)15M-", t or "")
    return m.group(1) if m else None


def title_for(ticker):
    coin = coin_from_ticker(ticker)
    return f"{coin} price up in next 15 mins?" if coin else (ticker or "Kalshi 15-min market")


def fmt_cts(x):
    return f"{x * 100:.1f}\u00a2"


def taken_price(side, yes_mid):
    return yes_mid if side == "YES" else 1 - yes_mid


def close_et_label(wc):
    # VM local timezone is UTC; windows are America/New_York.
    from datetime import timezone
    try:
        from zoneinfo import ZoneInfo
        et = datetime.fromtimestamp(wc, tz=ZoneInfo("America/New_York"))
    except Exception:
        et = datetime.fromtimestamp(wc, tz=timezone.utc)
    return et.strftime("%I:%M %p ET").lstrip("0")


def rolling_accuracy():
    try:
        rows = [json.loads(l) for l in open(GRADES) if l.strip()]
    except FileNotFoundError:
        return None
    auth = [g for g in rows
            if g.get("proxy") == "kalshi" and not g.get("excluded_from_accuracy")]
    scored = [g for g in auth if g.get("correct") is not None]
    recent = scored[-50:]
    if not recent:
        return None
    wins = sum(1 for g in recent if g["correct"])
    return wins, len(recent)

def rolling_pnl():
    """Honest rolling P&L over the same last-50 Kalshi-settled population as
    rolling_accuracy(): executable bid/ask prices, net of Kalshi taker fees,
    as a fraction of staked premium. Returns None when unavailable."""
    try:
        from quote_side_analysis import paper_fill
    except Exception:
        return None
    try:
        rows = [json.loads(l) for l in open(GRADES) if l.strip()]
    except FileNotFoundError:
        return None
    auth = [g for g in rows
            if g.get("proxy") == "kalshi" and not g.get("excluded_from_accuracy")]
    scored = [g for g in auth if g.get("correct") is not None]
    recent = scored[-50:]
    if not recent:
        return None
    try:
        exits = {}
        for line in open(EXITS):
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("signal_key"):
                exits[e["signal_key"]] = e
    except FileNotFoundError:
        exits = {}
    pnl = stake = 0.0
    for g in recent:
        try:
            fill = paper_fill(g, exits.get(g.get("key")))
        except Exception:
            continue
        if not fill or fill["executable_pnl_net"] is None \
                or fill["entry_executable_taken"] is None:
            continue
        pnl += fill["executable_pnl_net"]
        stake += fill["entry_executable_taken"]
    if stake <= 0:
        return None
    return pnl / stake


def parse_ts(ts):
    return datetime.fromisoformat(ts).timestamp()


def format_entry(key, e, now):
    ticker = e.get("ticker") or key.split(":")[0]
    side = e["side"]
    m = e["market_mid"]
    px_taken = taken_price(side, m)
    fair = e.get("fair_calibrated_taken")
    win_prob = fair if isinstance(fair, (int, float)) and 0 < fair < 1 else None
    edge = e.get("edge")
    edge_pts = abs(edge) * 100 if isinstance(edge, (int, float)) else None
    spread = e.get("spread")
    wc = e["window_close_utc"]
    mins_left = (wc - now) / 60
    win_txt = f"{win_prob:.0%}" if win_prob is not None else "n/a"
    edge_txt = f"+{edge_pts:.1f} pts" if edge_pts is not None else "n/a"
    spread_txt = f"{spread * 100:.1f}\u00a2" if isinstance(spread, (int, float)) else "n/a"
    fair_txt = f"{fair * 100:.1f}\u00a2" if isinstance(fair, (int, float)) else "n/a"
    acc = rolling_accuracy()
    acc_line = (f"Model running {acc[0]}/{acc[1]} ({acc[0] / acc[1]:.0%}) "
                f"on last 50 graded signals (Kalshi-settled only)."
                if acc else "No graded signals yet.")
    pnl = rolling_pnl()
    pnl_line = (f" Rolling P\u0026L {pnl:+.1%} on the same signals "
                f"(executable bid/ask, net of taker fees)."
                if pnl is not None else "")
    return (
        f"\U0001f4ca New signal \u2014 model-estimated {win_txt} win chance\n\n"
        f"{title_for(ticker)} \u2014 take {side}. Market has {side} at {fmt_cts(px_taken)}, "
        f"model fair value {fair_txt}, edge {edge_txt}. Spread {spread_txt}. "
        f"About {mins_left:.0f} min left in the window (closes {close_et_label(wc)}).\n\n"
        f"{DISCLAIMER}\n{acc_line}{pnl_line}"
    )


def format_exit(key, e, now):
    ticker = e.get("ticker") or key.split(":")[0]
    side = e["side"]
    entry_taken = taken_price(side, e["market_mid"])
    now_taken = taken_price(side, e["exit_mid_now"])
    pnl = (now_taken - entry_taken) * 100
    if pnl >= 1:
        move, advice = f"up about {pnl:.0f}\u00a2", "exit to bank the favorable move."
    elif pnl <= -1:
        move, advice = f"down about {abs(pnl):.0f}\u00a2", "exit to cut the loss."
    else:
        move, advice = "about flat", "exit flat."
    reason = e.get("exit_reason")
    why = {"edge_gone": "The mispricing closed \u2014 ",
           "edge_reversed": "The edge flipped \u2014 "}.get(reason, "")
    wc = e.get("window_close_utc")
    if wc and wc > now:
        when = (f"Window closes {close_et_label(wc)}, "
                f"about {(wc - now) / 60:.0f} minutes left.")
    else:
        when = "That window has now closed."
    edge = e.get("edge")
    edge_txt = f"+{abs(edge) * 100:.1f}" if isinstance(edge, (int, float)) else "n/a"
    return (
        f"\U0001f6a8 GET OUT NOW \u2014 {title_for(ticker)} \u2014 {side}\n\n"
        f"If you took {side} at {fmt_cts(entry_taken)} ({edge_txt} edge), "
        f"it's now {fmt_cts(now_taken)} \u2014 {move}. "
        f"{why}{advice} {when} Paper only, no orders."
    )


def main():
    now = time.time()
    with open(STATE) as f:
        state = json.load(f)
    alerted = state.get("alerted", {}) or {}

    fd = os.open(LEDGER, os.O_RDWR | os.O_CREAT, 0o644)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            ledger = json.load(f)
        except (json.JSONDecodeError, ValueError):
            ledger = {}
        alerts = []

        entry_cands = []
        for key, e in alerted.items():
            if not isinstance(e, dict):
                continue
            lg = ledger.get(key, {})
            if lg.get("entry_delivered_ts"):
                continue
            if e.get("exit_ts"):
                continue
            wc = e.get("window_close_utc")
            if not wc or wc - now < ENTRY_MIN_SECS_LEFT:
                continue
            side = e.get("side")
            m = e.get("market_mid")
            if side not in ("YES", "NO") or not isinstance(m, (int, float)):
                continue
            fair = e.get("fair_calibrated_taken")
            win_prob = fair if isinstance(fair, (int, float)) and 0 < fair < 1 else -1
            entry_cands.append((-win_prob, key, e))
        entry_cands.sort(key=lambda t: t[0])
        for _, key, e in entry_cands:
            alerts.append({"kind": "entry", "key": key,
                           "text": format_entry(key, e, now)})
            ledger.setdefault(key, {})["entry_delivered_ts"] = now

        for key, e in alerted.items():
            if not isinstance(e, dict):
                continue
            lg = ledger.get(key, {})
            if lg.get("exit_delivered_ts"):
                continue
            exit_ts = e.get("exit_ts")
            if not exit_ts:
                continue
            if not lg.get("entry_delivered_ts"):
                continue  # orphan exit: entry never reached chat; stay silent
            try:
                exit_age = now - parse_ts(exit_ts)
            except (ValueError, TypeError):
                continue
            if exit_age > EXIT_MAX_AGE_SECS:
                continue
            side = e.get("side")
            m = e.get("market_mid")
            exit_mid = e.get("exit_mid_now")
            if (side not in ("YES", "NO")
                    or not isinstance(m, (int, float))
                    or not isinstance(exit_mid, (int, float))):
                continue
            alerts.append({"kind": "exit", "key": key,
                           "text": format_exit(key, e, now)})
            ledger.setdefault(key, {})["exit_delivered_ts"] = now

        f.seek(0)
        f.truncate()
        json.dump(ledger, f, indent=1)

    # Durable outbox: verbatim alert texts, so a worker that summarizes
    # instead of relaying verbatim never loses the actual content.
    if alerts:
        with open(OUTBOX, "a") as ob:
            for a in alerts:
                ob.write(json.dumps({"ts": now, "kind": a["kind"],
                                     "key": a["key"], "text": a["text"]}) + "\n")

    sys.stdout.write(json.dumps({"ok": True, "alerts": alerts,
                                 "count": len(alerts)}, indent=1) + "\n")


if __name__ == "__main__":
    main()
