"""Fair-value model for Kalshi 15-minute crypto up/down windows (paper only).

Ported from a companion project's scripts/same_day_loop.py (phi, fair_15m,
shrink_midrange, agrees_with_lean, in_warmup, parse_15m_window, kraken_trades,
kraken_candles, KRAKEN_PAIRS). Copied, not imported: the two repos stay
independent. No scanner imports here; the pure functions never touch the
network.

A KX{SYM}15M window pays YES when spot ends the window above its opening
reference. Zero-drift Brownian fair value of YES:
    fair_raw = Phi(lr / (sigma * sqrt(t)))
    lr    = log(p_now / p_ref)
    p_ref = mean Kraken trade price in [w_open - 60, w_open)
    p_now = latest Kraken spot
    sigma = RMS of the trailing 60 one-minute Kraken log returns
    t     = max(minutes left, 0.5)

Kraken endpoints are public and keyless. No orders, no credentials.
"""

from __future__ import annotations

import math
import re
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

KRAKEN_API = "https://api.kraken.com/0/public"
KRAKEN_PAIRS = {
    "BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD",
    "DOGE": "DOGEUSD", "HYPE": "HYPEUSD", "ZEC": "ZECUSD",
    "NEAR": "NEARUSD", "BNB": "BNBUSD",
}
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
WINDOW_S = 900
AGREE_LEAN_MIN = 0.5           # min taken-side price (drops contrarian takes)
WINDOW_WARMUP_MIN = 3.0        # opening print + fresh book are unsettled before this
SHRINK_ALPHA, SHRINK_LO, SHRINK_HI = 0.25, 0.40, 0.60
REF_WINDOW_S = 60              # p_ref = mean trade price in [w_open - 60, w_open)
TRADES_LOOKBACK_S = 300        # trades are fetched since w_open - 300
SIGMA_RETURNS = 60             # trailing one-minute log returns in sigma
MIN_CANDLES = 70
MIN_T_MIN = 0.5
# Kraken's Trades endpoint returns at most 1000 trades from `since`; on busy
# pairs (BTC) that ends minutes before now, so the newest 1-min candle close
# is used when it is newer, and a spot older than this is refused.
SPOT_MAX_AGE_S = 180
KRAKEN_TIMEOUT = 20
KRAKEN_ATTEMPTS = 2

_TICKER_RE = re.compile(r"^KX([A-Z]+)15M-")


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def fair_15m(pair, w_open, w_close, now_ts, trades, candles):
    """(fair_prob, p_ref, p_now, sigma_per_min, mins_left) or None.

    trades: Kraken Trades rows [price, volume, time, ...] fetched since
    w_open - TRADES_LOOKBACK_S. candles: [(open_ts, close)] one-minute OHLC.
    `pair` is informational only. Any malformed input -> None.
    """
    try:
        rows = [(float(t[0]), float(t[2])) for t in trades]
        ref = [p for p, ts in rows if w_open - REF_WINDOW_S <= ts < w_open]
        if not ref:
            return None
        p_ref = sum(ref) / len(ref)
        p_now, p_now_ts = rows[-1]
        if len(candles) < MIN_CANDLES:
            return None
        cs = [(float(ts), float(c)) for ts, c in candles]
        if cs[-1][0] > p_now_ts:
            p_now_ts, p_now = cs[-1]
        if now_ts - p_now_ts > SPOT_MAX_AGE_S or p_ref <= 0 or p_now <= 0:
            return None
        rets = [math.log(cs[i + 1][1] / cs[i][1])
                for i in range(len(cs) - SIGMA_RETURNS - 1, len(cs) - 1)]
        sigma = math.sqrt(sum(x * x for x in rets) / len(rets))
        if not sigma > 0:
            return None
        t = max((w_close - now_ts) / 60.0, MIN_T_MIN)
        z = math.log(p_now / p_ref) / (sigma * math.sqrt(t))
        return phi(z), p_ref, p_now, sigma, t
    except Exception:
        return None


