#!/usr/bin/env python3
"""Iteration 4 paper-only Track B retrospective screens. Read-only journal inputs.

Candidate definitions were recorded in iter-4/codex_findings.md before outcomes.
This is a screen-quality test, not a capital-redeployment replay.
"""
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
from iter1_trackb_screens import load_pairs, stats, costed_usd, infer_chain

ROOT = Path(__file__).resolve().parents[1]
CUTOFF = datetime(2026, 9, 28, 16, 24, tzinfo=timezone.utc).timestamp()


def epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


def build_sticky(pairs):
    """Mirror iter3 F2b; veto events must precede the tested entry."""
    journal = [json.loads(line) for line in open(ROOT / 'runs/paper-1h/candidates.jsonl') if line.strip()]
    observations = defaultdict(list)
    vetoes = defaultdict(list)
    for row in journal:
        key = (row.get('chain'), row.get('mint'))
        if row.get('event') == 'candidate' and row.get('ts_epoch'):
            observations[key].append(row)
        if row.get('event') == 'slip_veto' and number(row.get('ts_epoch')) is not None:
            t = float(row['ts_epoch'])
            if t >= CUTOFF:
                vetoes[key].append(t)

    def entry_epoch(e, c):
        wall = epoch(e['ts'])
        rows = observations[(infer_chain(e, c), e.get('mint'))]
        nearby = [(abs(wall - epoch(r['ts'])), r) for r in rows]
        if nearby:
            delta, row = min(nearby, key=lambda x: x[0])
            if delta <= 30 * 60:
                return wall + (float(row['ts_epoch']) - epoch(row['ts']))
        return wall

    for e, c in pairs:
        slip = number(e.get('slip_from_signal_pct'))
        if slip is not None and slip > 0:
            t = entry_epoch(e, c)
            if t < CUTOFF:
                vetoes[(infer_chain(e, c), e.get('mint'))].append(t)
    for times in vetoes.values():
        times.sort()

    def keep(e, c):
        slip = number(e.get('slip_from_signal_pct'))
        if slip is not None and slip > 0:
            return False
        t = entry_epoch(e, c)
        day = datetime.fromtimestamp(t, timezone.utc).date()
        return not any(v <= t and datetime.fromtimestamp(v, timezone.utc).date() == day
                       for v in vetoes[(infer_chain(e, c), e.get('mint'))])
    return keep


def metric(e, name):
    if name == 'ratio':
        buys, sells = number(e.get('m15_buys')), number(e.get('m15_sells'))
        return buys / max(sells, 1) if buys is not None and sells is not None else number(e.get('signal_ratio'))
    if name == 'age_hours':
        born = number(e.get('pool_created_at'))
        return (epoch(e['ts']) - born) / 3600 if born is not None else None
    return number(e.get({'liquidity': 'liquidity_usd', 'mcap': 'mcap_usd'}[name]))


def c9_search(is_pairs):
    tested, qualifiers = [], []
    for field in ('liquidity', 'ratio', 'mcap', 'age_hours'):
        values = [metric(e, field) for e, _ in is_pairs]
        values = [v for v in values if v is not None]
        if not values:
            continue
        cut = median(values)
        for side in ('low', 'high'):
            keep = lambda e, c, field=field, cut=cut, side=side: (
                metric(e, field) is not None and
                (metric(e, field) <= cut if side == 'low' else metric(e, field) > cut))
            s = stats([p for p in is_pairs if keep(*p)])
            label = f'{field} {"<=" if side == "low" else ">"} {cut:g}'
            tested.append((label, s))
            if s['n'] >= 30 and isinstance(s.get('pf'), (int, float)) and s['pf'] >= 1:
                qualifiers.append((s['pf'], -len(tested), label, keep))
    for window in sorted({e.get('window') for e, _ in is_pairs if e.get('window')}):
        keep = lambda e, c, window=window: e.get('window') == window
        s = stats([p for p in is_pairs if keep(*p)])
        label = f'window == {window}'
        tested.append((label, s))
        if s['n'] >= 30 and isinstance(s.get('pf'), (int, float)) and s['pf'] >= 1:
            qualifiers.append((s['pf'], -len(tested), label, keep))
    chosen = max(qualifiers) if qualifiers else None
    return tested, (chosen[2], chosen[3]) if chosen else None


