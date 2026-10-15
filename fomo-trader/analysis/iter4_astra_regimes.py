#!/usr/bin/env python3
"""Read-only lifetime regime diagnosis; outputs restricted to iter-4."""
import json, math, hashlib, statistics
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path
from iter1_trackb_screens import load_pairs, stats, costed_usd, infer_chain
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'hidden_files/deep-improvement-loop/iter-4'
def epoch(s): return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
obs=defaultdict(list)
for line in open(ROOT/'runs/paper-1h/candidates.jsonl'):
 r=json.loads(line)
 if r.get('event')=='candidate': obs[(r.get('chain'),r.get('mint'))].append(r)
def stamp(e,c):
 wall=epoch(e['ts']); near=sorted(obs[(infer_chain(e,c),e['mint'])],key=lambda r:abs(epoch(r['ts'])-wall))
 if near and abs(epoch(near[0]['ts'])-wall)<=1800:
  offset=round((near[0]['ts_epoch']-epoch(near[0]['ts']))/3600)*3600
  return wall+offset,'candidate-offset'
 # Only observed non-UTC process interval; other timestamps UTC per prior forensic audit.
 if '2026-09-28 19:51:29'<=e['ts']<'2026-09-29 00:00:00': return wall+14400,'EDT-interval'
 return wall,'historical-UTC'
def family(c):
 for s in ['dump detector','venue dump','trailing stop','stale exit','take profit']:
  if c['reason'].startswith(s): return s
 return 'other/manual'
pairs=load_pairs()[:206] # freeze requested snapshot by close append order
rows=[]
for i,(e,c) in enumerate(pairs):
 t,method=stamp(e,c); dt=datetime.fromtimestamp(t,timezone.utc); age=e.get('pool_created_at')
 rows.append(dict(i=i,pair=(e,c),chain=infer_chain(e,c),window=e.get('window'),source=e.get('source'),day=str(dt.date()),hour=f'{dt.hour:02}',hour6=f'{dt.hour//6*6:02}–{dt.hour//6*6+5:02}',epoch=t,clock=method,age_min=(t-age)/60 if age else None,hold_min=(epoch(c['ts'])-epoch(e['ts']))/60,exit=family(c),pnl=costed_usd(e,c)))
def summarize(rr):
 pp=[r['pair'] for r in rr];s=stats(pp)
 if not rr:return s
 s.update(wipes=sum(r['pnl']<=-3.5 for r in rr),stress_pf=stats(pp,.0045)['pf'],raw_n=len(rr),median_gain=statistics.median(r['pair'][0]['signal_gain_pct'] for r in rr),chains=dict(Counter(r['chain'] for r in rr)))
 return s
sections={};members={}
def group(title,func,rr=rows):
 g=defaultdict(list)
 for r in rr:g[str(func(r))].append(r)
 sections[title]={k:summarize(v) for k,v in sorted(g.items())};members[title]=g
for f in ['chain','window','source','day','hour','hour6','exit','clock']:group(f,lambda r,f=f:r[f])
group('day × chain',lambda r:r['day']+' '+r['chain'])
group('chain × window',lambda r:r['chain']+' '+r['window'])
def quint(title,fn,rr=rows):
 vals=sorted(fn(r) for r in rr if fn(r) is not None); cuts=[vals[math.ceil(len(vals)*k/5)-1] for k in range(1,5)] if vals else []
 def label(r):
  x=fn(r)
  if x is None:return 'missing'
  q=sum(x>v for v in cuts);lo=min(vals) if q==0 else cuts[q-1];hi=max(vals) if q==4 else cuts[q]
  return f'Q{q+1} {lo:g}..{hi:g}'
 group(title,label,rr)
for field in ['signal_gain_pct','liquidity_usd','mcap_usd','m15_buys','entry_latency_ms','slip_from_signal_pct']:
 quint(field,lambda r,f=field:r['pair'][0].get(f))
