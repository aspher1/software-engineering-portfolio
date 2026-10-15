"""Offline paper-journal replay. Prices and counterfactual fills are estimates."""

import argparse
from collections import defaultdict, deque
from datetime import datetime
import json
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
JOURNAL = ROOT / "runs/paper-1h/trades.jsonl"
LOG = ROOT / "runs/paper-1h/bot.log"
CONFIG = ROOT / "runs/paper-1h/config.json"

# Incremental round-trip execution estimates, not observed network fees.
# Journal P&L already reflects paper quotes; these costs stress those quotes.
SWAP_FEE = {"solana": 0.0025, "bsc": 0.0025}  # fraction per leg
SLIPPAGE = 0.0015  # fraction per leg; --stress triples this
BNB_GAS_NATIVE_PER_LEG = 0.00002  # estimated BNB gas
SOL_LAMPORTS = 1_000_000_000
SOL_FALLBACK_USD = 115.0  # approximate journal-era SOL price for 24 early closes without quotes


def chain_of(row):
    return row.get("chain") or ("bsc" if str(row.get("mint", "")).startswith("0x") else "solana")


def load_journal(path=JOURNAL):
    """FIFO pair by mint; closes retain append order across mixed local/UTC ts."""
    pending = defaultdict(deque)
    trades = []
    counts = defaultdict(int)
    for line in Path(path).open(encoding="utf-8"):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            counts["malformed_lines"] += 1
            continue
        kind = row.get("type")
        counts[kind] += 1
        if kind == "entry":
            pending[row["mint"]].append(row)
        elif kind == "close":
            entry = pending[row["mint"]].popleft() if pending[row["mint"]] else None
            merged = dict(entry or {})
            merged.update(row)
            merged["entry_record"] = entry
            merged["close_record"] = row
            merged["entry_ts"] = entry.get("ts") if entry else None
            merged["close_ts"] = row.get("ts")
            merged["chain"] = chain_of(merged)
            merged["unmatched_close"] = entry is None
            trades.append(merged)
    counts["unmatched_entries"] = sum(map(len, pending.values()))
    counts["unmatched_closes"] = sum(t["unmatched_close"] for t in trades)
    return trades, dict(counts)


SIGNAL = re.compile(
    r"\[(?P<time>\d\d:\d\d:\d\d)\] FOMO SIGNAL(?: \[(?P<chain>[^]]+)\])?: "
    r"(?P<name>.*?) \+(?P<gain>-?[\d.]+)% (?P<window>\w+), "
    r"buys/sells (?P<ratio>[\d.]+) \(liq \$(?P<liq>[\d,.]+)\)"
    r"(?: \[(?P<source>[^]]+)\])?(?: (?P<url>https?://\S+))?"
)
PREPUMP = re.compile(
    r"\[(?P<time>\d\d:\d\d:\d\d)\] PRE-PUMP \[(?P<chain>[^]]+)\]: "
    r"(?P<name>.*?) 5m \+(?P<m5>-?[\d.]+)% m5 / \+(?P<m15>-?[\d.]+)% m15, "
    r"buys/sells (?P<ratio>[\d.]+) \(liq \$(?P<liq>[\d,.]+)\)"
    r"(?: (?P<url>https?://\S+))?"
)


def parse_signals(path=LOG):
    signals = []
    for line in Path(path).open(encoding="utf-8", errors="replace"):
        match = SIGNAL.search(line)
        early = False
        if not match:
            match = PREPUMP.search(line)
            early = bool(match)
        if not match:
            continue
        d = match.groupdict()
        signals.append({"time": d["time"], "chain": d.get("chain") or "solana",
                        "name": d["name"], "m15_gain_pct": float(d.get("m15") or d.get("gain")),
                        "m5_gain_pct": float(d["m5"]) if early else None,
                        "buy_sell_ratio": float(d["ratio"]),
                        "liquidity_usd": float(d["liq"].replace(",", "")),
                        "window": "5m" if early else d["window"],
                        "source": d.get("source") or ("geckoterminal" if d.get("url") else "unknown"),
                        "url": d.get("url"), "early": early})
    return signals


def estimated_cost(trade, config=None, stress=False):
    """Round-trip fee/slippage on stake plus chain transaction estimates."""
    config = config or {}
    chain = chain_of(trade)
    stake = float(trade.get("buy_bnb") or trade.get("buy_sol") or 0)
    price_ratio = max(0.0, float(trade.get("exit") or 0) / float(trade.get("entry") or 1))
    gross_out = stake * price_ratio
    variable = (stake + gross_out) * (SWAP_FEE[chain] + SLIPPAGE * (3 if stress else 1))
    fixed = (2 * BNB_GAS_NATIVE_PER_LEG if chain == "bsc" else
             2 * float(config.get("exit", {}).get("max_priority_fee_lamports", 2_000_000)) / SOL_LAMPORTS)
    return variable + fixed


