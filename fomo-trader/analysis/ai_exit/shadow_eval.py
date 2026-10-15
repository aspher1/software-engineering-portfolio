"""Offline shadow evaluator: deterministic baseline vs AI advisor on quote paths.

Replays Z4-style quote paths (ts, price_usd marks) through two engines:

  BASELINE - the bot's deterministic exit state machine, reimplemented here
    from runs/paper-1h/config.json semantics (TP ladder, trailing stop,
    hard stop, dump detector, stale exit). The venue m5 tripwire is NOT
    replayed: it needs DexScreener's tape, which is not in the quote path.
  ADVISOR  - baseline PLUS an ExitAdvisor whose EXIT can only fire earlier
    (via advisor.combine_with_deterministic - the floor invariant holds in
    replay exactly as it would live).

Cost model mirrors analysis/z5_exit_arch/e3.py (E3):
  notional $10/position, venue 30bps/side, entry slippage 100bps,
  exit slippage 500bps, priority fee 2,000,000 lamports/tx converted at
  sol_usd. Marks are not fills: the exit slippage + priority fee are the
  honest haircut, and the report says so.

Usage:
  python -m analysis.ai_exit.shadow_eval --synthetic   # runs now, no data needed
  python -m analysis.ai_exit.shadow_eval --quote-paths <dir>  # real Z4 paths
                                                       # (needs >=1 file; the
                                                       #  preregistered gate
                                                       #  needs >=30 trades)

SHADOW-ONLY. Reads quote paths; never touches the bot, journal, or state.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from analysis.ai_exit.advisor import (ExitAdvisor, PositionState, QuoteTick,
                                      combine_with_deterministic,
                                      load_z4_quote_path)
from analysis.ai_exit.local_policy import LocalExitPolicy

# ---------------------------------------------------------------------------
# Cost model (mirrors E3)
# ---------------------------------------------------------------------------
NOTIONAL_USD = 10.0
VENUE_FEE_BPS = 30.0
ENTRY_SLIPPAGE_BPS = 100.0
EXIT_SLIPPAGE_BPS = 500.0
PRIORITY_FEE_LAMPORTS = 2_000_000
SOL_USD = 200.0  # research normalization; real runs use the day's rate

# Live exit semantics (runs/paper-1h/config.json, 2026-09-27). Passed in as
# exit_cfg so replay can also test other configs.
LIVE_EXIT_CFG = {
    "take_profits": [[50, 50]],
    "trailing_stop_pct": 25.0,
    "hard_stop_pct": 35.0,
    "dump_drop_pct": 12.0,
    "dump_window_sec": 60.0,
    "trail_after_tp_pct": 12.0,
    "stale_exit_min": 30.0,
    "stale_exit_max_gain_pct": 10.0,
}


def _priority_fee_usd() -> float:
    return PRIORITY_FEE_LAMPORTS / 1e9 * SOL_USD


@dataclass
class ReplayResult:
    exit_ts: float
    exit_price: float
    reason: str
    net_usd: float
    gain_pct: float          # net / notional * 100
    ticks_held: int
    advisor_exits: int = 0   # how many exits came from advisor (not det)
    rungs_fired: int = 0


class DeterministicExits:
    """The bot's deterministic exit state machine, replayed on marks."""

    def __init__(self, cfg: Optional[Dict] = None):
        c = dict(LIVE_EXIT_CFG)
        if cfg:
            c.update(cfg)
        self.cfg = c
        self.tps: List[Tuple[float, float]] = [
            (float(a), float(b)) for a, b in c.get("take_profits", [])]
        self.trail = float(c.get("trailing_stop_pct", 25.0))
        self.hard = float(c.get("hard_stop_pct", 35.0))
        self.dump_pct = float(c.get("dump_drop_pct", 12.0))
        self.dump_win = float(c.get("dump_window_sec", 60.0))
        self.trail_after_tp = float(c.get("trail_after_tp_pct", 12.0))
        self.stale_min = float(c.get("stale_exit_min", 30.0))
        self.stale_gain = float(c.get("stale_exit_max_gain_pct", 10.0))

    def new_position(self, entry_price: float, opened_ts: float) -> Dict:
        return {"entry": entry_price, "peak": entry_price,
                "opened_ts": opened_ts, "fired": set(),
                "hist": deque(maxlen=1500)}

    def _exit_cost(self, gross: float) -> float:
        return gross * (VENUE_FEE_BPS + EXIT_SLIPPAGE_BPS) / 1e4 + _priority_fee_usd()

    def check(self, st: Dict, ts: float, price: float,
              remaining: float) -> Tuple[bool, str, float]:
        """Returns (fired, reason, sell_frac_of_remaining).

        TP ladder first (partial sells keep the position alive), then the
        protective chain - mirroring the live _manage_once order.
        """
        entry, peak = st["entry"], st["peak"]
        if price > peak:
            st["peak"] = peak = price
        gain = (price - entry) / entry * 100.0 if entry > 0 else 0.0

        # TP ladder first (partial sells keep the position alive)
        for i, (tp_pct, sell_pct) in enumerate(self.tps):
            if i not in st["fired"] and gain >= tp_pct:
                st["fired"].add(i)
                return True, "TP+%g%%" % tp_pct, sell_pct / 100.0

        return self.check_protective(st, ts, price)

    def check_protective(self, st: Dict, ts: float,
                         price: float) -> Tuple[bool, str, float]:
        """Dump detector -> trailing -> hard stop -> stale. First trigger wins."""
        entry, peak = st["entry"], st["peak"]
        gain = (price - entry) / entry * 100.0 if entry > 0 else 0.0
        dd_peak = (peak - price) / peak * 100.0 if peak > 0 else 0.0
        dd_entry = -gain
        st["hist"].append((ts, price))
        cutoff = ts - self.dump_win
        wmax = max((p for t, p in st["hist"] if t >= cutoff), default=0.0)
        if wmax > 0 and (wmax - price) / wmax * 100.0 >= self.dump_pct:
            return True, "dump-detector", 1.0

        trail = self.trail
        if st["fired"]:
            trail = min(trail, self.trail_after_tp)
        if dd_peak >= trail:
            return True, "trailing-stop", 1.0
        if dd_entry >= self.hard:
            return True, "hard-stop", 1.0
        age_min = (ts - st["opened_ts"]) / 60.0
        if self.stale_min > 0 and age_min >= self.stale_min and gain < self.stale_gain:
            return True, "stale-exit", 1.0
        return False, "", 0.0


