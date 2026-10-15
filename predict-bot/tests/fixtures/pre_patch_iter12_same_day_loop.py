#!/usr/bin/env python3
"""Hourly same-day prediction-market opportunity scan (paper/analysis only).

Scans Kalshi markets closing today + Polymarket events ending today, then:
  1. Prices Kalshi 15-min crypto "up?" markets against live Kraken spot with a
     zero-drift Brownian fair-value model:
     P(up) = Phi(lr / (sigma * sqrt(t))), lr = log(spot / window-open ref),
     t = minutes left; sigma is the RMS of the trailing 60 1-min Kraken
     log returns.
  2. Attempts cross-venue matching (Kalshi vs Polymarket) for mispricings.
  3. Checks Kalshi bracket-ladder internal consistency (monotonicity).
Dedupes against data/same_day_loop_state.json and prints JSON. A signal is
"new" when its (ticker, window) key was not alerted before. Exits 0 always;
the caller decides what to relay to the user.

Every run also grades alerted 15-min signals whose window has closed:
the ground truth is ONLY Kalshi's own published settlement (`result` from
the public market API; only settled YES/NO are accepted). Windows whose
settlement is not published yet are skipped and retried on later runs,
indefinitely — grading never falls back to any price proxy and never
touches Kraken. Grades append to data/signal_grades.jsonl and rolling
accuracy (over Kalshi-settled rows only) is included in the JSON output.

Flags: --no-mark (diagnostic read-only: never marks signals alerted, never
writes state or grades), --grade-only (skip scanning, only grade closed
windows and print accuracy), --exits-only (exit watch: re-price only
still-open signals and print exits).

No orders are placed anywhere. No credentials are used.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone

import requests

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
STATE_PATH = os.path.join(DATA, "same_day_loop_state.json")
GRADE_PATH = os.path.join(DATA, "signal_grades.jsonl")
EDGE_THRESHOLD = 0.05          # min |edge| to flag a 15-min signal
AGREE_LEAN_MIN = 0.5           # min taken-side price to agree with the market's
                               # directional lean (drops contrarian takes)
WINDOW_WARMUP_MIN = 3.0        # min after window open before signals may fire;
                               # the opening print + fresh book are unsettled
STRIKE_STRADDLE_GATE = False   # off until validated on Kalshi-settled outcomes
                               # (round 3: failed the ship bar, see CHANGELOG)
XVENUE_THRESHOLD = 0.08        # min cross-venue gap to flag
KRAKEN_TRADES_PAGE = 1000      # Kraken Trades returns at most this many rows;
                               # a full page means p_now may be stale
GRADE_DELAY_S = 120            # grade a window only this long after it closed
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
KRAKEN_PAIRS = {
    "BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD",
    "DOGE": "DOGEUSD", "HYPE": "HYPEUSD", "ZEC": "ZECUSD",
    "NEAR": "NEARUSD", "BNB": "BNBUSD",
}
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_API = "https://gamma-api.polymarket.com"
IO_WORKERS = 4                 # max concurrent HTTP calls; kept low to avoid 429s


def agrees_with_lean(side, mid):
    """True when the taken side agrees with the market's directional lean.

    mid is the YES-oriented market mid. The taken-side price is mid for a
    YES take and 1-mid for a NO take. Agreement (taken-side price >= 0.5)
    means the model is not fading a market that leans >=50% the other way.
    """
    taken = mid if side == "YES" else 1 - mid
    return taken >= AGREE_LEAN_MIN


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _state_backup_path():
    # Derived dynamically: STATE_PATH is monkeypatched to tmp dirs in tests.
    return STATE_PATH + ".bak"


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        # Missing OR corrupt: fall back to the last good backup instead of
        # defaulting to empty (which would wipe dedupe history and re-alert
        # everything on the next mark run). The missing case matters:
        # save_state renames the live file to .bak BEFORE the new tmp lands,
        # so a crash in that gap leaves STATE_PATH absent while .bak holds
        # the dedupe history (second hazard found by a sibling lane,
        # iter-10). Trying the backup first closes that hole.
        try:
            with open(_state_backup_path()) as f:
                return json.load(f)
        except Exception:
            return {"alerted": {}}


def save_state(s):
    os.makedirs(DATA, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    try:
        os.replace(STATE_PATH, _state_backup_path())  # keep last good copy
    except FileNotFoundError:
        pass
    os.replace(tmp, STATE_PATH)  # atomic: readers never see a half-written file


def _state_lock_path():
    # Derived dynamically: STATE_PATH is monkeypatched to tmp dirs in tests.
    return STATE_PATH + ".lock"


@contextmanager
def _state_write_lock():
    """Exclusive inter-process lock for the state load->decide->save path.

    Serializes the exit-watch poll against the 5-min scan's mark phase so two
    concurrent writers cannot both stamp + emit the same exit (duplicate-exit
    race: both load, both see exit_ts unset, both fire, both save).

    The lock lives on a SIDECAR (STATE_PATH + ".lock"), never on STATE_PATH
    itself: save_state replaces the state file's inode on every write, so a
    writer that opened the path after a save would flock a different inode
    and mutual exclusion would be silently lost with overlapping writers
    (sibling-lane find, iter-10). The sidecar's inode is never replaced.

    Callers MUST re-read state from disk after acquiring (never trust a
    pre-lock in-memory read); the lock is held only for the short
    re-read -> decide -> save sequence, never across the scan's network
    phase. flock is released automatically if the lock holder dies.

    Rollout note: previous code locked STATE_PATH itself, so an old-code
    process overlapping the atomic deploy does not mutually exclude with a
    new-code process. The window is one scan cycle; deploy between scans.
    """
    os.makedirs(DATA, exist_ok=True)
    fd = os.open(_state_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # blocking; writers are short-lived
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def kalshi_same_day(today: str):
    """All open Kalshi markets closing on the ET date `today`.

    Day boundaries are America/New_York and DST-aware (the 25-hour day
    2026-11-01 ends at 00:00 EST, not 23:59:59 EDT). The bound is the next
    ET midnight, inclusive, so the 23:45-00:00 ET 15-min window — which
    closes exactly at midnight — is covered instead of silently dropped.
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    day_start = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=et)
    next_midnight = day_start + timedelta(days=1)
    next_day = next_midnight.strftime("%Y-%m-%d")
    # Inclusive end-of-day bound, DST-aware: a fixed +86399 bound dropped the
    # 23:45-00:00 ET window every day (it closes exactly at the next
    # midnight) and, on the 25-hour DST-end day, cut off an hour early.
    end_ts = int(next_midnight.timestamp())
    out, seen, cursor, pages = [], set(), None, 0
    while True:
        params = {"limit": 1000, "status": "open", "max_close_ts": end_ts}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{KALSHI_API}/markets", params=params, timeout=30)
        r.raise_for_status()
        d = r.json()
        mkts = d.get("markets", [])
        if not mkts:
            break
        for m in mkts:
            t = m.get("ticker")
            if t in seen:
                continue
            seen.add(t)
            try:
                close_dt = datetime.fromisoformat(
                    (m.get("close_time") or "").replace("Z", "+00:00")
                ).astimezone(et)
            except Exception:
                continue
            # A window closing exactly at the next ET midnight belongs to
            # `today`'s trading day even though its close prints as the next
            # calendar date. (max_close_ts already bounds closes to
            # next_midnight, so a next-day close here is exactly midnight.)
            if (close_dt.strftime("%Y-%m-%d") == today
                    or (close_dt.strftime("%Y-%m-%d") == next_day
                        and close_dt <= next_midnight)):
                out.append(m)
        cursor = d.get("cursor")
        pages += 1
        if not cursor or pages > 60:
            break
    return out


