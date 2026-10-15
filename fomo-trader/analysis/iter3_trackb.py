#!/usr/bin/env python3
"""Iteration 3 Track B screens (lane-direct; Codex/Astra quota-exhausted this iteration).
Reuses iter-1 FIFO pairing + lane-3 cost model. Walk-forward by close append order:
IS = first 2/3, OOS = last 1/3. Gates: OOS PF retention >=60% of IS; >=100 IS / >=30 OOS;
red flags (WR>90%, OOS decay>70%, single-regime-only, 3x-fee PF<1) -> FAIL.
Significance bar: OOS PF gain >= +0.25 vs baseline on same OOS window and/or
expectancy turning positive on >=30 OOS trades, surviving 3x-fee stress.
"""
import json, sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from iter1_trackb_screens import load_pairs, stats, costed_usd, infer_chain

ROOT = Path(__file__).resolve().parents[1]
CUTOFF = datetime(2026, 9, 28, 16, 24, tzinfo=timezone.utc).timestamp()  # slip veto ship


def epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(
        tzinfo=timezone.utc).timestamp()


def fmt(s):
    if s['n'] == 0:
        return 'n=0 net=NA PF=NA WR=NA exp=NA DD=NA'
    pf = s.get('pf')
    pf = f'{pf:.3f}' if isinstance(pf, (int, float)) else pf
    return (f"n={s['n']} net=${s.get('net', 0):+.2f} PF={pf} "
            f"WR={s.get('wr', 0):.1%} exp=${s.get('exp', 0):+.4f}")


def summarize(name, is_pairs, oos_pairs, keep):
    a = [p for p in is_pairs if keep(*p)]
    b = [p for p in oos_pairs if keep(*p)]
    si, so, stress = stats(a), stats(b), stats(b, slippage=.0045)
    baseline = stats(oos_pairs)
    si_pf, so_pf = si.get('pf'), so.get('pf')
    ok_pf = lambda x: isinstance(x, (int, float)) and x not in (0,)
    retention = (so_pf / si_pf) if (si['n'] and so['n'] and ok_pf(si_pf) and ok_pf(so_pf)) else None
    gain = so_pf - baseline['pf'] if (so['n'] and ok_pf(so_pf) and ok_pf(baseline.get('pf'))) else None
    flags = []
    if len(a) < 100: flags.append('IS<100')
    if len(b) < 30: flags.append('OOS<30')
    if retention is not None and retention < .6: flags.append('retention<60%')
    if retention is not None and 1 - retention > .7: flags.append('OOS decay>70%')
    if so.get('wr', 0) > .9: flags.append('WR>90%')
    if stress['n'] and not (isinstance(stress.get('pf'), (int, float)) and stress['pf'] >= 1):
        flags.append('3x-fee PF<1')
    if gain is None or (gain < .25 and so.get('exp', 0) <= 0):
        flags.append('no OOS significance')
    print(f'=== {name} ===')
    print(f'IS kept {len(a)}/{len(is_pairs)}: {fmt(si)}')
    print(f'OOS kept {len(b)}/{len(oos_pairs)}: {fmt(so)}')
    print(f'OOS 3x-fee: {fmt(stress)}')
    g = f'{gain:+.3f}' if gain is not None else 'NA'
    pct_ret = f'{retention:.1%}' if retention is not None else 'NA'
    print(f'OOS PF gain={g} PF retention={pct_ret} '
          f'flags={flags} verdict={"FAIL" if flags else "PASS"}')
    return a, b


