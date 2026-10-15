"""Frozen, offline round-5 entry-screen validation. No bot import or I/O writes."""
import argparse
from collections import defaultdict, deque
import hashlib
import json
import math
from pathlib import Path

from analysis import replay

ROOT = Path(__file__).resolve().parents[2]
JOURNAL = ROOT / 'runs/paper-1h/trades.jsonl'
CONFIG = ROOT / 'runs/paper-1h/config.json'
PREREGISTRATION = Path(__file__).with_name('ROUND5_PREREGISTRATION.md')
PREREGISTRATION_SHA256 = '5066abb3a9ca958e0da6732d51f721986636391dde56d99b73c59e6752dd66b8'


def verify_preregistration():
    if hashlib.sha256(PREREGISTRATION.read_bytes()).hexdigest() != PREREGISTRATION_SHA256:
        raise RuntimeError('frozen Round 5 pre-registration changed')

PROVISIONAL_N = 30
MIN_IS = 100
MIN_OOS = 30
RETENTION = .60
KILL_VARIANT = 'KILL_VARIANT'
STOP_ALL = 'STOP_ALL'
# Fixed before evaluation; no data-selected thresholds.
CANDIDATES = {
    'holder_concentration': ('holder_top1_pct', 40.0),
    'chase': ('slip_from_signal_pct', .10),
    'commit_liquidity': ('liquidity_usd', 40000.0),
    'entry_latency': ('entry_latency_ms', 60000.0),
}


def load_pairs(path=JOURNAL):
    """Pair in append order FIFO by chain and mint; keep entry fields separate."""
    pending = defaultdict(deque)
    pairs, counts = [], defaultdict(int)
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError('non-object row')
            except (json.JSONDecodeError, ValueError):
                counts['malformed'] += 1
                continue
            kind, mint = row.get('type'), row.get('mint')
            if kind not in ('entry', 'close') or not mint:
                continue
            key = (replay.chain_of(row), mint)
            counts[kind] += 1
            if kind == 'entry':
                pending[key].append(row)
            elif pending[key]:
                entry = pending[key].popleft()
                if 'slip_from_signal_pct' in entry:
                    merged = dict(entry)
                    merged.update(row)
                    merged['entry_record'] = entry
                    merged['close_record'] = row
                    merged['chain'] = key[0]
                    pairs.append(merged)
            else:
                counts['unmatched_closes'] += 1
    counts['unmatched_entries'] = sum(map(len, pending.values()))
    return pairs, dict(counts)


def split_chronological(pairs):
    # Journal entry timestamps can mix local and UTC; append order is authoritative.
    cut = len(pairs) * 2 // 3
    return pairs[:cut], pairs[cut:]


def summarize(rows, config=None, cost_multiplier=1):
    return replay.metrics(rows, config, cost_multiplier=cost_multiplier)


def oos_retention_ok(is_edge, oos_edge):
    return is_edge > 0 and oos_edge >= RETENTION * is_edge


def three_x_cost_stress(rows, config=None):
    return summarize(rows, config, cost_multiplier=3)['usd'] > 0


def significance_ok(is_n, oos_n):
    total = is_n + oos_n
    return ('verdict_capable' if is_n >= MIN_IS and oos_n >= MIN_OOS else
            'provisional' if total >= PROVISIONAL_N else 'awaiting_data')


def veto(name, entry):
    field, threshold = CANDIDATES[name]
    if name == 'holder_concentration':
        top1, top5 = entry.get('holder_top1_pct'), entry.get('holder_top5_pct')
        try:
            return (top1 is not None and top5 is not None and
                    (float(top1) > 40 or float(top5) > 75))
        except (TypeError, ValueError):
            return False
    value = entry.get(field)
    if value is None or isinstance(value, bool):
        return False  # missing evidence cannot validate a screen
    try:
        value = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(value):
        return False
    return value > threshold if name != 'commit_liquidity' else value < threshold


def kill_decision(oos_net, oos_n, cumulative_oos_net, stop_budget=-21.0):
    """Evaluate only at verdict-capable OOS size; caller freezes candidate on IS."""
    if oos_n < MIN_OOS:
        return 'INSUFFICIENT_OOS_HISTORY'
    if cumulative_oos_net <= stop_budget:
        return STOP_ALL
    if oos_net <= 0:
        return KILL_VARIANT
    return 'CONTINUE'