_RUN_CACHE = None
_RUN_CACHE_LOCK = threading.Lock()


@contextmanager
def _kraken_run_cache():
    """Memoize _run_cached fetches for the duration of the block."""
    global _RUN_CACHE
    _RUN_CACHE = {}
    try:
        yield
    finally:
        _RUN_CACHE = None


def _run_cached(fn, *args):
    """fn(*args), shared across threads while _kraken_run_cache is active.

    Failures are not cached, so each caller retries just as it would
    without the cache.
    """
    cache = _RUN_CACHE
    if cache is None:
        return fn(*args)
    with _RUN_CACHE_LOCK:
        entry = cache.setdefault((fn, args), {"lock": threading.Lock()})
    with entry["lock"]:
        if "value" not in entry:
            entry["value"] = fn(*args)
        return entry["value"]


def _parallel_map(fn, items):
    """list(map(fn, items)) on a small thread pool; result order is preserved."""
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(IO_WORKERS, len(items))) as ex:
        return list(ex.map(fn, items))


def _kraken_result(r):
    """The `result` dict of a Kraken public response, or {} on an API error.

    Kraken reports failures in the `error` array, often with HTTP 200 and no
    `result`. W-prefixed entries are warnings that accompany a valid result,
    so only non-warning entries count as errors.
    """
    body = r.json()
    if any(not str(e).startswith("W") for e in body.get("error") or []):
        return {}
    return body.get("result", {})


def kraken_trades(pair, since_ts):
    r = requests.get("https://api.kraken.com/0/public/Trades",
                     params={"pair": pair, "since": since_ts}, timeout=20)
    r.raise_for_status()
    res = _kraken_result(r)
    key = next((k for k in res if k != "last"), None)
    return res.get(key, []) if key else []


def kraken_candles(pair):
    r = requests.get("https://api.kraken.com/0/public/OHLC",
                     params={"pair": pair, "interval": 1}, timeout=20)
    r.raise_for_status()
    res = _kraken_result(r)
    key = next((k for k in res if k != "last"), None)
    return [(int(c[0]), float(c[4])) for c in res.get(key, [])] if key else []


def fair_15m(pair, w_open, w_close, now_ts, meta=None):
    """(fair_prob, ref_avg, last_px, sigma_per_min, mins_left, spot_ts) or None.

    fair_prob = Phi(lr / (sigma*sqrt(t))): zero-drift Brownian fair value of
    the YES price from live Kraken spot vs the window-open reference.
    spot_ts is the exchange timestamp of the last-trade print used as p_now:
    additive metadata for spot-staleness analysis. Never read by
    entry/exit/grading logic.

    When a `meta` dict is passed it receives `trades_fetch_ts` (wall-clock
    time the trades page came back), `trades_rows` (page length), and
    `candles` (the 1-min (ts, close) series, for the log-only jump test):
    logging metadata only, never read by any decision.
    """
    try:
        trades = _run_cached(kraken_trades, pair, w_open - 300)
        if meta is not None:
            meta["trades_fetch_ts"] = time.time()
            meta["trades_rows"] = len(trades)
        ref = [float(t[0]) for t in trades if w_open - 60 <= float(t[2]) < w_open]
        if not ref or not trades:
            return None
        p_ref = sum(ref) / len(ref)
        p_now = float(trades[-1][0])
        p_now_ts = float(trades[-1][2])
        cs = _run_cached(kraken_candles, pair)
        if meta is not None:
            meta["candles"] = cs
        if len(cs) < 70:
            return None
        rets = [math.log(cs[i + 1][1] / cs[i][1]) for i in range(len(cs) - 61, len(cs) - 1)]
        sigma = math.sqrt(sum(x * x for x in rets) / len(rets))
        if sigma <= 0:
            return None
        t = max((w_close - now_ts) / 60.0, 0.5)
        lr = math.log(p_now / p_ref)
        z = lr / (sigma * math.sqrt(t))
        return phi(z), p_ref, p_now, sigma, t, p_now_ts
    except Exception:
        return None


def shrink_midrange(fair, mid, alpha=0.25, lo=0.40, hi=0.60):
    """Shrink fair toward the market mid, but ONLY when fair is in the
    mid-range band [lo, hi] where the model is known-overconfident.
    Bivariate in (fair, mid): trusts the calibrated market more exactly
    where the model is broken. Never flips the signal side: the shrunk
    fair always lies between the raw fair and mid."""
    if not (lo <= fair <= hi):
        return fair
    return fair + alpha * (mid - fair)


def in_warmup(wo, now_ts, warmup_min=WINDOW_WARMUP_MIN):
    """True while the window's opening print is still unsettled."""
    return (now_ts - wo) < warmup_min * 60


def straddles_strike(p_now, p_ref, strike):
    """True when Kalshi's strike and the Kraken window-open reference sit on
    opposite sides of spot, i.e. the model's up/down premise disagrees with
    the contract's actual strike. A missing/invalid strike never straddles."""
    try:
        strike = float(strike)
    except (TypeError, ValueError):
        return False
    if not strike > 0:
        return False
    return (p_now >= p_ref) != (p_now >= strike)


JUMP_K = 4.0  # Lee-Mykland jump threshold: flag when max |r|/sigma_robust > k


def jump_test_statistic(candles, w_open, now_ts, k=JUMP_K):
    """(flag, stat): Lee-Mykland-2008-style jump flag over the window so far.

    candles: [(ts, close), ...] 1-min closes (the same series fair_15m
    prices from). stat = max |r_i| / sigma_robust over the 1-min log returns
    whose closes both fall in [w_open, now_ts], where sigma_robust is the
    bipower-variation robust sigma,
    sqrt((pi/2)/(n-1) * sum |r_i * r_{i-1}|); flag = stat > k.
    (None, None) when fewer than 6 closes span the window, when any input is
    unusable, or when k is not positive. Log-only metadata: never read by
    entry/exit/grading logic. Never raises.
    """
    try:
        if not candles:
            return None, None
        closes = []
        for c in candles:
            ts, px = c[0], c[1]
            ts, px = float(ts), float(px)
            if w_open <= ts <= now_ts:
                closes.append((ts, px))
        if len(closes) < 6:
            return None, None
        rets = []
        for i in range(len(closes) - 1):
            p0, p1 = closes[i][1], closes[i + 1][1]
            if not (p0 > 0 and p1 > 0 and math.isfinite(p0)
                    and math.isfinite(p1)):
                return None, None
            r = math.log(p1 / p0)
            if not math.isfinite(r):
                return None, None
            rets.append(r)
        n = len(rets)
        if n < 5:
            return None, None
        bv = ((math.pi / 2.0)
              * sum(abs(rets[i] * rets[i - 1]) for i in range(1, n)))
        if not bv > 0:
            return None, None
        sigma_robust = math.sqrt(bv / (n - 1))
        if not sigma_robust > 0:
            return None, None
        stat = max(abs(r) for r in rets) / sigma_robust
        kk = float(k)
        if not kk > 0:
            return None, None
        return (stat > kk), stat
    except Exception:
        return None, None


