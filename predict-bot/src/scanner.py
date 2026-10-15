"""Market scanner: pulls public market data from Polymarket (Gamma API, no auth)
and Kalshi (public market list). Normalizes into Market objects."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import requests


@dataclass
class Market:
    venue: str
    market_id: str
    question: str
    yes_price: float          # 0..1
    volume_usd: float = 0.0
    liquidity_usd: float = 0.0
    category: str = ""
    end_date: str = ""
    resolved: bool = False
    resolution: str = ""      # "YES" | "NO" | ""


def normalize_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # drop common filler words that differ between venues
    stop = {"will", "the", "a", "an", "be", "by", "of", "in", "on", "to",
            "for", "is", "it", "its", "at", "or", "and", "vs"}
    return " ".join(w for w in t.split() if w not in stop)


class PolymarketScanner:
    def __init__(self, gamma_api: str, timeout: int = 20):
        self.gamma_api = gamma_api.rstrip("/")
        self.timeout = timeout

    def fetch_markets(self, limit: int = 300) -> list[dict[str, Any]]:
        # Gamma public endpoint: active, non-closed events, each nesting its
        # binary markets. Sorted by 24h volume.
        params = {
            "active": "true",
            "closed": "false",
            "limit": min(limit, 500),
            "order": "volume24hr",
            "ascending": "false",
        }
        r = requests.get(f"{self.gamma_api}/events",
                         params=params, timeout=self.timeout)
        r.raise_for_status()
        events = r.json()
        markets: list[dict[str, Any]] = []
        for ev in events:
            for m in ev.get("markets", []):
                m = dict(m)
                m.setdefault("category", (ev.get("tags") or [{}])[0].get("label", "")
                             if ev.get("tags") else "")
                # Per-market liquidity/volume often live only at event level.
                m.setdefault("liquidity", ev.get("liquidity") or 0)
                if not m.get("volume"):
                    m["volume"] = ev.get("volume") or 0
                markets.append(m)
                if len(markets) >= limit:
                    return markets
        return markets

    def get_market(self, market_id: str) -> dict[str, Any]:
        r = requests.get(f"{self.gamma_api}/markets/{market_id}",
                         timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _is_binary(raw: dict[str, Any]) -> bool:
        outcomes = raw.get("outcomes")
        if isinstance(outcomes, str):
            import json
            try:
                outcomes = json.loads(outcomes)
            except json.JSONDecodeError:
                return False
        return isinstance(outcomes, list) and len(outcomes) == 2 and \
            {str(o).lower() for o in outcomes} == {"yes", "no"}

    @staticmethod
    def to_market(raw: dict[str, Any]) -> Market | None:
        try:
            if not PolymarketScanner._is_binary(raw):
                return None
            prices = raw.get("outcomePrices")
            if isinstance(prices, str):
                import json
                prices = json.loads(prices)
            yes_price = float(prices[0]) if prices else 0.5
            if not 0.01 <= yes_price <= 0.99:
                return None  # skip near-certain / dust markets
            return Market(
                venue="polymarket",
                market_id=str(raw.get("id") or raw.get("conditionId") or ""),
                question=str(raw.get("question", "")),
                yes_price=yes_price,
                volume_usd=float(raw.get("volume") or 0),
                liquidity_usd=float(raw.get("liquidity") or 0),
                category=str(raw.get("category") or raw.get("groupItemTitle") or ""),
                end_date=str(raw.get("endDate") or ""),
                resolved=bool(raw.get("closed")),
            )
        except (ValueError, TypeError, IndexError):
            return None

    def scan(self, cfg: dict) -> list[Market]:
        sc = cfg.get("scanner", {})
        raw = self.fetch_markets(limit=sc.get("max_markets_per_scan", 300))
        out: list[Market] = []
        excluded = {c.lower() for c in sc.get("exclude_categories", [])}
        for r in raw:
            m = self.to_market(r)
            if m is None:
                continue
            if m.volume_usd < sc.get("min_volume_usd", 0):
                continue
            if m.liquidity_usd < sc.get("min_liquidity_usd", 0):
                continue
            if m.category.lower() in excluded:
                continue
            out.append(m)
        return out

    def live_price(self, market_id: str) -> float:
        m = self.to_market(self.get_market(market_id))
        return m.yes_price if m else 0.5


class KalshiScanner:
    """Reference-price source. Market list endpoint is public; if it ever
    requires auth or fails, the mispricing detector simply skips cross-venue
    checks instead of trading blind."""

    def __init__(self, api: str, timeout: int = 20):
        self.api = api.rstrip("/")
        self.timeout = timeout
        self._cache: list[Market] = []
        self.available = True

    def refresh(self, limit: int = 1000) -> None:
        try:
            params = {"limit": min(limit, 1000), "status": "open",
                      "min_close_ts": 0}
            r = requests.get(f"{self.api}/markets", params=params,
                             timeout=self.timeout)
            r.raise_for_status()
            data = r.json().get("markets", [])
        except Exception:
            self.available = False
            return
        out: list[Market] = []
        for m in data:
            try:
                bid = m.get("yes_bid_dollars")
                ask = m.get("yes_ask_dollars")
                if bid is not None and ask is not None:
                    price = (float(bid) + float(ask)) / 2.0
                else:
                    last = m.get("last_price_dollars")
                    price = float(last) if last else 0.0
                if not 0.01 <= price <= 0.99:
                    continue
                out.append(Market(
                    venue="kalshi",
                    market_id=str(m.get("ticker", "")),
                    question=str(m.get("title", "")),
                    yes_price=price,
                    volume_usd=float(m.get("volume_fp", 0) or 0),
                    liquidity_usd=float(m.get("liquidity_dollars", 0) or 0),
                    category=str(m.get("category") or ""),
                    end_date=str(m.get("close_time") or ""),
                ))
            except (ValueError, TypeError):
                continue
        self._cache = out
        self.available = True

    def markets(self) -> list[Market]:
        return self._cache
