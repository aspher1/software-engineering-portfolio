"""Offline opportunity construction and delay-decay fit."""
from dataclasses import dataclass, field
from collections import defaultdict, deque
from math import isfinite
from .sources import valid_trade

DELAYS = (5, 15, 30, 60, 120)


@dataclass(frozen=True)
class CopyConfig:
    delay_s: int
    size_usd_cap: float = 7.0
    copy_fee_bps: float = 100.0
    priority_fee_lamports: int = 2_000_000
    slippage_bps: float = 100.0
    venue_fee_bps: float = 30.0


@dataclass(frozen=True)
class Opportunity:
    wallet: str
    mint: str
    buy_ts: float
    sell_ts: float
    buy_price_sol: float
    sell_price_sol: float
    buy_sig: str
    sell_sig: str
    leader_size_sol: float


@dataclass
class GenerationResult:
    opportunities: list[Opportunity] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def generate_opportunities(leader_trades, price_path, config):
    counts = defaultdict(int)
    open_buys = defaultdict(deque)
    opportunities = []
    seen = set()
    supplied = list(leader_trades)
    clean = []
    for trade in supplied:
        if valid_trade(trade):
            clean.append(trade)
        else:
            counts['invalid_trade'] += 1
    ordered = sorted(clean, key=lambda t: (t.ts, t.tx_sig))
    if clean != ordered:
        counts['out_of_order'] += 1
    for trade in ordered:
        if trade.tx_sig in seen:
            counts['duplicate_tx_sig'] += 1
            continue
        seen.add(trade.tx_sig)
        key = (trade.wallet, trade.mint)
        if trade.side == 'BUY':
            open_buys[key].append(trade)
            continue
        if not open_buys[key]:
            counts['unmatched_sell'] += 1
            continue
        buy = open_buys[key].popleft()
        buy_ts, sell_ts = buy.ts + config.delay_s, trade.ts + config.delay_s
        if sell_ts <= buy_ts:
            counts['invalid_timing'] += 1
            continue
        try:
            bp = price_path.price_at(buy.mint, buy_ts)
            sp = price_path.price_at(buy.mint, sell_ts)
        except Exception:
            counts['price_source_error'] += 1
            continue
        if not all(isinstance(p, (int, float)) and isfinite(p) and p > 0 for p in (bp, sp)):
            counts['missing_or_invalid_price'] += 1
            continue
        opportunities.append(Opportunity(buy.wallet, buy.mint, buy_ts, sell_ts,
                                         bp, sp, buy.tx_sig, trade.tx_sig, buy.size_sol))
    counts['unmatched_buy'] += sum(map(len, open_buys.values()))
    counts['generated'] = len(opportunities)
    return GenerationResult(opportunities, dict(counts))


def delay_decay(results_by_delay):
    """OLS on delay versus net USD per closed trade; zero crossing is extrapolated.

    The named 'edge_half_life' is the linear zero crossing, as specified for E1;
    it is not an exponential half-life.
    """
    points = sorted((float(delay), float(result['edge_per_trade'])) for delay, result in results_by_delay.items()
                    if result.get('n', 0) > 0 and isfinite(float(result.get('edge_per_trade', float('nan')))))
    if len(points) < 2:
        return {'slope': None, 'intercept': None, 'edge_half_life_s': None, 'r2': None}
    xm = sum(x for x, _ in points) / len(points)
    ym = sum(y for _, y in points) / len(points)
    xx = sum((x-xm)**2 for x, _ in points)
    if xx == 0:
        return {'slope': None, 'intercept': None, 'edge_half_life_s': None, 'r2': None}
    slope = sum((x-xm)*(y-ym) for x, y in points) / xx
    intercept = ym - slope*xm
    sst = sum((y-ym)**2 for _, y in points)
    sse = sum((y-intercept-slope*x)**2 for x, y in points)
    return {'slope': slope, 'intercept': intercept,
            'edge_half_life_s': -intercept/slope if slope < 0 else None,
            'r2': 1-sse/sst if sst else (1.0 if sse == 0 else None)}
