"""X1 rug-gap forensics: gap identification, loss anatomy, intervention counterfactuals.

Pure functions over journal trade dicts. Never raises on malformed input;
returns None/0.0 where a trade lacks the fields to score. Offline analysis
only -- nothing here runs in the bot hot path.
"""

import math

GAP_EXIT_PEAK_RATIO = 0.4      # exit/peak below this => rug gap
DUMP_TRIGGER = 0.88            # dump detector: -12% / 60s
VENUE_TRIGGER = 0.70           # venue m5 tripwire: -30%
TRAIL_TRIGGER = 0.70           # trailing stop: -30%
SOL_FALLBACK_USD = 115.0


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def chain_of(trade):
    if not isinstance(trade, dict):
        return "solana"
    return trade.get("chain") or ("bsc" if str(trade.get("mint", "")).startswith("0x") else "solana")


def is_rug_gap(trade):
    """True when the recorded exit is under 40% of the recorded lifetime peak."""
    if not isinstance(trade, dict):
        return False
    peak, exit_ = _f(trade.get("peak")), _f(trade.get("exit"))
    if peak is None or exit_ is None or peak <= 0:
        return False
    return exit_ / peak < GAP_EXIT_PEAK_RATIO


def trigger_ratio(trade):
    """Price/peak ratio at which the bot's exit logic should have fired."""
    reason = str((trade or {}).get("reason", ""))
    if "dump detector" in reason:
        return DUMP_TRIGGER
    return VENUE_TRIGGER  # venue dump tripwire and trailing stop are both -30%


def usd_per_entry_unit(trade):
    """USD value of one entry-price unit of the position, or None if uncomputable."""
    if not isinstance(trade, dict):
        return None
    entry = _f(trade.get("entry"))
    stake = _f(trade.get("buy_sol")) or _f(trade.get("buy_bnb")) or 0.0
    if entry is None or entry <= 0 or stake is None:
        return None
    rate = _f(trade.get("sol_usd"))
    if rate is None and chain_of(trade) == "solana":
        rate = SOL_FALLBACK_USD
    if rate is None:
        return None
    return stake * rate / entry


def anatomy(trade):
    """Decompose a gap trade's peak-to-exit loss into detection-lag vs gap.

    Returns (lag_usd, gap_usd):
      lag_usd -- loss from peak down to the trigger price (what a perfect,
                 zero-latency trigger at the configured threshold would save,
                 assuming a fill AT the trigger price -- optimistic).
      gap_usd -- loss from the trigger price down to the actual exit fill
                 (money that vanished after any trigger could have fired).
    Returns (0.0, 0.0) for unscorable trades.
    """
    if not isinstance(trade, dict) or not is_rug_gap(trade):
        return (0.0, 0.0)
    peak, exit_ = _f(trade.get("peak")), _f(trade.get("exit"))
    scale = usd_per_entry_unit(trade)
    if peak is None or exit_ is None or scale is None:
        return (0.0, 0.0)
    trig_px = peak * trigger_ratio(trade)
    lag = max(0.0, peak - trig_px) * scale
    gap = max(0.0, trig_px - exit_) * scale
    return (lag, gap)


def is_dump_exit(trade):
    reason = str((trade or {}).get("reason", "")).lower()
    return "dump detector" in reason or "venue dump" in reason


def cooldown_counterfactual(trades, cooldown_hours=24):
    """Replay entries, vetoing any whose mint had a dump exit in the prior window.

    Returns (vetoed_trades, kept_trades). Uses only close timestamps that
    precede each entry in journal order -- no look-ahead.
    """
    vetoed, kept = [], []
    last_dump = {}
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        mint = t.get("mint")
        prior = last_dump.get(mint)
        if prior is not None and t.get("type") == "entry":
            vetoed.append(t)
        elif t.get("type") == "entry":
            kept.append(t)
        if t.get("type") == "close" and is_dump_exit(t):
            last_dump[mint] = t.get("close_ts")
        # NOTE: full 24h timestamp comparison needs parsed clocks; the journal
        # mixes EDT/UTC naive stamps, so this veto is conservative: any prior
        # dump exit on the same mint vetoes. The live bot compares real
        # timestamps with a true 24h window (fomo_trader._dump_cooldown_active).
    return vetoed, kept


def gain_veto_split(trades, threshold_pct=300.0):
    """Split trades into (kept, vetoed) by signal_gain_pct threshold for IS/OOS eval."""
    kept, vetoed = [], []
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        g = _f(t.get("signal_gain_pct"))
        if g is not None and g > threshold_pct:
            vetoed.append(t)
        else:
            kept.append(t)
    return kept, vetoed
