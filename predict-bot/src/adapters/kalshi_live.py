"""Kalshi live adapter — STUB.

To go live on Kalshi you must implement:
  - RSA auth (KALSHI_KEY_ID + private key) per Kalshi API docs
  - place_order → POST /portfolio/orders (limit orders)
  - cancel/reconcile, balance checks, fill polling

Until then, every method raises LiveTradingDisabled. This is deliberate:
there is no half-wired live path that could move real money by accident.
"""

from __future__ import annotations

import os

from .base import BrokerAdapter, Fill, LiveTradingDisabled, Order, Position


class KalshiLiveAdapter(BrokerAdapter):
    venue = "kalshi"
    is_live = True

    def __init__(self, config: dict):
        self.require_live_enabled(config)
        if not os.environ.get("KALSHI_KEY_ID"):
            raise LiveTradingDisabled(
                "KALSHI_KEY_ID not set in environment. Live Kalshi trading "
                "requires API credentials — see .env.example."
            )
        raise NotImplementedError(
            "KalshiLiveAdapter order routing is not implemented yet. "
            "Implement RSA auth + POST /portfolio/orders before enabling."
        )

    def place_order(self, order: Order) -> Fill:
        raise NotImplementedError

    def get_price(self, market_id: str) -> float:
        raise NotImplementedError

    def get_open_positions(self) -> list[Position]:
        raise NotImplementedError
