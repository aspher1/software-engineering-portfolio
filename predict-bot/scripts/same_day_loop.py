#!/usr/bin/env python3
"""2-minute same-day prediction-market opportunity scan (paper/analysis only).

Scans Kalshi markets closing today + Polymarket events ending today, then:
  1. Prices Kalshi 15-min crypto "up?" markets against live Kraken spot with a
     zero-drift Brownian fair-value model:
     P(up) = Phi(lr / (sigma * sqrt(t))), lr = log(spot / window-open ref),
     t = minutes left; sigma is the RMS of the trailing 60 1-min Kraken
     log returns.
  2. Attempts cross-venue matching (Kalshi vs Polymarket) for mispricings.
  3. Checks Kalshi bracket-ladder internal consistency (monotonicity).
Dedupes against data/same_day_loop_state.json and prints JSON. A signal is
"new" when its (ticker, side) key was not alerted before. Exits 0 always;
the caller decides what to relay to the user.

Every run also grades alerted 15-min signals whose window has closed:
the ground truth is ONLY Kalshi's own published settlement (`result` from
the public market API; only settled YES/NO are accepted). Windows whose
settlement is not published yet are skipped and retried on later runs,
indefinitely — grading never falls back to any price proxy and never
touches Kraken. Grades append to data/signal_grades.jsonl and rolling
accuracy (over Kalshi-settled rows only) is included in the JSON output.

Flags: --no-mark (diagnostic read-only: never marks signals alerted, never
writes state or grades, and never fires the exit-copilot advisory),
--grade-only (skip scanning, only grade closed windows and print accuracy),
--exits-only (exit watch: re-price only still-open signals and print exits).

No orders are placed anywhere. No credentials are used.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone

import requests
from kalshi_client import kalshi_get, stats as kalshi_request_stats

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
STATE_PATH = os.path.join(DATA, "same_day_loop_state.json")
GRADE_PATH = os.path.join(DATA, "signal_grades.jsonl")
PERSISTENCE_SHADOW_DELAY_S = 90
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


class KalshiMarkets(list):
    """Market rows with additive fetch-degradation diagnostics."""

    def __init__(self):
        super().__init__()
        self.failures = []


def _day_bounds(today: str):
    """ET day bounds for `today`: (end_ts, next_midnight, next_day).

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
    return int(next_midnight.timestamp()), next_midnight, next_day


def _close_in_today(m, today, next_day, next_midnight, et):
    """Close-date filter shared by the sweep and the series fetch."""
    try:
        close_dt = datetime.fromisoformat(
            (m.get("close_time") or "").replace("Z", "+00:00")
        ).astimezone(et)
    except Exception:
        return False
    # A window closing exactly at the next ET midnight belongs to `today`'s
    # trading day even though its close prints as the next calendar date.
    # (max_close_ts already bounds closes to next_midnight, so a next-day
    # close here is exactly midnight.)
    return (close_dt.strftime("%Y-%m-%d") == today
            or (close_dt.strftime("%Y-%m-%d") == next_day
                and close_dt <= next_midnight))


def _kalshi_sweep_fresh(today: str, polite_sleep: float = 0.0):
    """Full paginated end-of-day sweep. Returns KalshiMarkets (.failures set).

    `polite_sleep` adds an extra pause between pages so a background refresh
    leaves headroom in the shared 30 req/min bucket for the live lanes.
    """
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    end_ts, next_midnight, next_day = _day_bounds(today)
    out, seen, cursor, pages = KalshiMarkets(), set(), None, 0
    while True:
        params = {"limit": 1000, "status": "open", "max_close_ts": end_ts}
        if cursor:
            params["cursor"] = cursor
        try:
            r = kalshi_get(f"{KALSHI_API}/markets", params=params, timeout=30)
            d = r.json()
        except Exception as exc:
            out.failures.append(f"sweep page {pages + 1}: {exc}")
            break
        mkts = d.get("markets", [])
        if not mkts:
            break
        for m in mkts:
            t = m.get("ticker")
            if t in seen:
                continue
            seen.add(t)
            if _close_in_today(m, today, next_day, next_midnight, et):
                out.append(m)
        cursor = d.get("cursor")
        pages += 1
        if polite_sleep:
            time.sleep(polite_sleep)
        if not cursor or pages > 60:
            break
    return out


