#!/usr/bin/env python3
"""iter8_lane_direct.py — LANE-CODEX-ASTRA iteration 8 (lane-direct: Codex quota-walled).

Read-only vs bot state. Never touches trades.jsonl / state.json / bot.log / deaths.log.

1. Baseline repro on frozen 215-close book (fidelity check vs iter-7: -$175.52 / PF 0.2747 / WR 26.6%)
   then full-book (235 closes) headline + regime cuts.
2. R11: adverse-selection processing order. Replay: per ET-day, enter-verdict candidate pool
   first-seen per (chain,mint); N_d = actual entries that day; status-quo = top-N desc m15_gain
   (fidelity check vs actual entered set), R11 = top-N asc m15_gain. Walk-forward IS/OOS by
   close append order; gates per frozen Track B.
3. R12/early cohort characterization (entered early trades by decel ratio).
4. Regime diagnosis: gain quartile, chain, window, ET day, exit reason.

Cost model (frozen, matches iter1_trackb_screens.py):
  costed = raw_usd - rate * [stake * (1 + exit/entry) * (0.0025 + slippage) + fixed]
  slippage base 0.0015/leg; 3x-fee stress slippage 0.0045/leg; fixed 0.004 SOL / 0.00004 BNB.
"""
import json, math
from collections import defaultdict, deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PROJ = '/home/hatch/workspace/fomo-trader'
ET = ZoneInfo('America/New_York')

# ---------- pairing / costing (frozen) ----------
def load_pairs():
    trades = [json.loads(l) for l in open(f'{PROJ}/runs/paper-1h/trades.jsonl') if l.strip()]
    openq, pairs = {}, []
    for t in trades:
        key = (t.get('chain') or ('bsc' if str(t.get('mint','')).startswith('0x') else 'solana'),
               t.get('mint'))
        if t.get('type') == 'entry':
            openq.setdefault(key, deque()).append(t)
        elif t.get('type') == 'close':
            if key in openq and openq[key]:
                pairs.append((openq[key].popleft(), t))
    return pairs  # close append order

def infer_chain(e, c):
    ch = c.get('chain') or e.get('chain')
    if ch: return ch
    return 'bsc' if str(e.get('mint','')).startswith('0x') else 'solana'

def raw_usd(e, c):
    if c.get('realized_usd') is not None:
        return float(c['realized_usd'])
    nat = c.get('realized_sol')
    if nat is None: nat = c.get('realized_bnb')
    if nat is None: return None
    rate = c.get('sol_usd') or e.get('sol_usd')
    if rate is None: rate = 115 if infer_chain(e,c)=='solana' else 600
    return float(nat)*float(rate)

def costed_usd(e, c, slippage=0.0015):
    r = raw_usd(e, c)
    if r is None or not e.get('entry') or not c.get('exit'): return None
    chain = infer_chain(e, c)
    rate = c.get('sol_usd') or e.get('sol_usd') or (115 if chain=='solana' else 600)
    stake = float(e.get('buy_sol') or 0)
    fixed = 0.004 if chain=='solana' else 0.00004
    ratio = float(c['exit'])/float(e['entry'])
    return r - float(rate)*(stake*(1+ratio)*(0.0025+slippage) + fixed)

def stats_of(vals):
    n=len(vals)
    if n==0: return dict(n=0)
    gp=sum(v for v in vals if v>0); gl=-sum(v for v in vals if v<0)
    wins=sum(1 for v in vals if v>0)
    return dict(n=n, net=sum(vals), pf=(gp/gl if gl>0 else float('inf')),
                wr=wins/n, avg=sum(vals)/n)

def et_day_of(row):
    te = row.get('ts_epoch')
    if te:
        return datetime.fromtimestamp(float(te), tz=ET).strftime('%Y-%m-%d')
    ts = str(row.get('ts',''))
    try:
        return datetime.strptime(ts[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).astimezone(ET).strftime('%Y-%m-%d')
    except Exception:
        return '?'

pairs = load_pairs()
print('FIFO pairs:', len(pairs))

# ---------- 1. baseline ----------
def pair_stats(pairs, slip=0.0015):
    vals=[costed_usd(e,c,slip) for e,c in pairs]
    vals=[v for v in vals if v is not None]
    return stats_of(vals), vals

def show(label, pairs, slip=0.0015):
    s, vals = pair_stats(pairs, slip)
    dd = max_drawdown(vals)
    print(f'{label}: n={s["n"]} net=${s["net"]:.2f} PF={s["pf"]:.4f} WR={s["wr"]:.3f} avg=${s["avg"]:.2f} maxDD=${dd:.2f}')
    return s

def max_drawdown(vals):
    peak=0.0; cum=0.0; mdd=0.0
    for v in vals:
        cum+=v; peak=max(peak,cum); mdd=max(mdd, peak-cum)
    return mdd

print('\n=== 1a. FIDELITY: frozen 215-close book (iter-7 baseline) ===')
p215 = pairs[:215]
usd215 = [(e,c) for e,c in p215 if c.get('realized_usd') is not None]
print('USD-stamped closes:', len(usd215))
s215 = show('iter7-book USD-stamped', usd215)
bsc215 = [(e,c) for e,c in p215 if infer_chain(e,c)=='bsc']
print('BSC closes:', len(bsc215)); show('iter7-book BSC', bsc215)

print('\n=== 1b. FULL BOOK headline ===')
show('FULL all', pairs)
show('FULL 3x-fee', pairs, 0.0045)
by_chain=defaultdict(list)
for e,c in pairs: by_chain[infer_chain(e,c)].append((e,c))
for ch,pl in by_chain.items(): show(f'FULL chain={ch}', pl)
usd_all=[(e,c) for e,c in pairs if c.get('realized_usd') is not None]
show('FULL USD-stamped', usd_all)

print('\n=== 1c. exit-reason cut (full book) ===')
by_exit=defaultdict(list)
for e,c in pairs:
    r=str(c.get('reason',''))
    if 'dump' in r.lower() or '-100' in r or 'venue' in r.lower(): k='dump/wipe'
    elif 'trailing' in r.lower(): k='trailing'
    elif 'take' in r.lower() or 'tp' in r.lower() or 'rung' in r.lower(): k='take-profit'
    elif 'stale' in r.lower(): k='stale'
    elif 'hard' in r.lower() or 'stop' in r.lower(): k='hard-stop'
    else: k='other:'+r[:40]
    by_exit[k].append((e,c))
for k,pl in sorted(by_exit.items(), key=lambda x:-len(x[1])):
    show(f'  exit={k}', pl)
