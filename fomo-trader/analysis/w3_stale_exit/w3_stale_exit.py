"""W3 offline stale-exit replay. No live decision path or network access."""

from collections import Counter
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path

from analysis import replay

LOG = logging.getLogger(__name__)
TIMEOUTS = (180, 300, 600, 900, 1800, 3600, 7200, 14400, 28800)
PARAMS = Path(__file__).with_name("w3_stale_exit_params.json")
METRIC_KEYS = ("n", "usd", "usd_n", "win_rate", "pf", "max_dd_usd", "native_by_chain")


def fallback(counters, reason):
    counters[reason] += 1
    LOG.warning("W3_FALLBACK reason=%s count=%d", reason, counters[reason])


def load_params(path=PARAMS, counters=None):
    """Read research verdict; missing/corrupt data disables any future rule."""
    counters = counters if counters is not None else Counter()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or type(data.get("ship_recommend")) is not bool or not isinstance(data.get("rule"), str):
            raise ValueError("invalid schema")
        return data
    except (OSError, ValueError, TypeError):
        fallback(counters, "params_unavailable")
        return {"track": "W3", "ship_recommend": False, "rule": ""}


def _finite_positive(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        return False
    return math.isfinite(number) and number > 0


def _parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def prepare(trades, as_of=None):
    """Validate once, then sort nominal entry timestamps for the IS/OOS split.

    Bad price/P&L records cannot enter replay.metrics. Bad timing records with
    valid economics remain in the baseline but receive no timeout treatment.
    """
    now = as_of or datetime.now(timezone.utc)
    counters = Counter()
    eligible = []
    for row in trades:
        if not isinstance(row, dict):
            fallback(counters, "malformed_trade")
            continue
        chain = replay.chain_of(row)
        pnl_key = "realized_bnb" if chain == "bsc" else "realized_sol"
        # Historical BSC rows use buy_sol for the stake despite realized_bnb.
        stake = row.get("buy_bnb") or row.get("buy_sol")
        if chain not in ("solana", "bsc") or not all(_finite_positive(row.get(k)) for k in ("entry", "exit", "peak")) or not _finite_positive(stake):
            fallback(counters, "invalid_economics")
            continue
        try:
            pnl = float(row[pnl_key])
            if not math.isfinite(pnl):
                raise ValueError
            if chain == "bsc" and not _finite_positive(row.get("sol_usd")):
                raise ValueError
            if chain == "solana" and row.get("sol_usd") is not None and not _finite_positive(row["sol_usd"]):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            fallback(counters, "invalid_economics")
            continue
        entry, close = _parse_ts(row.get("entry_ts")), _parse_ts(row.get("close_ts"))
        duration = None
        if entry is None or close is None or entry.tzinfo != close.tzinfo:
            fallback(counters, "missing_or_mixed_timestamp")
        else:
            # Naive journal clocks have no zone annotation. Comparison with UTC
            # only catches clearly future records; it cannot repair EDT/UTC.
            utc_entry = entry.replace(tzinfo=timezone.utc) if entry.tzinfo is None else entry.astimezone(timezone.utc)
            utc_close = close.replace(tzinfo=timezone.utc) if close.tzinfo is None else close.astimezone(timezone.utc)
            if utc_entry > now or utc_close > now:
                fallback(counters, "future_timestamp")
            elif close < entry:
                fallback(counters, "clock_skew")
            else:
                duration = (close - entry).total_seconds()
                if entry.date() == close.date() and 7200 <= duration <= 18000:
                    # Four-hour EDT/UTC switch can mimic this whole interval.
                    duration = None
                    fallback(counters, "ambiguous_four_hour_clock")
        eligible.append((row, entry, duration))
    # Unmatched closes cannot be assigned chronologically by entry time.
    ordered = sorted((x for x in eligible if x[1] is not None),
                     key=lambda x: x[1].replace(tzinfo=timezone.utc) if x[1].tzinfo is None
                     else x[1].astimezone(timezone.utc))
    return ordered, counters, len(eligible)


def _adjust(row, affected, cost_multiplier):
    if not affected:
        return row
    actual_native, _ = replay.net_pnl(row, cost_multiplier=cost_multiplier)
    target = min(0.0, actual_native)
    copy = dict(row)
    key = "realized_bnb" if replay.chain_of(row) == "bsc" else "realized_sol"
    copy[key] = target + replay.estimated_cost(row) * cost_multiplier
    return copy


def _score(rows, timeout, cost_multiplier=1):
    adjusted = [_adjust(row, duration is not None and duration > timeout, cost_multiplier)
                for row, _, duration in rows]
    return replay.metrics(adjusted, cost_multiplier=cost_multiplier)


def _edge(candidate, baseline):
    return (candidate["usd"] - baseline["usd"]) / candidate["n"] if candidate["n"] else 0.0


def sweep(trades, as_of=None):
    ordered, counters, valid_count = prepare(trades, as_of)
    cut = len(ordered) * 2 // 3
    parts = {"is": ordered[:cut], "oos": ordered[cut:]}
    baseline = {k: replay.metrics([x[0] for x in part]) for k, part in parts.items()}
    stress_base = {k: replay.metrics([x[0] for x in part], cost_multiplier=3) for k, part in parts.items()}
    chains = ("solana", "bsc")
    results = []
    for timeout in TIMEOUTS:
        scores = {k: _score(part, timeout) for k, part in parts.items()}
        stress = {k: _score(part, timeout, 3) for k, part in parts.items()}
        affected = {k: sum(d is not None and d > timeout for _, _, d in part) for k, part in parts.items()}
        chain_edges = {}
        for chain in chains:
            sub = [x for x in parts["oos"] if replay.chain_of(x[0]) == chain]
            base = replay.metrics([x[0] for x in sub])
            chain_edges[chain] = {"n": len(sub), "affected": sum(d is not None and d > timeout for _, _, d in sub),
                                  "edge_usd_per_trade": _edge(_score(sub, timeout), base)}
        is_edge = _edge(scores["is"], baseline["is"])
        oos_edge = _edge(scores["oos"], baseline["oos"])
        stress_is = _edge(stress["is"], stress_base["is"])
        stress_oos = _edge(stress["oos"], stress_base["oos"])
        retention = oos_edge / is_edge if is_edge > 0 else None
        gate = {
            "chronological_split": bool(parts["is"] and parts["oos"]),
            "oos_edge_retention_60pct": is_edge > 0 and oos_edge >= 0.6 * is_edge,
            "oos_not_worse": oos_edge >= -1e-9,
            "wr_le_90pct": scores["oos"]["win_rate"] <= 0.9,
            "oos_decay_le_70pct": retention is not None and retention >= 0.3,
            "both_chains_benefit": all(chain_edges[c]["affected"] > 0 and chain_edges[c]["edge_usd_per_trade"] > 0 for c in chains),
            "stress_same_verdict": stress_is > 0 and stress_oos >= 0.6 * stress_is and stress_oos >= -1e-9,
        }
        results.append({"timeout_s": timeout, "is": scores["is"], "oos": scores["oos"],
                        "stress_is": stress["is"], "stress_oos": stress["oos"],
                        "affected": affected, "chain_oos": chain_edges, "is_edge": is_edge,
                        "oos_edge": oos_edge, "retention": retention, "gates": gate})
    preliminary = [all(item["gates"].values()) for item in results]
    for i, item in enumerate(results):
        adjacent = ((i > 0 and preliminary[i - 1]) or
                    (i + 1 < len(results) and preliminary[i + 1]))
        item["gates"]["broad_plateau"] = bool(adjacent)
        item["pass"] = all(item["gates"].values())
    passing = [r for r in results if r["pass"]]
    best = max(passing, key=lambda r: (r["oos_edge"], -r["timeout_s"])) if passing else None
    return {"baseline": baseline, "stress_baseline": stress_base, "results": results,
            "best": best, "counters": dict(counters), "valid_economic_closes": valid_count,
            "split_n": {k: len(v) for k, v in parts.items()}, "unmatched_excluded": valid_count - len(ordered)}


def params_from(result):
    best = result["best"]
    return {"track": "W3", "ship_recommend": best is not None,
            "rule": f"Close paper position when elapsed time since entry exceeds {best['timeout_s']} seconds." if best else "",
            "timeouts_swept": list(TIMEOUTS), "best_timeout": best["timeout_s"] if best else None,
            "is_metrics": best["is"] if best else {}, "oos_metrics": best["oos"] if best else {},
            "gates": best["gates"] if best else {str(r["timeout_s"]): r["gates"] for r in result["results"]},
            "notes": "Historical replay only; ambiguous naive clocks and unknown timeout fills limit inference."}


if __name__ == "__main__":
    trades, _ = replay.load_journal()
    print(json.dumps(params_from(sweep(trades)), indent=2, allow_nan=False))
