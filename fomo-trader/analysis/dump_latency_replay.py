"""Read-only shadow-dump replay. Run from repo root; writes nothing."""
import json
import sys
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shadow_dump import evaluate
JOURNAL = ROOT / "runs/paper-1h/trades.jsonl"
PATHS = ROOT / "runs/paper-1h/quote_paths"


def rows(path):
    with path.open() as stream:
        for line in stream:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def is_rug(row):
    reason = row.get("reason", "").lower()
    return "dump detector" in reason or "venue dump" in reason


def usd_pnl(row):
    value = row.get("realized_usd")
    if value is not None:
        return float(value)
    native = row.get("realized_sol")
    fx = row.get("sol_usd")
    return float(native) * float(fx) if native is not None and fx else None


def quoted_path(row, prior_entry):
    path = PATHS / (row["mint"] + ".jsonl")
    if not path.exists():
        return []
    low = stamp(prior_entry["ts"]) if prior_entry else float("-inf")
    high = stamp(row["ts"])
    return [(stamp(r["ts"]), r["price_usd"])
            for r in rows(path) if r.get("price_usd") is not None
            and low <= stamp(r["ts"]) <= high]


def first_shadow(path):
    history = deque(maxlen=300)
    for ts, price in path:
        history.append((ts, price))
        if evaluate(history, ts, price):
            return ts, price
    return None


def main():
    latest_entry = {}
    records = []
    for row in rows(JOURNAL):
        key = (row.get("chain", "solana"), row.get("mint"))
        if row.get("type") == "entry":
            latest_entry[key] = row
        elif row.get("type") == "close":
            records.append((row, latest_entry.get(key)))
            latest_entry.pop(key, None)
    groups = defaultdict(list)
    fp = []
    savings = 0.0
    observed_savings = 0.0
    modeled_savings = 0.0
    nonrug_count = 0
    nonrug_path_count = 0
    for row, entry in records:
        peak, exit_px = row.get("peak") or 0, row.get("exit") or 0
        pnl = usd_pnl(row)
        path = quoted_path(row, entry)
        signal = first_shadow(path)
        if is_rug(row):
            # Near-zero final marks are treated as one-tick gaps when no
            # earlier path proves a recoverable crossing. A close record
            # alone cannot prove the number of polls in the descent.
            gap = bool(peak and exit_px / peak <= 0.10)
            crossing_before_gap = (signal and path and
                                   signal[1] > path[-1][1] * 2)
            classification = ("instant" if gap and not crossing_before_gap
                              else "gradual")
            groups[classification].append((row, pnl, signal, bool(path)))
            if classification == "gradual" and pnl is not None:
                # Existing path: scale the first shadow USD mark to the
                # journal's native exit mark using the final captured mark.
                # Otherwise, assume a linear 30s peak-to-exit descent and
                # a 2s tick after the 8% crossing.
                modeled = peak * (1 - .08 -
                                  2 / 30 * (1 - exit_px / peak)) if peak else exit_px
                candidate = (signal[1] / path[-1][1] * exit_px
                             if signal and path and path[-1][1] else modeled)
                candidate = min(max(candidate, exit_px), peak)
                stake = (row.get("buy_sol") or 0) * (row.get("sol_usd") or 0)
                remaining = 1 - sum(.5 for _ in row.get("rungs", []))
                amount = max(0, stake * remaining *
                             (candidate - exit_px) / row["entry"])
                savings += amount
                if signal:
                    observed_savings += amount
                else:
                    modeled_savings += amount
        elif ("take profit" in row.get("reason", "").lower()
              or "trailing stop" in row.get("reason", "").lower()):
            nonrug_count += 1
            nonrug_path_count += bool(path)
            if signal and row.get("entry") and path and pnl is not None:
                candidate = signal[1] / path[-1][1] * row["exit"]
                stake = (row.get("buy_sol") or 0) * (row.get("sol_usd") or 0)
                estimated = stake * (candidate / row["entry"] - 1)
                fp.append((row, pnl - estimated))
    for kind in ("instant", "gradual"):
        items = groups[kind]
        losses = sum(min(0, p) for _, p, _, _ in items if p is not None)
        print(f"{kind}: {len(items)} exits, loss ${-losses:.2f}")
        for row, pnl, signal, path in items:
            print(f"  {row['ts']} {row['name']}: peak={row.get('peak')} "
                  f"exit={row.get('exit')} P&L={pnl} "
                  f"path={'yes' if path else 'no'} signal={signal}")
    cost = sum(max(0, loss) for _, loss in fp)
    print(f"gradual gross estimated savings: ${savings:.2f}")
    print(f"  observed quote paths: ${observed_savings:.2f}; "
          f"modeled missing paths: ${modeled_savings:.2f}")
    print(f"TP/trailing exits: {nonrug_count}; with captured paths: "
          f"{nonrug_path_count}")
    print(f"observed-path TP/trailing early cuts: {len(fp)}, "
          f"positive false-positive cost: ${cost:.2f}")
    print(f"illustrative net before unobserved false positives: "
          f"${savings-cost:.2f}")
    print("Limit: quote capture is incomplete; venue m5 history is absent. "
          "Linear estimates ignore execution impact, rung timing and "
          "intra-window reversals. They are illustrative bounds, not fills.")


if __name__ == "__main__":
    main()
