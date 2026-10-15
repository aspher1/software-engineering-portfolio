"""BrokerAdapter: the single seam between paper trading and live trading.

Every venue implements this interface. The run loop only ever talks to a
BrokerAdapter, so paper mode and live mode share the exact same trading logic.
The difference is entirely in the adapter — and the live adapters refuse to
operate unless explicitly enabled with credentials present.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


class LiveTradingDisabled(RuntimeError):
    """Raised whenever live order flow is attempted without explicit opt-in."""


@dataclass
class Order:
    market_id: str
    venue: str
    side: str            # "YES" or "NO"
    size_usd: float
    limit_price: float   # 0..1 probability price
    strategy_version: str = ""
    signal_id: str = ""


@dataclass
class Fill:
    order: Order
    fill_price: float
    fill_size_usd: float
    timestamp: str = ""
    live: bool = False
    venue_order_id: Optional[str] = None


@dataclass
class Position:
    market_id: str
    venue: str
    side: str
    size_usd: float
    entry_price: float
    current_price: float = 0.0
    status: str = "open"          # open | resolved_win | resolved_loss
    strategy_version: str = ""
    signal_id: str = ""

    def pnl(self) -> float:
        # Binary contract settles at 1.0 (win) or 0.0 (loss).
        if self.status == "resolved_win":
            return self.size_usd * (1.0 - self.entry_price) / self.entry_price
        if self.status == "resolved_loss":
            return -self.size_usd
        # mark to market while open
        return self.size_usd * (self.current_price - self.entry_price) / self.entry_price


class BrokerAdapter(ABC):
    """Interface shared by paper and live adapters."""

    venue: str = ""
    is_live: bool = False

    @abstractmethod
    def place_order(self, order: Order) -> Fill: ...

    @abstractmethod
    def get_price(self, market_id: str) -> float: ...

    @abstractmethod
    def get_open_positions(self) -> list[Position]: ...

    def require_live_enabled(self, config: dict) -> None:
        if not config.get("mode", {}).get("live_trading", False):
            raise LiveTradingDisabled(
                f"{type(self).__name__}: live trading is disabled. "
                "Set mode.live_trading=true in config.yaml AND provide API "
                "credentials to enable real orders."
            )
