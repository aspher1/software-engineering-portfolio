"""Paper broker: simulates fills against public quotes. No money moves."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .base import BrokerAdapter, Fill, Order, Position


class PaperBroker(BrokerAdapter):
    venue = "paper"
    is_live = False

    def __init__(self, price_fn, slippage: float = 0.002):
        # price_fn(market_id) -> current YES price 0..1; injected so tests
        # and the run loop can wire it to the scanner.
        self._price_fn = price_fn
        self._slippage = slippage
        self._positions: dict[str, Position] = {}
        self.fills: list[Fill] = []

    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def get_price(self, market_id: str) -> float:
        return self._price_fn(market_id)

    def place_order(self, order: Order) -> Fill:
        quote = self.get_price(order.market_id)
        # Paper fill: pay the quote plus a slippage haircut, always against us.
        fill_price = min(0.99, quote + self._slippage) if order.side == "YES" \
            else min(0.99, (1.0 - quote) + self._slippage)
        fill = Fill(
            order=order, fill_price=fill_price, fill_size_usd=order.size_usd,
            timestamp=self.now(), live=False,
            venue_order_id=f"paper-{uuid.uuid4().hex[:12]}",
        )
        self.fills.append(fill)
        pos = Position(
            market_id=order.market_id, venue=order.venue, side=order.side,
            size_usd=order.size_usd, entry_price=fill_price,
            current_price=fill_price, strategy_version=order.strategy_version,
            signal_id=order.signal_id,
        )
        self._positions[order.market_id + ":" + order.side] = pos
        return fill

    def get_open_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.status == "open"]

    def mark_to_market(self) -> None:
        for key, pos in self._positions.items():
            if pos.status != "open":
                continue
            quote = self.get_price(pos.market_id)
            pos.current_price = quote if pos.side == "YES" else 1.0 - quote

    def resolve(self, market_id: str, side: str, won: bool) -> Position | None:
        key = market_id + ":" + side
        pos = self._positions.get(key)
        if pos is None or pos.status != "open":
            return None
        pos.status = "resolved_win" if won else "resolved_loss"
        return pos
