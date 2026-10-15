#!/usr/bin/env python3
"""Offline Track B replay of the frozen 100% scanner-gain entry veto.

Experimental and unshipped. Journal rows are read only; skipped trades have
zero counterfactual P&L, and retained trades keep their recorded exits.
"""

from collections import defaultdict, deque
from dataclasses import dataclass
import json
from pathlib import Path

JOURNAL = Path(__file__).resolve().parents[1] / "runs/paper-1h/trades.jsonl"
IS_SIZE = 106


@dataclass(frozen=True)
class Pair:
    entry: dict
    close: dict
    chain: str
    entry_index: int
    close_index: int


def chain_of(row):
    return row.get("chain") or ("solana" if len(str(row.get("mint", ""))) > 40 else "bsc")


def pair_rows(rows):
    """FIFO by (chain, mint), preserving close append order."""
    pending = defaultdict(deque)
    pairs = []
    unmatched_closes = 0
    for index, row in enumerate(rows):
        key = (chain_of(row), row.get("mint"))
        if row.get("type") == "entry":
            pending[key].append((index, row))
        elif row.get("type") == "close":
            if not pending[key]:
                unmatched_closes += 1
                continue
            entry_index, entry = pending[key].popleft()
            pairs.append(Pair(entry, row, key[0], entry_index, index))
    return pairs, unmatched_closes, sum(map(len, pending.values()))


def split_pairs(pairs, is_size=IS_SIZE):
    """Use close append order, purging entries that straddle the boundary."""
    if len(pairs) < is_size:
        raise ValueError(f"need at least {is_size} paired closes")
    boundary = pairs[is_size - 1].close_index
    train = pairs[:is_size]
    holdout = [p for p in pairs[is_size:] if p.entry_index > boundary]
    return train, holdout, len(pairs) - is_size - len(holdout)


def stake(pair):
    return float(pair.entry.get("buy_bnb") or pair.entry.get("buy_sol") or 0)


def native_realized(pair):
    field = "realized_bnb" if pair.chain == "bsc" else "realized_sol"
    return float(pair.close.get(field) or 0)


def net_usd(pair, stress=False):
    s = stake(pair)
    entry_price = float(pair.close.get("entry") or pair.entry.get("entry") or 0)
    exit_price = float(pair.close.get("exit") or 0)
    q = max(0.0, exit_price / entry_price) if entry_price else 0.0
    rate = (pair.close.get("bnb_usd") or pair.close.get("sol_usd") or
            pair.entry.get("bnb_usd") or pair.entry.get("sol_usd") or
            (1200.0 if pair.chain == "bsc" else 115.0))
    fixed = 0.00004 if pair.chain == "bsc" else 0.004
    slip = 0.0045 if stress else 0.0015
    return float(rate) * (native_realized(pair) - s * (1 + q) * (0.0025 + slip) - fixed)


def veto(pair):
    """Frozen predicate: `(signal_gain_pct or 0) >= 100.0`."""
    return float(pair.entry.get("signal_gain_pct") or 0) >= 100.0


def wipe(pair):
    reason = str(pair.close.get("reason") or "").lower()
    return (stake(pair) > 0 and native_realized(pair) <= -0.85 * stake(pair)
            and ("dump detector" in reason or "venue dump" in reason))


def max_drawdown(values):
    equity = peak = worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def cohort(pairs, stress=False):
    kept = [p for p in pairs if not veto(p)]
    vetoed = [p for p in pairs if veto(p)]
    values = [net_usd(p, stress) for p in kept]
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    full_net = sum(net_usd(p, stress) for p in pairs)
    kept_net = sum(values)
    return {
        "n": len(pairs), "kept": len(kept), "retention": len(kept) / len(pairs),
        "full_net": full_net, "kept_net": kept_net,
        "edge": (kept_net - full_net) / len(pairs),
        "wr": len(wins) / len(kept) if kept else 0.0,
        "pf": sum(wins) / -sum(losses) if losses else float("inf"),
        "max_dd": max_drawdown(values),
        "wipes_vetoed": sum(wipe(p) for p in vetoed),
        "wipes_kept": sum(wipe(p) for p in kept),
        "winners_vetoed": sum(net_usd(p, stress) > 0 for p in vetoed),
    }


