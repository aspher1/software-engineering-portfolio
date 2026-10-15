#!/usr/bin/env python3
"""Iteration 5 Track B, lane-direct leg (Codex implementer + Astra analyst run
in parallel as CLI agents). Independent cross-check of R3 fee-bias veto,
S1 one-touch, S2 venue, marginal slip-band mechanism check, F2 data growth.
Conventions mirror iter-4: FIFO pairing, costed_usd (0.0015/leg), walk-forward
close-append IS=first 2/3 OOS=last 1/3. Gates: IS>=100, OOS>=30,
retention>=60%, 3x-fee OOS PF>=1, significance = OOS PF gain>=+0.25 vs
same-window baseline OR exp>0 on >=30 OOS. Red flags -> FAIL.
"""
import json, sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from iter1_trackb_screens import load_pairs, stats, costed_usd

ROOT = Path(__file__).resolve().parents[1]
FEE_BIAS = 0.0025  # PancakeSwap V2 LP fee, on-chain constant, pre-declared


def fmt(s):
    if s.get('n', 0) == 0:
        return 'n=0'
    pf = s.get('pf')
    pf = f'{pf:.3f}' if isinstance(pf, (int, float)) else pf
    return (f"n={s['n']} net=${s.get('net', 0):+.2f} PF={pf} "
            f"WR={s.get('wr', 0):.1%} exp=${s.get('exp', 0):+.4f} "
            f"dd=${s.get('max_dd', 0):.2f}")


def slip(e):
    v = e.get('slip_from_signal_pct')
    try:
        v = float(v)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def verdict(name, a, b, base_pf):
    si, so = stats(a), stats(b)
    stress = stats(b, slippage=.0045)
    ok = lambda x: isinstance(x, (int, float)) and x not in (0,)
    ret = (so['pf'] / si['pf']) if (si['n'] and so['n'] and ok(so.get('pf'))
                                    and ok(si.get('pf'))) else None
    gain = (so['pf'] - base_pf) if (so['n'] and ok(so.get('pf'))
                                    and ok(base_pf)) else None
    flags = []
    if len(a) < 100: flags.append('IS<100')
    if len(b) < 30: flags.append('OOS<30')
    if ret is not None and ret < .6: flags.append('retention<60%')
    if ret is not None and 1 - ret > .7: flags.append('OOSdecay>70%')
    if so.get('wr', 0) > .9: flags.append('WR>90%')
    if stress['n'] and not (isinstance(stress.get('pf'), (int, float))
                             and stress['pf'] >= 1):
        flags.append('3x-fee<1')
    if gain is None or (gain < .25 and so.get('exp', 0) <= 0):
        flags.append('no-significance')
    v = 'FAIL' if flags else 'PASS'
    g = f'{gain:+.3f}' if gain is not None else 'NA'
    r = f'{ret:.1%}' if ret is not None else 'NA'
    print(f'=== {name} ===')
    print(f'  IS kept {len(a)}: {fmt(si)}')
    print(f'  OOS kept {len(b)}: {fmt(so)}')
    print(f'  OOS 3x-fee: {fmt(stress)}')
    print(f'  PFgain={g} retention={r} flags={flags} -> {v}')
    return v, dict(si=si, so=so, stress=stress, gain=gain, ret=ret, flags=flags)


