#!/usr/bin/env python3
"""Iteration 4 Track B, lane-direct leg (Codex/Astra run in parallel as agents).
Mirrors iter-3 methodology: FIFO pairing, costed_usd (slip 0.0015 base),
walk-forward by close append order IS=first 2/3, OOS=last 1/3.
Gates: IS>=100, OOS>=30, OOS PF retention>=60% of IS, 3x-fee (slip 0.0045)
OOS PF>=1.0, significance = OOS PF gain>=+0.25 vs same-window baseline OR
exp>0 on >=30 OOS. Red flags -> FAIL.
"""
import json, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from iter1_trackb_screens import load_pairs, stats, costed_usd, infer_chain

ROOT = Path(__file__).resolve().parents[1]
CUTOFF = datetime(2026, 9, 28, 16, 24, tzinfo=timezone.utc).timestamp()


def epoch(v):
    if isinstance(v, (int, float)):
        return float(v)
    return datetime.fromisoformat(v.replace('Z', '+00:00')).replace(
        tzinfo=timezone.utc).timestamp()


def fmt(s):
    if s['n'] == 0:
        return 'n=0 net=NA PF=NA WR=NA exp=NA'
    pf = s.get('pf')
    pf = f'{pf:.3f}' if isinstance(pf, (int, float)) else pf
    return (f"n={s['n']} net=${s.get('net', 0):+.2f} PF={pf} "
            f"WR={s.get('wr', 0):.1%} exp=${s.get('exp', 0):+.4f}")


def verdict(name, a, b, baseline_pf):
    si, so, stress = stats(a), stats(b), stats(b, slippage=.0045)
    ok = lambda x: isinstance(x, (int, float)) and x not in (0,)
    ret = (so.get('pf') / si.get('pf')) if (si['n'] and so['n'] and ok(so.get('pf')) and ok(si.get('pf'))) else None
    gain = so.get('pf') - baseline_pf if (so['n'] and ok(so.get('pf')) and ok(baseline_pf)) else None
    flags = []
    if len(a) < 100: flags.append('IS<100')
    if len(b) < 30: flags.append('OOS<30')
    if ret is not None and ret < .6: flags.append('retention<60%')
    if ret is not None and 1 - ret > .7: flags.append('OOS decay>70%')
    if so.get('wr', 0) > .9: flags.append('WR>90%')
    if stress['n'] and not (isinstance(stress.get('pf'), (int, float)) and stress.get('pf') >= 1):
        flags.append('3x-fee PF<1')
    if gain is None or (gain < .25 and so.get('exp', 0) <= 0):
        flags.append('no OOS significance')
    v = 'FAIL' if flags else 'PASS'
    g = f'{gain:+.3f}' if gain is not None else 'NA'
    r = f'{ret:.1%}' if ret is not None else 'NA'
    print(f'=== {name} ===')
    print(f'IS kept {len(a)}: {fmt(si)}')
    print(f'OOS kept {len(b)}: {fmt(so)}')
    print(f'OOS 3x-fee: {fmt(stress)}')
    print(f'PF gain={g} retention={r} flags={flags} verdict={v}')
    return v


