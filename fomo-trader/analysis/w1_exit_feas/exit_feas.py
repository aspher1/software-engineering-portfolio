"""W1: pre-entry exit-feasibility simulation.

Estimates the slippage a planned position would suffer on exit, from
entry-time data only, and vetoes entries whose estimated exit impact
exceeds a calibrated threshold. Pure stdlib, local, sub-50ms, fail-open.

Validation verdict (2026-09-27): REJECTED — vacuous for this bot's
position sizes (~$7) against its minimum liquidity ($15k+). Max estimated
impact across 49 scorable entries is 0.085%, two orders of magnitude below
any plausible threshold, so no threshold discriminates. See
analysis/w1_exit_feas/exit_feas_report.md.
"""

import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_THRESHOLD_PCT = 25.0
PARAMS_PATH = Path(__file__).resolve().parent / "exit_feas.json"

# Conservative fudge: real exits into thin books are worse than the
# constant-product average. Even at 5x the estimate stays << 1% here.
IMPACT_FUDGE = 1.0

# Fallback accounting: every fail-open path increments one of these and
# logs. Fallbacks are rare by design; the counters let operators see them.
# Guarded by a lock: approve() may run on the scan thread while a monitor
# thread reads the counters.
_fallback_lock = threading.Lock()
fallback_counts = {
    "params_unavailable": 0,   # params file missing/corrupt -> defaults
    "missing_liquidity": 0,    # entry has no liquidity_usd -> approve
    "missing_position": 0,     # entry has no computable position_usd -> approve
    "exception": 0,            # unexpected error in approve -> approve
}


def _count(reason):
    with _fallback_lock:
        fallback_counts[reason] = fallback_counts.get(reason, 0) + 1
    log.warning("EXIT_FEAS_FALLBACK reason=%s counts=%s", reason,
                dict(fallback_counts))


def reset_fallback_counts():
    with _fallback_lock:
        for k in fallback_counts:
            fallback_counts[k] = 0


def estimate_exit_impact_pct(position_usd, liquidity_usd, volume_usd=None):
    """Estimated average execution impact (%) of selling position_usd.

    Constant-product approximation: selling P into a pool whose token-side
    reserve is ~liquidity_usd/2 gives average impact P / (L/2 + P).
    Optionally bounded by 15m volume: the market must absorb the sale.
    Returns 0.0 (approve) on missing/nonpositive inputs — fail open.
    """
    try:
        p = float(position_usd)
        liq = float(liquidity_usd)
    except (TypeError, ValueError):
        return 0.0
    if not (p > 0 and liq > 0 and math.isfinite(p) and math.isfinite(liq)):
        return 0.0
    reserve = liq / 2.0
    impact = p / (reserve + p)
    if volume_usd is not None:
        try:
            v = float(volume_usd)
            if v > 0 and math.isfinite(v):
                impact = max(impact, p / (v + p))
        except (TypeError, ValueError):
            pass
    return 100.0 * impact * IMPACT_FUDGE


@dataclass(frozen=True)
class Params:
    threshold_pct: float = DEFAULT_THRESHOLD_PCT
    enabled: bool = True

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        try:
            t = float(d.get("threshold_pct", DEFAULT_THRESHOLD_PCT))
        except (TypeError, ValueError):
            t = DEFAULT_THRESHOLD_PCT
        if not (t > 0 and math.isfinite(t)):
            t = DEFAULT_THRESHOLD_PCT
        return cls(threshold_pct=t, enabled=bool(d.get("enabled", True)))


def load_params(path=PARAMS_PATH):
    """Load Params from JSON; fail open to defaults on any problem.

    Call once at startup and pass the Params object into approve(); the
    hot path must not do file I/O per call.
    """
    try:
        with open(path, encoding="utf-8") as f:
            return Params.from_dict(json.load(f).get("params", {}))
    except (OSError, ValueError, AttributeError) as exc:
        _count("params_unavailable")
        log.debug("EXIT_FEAS params file unreadable: %r", exc)
        return Params()


