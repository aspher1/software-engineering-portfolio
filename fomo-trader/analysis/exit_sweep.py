"""Offline paper exit sweep. No quote path or executable fills are available.

This extends replay's entry/peak/exit counterfactual semantics. A tighter
recorded trail or dump threshold can estimate an earlier fill; wider thresholds,
new stale times, and other dump windows cannot reconstruct later/earlier quotes.
Rug gaps retain the recorded final exit. TP threshold fills are optimistic:
peak proves reach, not ordering, depth, or actual execution. Results are
anchored to recorded paper realized P&L by subtracting the baseline model's
price ratio; both baseline and candidate receive replay's round-trip costs.
"""

import argparse
from dataclasses import dataclass, replace
from datetime import datetime
import json
from pathlib import Path
import re

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis import replay


@dataclass(frozen=True)
class Params:
    take_profits: tuple = ((50, 50),)
    trailing_stop_pct: int = 25
    hard_stop_pct: int = 35
    stale_exit_min: int = 30
    stale_exit_max_gain_pct: int = 10
    dump_drop_pct: int = 12
    dump_window_sec: int = 60
    trail_after_tp_pct: int = 12

    def patch(self):
        return {"exit": {k: [list(x) for x in v] if k == "take_profits" else v
                         for k, v in vars(self).items()}}


def from_config(config):
    ex = config["exit"]
    return Params(tuple(tuple(x) for x in ex["take_profits"]),
                  *(ex[k] for k in list(Params.__dataclass_fields__)[1:]))


def age_minutes(trade):
    if not trade.get("entry_ts") or not trade.get("close_ts"):
        return None
    return (datetime.fromisoformat(trade["close_ts"]) -
            datetime.fromisoformat(trade["entry_ts"])).total_seconds() / 60


def stale_eligible_at_close(trade, params):
    """Eligibility at the observed close, never an assumed earlier fill."""
    age = age_minutes(trade)
    if age is None or params.stale_exit_min <= 0 or age < params.stale_exit_min:
        return False
    return (float(trade["exit"]) / float(trade["entry"]) - 1) * 100 < params.stale_exit_max_gain_pct


def modeled_price(trade, params):
    """Unit stake exit value under observable, deliberately limited triggers."""
    entry, peak, recorded = (float(trade[k]) for k in ("entry", "peak", "exit"))
    if entry <= 0 or peak <= 0:
        raise ValueError("entry and peak must be positive")
    reason = str(trade.get("reason", "")).lower()
    rug = replay.rug_gap(trade)
    final = recorded
    rung_hit = any(peak >= entry * (1 + gain / 100) for gain, _ in params.take_profits)
    if not rug:
        if "trailing stop" in reason:
            observed = re.search(r"([\d.]+)% from peak", reason)
            if observed:
                effective = min(params.trailing_stop_pct,
                                params.trail_after_tp_pct if rung_hit else 100)
                if effective < float(observed.group(1)):
                    final = max(final, peak * (1 - effective / 100))
        if "dump detector" in reason and params.dump_drop_pct > 0:
            match = re.search(r"-([\d.]+)% in (\d+)s", reason)
            if match and params.dump_window_sec == int(match.group(2)) and params.dump_drop_pct < float(match.group(1)):
                observed_drop = float(match.group(1)) / 100
                if observed_drop < 1:
                    # The reason reports the drop from the recent-window
                    # maximum. Infer that maximum from the recorded fill;
                    # lifetime peak may be older and must not stand in.
                    recent_max = recorded / (1 - observed_drop)
                    final = max(final, recent_max * (1 - params.dump_drop_pct / 100))
        # Earlier stale eligibility can be classified separately, but the
        # price at that earlier timeout is absent; no fill is fabricated.
        stop = entry * (1 - params.hard_stop_pct / 100)
        if final < stop:
            final = stop
    remain = 1.0
    proceeds = 0.0
    for gain, sold_pct in params.take_profits:
        if peak >= entry * (1 + gain / 100) and remain > 0:
            fraction = min(remain, sold_pct / 100)
            proceeds += fraction * entry * (1 + gain / 100)
            remain -= fraction
    return (proceeds + remain * final) / entry


