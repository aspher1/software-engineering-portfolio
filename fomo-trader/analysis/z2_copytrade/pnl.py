"""Fully costed paper P&L in USD."""
from math import isfinite


def paper_pnl(opportunity, config, sol_usd):
    # analysis/replay.py lines 92-105: gross_out = stake * exit/entry;
    # net = gross_out - stake - variable costs on both legs - fixed chain costs.
    if not isinstance(sol_usd, (int, float)) or not isfinite(sol_usd) or sol_usd <= 0:
        return None
    bp, sp = opportunity.buy_price_sol, opportunity.sell_price_sol
    if not all(isinstance(x, (int, float)) and isfinite(x) and x > 0 for x in (bp, sp)):
        return None
    stake = min(config.size_usd_cap, opportunity.leader_size_sol * sol_usd)
    if not isfinite(stake) or stake <= 0:
        return None
    gross_out = stake * sp / bp
    variable_rate = (config.venue_fee_bps + config.copy_fee_bps + config.slippage_bps) / 10000
    costs = (stake + gross_out) * variable_rate + 2 * config.priority_fee_lamports / 1e9 * sol_usd
    return gross_out - stake - costs


def summarize(values):
    values = [float(v) for v in values if isinstance(v, (int, float)) and isfinite(v)]
    n = len(values)
    gains = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    equity = peak = max_dd = 0.0
    for v in values:
        equity += v
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {'net_usd': sum(values), 'pf': gains/losses if losses else None,
            'wr': sum(v > 0 for v in values)/n if n else None, 'max_dd': max_dd,
            'n': n, 'edge_per_trade': sum(values)/n if n else None}
