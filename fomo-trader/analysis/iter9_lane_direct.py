#!/usr/bin/env python3
"""iter9_lane_direct.py — LANE-CODEX-ASTRA iteration 9 (lane-direct: Codex quota-walled).
Read-only vs bot state. Never touches trades.jsonl / state.json / bot.log / deaths.log.
Reuse of frozen iter-8 methods + new work:
 1. Baseline repro (frozen method), reconcile vs iter-8 (236 closes, -$240.64, PF 0.3222, WR 29.8%)
 2. Fresh cohort since iter-8 book
 3. Gain decile gradient on full book
 4. NEW instrumented entry features (holder_top1/top5, slip_from_signal_pct, lp_burn/lock,
    entry_latency_ms, mcap) joined to outcomes -> wipe/feature cuts
 5. Trailing-gap-bleed forensics (trailing closes far beyond the 25% trail)
 6. R10 reject counts, R11/R12 frozen-protocol status, slip-veto forward update
"""
import json, math
from collections import defaultdict, deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PROJ = '/home/hatch/workspace/fomo-trader'
ET = ZoneInfo('America/New_York')

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
    return pairs

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
    eq=0; peak=0; dd=0
    for v in vals:
        eq+=v; peak=max(peak,eq); dd=max(dd,peak-eq)
    return dict(n=n, net=sum(vals), pf=(gp/gl if gl>0 else float('inf')),
                wr=wins/n, avg=sum(vals)/n, dd=dd)

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
usd = [(e,c,raw_usd(e,c)) for e,c in pairs]
usd = [(e,c,v) for e,c,v in usd if v is not None]

print('=== 1. BASELINE (frozen method) ===')
vals=[v for _,_,v in usd]
print('closes:', len(usd), 'entries-paired:', len(pairs))
s=stats_of(vals)
print('net=%.2f PF=%.4f WR=%.3f maxDD=%.2f' % (s['net'], s['pf'], s['wr'], s['dd']))

# frozen 215-book check: first 215 file-order closes
s215=stats_of([v for _,_,v in usd[:215]])
print('first-215 closes: net=%.2f PF=%.4f WR=%.3f (iter-8 expected: -175.52 / 0.2747 / 0.266)' % (s215['net'], s215['pf'], s215['wr']))

print()
print('=== 2. FRESH COHORT (closes after iter-8 book of 236) ===')
fresh=[v for _,_,v in usd[236:]]
sf=stats_of(fresh)
print('n=%d net=%.2f PF=%.4f WR=%.3f' % (sf['n'], sf['net'], sf['pf'], sf['wr']))
if fresh:
    for i,(e,c,v) in enumerate(usd[236:], start=237):
        print('  close#%d %s %s %s pnl=%.2f' % (i, et_day_of(c), (c.get('name') or '?')[:22], (c.get('reason') or '?')[:42], v))

print()
print('=== 3. GAIN DECILE GRADIENT (full book, signal_gain_pct from entry) ===')
ge=[(e.get('signal_gain_pct'), v) for e,c,v in usd if e.get('signal_gain_pct') is not None]
ge.sort(key=lambda x: x[0])
n=len(ge); d=n//10
for i in range(10):
    seg=ge[i*d:(i+1)*d] if i<9 else ge[i*d:]
    if not seg: continue
    sv=stats_of([v for _,v in seg])
    print('D%d gain[%.0f,%.0f] n=%d net=%+.2f PF=%.3f WR=%.2f' % (i+1, seg[0][0], seg[-1][0], len(seg), sv['net'], sv['pf'], sv['wr']))

print()
print('=== 4. NEW INSTRUMENTED FEATURES coverage on entries with outcomes ===')
feats=['slip_from_signal_pct','holder_top1_pct','holder_top5_pct','lp_burn_pct','lp_locked','entry_latency_ms','mcap_usd','liquidity_usd','signal_ratio']
for f in feats:
    cov=sum(1 for e,c,v in usd if e.get(f) is not None)
    print('  %-22s covered: %d/%d' % (f, cov, len(usd)))

def num(x):
    try:
        f=float(x); return f if math.isfinite(f) else None
    except Exception: return None