def shrink_midrange(fair, mid, alpha=SHRINK_ALPHA, lo=SHRINK_LO, hi=SHRINK_HI):
    """Shrink fair toward the market mid, but ONLY when fair is in [lo, hi],
    where the model is known-overconfident. Never flips the signal side: the
    shrunk fair always lies between the raw fair and mid."""
    if not (lo <= fair <= hi):
        return fair
    return fair + alpha * (mid - fair)


def agrees_with_lean(side, mid):
    """True when the taken-side price (mid for YES, 1 - mid for NO) is >= 0.5,
    i.e. the model is not fading a market that leans the other way."""
    taken = mid if side == "YES" else 1 - mid
    return taken >= AGREE_LEAN_MIN


def in_warmup(w_open, now_ts, warmup_min=WINDOW_WARMUP_MIN):
    """True while the window's opening print is still unsettled."""
    return (now_ts - w_open) < warmup_min * 60


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
        close_et = datetime(2000 + int(yy), mo, int(dd), int(hh), int(mi),
                            tzinfo=ZoneInfo("America/New_York"))
    except ValueError:
        return None
    close_utc = int(close_et.timestamp())
    return sym, close_utc - WINDOW_S, close_utc


def _iso_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def market_window(m):
    """(sym, w_open, w_close) for a listing row of a supported 15-min crypto
    window, or None. Prefers the listing's open_time/close_time (when they
    span exactly one window AND equal the ticker's own window; a mismatch
    fails closed); otherwise parses the ticker."""
    ticker = m.get("ticker") or ""
    mm = _TICKER_RE.match(ticker)
    if not mm or mm.group(1) not in KRAKEN_PAIRS:
        return None
    parsed = parse_15m_window(ticker)
    wo, wc = _iso_ts(m.get("open_time")), _iso_ts(m.get("close_time"))
    if wo is not None and wc is not None and int(wc) - int(wo) == WINDOW_S:
        if parsed is None or (int(wo), int(wc)) != (parsed[1], parsed[2]):
            return None
        return mm.group(1), int(wo), int(wc)
    if parsed is None or parsed[0] != mm.group(1):
        return None
    return parsed


# ----------------------------------------------------------------- kraken ----

def _plain_get_json(url, params):
    last = None
    for attempt in range(KRAKEN_ATTEMPTS):
        try:
            r = requests.get(url, params=params, timeout=KRAKEN_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            if attempt + 1 < KRAKEN_ATTEMPTS:
                time.sleep(0.5)
    raise last


def _kraken_result(endpoint, params, http=None):
    """Result rows of a public Kraken endpoint. http(url, params) -> JSON may
    be injected (the scanner passes its semaphore-bounded getter)."""
    d = (http or _plain_get_json)(f"{KRAKEN_API}/{endpoint}", params)
    if d.get("error"):
        raise RuntimeError(f"kraken {endpoint}: {'; '.join(map(str, d['error']))}")
    res = d.get("result") or {}
    key = next((k for k in res if k != "last"), None)
    return res.get(key, []) if key else []


def kraken_trades(pair, since_ts, http=None):
    """Raw Kraken trades [price, volume, time, ...] since since_ts (int seconds)."""
    return _kraken_result("Trades", {"pair": pair, "since": int(since_ts)}, http)


def kraken_candles(pair, http=None):
    """One-minute candles as [(open_ts, close)]."""
    return [(int(c[0]), float(c[4]))
            for c in _kraken_result("OHLC", {"pair": pair, "interval": 1}, http)]


class KrakenCache:
    """Kraken trades/candles for one run, keyed by pair. Each pair is fetched
    at most once (two calls); a failed pair stays absent."""

    def __init__(self):
        self._data, self._lock = {}, threading.Lock()

    def put(self, pair, trades, candles):
        with self._lock:
            self._data[pair] = (trades, candles)

    def get(self, pair):
        """(trades, candles) or None when the pair was not fetched."""
        with self._lock:
            return self._data.get(pair)

    def fetch(self, pair, since_ts, http=None):
        """Fetch a pair once. Returns an error string, or None on success."""
        if self.get(pair) is not None:
            return None
        try:
            trades = kraken_trades(pair, since_ts, http)
            candles = kraken_candles(pair, http)
        except Exception as e:
            return f"kraken {pair}: {type(e).__name__}: {e}"
        self.put(pair, trades, candles)
        return None
