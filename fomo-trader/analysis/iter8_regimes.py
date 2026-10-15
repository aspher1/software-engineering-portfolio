#!/usr/bin/env python3
"""iter8 R11 mechanism + selection experiment, R12 characterization, regimes. Read-only."""
import json, math
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PROJ = '/home/hatch/workspace/fomo-trader'
ET = ZoneInfo('America/New_York')
J0 = '2026-09-28 16:25:16'  # first candidate-journal row (UTC); replay window starts here

def chain_of(r):
    ch = r.get('chain')
    return ch if ch else ('bsc' if str(r.get('mint','')).startswith('0x') else 'solana')

def pnl_usd(c):
    if c.get('realized_usd') is not None: return float(c['realized_usd'])
    nat = c.get('realized_sol')
    if nat is None: nat = c.get('realized_bnb')
    if nat is None: return None
    return float(nat)*float(c.get('sol_usd') or 115)

def etd(r):
    te = r.get('ts_epoch')
    if te:
        try: return datetime.fromtimestamp(float(te), tz=ET).strftime('%Y-%m-%d')
        except Exception: pass
    try: return datetime.strptime(str(r.get('ts'))[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).astimezone(ET).strftime('%Y-%m-%d')
    except Exception: return '?'

def S(v, label=''):
    n = len(v)
    if n == 0: return f'{label}: n=0'
    gp = sum(x for x in v if x > 0); gl = -sum(x for x in v if x < 0)
    pf = gp/gl if gl > 0 else float('inf')
    wr = sum(1 for x in v if x > 0)/n
    return f'{label}: n={n} net=${sum(v):+.2f} PF={pf:.4f} WR={wr:.3f} avg=${sum(v)/n:+.2f}'

rows = [json.loads(l) for l in open(f'{PROJ}/runs/paper-1h/trades.jsonl') if l.strip()]
# FIFO pairs with entry refs
openq = defaultdict(list); pairs = []
for r in rows:
    key = (chain_of(r), r.get('mint'))
    if r.get('type') == 'entry': openq[key].append(r)
    elif r.get('type') == 'close' and openq[key]:
        e = openq[key].pop(0); v = pnl_usd(r)
        if v is not None: pairs.append((e, r, v))

print('=== A. gain-gradient mechanism (entered trades, canonical P&L) ===')
gains = []
for e, c, v in pairs:
    g = e.get('signal_gain_pct')
    if g is not None: gains.append((float(g), v))
gains.sort()
n = len(gains); q = n//4
for i, name in enumerate(['Q1 lowest-gain','Q2','Q3','Q4 highest-gain']):
    seg = gains[i*q:(i+1)*q] if i < 3 else gains[3*q:]
    print(S([v for _, v in seg], name))
# deciles for the tail
print('--- deciles ---')
d = n//10
for i in range(10):
    seg = gains[i*d:(i+1)*d] if i < 9 else gains[9*d:]
    lo = seg[0][0]; hi = seg[-1][0]
    print(S([v for _, v in seg], f'D{i+1} [{lo:.0f},{hi:.0f}]'))

print('\n=== B. day-regime cut (canonical, closes in file order) ===')
byday = defaultdict(list)
for e, c, v in pairs: byday[etd(c)].append(v)
for dd in sorted(byday): print(S(byday[dd], dd))

print('\n=== C. hour-of-day (ET) entry cut ===')
byh = defaultdict(list)
for e, c, v in pairs:
    try:
        h = datetime.strptime(str(e.get('ts'))[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).astimezone(ET).hour
    except Exception: h = -1
    byh[h].append(v)
for h in sorted(byh):
    if len(byh[h]) >= 5: print(S(byh[h], f'h={h:02d}'))