def net_pnl(trade, config=None, stress=False, cost_multiplier=1):
    chain = chain_of(trade)
    raw = trade.get("realized_bnb") if chain == "bsc" else trade.get("realized_sol")
    native = float(raw or 0) - estimated_cost(trade, config, stress) * cost_multiplier
    rate = trade.get("sol_usd")
    if rate is None and chain == "solana":
        rate = SOL_FALLBACK_USD
    usd = native * float(rate) if rate is not None else None
    return native, usd


def bucket(value, edges, labels):
    if value is None:
        return "missing"
    for edge, label in zip(edges, labels):
        if float(value) < edge:
            return label
    return labels[-1]


def attributes(trade):
    ts = trade.get("entry_ts") or trade.get("close_ts")
    top1 = trade.get("holder_top1_pct")
    holder = ("missing" if top1 is None else
              "<10" if float(top1) < 10 else
              "10-25" if float(top1) < 25 else
              "25-40" if float(top1) <= 40 else ">40")
    burned = trade.get("lp_burn_pct")
    lp = ("burned" if burned is not None and float(burned) > 0 else
          "locked" if trade.get("lp_locked") is True else "unknown")
    return {
        "chain": chain_of(trade),
        "liquidity": bucket(trade.get("liquidity_usd"), [10000, 25000, 50000, math.inf], ["<10k", "10-25k", "25-50k", ">50k"]),
        "gain": bucket(trade.get("signal_gain_pct"), [100, 200, 400, math.inf], ["<100", "100-200", "200-400", ">400"]),
        "ratio": bucket(trade.get("signal_ratio"), [2, 4, math.inf], ["<2", "2-4", ">4"]),
        "hour_utc": ts[11:13] if ts else "missing",
        "source": trade.get("source") or "missing",
        "holder_top1_pct": holder,
        "lp_evidence": lp,
    }


def rug_gap(trade):
    peak, exit_ = trade.get("peak"), trade.get("exit")
    return bool(peak and exit_ is not None and float(exit_) / float(peak) < .4)


def counterfactual_exit(trade, tp_rungs, trail_pct, hard_stop_pct):
    """Estimate exit using only entry/peak/exit and recorded reason.

    A tighter trail replaces a recorded trail fill at peak*(1-trail). A TP
    rung fires if recorded peak reaches its threshold; partial proceeds use
    the threshold price. A hard stop replaces a lower recorded final exit.
    Intratrade ordering, gaps, price impact, quote depth and rung timing are
    unknown. These optimistic fills cannot establish executable performance.
    """
    entry, peak, exit_ = (float(trade[k]) for k in ("entry", "peak", "exit"))
    if entry <= 0:
        raise ValueError("entry price must be positive")
    reason = str(trade.get("reason", "")).lower()
    if "trail" in reason:
        old = re.search(r"([\d.]+)% from peak", reason)
        if old and trail_pct < float(old.group(1)):
            exit_ = peak * (1 - trail_pct / 100)
    stop = entry * (1 - hard_stop_pct / 100)
    if exit_ < stop:
        exit_ = stop
    remain, proceeds = 1.0, 0.0
    for gain, sell_pct in tp_rungs:
        if peak >= entry * (1 + float(gain) / 100):
            portion = min(remain, float(sell_pct) / 100)
            proceeds += portion * entry * (1 + float(gain) / 100)
            remain -= portion
    return proceeds + remain * exit_


def counterfactual_dump_trade(trade, threshold_pct, config=None):
    """Optimistic tighter-dump fill using the recorded lifetime peak.

    Assume price moves monotonically from that peak through peak*(1-threshold)
    and that the remaining bag sells there. Actual gaps, ordering, liquidity
    and the 60-second rolling peak are unknown. This is not a fill forecast.
    """
    reason = str(trade.get("reason") or "").lower()
    if "dump detector" not in reason and "venue dump" not in reason:
        return trade
    if not all(trade.get(k) for k in ("entry", "peak", "exit")):
        return trade
    if not 0 < threshold_pct < 100:
        raise ValueError("dump threshold must be between 0 and 100")
    config = config or {}
    old_exit = counterfactual_exit(trade, [], 100, 100)
    new_exit = max(old_exit, float(trade["peak"]) * (1 - threshold_pct / 100))
    if new_exit == old_exit:
        return trade
    rungs = config.get("exit", {}).get("take_profits", [])
    sold = sum(float(rungs[int(r[0])][1]) / 100 for r in trade.get("rungs", [])
               if int(r[0]) < len(rungs))
    remaining = max(0.0, 1 - sold)
    stake = float(trade.get("buy_sol") or trade.get("buy_bnb") or 0)
    pnl_key = "realized_bnb" if chain_of(trade) == "bsc" else "realized_sol"
    adjusted = dict(trade)
    adjusted[pnl_key] = float(trade.get(pnl_key) or 0) + (
        stake * remaining * (new_exit - old_exit) / float(trade["entry"]))
    adjusted["exit"] = new_exit
    return adjusted