def candidate_trade(trade, params, baseline, config):
    """Adjust recorded realized native P&L by modeled cash-flow difference."""
    row = dict(trade)
    chain = replay.chain_of(trade)
    stake = float(trade.get("buy_bnb") or trade.get("buy_sol") or 0)
    delta = stake * (modeled_price(trade, params) - modeled_price(trade, baseline))
    key = "realized_bnb" if chain == "bsc" else "realized_sol"
    row[key] = float(trade.get(key) or 0) + delta
    row["exit"] = float(trade["entry"]) * modeled_price(trade, params)
    # replay.estimated_cost uses the effective round-trip outflow ratio.
    # Preserve baseline's exact recorded cost when model delta is zero.
    if abs(delta) < 1e-15:
        row["exit"] = trade["exit"]
    return row


def scored(trades, params, baseline, config, stress=False):
    return replay.metrics([candidate_trade(t, params, baseline, config) for t in trades], config, stress)


def edge_by_group(trades, params, baseline, config, field):
    groups = {}
    for t in trades:
        key = replay.attributes(t).get(field) if field != "reason" else t.get("reason", "").split(":")[0].split(" -")[0]
        groups.setdefault(key, []).append(t)
    return {k: scored(v, params, baseline, config)["usd"] - replay.metrics(v, config)["usd"] for k, v in groups.items()}


def validation(is_base, is_candidate, oos_base, oos_candidate, oos_stress,
               group_edges, plateau_ok):
    is_edge = (is_candidate["usd"] - is_base["usd"]) / is_base["n"]
    oos_edge = (oos_candidate["usd"] - oos_base["usd"]) / oos_base["n"]
    flags = []
    if is_edge <= 0 or oos_edge < .6 * is_edge:
        flags.append("edge retention below 60%")
    if oos_candidate["win_rate"] > .9:
        flags.append("win rate above 90%")
    for field, values in group_edges.items():
        positive = [x for x in values.values() if x > 0]
        if len(positive) < 2 or max(positive, default=0) >= .8 * sum(positive):
            flags.append("edge concentrated in " + field)
    if oos_stress["usd"] <= 0:
        flags.append("OOS 3x slippage net nonpositive")
    if not plateau_ok:
        flags.append("no broad IS plateau")
    return {"is_edge_per_trade": is_edge, "oos_edge_per_trade": oos_edge,
            "retention": oos_edge / is_edge if is_edge > 0 else None,
            "flags": flags, "ships": not flags}


def ladders():
    gains = (30, 40, 50, 60, 80, 100)
    fractions = (25, 50, 75, 100)
    out = [((g, f),) for g in gains for f in fractions]
    out += [((g1, f1), (g2, f2)) for g1 in gains for g2 in gains if g2 > g1
            for f1 in (25, 50, 75) for f2 in (25, 50, 75, 100) if f1 + f2 <= 100]
    return out


def dimensions():
    return {
        "take_profits": ladders(),
        "trailing_stop_pct": (10, 15, 20, 25, 30, 40),
        "hard_stop_pct": (20, 25, 30, 35, 45, 50),
        "stale_exit_min": (0, 10, 15, 30, 45, 60),
        "stale_exit_max_gain_pct": (5, 10, 15, 20),
        "dump_drop_pct": (0, 8, 12, 15, 20),
        "dump_window_sec": (30, 60, 120),
        "trail_after_tp_pct": (8, 12, 15, 20, 25),
    }


def plateau_support(params, gain, baseline, dims, gain_for):
    """Return changed axes whose adjacent IS values retain half the gain."""
    supported = []
    for field, values in dims.items():
        current = getattr(params, field)
        if current not in values or current == getattr(baseline, field):
            continue
        idx = values.index(current)
        neighbors = [gain_for(replace(params, **{field: values[j]}))
                     for j in (idx - 1, idx + 1) if 0 <= j < len(values)]
        if neighbors and min(neighbors) >= .5 * gain:
            supported.append(field)
    return supported


