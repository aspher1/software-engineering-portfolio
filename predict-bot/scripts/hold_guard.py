#!/usr/bin/env python3
"""Hold-guard: rapid-exit alerts for OPEN Profit Kalshi Bot positions.

Randy's request: when a signal was given and the position is still held,
he wants an IMMEDIATE "get out" ping the moment the taken-side price starts
collapsing or the crowd is dumping his side at a crazy rate — before the
2-minute exit watcher's edge model would fire.

How it works:
  - Reads ~/workspace/predict-bot/data/same_day_loop_state.json READ-ONLY
    to find signals that are open and held: dict entries with a side of
    YES/NO, no exit_ts, and a window still open.
  - Polls the public Kalshi markets API for the 9 coins' live 15-min windows
    (same sweep as flip_watch.py) and keeps a short TAKEN-SIDE price
    history per open signal (taken price = YES mid for YES takes,
    1 - YES mid for NO takes).
  - Fires when the taken-side price collapses fast:
      * big    : drop >= HG_BIG_DROP     (10c) within HG_BIG_WINDOW_S     (60s), or
      * fast   : drop >= HG_FAST_DROP    (7c)  within HG_FAST_WINDOW_S    (30s), or
      * instant: drop >= HG_INSTANT_DROP (5c)  within HG_INSTANT_WINDOW_S (15s)
    Both tiers measure the ADVERSE move (crowd rushing toward the opposite
    side of the held position). Favourable runs never fire.
  - Dedupes in its own state file: at most one alert per open position;
    a second alert only re-arms after the taken price falls another
    HG_REARM_DROP (10c) below the price at the first alert. 5-min per-signal
    cooldown, and NEVER fires after exit_ts is stamped or the window closed.

Additive only: does NOT change entry/exit logic, thresholds, agreement
filter, or grading. Paper/analysis only.

Output: one JSON object on stdout:
  {"ok": true, "as_of_utc": ..., "open": N, "evaluated": M, "alerts": [...]}
  {"ok": false, "error": "..."}
State: ~/hooks/state/kalshi-hold-guard.json (per-signal price history +
per-signal alert memory). Scanner state is never written.
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import requests
from kalshi_client import kalshi_get, stats as kalshi_request_stats

COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "ZEC", "NEAR", "BNB"]
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

SCANNER_STATE = os.path.expanduser(
    "~/workspace/predict-bot/data/same_day_loop_state.json")
STATE_FILE = os.path.expanduser("~/hooks/state/kalshi-hold-guard.json")

# --- trigger tunables -------------------------------------------------------
HG_BIG_DROP = 0.10      # 10c adverse drop within the big window -> fire
HG_BIG_WINDOW_S = 60
HG_FAST_DROP = 0.07     # 7c adverse drop within the fast window -> fire
HG_FAST_WINDOW_S = 30
HG_INSTANT_DROP = 0.05  # 5c adverse drop within the instant window -> fire fast
HG_INSTANT_WINDOW_S = 15
HG_REARM_DROP = 0.10    # re-alert only after another 10c down from last alert
HG_COOLDOWN_S = 300     # per-signal alert cooldown, seconds
HG_MIN_SPAN_S = 5       # need this much history before alerting (thin-book guard)
HIST_KEEP_S = 120       # price history retained, seconds
HG_MIN_LEFT = 1.0       # don't bother with < 1 min left in the window


def num(x):
    try:
        f = float(x)
        return f if math.isfinite(f) else 0.0
    except (TypeError, ValueError):
        return 0.0


def taken_price(side, yes_mid):
    """Taken-side mid: YES mid or 1-YES mid for NO.

    Entry and live prices both use mids, so these are paper comparisons.
    At quotes, YES entry/exit use ask/bid; NO use 1-YES bid/ask.
    """
    return yes_mid if side == "YES" else 1.0 - yes_mid


def entry_taken(side, market_mid, edge):
    """(entry_price_taken, entry_edge_taken) from scanner-state YES-oriented fields."""
    if market_mid is None:
        return None, None
    if side == "YES":
        return market_mid, edge
    return 1.0 - market_mid, (-edge if edge is not None else None)


def open_signals(alerted, now_ts):
    """Signals that are open and held: dict values, side YES/NO, no exit_ts,
    window still open with >= HG_MIN_LEFT minutes left. Returns a list of
    dicts keyed by the state key (ticker:SIDE)."""
    out = []
    for key, v in (alerted or {}).items():
        if not isinstance(v, dict) or v.get("side") not in ("YES", "NO"):
            continue
        if v.get("exit_ts"):
            continue
        wc = v.get("window_close_utc")
        if not wc or wc <= now_ts:
            continue
        if (wc - now_ts) / 60.0 < HG_MIN_LEFT:
            continue
        ticker = v.get("ticker")
        if not ticker:
            continue
        ep, ee = entry_taken(v["side"], v.get("market_mid"), v.get("edge"))
        out.append({
            "signal_key": key, "ticker": ticker, "side": v["side"],
            "window_close_utc": wc, "entry_ts": v.get("ts"),
            "entry_price_taken": ep, "entry_edge_taken": ee,
        })
    return out


def decide(hist, now_ts):
    """Pure trigger logic.

    hist: [[ts, taken_price], ...] (chronological) for one open signal.
    Returns (drop, span_s, tier) when the taken-side price collapsed fast
    (big tier first), else None. Only adverse (falling) moves fire.
    """
    win_big = [[t, p] for t, p in hist if now_ts - t <= HG_BIG_WINDOW_S]
    win_fast = [[t, p] for t, p in hist if now_ts - t <= HG_FAST_WINDOW_S]
    win_instant = [[t, p] for t, p in hist if now_ts - t <= HG_INSTANT_WINDOW_S]
    for win, thresh, tier in ((win_big, HG_BIG_DROP, "big"),
                              (win_fast, HG_FAST_DROP, "fast"),
                              (win_instant, HG_INSTANT_DROP, "instant")):
        if len(win) >= 2 and (win[-1][0] - win[0][0]) >= HG_MIN_SPAN_S:
            peak = max(p for _, p in win)
            drop = peak - win[-1][1]
            if drop >= thresh:
                return round(drop, 3), round(win[-1][0] - win[0][0], 1), tier
    return None


def may_fire(ent, now_ts, now_price):
    """Dedupe/rearm/cooldown gate. ent is the per-signal memory dict.

    Returns True if an alert may fire now, else False. First alert always
    allowed; afterwards only after a further HG_REARM_DROP collapse and past
    the cooldown.
    """
    last_ts = ent.get("last_alert_ts", 0) or 0
    last_px = ent.get("last_alert_price")
    if not last_ts:
        return True
    if now_ts - last_ts < HG_COOLDOWN_S:
        return False
    if last_px is None:
        return True
    return (last_px - now_price) >= HG_REARM_DROP - 1e-9


def pullback_fields(entry_price_taken, now_price_taken, peak_to_now_drop):
    """Keep recent-peak decline separate from entry-to-now paper P&L."""
    pnl = (round(now_price_taken - entry_price_taken, 3)
           if entry_price_taken is not None else None)
    if pnl is not None and pnl > 0:
        wording = "protect paper profit"
    elif pnl is not None and pnl < 0:
        wording = "cut the paper loss"
    else:
        wording = "adverse move"
    return {"peak_to_now_drop": peak_to_now_drop,
            "entry_to_now_pnl": pnl, "alert_wording": wording}


def load_state(path):
    try:
        with open(path) as f:
            s = json.load(f)
            return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, s):
    if os.environ.get("HATCH_HOOK_DRY_RUN") == "1":
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, path)


def fetch_live(now_ts):
    """Live 15-min windows for the 9 coins (same sweep as flip_watch.py)."""
    # Same bounding as flip_watch.py: windows closing within the next 30 min
    # (the 9 coins' live 15-min windows). Bound the sweep to avoid pulling
    # ~11k same-day markets every poll.
    mkts, cursor = [], None
    for _ in range(10):
        params = {"limit": 1000, "status": "open",
                  "min_close_ts": int(now_ts) - 120,
                  "max_close_ts": int(now_ts) + 1800}
        if cursor:
            params["cursor"] = cursor
        r = kalshi_get(f"{KALSHI_API}/markets", params=params, timeout=20)
        r.raise_for_status()
        d = r.json()
        mkts.extend(d.get("markets", []))
        cursor = d.get("cursor")
        if not cursor:
            break
    live = {}
    for m in mkts:
        tk = m.get("ticker") or ""
        if "15M" not in tk:
            continue
        sym = tk.split("15M")[0].replace("KX", "")
        if sym not in COINS:
            continue
        try:
            wo = datetime.fromisoformat(
                m["open_time"].replace("Z", "+00:00")).timestamp()
            wc = datetime.fromisoformat(
                m["close_time"].replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if not (wo <= now_ts < wc):
            continue
        b = num(m.get("yes_bid_dollars"))
        a = num(m.get("yes_ask_dollars"))
        mid = (b + a) / 2 if (b > 0 and a > b) else num(
            m.get("last_price_dollars"))
        if not (0.01 <= mid <= 0.99):
            continue
        live[tk] = {"ticker": tk, "title": m.get("title"), "sym": sym,
                    "mid": mid, "close_time": m.get("close_time"),
                    "mins_left": round((wc - now_ts) / 60, 1)}
    return live


def main():
    now_ts = time.time()
    scanner = load_state(SCANNER_STATE)
    opens = open_signals(scanner.get("alerted"), now_ts)
    if not opens:
        save_state(STATE_FILE, {"signals": {}, "updated_utc":
                                datetime.now(timezone.utc).isoformat()})
        print(json.dumps({"kalshi_requests": dict(kalshi_request_stats), "ok": True,
                          "as_of_utc": datetime.now(timezone.utc).isoformat(),
                          "open": 0, "evaluated": 0, "alerts": []}))
        return
    try:
        live = fetch_live(now_ts)
    except Exception as e:
        print(json.dumps({"kalshi_requests": dict(kalshi_request_stats), "ok": False,
                          "error": "markets fetch failed: %s" % e}))
        return

    st = load_state(STATE_FILE)
    mems = st.get("signals", {})
    alerts = []
    for sig in opens:
        key = sig["signal_key"]
        ent = mems.get(key, {"hist": [], "last_alert_ts": 0,
                             "last_alert_price": None})
        hist = [[t, p] for t, p in ent.get("hist", [])
                if now_ts - t <= HIST_KEEP_S]
        L = live.get(sig["ticker"])
        if not L:
            ent["hist"] = hist  # keep aging; ticker may reappear next poll
            mems[key] = ent
            continue
        px = taken_price(sig["side"], L["mid"])
        hist.append([now_ts, px])
        ent["hist"] = hist
        hit = decide(hist, now_ts)
        if hit and may_fire(ent, now_ts, px):
            drop, span, tier = hit
            ent["last_alert_ts"] = now_ts
            ent["last_alert_price"] = px
            ep = sig["entry_price_taken"]
            ee = sig["entry_edge_taken"]
            alerts.append({
                "signal_key": key, "ticker": sig["ticker"],
                "title": L["title"], "coin": L["sym"], "side": sig["side"],
                "tier": tier, "entry_ts": sig["entry_ts"],
                "entry_price_taken": (round(ep, 3)
                                      if ep is not None else None),
                "entry_edge_taken": (round(ee, 3)
                                     if ee is not None else None),
                "now_price_taken": round(px, 3),
                "drop_pts": drop, "span_s": span,
                "mins_left": L["mins_left"], "close_time": L["close_time"],
                **pullback_fields(ep, px, drop),
            })
        mems[key] = ent
    live_tks = {s["ticker"] for s in opens}
    mems = {k: v for k, v in mems.items()
            if k.split(":")[0] in live_tks or
            (v.get("hist") and now_ts - v["hist"][-1][0] <= HIST_KEEP_S)}
    st["signals"] = mems
    st["updated_utc"] = datetime.now(timezone.utc).isoformat()
    save_state(STATE_FILE, st)
    print(json.dumps({"kalshi_requests": dict(kalshi_request_stats), "ok": True,
                      "as_of_utc": datetime.now(timezone.utc).isoformat(),
                      "open": len(opens), "evaluated": len(live),
                      "alerts": alerts}))


if __name__ == "__main__":
    main()
