#!/usr/bin/env python3
"""Iteration 2 retrospective screens. Reads bot journals; writes nothing to them."""
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from iter1_trackb_screens import load_pairs, stats, costed_usd, infer_chain

ROOT = Path(__file__).resolve().parents[1]
CUTOFF = datetime(2026, 9, 28, 16, 24, tzinfo=timezone.utc).timestamp()
SIGHTING = datetime(2026, 9, 28, 16, 25, tzinfo=timezone.utc).timestamp()


def epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(
        tzinfo=timezone.utc).timestamp()


def pct(value):
    return 'NA' if value is None else f'{value:.1%}'


def fmt(s):
    if s['n'] == 0:
        return 'n=0 net=NA PF=NA WR=NA exp=NA DD=NA'
    return (f"n={s['n']} net=${s.get('net', 0):+.2f} PF={s.get('pf', 'NA')} "
            f"WR={pct(s.get('wr'))} exp=${s.get('exp', 0):+.4f} "
            f"DD=${s.get('max_dd', 0):.2f}")


def summarize(name, is_pairs, oos_pairs, keep):
    a = [p for p in is_pairs if keep(*p)]
    b = [p for p in oos_pairs if keep(*p)]
    si, so, stress = stats(a), stats(b), stats(b, slippage=.0045)
    baseline = stats(oos_pairs)
    retention = (so['pf'] / si['pf'] if si['n'] and so['n'] and
                 si.get('pf') not in (0, 'inf', None) and so.get('pf') != 'inf'
                 else None)
    gain = so['pf'] - baseline['pf'] if so['n'] and so.get('pf') != 'inf' else None
    flags = []
    if len(a) < 100: flags.append('IS<100')
    if len(b) < 30: flags.append('OOS<30')
    if retention is not None and retention < .6: flags.append('retention<60%')
    if retention is not None and 1-retention > .7: flags.append('OOS decay>70%')
    if so.get('wr', 0) > .9: flags.append('WR>90%')
    if not stress['n']: flags.append('3x-fee unavailable')
    elif stress.get('pf') != 'inf' and stress.get('pf', 0) < 1: flags.append('3x-fee PF<1')
    if gain is None or (gain < .25 and so.get('exp', 0) <= 0): flags.append('no OOS significance')
    print(f'=== {name} ===')
    print(f'IS kept {len(a)}/{len(is_pairs)}: {fmt(si)}')
    print(f'OOS kept {len(b)}/{len(oos_pairs)}: {fmt(so)}')
    print(f'OOS 3x-fee: {fmt(stress)}')
    print(f'OOS PF gain={f"{gain:+.3f}" if gain is not None else "NA"} PF retention={pct(retention)} flags={flags} verdict={"FAIL" if flags else "PASS"}')
    return a, b


