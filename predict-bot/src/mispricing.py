"""Mispricing detector: matches Polymarket markets to Kalshi equivalents by
normalized title, then flags gaps >= threshold.

The independent "fair probability" is the other venue's price for the SAME
event — no LLM guessing, no vibes. Markets with no cross-venue match are
skipped: the bot only acts on what it can independently price."""

from __future__ import annotations

from dataclasses import dataclass

from .scanner import Market, normalize_title


@dataclass
class Signal:
    signal_id: str
    market_id: str          # venue where we would trade (primary venue)
    venue: str
    side: str               # "YES" or "NO" — the side the gap favors
    our_price: float        # quoted price on the trading venue
    fair_price: float       # reference venue's price for the same event
    gap: float              # abs(our_price - fair_price)
    expected_edge: float    # gap minus slippage haircut
    strategy_version: str = ""


def jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class MispricingDetector:
    def __init__(self, threshold: float = 0.08, match_min_jaccard: float = 0.6,
                 slippage_haircut: float = 0.01):
        self.threshold = threshold
        self.match_min_jaccard = match_min_jaccard
        self.slippage_haircut = slippage_haircut
        self._n = 0

    def match(self, market: Market, reference: list[Market]) -> Market | None:
        """Best cross-venue match by normalized-title Jaccard similarity."""
        norm = normalize_title(market.question)
        best, best_score = None, 0.0
        for r in reference:
            s = jaccard(norm, normalize_title(r.question))
            if s > best_score:
                best, best_score = r, s
        return best if best_score >= self.match_min_jaccard else None

    def detect(self, markets: list[Market], reference: list[Market],
               strategy_version: str = "") -> list[Signal]:
        signals: list[Signal] = []
        for m in markets:
            ref = self.match(m, reference)
            if ref is None:
                continue
            gap = abs(m.yes_price - ref.yes_price)
            if gap < self.threshold:
                continue
            edge = gap - self.slippage_haircut
            if edge <= 0:
                continue
            # Trade the cheaper side: if our venue prices YES lower than fair,
            # buy YES here; if higher, buy NO here.
            side = "YES" if m.yes_price < ref.yes_price else "NO"
            self._n += 1
            signals.append(Signal(
                signal_id=f"sig-{self._n:06d}",
                market_id=m.market_id,
                venue=m.venue,
                side=side,
                our_price=m.yes_price if side == "YES" else 1.0 - m.yes_price,
                fair_price=ref.yes_price if side == "YES" else 1.0 - ref.yes_price,
                gap=gap,
                expected_edge=edge,
                strategy_version=strategy_version,
            ))
        # Biggest edge first so caps bind on the best opportunities.
        signals.sort(key=lambda s: s.expected_edge, reverse=True)
        return signals
