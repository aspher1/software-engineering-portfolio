"""Trader: turns signals into paper orders with Kelly sizing, hard caps, and a
full journal. Never touches live adapters — the run loop wires the broker."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .adapters.base import Order
from .adapters.paper import PaperBroker
from .mispricing import Signal


def kelly_fraction(p_win: float, price: float) -> float:
    """Fraction of bankroll the Kelly criterion suggests for a binary bet.

    price = cost of the side we're buying (0..1). Payout is 1.0 on win.
    b = net odds = (1 - price) / price.
    """
    if price <= 0 or price >= 1 or p_win <= price:
        return 0.0
    b = (1.0 - price) / price
    f = (b * p_win - (1.0 - p_win)) / b
    return max(0.0, min(1.0, f))


class Trader:
    def __init__(self, config: dict, broker: PaperBroker, data_dir: str):
        self.cfg = config
        self.broker = broker
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.journal_path = os.path.join(data_dir, "journal.jsonl")
        self._trades_today = 0
        self._day = datetime.now(timezone.utc).date().isoformat()

    def _reset_day(self):
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self._day:
            self._day, self._trades_today = today, 0

    def bankroll(self) -> float:
        base = float(self.cfg["strategy"]["bankroll"])
        realized = sum(
            p.pnl() for p in self._all_positions() if p.status != "open"
        )
        return base + realized

    def _all_positions(self):
        # PaperBroker keeps positions internally; reach in (same package).
        return list(self.broker._positions.values())

    def execute(self, signals: list[Signal]) -> list[dict]:
        self._reset_day()
        st = self.cfg["strategy"]
        executed: list[dict] = []
        for sig in signals:
            if len(self.broker.get_open_positions()) >= st["max_open_positions"]:
                break
            if self._trades_today >= st["daily_trade_cap"]:
                break
            # No duplicate open position on the same market+side.
            if any(p.market_id == sig.market_id and p.side == sig.side
                   for p in self.broker.get_open_positions()):
                continue
            frac = kelly_fraction(sig.fair_price, sig.our_price)
            frac *= float(st["kelly_fraction"])
            size = self.bankroll() * frac
            size = min(size, self.bankroll() * float(st["max_risk_per_trade"]))
            if size < 1.0:
                continue  # dust — skip
            order = Order(
                market_id=sig.market_id, venue=sig.venue, side=sig.side,
                size_usd=round(size, 2), limit_price=sig.our_price,
                strategy_version=sig.strategy_version, signal_id=sig.signal_id,
            )
            fill = self.broker.place_order(order)
            self._trades_today += 1
            entry = {
                "ts": fill.timestamp, "type": "fill",
                "signal_id": sig.signal_id, "market_id": sig.market_id,
                "venue": sig.venue, "side": sig.side,
                "our_price": sig.our_price, "fair_price": sig.fair_price,
                "gap": sig.gap, "expected_edge": sig.expected_edge,
                "fill_price": fill.fill_price, "size_usd": fill.fill_size_usd,
                "strategy_version": sig.strategy_version, "live": False,
                "kelly_fraction_used": float(st["kelly_fraction"]),
            }
            self._journal(entry)
            executed.append(entry)
        return executed

    def grade_resolutions(self, fetch_resolution) -> list[dict]:
        """Ask fetch_resolution(market_id, side) -> Optional[bool won].
        Resolves open paper positions and journals the grades."""
        fills_by_signal = {e["signal_id"]: e for e in self.journal_entries("fill")}
        graded: list[dict] = []
        for pos in self.broker.get_open_positions():
            won = fetch_resolution(pos.market_id, pos.side)
            if won is None:
                continue
            self.broker.resolve(pos.market_id, pos.side, won)
            fill = fills_by_signal.get(pos.signal_id, {})
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(), "type": "grade",
                "signal_id": pos.signal_id, "market_id": pos.market_id,
                "side": pos.side, "won": won, "pnl": round(pos.pnl(), 2),
                "gap": fill.get("gap", 0.0),
                "kelly_fraction_used": fill.get("kelly_fraction_used", 0.0),
                "strategy_version": pos.strategy_version, "live": False,
            }
            self._journal(entry)
            graded.append(entry)
        return graded

    def _journal(self, entry: dict) -> None:
        with open(self.journal_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def journal_entries(self, type_: str) -> list[dict]:
        if not os.path.exists(self.journal_path):
            return []
        out = []
        with open(self.journal_path) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") == type_:
                    out.append(e)
        return out