for chain in ['bsc','solana']:quint(chain+' gain',lambda r:r['pair'][0]['signal_gain_pct'],[r for r in rows if r['chain']==chain])
group('age natural',lambda r:'missing' if r['age_min'] is None else '<0 invalid' if r['age_min']<0 else '<5m' if r['age_min']<5 else '5–15m' if r['age_min']<15 else '15–60m' if r['age_min']<60 else '1–24h' if r['age_min']<1440 else '>=24h')
quint('age quintiles',lambda r:r['age_min'])
group('hold natural (outcome)',lambda r:'invalid negative clock' if r['hold_min']<0 else '<1m' if r['hold_min']<1 else '1–5m' if r['hold_min']<5 else '5–15m' if r['hold_min']<15 else '15–60m' if r['hold_min']<60 else '>=60m')
group('gain fixed bands',lambda r:'<60' if r['pair'][0]['signal_gain_pct']<60 else '60–100' if r['pair'][0]['signal_gain_pct']<100 else '100–200' if r['pair'][0]['signal_gain_pct']<200 else '>=200')
group('slip sign',lambda r:'missing' if r['pair'][0].get('slip_from_signal_pct') is None else 'positive' if r['pair'][0]['slip_from_signal_pct']>0 else 'nonpositive')
cut=epoch('2026-09-28 16:24:00')
group('veto regime',lambda r:'pre-veto' if r['epoch']<cut else 'post-veto')
group('append cohort',lambda r:'history first 189' if r['i']<189 else 'newest 17')
for title,rr in [('newest 17',rows[189:]),('post-veto',[r for r in rows if r['epoch']>=cut]),('BSC since Sep27',[r for r in rows if r['chain']=='bsc' and r['day']>='2026-09-27'])]:
 for field in ['window','exit','day']:group(title+' '+field,lambda r,f=field:r[f],rr)
 quint(title+' gain',lambda r:r['pair'][0]['signal_gain_pct'],rr)
 group(title+' fixed gain',lambda r:'<60' if r['pair'][0]['signal_gain_pct']<60 else '60–100' if r['pair'][0]['signal_gain_pct']<100 else '100–200' if r['pair'][0]['signal_gain_pct']<200 else '>=200',rr)
extra={}
for name,rr in [('all',rows),('history',rows[:189]),('new17',rows[189:]),('pre-veto',[r for r in rows if r['epoch']<cut]),('post-veto',[r for r in rows if r['epoch']>=cut])]:
 extra[name]=dict(summary=summarize(rr),wipes_net=sum(r['pnl'] for r in rr if r['pnl']<=-3.5),nonwipes=summarize([r for r in rr if r['pnl']>-3.5]),rung_trades=sum(bool(r['pair'][1]['rungs']) for r in rr),no_peak=sum(r['pair'][1]['peak']<=r['pair'][0]['entry'] for r in rr),positive_slip=sum((r['pair'][0].get('slip_from_signal_pct') or 0)>0 for r in rr),median_hold=statistics.median(r['hold_min'] for r in rr),min_utc=datetime.fromtimestamp(min(r['epoch'] for r in rr),timezone.utc).isoformat(),max_utc=datetime.fromtimestamp(max(r['epoch'] for r in rr),timezone.utc).isoformat())
positive=[(title,k,s) for title,g in sections.items() for k,s in g.items() if s['n']>=30 and s['exp']>0]
coverage={f:sum(e.get(f) is not None for e,c in pairs) for f in ['pool_created_at','mcap_usd','m15_buys','entry_latency_ms','holder_top1_pct','holder_top5_pct','lp_burn_pct','lp_locked']}
OUT.mkdir(parents=True,exist_ok=True)
data=dict(baseline=summarize(rows),coverage=coverage,extra=extra,sections=sections,positive_n30=positive,pair_sha256=hashlib.sha256(json.dumps(pairs,sort_keys=True).encode()).hexdigest())
(OUT/'astra_regime_results.json').write_text(json.dumps(data,indent=2))
lines=[]
for title,g in sections.items():
 lines+=['\n### '+title,'| Bucket | n | Net $ | PF | WR % | Exp $ | Wipes | 3x PF |','|---|---:|---:|---:|---:|---:|---:|---:|']
 for k,s in g.items():lines.append(f"| {k} | {s['n']} | {s['net']:.2f} | {s['pf']} | {s['wr']*100:.1f} | {s['exp']:.4f} | {s['wipes']} | {s['stress_pf']} |")
(OUT/'astra_regime_tables.md').write_text('\n'.join(lines)+'\n')
print(json.dumps(dict(extra=extra,coverage=coverage,positive_n30=positive),indent=2))
