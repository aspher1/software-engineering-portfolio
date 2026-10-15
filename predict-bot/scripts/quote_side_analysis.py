"""Read-only, one-contract paper accounting from recorded alert and exit quotes.

No order sizing or depth is assumed. Top-of-book prices are observations,
not guaranteed fills. This module is never imported by the scanner.
"""

import argparse
import json
import statistics
from pathlib import Path

from same_day_loop import _kalshi_taker_fee_dollars


def _price(value):
    if isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (ValueError, TypeError):
        return None
    return price if 0 < price < 1 else None


def paper_fill(grade, exit_event=None):
    """Return paired mid and executable P&L in dollars per one contract.

    Fees apply at each taker trade, including both legs of an early exit.
    Settlement pays 0 or 1 and has no taker exit fee. Missing entry quotes
    never fall back to the mid. An exit event lacking its bid is unavailable.
    """
    side = grade.get("side")
    if side not in ("YES", "NO") or grade.get("actual") not in ("YES", "NO"):
        return None
    mid = _price(grade.get("market_mid"))
    bid, ask = _price(grade.get("bid")), _price(grade.get("ask"))
    quotes_ok = bid is not None and ask is not None and bid < ask
    entry_executable = (ask if side == "YES" else 1 - bid) if quotes_ok else None
    entry_mid = (mid if side == "YES" else 1 - mid) if mid is not None else None
    if exit_event is None:
        exit_mid = exit_executable = float(grade["actual"] == side)
        exit_fee = False
    else:
        exit_mid = _price(exit_event.get("exit_price_taken"))
        # Use the recorded taken-side bid, checked against the source YES
        # quote if available. Never use the taken-side ask for a sale.
        exit_executable = _price(exit_event.get("exit_bid_taken"))
        yes_bid = _price(exit_event.get("exit_yes_bid"))
        yes_ask = _price(exit_event.get("exit_yes_ask"))
        source = yes_bid if side == "YES" else 1 - yes_ask if yes_ask is not None else None
        if source is not None:
            exit_executable = round(source, 6)
        exit_fee = True
    def account(entry, exit_price):
        if entry is None or exit_price is None:
            return None, None
        gross = exit_price - entry
        net = gross - _kalshi_taker_fee_dollars(1, entry)
        if exit_fee:
            net -= _kalshi_taker_fee_dollars(1, exit_price)
        return round(gross, 6), round(net, 6)
    mid_gross, mid_net = account(entry_mid, exit_mid)
    executable_gross, executable_net = account(entry_executable, exit_executable)
    return {
        "key": grade.get("key"), "side": side,
        "exit_type": "early_exit" if exit_fee else "settlement",
        "entry_mid_taken": entry_mid, "entry_executable_taken": entry_executable,
        "exit_mid_taken": exit_mid, "exit_executable_taken": exit_executable,
        "mid_pnl_gross": mid_gross, "mid_pnl_net": mid_net,
        "executable_pnl_gross": executable_gross,
        "executable_pnl_net": executable_net,
        "mid_vs_executable_gap_net": (round(mid_net - executable_net, 6)
                                       if mid_net is not None and executable_net is not None
                                       else None),
    }


def _quantile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (position - lower)


def summarize(rows):
    gaps = [r["mid_vs_executable_gap_net"] for r in rows
            if r["mid_vs_executable_gap_net"] is not None]
    return {
        "kalshi_rows": len(rows), "paired_rows": len(gaps),
        "unavailable_rows": len(rows) - len(gaps),
        "gap_mean_dollars_per_contract": round(statistics.mean(gaps), 6) if gaps else None,
        "gap_median_dollars_per_contract": round(statistics.median(gaps), 6) if gaps else None,
        "gap_p10": round(_quantile(gaps, .1), 6) if gaps else None,
        "gap_p25": round(_quantile(gaps, .25), 6) if gaps else None,
        "gap_p75": round(_quantile(gaps, .75), 6) if gaps else None,
        "gap_p90": round(_quantile(gaps, .9), 6) if gaps else None,
        "mid_pnl_net_mean": round(statistics.mean(r["mid_pnl_net"] for r in rows if r["mid_vs_executable_gap_net"] is not None), 6) if gaps else None,
        "executable_pnl_net_mean": round(statistics.mean(r["executable_pnl_net"] for r in rows if r["mid_vs_executable_gap_net"] is not None), 6) if gaps else None,
    }


def load_jsonl(path):
    with open(path) as source:
        return [json.loads(line) for line in source if line.strip()]


def run(grades_path, exits_path, output_path=None):
    grades = [g for g in load_jsonl(grades_path) if g.get("proxy") == "kalshi"]
    exits = {e["signal_key"]: e for e in load_jsonl(exits_path)}
    rows = [paper_fill(g, exits.get(g.get("key"))) for g in grades]
    rows = [r for r in rows if r is not None]
    if output_path:
        with open(output_path, "w") as out:
            for row in rows:
                out.write(json.dumps(row) + "\n")
    return summarize(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grades", default="data/signal_grades.jsonl")
    parser.add_argument("--exits", default="data/exit_events.jsonl")
    parser.add_argument("--output", default="data/quote_side_paper.jsonl")
    args = parser.parse_args()
    print(json.dumps(run(args.grades, args.exits, args.output), indent=2))
