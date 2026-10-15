#!/usr/bin/env python3
"""Rapid-flip detector for Kalshi 15-min crypto windows (paper/analysis only).

Polls the public Kalshi markets API for the 9 scanned coins' live 15-min
windows, keeps a short mid-price history per ticker, and reports when the
crowd flips fast:

  - mid moves >= FLIP_MOVE (default 10c) within FLIP_WINDOW_S (30s).
    A 50c cross is reported as context in the flip message but does not
    trigger on its own (small crosses are noise by design).

Output: one JSON object on stdout:
  {"ok": true, "as_of_utc": ..., "evaluated": N, "flips": [...]}
  {"ok": false, "error": "..."}
State: ~/hooks/state/kalshi-flip-watch.json (per-ticker price history +
per-ticker alert cooldown). Not written on dry runs.

All tunables are env-overridable (FLIP_MOVE etc.); code defaults apply
when unset or unparsable.
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import requests
from kalshi_client import kalshi_get, stats as kalshi_request_stats
from zoneinfo import ZoneInfo

COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "ZEC", "NEAR", "BNB"]
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
STATE_FILE = os.path.expanduser("~/hooks/state/kalshi-flip-watch.json")

# Tunables: env overrides, code defaults below. FLIP_MOVE raised 0.05 -> 0.10
# 2026-10-02 per user: 5c crosses are noise; alert only on >=10c moves.
def _env_float(name, default):
    try:
        v = float(os.environ.get(name, ""))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _env_int(name, default):
    try:
        v = int(float(os.environ.get(name, "")))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


FLIP_MOVE = _env_float("FLIP_MOVE", 0.10)
FLIP_WINDOW_S = _env_int("FLIP_WINDOW_S", 30)
HIST_KEEP_S = _env_int("HIST_KEEP_S", 60)
MIN_SPAN_S = _env_int("MIN_SPAN_S", 10)
COOLDOWN_S = _env_int("COOLDOWN_S", 300)


def num(x):
    try:
        f = float(x)
        return f if math.isfinite(f) else 0.0
    except (TypeError, ValueError):
        return 0.0


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
            return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(s):
    if os.environ.get("HATCH_HOOK_DRY_RUN") == "1":
        return
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_FILE)


def main():
    now = time.time()
    # End of the *Eastern* calendar day. The 19:45-20:00 ET 15-min windows
    # close at midnight UTC, which a UTC end-of-day bound would wrongly
    # exclude (blind spot every evening 19:45-20:00 ET).
    et_now = datetime.now(ZoneInfo("America/New_York"))
    end_ts = int(et_now.replace(hour=23, minute=59, second=59,
                                microsecond=0).timestamp())
    try:
        mkts, cursor = [], None
        for _ in range(10):
            # Bound the sweep to windows closing soon: the full end-of-day
            # sweep pulls ~11k markets (~7s); we only need the 9 coins'
            # live 15-min windows, which close within 15 min.
            params = {"limit": 1000, "status": "open",
                      "min_close_ts": int(now) - 120,
                      "max_close_ts": int(now) + 1800}
            if cursor:
                params["cursor"] = cursor
            r = kalshi_get(f"{KALSHI_API}/markets", params=params,
                             timeout=20)
            r.raise_for_status()
            d = r.json()
            mkts.extend(d.get("markets", []))
            cursor = d.get("cursor")
            if not cursor:
                break
    except Exception as e:
        print(json.dumps({"kalshi_requests": dict(kalshi_request_stats), "ok": False,
                          "error": "markets fetch failed: %s" % e}))
        return

    live = []
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
        if not (wo <= now < wc):
            continue
        b = num(m.get("yes_bid_dollars"))
        a = num(m.get("yes_ask_dollars"))
        mid = (b + a) / 2 if (b > 0 and a > b) else num(
            m.get("last_price_dollars"))
        if not (0.01 <= mid <= 0.99):
            continue
        live.append({"ticker": tk, "title": m.get("title"), "sym": sym,
                     "mid": mid, "close_time": m.get("close_time"),
                     "mins_left": round((wc - now) / 60, 1)})

    st = load_state()
    tickers = st.get("tickers", {})
    flips = []
    for L in live:
        tk = L["ticker"]
        ent = tickers.get(tk, {"hist": [], "last_alert_ts": 0})
        hist = [[ts, px] for ts, px in ent.get("hist", [])
                if now - ts <= HIST_KEEP_S]
        hist.append([now, L["mid"]])
        ent["hist"] = hist
        win = [[ts, px] for ts, px in hist if now - ts <= FLIP_WINDOW_S]
        if len(win) >= 2 and (win[-1][0] - win[0][0]) >= MIN_SPAN_S:
            old_ts, old_px = win[0]
            new_px = win[-1][1]
            move = new_px - old_px
            crossed = (old_px - 0.50) * (new_px - 0.50) < 0
            if abs(move) >= FLIP_MOVE and \
                    (now - ent.get("last_alert_ts", 0)) >= COOLDOWN_S:
                ent["last_alert_ts"] = now
                ent["last_alert_dir"] = "up" if move > 0 else "down"
                flips.append({
                    "ticker": tk, "title": L["title"], "coin": L["sym"],
                    "direction": "toward YES" if move > 0 else "toward NO",
                    "from_mid": round(old_px, 3), "to_mid": round(new_px, 3),
                    "move_pts": round(abs(move), 3),
                    "span_s": round(win[-1][0] - old_ts, 1),
                    "crossed_50": crossed,
                    "mins_left": L["mins_left"],
                    "close_time": L["close_time"],
                })
        tickers[tk] = ent
    live_tks = {L["ticker"] for L in live}
    tickers = {k: v for k, v in tickers.items() if k in live_tks}
    st["tickers"] = tickers
    st["updated_utc"] = datetime.now(timezone.utc).isoformat()
    save_state(st)
    print(json.dumps({"kalshi_requests": dict(kalshi_request_stats), "ok": True,
                      "as_of_utc": datetime.now(timezone.utc).isoformat(),
                      "evaluated": len(live), "flips": flips}))


main()
