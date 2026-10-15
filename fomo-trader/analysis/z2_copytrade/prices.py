"""Minute close price paths. A close is usable only at candle start + 60 s."""
from abc import ABC, abstractmethod
from bisect import bisect_left
from math import isfinite
import time
from urllib.parse import quote, urlencode
from .sources import TokenBucket, _get_json


class PricePath(ABC):
    @abstractmethod
    def price_at(self, mint: str, decision_ts: float) -> float | None: ...


class GeckoTerminalPricePath(PricePath):
    """Pool map must be fixed before replay; mint is never assumed to be a pool.

    Public API is documented at roughly 10 requests/minute; bucket defaults to
    10 and is hard-capped at 20. OHLCV minute close is an approximation, not an
    executable quote. Missing bars or API errors fail open by returning None.
    """
    def __init__(self, pool_by_mint, requester=None, per_minute=10, max_wait_s=60):
        self.pool_by_mint = dict(pool_by_mint)
        self.requester = requester or _get_json
        self.bucket = TokenBucket(per_minute)
        self.max_wait_s = max_wait_s
        self.cache = {}
        self.missing = 0
        self.errors = 0
        self.circuit_until = 0.0

    def _load(self, mint, until):
        pool = self.pool_by_mint.get(mint)
        if not pool:
            self.missing += 1
            return [], []
        if time.monotonic() < self.circuit_until:
            self.errors += 1
            return [], []
        self.bucket.acquire()
        url = ('https://api.geckoterminal.com/api/v2/networks/solana/pools/'
               + quote(pool, safe='') + '/ohlcv/minute?' + urlencode({'aggregate': 1, 'limit': 1000, 'before_timestamp': int(until + 120), 'currency': 'token'}))
        try:
            payload = self.requester(url)
            rows = payload['data']['attributes']['ohlcv_list']
            bars = sorted((float(row[0]) + 60, float(row[4])) for row in rows
                          if len(row) >= 5 and isfinite(float(row[4])) and float(row[4]) > 0)
            return [t for t, _ in bars], [p for _, p in bars]
        except Exception:
            self.errors += 1
            self.circuit_until = time.monotonic() + 60
            return [], []

    def price_at(self, mint, decision_ts):
        if not isinstance(decision_ts, (int, float)) or not isfinite(decision_ts):
            self.missing += 1
            return None
        if mint not in self.cache or (self.cache[mint][0] and decision_ts > self.cache[mint][0][-1]):
            self.cache[mint] = self._load(mint, decision_ts)
        times, prices = self.cache[mint]
        index = bisect_left(times, decision_ts)
        if index >= len(times) or times[index] - decision_ts > self.max_wait_s:
            self.missing += 1
            return None
        return prices[index]