def main():
    pairs = load_pairs()
    n = len(pairs)
    k = 2 * n // 3
    is_pairs, oos_pairs = pairs[:k], pairs[k:]
    print(f'FIFO pairs={n} IS={len(is_pairs)} OOS={len(oos_pairs)}')
    base = stats(oos_pairs)
    print(f'OOS baseline: {fmt(base)}')

    def K(name, fn):
        a = [p for p in is_pairs if fn(*p)]
        b = [p for p in oos_pairs if fn(*p)]
        return verdict(name, a, b, base.get('pf'))

    K('BASELINE', lambda e, c: True)
    K('C8a gain<=100', lambda e, c: float(e.get('signal_gain_pct') or 0) <= 100)
    K('C8b gain<=60', lambda e, c: float(e.get('signal_gain_pct') or 0) <= 60)

    # F2b sticky veto machinery
    journal = [json.loads(l) for l in open(ROOT / 'runs/paper-1h/candidates.jsonl') if l.strip()]
    obs = defaultdict(list)
    for row in journal:
        if row.get('event') == 'candidate' and row.get('ts_epoch'):
            obs[(row.get('chain'), row.get('mint'))].append(row)
    vetoes = defaultdict(list)
    for row in journal:
        if row.get('event') == 'slip_veto' and float(row.get('ts_epoch', 0)) >= CUTOFF:
            vetoes[(row.get('chain'), row.get('mint'))].append(float(row['ts_epoch']))

    def entry_epoch(e, c):
        wall = epoch(e['ts'])
        rows = obs[(infer_chain(e, c), e.get('mint'))]
        if rows:
            delta, row = min(((abs(wall - epoch(r['ts'])), r) for r in rows), key=lambda x: x[0])
            if delta <= 1800:
                return wall + (float(row['ts_epoch']) - epoch(row['ts']))
        return wall

    for e, c in pairs:
        slip = e.get('slip_from_signal_pct')
        if slip is not None and slip > 0:
            t = entry_epoch(e, c)
            if t < CUTOFF:
                vetoes[(infer_chain(e, c), e.get('mint'))].append(t)
    for ts in vetoes.values():
        ts.sort()

    def sticky(e, c):
        t = entry_epoch(e, c)
        if (e.get('slip_from_signal_pct') or 0) > 0:
            return False
        key = (infer_chain(e, c), e.get('mint'))
        day = datetime.fromtimestamp(t, timezone.utc).date()
        return not any(v <= t and datetime.fromtimestamp(v, timezone.utc).date() == day
                       for v in vetoes[key])

    K('C8c gain<=100 + F2b', lambda e, c: float(e.get('signal_gain_pct') or 0) <= 100 and sticky(e, c))
    K('C8d gain<=60 + F2b', lambda e, c: float(e.get('signal_gain_pct') or 0) <= 60 and sticky(e, c))
    K('C3 F2b alone (retest)', lambda e, c: sticky(e, c))

    # C9 diagnostics: IS-only entry-metric buckets, PF>=1 & n>=30 qualifies
    print('=== C9 wipe-predictor diagnostics (IS only, natural buckets) ===')
    def buckets(label, keyfn):
        bs = defaultdict(list)
        for p in is_pairs:
            bs[keyfn(*p)].append(p)
        for name in sorted(bs, key=str):
            s = stats(bs[name])
            pf = s.get('pf')
            mark = ' <-- QUALIFIES' if (s['n'] >= 30 and isinstance(pf, (int, float)) and pf >= 1) else ''
            print(f'  {label}={name}: {fmt(s)}{mark}')
    buckets('window', lambda e, c: e.get('window'))
    buckets('chain', lambda e, c: infer_chain(e, c))
    buckets('liq', lambda e, c: 'hi' if (e.get('liquidity_usd') or 0) >= 20000 else 'lo')
    buckets('buys', lambda e, c: 'hi' if (e.get('m15_buys') or 0) >= 50 else 'lo')
    buckets('buysell', lambda e, c: 'hi' if (e.get('m15_buys') or 0) >= (e.get('m15_sells') or 0) else 'lo')
    buckets('slip', lambda e, c: 'pos' if (e.get('slip_from_signal_pct') or 0) > 0 else ('neg' if (e.get('slip_from_signal_pct') or 0) < -0.05 else 'flat'))

    # gain-inversion refresh (all pairs, costed)
    print('=== gain-inversion refresh (all pairs, costed) ===')
    caps = [40, 60, 80, 100, 150, 200]
    for cap in caps:
        sub = [p for p in pairs if float(p[0].get('signal_gain_pct') or 0) <= cap]
        print(f'  gain<={cap}: {fmt(stats(sub))}')
    gains = sorted((float(p[0].get('signal_gain_pct') or 0), i) for i, p in enumerate(pairs))
    qs = [gains[i * len(gains) // 5:(i + 1) * len(gains) // 5] for i in range(5)]
    for i, q in enumerate(qs):
        sub = [pairs[j] for _, j in q]
        print(f'  Q{i+1} gain[{q[0][0]:.0f},{q[-1][0]:.0f}]: {fmt(stats(sub))}')

    # wipes refresh
    wv = [(p, costed_usd(*p)) for p in pairs]
    wv = [(p, v) for p, v in wv if v is not None]
    wipes = [v for _, v in wv if v <= -3.5]
    net = sum(v for _, v in wv)
    print(f'=== wipes: {len(wipes)}/{len(wv)} pairs, wipe_sum=${sum(wipes):+.2f}, net=${net:+.2f}, share={sum(wipes)/net:.1%}')

    # forward-validation protocol: closes entered after CUTOFF
    post = []
    for e, c in pairs:
        try:
            t = epoch(e['ts'])
        except Exception:
            continue
        if t >= CUTOFF:
            post.append((e, c))
    sp = stats(post)
    print(f'=== forward-validation (>=2026-09-28 16:24 UTC entries): {fmt(sp)}')


if __name__ == '__main__':
    main()
