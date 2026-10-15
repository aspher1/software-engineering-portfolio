"""Polymarket live adapter — STUB.

To go live on Polymarket you must implement:
  - CLOB API auth (private key + API credentials derivation)
  - place_order → signed POST to the CLOB endpoint
  - US persons are restricted from real-money Polymarket trading; the adapter
    must enforce a jurisdiction check before any order.

Until then, every method raises LiveTradingDisabled. This is deliberate:
there is no half-wired live path that could move real money by accident.
"""

from __future__ import annotations

import os

from .base import BrokerAdapter, Fill, LiveTradingDisabled, Order, Position


class PolymarketLiveAdapter(BrokerAdapter):
    venue = "polymarket"
    is_live = True

    def __init__(self, config: dict):
        self.require_live_enabled(config)
        if not os.environ.get("POLYMARKET_PRIVATE_KEY"):
            raise LiveTradingDisabled(
                "POLYMARKET_PRIVATE_KEY not set in environment. Live "
                "Polymarket trading requires a funded wallet + CLOB "
                "credentials — see .env.example."
            )
        raise NotImplementedError(
            "PolymarketLiveAdapter order routing is not implemented yet. "
            "Implement CLOB signing + jurisdiction check before enabling."
        )

    def place_order(self, order: Order) -> Fill:
        raise NotImplementedError

    def get_price(self, market_id: str) -> float:
        raise NotImplementedError

    def get_open_positions(self) -> list[Position]:
        raise NotImplementedError