def sweep(trades, config):
    baseline = from_config(config)
    is_rows, oos_rows = trades[:45], trades[45:]
    is_base = replay.metrics(is_rows, config)
    oos_base = replay.metrics(oos_rows, config)
    seen = {baseline}
    candidates = []
    def add(p):
        if p not in seen:
            seen.add(p)
            candidates.append((scored(is_rows, p, baseline, config)["usd"] - is_base["usd"], p))
    dims = dimensions()
    # Coarse one-factor grid, then pairwise refinement among IS leaders.
    for field, values in dims.items():
        for value in values:
            add(replace(baseline, **{field: value}))
    leaders = [p for _, p in sorted(candidates, key=lambda x: x[0], reverse=True)[:8]]
    for p in leaders:
        changed = [k for k in dims if getattr(p, k) != getattr(baseline, k)]
        for field in dims:
            if field not in changed:
                for value in dims[field]:
                    add(replace(p, **{field: value}))
    candidates.sort(key=lambda x: x[0], reverse=True)
    scored_is = dict((p, gain) for gain, p in candidates)
    def gain_for(p):
        if p not in scored_is:
            scored_is[p] = scored(is_rows, p, baseline, config)["usd"] - is_base["usd"]
        return scored_is[p]
    results = []
    for gain, p in candidates:
        if gain <= 0:
            break
        oos = scored(oos_rows, p, baseline, config)
        stress = scored(oos_rows, p, baseline, config, True)
        axes = plateau_support(p, gain, baseline, dims, gain_for)
        ok = len(axes) >= 2
        groups = {field: edge_by_group(oos_rows, p, baseline, config, field)
                  for field in ("chain", "liquidity", "reason")}
        gate = validation(is_base, scored(is_rows, p, baseline, config), oos_base,
                          oos, stress, groups, ok)
        results.append({"params": p, "is_gain": gain, "plateau_axes": axes,
                        "is": scored(is_rows, p, baseline, config), "oos": oos,
                        "is_stress": scored(is_rows, p, baseline, config, True),
                        "oos_stress": stress, "gate": gate})
    winners = [r for r in results if r["gate"]["ships"]]
    return {"baseline": baseline, "is_base": is_base, "oos_base": oos_base,
            "is_base_stress": replay.metrics(is_rows, config, True),
            "oos_base_stress": replay.metrics(oos_rows, config, True),
            "tested": len(seen), "positive_is": len(results),
            "winner": max(winners, key=lambda r: (len(r["plateau_axes"]), r["oos"]["usd"]), default=None),
            "top": results[:5], "plateau_count": sum(bool(r["plateau_axes"]) for r in results)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine readable summary")
    args = parser.parse_args()
    config = json.loads(replay.CONFIG.read_text())
    if config.get("dry_run") is not True:
        raise SystemExit("paper config dry_run must be true")
    trades, counts = replay.load_journal()
    result = sweep(trades, config)
    def encode(obj):
        if isinstance(obj, Params):
            return obj.patch()["exit"]
        raise TypeError(type(obj).__name__)
    if args.json:
        print(json.dumps({"counts": counts, "split": [45, len(trades)-45], **result}, default=encode, indent=2))
    else:
        print(f"Journal {counts}; close_ts IS/OOS 45/{len(trades)-45}; grid {result['tested']} candidates")
        for part in ("is", "oos"):
            print(part.upper(), "baseline", result[part + "_base"], "3x", result[part + "_base_stress"])
        for row in result["top"]:
            print("candidate", row["params"].patch(), "IS", row["is"], "OOS", row["oos"],
                  "OOS 3x", row["oos_stress"], "plateau", row["plateau_axes"], "gate", row["gate"])
        print("Recommendation:", result["winner"]["params"].patch() if result["winner"] else "NONE")


if __name__ == "__main__":
    main()