def main():
    pairs = load_pairs()  # iteration-1 FIFO implementation, close append order
    is_pairs, oos_pairs = pairs[:110], pairs[110:]
    print(f'FIFO pairs={len(pairs)} IS={len(is_pairs)} OOS={len(oos_pairs)}')
    summarize('BASELINE', is_pairs, oos_pairs, lambda e, c: True)
    journal = [json.loads(line) for line in open(ROOT / 'runs/paper-1h/candidates.jsonl') if line.strip()]
    observations = defaultdict(list)
    for row in journal:
        if row.get('event') == 'candidate' and row.get('ts_epoch'):
            observations[(row.get('chain'), row.get('mint'))].append(row)

    def entry_epoch(e, c):
        """Infer the entry clock offset from a same-mint nearby observation.

        Some late journal rows print EDT while ts_epoch remains UTC. Trades only
        carry the printed wall clock, so a nearby same-mint row identifies the
        process-local offset without assuming one timezone for the full file.
        """
        wall = epoch(e['ts'])
        rows = observations[(infer_chain(e, c), e.get('mint'))]
        nearby = [(abs(wall-epoch(r['ts'])), r) for r in rows]
        if nearby:
            delta, row = min(nearby, key=lambda x: x[0])
            if delta <= 30*60:
                return wall + (float(row['ts_epoch'])-epoch(row['ts']))
        return wall
    vetoes = defaultdict(list)
    for row in journal:
        if row.get('event') == 'slip_veto' and float(row.get('ts_epoch', 0)) >= CUTOFF:
            vetoes[(row.get('chain'), row.get('mint'))].append(float(row['ts_epoch']))
    # Historical would-be vetoes are constructed from entry evidence, separately
    # from the journaled post-ship veto events. Entry order is not close order.
    for e, c in pairs:
        slip = e.get('slip_from_signal_pct')
        if slip is not None and slip > 0:
            t = entry_epoch(e, c)
            if t < CUTOFF:
                vetoes[(infer_chain(e, c), e.get('mint'))].append(t)
    for timestamps in vetoes.values():
        timestamps.sort()

    def sticky(e, c, same_day=True):
        t = entry_epoch(e, c)
        slip = e.get('slip_from_signal_pct')
        if slip is not None and slip > 0:
            return False  # an attempt creates and triggers its own veto
        key = (infer_chain(e, c), e.get('mint'))
        day = datetime.fromtimestamp(t, timezone.utc).date()
        return not any(v <= t and (not same_day or datetime.fromtimestamp(v, timezone.utc).date() == day)
                       for v in vetoes[key])

    for label, same_day in [('F2b same-day sticky', True), ('F2b forever sticky', False)]:
        a, b = summarize(label, is_pairs, oos_pairs, lambda e, c: sticky(e, c, same_day))
        blocked = [p for p in oos_pairs if p not in b]
        for kind, subset in [('positive-slip', [p for p in blocked if p[0].get('slip_from_signal_pct') is not None and p[0]['slip_from_signal_pct'] > 0]),
                             ('sticky re-admission', [p for p in blocked if p[0].get('slip_from_signal_pct') is None or p[0]['slip_from_signal_pct'] <= 0])]:
            s = stats(subset)
            wipes = sum(costed_usd(*p) <= -3.5 for p in subset if costed_usd(*p) is not None)
            print(f'  blocked {kind}: {fmt(s)} wipes={wipes} mints={[p[0].get("name") for p in subset]}')
        print(f'  regime: IS chains={dict(Counter(infer_chain(*p) for p in a))}; OOS chains={dict(Counter(infer_chain(*p) for p in b))}')

    summarize('F5 disable BSC 5m', is_pairs, oos_pairs,
              lambda e, c: not (infer_chain(e, c) == 'bsc' and e.get('window') == '5m'))

    for label, threshold in [('F3b literal slip<=-10 (unit diagnostic)', -10),
                             ('F3b correct -10%=fraction -0.10', -.10)]:
        a, b = summarize(label, is_pairs, oos_pairs,
                         lambda e, c: e.get('slip_from_signal_pct') is None or e['slip_from_signal_pct'] <= threshold)
        print(f'  known slips retained IS={sum(p[0].get("slip_from_signal_pct") is not None for p in a)} OOS={sum(p[0].get("slip_from_signal_pct") is not None for p in b)}; unknown fail-open IS={sum(p[0].get("slip_from_signal_pct") is None for p in a)} OOS={sum(p[0].get("slip_from_signal_pct") is None for p in b)}')

    print('=== F2 first-journaled sighting (descriptive only) ===')
    first = {}
    for row in journal:
        if row.get('event') == 'candidate' and float(row.get('ts_epoch', 0)) >= SIGHTING:
            key = (row.get('chain'), row.get('mint'))
            first[key] = min(first.get(key, float('inf')), float(row['ts_epoch']))
    ages = []
    for e, c in pairs:
        t = entry_epoch(e, c)
        if t < SIGHTING: continue
        key = (infer_chain(e, c), e.get('mint'))
        f = first.get(key)
        ages.append((e.get('name'), (t-f)/60 if f is not None and f <= t else None, e.get('pool_created_at'), costed_usd(e,c)))
    print('post-16:25 entries, name, journal age minutes, pool_created_at, costed USD:')
    for row in ages: print(row)
    known = [a for _,a,_,_ in ages if a is not None]
    print(f'n={len(ages)} sighting age available={len(known)} missing={len(ages)-len(known)}; min/median/max minutes={min(known) if known else None}/{sorted(known)[len(known)//2] if known else None}/{max(known) if known else None}')
    print(f'journal rows={len(journal)} null pool_created_at={sum(r.get("pool_created_at") is None for r in journal)}/{len(journal)}')


if __name__ == '__main__':
    main()