def _kalshi_crypto_series_fresh(today: str):
    """The 9 explicit 15-min crypto series fetches. ALWAYS fresh.

    Returns (rows, failures). 2026-10-06: the unfiltered open-market sweep
    stopped containing any 15M crypto series (61k cap + Kalshi's result
    ordering pushed them out), leaving the scanner blind to its core
    markets. Fetching each series explicitly keeps entry/exit coverage
    independent of sweep ordering — and of the sweep cache.
    """
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    end_ts, next_midnight, next_day = _day_bounds(today)
    rows, failures = [], []
    for sym in KRAKEN_PAIRS:
        try:
            params = {"limit": 1000, "status": "open",
                      "series_ticker": f"KX{sym}15M",
                      "max_close_ts": end_ts}
            r = kalshi_get(f"{KALSHI_API}/markets", params=params, timeout=30)
            r.raise_for_status()
            for m in r.json().get("markets", []):
                if _close_in_today(m, today, next_day, next_midnight, et):
                    rows.append(m)
        except Exception as exc:
            failures.append(f"series KX{sym}15M: {exc}")
    return rows, failures


# ---- Sweep cache (2026-10-06 cadence fix) ----
# The full end-of-day sweep (up to 61 Kalshi pages) exists for cross-venue /
# bracket coverage, not the 15-min entry signals. At the shared 30 req/min
# bucket pace it costs ~120s+, blowing the 2-min cron budget and getting
# scans killed. Cache it in the repo data dir (durable: /tmp is wiped across
# exec calls here) and refresh it in a detached background worker; the 9
# crypto series above are ALWAYS fetched fresh every scan.
SWEEP_CACHE_PATH = os.path.join(DATA, "sweep_cache.json")
SWEEP_CACHE_TTL_S = 12 * 60  # serve the sweep from cache for ~12 minutes
SWEEP_REFRESH_LOCK_PATH = os.path.join(DATA, "sweep_refresh.lock")
SWEEP_REFRESH_STATE_PATH = os.path.join(DATA, "sweep_refresh_state.json")
SWEEP_SPAWN_COOLDOWN_S = 5 * 60  # don't respawn a refresher more often
# Market-row fields consumed downstream (live15 extraction + cross-venue
# check). Everything else is dropped before caching to keep the file small.
SWEEP_CACHE_FIELDS = ("ticker", "event_ticker", "status", "title",
                      "open_time", "close_time", "yes_bid_dollars",
                      "yes_ask_dollars", "last_price_dollars",
                      "volume_fp", "floor_strike", "cap_strike")


def _load_sweep_cache(today):
    """Return (markets, age_s), or (None, None) on miss / wrong day / corrupt."""
    try:
        with open(SWEEP_CACHE_PATH) as f:
            c = json.load(f)
    except (OSError, ValueError):
        return None, None
    if c.get("today_et") != today:
        return None, None
    try:
        age = time.time() - float(c.get("fetched_at_ts", 0))
    except (TypeError, ValueError):
        return None, None
    if age < 0:
        return None, None
    return c.get("markets") or [], age


def _save_sweep_cache(today, markets):
    """Atomically write the trimmed sweep cache (tmp + rename)."""
    trimmed = [{k: m.get(k) for k in SWEEP_CACHE_FIELDS} for m in markets]
    payload = {"today_et": today, "fetched_at_ts": time.time(),
               "markets": trimmed}
    os.makedirs(DATA, exist_ok=True)
    tmp = SWEEP_CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, SWEEP_CACHE_PATH)


def _spawn_sweep_refresh(today):
    """Fire-and-forget detached sweep refresh. Never blocks the scan.

    Returns True when a refresher was spawned. A cooldown sidecar keeps a
    dead Kalshi endpoint from respawning a doomed refresher every scan.
    """
    try:
        with open(SWEEP_REFRESH_STATE_PATH) as f:
            last = float(json.load(f).get("last_attempt_ts", 0) or 0)
        if time.time() - last < SWEEP_SPAWN_COOLDOWN_S:
            return False
    except (OSError, ValueError):
        pass
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(SWEEP_REFRESH_STATE_PATH, "w") as f:
            json.dump({"last_attempt_ts": time.time()}, f)
    except OSError:
        pass
    script = os.path.join(BASE, "scripts", "same_day_loop.py")
    log = os.path.join(DATA, "sweep_refresh.log")
    try:
        with open(log, "a") as lf:
            subprocess.Popen(
                [sys.executable, script, "--refresh-sweep-cache",
                 "--sweep-date", today],
                stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True)
        return True
    except Exception:
        # Log loudly: a silently-false spawn would leave the sweep stale
        # forever with no trace.
        import traceback as _tb
        try:
            with open(log, "a") as _lf:
                _lf.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime())
                          + "SPAWN FAILED: " + _tb.format_exc())
        except OSError:
            pass
        return False