def replay(marks: Sequence[Tuple[float, float]],
           exit_cfg: Optional[Dict] = None,
           advisor: Optional[ExitAdvisor] = None,
           mint: str = "synthetic", chain: str = "solana") -> ReplayResult:
    """Replay one position over (ts, price_usd) marks. Returns the outcome."""
    det = DeterministicExits(exit_cfg)
    cfg = det.cfg
    ts0, entry = marks[0][0], marks[0][1]
    st = det.new_position(entry, ts0)
    tokens = NOTIONAL_USD / entry if entry > 0 else 0.0
    entry_cost = (NOTIONAL_USD * (VENUE_FEE_BPS + ENTRY_SLIPPAGE_BPS) / 1e4
                  + _priority_fee_usd())
    remaining = 1.0
    proceeds = 0.0
    advisor_exits = 0
    ticks = 0

    for ts, price in marks:
        ticks += 1
        if remaining <= 1e-9:
            break
        fired, reason, sell_frac = det.check(st, ts, price, remaining)

        if fired and reason.startswith("TP+") and sell_frac < 1.0:
            # Partial TP rung: bank the slice, then keep checking the
            # protective chain in the SAME tick, exactly like the live loop
            # (TP loop first, then the dump/trailing/hard/stale elif chain).
            gross = remaining * sell_frac * tokens * price
            proceeds += gross - det._exit_cost(gross)
            remaining *= (1.0 - sell_frac)
            fired, reason, sell_frac = det.check_protective(st, ts, price)

        advice_exit = False
        if not fired and advisor is not None and price > 0:
            pos = PositionState(
                mint=mint, chain=chain, entry_price=entry,
                peak_price=st["peak"], current_price=price,
                opened_ts=ts0, now_ts=ts, rungs_fired=len(st["fired"]),
                exit_cfg=cfg)
            quotes = [QuoteTick(t, p) for t, p in marks]
            advice = advisor.advise(pos, quotes, as_of_ts=ts)
            should_exit, combined_reason = combine_with_deterministic(
                False, "", advice)
            if should_exit:
                fired, reason, sell_frac = True, combined_reason, 1.0
                advice_exit = True

        if fired:
            sell_frac = min(1.0, max(sell_frac, 0.0))
            # TP rung that isn't 100% keeps the remainder alive
            if reason.startswith("TP+") and sell_frac < 1.0:
                gross = remaining * sell_frac * tokens * price
                proceeds += gross - det._exit_cost(gross)
                remaining *= (1.0 - sell_frac)
                continue
            gross = remaining * tokens * price
            proceeds += gross - det._exit_cost(gross)
            remaining = 0.0
            if advice_exit:
                advisor_exits = 1
            net = proceeds - NOTIONAL_USD - entry_cost
            return ReplayResult(ts, price, reason, net,
                                net / NOTIONAL_USD * 100.0, ticks,
                                advisor_exits, len(st["fired"]))

    # path ended with size left: forced close at last mark (counted, labeled)
    if remaining > 1e-9:
        price = marks[-1][1]
        gross = remaining * tokens * price
        proceeds += gross - det._exit_cost(gross)
    net = proceeds - NOTIONAL_USD - entry_cost
    return ReplayResult(marks[-1][0], marks[-1][1], "path-end (forced)", net,
                        net / NOTIONAL_USD * 100.0, ticks, advisor_exits,
                        len(st["fired"]))