def evaluate(name, pairs, split, keep):
    is_pairs, oos_pairs = pairs[:split], pairs[split:]
    a, b = [p for p in is_pairs if keep(*p)], [p for p in oos_pairs if keep(*p)]
    si, so, stress, base = stats(a), stats(b), stats(b, slippage=.0045), stats(oos_pairs)
    pf_i, pf_o, pf_b = si.get('pf'), so.get('pf'), base.get('pf')
    retention = pf_o / pf_i if isinstance(pf_i, (int, float)) and pf_i > 0 and isinstance(pf_o, (int, float)) else None
    gain = pf_o - pf_b if isinstance(pf_o, (int, float)) and isinstance(pf_b, (int, float)) else None
    # iter1 stats renders a zero-loss PF as the string 'inf'. It is valid
    # for arithmetic here, but remains a small-sample warning in the report.
    if pf_o == 'inf' and isinstance(pf_i, (int, float)) and pf_i > 0:
        retention = math.inf
    if pf_o == 'inf' and isinstance(pf_b, (int, float)):
        gain = math.inf
    flags = []
    if si['n'] < 100: flags.append('IS<100')
    if so['n'] < 30: flags.append('OOS<30')
    if retention is None or retention < .6: flags.append('retention<60%/undefined')
    if retention is not None and 1 - retention > .7: flags.append('OOS decay>70%')
    if so.get('wr', 0) > .9: flags.append('WR>90%')
    if stress.get('pf') != 'inf' and (not isinstance(stress.get('pf'), (int, float)) or stress['pf'] < 1):
        flags.append('3x-fee PF<1/undefined')
    if gain is None or (gain < .25 and (so['n'] < 30 or so.get('exp', 0) <= 0)):
        flags.append('no OOS significance')
    for field, get in [('chain', lambda p: infer_chain(*p)), ('window', lambda p: p[0].get('window'))]:
        baseline_mix = {get(p) for p in oos_pairs if get(p) is not None}
        kept_mix = {get(p) for p in b if get(p) is not None}
        if len(baseline_mix) > 1 and len(kept_mix) < 2:
            flags.append(f'degenerate {field} mix')
    return dict(name=name, split=split, is_stats=si, oos_stats=so, stress=stress,
                baseline=base, retention=retention, gain=gain, flags=flags,
                verdict='FAIL' if flags else 'PASS', is_mix=dict(Counter(infer_chain(*p) for p in a)),
                oos_chain=dict(Counter(infer_chain(*p) for p in b)),
                oos_window=dict(Counter(p[0].get('window') for p in b)))


def main():
    pairs = load_pairs()
    split = 2 * len(pairs) // 3
    sticky = build_sticky(pairs)
    cap = lambda limit: lambda e, c: (number(e.get('signal_gain_pct')) is not None
                                       and number(e['signal_gain_pct']) <= limit)
    tested, c9 = c9_search(pairs[:split])
    candidates = [('C8a gain<=100', cap(100)), ('C8b gain<=60', cap(60)),
                  ('C8c gain<=100 + F2b', lambda e, c: cap(100)(e, c) and sticky(e, c)),
                  ('C8d gain<=60 + F2b', lambda e, c: cap(60)(e, c) and sticky(e, c))]
    if c9:
        candidates.append(('C9 ' + c9[0], c9[1]))
    results = [evaluate('BASELINE', pairs, split, lambda e, c: True)]
    results += [evaluate(name, pairs, split, keep) for name, keep in candidates]
    extra = {}
    for name, keep in candidates:
        extra[name] = {
            'splits': [evaluate(name, pairs, int(len(pairs) * frac), keep)
                       for frac in (.60, .70)],
            'fees': {str(mult): stats([p for p in pairs[split:] if keep(*p)], slippage=.0015 * mult)
                     for mult in (1, 2, 3)},
            'slices': {field + ':' + str(value): stats([p for p in pairs[split:] if keep(*p) and getter(p) == value])
                       for field, getter in [('chain', lambda p: infer_chain(*p)),
                                             ('window', lambda p: p[0].get('window'))]
                       for value in sorted({getter(p) for p in pairs[split:] if getter(p) is not None})},
        }
    vals = [costed_usd(*p) for p in pairs]
    vals = [v for v in vals if v is not None]
    wipes = [v for v in vals if v <= -3.5]
    output = {'pairs': len(pairs), 'split': split, 'valued': len(vals),
              'wipes': {'n': len(wipes), 'net': round(sum(wipes), 2),
                        'total_net': round(sum(vals), 2)},
              'c9_is_buckets': [{'bucket': label, **s} for label, s in tested],
              'c9_selection': c9[0] if c9 else None,
              'main': results, 'falsification': extra}
    if '--json' in sys.argv:
        print(json.dumps(output, indent=2))
    else:
        print(f'FIFO pairs={len(pairs)} valued={len(vals)} split={split}/{len(pairs)-split} '
              f'wipes={len(wipes)} net=${sum(wipes):+.2f} total=${sum(vals):+.2f}')
        print('C9 IS selection:', c9[0] if c9 else 'none qualified')
        print('name | IS n/net/PF | OOS n/net/PF/WR/exp | PF gain | retention | 3x PF | verdict/flags')
        for r in results:
            a, b = r['is_stats'], r['oos_stats']
            print(f"{r['name']} | {a['n']}/{a.get('net')}/{a.get('pf')} | "
                  f"{b['n']}/{b.get('net')}/{b.get('pf')}/{b.get('wr')}/{b.get('exp')} | "
                  f"{r['gain']} | {r['retention']} | {r['stress'].get('pf')} | "
                  f"{r['verdict']} {','.join(r['flags'])}")
        for name, detail in extra.items():
            print(name, 'splits', [(r['split'], r['verdict'], r['oos_stats'].get('pf'))
                                  for r in detail['splits']], 'fees',
                  [(k, v.get('pf')) for k, v in detail['fees'].items()])
    return output


if __name__ == '__main__':
    main()