def _refresh_sweep_cache(today):
    """--refresh-sweep-cache worker: full sweep, polite pace, atomic write.

    Never touches scanner state, grades, or alerts. A non-blocking lock
    keeps overlapping scans from stacking refreshers; on sweep failure the
    old cache is left intact for the next attempt.
    """
    os.makedirs(DATA, exist_ok=True)
    fd = os.open(SWEEP_REFRESH_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(json.dumps({"ok": True, "skipped": "refresh already running"}))
            return
        try:
            sweep = _kalshi_sweep_fresh(today, polite_sleep=2.0)
            if sweep.failures and not sweep:
                print(json.dumps({"ok": False,
                                  "error": "sweep failed; cache untouched",
                                  "failures": sweep.failures}))
                return
            _save_sweep_cache(today, sweep)
            print(json.dumps({"ok": True, "today": today,
                              "markets": len(sweep),
                              "failures": sweep.failures}))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def kalshi_same_day(today: str):
    """All open Kalshi markets closing on the ET date `today`.

    Composition (2026-10-06 cadence fix): the 9 crypto series are ALWAYS
    fetched fresh — the 15-min entry/exit path depends on them. The full
    end-of-day sweep (cross-venue/bracket coverage) is served from
    data/sweep_cache.json and refreshed in a detached background worker
    every ~12 minutes, so a routine scan costs ~9 series requests instead
    of ~70. Series rows win ticker ties over cached sweep rows (fresher).
    """
    out = KalshiMarkets()
    out.cache_notes = []
    # 1. Crypto series: always fresh.
    series_rows, series_failures = _kalshi_crypto_series_fresh(today)
    seen = set()
    for m in series_rows:
        t = m.get("ticker")
        if t in seen:
            continue
        seen.add(t)
        out.append(m)
    out.failures.extend(series_failures)
    # 2. End-of-day sweep: cached.
    cached, age = _load_sweep_cache(today)
    if cached is not None and age <= SWEEP_CACHE_TTL_S:
        added = 0
        for m in cached:
            t = m.get("ticker")
            if t in seen:
                continue
            seen.add(t)
            out.append(m)
            added += 1
        out.cache_notes.append(
            f"sweep_cache=hit age_s={int(age)} markets={added}")
    else:
        spawned = _spawn_sweep_refresh(today)
        if cached is not None:
            added = 0
            for m in cached:
                t = m.get("ticker")
                if t in seen:
                    continue
                seen.add(t)
                out.append(m)
                added += 1
            # Stale-but-served: the data was still usable (freshness only
            # matters for the EOD sweep, never for the 15-min crypto path),
            # so this is a diagnostic note, not a fetch failure. Counting it
            # as a failure flipped healthy boundary-rollover scans to
            # ok:false whenever fair_snap was also empty (13:59/14:17 EDT
            # false alarms, 2026-10-06).
            out.cache_notes.append(
                f"sweep_cache=stale age_s={int(age)} markets={added} "
                f"refresh_spawned={spawned}")
        else:
            out.failures.append(f"sweep_cache=cold refresh_spawned={spawned}")
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
    time the trades page came back), `trades_rows` (page length), `candles`
    (the 1-min (ts, close) series, for the log-only jump test),
    `p_now_candle` (latest 1-min candle close) and `p_now_candle_ts` (its
    exchange open-ts, seconds): logging metadata only, never read by any
    decision. The candle pair is the iter-12 D1a shadow for the p_now
    staleness replay (p_now = newer of last-trade / latest candle close,
    refuse spot older than 180s).
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
            # p_now staleness shadow (iter-12 D1a): latest 1-min candle close
            # and its exchange open-ts, next to the last-trade p_now/p_now_ts
            # above. Logging only: never read by any entry/exit/grading
            # decision. None when the candle fetch came back empty.
            meta["p_now_candle"] = cs[-1][1] if cs else None
            meta["p_now_candle_ts"] = cs[-1][0] if cs else None
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
    """Mean per-signal P&L staking exactly $1 per signal at the mid-derived
    taken-side price (NOT the tradeable taken price).

    p = market_mid for YES takes, 1 - market_mid for NO takes. Gross:
    win -> (1-p)/p, loss -> -1. net=True additionally subtracts the
    Kalshi taker fee on the contracts $1 buys at that mid-derived price.
    Reporting only: never read by any decision. None when no row carries
    a market mid.
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


def calibrated_taken_price(mid, edge, side):
    """Shadow probability in 0–1 taken-side units; never used for decisions."""
    taken = mid if side == "YES" else 1 - mid
    # Post-agreement realized lift / claimed edge ≈ 4.3/9.9 ≈ 0.43;
    # 0.4 is the starting coefficient pending prospective validation.
    return round(taken + 0.4 * abs(edge), 3)


def edge_gate_ladder_shadow(edge):
    """Shadow gate flags for an emitted signal, using fractional taken edge."""
    edge_taken = abs(edge)
    # Chronological last 60% of 647 OOS Kalshi-settled rows, graded
    # 2026-10-02 through 2026-10-05: paper-mid payoff/signal was +7.38c
    # at >=5c (n=647, 76.0% wins), +10.22c at >=7c (n=427, 77.0%),
    # +13.83c at >=10c (n=257, 77.8%). Prospective experiment only:
    # validate total paper payoff per available window over full days.
    return {"gate_edge5_pass": True,
            "gate_edge7_pass": edge_taken >= 0.07,
            "gate_edge10_pass": edge_taken >= 0.10}


def executable_edge_shadow(edge, bid, ask):
    """Observed taken-edge after crossing half the spread and entry fee.

    Measurement only. The existing entry threshold remains the sole gate.
    """
    if not (0 < bid < ask < 1):
        return {"executable_edge": None, "gate_executable_edge_pass": None}
    entry = ask if edge > 0 else 1 - bid
    remaining = abs(edge) - (ask - bid) / 2 - _kalshi_taker_fee_dollars(1, entry)
    return {"executable_edge": round(remaining, 6),
            "gate_executable_edge_pass": remaining >= EDGE_THRESHOLD}


def accuracy_stats(grades):
    """Rolling stats over the last 50 Kalshi-settled signals.

    Only rows graded from Kalshi's published settlement (`proxy == "kalshi"`)
    count. Legacy Kraken-proxy rows (proxy == "kraken", flagged
    excluded_from_accuracy) are excluded: the proxy disagrees with Kalshi
    settlement ~20% of the time, so mixing them in would misstate accuracy.

    `post_filter` repeats the stats over the last 50 settled rows whose taken
    side agrees with the market lean (the population the agreement filter
    still admits); rows without a logged mid cannot be classified and are
    left out. `win_rate_priced` (added iter-19) reports the win rate over the
    mid-carrying population that `_mean_taken_price` and the `_pnl_*`
    helpers use, with `no_mid_rows` counting the excluded remainder; the
    legacy `win_rate` (all scored rows) is kept byte-identical for
    downstream consumers. Reporting only: nothing reads these numbers for
    any decision.
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
    # Matched-denominator population for R1 (iter-19): rows carrying a
    # market mid — the same population _mean_taken_price and the _pnl_*
    # helpers use.
    priced = [g for g in recent if g.get("market_mid") is not None]
    priced_wins = sum(1 for g in priced if g["correct"])
    calibrated = [g.get("fair_calibrated_taken") if g.get("fair_calibrated_taken") is not None
                  else calibrated_taken_price(g["market_mid"], g["edge"], g["side"])
                  for g in priced if g.get("edge") is not None]
    mid_payoffs = [(1.0 if g["correct"] else 0.0) -
                   (g["market_mid"] if g.get("side") == "YES" else 1 - g["market_mid"])
                   for g in priced]
    windows = {(g.get("ticker") or g.get("key", "").split(":")[0])
               for g in recent if g.get("ticker") or g.get("key")}
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
        # Matched-denominator companion to win_rate (iter-19 R1): the
        # mid-carrying population that _mean_taken_price and the _pnl_*
        # helpers use; `no_mid_rows` counts the excluded remainder. Legacy
        # keys above are untouched and byte-identical (see
        # test_accuracy_legacy_keys_byte_identical); new keys are appended
        # at the END only. Reporting only: never read by any decision.
        "no_mid_rows": len(recent) - len(priced),
        "win_rate_priced": (round(priced_wins / len(priced), 3)
                            if priced else None),
        "mean_calibrated_probability": (round(sum(calibrated) / len(calibrated), 3)
                                         if calibrated else None),
        "mean_paper_mid_payoff": (round(sum(mid_payoffs) / len(mid_payoffs), 3)
                                  if mid_payoffs else None),
        "independent_windows": len(windows),
        "entry_policy_cohort": (recent[0].get("entry_policy_cohort")
                                if recent and recent[0].get("entry_policy_cohort")
                                and all(g.get("entry_policy_cohort") ==
                                        recent[0]["entry_policy_cohort"] for g in recent)
                                else "mixed_or_legacy"),
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
            "fair_calibrated_taken": val.get("fair_calibrated_taken") if isinstance(val, dict) else None,
            "gate_edge5_pass": val.get("gate_edge5_pass") if isinstance(val, dict) else None,
            "gate_edge7_pass": val.get("gate_edge7_pass") if isinstance(val, dict) else None,
            "gate_edge10_pass": val.get("gate_edge10_pass") if isinstance(val, dict) else None,
            "executable_edge": val.get("executable_edge") if isinstance(val, dict) else None,
            "gate_executable_edge_pass": val.get("gate_executable_edge_pass") if isinstance(val, dict) else None,
            "xrp_shadow_suppressed": val.get("xrp_shadow_suppressed") if isinstance(val, dict) else None,
            "entry_policy_cohort": val.get("entry_policy_cohort") if isinstance(val, dict) else None,
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
            # p_now staleness shadow (iter-12 D1a): latest 1-min candle close
            # + its exchange ts + its alert-time age, plus the last-trade
            # p_now's own exchange ts. Additive metadata only: never read by
            # grading math. None on entries marked before they were logged.
            "p_now_candle": val.get("p_now_candle") if isinstance(val, dict) else None,
            "p_now_candle_ts": val.get("p_now_candle_ts") if isinstance(val, dict) else None,
            "p_now_ts": val.get("p_now_ts") if isinstance(val, dict) else None,
            "spot_age_candle_s": val.get("spot_age_candle_s") if isinstance(val, dict) else None,
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
            # alert-time Kalshi volume (iter-16 E.2), copied for future
            # liquidity slices. Additive metadata only: never read by
            # grading math. None on entries marked before it was logged.
            "volume": val.get("volume") if isinstance(val, dict) else None,
            # same-window concurrency at entry (iter-16, log-first shadow
            # for the W2 counterfactual). Additive metadata only: never
            # read by grading math. None on legacy/xvenue entries.
            "cluster_rank_at_entry": (
                val.get("cluster_rank_at_entry") if isinstance(val, dict) else None),
            "cluster_size_at_entry": (
                val.get("cluster_size_at_entry") if isinstance(val, dict) else None),
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


