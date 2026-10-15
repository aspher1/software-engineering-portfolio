#!/usr/bin/env python3
"""
iter1_trackb_screens.py — Track B screen validation for deep-improvement loop iteration 1.

Tests retrospective entry screens on lifetime FIFO pairs from runs/paper-1h/trades.jsonl.
One candidate at a time. Read-only w.r.t. bot state (never touches journals/state/logs).

Cost model (matches lane-3 research-pass convention):
  costed = raw_usd - rate * [stake * (1 + exit/entry) * (0.0025 + slippage) + fixed]
  base slippage 0.15%/leg; stress slippage 0.45%/leg (3x-fee stress)
  fixed round-trip: 0.004 SOL / 0.00004 BNB

Walk-forward: pairs ordered by close APPEND ORDER (timestamps have discontinuities),
  IS = first 110 pairs, OOS = last 54 pairs.
Gates: OOS PF retention >= 60% of IS PF; >=100 IS / >=30 OOS trades;
  red flags -> REJECT: WR > 90%, OOS decay > 70%, single-regime only, fails 3x-fee stress.
Significance (coordinator's bar): OOS PF gain >= +0.25 vs baseline on same OOS window,
  and/or expected PnL/trade turning positive on >= 30 OOS trades, surviving 3x-fee stress.

Caveat: a retrospective veto removes the trade's P&L with no capital redeployment.
This is a screen-quality test, not a full strategy replay.
"""
import json, math
from collections import deque

PROJ = '/home/hatch/workspace/fomo-trader'

def load_pairs():
    trades = [json.loads(l) for l in open(f'{PROJ}/runs/paper-1h/trades.jsonl') if l.strip()]
    openq, pairs = {}, []
    for t in trades:
        key = (t.get('chain'), t.get('mint'))
        if t.get('type') == 'entry':
            openq.setdefault(key, deque()).append(t)
        elif t.get('type') == 'close':
            if key in openq and openq[key]:
                pairs.append((openq[key].popleft(), t))
    return pairs  # append order

def infer_chain(e, c):
    ch = c.get('chain') or e.get('chain')
    if ch: return ch
    mint = e.get('mint', '')
    return 'bsc' if mint.startswith('0x') else 'solana'

def raw_usd(e, c):
    if c.get('realized_usd') is not None:
        return float(c['realized_usd'])
    nat = c.get('realized_sol')
    if nat is None:
        nat = c.get('realized_bnb')
    if nat is None:
        return None
    rate = c.get('sol_usd') or e.get('sol_usd')
    if rate is None:
        rate = 115 if infer_chain(e, c) == 'solana' else 600
    return float(nat) * float(rate)

def costed_usd(e, c, slippage=0.0015):
    r = raw_usd(e, c)
    if r is None or not e.get('entry') or not c.get('exit'):
        return None
    chain = infer_chain(e, c)
    rate = c.get('sol_usd') or e.get('sol_usd') or (115 if chain == 'solana' else 600)
    stake = float(e.get('buy_sol') or 0)
    fixed = 0.004 if chain == 'solana' else 0.00004
    ratio = float(c['exit']) / float(e['entry'])
    cost = float(rate) * (stake * (1 + ratio) * (0.0025 + slippage) + fixed)
    return r - cost

def stats(pairs, slippage=0.0015):
    vals = []
    for e, c in pairs:
        v = costed_usd(e, c, slippage)
        if v is not None:
            vals.append(v)
    n = len(vals)
    if n == 0:
        return dict(n=0)
    wins = [v for v in vals if v > 0]
    losses = [-v for v in vals if v <= 0]
    gross_w = sum(wins); gross_l = sum(losses)
    pf = gross_w / gross_l if gross_l > 0 else float('inf')
    # max drawdown on cumulative curve
    cum, peak, dd = 0.0, 0.0, 0.0
    for v in vals:
        cum += v; peak = max(peak, cum); dd = max(dd, peak - cum)
    return dict(n=n, net=round(sum(vals), 2),
                pf=round(pf, 3) if pf != float('inf') else 'inf',
                wr=round(len(wins) / n, 4),
                exp=round(sum(vals) / n, 4),
                avg_win=round(gross_w / len(wins), 4) if wins else 0,
                avg_loss=round(gross_l / len(losses), 4) if losses else 0,
                max_dd=round(dd, 2))

