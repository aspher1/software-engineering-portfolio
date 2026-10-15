#!/usr/bin/env python3
"""iter8 R11 replay + regimes + R12 characterization. Read-only."""
import json
from collections import defaultdict, deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PROJ = '/home/hatch/workspace/fomo-trader'
ET = ZoneInfo('America/New_York')

def chain_of(r):
    ch = r.get('chain')
    if ch: return ch
    return 'bsc' if str(r.get('mint','')).startswith('0x') else 'solana'

def pnl_usd(c):
    if c.get('realized_usd') is not None: return float(c['realized_usd'])
    nat = c.get('realized_sol')
    if nat is None: nat = c.get('realized_bnb')
    if nat is None: return None
    rate = c.get('sol_usd') or 115
    return float(nat)*float(rate)

def et_day(ts_epoch, ts_str):
    try:
        if ts_epoch: return datetime.fromtimestamp(float(ts_epoch), tz=ET).strftime('%Y-%m-%d')
    except Exception: pass
    try:
        return datetime.strptime(str(ts_str)[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).astimezone(ET).strftime('%Y-%m-%d')
    except Exception: return '?'

rows=[json.loads(l) for l in open(f'{PROJ}/runs/paper-1h/trades.jsonl') if l.strip()]
closes=[r for r in rows if r.get('type')=='close']
entries=[r for r in rows if r.get('type')=='entry']

print('=== FULL-BOOK canonical baseline (bot recorded P&L) ===')
usd=[pnl_usd(c) for c in closes]; usd=[v for v in usd if v is not None]
gp=sum(v for v in usd if v>0); gl=-sum(v for v in usd if v<0)
print(f'closes={len(closes)} usd-known={len(usd)} net=${sum(usd):.2f} PF={gp/gl:.4f} WR={sum(1 for v in usd if v>0)/len(usd):.4f}')
sol=[c for c in closes if chain_of(c)!='bsc']
print(f'non-BSC closes={len(sol)} realized_sol sum={sum(float(c.get("realized_sol") or 0) for c in sol):.4f}')
# max DD on USD curve (file order)
cum=0; peak=0; mdd=0
for v in usd: cum+=v; peak=max(peak,cum); mdd=max(mdd,peak-cum)
print(f'maxDD=${mdd:.2f}')
by_exit=defaultdict(list)
for c in closes:
    r=str(c.get('reason','')); rl=r.lower()
    k='dump/wipe' if ('dump' in rl or 'venue' in rl or '-100' in r) else ('trailing' if 'trailing' in rl else ('take-profit' if ('take' in rl or 'rung' in rl) else ('stale' if 'stale' in rl else 'other')))
    v=pnl_usd(c)
    if v is not None: by_exit[k].append(v)
for k,v in sorted(by_exit.items(), key=lambda x:-len(x[1])):
    gp2=sum(x for x in v if x>0); gl2=-sum(x for x in v if x<0)
    print(f'  exit={k}: n={len(v)} net=${sum(v):.2f} PF={(gp2/gl2 if gl2>0 else float("inf")):.3f} WR={sum(1 for x in v if x>0)/len(v):.3f}')

# per-mint FIFO P&L lists (canonical close order)
mint_pnls=defaultdict(deque)
openq=defaultdict(deque)
for r in rows:
    key=(chain_of(r), r.get('mint'))
    if r.get('type')=='entry': openq[key].append(r)
    elif r.get('type')=='close' and openq[key]:
        openq[key].popleft()
        v=pnl_usd(r)
        if v is not None: mint_pnls[key].append(v)
print('mints with closes:', len(mint_pnls), 'total paired closes:', sum(len(d) for d in mint_pnls.values()))

# candidate pool: first-seen per (et_day, chain, mint) with enter verdict
pool=defaultdict(dict)  # day -> {(chain,mint): (m15_gain, window)}
nday=defaultdict(int)
for l in open(f'{PROJ}/runs/paper-1h/candidates.jsonl'):
    if not l.strip(): continue
    r=json.loads(l)
    if r.get('event')!='candidate' or r.get('guardrail_verdict')!='enter': continue
    d=et_day(r.get('ts_epoch'), r.get('ts'))
    key=(chain_of(r), r.get('mint'))
    g=r.get('m15_gain_pct')
    if g is None: continue
    if key not in pool[d]: pool[d][key]=(float(g), r.get('window'))
for e in entries:
    nday[et_day(e.get('ts_epoch'), e.get('ts'))]+=1
print('\n=== day pool vs entries ===')
for d in sorted(set(list(pool.keys())+list(nday.keys()))):
    print(f'{d}: pool={len(pool[d])} entries={nday[d]}')

# R11 replay
def select(daypool, n, asc):
    items=sorted(daypool.items(), key=lambda kv: kv[1][0], reverse=not asc)
    return [k for k,_ in items[:n]]

def sim_pnl(days_order, selector):
    used=defaultdict(int); vals=[]; matched=0; sel_total=0
    for d in days_order:
        n=nday[d]
        if n<=0 or d not in pool: continue
        for key in selector(pool[d], n):
            sel_total+=1
            lst=mint_pnls.get(key, [])
            if used[key] < len(lst):
                vals.append(lst[used[key]]); used[key]+=1; matched+=1
    return vals, matched, sel_total

days_sorted=sorted(d for d in nday if nday[d]>0)
actual_vals,_,_ = sim_actual(days_sorted) if False else (None,None,None)
# actual: entries in file order per day, FIFO
used=defaultdict(int); actual_by_day=defaultdict(list); actual_all=[]
for d in days_sorted:
    for e in [x for x in entries if et_day(x.get('ts_epoch'),x.get('ts'))==d]:
        key=(chain_of(e), e.get('mint')); lst=mint_pnls.get(key, [])
        if used[key]<len(lst):
            v=lst[used[key]]; used[key]+=1
            actual_by_day[d].append(v); actual_all.append(v)
desc_vals,desc_m,desc_t = sim_pnl(days_sorted, lambda p,n: select(p,n,False))
asc_vals,asc_m,asc_t = sim_pnl(days_sorted, lambda p,n: select(p,n,True))

def S(v):
    n=len(v)
    if n==0: return 'n=0'
    gp=sum(x for x in v if x>0); gl=-sum(x for x in v if x<0)
    return f'n={n} net=${sum(v):.2f} PF={(gp/gl if gl>0 else float("inf")):.4f} WR={sum(1 for x in v if x>0)/n:.4f} avg=${sum(v)/n:.2f}'
print('\n=== R11 replay (canonical P&L) ===')
print('ACTUAL entered :', S(actual_all))
print('DESC sim       :', S(desc_vals), f'(matched {desc_m}/{desc_t})')
print('ASC  sim (R11) :', S(asc_vals), f'(matched {asc_m}/{asc_t})')
# fidelity: overlap desc vs actual day sets
print('coverage note: sims only score mints with paired closes; unmatched selections excluded (see matched/total)')

# walk-forward: split CLOSES into IS/OOS 2/3-1/3 by file order; attribute day selections to windows by entry day
split=len(closes)*2//3
is_days=set(); oos_days=set()
# map each close index -> entry day: rebuild FIFO with day tags
openq2=defaultdict(deque); close_day=[]
for r in rows:
    key=(chain_of(r), r.get('mint'))
    if r.get('type')=='entry': openq2[key].append(et_day(r.get('ts_epoch'), r.get('ts')))
    elif r.get('type')=='close' and openq2[key]:
        close_day.append(openq2[key].popleft())
for i,d in enumerate(close_day):
    (is_days if i<split else oos_days).add(d)
print(f'\nwalk-forward split: IS closes={split} days={len(is_days)}, OOS closes={len(closes)-split} days={len(oos_days)}')
for name, dayset in [('IS',is_days),('OOS',oos_days)]:
    av=[v for d in dayset for v in actual_by_day[d]]
    dv,_,_=sim_pnl(sorted(dayset), lambda p,n: select(p,n,False))
    xv,_,_=sim_pnl(sorted(dayset), lambda p,n: select(p,n,True))
    print(f'{name} ACTUAL :', S(av))
    print(f'{name} DESC   :', S(dv))
    print(f'{name} ASC    :', S(xv))
    # 3x-fee stress: -$0.04/trade (validation-lane convention)
    for nm, vv in [('ACTUAL',av),('DESC',dv),('ASC',xv)]:
        sv=[x-0.04 for x in vv]; n=len(sv)
        if n:
            gp=sum(x for x in sv if x>0); gl=-sum(x for x in sv if x<0)
            print(f'   3x-fee {nm}: n={n} net=${sum(sv):.2f} PF={(gp/gl if gl>0 else float("inf")):.4f}')
