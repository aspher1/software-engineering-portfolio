"""Local exit policy: the "AI" that can actually run near the hot path.

Fast, local, explainable, deterministic. It watches the Z4 quote path for
three fade signatures and recommends an EARLIER exit when one fires:

  1. round-trip from peak - the coin pumped (peak_gain >= threshold) and has
     faded back near entry. The deterministic layer would hold until -25%
     from peak / -35% from entry; we bank the scraps before it goes red.
  2. accelerating drawdown - drawdown-from-peak is deepening AND its velocity
     is increasing: the pre-emptive version of the trailing stop.
  3. momentum decay - gain velocity has turned negative and the position has
     given back more than half its peak gain while still green.

All thresholds live in POLICY_DEFAULTS and are PROTOTYPE PLACEHOLDERS -
chosen by judgment, NOT tuned on data (there are no real quote paths yet;
Z4 capture is off in the live config as of 2026-09-27). They must be
re-derived from real quote paths before any live use, per
AI_EXIT_PREREGISTRATION.md.

Honest limitations, encoded in the code:
  - Marks are not fills. A mark-implied exit may be unfillable at the mark
    (thin pools quote dust on size; see the bot's PRICE-IMPACT WARNING).
  - liquidity_usd is always null in Z4 today, so there is NO bid-depth /
    liquidity-collapse feature. The policy uses price action only.
  - Fail-closed: insufficient data, null marks, or any exception -> HOLD.
  - No-look-ahead: only quotes with ts <= as_of_ts are used.
  - Latency budget: <50ms per advise() (standing hot-path budget). The
    computation is O(n) over at most a few thousand marks; measured in
    tests at well under 1ms.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Sequence, Tuple

from .advisor import (EXIT, HOLD, ExitAdvice, ExitAdvisor, PositionState,
                      QuoteTick, exit_now, filter_quotes, hold, valid_marks)

# ---------------------------------------------------------------------------
# Prototype thresholds - PLACEHOLDERS, not tuned. See module docstring.
# Units: percents and seconds.
# ---------------------------------------------------------------------------
POLICY_DEFAULTS: Dict[str, float] = {
    # minimum usable marks before the policy may fire
    "min_ticks": 12,
    # 1. round-trip from peak
    "roundtrip_peak_gain_pct": 25.0,   # peak must have been this good...
    "roundtrip_gain_now_pct": 5.0,     # ...and now we're back near entry
    "roundtrip_min_secs_since_peak": 30.0,
    # 2. accelerating drawdown (pre-empting the trailing stop)
    "accel_dd_min_pct": 10.0,          # drawdown-from-peak deep enough to matter
    "accel_dd_vel_pct_per_min": 8.0,   # deepening this fast over the window
    "accel_window_sec": 120.0,
    # 3. momentum decay
    "decay_peak_gain_pct": 15.0,
    "decay_gain_vel_pct_per_min": -4.0,  # gain velocity at/below this
    "decay_vel_window_sec": 180.0,
    "decay_giveback_frac": 0.5,        # given back >= half the peak gain
    # latency guard: a late EXIT is priced on a stale mark, so it is
    # downgraded to HOLD (fail-closed). The next manage-loop tick re-evaluates.
    "max_latency_ms": 50.0,
}


def _slope_pct_per_min(pts: List[Tuple[float, float]],
                       ref_price: float) -> float:
    """Least-squares slope of price-vs-time, expressed as %/min of ref_price."""
    n = len(pts)
    if n < 2 or not ref_price or ref_price <= 0:
        return 0.0
    sx = sy = sxx = sxy = 0.0
    for t, p in pts:
        sx += t
        sy += p
    mx = sx / n
    my = sy / n
    for t, p in pts:
        dx = t - mx
        sxx += dx * dx
        sxy += dx * (p - my)
    if sxx <= 0:
        return 0.0
    slope_per_sec = sxy / sxx
    return slope_per_sec * 60.0 / ref_price * 100.0


def compute_features(pos: PositionState,
                     quotes: Sequence[QuoteTick],
                     cfg: Optional[Dict] = None) -> Dict:
    """Explainability snapshot. Pure function of quotes <= decision time."""
    marks = valid_marks(quotes)
    feats: Dict[str, float] = {"n_marks": float(len(marks))}
    if not marks or pos.entry_price <= 0:
        return feats
    ts = [t for t, _ in marks]
    px = [p for _, p in marks]
    entry = pos.entry_price
    last = px[-1]
    peak = max(px)
    peak_i = px.index(peak)
    feats["gain_now_pct"] = (last - entry) / entry * 100.0
    feats["peak_gain_pct"] = (peak - entry) / entry * 100.0
    feats["dd_from_peak_pct"] = (peak - last) / peak * 100.0 if peak > 0 else 0.0
    feats["time_since_peak_sec"] = ts[-1] - ts[peak_i]
    feats["age_sec"] = ts[-1] - ts[0]

    c = dict(POLICY_DEFAULTS)
    if cfg:
        c.update(cfg)
    # drawdown velocity over the recent window, and the window before it
    # (to detect acceleration)
    w = c["accel_window_sec"]
    recent = [(t, p) for t, p in marks if t >= ts[-1] - w]
    prev = [(t, p) for t, p in marks if ts[-1] - 2 * w <= t < ts[-1] - w]
    feats["dd_vel_recent_pct_per_min"] = -_slope_pct_per_min(recent, peak)
    feats["dd_vel_prev_pct_per_min"] = -_slope_pct_per_min(prev, peak)
    # gain velocity over the decay window
    dw = c["decay_vel_window_sec"]
    dwin = [(t, p) for t, p in marks if t >= ts[-1] - dw]
    feats["gain_vel_pct_per_min"] = _slope_pct_per_min(dwin, entry)
    return feats


class LocalExitPolicy(ExitAdvisor):
    """Default backend. Deterministic, explainable, <50ms."""

    name = "local_policy"

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = dict(POLICY_DEFAULTS)
        if cfg:
            self.cfg.update(cfg)
        self.budget_breaches = 0  # late-EXIT downgrades; validation kill signal

    def advise(self, pos: PositionState,
               quotes: Sequence[QuoteTick],
               as_of_ts: Optional[float] = None) -> ExitAdvice:
        t0 = time.perf_counter()
        try:
            result = self._advise_inner(pos, quotes, as_of_ts, t0)
        except Exception as e:  # fail-closed: never EXIT on error
            ms = (time.perf_counter() - t0) * 1000.0
            return hold("policy error (fail-closed): %s" % type(e).__name__,
                        self.name, ms, {"error": str(e)[:120]})
        ms = (time.perf_counter() - t0) * 1000.0
        budget = float(self.cfg.get("max_latency_ms", 50.0))
        if ms > budget and result.decision == EXIT:
            # A late EXIT is worse than no EXIT: it was priced on a stale
            # mark. Downgrade to HOLD (fail-closed); the next manage-loop
            # tick re-evaluates from a fresh mark.
            self.budget_breaches += 1
            return hold(
                "latency budget exceeded (%.1fms > %.0fms); EXIT downgraded" % (ms, budget),
                self.name, ms,
                dict(result.features, budget_breach_ms=round(ms, 2)))
        return result

    def _advise_inner(self, pos, quotes, as_of_ts, t0):
        c = self.cfg
        seen = filter_quotes(quotes, as_of_ts)
        marks = valid_marks(seen)
        feats = compute_features(pos, seen, c)
        ms = lambda: (time.perf_counter() - t0) * 1000.0  # noqa: E731

        if len(marks) < c["min_ticks"]:
            return hold("insufficient marks (%d < %d)" % (len(marks), c["min_ticks"]),
                        self.name, ms(), feats)
        if pos.entry_price <= 0 or pos.current_price <= 0:
            return hold("invalid position prices", self.name, ms(), feats)

        gain_now = feats["gain_now_pct"]
        peak_gain = feats["peak_gain_pct"]
        dd_peak = feats["dd_from_peak_pct"]

        # 1. round-trip from peak: bank the scraps before it goes red
        if (peak_gain >= c["roundtrip_peak_gain_pct"]
                and gain_now <= c["roundtrip_gain_now_pct"]
                and feats["time_since_peak_sec"] >= c["roundtrip_min_secs_since_peak"]):
            return exit_now(
                "round-trip from peak: peaked +%.1f%%, now %+.1f%%" % (peak_gain, gain_now),
                self.name, ms(), feats, confidence=0.65)

        # 2. accelerating drawdown: pre-empt the trailing stop
        if (dd_peak >= c["accel_dd_min_pct"]
                and feats["dd_vel_recent_pct_per_min"] >= c["accel_dd_vel_pct_per_min"]
                and feats["dd_vel_recent_pct_per_min"] > feats["dd_vel_prev_pct_per_min"]):
            return exit_now(
                "accelerating drawdown: -%.1f%% from peak, deepening at %.1f%%/min"
                % (dd_peak, feats["dd_vel_recent_pct_per_min"]),
                self.name, ms(), feats, confidence=0.6)

        # 3. momentum decay: gave back half the peak gain, still fading
        if (peak_gain >= c["decay_peak_gain_pct"]
                and feats["gain_vel_pct_per_min"] <= c["decay_gain_vel_pct_per_min"]
                and gain_now <= peak_gain * (1.0 - c["decay_giveback_frac"])):
            return exit_now(
                "momentum decay: peaked +%.1f%%, now %+.1f%%, fading at %.1f%%/min"
                % (peak_gain, gain_now, feats["gain_vel_pct_per_min"]),
                self.name, ms(), feats, confidence=0.55)

        return hold("no fade signature (peak %+.1f%%, now %+.1f%%, dd %.1f%%)"
                    % (peak_gain, gain_now, dd_peak),
                    self.name, ms(), feats)