def filter_sweep(trades, field, thresholds, keep, config=None):
    """Evaluate fixed candidates on chronological first 2/3 IS, rest OOS.

    Missing entry evidence passes through, matching the prior round.
    Journal append order is chronological even when launcher TZ changed.
    """
    cut = len(trades) * 2 // 3
    train, holdout = trades[:cut], trades[cut:]
    baseline = (metrics(train, config), metrics(holdout, config))
    results = []
    for threshold in thresholds:
        kept = ([t for t in train if t.get(field) is None or keep(float(t[field]), threshold)],
                [t for t in holdout if t.get(field) is None or keep(float(t[field]), threshold)])
        scored = tuple(metrics(part, config) for part in kept)
        stressed = tuple(metrics(part, config, cost_multiplier=3) for part in kept)
        edge = tuple((scored[i]["usd"] / scored[i]["n"] -
                      baseline[i]["usd"] / baseline[i]["n"])
                     if scored[i]["n"] and baseline[i]["n"] else 0.0
                     for i in (0, 1))
        results.append({"threshold": threshold, "metrics": scored,
                        "stress": stressed, "edge_per_trade": edge,
                        "retention": edge[1] / edge[0] if edge[0] > 0 else None})
    return baseline, results


def metrics(trades, config=None, stress=False, cost_multiplier=1):
    rows = [(*net_pnl(t, config, stress, cost_multiplier), chain_of(t)) for t in trades]
    vals = [x[0] for x in rows]
    pnl_usd = [x[1] for x in rows if x[1] is not None]
    wins = sum(x > 0 for x in vals)
    gains = sum(x for x in pnl_usd if x > 0)
    losses = -sum(x for x in pnl_usd if x < 0)
    cumulative = high = drawdown = 0.0
    for value in (x[1] or 0.0 for x in rows):
        cumulative += value
        high = max(high, cumulative)
        drawdown = max(drawdown, high - cumulative)
    usd_vals = [x[1] for x in rows if x[1] is not None]
    native_by_chain = {chain: sum(x[0] for x in rows if x[2] == chain)
                       for chain in sorted({x[2] for x in rows})}
    return {"n": len(rows), "native_by_chain": native_by_chain, "usd": sum(usd_vals),
            "usd_n": len(usd_vals), "win_rate": wins / len(rows) if rows else 0,
            "pf": gains / losses if losses else (math.inf if gains else 0),
            "max_dd_usd": drawdown}


def attribution(trades, config=None, stress=False):
    output = {}
    for field in ("chain", "liquidity", "gain", "ratio", "hour_utc", "source",
                  "holder_top1_pct", "lp_evidence"):
        groups = defaultdict(list)
        for trade in trades:
            groups[attributes(trade)[field]].append(trade)
        output[field] = {key: metrics(rows, config, stress) for key, rows in groups.items()}
    reasons = defaultdict(list)
    for trade in trades:
        reason = trade.get("reason") or "missing"
        reasons["rug-gap" if rug_gap(trade) else reason.split(":")[0].split(" -")[0]].append(trade)
    output["loss_reason"] = {key: metrics(rows, config, stress) for key, rows in reasons.items()}
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stress", action="store_true", help="triple estimated slippage")
    parser.add_argument("--attribution", action="store_true")
    args = parser.parse_args()
    trades, counts = load_journal()
    config = json.loads(CONFIG.read_text())
    signals = parse_signals()
    print("Journal counts:", counts, "parsed signals:", len(signals))
    print("ts | chain | name | net native | net USD | reason")
    for trade in trades:
        native, usd = net_pnl(trade, config, args.stress)
        print(f"{trade['close_ts']} | {trade['chain']} | {trade['name']} | {native:+.6f} | {usd if usd is not None else 'missing'} | {trade.get('reason')}")
    print("Overall:", metrics(trades, config, args.stress))
    for chain in ("solana", "bsc"):
        print(chain, metrics([t for t in trades if t["chain"] == chain], config, args.stress))
    if args.attribution:
        report = attribution(trades, config, args.stress)
        for field, groups in report.items():
            print(field)
            for key, result in sorted(groups.items(), key=lambda item: item[1]["usd"]):
                print(" ", key, result)
        patterns = [(result["usd"], field, key, result["n"])
                    for field in ("liquidity", "gain", "ratio", "source")
                    for key, result in report[field].items() if key != "missing"]
        print("Top losing patterns (overlapping groups):")
        for usd, field, key, count in sorted(patterns)[:3]:
            print(f"  {field}={key}: n={count}, net USD={usd:+.2f}")


if __name__ == "__main__":
    main()