def parse_15m_window(ticker):
    """(sym, open_utc_ts, close_utc_ts) from a Kalshi 15-min ticker, or None.

    Ticker form: KXBTC15M-26SEP301300-00 = 2026-SEP-30, 13:00 ET close.
    """
    m = re.match(r"^KX([A-Z]+)15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-\d+$",
                 ticker or "")
    if not m:
        return None
    sym, yy, mon, dd, hh, mi = m.groups()
    mo = MONTHS.get(mon)
    if not mo:
        return None
    try:
        from zoneinfo import ZoneInfo
        close_et = datetime(2000 + int(yy), mo, int(dd), int(hh), int(mi),
                            tzinfo=ZoneInfo("America/New_York"))
        close_utc = int(close_et.timestamp())
        return sym, close_utc - 900, close_utc
    except Exception:
        return None


class GradesUnavailable(Exception):
    """The grades file exists but cannot be read (permissions, IO error,
    mid-file read failure). Raised instead of returning a fabricated empty
    history: callers must treat the read as failed, never as "no grades".
    A missing file is NOT an error — it simply means nothing graded yet."""


def _grade_dedupe_key(g):
    """Hashable dedupe key for a grade row. Real keys are strings; a corrupt
    non-scalar key must not nuke the whole read (TypeError on set add)."""
    key = g.get("key") if isinstance(g, dict) else None
    return key if isinstance(key, (str, int, float, type(None))) else repr(key)


def load_grades():
    grades = []
    seen = set()
    try:
        f = open(GRADE_PATH)
    except FileNotFoundError:
        return []
    except OSError as e:
        raise GradesUnavailable(f"{GRADE_PATH}: {e}") from e
    try:
        with f:
            data = f.read()
    except OSError as e:
        raise GradesUnavailable(f"{GRADE_PATH}: {e}") from e
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            g = json.loads(line)
        except Exception:
            continue  # one bad line no longer hides the rest
        if not isinstance(g, dict):
            continue  # a non-dict line no longer hides the rest either
        dkey = _grade_dedupe_key(g)
        if dkey in seen:
            continue
        seen.add(dkey)
        grades.append(g)
    return grades


def append_grades(rows):
    """Append grade rows. Caller MUST hold _grades_append_lock() (grade_pending
    does): the newline check + writes are only atomic under the lock."""
    if not rows:
        return
    os.makedirs(DATA, exist_ok=True)
    try:  # ensure the file ends with a newline before appending a new line
        with open(GRADE_PATH, "rb") as f:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                need_nl = True
            else:
                need_nl = False
    except Exception:
        need_nl = False
    with open(GRADE_PATH, "a") as f:
        if need_nl:
            f.write("\n")
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _grades_lock_path():
    # Derived dynamically: GRADE_PATH is monkeypatched to tmp dirs in tests.
    return GRADE_PATH + ".lock"