def main():
    pairs = load_pairs()
    n = len(pairs)
    k = 2 * n // 3
    is_pairs, oos_pairs = pairs[:k], pairs[k:]
    print(f'FIFO pairs={n} IS={len(is_pairs)} OOS={len(oos_pairs)}')
    summarize('BASELINE', is_pairs, oos_pairs, lambda e, c: True)

    # C1: kill 5m entry window
    summarize('C1 disable 5m window', is_pairs, oos_pairs,
              lambda e, c: e.get('window') != '5m')

    # C2: disable BSC entries
    summarize('C2 disable BSC chain', is_pairs, oos_pairs,
              lambda e, c: infer_chain(e, c) != 'bsc')

    # --- F2b sticky-veto machinery (mirrors iter-2, entry-clock offset via candidates.jsonl)
    journal = [json.loads(l) for l in open(ROOT / 'runs/paper-1h/candidates.jsonl') if l.strip()]
    observations = defaultdict(list)
    for row in journal:
        if row.get('event') == 'candidate' and row.get('ts_epoch'):
            observations[(row.get('chain'), row.get('mint'))].append(row)

    def entry_epoch(e, c):
        wall = epoch(e['ts'])
        rows = observations[(infer_chain(e, c), e.get('mint'))]
        nearby = [(abs(wall - epoch(r['ts'])), r) for r in rows]
        if nearby:
            delta, row = min(nearby, key=lambda x: x[0])
            if delta <= 30 * 60:
                return wall + (float(row['ts_epoch']) - epoch(row['ts']))
        return wall

    vetoes = defaultdict(list)
    for row in journal:
        if row.get('event') == 'slip_veto' and float(row.get('ts_epoch', 0)) >= CUTOFF:
            vetoes[(row.get('chain'), row.get('mint'))].append(float(row['ts_epoch']))
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
            return False
        key = (infer_chain(e, c), e.get('mint'))
        day = datetime.fromtimestamp(t, timezone.utc).date()
        return not any(v <= t and (not same_day or
                                   datetime.fromtimestamp(v, timezone.utc).date() == day)
                       for v in vetoes[key])

    a3, b3 = summarize('C3 F2b same-day sticky veto', is_pairs, oos_pairs,
                       lambda e, c: sticky(e, c, True))
    blocked = [p for p in oos_pairs if p not in b3]
    for kind, subset in [('positive-slip', [p for p in blocked if p[0].get('slip_from_signal_pct') not in (None,) and p[0]['slip_from_signal_pct'] > 0]),
                         ('sticky re-admission', [p for p in blocked if p[0].get('slip_from_signal_pct') is None or p[0]['slip_from_signal_pct'] <= 0])]:
        s = stats(subset)
        wipes = sum(costed_usd(*p) <= -3.5 for p in subset if costed_usd(*p) is not None)
        print(f'  blocked {kind}: {fmt(s)} wipes={wipes} names={[p[0].get("name") for p in subset]}')

    # C4: slip <= -0.10 band + F2b
    summarize('C4 slip<=-0.10 + F2b sticky', is_pairs, oos_pairs,
              lambda e, c: (e.get('slip_from_signal_pct') is None or
                            e['slip_from_signal_pct'] <= -0.10) and sticky(e, c, True))

    # C7: gain-quintile gate — only if an IS quintile shows PF>=1 with n>=30
    gains = sorted((float(e.get('signal_gain_pct') or 0), i) for i, (e, c) in enumerate(is_pairs))
    qs = [gains[i * len(gains) // 5:(i + 1) * len(gains) // 5] for i in range(5)]
    print('=== C7 gain-quintile exploration (IS) ===')
    cand = None
    for i, q in enumerate(qs):
        sub = [is_pairs[j] for _, j in q]
        s = stats(sub)
        lo, hi = q[0][0], q[-1][0]
        print(f'  Q{i + 1} gain [{lo:.0f},{hi:.0f}]: {fmt(s)}')
        if s['n'] >= 30 and isinstance(s.get('pf'), (int, float)) and s['pf'] >= 1:
            cand = (i, lo, hi)
    if cand:
        i, lo, hi = cand
        summarize(f'C7 gain band Q{i+1} [{lo:.0f},{hi:.0f}]',
                  is_pairs, oos_pairs,
                  lambda e, c, lo=lo, hi=hi: lo <= float(e.get('signal_gain_pct') or 0) <= hi)
    else:
        print('  -> no IS quintile with PF>=1 at n>=30: C7 NOT_VERIFIABLE as a candidate')

    # C5 descriptive: dump vs trailing-stop entry metrics
    def fam(c):
        rs = c.get('reason', '')
        if 'dump detector' in rs or 'venue dump' in rs: return 'dump'
        if 'trailing stop' in rs: return 'trail'
        if 'stale exit' in rs: return 'stale'
        if 'take profit' in rs: return 'tp'
        return 'other'
    print('=== C5 exit-family study (descriptive) ===')
    for f in ['dump', 'trail', 'stale', 'tp']:
        sub = [(e, c) for e, c in pairs if fam(c) == f]
        vals = [costed_usd(e, c) for e, c in sub]
        vals = [v for v in vals if v is not None]
        net = sum(vals)
        print(f'  {f}: n={len(sub)} valued={len(vals)} net=${net:+.2f}')
    # C6 descriptive: stale-exit counterfactual bound
    stale = [(e, c) for e, c in pairs if fam(c) == 'stale']
    sv = [costed_usd(e, c) for e, c in stale]
    sv = [v for v in sv if v is not None]
    print(f'=== C6 stale-exit counterfactual ===')
    print(f'  stale exits realized: n={len(sv)} net=${sum(sv):+.2f}; '
          f'if held to full wipe instead: worst-case net=${sum(min(v, -6.94) for v in sv):+.2f} '
          f'(bounded by max observed stake loss ~$6.94)')


if __name__ == '__main__':
    main()