# ---------------------------------------------------------------------------
# Synthetic quote-path generators (seeded). Entry normalized to 1.0.
# ---------------------------------------------------------------------------
def _noise(rng: random.Random, scale: float) -> float:
    return rng.gauss(0, scale)


def gen_pump_dump(seed: int = 1, tick_s: float = 5.0) -> List[Tuple[float, float]]:
    """+80% over 3 min, then rug-style collapse to 10% of peak."""
    rng = random.Random(seed)
    out, t, p = [], 0.0, 1.0
    while t <= 180:
        p = 1.0 + 0.80 * (t / 180) + _noise(rng, 0.01)
        out.append((t, max(p, 0.01))); t += tick_s
    peak = out[-1][1]
    while t <= 300:
        f = (t - 180) / 120
        p = peak * (1 - 0.90 * f) + _noise(rng, 0.005)
        out.append((t, max(p, 0.01))); t += tick_s
    while t <= 420:
        out.append((t, out[-1][1] * (1 + _noise(rng, 0.002)))); t += tick_s
    return out


def gen_slow_fade(seed: int = 2, tick_s: float = 5.0) -> List[Tuple[float, float]]:
    """+30% over 5 min, bleeds to -20% over the next 25 min."""
    rng = random.Random(seed)
    out, t = [], 0.0
    while t <= 300:
        p = 1.0 + 0.30 * (t / 300) + _noise(rng, 0.008)
        out.append((t, max(p, 0.01))); t += tick_s
    peak = out[-1][1]
    while t <= 1800:
        f = (t - 300) / 1500
        p = peak - (peak - 0.80) * f + _noise(rng, 0.008)
        out.append((t, max(p, 0.01))); t += tick_s
    return out


def gen_moon_hold(seed: int = 3, tick_s: float = 5.0) -> List[Tuple[float, float]]:
    """+150% over 10 min, then holds with noise for 20 min (advisor must not sell)."""
    rng = random.Random(seed)
    out, t = [], 0.0
    while t <= 600:
        p = 1.0 + 1.50 * (t / 600) + _noise(rng, 0.01)
        out.append((t, max(p, 0.01))); t += tick_s
    top = out[-1][1]
    while t <= 1800:
        out.append((t, top * (1 + _noise(rng, 0.02)))); t += tick_s
    return out


def gen_chop(seed: int = 4, tick_s: float = 5.0) -> List[Tuple[float, float]]:
    """Sideways chop +/-10% for 30 min."""
    rng = random.Random(seed)
    out, t = [], 0.0
    while t <= 1800:
        p = 1.0 + 0.10 * math.sin(t / 120.0) + _noise(rng, 0.015)
        out.append((t, max(p, 0.01))); t += tick_s
    return out