def evaluate(pairs, config=None):
    verify_preregistration()
    train, holdout = split_chronological(pairs)
    status = significance_ok(len(train), len(holdout))
    result = {'status': status, 'ship_recommend': False,
              'baseline': {'is': summarize(train, config), 'oos': summarize(holdout, config)},
              'candidates': {}}
    # Below 30 closes: descriptive-only. Never evaluate candidate thresholds.
    if status == 'awaiting_data':
        return result
    for name in CANDIDATES:
        if name == 'holder_concentration' and not all(
                t['entry_record'].get('holder_top1_pct') is not None and
                t['entry_record'].get('holder_top5_pct') is not None for t in pairs):
            result['candidates'][name] = {'status': 'not_runnable'}
            continue
        is_kept = [t for t in train if not veto(name, t['entry_record'])]
        oos_kept = [t for t in holdout if not veto(name, t['entry_record'])]
        base_is, base_oos = result['baseline']['is'], result['baseline']['oos']
        is_stats, oos_stats = summarize(is_kept, config), summarize(oos_kept, config)
        is_edge = is_stats['usd'] / len(train) - base_is['usd'] / len(train) if train else 0
        oos_edge = oos_stats['usd'] / len(holdout) - base_oos['usd'] / len(holdout) if holdout else 0
        result['candidates'][name] = {
            'status': status, 'is': is_stats, 'oos': oos_stats,
            'is_edge_per_original_trade_usd': is_edge,
            'oos_edge_per_original_trade_usd': oos_edge,
            'retention_ok': oos_retention_ok(is_edge, oos_edge),
            'stress_ok': three_x_cost_stress(oos_kept, config),
            'kill': kill_decision(oos_stats['usd'], len(holdout), oos_stats['usd']),
        }
    # No automatic shipping: single-regime, plateau, and multiple-comparison
    # gates require a documented manual review at verdict-capable size.
    return result


def bucket(value, bounds, labels):
    try:
        x = float(value)
        if not math.isfinite(x):
            return 'missing'
    except (TypeError, ValueError):
        return 'missing'
    return next((label for bound, label in zip(bounds, labels) if x < bound), labels[-1])


def attributes(trade):
    e = trade['entry_record']
    buys, sells = e.get('m15_buys'), e.get('m15_sells')
    try:
        ratio = float(buys) / float(sells) if float(sells) > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        ratio = None
    return {
        'chain': trade['chain'],
        'commit_liquidity': bucket(e.get('liquidity_usd'), [20000, 40000, 80000, math.inf], ['<20k', '20-40k', '40-80k', '>=80k']),
        'signal_gain': bucket(e.get('signal_gain_pct'), [50, 100, 200, math.inf], ['<50%', '50-100%', '100-200%', '>=200%']),
        'slip_from_signal': bucket(e.get('slip_from_signal_pct'), [-.1, 0, .1, math.inf], ['<-10%', '-10-0%', '0-10%', '>=10%']),
        'buys_sells_ratio': bucket(ratio, [1, 2, 4, math.inf], ['<1', '1-2', '2-4', '>=4']),
        'mcap': bucket(e.get('mcap_usd'), [100000, 500000, 1000000, math.inf], ['<100k', '100-500k', '500k-1m', '>=1m']),
        'hour_of_day': (str(e.get('ts') or '')[11:13] or 'missing'),
    }


def attribution(pairs, config=None):
    grouped = defaultdict(lambda: defaultdict(list))
    for trade in pairs:
        for field, label in attributes(trade).items():
            grouped[field][label].append(trade)
    return {field: {label: summarize(rows, config) for label, rows in sorted(groups.items())}
            for field, groups in grouped.items()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--journal', type=Path, default=JOURNAL)
    args = parser.parse_args()
    pairs, counts = load_pairs(args.journal)
    config = json.loads(CONFIG.read_text())
    print(json.dumps({'counts': counts, 'evaluation': evaluate(pairs, config),
                      'attribution': attribution(pairs, config)}, indent=2,
                     default=str))


if __name__ == '__main__':
    main()
