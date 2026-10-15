"""Paper bankroll / money management for FOMO Trader.

A single USD-denominated paper bankroll covers both chains. It is seeded
once from the trade journal's all-time realized P&L, then moves with every
priced close. Three adaptive guards build on top of the fixed risk rails:

1. Risk-based ticket sizing: ticket_usd = risk_per_trade_pct * bankroll /
   hard_stop_pct, capped at the configured fixed ticket size. Position size
   can only shrink as the bankroll falls, never grow above the configured
   size.
2. Dynamic daily loss cap: the effective USD kill-switch cap is
   min(fixed usd cap, daily_loss_pct * day-start bankroll).
3. Drawdown brake: new entries are blocked while drawdown from the equity
   peak is >= max_drawdown_pct, resuming below drawdown_resume_pct
   (hysteresis). This mirrors the validation bar's max-DD < 15% rule.

All functions are pure over (state, cfg) dicts so they are trivially
testable; the bot wires them in at entry/commit/close/day-roll.
"""

DEFAULTS = {
    "starting_bankroll_usd": 1000.0,
    "risk_per_trade_pct": 1.0,
    "daily_loss_pct": 6.0,
    "max_drawdown_pct": 15.0,
    "drawdown_resume_pct": 10.0,
}


def settings(cfg):
    m = (cfg.get("money") or {}).copy()
    for k, v in DEFAULTS.items():
        m.setdefault(k, v)
    return m


def ensure_state(state, cfg):
    """Create bankroll keys if missing. Returns True if anything was added."""
    m = settings(cfg)
    added = False
    if "bankroll_usd" not in state:
        state["bankroll_usd"] = round(float(m["starting_bankroll_usd"]), 2)
        added = True
    if "equity_peak_usd" not in state:
        state["equity_peak_usd"] = state["bankroll_usd"]
        added = True
    if "day_start_bankroll_usd" not in state:
        state["day_start_bankroll_usd"] = state["bankroll_usd"]
        added = True
    if "bankroll_seeded" not in state:
        state["bankroll_seeded"] = False
        added = True
    return added


def _journal_pnl_usd(journal_path):
    """Sum all-time realized USD over close records in the journal.

    Rows with realized_usd use it; rows with native realized + a
    contemporaneous sol_usd/bnb rate use the product. Rows with neither
    are skipped (documented, not estimated).
    """
    total, priced, skipped = 0.0, 0, 0
    try:
        with open(journal_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = __import__("json").loads(line)
                except Exception:
                    continue
                if r.get("type") != "close":
                    continue
                u = r.get("realized_usd")
                if u is not None:
                    total += u
                    priced += 1
                    continue
                px = r.get("sol_usd") or r.get("bnb_usd")
                nat = r.get("realized_sol")
                if nat is None:
                    nat = r.get("realized_bnb")
                if px and nat:
                    total += nat * px
                    priced += 1
                else:
                    skipped += 1
    except FileNotFoundError:
        pass
    return round(total, 2), priced, skipped


def seed_from_journal(state, cfg, journal_path):
    """One-time: bankroll = starting_bankroll + journal all-time P&L.

    Sets bankroll_seeded so it never runs twice. Returns a dict describing
    what was done (for logging).
    """
    ensure_state(state, cfg)
    if state.get("bankroll_seeded"):
        return {"seeded": False}
    m = settings(cfg)
    pnl, priced, skipped = _journal_pnl_usd(journal_path)
    bankroll = round(float(m["starting_bankroll_usd"]) + pnl, 2)
    state["bankroll_usd"] = bankroll
    state["equity_peak_usd"] = max(float(m["starting_bankroll_usd"]), bankroll)
    state["day_start_bankroll_usd"] = bankroll
    state["bankroll_seeded"] = True
    return {"seeded": True, "pnl_usd": pnl, "priced_closes": priced,
            "skipped_closes": skipped, "bankroll_usd": bankroll}


def apply_close(state, realized_usd):
    """Move the bankroll by a priced close's realized USD. Returns new bankroll."""
    if realized_usd is None:
        return state.get("bankroll_usd")
    b = round(state.get("bankroll_usd", 0.0) + realized_usd, 2)
    state["bankroll_usd"] = b
    if b > state.get("equity_peak_usd", b):
        state["equity_peak_usd"] = b
    return b


def drawdown_pct(state):
    peak = state.get("equity_peak_usd") or 0
    if peak <= 0:
        return 0.0
    return round((peak - state.get("bankroll_usd", peak)) / peak * 100.0, 2)


def entries_blocked_by_drawdown(state, cfg):
    """True while drawdown >= max; stays blocked until it recovers below
    the resume threshold (hysteresis)."""
    m = settings(cfg)
    dd = drawdown_pct(state)
    if dd >= m["max_drawdown_pct"]:
        state["_dd_brake_on"] = True
    elif dd < m["drawdown_resume_pct"]:
        state["_dd_brake_on"] = False
    return bool(state.get("_dd_brake_on", False))


def effective_daily_loss_cap_usd(state, cfg, fixed_cap_usd):
    """Adaptive daily USD cap: min(fixed cap, daily_loss_pct * day-start bankroll)."""
    m = settings(cfg)
    dyn = m["daily_loss_pct"] / 100.0 * state.get("day_start_bankroll_usd",
                                                 state.get("bankroll_usd", 0.0))
    dyn = max(dyn, 0.0)
    if fixed_cap_usd is None:
        # No legacy USD cap and no money section: preserve the old
        # behavior of having no USD kill switch at all (rather than a
        # 0.0 cap that would trip on any loss).
        if "money" not in (cfg or {}):
            return None
        return round(dyn, 2)
    return round(min(float(fixed_cap_usd), dyn), 2)


def ticket_usd_ceiling(state, cfg, hard_stop_pct):
    """Risk-based ticket ceiling in USD. Never raises size above the
    configured fixed ticket; the caller takes min(configured, ceiling)."""
    m = settings(cfg)
    hs = float(hard_stop_pct) if hard_stop_pct else 35.0
    if hs <= 0:
        return None
    return round(m["risk_per_trade_pct"] / 100.0 *
                 state.get("bankroll_usd", 0.0) / (hs / 100.0), 2)


def roll_day(state, cfg):
    """Call on day rollover: snapshot today's starting bankroll."""
    ensure_state(state, cfg)
    state["day_start_bankroll_usd"] = state.get("bankroll_usd")