@contextmanager
def _grades_append_lock():
    """Exclusive inter-process lock for appending to the grades file.

    Lives on a SIDECAR (GRADE_PATH + ".lock"), never on the grades file
    itself: flocking a file this process is about to re-open can
    self-deadlock, and any future compaction would replace the inode (the
    same hazard the state sidecar fixes). Held only for the short
    re-read -> filter -> append sequence, never across the settlement
    network phase. Lock order: the grades lock is never taken while the
    state lock is held (grade_pending runs outside the state lock), so no
    lock-order deadlock is possible.
    """
    os.makedirs(DATA, exist_ok=True)
    fd = os.open(_grades_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # blocking; graders are short-lived
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _pnl_per_dollar_staked(rows):
    pnl, staked = 0.0, 0.0
    for g in rows:
        m = g.get("market_mid")
        if m is None:
            continue
        # one paper contract per signal: YES costs m, NO costs (1-m).
        # "per dollar staked" = total profit / total dollars paid for contracts.
        staked += m if g.get("side") == "YES" else (1 - m)
        if g.get("side") == "YES":
            pnl += (1 - m) if g["correct"] else -m
        else:
            pnl += m if g["correct"] else -(1 - m)
    return round(pnl / staked, 4) if staked else None


KALSHI_TAKER_RATE = 0.07  # fee schedule effective 2026-07-07


def _kalshi_taker_fee_dollars(contracts, price):
    """Taker fee in dollars for `contracts` $1-payout contracts at `price`.

    Kalshi schedule: ceil(0.07 * C * P * (1-P) * 100) / 100, P in dollars.
    Reporting only: never read by any decision.
    """
    return math.ceil(KALSHI_TAKER_RATE * contracts * price * (1 - price)
                     * 100) / 100


def _pnl_1usd_stake(rows, net=False):
    """Mean per-signal P&L staking exactly $1 per signal at the taken price.

    Gross: win -> (1-p)/p, loss -> -1. net=True additionally subtracts the
    Kalshi taker fee on the contracts $1 buys at the taken price. Reporting
    only: never read by any decision. None when no row carries a market mid.
    """
    vals = []
    for g in rows:
        m = g.get("market_mid")
        if m is None:
            continue
        p = m if g.get("side") == "YES" else 1 - m
        if not 0 < p < 1:
            continue
        v = (1 - p) / p if g.get("correct") else -1.0
        if net:
            v -= _kalshi_taker_fee_dollars(1.0 / p, p)
        vals.append(v)
    return round(sum(vals) / len(vals), 4) if vals else None


def _mean_taken_price(rows):
    """Mean taken-side entry price: the win rate a market-calibrated
    baseline would expect. None when no row carries a market mid."""
    px = [g["market_mid"] if g.get("side") == "YES" else 1 - g["market_mid"]
          for g in rows if g.get("market_mid") is not None]
    return round(sum(px) / len(px), 3) if px else None


def accuracy_stats(grades):
    """Rolling stats over the last 50 Kalshi-settled signals.

    Only rows graded from Kalshi's published settlement (`proxy == "kalshi"`)
    count. Legacy Kraken-proxy rows (proxy == "kraken", flagged
    excluded_from_accuracy) are excluded: the proxy disagrees with Kalshi
    settlement ~20% of the time, so mixing them in would misstate accuracy.

    `post_filter` repeats the stats over the last 50 settled rows whose taken
    side agrees with the market lean (the population the agreement filter
    still admits); rows without a logged mid cannot be classified and are
    left out. Reporting only: nothing reads these numbers for any decision.
    """
    authoritative = [g for g in grades
                     if g.get("proxy") == "kalshi"
                     and not g.get("excluded_from_accuracy")]
    scored = [g for g in authoritative if g.get("correct") is not None]
    recent = scored[-50:]
    wins = sum(1 for g in recent if g["correct"])
    note = ("settlement per Kalshi's published result only "
            "(legacy Kraken-proxy rows excluded); gross of fees")
    post = [g for g in scored if g.get("market_mid") is not None
            and agrees_with_lean(g.get("side"), g["market_mid"])][-50:]
    post_wins = sum(1 for g in post if g["correct"])
    return {
        "graded": len(recent),
        "wins": wins,
        "win_rate": round(wins / len(recent), 3) if recent else None,
        "pnl_per_dollar_staked": _pnl_per_dollar_staked(recent),
        "note": note,
        "mean_taken_price": _mean_taken_price(recent),
        "post_filter": {
            "graded": len(post),
            "wins": post_wins,
            "win_rate": round(post_wins / len(post), 3) if post else None,
            "mean_taken_price": _mean_taken_price(post),
            "pnl_per_dollar_staked": _pnl_per_dollar_staked(post),
            "pnl_1usd_stake_gross": _pnl_1usd_stake(post, net=False),
            "pnl_1usd_stake_net": _pnl_1usd_stake(post, net=True),
        },
        # $1-stake gross/net-of-cost P&L beside the legacy key (iter-11
        # item 5). New keys only, appended AFTER all legacy keys so the
        # legacy key set and order stay byte-identical. Reporting only:
        # never read by any decision.
        "pnl_1usd_stake_gross": _pnl_1usd_stake(recent, net=False),
        "pnl_1usd_stake_net": _pnl_1usd_stake(recent, net=True),
    }


def grade_pending(alerted, now_ts, mark):
    """Grade alerted 15m signals whose window closed >= GRADE_DELAY_S ago.

    Ground truth is ONLY Kalshi's own published settlement (`result` field
    from the public market API); only settled YES/NO are accepted. When
    Kalshi hasn't published a settlement yet (None: missing, unsettled, or
    fetch failure), NOTHING is written for that window this run — it is
    skipped and retried on later runs, indefinitely, at any age. There is
    no price-proxy fallback: grading never touches Kraken. Returns
    (new_grade_rows, accuracy_stats). With mark=False nothing is written
    (diagnostic mode). Grading never affects entry/exit decisions.
    """
    grades = load_grades()
    graded_keys = {_grade_dedupe_key(g) for g in grades}
    new_rows = []
    cands = []
    for key, val in alerted.items():
        if key in graded_keys or key.startswith("xvenue:"):
            continue
        if isinstance(val, dict):
            ticker = val.get("ticker", "")
            side = val.get("side", "")
            wo, wc = val.get("window_open_utc"), val.get("window_close_utc")
        else:
            if ":" not in key:  # legacy "TICKER:SIDE" string entries
                continue
            ticker, side = key.rsplit(":", 1)
            parsed = parse_15m_window(ticker)
            if not parsed:
                continue
            _, wo, wc = parsed
        if not wc or wc > now_ts - GRADE_DELAY_S:
            continue
        cands.append((key, ticker, side, wo, wc, val))
    # Settlement truth from Kalshi itself, fetched in parallel (best-effort;
    # any failure -> None -> defer to a later run, never a proxy).
    tickers = sorted({t for _, t, _, _, _, _ in cands})
    settle = dict(zip(tickers, _parallel_map(kalshi_settlement, tickers)))
    for key, ticker, side, wo, wc, val in cands:
        actual = settle.get(ticker)
        if actual is None:
            continue  # settlement not published yet; retry next run, forever
        sym = ticker.split("15M")[0].replace("KX", "")
        new_rows.append({
            "key": key, "ticker": ticker, "sym": sym, "side": side,
            "edge": val.get("edge") if isinstance(val, dict) else None,
            "market_mid": val.get("market_mid") if isinstance(val, dict) else None,
            # copied at grade time so the row is self-contained for future
            # shrink re-validation; never read by grading math.
            "fair_raw": val.get("fair_raw") if isinstance(val, dict) else None,
            # alert-time order-book imbalance, copied for future validation
            # of idea #1; never read by grading math.
            "ob_imb_at_entry": val.get("ob_imb_at_entry") if isinstance(val, dict) else None,
            # alert-time spot age / spread / minutes-left, copied so future
            # iterations can slice accuracy by entry-time market context and
            # by Kraken spot staleness. Additive metadata only: never read
            # by grading math. None on legacy rows marked before logging.
            "spot_age_s": val.get("spot_age_s") if isinstance(val, dict) else None,
            "spread": val.get("spread") if isinstance(val, dict) else None,
            "mins_left": val.get("mins_left") if isinstance(val, dict) else None,
            # alert-time fetch-layer and model inputs (spot age at Kraken
            # fetch time, full-page flag, p_ref/p_now/sigma, Kalshi strike),
            # copied for staleness/basis replays. Additive metadata only:
            # never read by grading math. None on entries marked before
            # they were logged.
            "spot_age_fetch_s": val.get("spot_age_fetch_s") if isinstance(val, dict) else None,
            "trades_full_page": val.get("trades_full_page") if isinstance(val, dict) else None,
            "p_ref": val.get("p_ref") if isinstance(val, dict) else None,
            "p_now": val.get("p_now") if isinstance(val, dict) else None,
            "sigma": val.get("sigma") if isinstance(val, dict) else None,
            "floor_strike": val.get("floor_strike") if isinstance(val, dict) else None,
            "window_open_utc": wo, "window_close_utc": wc,
            "p_open": None, "p_close": None,  # never proxied; always None
            "actual": actual, "correct": (actual == side),
            "graded_at_utc": datetime.now(timezone.utc).isoformat(),
            "proxy": "kalshi",
            # alert-time top-of-book, copied for future spread-depth replays.
            # Additive metadata only: never read by grading math. None on
            # entries marked before bid/ask were logged.
            "bid": val.get("bid") if isinstance(val, dict) else None,
            "ask": val.get("ask") if isinstance(val, dict) else None,
            # Lee-Mykland-style jump flag at alert time (iter-11 item 0,
            # log-first). Additive metadata only: never read by grading
            # math. None on entries marked before it was logged.
            "jump_flag": val.get("jump_flag") if isinstance(val, dict) else None,
            "jump_stat": val.get("jump_stat") if isinstance(val, dict) else None,
        })
    if mark:
        # Serialize appends: settlements were fetched outside the lock;
        # re-read + filter by key INSIDE the grades lock before appending,
        # so a concurrent grader cannot slip a duplicate row between our
        # read and our append. (grade_pending runs outside the state lock,
        # so the grades lock is never nested inside the state lock.)
        with _grades_append_lock():
            fresh = load_grades()
            fresh_keys = {_grade_dedupe_key(g) for g in fresh}
            new_rows = [r for r in new_rows
                        if _grade_dedupe_key(r) not in fresh_keys]
            append_grades(new_rows)
            grades = fresh + new_rows
    return new_rows, accuracy_stats(grades)


def poly_same_day(today):
    out, offset = [], 0
    while True:
        r = requests.get(f"{GAMMA_API}/events",
                         params={"active": "true", "closed": "false",
                                 "limit": 500, "offset": offset,
                                 "order": "volume24hr", "ascending": "false"},
                         timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for e in batch:
            if (e.get("endDate") or "")[:10] != today:
                continue
            for m in e.get("markets", []):
                try:
                    outcomes, prices = m.get("outcomes"), m.get("outcomePrices")
                    if isinstance(outcomes, str):
                        outcomes = json.loads(outcomes)
                    if isinstance(prices, str):
                        prices = json.loads(prices)
                    if not (isinstance(outcomes, list) and len(outcomes) == 2):
                        continue
                    yp = float(prices[0])
                    if not 0.01 <= yp <= 0.99:
                        continue
                    out.append({
                        "event": e.get("title"), "question": m.get("question"),
                        "yes": yp,
                        "volume": num(m.get("volume")) or num(e.get("volume")),
                    })
                except Exception:
                    continue
        offset += 500
        if len(batch) < 500 or offset >= 3000:
            break
    return out


def normalize_title(title):
    import re
    t = re.sub(r"[^a-z0-9 ]", " ", (title or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    stop = {"will", "the", "a", "an", "be", "by", "of", "in", "on", "to",
            "for", "is", "it", "its", "at", "or", "and", "vs"}
    return " ".join(w for w in t.split() if w not in stop)


def jaccard(a, b):
    sa, sb = set(a.split()), set(b.split())
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


EXIT_EDGE = 0.0  # exit an open signal when its taken-side edge falls to this or below
EXIT_MIN_LEFT = 1.0  # don't bother exiting with less than this many minutes left


def decide_exit(side, mid_now, fair_now):
    """Pure exit decision for one open signal.

    side: "YES"/"NO" as originally alerted. mid_now/fair_now are YES terms.
    Returns "edge_reversed" / "edge_gone" when the trade thesis is dead,
    else None (hold). Entry needs |edge| >= EDGE_THRESHOLD, so the band
    (EXIT_EDGE, EDGE_THRESHOLD) acts as hysteresis against flip-flopping.
    """
    edge_now = fair_now - mid_now
    side_now = edge_now if side == "YES" else -edge_now
    if side_now <= EXIT_EDGE:
        return "edge_reversed" if side_now < -0.02 else "edge_gone"
    return None


def exit_direction(entry_price_taken, now_price_taken):
    """"favourable" when the taken side's price is above its entry price,
    else "adverse"; None when the entry price was never logged.

    Compared at the 3dp the exit payload displays. An edge exit says the
    model's thesis is dead, not which way the market moved; this field
    reports the move. Display metadata only: never read by decide_exit,
    exit filtering/sorting, or any entry/grading logic.
    """
    if entry_price_taken is None:
        return None
    if round(now_price_taken, 3) > round(entry_price_taken, 3):
        return "favourable"
    return "adverse"


def check_exits(alerted, live_by_ticker, now_ts, mark):
    """Re-price every still-open alerted 15m signal against the live market.

    Returns a list of exit dicts for signals whose taken-side edge has
    collapsed to EXIT_EDGE or below (thesis dead: edge gone or reversed).
    When mark is true, exited signals are stamped so they never re-fire.
    """
    exits, jobs = [], []
    for key, v in alerted.items():
        if not isinstance(v, dict) or v.get("side") not in ("YES", "NO"):
            continue  # cross-venue keys and malformed entries have no side
        if v.get("exit_ts"):
            continue
        wc = v.get("window_close_utc")
        if not wc or wc <= now_ts:
            continue  # closed windows grade out; nothing to exit
        if (wc - now_ts) / 60.0 < EXIT_MIN_LEFT:
            continue
        live = live_by_ticker.get(v.get("ticker"))
        if not live:
            continue
        m, sym, wo, _wc = live
        b, a = num(m.get("yes_bid_dollars")), num(m.get("yes_ask_dollars"))
        if not (b > 0 and a > b):
            continue
        mid = (b + a) / 2
        jobs.append((v, m, wc, mid, (KRAKEN_PAIRS[sym], wo, wc, now_ts)))
    with _kraken_run_cache():
        fairs = _parallel_map(lambda j: fair_15m(*j[4]), jobs)
    for (v, m, wc, mid, _args), res in zip(jobs, fairs):
        if not res:
            continue
        fair = shrink_midrange(res[0], mid)
        reason = decide_exit(v["side"], mid, fair)
        if not reason:
            continue
        edge_now = fair - mid
        side_now = edge_now if v["side"] == "YES" else -edge_now
        # Taken-side conversions for display. The exit payload must speak the
        # taken side's price/edge, not the YES-oriented book terms: for a NO
        # take the taken price is 1-mid and the taken edge is -edge.
        em, ee = v.get("market_mid"), v.get("edge")
        if v["side"] == "YES":
            entry_price_taken, entry_edge_taken = em, ee
            now_price_taken, now_fair_taken = mid, fair
        else:
            entry_price_taken = 1 - em if em is not None else None
            entry_edge_taken = -ee if ee is not None else None
            now_price_taken, now_fair_taken = 1 - mid, 1 - fair
        exits.append({
            "ticker": v.get("ticker"), "title": m.get("title"),
            "side": v["side"], "reason": reason,
            "entry_ts": v.get("ts"),
            # DEPRECATED (YES-oriented): prefer entry_price_taken /
            # entry_edge_taken below. Kept for backward compatibility.
            "entry_edge": ee,
            "entry_mid": em,
            # Taken-side entry leg (canonical for display).
            "entry_price_taken": (round(entry_price_taken, 3)
                                  if entry_price_taken is not None else None),
            "entry_edge_taken": (round(entry_edge_taken, 3)
                                 if entry_edge_taken is not None else None),
            # DEPRECATED (YES-oriented): prefer now_price_taken /
            # now_fair_taken below. Kept for backward compatibility.
            "now_mid": round(mid, 3), "now_fair": round(fair, 3),
            # Taken-side now leg (canonical for display).
            "now_price_taken": round(now_price_taken, 3),
            "now_fair_taken": round(now_fair_taken, 3),
            "now_edge": round(side_now, 3),  # already taken-side
            "exit_direction": exit_direction(entry_price_taken, now_price_taken),
            "mins_left": round((wc - now_ts) / 60.0, 1),
            "window_close_utc": wc,
        })
        if mark:
            v["exit_ts"] = datetime.now(timezone.utc).isoformat()
            v["exit_reason"] = reason
            v["exit_edge_now"] = round(side_now, 3)
            v["exit_mid_now"] = round(mid, 3)
    # most urgent first: deepest negative edge, then least time left
    exits.sort(key=lambda e: (e["now_edge"], -e["mins_left"]))
    return exits


def open_signals(alerted, now_ts):
    """Pure: ticker -> entry for alerted 15m signals still open and not exited."""
    out = {}
    for key, v in alerted.items():
        if not isinstance(v, dict) or v.get("side") not in ("YES", "NO"):
            continue
        if v.get("exit_ts"):
            continue
        wc = v.get("window_close_utc")
        if not wc or wc <= now_ts:
            continue
        if (wc - now_ts) / 60.0 < EXIT_MIN_LEFT:
            continue
        out[v.get("ticker")] = v
    return out


def kalshi_market(ticker):
    """Single-market quote fetch (lightweight, for the exit watch)."""
    r = requests.get(f"{KALSHI_API}/markets/{ticker}", timeout=15)
    r.raise_for_status()
    return r.json().get("market") or {}


def _book_side(levels):
    """[(price, size)] from [[price, qty], ...] (strings or numbers).

    None/missing side -> []. Raises ValueError on any malformed level.
    """
    if levels is None:
        return []
    if not isinstance(levels, list):
        raise ValueError("book side is not a list")
    out = []
    for lv in levels:
        if not isinstance(lv, (list, tuple)) or len(lv) < 2:
            raise ValueError("malformed level")
        if not all(isinstance(x, (str, int, float)) and not isinstance(x, bool)
                   for x in lv[:2]):
            raise ValueError("malformed level")
        px, qty = float(lv[0]), float(lv[1])
        if not (math.isfinite(px) and math.isfinite(qty)) or qty < 0:
            raise ValueError("malformed level")
        out.append((px, qty))
    return out


def book_imbalance(orderbook, depth=5):
    """Signed YES-side depth imbalance (V_bid - V_ask) / (V_bid + V_ask).

    Kalshi books list bids only: V_bid sums the sizes of the top-`depth` YES
    bids (highest price first); V_ask sums the top-`depth` NO bids (highest
    NO price first), which are the YES asks at 1 - no_price. Result is in
    [-1, 1], +1 = bid-heavy. Accepts the API body ({"orderbook_fp": {...}})
    or a raw book with "yes_dollars"/"no_dollars" (or legacy "yes"/"no").
    Empty/missing/malformed book -> None; never raises. Logging-only
    metadata: never read by entry/exit/grading logic.
    """
    try:
        if not isinstance(orderbook, dict):
            return None
        book = orderbook.get("orderbook_fp", orderbook.get("orderbook", orderbook))
        if not isinstance(book, dict):
            return None
        yes_key = "yes_dollars" if "yes_dollars" in book else "yes"
        no_key = "no_dollars" if "no_dollars" in book else "no"
        if yes_key not in book and no_key not in book:
            return None
        depth = int(depth)
        if depth <= 0:
            return None
        yes = sorted(_book_side(book.get(yes_key)), key=lambda l: -l[0])[:depth]
        no = sorted(_book_side(book.get(no_key)), key=lambda l: -l[0])[:depth]
        v_bid = sum(q for _, q in yes)
        v_ask = sum(q for _, q in no)
        total = v_bid + v_ask
        if not (total > 0 and math.isfinite(total)):
            return None
        return max(-1.0, min(1.0, (v_bid - v_ask) / total))
    except Exception:
        return None


def entry_ob_imbalance(ticker):
    """Best-effort top-of-book imbalance for `ticker` at alert time.

    Any failure (network, HTTP, JSON, parse) -> None; never raises.
    Logging-only metadata: never read by entry/exit/grading logic.
    """
    try:
        r = requests.get(f"{KALSHI_API}/markets/{ticker}/orderbook", timeout=10)
        r.raise_for_status()
        return book_imbalance(r.json())
    except Exception:
        return None


CONTRARIAN_LOG_MAX_BYTES = 512 * 1024  # rotate the shadow log past this size
CONTRARIAN_LOG_KEEP_BYTES = 128 * 1024  # ...keeping this much on rotation


def _contrarian_log_path():
    # Derived dynamically: DATA is monkeypatched to tmp dirs in tests.
    return os.path.join(DATA, "contrarian_drops.jsonl")


def _shadow_log_contrarian(rec):
    """Append one contrarian-drop record to the bounded shadow log.

    Called only from the lock-free scan phase, never inside the alerted
    path and never under the state lock. Best-effort and lossy by design:
    rotation is not atomic across concurrent scans, but O_APPEND keeps
    individual lines intact. Never raises. Never read by any decision.
    """
    try:
        os.makedirs(DATA, exist_ok=True)
        path = _contrarian_log_path()
        try:
            if os.path.getsize(path) > CONTRARIAN_LOG_MAX_BYTES:
                with open(path, "rb") as f:
                    tail = f.read()[-CONTRARIAN_LOG_KEEP_BYTES:]
                nl = tail.find(b"\n")
                with open(path, "wb") as f:
                    f.write(tail[nl + 1:] if nl >= 0 else tail)
        except FileNotFoundError:
            pass
        except OSError:
            return
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def kalshi_settlement(ticker):
    """Actual Kalshi settlement from the public market `result` field.

    Returns "YES"/"NO", or None when the market is unsettled, unknown, or
    the fetch fails (any error -> None, never raises). Grading-only helper:
    never affects entry/exit decisions.
    """
    try:
        r = (kalshi_market(ticker).get("result") or "").strip().lower()
        if r == "yes":
            return "YES"
        if r == "no":
            return "NO"
    except Exception:
        pass
    return None


def _fetch_quote(ticker):
    """(market, None) on success, (None, exception) on failure."""
    try:
        return kalshi_market(ticker), None
    except Exception as e:
        return None, e


def _exits_only_payload(alerted, now_ts, mark):
    """Build the exits-only JSON payload dict (no printing, no state write).

    Pure worker: re-prices open signals and optionally stamps exits in the
    passed-in `alerted` dict. Callers own locking/persistence/printing.
    """
    opens = open_signals(alerted, now_ts)
    live_by_ticker, failures = {}, []
    quotes = _parallel_map(_fetch_quote, opens)
    for (tk, v), (m, err) in zip(opens.items(), quotes):
        if err is not None:
            failures.append(f"{tk}: {err}")
            continue
        if not m:
            failures.append(f"{tk}: empty market")
            continue
        sym = tk.split("15M")[0].replace("KX", "")
        if sym not in KRAKEN_PAIRS:
            continue
        live_by_ticker[tk] = (m, sym, v.get("window_open_utc"),
                              v.get("window_close_utc"))
    if opens and not live_by_ticker:
        # priced nothing: failed run so the caller's 429 rule applies
        return {"ok": False, "mode": "exits_only",
                "error": f"no quotes fetched for {len(opens)} open signals",
                "quote_failures": failures}
    exits = check_exits(alerted, live_by_ticker, now_ts, mark)
    return {
        "ok": True, "mode": "exits_only",
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "open_evaluated": len(live_by_ticker),
        "exits_new": len(exits), "exits": exits,
        "quote_failures": failures,
    }


def exits_only_locked(now_ts):
    """Locked load->decide->save for the exit watch. Returns the payload dict.

    Takes the exclusive state lock, re-reads state from disk INSIDE the lock
    (no stale pre-lock reads), decides + stamps exits, saves, releases.
    This is what makes concurrent exit-watch polls (or a scan) serialize:
    the second writer sees the first's exit_ts stamps and emits nothing.
    """
    with _state_write_lock():
        alerted = load_state().get("alerted", {})
        payload = _exits_only_payload(alerted, now_ts, mark=True)
        save_state({"alerted": alerted})
    return payload


def exits_only_run(state, alerted, now_ts, mark):
    """Exit-watch mode: re-price only open signals, print exits as JSON."""
    if mark:
        # Serialize writers: the locked path re-reads state from disk so
        # two concurrent polls cannot both stamp+emit the same exit.
        payload = exits_only_locked(now_ts)
    else:
        payload = _exits_only_payload(alerted, now_ts, mark=False)
    print(json.dumps(payload, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-mark", action="store_true",
                    help="diagnostic read-only: never mark signals alerted, "
                         "never write state or grades")
    ap.add_argument("--grade-only", action="store_true",
                    help="skip scanning; only grade closed windows and print accuracy")
    ap.add_argument("--exits-only", action="store_true",
                    help="exit watch: re-price only still-open signals and print exits")
    args = ap.parse_args()
    mark = not args.no_mark

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    from zoneinfo import ZoneInfo as _ZI
    today_et = now.astimezone(_ZI("America/New_York")).strftime("%Y-%m-%d")
    now_ts = int(now.timestamp())
    state = load_state()
    alerted = state.get("alerted", {})
    signals, notes = [], []
    # (key, signal_dict, entry_record) accumulated during the scan; merged
    # into freshly re-read state under the write lock in the mark phase, so
    # the scan's network phase never holds the lock.
    pending_entries = []

    if args.grade_only:
        try:
            new_grades, acc = grade_pending(alerted, now_ts, mark)
        except GradesUnavailable as e:
            # Fail closed: the grades file exists but is unreadable. Never
            # fabricate an empty history (that would mass re-grade and
            # duplicate-append on the next healthy run).
            print(json.dumps({"ok": False, "error": f"grades_unavailable: {e}"}))
            return
        print(json.dumps({"ok": True, "as_of_utc": now.isoformat(),
                          "graded_new": len(new_grades),
                          "new_grades": new_grades,
                          "accuracy": acc}, indent=1))
        return

    if args.exits_only:
        exits_only_run(state, alerted, now_ts, mark)
        return

    # ---- 1. Kalshi same-day universe ----
    try:
        kmkts = kalshi_same_day(today_et)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"kalshi fetch failed: {e}"}))
        return
    notes.append(f"kalshi_same_day_markets={len(kmkts)}")

    # ---- 2. 15-min crypto windows live right now ----
    live15 = []
    for m in kmkts:
        tk = m.get("ticker") or ""
        if "15M" not in tk:
            continue
        try:
            wo = int(datetime.fromisoformat(m["open_time"].replace("Z", "+00:00")).timestamp())
            wc = int(datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
        if wo <= now_ts < wc:
            sym = tk.split("15M")[0].replace("KX", "")
            if sym in KRAKEN_PAIRS:
                live15.append((m, sym, wo, wc))
    notes.append(f"live_15m_crypto_windows={len(live15)}")

    for m, sym, wo, wc in live15:
        if in_warmup(wo, now_ts):
            continue  # re-evaluated on the next scan with a settled book
        b, a = num(m.get("yes_bid_dollars")), num(m.get("yes_ask_dollars"))
        if not (b > 0 and a > b):
            continue
        mid = (b + a) / 2
        if not 0.01 <= mid <= 0.99:
            continue
        fetch_meta = {}
        res = fair_15m(KRAKEN_PAIRS[sym], wo, wc, now_ts, fetch_meta)
        if not res:
            continue
        fair, p_ref, p_now, sigma, t, p_now_ts = res
        # Raw (pre-shrink) fair, logged for future shrink re-validation.
        # Additive metadata only: never read by entry/exit/grading logic.
        fair_raw = fair
        fair = shrink_midrange(fair, mid)
        edge = fair - mid
        if abs(edge) < EDGE_THRESHOLD:
            continue
        if STRIKE_STRADDLE_GATE and straddles_strike(p_now, p_ref, m.get("floor_strike")):
            continue
        side = "YES" if edge > 0 else "NO"
        if not agrees_with_lean(side, mid):
            # Shadow-log the contrarian drop (iter-11 item 7): the scan
            # phase is lock-free and this branch never touches the alerted
            # path. Gated on mark so --no-mark stays fully read-only.
            if mark:
                _shadow_log_contrarian({
                    "ts": now.isoformat(), "ticker": m["ticker"],
                    "side": side, "market_mid": round(mid, 3),
                    "fair": round(fair, 3), "edge": round(edge, 3),
                    "spread": round(a - b, 3), "mins_left": round(t, 1),
                    "window_open_utc": wo, "window_close_utc": wc,
                })
            continue  # agreement filter: drop contrarian takes
        key = f"{m['ticker']}:{side}"
        is_new = key not in alerted
        sig = {
            "type": "15m_fade", "ticker": m["ticker"], "title": m.get("title"),
            "side": side, "market_mid": round(mid, 3), "fair": round(fair, 3),
            "edge": round(edge, 3),
            "spread": round(a - b, 3),
            "volume": num(m.get("volume_fp")),
            "mins_left": round(t, 1),
            "window_close_utc": m.get("close_time"),
            "new": is_new,
        }
        signals.append(sig)
        if is_new and mark:
            # Alert-time order-book imbalance (idea #1, log-first phase).
            # Fetched strictly after every gate; logged to state only, never
            # printed, never read by any decision. Failure -> None.
            try:
                imb = entry_ob_imbalance(m["ticker"])
            except Exception:
                imb = None
            ob_imb = round(imb, 3) if imb is not None else None
            # Jump flag, log-first (iter-11 item 0): Lee-Mykland-style test
            # on the window-so-far 1-min returns, computed from the candles
            # fair_15m already fetched (meta["candles"]) — zero extra
            # network, and never evaluated in --no-mark (no pending entries
            # there). Logged to state only; NEVER read by any entry/exit/
            # grading decision; never raises (None on any failure).
            jf, js = jump_test_statistic(fetch_meta.get("candles"), wo, now_ts)
            # Not marked yet: merged into freshly re-read state under the
            # write lock in the mark phase (duplicate-exit/entry race fix).
            pending_entries.append((key, sig, {
                "ts": now.isoformat(), "ticker": m["ticker"], "side": side,
                "edge": round(edge, 3), "market_mid": round(mid, 3),
                "window_open_utc": wo, "window_close_utc": wc,
                # alert-time market context for future regime analysis.
                # Additive metadata only: never read by entry/exit logic,
                # never used for grading — cannot alter any decision.
                "spread": round(a - b, 3), "mins_left": round(t, 1),
                # alert-time top-of-book (iter-11 item 6). Additive
                # metadata only: never read by entry/exit/grading logic.
                "bid": round(b, 3), "ask": round(a, 3),
                # pre-shrink fair: lets future iterations re-validate the
                # shrink upgrade on fresh data (inverting the shrink from the
                # recorded edge is not exact when the mid is outside the band).
                "fair_raw": round(fair_raw, 3),
                # signed top-5 YES depth imbalance at alert time, +1 =
                # bid-heavy (None when the book fetch/parse failed).
                "ob_imb_at_entry": ob_imb,
                # age (seconds) of the Kraken spot print used as p_now, at
                # alert time. Additive metadata only: never read by
                # entry/exit/grading logic. Lets future iterations measure
                # how often p_now is stale (the Trades endpoint caps at
                # 1000 rows) and validate a staleness guard on new data.
                "spot_age_s": int(round(now_ts - p_now_ts)),
                # same age measured when the trades page came back (the
                # field above uses run start, so it goes negative after a
                # slow scan), plus whether that page hit Kraken's row cap.
                # Additive metadata only: never read by any decision.
                "spot_age_fetch_s": (
                    int(round(fetch_meta["trades_fetch_ts"] - p_now_ts))
                    if "trades_fetch_ts" in fetch_meta else None),
                "trades_full_page": (
                    fetch_meta["trades_rows"] >= KRAKEN_TRADES_PAGE
                    if "trades_rows" in fetch_meta else None),
                # model inputs and the contract strike, for basis/straddle
                # replays without refetching. floor_strike comes from the
                # already-fetched market (no extra call). Never read by
                # any decision.
                "p_ref": p_ref, "p_now": p_now, "sigma": sigma,
                "floor_strike": m.get("floor_strike"),
                # Lee-Mykland-style jump flag at alert time (iter-11 item
                # 0, log-first). Additive metadata only: never read by any
                # entry/exit/grading decision.
                "jump_flag": jf,
                "jump_stat": round(js, 3) if js is not None else None,
            }))

    # ---- 2b. live_by_ticker for the exit check (the check itself runs in
    # the locked mark phase below) ----
    live_by_ticker = {m["ticker"]: (m, sym, wo, wc) for m, sym, wo, wc in live15}

    # ---- 3. Cross-venue gaps (best effort) ----
    try:
        poly = [p for p in poly_same_day(today) if p["volume"] >= 5000]
        notes.append(f"polymarket_same_day_liquid={len(poly)}")
        pref = [(p, normalize_title(f"{p['event']}: {p['question']}")) for p in poly]
        checked = 0
        for m in kmkts:
            if num(m.get("volume_fp")) < 500:
                continue
            b, a = num(m.get("yes_bid_dollars")), num(m.get("yes_ask_dollars"))
            px = (b + a) / 2 if b > 0 and a > b else num(m.get("last_price_dollars"))
            if not 0.01 <= px <= 0.99:
                continue
            kn = normalize_title(m.get("title"))
            best, bs = None, 0.0
            for p, pn in pref:
                s = jaccard(kn, pn)
                if s > bs:
                    best, bs = p, s
            checked += 1
            if best and bs >= 0.5 and abs(px - best["yes"]) >= XVENUE_THRESHOLD:
                key = f"xvenue:{m['ticker']}"
                is_new = key not in alerted
                sig = {
                    "type": "cross_venue", "ticker": m["ticker"],
                    "title": m.get("title"), "kalshi_px": round(px, 3),
                    "poly_px": round(best["yes"], 3),
                    "poly_market": f"{best['event']}: {best['question']}",
                    "gap": round(abs(px - best["yes"]), 3),
                    "side": "YES" if px < best["yes"] else "NO",
                    "new": is_new,
                }
                signals.append(sig)
                if is_new and mark:
                    # Merged into freshly re-read state under the write lock
                    # in the mark phase (duplicate-entry race fix).
                    pending_entries.append(
                        (key, sig,
                         {"ts": now.isoformat(), "ticker": m["ticker"]}))
            if checked > 400:
                break
    except Exception as e:
        notes.append(f"cross_venue_skipped={e}")

    # ---- 4. Mark phase: merge new entries + stamp exits atomically ----
    # The whole scan above ran lock-free (network phase). Only this short
    # re-read -> merge -> decide -> save sequence holds the exclusive state
    # lock, so a concurrent exit-watch poll cannot interleave and double-fire.
    if mark:
        with _state_write_lock():
            fresh = load_state()  # fresh read inside the lock: no stale reads
            fresh_alerted = fresh.get("alerted", {})
            for key, sig, rec in pending_entries:
                if key in fresh_alerted:
                    sig["new"] = False  # lost the race: already alerted
                else:
                    fresh_alerted[key] = rec
            exits = check_exits(fresh_alerted, live_by_ticker, now_ts,
                                mark=True)
            save_state({"alerted": fresh_alerted})
        alerted = fresh_alerted
    else:
        exits = check_exits(alerted, live_by_ticker, now_ts, mark=False)
    notes.append(f"exit_check_open_evaluated={len(exits)}_exits")
    new_signals = [s for s in signals if s.get("new")]
    try:
        new_grades, acc = grade_pending(alerted, now_ts, mark)
    except Exception as e:
        # Grading must never swallow alert delivery: signals were already
        # stamped and saved above, so print the JSON payload anyway and note
        # the grading failure instead of exiting with empty stdout.
        notes.append(f"grading_failed={e}")
        new_grades = []
        try:
            acc = accuracy_stats(load_grades())
        except GradesUnavailable as ge:
            # The :1137 trap (sibling-lane find, iter-10): load_grades now
            # raises when the grades file exists but is unreadable — calling
            # it unguarded here would crash the whole payload print (the
            # iter-2 empty-output failure). Report unknown accuracy instead
            # of fabricating zeros.
            notes.append(f"grades_unavailable={ge}")
            acc = {"ok": False, "error": "grades_unavailable"}
    print(json.dumps({
        "ok": True,
        "as_of_utc": now.isoformat(),
        "notes": notes,
        "signals_total": len(signals),
        "signals_new": len(new_signals),
        "new_signals": new_signals,
        "exits_new": len(exits),
        "exits": exits,
        "graded_new": len(new_grades),
        "new_grades": new_grades,
        "accuracy": acc,
    }, indent=1))


if __name__ == "__main__":
    main()