def gen_rug(seed: int = 5, tick_s: float = 5.0) -> List[Tuple[float, float]]:
    """+40% over 2 min, then -95% in two ticks (classic rug)."""
    rng = random.Random(seed)
    out, t = [], 0.0
    while t <= 120:
        p = 1.0 + 0.40 * (t / 120) + _noise(rng, 0.008)
        out.append((t, max(p, 0.01))); t += tick_s
    peak = out[-1][1]
    out.append((t, peak * 0.30)); t += tick_s
    out.append((t, peak * 0.05)); t += tick_s
    while t <= 600:
        out.append((t, out[-1][1] * (1 + _noise(rng, 0.01)))); t += tick_s
    return out


GENERATORS = {
    "pump_dump": gen_pump_dump,
    "slow_fade": gen_slow_fade,
    "moon_hold": gen_moon_hold,
    "chop": gen_chop,
    "rug": gen_rug,
}


def summarize(results: List[ReplayResult]) -> Dict:
    n = len(results)
    nets = [r.net_usd for r in results]
    wins = [x for x in nets if x > 0]
    losses = [-x for x in nets if x <= 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    reasons: Dict[str, int] = {}
    for r in results:
        reasons[r.reason.split(":")[0]] = reasons.get(r.reason.split(":")[0], 0) + 1
    return {
        "closes": n,
        "net_usd": round(sum(nets), 2),
        "win_rate": round(len(wins) / n, 3) if n else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else 0.0,
        "avg_net_usd": round(sum(nets) / n, 2) if n else 0.0,
        "advisor_exits": sum(r.advisor_exits for r in results),
        "reasons": reasons,
    }


def evaluate(paths: Dict[str, List[Tuple[float, float]]],
             exit_cfg: Optional[Dict] = None,
             advisor: Optional[ExitAdvisor] = None) -> Dict:
    base = summarize([replay(m, exit_cfg, None, name) for name, m in paths.items()])
    out = {"baseline": base}
    if advisor is not None:
        adv = summarize([replay(m, exit_cfg, advisor, name)
                         for name, m in paths.items()])
        adv["delta_net_usd_vs_baseline"] = round(
            adv["net_usd"] - base["net_usd"], 2)
        out["advisor(%s)" % advisor.name] = adv
    return out


def report_text(rep: Dict) -> str:
    lines = ["# AI exit-manager shadow evaluation",
             "",
             "Marks are not fills. Exit slippage 500bps + priority fee applied.",
             ""]
    for side, s in rep.items():
        lines.append("## %s" % side)
        lines.append("- closes: %d | net: $%.2f | win rate: %.1f%% | PF: %.2f | avg: $%.2f" % (
            s["closes"], s["net_usd"], s["win_rate"] * 100, s["profit_factor"],
            s["avg_net_usd"]))
        if "delta_net_usd_vs_baseline" in s:
            lines.append("- delta vs baseline: $%+.2f" % s["delta_net_usd_vs_baseline"])
        lines.append("- advisor-attributed exits: %d" % s["advisor_exits"])
        lines.append("- reasons: %s" % json.dumps(s["reasons"]))
        lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--quote-paths", default=None)
    ap.add_argument("--seed", type=int, default=20260927)
    args = ap.parse_args(argv)

    paths: Dict[str, List[Tuple[float, float]]] = {}
    if args.synthetic:
        for name, gen in GENERATORS.items():
            paths[name] = gen(args.seed)
    if args.quote_paths:
        for fp in sorted(glob.glob(os.path.join(args.quote_paths, "*.jsonl"))):
            ticks = load_z4_quote_path(fp)
            marks = [(q.ts, q.price_usd) for q in ticks if q.price_usd]
            if len(marks) >= 12:
                paths[os.path.basename(fp)] = marks
    if not paths:
        print("no paths: pass --synthetic or --quote-paths <dir>")
        return 2

    advisor = LocalExitPolicy()
    rep = evaluate(paths, LIVE_EXIT_CFG, advisor)
    print(report_text(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
