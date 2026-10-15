"""X3 offline paper signal-gain veto study. No bot integration."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analysis import replay, signal_filter

HERE = Path(__file__).resolve().parent
PARAMS_PATH = HERE / "x3_lowgain.json"
THRESHOLDS = (75, 100, 125, 150)


def _finite_nonnegative(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _params(data):
    if not isinstance(data, dict) or type(data.get("ship_recommend")) is not bool:
        raise ValueError("invalid params")
    rule = data.get("rule")
    if not isinstance(rule, dict):
        raise ValueError("invalid rule")
    gain = _finite_nonnegative(rule.get("gain_lt_pct"))
    if gain not in THRESHOLDS:
        raise ValueError("invalid gain threshold")
    ratio = rule.get("ratio_lt")
    if ratio is not None and _finite_nonnegative(ratio) != 4:
        raise ValueError("invalid ratio threshold")
    return gain, ratio


def evaluate(signal, *, params=None, path=PARAMS_PATH, counters=None):
    """Pure fail-open decision; counters['fallbacks'] increments on uncertainty."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8")) if params is None else params
        threshold, ratio_limit = _params(data)
        if not data["ship_recommend"]:
            return {"approved": True, "fallback": False, "reason": "not_shipped"}
        if not isinstance(signal, dict):
            raise ValueError("invalid signal")
        gain = _finite_nonnegative(signal.get("m15_gain_pct"))
        if gain is None:
            raise ValueError("invalid gain")
        if ratio_limit is not None:
            ratio = _finite_nonnegative(signal.get("buy_sell_ratio"))
            if ratio is None:
                raise ValueError("invalid ratio")
            if ratio >= ratio_limit:
                return {"approved": False, "fallback": False, "reason": "ratio_veto"}
        return {"approved": gain < threshold, "fallback": False,
                "reason": "gain_pass" if gain < threshold else "gain_veto"}
    except Exception:
        try:
            if counters is not None:
                counters["fallbacks"] = counters.get("fallbacks", 0) + 1
        except Exception:
            pass
        return {"approved": True, "fallback": True, "reason": "fallback"}


def approve(signal, *, params=None, path=PARAMS_PATH, counters=None):
    """Approve by default whenever evidence or parameters are unusable."""
    return evaluate(signal, params=params, path=path, counters=counters)["approved"]


def utc_entry(row):
    stamp = datetime.fromisoformat(row["trade"]["entry_ts"])
    return stamp + timedelta(hours=row.get("utc_offset_hours") or 0)


def population(rows):
    valid, excluded = [], {"no_close": 0, "no_entry_ts": 0, "invalid_gain": 0,
                           "post_entry_signal": 0, "invalid_timestamp": 0}
    for row in rows:
        trade = row.get("trade")
        if not trade or not trade.get("close_record"):
            excluded["no_close"] += 1
            continue
        if not trade.get("entry_ts"):
            excluded["no_entry_ts"] += 1
            continue
        gain = _finite_nonnegative(signal_filter.signal_values(row).get("m15_gain_pct"))
        if gain is None:
            excluded["invalid_gain"] += 1
            continue
        try:
            stamp = datetime.fromisoformat(trade["entry_ts"])
            signal_ts = row.get("signal_ts")
            if signal_ts is None or signal_ts > stamp:
                excluded["post_entry_signal"] += 1
                continue
            utc_entry(row)
        except (TypeError, ValueError, OverflowError):
            excluded["invalid_timestamp"] += 1
            continue
        valid.append(row)
    valid.sort(key=lambda r: (utc_entry(r), r.get("line_index", 0)))
    return valid, excluded


def split_rows(rows):
    """Chronological floor(2N/3) split, including empty inputs."""
    ordered = sorted(rows, key=lambda r: (utc_entry(r), r.get("line_index", 0)))
    cut = len(ordered) * 2 // 3
    return ordered[:cut], ordered[cut:]


def _metric(rows, stress=False):
    trades = [r["trade"] for r in rows]
    result = replay.metrics(trades, stress=stress)
    return {"n": result["n"], "pnl_usd": result["usd"],
            "pf": result["pf"] if math.isfinite(result["pf"]) else None,
            "win_rate": result["win_rate"], "max_drawdown_usd": result["max_dd_usd"],
            "usd_per_trade": result["usd"] / result["n"] if result["n"] else None}


def _keep(row, threshold, ratio_limit=None):
    values = signal_filter.signal_values(row)
    gain = _finite_nonnegative(values.get("m15_gain_pct"))
    ratio = _finite_nonnegative(values.get("buy_sell_ratio")) if ratio_limit is not None else None
    return gain is not None and gain < threshold and (ratio_limit is None or
                                                      ratio is not None and ratio < ratio_limit)


def summarize(rows, threshold=None, ratio_limit=None):
    baseline = _metric(rows)
    selected = [r for r in rows if threshold is None or _keep(r, threshold, ratio_limit)]
    result = _metric(selected)
    stressed = _metric(selected, stress=True)
    b = baseline["usd_per_trade"]
    r = result["usd_per_trade"]
    result["edge_usd_per_trade"] = r - b if r is not None and b is not None else None
    result["stress_3x_slippage_pnl_usd"] = stressed["pnl_usd"]
    result["vetoed_n"] = len(rows) - len(selected)
    return result