print()
print('=== 5. WIPE vs SURVIVOR feature cuts (wipe = pnl <= -3.50) ===')
for f in ['slip_from_signal_pct','holder_top1_pct','holder_top5_pct','lp_burn_pct','entry_latency_ms','mcap_usd','liquidity_usd','signal_ratio']:
    rows=[(num(e.get(f)), v) for e,c,v in usd]
    rows=[(x,v) for x,v in rows if x is not None]
    if len(rows)<10: print('  %-22s n=%d (skip)' % (f, len(rows))); continue
    wipes=[x for x,v in rows if v<=-3.50]; surv=[x for x,v in rows if v>-3.50]
    def med(a):
        a=sorted(a); return a[len(a)//2] if a else float('nan')
    print('  %-22s n=%d wipes=%d med_wipe=%.4g med_surv=%.4g' % (f, len(rows), len(wipes), med(wipes), med(surv)))

print()
print('=== 6. HOLDER CONCENTRATION split (if n>=10) ===')
rows=[(num(e.get('holder_top1_pct')), v, e.get('name')) for e,c,v in usd]
rows=[r for r in rows if r[0] is not None]
if len(rows)>=10:
    rows.sort(key=lambda x: x[0]); n=len(rows)
    lo=rows[:n//2]; hi=rows[n//2:]
    for tag,seg in (('top1 LOW half',lo),('top1 HIGH half',hi)):
        sv=stats_of([v for _,v,_ in seg])
        print('  %-14s n=%d net=%+.2f PF=%.3f WR=%.2f wipes=%d' % (tag, len(seg), sv['net'], sv['pf'], sv['wr'], sum(1 for _,v,_ in seg if v<=-3.50)))
    print('  top1 range: [%.2f, %.2f]' % (rows[0][0], rows[-1][0]))
else:
    print('  n=%d too small' % len(rows))

print()
print('=== 7. ENTRY SLIP direction cut ===')
rows=[(num(e.get('slip_from_signal_pct')), v) for e,c,v in usd]
rows=[r for r in rows if r[0] is not None]
print('  entries with slip:', len(rows))
if rows:
    neg=[v for x,v in rows if x<0]; pos=[v for x,v in rows if x>0]; z=[v for x,v in rows if x==0]
    for tag,seg in (('slip<0 dip-buy',neg),('slip==0',z),('slip>0 chase',pos)):
        if seg:
            sv=stats_of(seg)
            print('  %-15s n=%3d net=%+.2f PF=%.3f WR=%.2f avg=%+.3f' % (tag, len(seg), sv['net'], sv['pf'], sv['wr'], sv['avg']))

print()
print('=== 8. TRAILING cohort forensics ===')
import re
trail=[(e,c,v) for e,c,v in usd if str(c.get('reason','')).startswith('trailing stop')]
print('  trailing closes:', len(trail))
xs=[]
for e,c,v in trail:
    m=re.search(r'trailing stop -([\d.]+)%', str(c.get('reason')))
    if m: xs.append((float(m.group(1)), v))
xs.sort()
far=[x for x in xs if x[0]>40]
print('  dd_peak>40%% (gap-bleeds): n=%d of %d' % (len(far), len(xs)))
svf=stats_of([v for _,v in far]); svn=stats_of([v for x,v in xs if x<=40])
print('  gap-bleed  : net=%+.2f PF=%.3f' % (svf['net'] if svf['n'] else 0, svf['pf'] if svf['n'] else 0))
print('  near-trail : net=%+.2f PF=%.3f n=%d' % (svn['net'], svn['pf'], svn['n']))
print('  gap-bleed examples:', [(round(x,1), round(v,2)) for x,v in far[:8]])

print()
print('=== 9. EXIT REASON totals ===')
def rcat(r):
    r=str(r)
    if r.startswith('dump detector') or r.startswith('venue dump') or r=='trailing stop -100.0% from peak': return 'dump/wipe'
    if r.startswith('trailing stop'): return 'trailing'
    if r.startswith('stale'): return 'stale'
    if r.startswith('take profit'): return 'tp'
    if r.startswith('hard stop'): return 'hard'
    return 'other:'+r[:30]
cats=defaultdict(list)
for e,c,v in usd: cats[rcat(c.get('reason'))].append(v)
for k in sorted(cats):
    sv=stats_of(cats[k]); print('  %-28s n=%3d net=%+.2f PF=%.3f' % (k, sv['n'], sv['net'], sv['pf']))

print()
print('=== 10. JOURNAL status ===')
import collections
ev=collections.Counter()
with open(f'{PROJ}/runs/paper-1h/candidates.jsonl') as f:
    for line in f:
        try: r=json.loads(line)
        except: continue
        ev[r.get('event')]+=1
print('  candidates.jsonl events:', dict(ev))
