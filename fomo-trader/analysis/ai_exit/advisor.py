"""Advisor framework: the interface every exit backend implements, plus the
floor gatekeeper that makes the deterministic exits unsuppressible.

SHADOW-ONLY. The core invariant, encoded in `combine_with_deterministic` and
tested in tests/test_ai_exit_advisor.py:

    A deterministic exit trigger ALWAYS fires, regardless of what any
    advisor says. An advisor can only ADD an exit recommendation; it can
    never delay, veto, override, or weaken the hard stop, trailing stop,
    take-profit ladder, dump detector, venue tripwire, or stale exit.

Structurally this is enforced because advise() returns only EXIT or HOLD,
where HOLD means "no opinion - let the deterministic layer decide". There
is no "STAY" / "veto" decision in the vocabulary at all.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

EXIT = "EXIT"
HOLD = "HOLD"

# Z4 quote-path timestamp format (local journal strings, e.g. "2026-09-24 07:56:11")
Z4_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class QuoteTick:
    """One observed mark. ts is epoch seconds (naive local); price_usd may be None."""
    ts: float
    price_usd: Optional[float]


@dataclass(frozen=True)
class PositionState:
    """Everything an advisor may know about the open position at decision time."""
    mint: str
    chain: str
    entry_price: float          # native entry price (per-token)
    peak_price: float           # highest native mark seen so far
    current_price: float        # latest native mark
    opened_ts: float            # epoch seconds
    now_ts: float               # epoch seconds (decision time)
    rungs_fired: int = 0        # TP rungs already taken
    exit_cfg: Dict = field(default_factory=dict)  # the bot's exit config block


@dataclass(frozen=True)
class ExitAdvice:
    """The only thing an advisor may emit."""
    decision: str               # EXIT or HOLD - there is no veto/stay
    reason: str                 # human-readable, logged
    confidence: float           # 0.0..1.0
    backend: str                # which backend produced this
    latency_ms: float           # measured advise() wall time
    features: Dict = field(default_factory=dict)  # explainability snapshot

    def __post_init__(self):
        if self.decision not in (EXIT, HOLD):
            raise ValueError("advice decision must be EXIT or HOLD, got %r"
                             % (self.decision,))
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be in [0,1], got %r"
                             % (self.confidence,))


def hold(reason: str, backend: str, latency_ms: float = 0.0,
         features: Optional[Dict] = None, confidence: float = 0.0) -> ExitAdvice:
    return ExitAdvice(HOLD, reason, confidence, backend, latency_ms,
                      features or {})


def exit_now(reason: str, backend: str, latency_ms: float = 0.0,
             features: Optional[Dict] = None,
             confidence: float = 0.5) -> ExitAdvice:
    return ExitAdvice(EXIT, reason, confidence, backend, latency_ms,
                      features or {})


class ExitAdvisor:
    """Base class for exit backends. Subclass and implement advise()."""

    name = "base"

    def advise(self, pos: PositionState,
               quotes: Sequence[QuoteTick],
               as_of_ts: Optional[float] = None) -> ExitAdvice:
        """Return EXIT or HOLD.

        `quotes` is the Z4 quote path (marks, may contain None prices).
        `as_of_ts`: no-look-ahead cutoff - only quotes with ts <= as_of_ts
        may influence the decision. None means "use all quotes".
        Must be fail-closed: any error or insufficient data -> HOLD, never EXIT.
        """
        raise NotImplementedError


def filter_quotes(quotes: Sequence[QuoteTick],
                  as_of_ts: Optional[float]) -> List[QuoteTick]:
    """No-look-ahead filter: drop every tick after the decision time."""
    if as_of_ts is None:
        return list(quotes)
    return [q for q in quotes if q.ts <= as_of_ts]


def combine_with_deterministic(deterministic_exit: bool,
                               det_reason: str,
                               advice: ExitAdvice) -> Tuple[bool, str]:
    """Apply the floor invariant.

    Returns (should_exit, reason). The deterministic layer's decision is
    final: if it fires, we exit with ITS reason no matter what the advisor
    said. The advisor's EXIT can only add an exit when the deterministic
    layer is silent. There is no code path by which an advisor suppresses
    a hard stop, trailing stop, TP rung, dump exit, or stale exit.
    """
    if deterministic_exit:
        return True, det_reason
    if advice.decision == EXIT:
        return True, "ai-advisor(%s): %s" % (advice.backend, advice.reason)
    return False, ""


def parse_z4_ts(s: str) -> Optional[float]:
    try:
        return datetime.strptime(s, Z4_TS_FORMAT).timestamp()
    except (ValueError, TypeError):
        return None


def load_z4_quote_path(path: str) -> List[QuoteTick]:
    """Read a Z4 quote_paths/<mint>.jsonl file into QuoteTicks.

    Mirrors the Z4 schema: ts (local journal string), mint, chain, price_usd
    (nullable mark), liquidity_usd (always null today - ignored). Bad lines
    are skipped and counted; the loader never raises on bad data.
    """
    ticks: List[QuoteTick] = []
    skipped = 0
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    ts = parse_z4_ts(row.get("ts"))
                    px = row.get("price_usd")
                    if ts is None:
                        skipped += 1
                        continue
                    if px is not None:
                        px = float(px)
                        if not math.isfinite(px) or px <= 0:
                            px = None
                    ticks.append(QuoteTick(ts=ts, price_usd=px))
                except (ValueError, TypeError, AttributeError):
                    skipped += 1
    except OSError:
        pass
    ticks.sort(key=lambda q: q.ts)
    return ticks


def valid_marks(quotes: Sequence[QuoteTick]) -> List[Tuple[float, float]]:
    """(ts, price) pairs with usable prices, in time order."""
    return [(q.ts, q.price_usd) for q in quotes if q.price_usd is not None]