def load_rows(path=JOURNAL):
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def main():
    pairs, unmatched_closes, unmatched_entries = pair_rows(load_rows())
    train, holdout, purged = split_pairs(pairs)
    results = {(name, stress): cohort(part, stress) for name, part in
               (("IS", train), ("OOS", holdout)) for stress in (False, True)}
    print(f"paired={len(pairs)} unmatched_closes={unmatched_closes} "
          f"unmatched_entries={unmatched_entries} purged={purged}")
    print("cohort cost    n kept retain   full_net  kept_net edge/trade  WR     PF   maxDD wipes(v/k) winners_vetoed")
    for name in ("IS", "OOS"):
        for stress in (False, True):
            r = results[name, stress]
            print(f"{name:3} {'stress' if stress else 'base':6} {r['n']:3} {r['kept']:4} "
                  f"{r['retention']*100:6.1f}% {r['full_net']:9.2f} {r['kept_net']:9.2f} "
                  f"{r['edge']:10.4f} {r['wr']*100:5.1f}% {r['pf']:6.3f} "
                  f"{r['max_dd']:7.2f} {r['wipes_vetoed']:2}/{r['wipes_kept']:<2} "
                  f"{r['winners_vetoed']:3}")
    is_base, is_stress = results['IS', False], results['IS', True]
    oos_base, oos_stress = results['OOS', False], results['OOS', True]
    retention_base = oos_base['edge'] / is_base['edge'] if is_base['edge'] > 0 else float('-inf')
    retention_stress = oos_stress['edge'] / is_stress['edge'] if is_stress['edge'] > 0 else float('-inf')
    kept_oos = [p for p in holdout if not veto(p)]
    chain_split = {chain: (len(part), sum(net_usd(p) for p in part)) for chain in
                   sorted({p.chain for p in kept_oos})
                   for part in ([p for p in kept_oos if p.chain == chain],)}
    # Consecutive OOS blocks expose whether savings rely on a single wipe cluster.
    blocks = [(i + 1, min(i + 20, len(holdout)),
               sum(-net_usd(p) for p in holdout[i:i+20] if veto(p)),
               sum(wipe(p) for p in holdout[i:i+20] if veto(p)))
              for i in range(0, len(holdout), 20)]
    savings = oos_base['kept_net'] - oos_base['full_net']
    one_block_share = max((value for _, _, value, _ in blocks), default=0) / savings if savings > 0 else 0
    print(f"kept OOS chain split (n, base net): {chain_split}")
    print(f"OOS 20-close blocks (start, end, savings, vetoed wipes): {blocks}")
    print(f"largest-block savings share: {one_block_share*100:.1f}%")
    gates = [
        ("OOS base edge >= 60% IS", retention_base >= .60, f"{retention_base*100:.1f}%"),
        ("OOS stress edge >= 60% IS", retention_stress >= .60, f"{retention_stress*100:.1f}%"),
        ("IS original >= 100", len(train) >= 100, str(len(train))),
        ("OOS original >= 30", len(holdout) >= 30, str(len(holdout))),
        ("OOS kept base net > 0", oos_base['kept_net'] > 0, f"${oos_base['kept_net']:.2f}"),
        ("OOS kept stress net > 0", oos_stress['kept_net'] > 0, f"${oos_stress['kept_net']:.2f}"),
        ("OOS kept >= 30", len(kept_oos) >= 30, str(len(kept_oos))),
        ("OOS kept WR <= 90%", oos_base['wr'] <= .90, f"{oos_base['wr']*100:.1f}%"),
        ("OOS edge decay <= 70%", retention_base >= .30 and retention_stress >= .30,
         f"base {100*(1-retention_base):.1f}%, stress {100*(1-retention_stress):.1f}%"),
        ("No single OOS 20-close block >70% savings", one_block_share <= .70,
         f"{one_block_share*100:.1f}%"),
        ("Kept OOS spans both chains", len(chain_split) >= 2, str(chain_split)),
    ]
    print("gate | result | observed")
    for label, passed, observed in gates:
        print(f"{label} | {'PASS' if passed else 'FAIL'} | {observed}")
    verdict = "INCONCLUSIVE" if len(kept_oos) < 30 else "PASS" if all(g[1] for g in gates) else "REJECT"
    print(f"VERDICT: {verdict} (experimental, unshipped)")


if __name__ == "__main__":
    main()