def main():
    pairs = [(e, c) for e, c in load_pairs()
             if (c.get('chain') or e.get('chain')) == 'bsc']
    n = len(pairs)
    k = 2 * n // 3
    isp, oosp = pairs[:k], pairs[k:]
    print(f'BSC FIFO pairs={n} IS={len(isp)} OOS={len(oosp)}')
    base = stats(oosp)
    print(f'OOS baseline: {fmt(base)}')
    base_is = stats(isp)
    print(f'IS baseline:  {fmt(base_is)}')

    def K(name, fn):
        return verdict(name, [p for p in isp if fn(*p)],
                       [p for p in oosp if fn(*p)], base.get('pf'))

    # marginal slip band mechanism check (full book)
    print('\n--- slip micro-bands (full book) ---')
    bands = [('-inf,-0.5%]', lambda s: s is not None and s <= -0.005),
             ('(-0.5%,0]', lambda s: s is not None and -0.005 < s <= 0),
             ('(0,0.25%] R3-marginal', lambda s: s is not None and 0 < s <= FEE_BIAS),
             ('(0.25%,1%]', lambda s: s is not None and FEE_BIAS < s <= 0.01),
             ('(1%,inf)', lambda s: s is not None and s > 0.01),
             ('missing', lambda s: s is None)]
    for name, fn in bands:
        q = [p for p in pairs if fn(slip(p[0]))]
        print(f'  {name:22s} {fmt(stats(q))}')

    # R3 vs live rule head-to-head
    print('\n--- R3 vs live strict>0 veto (full book kept sets) ---')
    live = [p for p in pairs if (slip(p[0]) is None or slip(p[0]) <= 0)]
    r3 = [p for p in pairs if (slip(p[0]) is None or slip(p[0]) <= FEE_BIAS)]
    print(f'  live rule kept: {fmt(stats(live))}')
    print(f'  R3 kept:        {fmt(stats(r3))}')
    print(f'  delta pairs admitted by R3: {len(r3) - len(live)}')

    K('R3 fee-bias veto', lambda e, c: slip(e) is None or slip(e) <= FEE_BIAS)

    # S1: one-touch per mint per UTC day (first entry per mint+day in append order)
    seen = set()
    def s1(e, c):
        key = (e.get('chain'), e.get('mint'), str(e.get('ts', ''))[:10])
        if key in seen:
            return False
        seen.add(key)
        return True
    seen.clear()
    K('S1 one-touch/mint/day', s1)

    # S1b: first-ever entry per mint vs re-entries (bucket stats, full book)
    print('\n--- first-ever vs re-entry buckets (full book) ---')
    first, re = [], []
    seen_m = set()
    for e, c in pairs:
        m = (e.get('chain'), e.get('mint'))
        (first if m not in seen_m else re).append((e, c))
        seen_m.add(m)
    print(f'  first-ever {fmt(stats(first))}')
    print(f'  re-entry   {fmt(stats(re))}')

    # S2: venue buckets
    print('\n--- venue buckets (full book) ---')
    by_src = defaultdict(list)
    for p in pairs:
        by_src[p[0].get('source') or 'unknown'].append(p)
    for src, q in sorted(by_src.items(), key=lambda x: -len(x[1])):
        print(f'  {src:16s} {fmt(stats(q))}')
    maj = max(by_src.items(), key=lambda x: len(x[1]))[0]
    print(f'  majority source: {maj}')
    K(f'S2 keep-{maj}-only', lambda e, c: (e.get('source') or 'unknown') == maj)

    # split falsification for R3
    print('\n--- R3 split falsification ---')
    for frac in (0.6, 0.7):
        kk = int(n * frac)
        ai, ao = pairs[:kk], pairs[kk:]
        b = stats(ao)
        v, d = verdict(f'R3 @{int(frac*100)}/{int(100-frac*100)}',
                       [p for p in ai if slip(p[0]) is None or slip(p[0]) <= FEE_BIAS],
                       [p for p in ao if slip(p[0]) is None or slip(p[0]) <= FEE_BIAS],
                       b.get('pf'))

    # concentration: drop top-2 OOS winners from R3 kept
    print('\n--- R3 OOS concentration check ---')
    r3oos = [p for p in oosp if slip(p[0]) is None or slip(p[0]) <= FEE_BIAS]
    vals = sorted(((costed_usd(e, c), (e, c)) for e, c in r3oos),
                  key=lambda x: x[0] or -9e9, reverse=True)
    trimmed = [p for _, p in vals[2:]]
    print(f'  R3 OOS full:    {fmt(stats(r3oos))}')
    print(f'  R3 OOS drop-2:  {fmt(stats(trimmed))}')

    # F2 data growth
    print('\n--- F2 pool-age data growth ---')
    aged = [(e, c) for e, c in pairs if e.get('pool_created_at')]
    print(f'  paired closes with pool_created_at: {len(aged)} (need >=30/side)')
    jr = [json.loads(l) for l in open(ROOT / 'runs/paper-1h/candidates.jsonl')
          if l.strip()]
    with_pc = sum(1 for r in jr if r.get('pool_created_at'))
    print(f'  candidates.jsonl rows: {len(jr)}, with pool_created_at: {with_pc}')


if __name__ == '__main__':
    main()