def append_exit_events(exits, alerted, path=None):
    """Best-effort append of newly stamped paper exits; never affects scanning."""
    try:
        path = path or os.path.join(DATA, "exit_events.jsonl")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with open(path, "a") as log:
                for e in exits:
                    stamped = alerted[e["signal_key"]]
                    rec = {
                        "signal_key": e["signal_key"], "ticker": e["ticker"],
                        "sym": e["ticker"].split("15M")[0].replace("KX", ""),
                        "side": e["side"], "entry_ts": e["entry_ts"],
                        "entry_price_taken": e["entry_price_taken"],
                        "entry_edge_taken": e["entry_edge_taken"],
                        "exit_ts": stamped["exit_ts"], "exit_reason": e["reason"],
                        "exit_price_taken": e["now_price_taken"],
                        "now_fair_taken": e["now_fair_taken"],
                        "now_edge": e["now_edge"],
                        "exit_yes_bid": e["exit_yes_bid"],
                        "exit_yes_ask": e["exit_yes_ask"],
                        "exit_bid_taken": e["exit_bid_taken"],
                        "quoted_spread_taken": e["quoted_spread_taken"],
                        "exit_direction": e["exit_direction"],
                        "mins_left": e["mins_left"],
                        "window_open_utc": stamped.get("window_open_utc"),
                        "window_close_utc": e["window_close_utc"],
                    }
                    log.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def check_exits(alerted, live_by_ticker, now_ts, mark, exit_event_path=None):
    """Re-price every still-open alerted 15m signal against the live market.

    Returns a list of exit dicts for signals whose taken-side edge has
    collapsed to EXIT_EDGE or below (thesis dead: edge gone or reversed).
    When mark is true, exited signals are stamped so they never re-fire.
    The exit-copilot advisory is NOT called here: callers attach it via
    attach_copilot_advice() after releasing _state_write_lock() (iter-20 R2
    residual), so the ~4s LLM call never holds the exclusive state lock.
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
        jobs.append((key, v, m, wc, mid, b, a,
                     (KRAKEN_PAIRS[sym], wo, wc, now_ts)))
    with _kraken_run_cache():
        fairs = _parallel_map(lambda j: fair_15m(*j[7]), jobs)
    for (key, v, m, wc, mid, b, a, _args), res in zip(jobs, fairs):
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
            "signal_key": key,
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
            # Kalshi's NO bid/ask complement the YES ask/bid respectively.
            # Quote-side paper prices are observations, never exit inputs.
            "exit_yes_bid": round(b, 3), "exit_yes_ask": round(a, 3),
            "entry_ask_taken": (v.get("ask") if v["side"] == "YES"
                                else round(1 - v["bid"], 3) if v.get("bid") is not None
                                else None),
            "exit_bid_taken": round(b if v["side"] == "YES" else 1 - a, 3),
            "quoted_spread_taken": round(a - b, 3),
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
    exits.sort(key=lambda e: (e["now_edge"], e["mins_left"]))
    if mark and exits:
        append_exit_events(exits, alerted, path=exit_event_path)
    return exits


def attach_copilot_advice(exits):
    """Advisory-only: attach a fast-LLM second opinion to the most urgent exit.

    Decision-neutral: never changes entry/exit decisions, only attaches
    {"decision","reason","latency_ms","backend"} to exits[0] or nothing.
    Enable with EXIT_COPILOT_ENABLED=1; kill switch: data/exit_copilot.kill.
    Never raises (any failure -> no attachment).

    MUST be called by the caller AFTER _state_write_lock() is released
    (iter-20 R2 residual): the LLM call takes up to ~4s and must not hold
    the exclusive state lock, which would serialize the exit-watch poll
    against the 5-min scan's mark phase. Callers gate on mark themselves so
    --no-mark runs stay fully read-only.
    """
    if not exits:
        return
    try:
        from exit_copilot import maybe_copilot_review
        advice = maybe_copilot_review(exits[0])
        if advice:
            exits[0]["copilot"] = advice
    except Exception:
        pass


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
    r = kalshi_get(f"{KALSHI_API}/markets/{ticker}", timeout=15)
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
        r = kalshi_get(f"{KALSHI_API}/markets/{ticker}/orderbook", timeout=10)
        r.raise_for_status()
        return book_imbalance(r.json())
    except Exception:
        return None


def annotate_cluster_rank(pending_entries):
    """Stamp cluster_rank_at_entry / cluster_size_at_entry on 15m entries.

    Groups the run's pending 15m entry records by int window_close_utc
    (xvenue entries carry none and are skipped); within a group, rank is
    1 + position in ticker-ascending order and size is the group length.
    In-memory only: mutates the rec dicts, writes nothing, and is called
    from the lock-free scan phase before the mark phase (never under the
    state lock). Log-first shadow (iter-16): never read by any entry/exit/
    grading decision.
    """
    groups = {}
    for _key, _sig, rec in pending_entries:
        wc = rec.get("window_close_utc")
        if isinstance(wc, int) and not isinstance(wc, bool):
            groups.setdefault(wc, []).append(rec)
    for recs in groups.values():
        recs.sort(key=lambda r: r.get("ticker") or "")
        for i, rec in enumerate(recs):
            rec["cluster_rank_at_entry"] = i + 1
            rec["cluster_size_at_entry"] = len(recs)


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


def update_persistence_shadow(state, observations, now_ts):
    """Advance the two-scan measurement; no result is used by signal logic.

    observations maps (ticker, window_close_utc) to unrounded YES mid/edge.
    The stored first scan survives scanner restarts. A missing or ineligible
    second scan becomes a hypothetical rejection after 90 seconds.
    """
    pending = state.setdefault("pending", {})
    completed = state.setdefault("completed", {})
    logs = []
    for key, first in list(pending.items()):
        if now_ts - first["ts"] < PERSISTENCE_SHADOW_DELAY_S:
            continue
        obs = observations.get((first["ticker"], first["window_close_utc"]))
        side = first["side"]
        second = None
        if obs is not None:
            second = {"edge_taken": round(obs["edge"] if side == "YES" else -obs["edge"], 3),
                      "mid_taken": round(obs["mid"] if side == "YES" else 1 - obs["mid"], 3),
                      "mins_left": round(obs["mins_left"], 1)}
        accepted = bool(second and second["edge_taken"] >= EDGE_THRESHOLD
                        and second["mid_taken"] >= AGREE_LEAN_MIN
                        and second["mins_left"] >= 2)
        logs.append({"signal_key": f'{first["ticker"]}:{side}',
                     "ticker": first["ticker"], "side": side,
                     "window_close_utc": first["window_close_utc"],
                     "first_seen_ts": first["ts"], "second_scan_ts": now_ts,
                     "first_scan": first["scan"], "second_scan": second,
                     "decision": "would_accept" if accepted else "would_reject"})
        completed[key] = first["window_close_utc"]
        del pending[key]
    for (ticker, wc), obs in observations.items():
        side = "YES" if obs["edge"] > 0 else "NO"
        edge_taken = abs(obs["edge"])
        mid_taken = obs["mid"] if side == "YES" else 1 - obs["mid"]
        if edge_taken < EDGE_THRESHOLD or mid_taken < AGREE_LEAN_MIN or obs["mins_left"] < 2:
            continue
        key = f"{ticker}:{side}:{wc}"
        if key not in pending and key not in completed:
            pending[key] = {"ticker": ticker, "side": side,
                            "window_close_utc": wc, "ts": now_ts,
                            "scan": {"edge_taken": round(edge_taken, 3),
                                     "mid_taken": round(mid_taken, 3),
                                     "mins_left": round(obs["mins_left"], 1)}}
    state["completed"] = {k: wc for k, wc in completed.items() if wc > now_ts}
    return logs


def record_persistence_shadow(observations, now_ts):
    """Best-effort shadow persistence; failure cannot affect paper signals."""
    try:
        os.makedirs(DATA, exist_ok=True)
        path = os.path.join(DATA, "persistence_shadow_state.json")
        with open(path + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with open(path) as f:
                    state = json.load(f)
            except (OSError, ValueError):
                state = {}
            logs = update_persistence_shadow(state, observations, now_ts)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, path)
            if logs:
                with open(os.path.join(DATA, "persistence_shadow_log.jsonl"), "a") as f:
                    for rec in logs:
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
    # Advisory only, and only after the lock is released: the LLM call must
    # not serialize against the scan's mark phase (iter-20 R2 residual).
    attach_copilot_advice(payload.get("exits") or [])
    return payload


def exits_only_run(state, alerted, now_ts, mark):
    """Exit-watch mode: re-price only open signals, print exits as JSON."""
    if mark:
        # Serialize writers: the locked path re-reads state from disk so
        # two concurrent polls cannot both stamp+emit the same exit.
        payload = exits_only_locked(now_ts)
    else:
        payload = _exits_only_payload(alerted, now_ts, mark=False)
    payload["kalshi_requests"] = dict(kalshi_request_stats)
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
    ap.add_argument("--refresh-sweep-cache", action="store_true",
                    help="background worker: full end-of-day sweep, atomic "
                         "cache write; never touches state, grades, or alerts")
    ap.add_argument("--sweep-date", default=None,
                    help="ET date (YYYY-MM-DD) for --refresh-sweep-cache; "
                         "defaults to today")
    args = ap.parse_args()
    mark = not args.no_mark

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    from zoneinfo import ZoneInfo as _ZI
    today_et = now.astimezone(_ZI("America/New_York")).strftime("%Y-%m-%d")
    now_ts = int(now.timestamp())
    if args.refresh_sweep_cache:
        _refresh_sweep_cache(args.sweep_date or today_et)
        return
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
            print(json.dumps({"kalshi_requests": dict(kalshi_request_stats), "ok": False, "error": f"grades_unavailable: {e}"}))
            return
        print(json.dumps({"kalshi_requests": dict(kalshi_request_stats), "ok": True, "as_of_utc": now.isoformat(),
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
        print(json.dumps({"kalshi_requests": dict(kalshi_request_stats), "ok": False, "error": f"kalshi fetch failed: {e}"}))
        return
    fetch_failures = getattr(kmkts, "failures", [])
    notes.extend(getattr(kmkts, "cache_notes", []))
    notes.extend(f"kalshi_degraded={failure}" for failure in fetch_failures)
    if fetch_failures and not kmkts:
        print(json.dumps({"kalshi_requests": dict(kalshi_request_stats), "ok": False, "error": "no usable Kalshi markets fetched",
                          "notes": notes}))
        return
    notes.append(f"kalshi_same_day_markets={len(kmkts)}")

    # ---- 2. 15-min crypto windows live right now ----
    live15 = []
    # Per-coin model snapshot for the flip analyst (additive only: never
    # read by entry/exit/grading logic). Lets the analyst label its calls
    # MODEL+TAPE (model edge agrees with the tape) vs TAPE-ONLY.
    fair_snap = {}
    persistence_observations = {}
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
        persistence_observations[(m["ticker"], wc)] = {
            "mid": mid, "edge": edge, "mins_left": t}
        # Snapshot every priced window (even below-threshold: the analyst
        # needs to know when there is NO model edge too). Additive only.
        fair_snap[sym] = {
            "ticker": m["ticker"], "mid": round(mid, 3),
            "fair": round(fair, 3), "edge": round(edge, 3),
            "abs_edge": round(abs(edge), 3),
            "model_side": "YES" if edge > 0 else "NO",
            "has_signal": abs(edge) >= EDGE_THRESHOLD,
            "spread": round(a - b, 3), "mins_left": round(t, 1),
        }
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
        fair_calibrated_taken = calibrated_taken_price(mid, edge, side)
        gate_shadow = edge_gate_ladder_shadow(edge)
        quote_gate_shadow = executable_edge_shadow(edge, b, a)
        # XRP post-agreement was -5.4c mean mid payoff on 84 signals;
        # this is a prospective experiment, not a filter.
        xrp_shadow_suppressed = sym == "XRP"
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
                "fair_calibrated_taken": fair_calibrated_taken,
                **gate_shadow,
                **quote_gate_shadow,
                "xrp_shadow_suppressed": xrp_shadow_suppressed,
                "entry_policy_cohort": "post-agreement",
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
                # p_now staleness shadow (iter-12 D1a): the latest 1-min
                # candle close, its exchange ts, its age at alert time, and
                # the last-trade p_now's own exchange ts, next to the
                # last-trade p_now. Together with spot_age_s these make a
                # grade row self-contained for the future staleness-guard
                # replay: p_now' = p_now_candle iff p_now_candle_ts >
                # p_now_ts, and the spot is refused when its alert-time age
                # (spot_age_s) or fetch-time age (spot_age_fetch_s) exceeds
                # 180s. (spot_age_candle_s is the interval-open ts, not an
                # age — it can be negative; never gate a rule on it directly.)
                # Additive
                # metadata only: never read by any entry/exit decision.
                "p_now_candle": (
                    fetch_meta["p_now_candle"]
                    if isinstance(fetch_meta.get("p_now_candle"), (int, float))
                    else None),
                "p_now_candle_ts": (
                    fetch_meta["p_now_candle_ts"]
                    if isinstance(fetch_meta.get("p_now_candle_ts"), (int, float))
                    else None),
                "p_now_ts": (
                    int(p_now_ts) if isinstance(p_now_ts, (int, float)) else None),
                "spot_age_candle_s": (
                    int(round(now_ts - fetch_meta["p_now_candle_ts"]))
                    if isinstance(fetch_meta.get("p_now_candle_ts"), (int, float))
                    else None),
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
                # alert-time Kalshi volume, same value as the printed
                # signal's "volume" (iter-16 E.2). Additive metadata only:
                # never read by any entry/exit/grading decision.
                "volume": num(m.get("volume_fp")),
            }))

    if mark:
        record_persistence_shadow(persistence_observations, now_ts)

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

    # ---- 3b. Same-window cluster rank (log-first shadow, iter-16) ----
    # Still lock-free scan phase. Annotates only the pending entry records
    # (empty under --no-mark, so nothing is written there); never touches
    # sig or the printed payload, and never read by any entry/exit/grading
    # decision.
    annotate_cluster_rank(pending_entries)

    # ---- 4. Mark phase: merge new entries + stamp exits atomically ----
    # The whole scan above ran lock-free (network phase). Only this short
    # re-read -> merge -> decide -> save sequence holds the exclusive state
    # lock, so a concurrent exit-watch poll cannot interleave and double-fire.
    if mark:
        with _state_write_lock():
            fresh = load_state()  # fresh read inside the lock: no stale reads
            fresh_alerted = fresh.get("alerted", {})
            exit_eligible = dict(fresh_alerted)  # shared records, pre-merge keys
            for key, sig, rec in pending_entries:
                if key in fresh_alerted:
                    sig["new"] = False  # lost the race: already alerted
                else:
                    fresh_alerted[key] = rec
            exits = check_exits(exit_eligible, live_by_ticker, now_ts,
                                mark=True)
            save_state({"alerted": fresh_alerted})
        alerted = fresh_alerted
        # Advisory only, after the lock is released: the LLM call must not
        # hold the exclusive state lock (iter-20 R2 residual). Gated on mark
        # so --no-mark runs stay fully read-only (iter-19 R2 gating kept).
        attach_copilot_advice(exits)
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
    scan_ok = not fetch_failures or bool(fair_snap or signals)
    print(json.dumps({"kalshi_requests": dict(kalshi_request_stats),
        "ok": scan_ok,
        **({"error": "degraded fetch left nothing usable priced"} if not scan_ok else {}),
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
    # Model fair-value snapshot for the flip analyst (additive only: never
    # read by entry/exit/grading logic; a failed write must never break the
    # scan). Lets the analyst label calls MODEL+TAPE vs TAPE-ONLY.
    # Gated on mark so --no-mark stays fully read-only.
    if mark:
        write_fair_snapshot(DATA, fair_snap, now.isoformat(), now_ts)


def write_fair_snapshot(data_dir, fair_snap, now_iso, now_ts):
    """Atomic write of the per-coin model fair/edge snapshot for the flip
    analyst. Additive only: never read by entry/exit/grading logic, and a
    failed write must never raise (the scan must not break)."""
    try:
        tmp = os.path.join(data_dir, "latest_fair.json.tmp")
        with open(tmp, "w") as f:
            json.dump({"as_of_utc": now_iso, "as_of_ts": now_ts,
                       "coins": fair_snap}, f)
        os.replace(tmp, os.path.join(data_dir, "latest_fair.json"))
    except Exception:
        pass


if __name__ == "__main__":
    main()