def report(name, pairs_is, pairs_oos, screen_fn, slippage_base=0.0015):
    kept_is = [(e, c) for e, c in pairs_is if screen_fn(e, c)]
    kept_oos = [(e, c) for e, c in pairs_oos if screen_fn(e, c)]
    s_is, s_oos = stats(kept_is), stats(kept_oos)
    s_oos_stress = stats(kept_oos, slippage=0.0045)
    base_is = stats(pairs_is); base_oos = stats(pairs_oos)
    print(f'=== {name} ===')
    print(f'  IS : kept {s_is["n"]}/{len(pairs_is)}  net={s_is["net"]} pf={s_is["pf"]} wr={s_is["wr"]} exp/trade={s_is["exp"]} dd={s_is["max_dd"]}')
    print(f'  OOS: kept {s_oos["n"]}/{len(pairs_oos)}  net={s_oos["net"]} pf={s_oos["pf"]} wr={s_oos["wr"]} exp/trade={s_oos["exp"]} dd={s_oos["max_dd"]}')
    print(f'  OOS 3x-fee: net={s_oos_stress["net"]} pf={s_oos_stress["pf"]} wr={s_oos_stress["wr"]}')
    # gates
    try:
        ret = (s_oos['pf'] / s_is['pf']) if s_is['pf'] not in (0, 'inf') and s_oos['pf'] != 'inf' else None
    except Exception:
        ret = None
    decay = (1 - ret) if ret is not None else None
    base_ret = None
    try:
        if base_is['pf'] not in (0, 'inf') and base_oos['pf'] != 'inf' and base_is['pf']:
            base_ret = base_oos['pf'] / base_is['pf']
    except Exception:
        pass
    pf_gain = None
    try:
        if s_oos['pf'] != 'inf' and base_oos['pf'] != 'inf':
            pf_gain = s_oos['pf'] - base_oos['pf']
    except Exception:
        pass
    print(f'  baseline OOS (no screen): n={base_oos["n"]} net={base_oos["net"]} pf={base_oos["pf"]} wr={base_oos["wr"]} retention={round(base_ret,3) if base_ret else None}')
    print(f'  screen OOS retention vs IS: {round(ret,3) if ret else None} | OOS decay: {round(decay,3) if decay else None} | OOS PF gain vs baseline: {round(pf_gain,3) if pf_gain is not None else None}')
    flags = []
    if s_is['n'] < 100: flags.append('IS<100')
    if s_oos['n'] < 30: flags.append('OOS<30')
    if ret is not None and ret < 0.6: flags.append('retention<60%')
    if s_oos.get('wr', 0) > 0.9: flags.append('WR>90%')
    if decay is not None and decay > 0.7: flags.append('OOS decay>70%')
    if s_oos_stress['pf'] != 'inf' and isinstance(s_oos_stress['pf'], float) and s_oos_stress['pf'] < 1.0 and s_oos['pf'] != 'inf':
        flags.append('fails 3x-fee (PF<1)')
    verdict = 'FAIL' if flags else 'PASS-GATES'
    # significance bar (coordinator's): ONLY for gate-passing candidates:
    # OOS PF gain >= +0.25 and/or exp/trade positive on >=30 OOS, surviving 3x stress
    stress_ok = (s_oos_stress['pf'] == 'inf' or
                 (isinstance(s_oos_stress['pf'], float) and s_oos_stress['pf'] >= 1.0))
    sig = (not flags) and (s_oos['n'] >= 30) and stress_ok and (
        (pf_gain is not None and pf_gain >= 0.25) or (s_oos['exp'] > 0))
    print(f'  VERDICT: {verdict}  flags={flags}  significant={sig}')
    print()
    return dict(name=name, is_stats=s_is, oos_stats=s_oos, oos_stress=s_oos_stress,
                retention=ret, decay=decay, pf_gain=pf_gain, flags=flags,
                verdict=verdict, significant=sig)

if __name__ == '__main__':
    pairs = load_pairs()
    print(f'total pairs: {len(pairs)}')
    # regime mix per split
    is_pairs, oos_pairs = pairs[:110], pairs[110:]
    for nm, grp in [('IS', is_pairs), ('OOS', oos_pairs)]:
        chains = {}
        for e, c in grp:
            chains[infer_chain(e, c)] = chains.get(infer_chain(e, c), 0) + 1
        gains = [e.get('signal_gain_pct') for e, c in grp if e.get('signal_gain_pct') is not None]
        print(f'{nm}: n={len(grp)} chains={chains} signal_gain median={sorted(gains)[len(gains)//2]:.1f} max={max(gains):.1f}')
    print()
    report('BASELINE (no screen)', is_pairs, oos_pairs, lambda e, c: True)
    report('G1a: veto signal_gain >= 60 (NEW)', is_pairs, oos_pairs,
           lambda e, c: (e.get('signal_gain_pct') or 0) < 60)
    report('G1b: veto signal_gain >= 100 (control; prior round tested)', is_pairs, oos_pairs,
           lambda e, c: (e.get('signal_gain_pct') or 0) < 100)
    report('G2: veto positive slip (enriched subset only; shipped-veto reproduction)', is_pairs, oos_pairs,
           lambda e, c: (e.get('slip_from_signal_pct') is None) or (e.get('slip_from_signal_pct') <= 0))