def position_usd_of(entry):
    """Planned position size in USD from a journal entry row.

    Returns None when no computable, finite position size exists (caller
    treats this as a counted fail-open). Never raises.
    """
    def finite(x):
        try:
            v = float(x)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) and v > 0 else None

    e = entry or {}
    try:
        bs, su = finite(e.get("buy_sol")), finite(e.get("sol_usd"))
        if bs is not None and su is not None:
            return bs * su
        bb, bu = finite(e.get("buy_bnb")), finite(e.get("bnb_usd"))
        if bb is not None and bu is not None:
            return bb * bu
        bn = finite(e.get("buy_sol")) or finite(e.get("buy_bnb"))
        cpn, cpu = finite(e.get("commit_price_native")), finite(e.get("commit_price_usd"))
        if bn is not None and cpn is not None and cpu is not None:
            return bn * cpu / cpn
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return None


def approve(entry, params=None):
    """True = entry passes the exit-feasibility screen.

    Fail-open, but never silently: every fallback increments
    fallback_counts and logs a line. Never raises.

    Concurrency contract (for hot-path integration):
      - Pass a cached Params object (frozen dataclass, immutable ->
        race-free). Do NOT rely on per-call load_params(); it does file I/O.
      - The only shared mutable state is fallback_counts, guarded by
        _fallback_lock. approve() itself is otherwise pure.
      - No locks are taken on the approve/veto fast path.
    """
    try:
        params = params or load_params()
        if not params.enabled:
            return True
        e = entry or {}
        liq = e.get("liquidity_usd")
        try:
            liq_f = float(liq)
            liq_ok = math.isfinite(liq_f) and liq_f > 0
        except (TypeError, ValueError):
            liq_ok = False
        if not liq_ok:
            _count("missing_liquidity")
            return True
        p = position_usd_of(e)
        if p is None:
            _count("missing_position")
            return True
        impact = estimate_exit_impact_pct(p, liq, e.get("m15_volume_usd"))
        return impact <= params.threshold_pct
    except Exception as exc:  # noqa: BLE001 - veto must never raise
        _count("exception")
        log.warning("EXIT_FEAS_FALLBACK exception in approve: %r", exc)
        return True


def evaluate(trades, thresholds, net_pnl):
    """Apply each threshold veto to chronological trades; IS/OOS metrics.

    trades: replay.load_journal() merged rows in chronological order.
    net_pnl: callable(trade, stress=False) -> (native, usd).
    Returns dict threshold -> {"is": stats, "oos": stats} where each stats
    has n, pnl_usd, pf, wr, dd. IS = first 2/3, OOS = last 1/3.
    """
    n = len(trades)
    cut = 2 * n // 3
    out = {}
    for thr in thresholds:
        params = Params(threshold_pct=thr, enabled=True)
        kept_is, kept_oos = [], []
        for i, t in enumerate(trades):
            if approve(t.get("entry_record"), params):
                (kept_is if i < cut else kept_oos).append(t)
        out[thr] = {"is": _stats(kept_is, net_pnl), "oos": _stats(kept_oos, net_pnl)}
    return out


def _stats(trades, net_pnl, stress=False):
    pnls = []
    for t in trades:
        _, usd = net_pnl(t, stress=stress)
        if usd is not None:
            pnls.append(usd)
    gains = sum(x for x in pnls if x > 0)
    losses = -sum(x for x in pnls if x < 0)
    peak = 0.0
    dd = 0.0
    run = 0.0
    for x in pnls:
        run += x
        peak = max(peak, run)
        dd = max(dd, peak - run)
    return {
        "n": len(pnls),
        "pnl_usd": round(sum(pnls), 2),
        "pf": round(gains / losses, 3) if losses > 0 else None,
        "wr": round(sum(1 for x in pnls if x > 0) / len(pnls), 3) if pnls else 0.0,
        "dd": round(dd, 2),
    }