def _concentration(rows, threshold):
    selected = [r for r in rows if _keep(r, threshold)]
    amounts = [(replay.net_pnl(r["trade"])[1], r) for r in selected]
    amounts.sort(key=lambda pair: pair[0] if pair[0] is not None else -math.inf, reverse=True)
    leaders = [{"name": r["trade"].get("name"), "entry_ts": r["trade"].get("entry_ts"),
                "chain": replay.chain_of(r["trade"]), "pnl_usd": usd}
               for usd, r in amounts[:3]]
    positives = [usd for usd, _ in amounts if usd is not None and usd > 0]
    total = sum(usd for usd, _ in amounts if usd is not None)
    return {"top_contributors": leaders, "pnl_without_top_1_winner": total - sum(positives[:1]),
            "pnl_without_top_2_winners": total - sum(positives[:2]),
            "positive_contributor_count": len(positives)}


def load_rows():
    trades, journal_counts = replay.load_journal()
    log_lines = replay.LOG.read_text(encoding="utf-8", errors="replace").splitlines(True)
    deaths_path = replay.LOG.with_name("deaths.log")
    deaths_lines = deaths_path.read_text(encoding="utf-8", errors="replace").splitlines(True)
    signals = replay.parse_signals(replay.LOG)
    joined, join_meta = signal_filter.join_signals(signals, trades, log_lines,
                                                  deaths_lines=deaths_lines)
    rows, excluded = population(joined)
    return rows, {"journal_counts": journal_counts, "parsed_signals": len(signals),
                  "join_meta": join_meta, "excluded_join_rows": excluded,
                  "log_lines": len(log_lines)}


def run_study(rows, metadata=None):
    ins, outs = split_rows(rows)
    sweep = {}
    for threshold in THRESHOLDS:
        sweep[str(threshold)] = {"is": summarize(ins, threshold),
                                 "oos": summarize(outs, threshold),
                                 "concentration": {"is": _concentration(ins, threshold),
                                                   "oos": _concentration(outs, threshold)}}
    best = max(THRESHOLDS, key=lambda t: (sweep[str(t)]["is"]["edge_usd_per_trade"]
                   if sweep[str(t)]["is"]["edge_usd_per_trade"] is not None else -math.inf, -t))
    chosen = sweep[str(best)]
    oos_edge = chosen["oos"]["edge_usd_per_trade"]
    is_edge = chosen["is"]["edge_usd_per_trade"]
    adjacent = [t for t in THRESHOLDS if abs(THRESHOLDS.index(t) - THRESHOLDS.index(best)) == 1]
    plateau = bool(oos_edge is not None and oos_edge > 0 and any(
        (sweep[str(t)]["oos"]["edge_usd_per_trade"] or 0) > 0 for t in adjacent))
    mid = len(outs) // 2
    regimes = {"chains": {chain: summarize([r for r in outs if replay.chain_of(r["trade"]) == chain], best)
                           for chain in ("solana", "bsc")},
               "oos_halves": {"early": summarize(outs[:mid], best),
                              "late": summarize(outs[mid:], best)}}
    combo = None
    if oos_edge is not None and oos_edge > 0:
        combo = {"feature": "buy_sell_ratio < 4", "is": summarize(ins, best, 4),
                 "oos": summarize(outs, best, 4)}
        combo["improves_oos_edge"] = (combo["oos"]["edge_usd_per_trade"] is not None and
                                      combo["oos"]["edge_usd_per_trade"] > oos_edge)
    retention = oos_edge / is_edge if is_edge is not None and is_edge > 0 and oos_edge is not None else None
    gate = {
        "positive_is_edge": is_edge is not None and is_edge > 0,
        "positive_oos_edge": oos_edge is not None and oos_edge > 0,
        "retains_60pct_is_edge": retention is not None and retention >= .6,
        "oos_wr_at_most_90pct": chosen["oos"]["win_rate"] <= .9,
        "positive_oos_costed_pnl": chosen["oos"]["pnl_usd"] > 0,
        "positive_oos_stress_pnl": chosen["oos"]["stress_3x_slippage_pnl_usd"] > 0,
        "adjacent_oos_plateau": plateau,
        "not_one_chain_only": all(regimes["chains"][c]["n"] > 0 and
                                  regimes["chains"][c]["pnl_usd"] > 0 for c in ("solana", "bsc")),
        "not_one_half_only": all(regimes["oos_halves"][h]["n"] > 0 and
                                 regimes["oos_halves"][h]["pnl_usd"] > 0 for h in ("early", "late")),
        "not_top_two_dependent": chosen["concentration"]["oos"]["pnl_without_top_2_winners"] > 0,
    }
    return {"metadata": metadata or {}, "n": len(rows), "is_n": len(ins), "oos_n": len(outs),
            "is_last_entry_ts": ins[-1]["trade"]["entry_ts"] if ins else None,
            "oos_first_entry_ts": outs[0]["trade"]["entry_ts"] if outs else None,
            "is_last_entry_utc": utc_entry(ins[-1]).isoformat() if ins else None,
            "oos_first_entry_utc": utc_entry(outs[0]).isoformat() if outs else None,
            "baseline": {"is": summarize(ins), "oos": summarize(outs)},
            "sweep": sweep, "selected_is_threshold": best, "retention_ratio": retention,
            "plateau": {"passed": plateau, "adjacent_thresholds": adjacent},
            "regimes": regimes, "combo": combo, "gates": gate,
            "ship_recommend": bool(ins and outs and all(gate.values())),
            "rule": {"gain_lt_pct": best, "ratio_lt": None},
            "failed_gates": [key for key, value in gate.items() if not value]}


def main():
    rows, metadata = load_rows()
    result = run_study(rows, metadata)
    PARAMS_PATH.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("n", "is_n", "oos_n", "selected_is_threshold",
                                                   "failed_gates", "ship_recommend")}, indent=2))


if __name__ == "__main__":
    main()
