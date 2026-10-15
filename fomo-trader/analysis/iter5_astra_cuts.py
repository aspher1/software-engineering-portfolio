#!/usr/bin/env python3
"""Read-only iteration-5 diagnostic. Prints results; writes no runtime files."""
import json, math, hashlib, statistics
from pathlib import Path
from collections import defaultdict, deque, Counter
from datetime import datetime, timezone
from iter1_trackb_screens import infer_chain, raw_usd, costed_usd
ROOT=Path(__file__).resolve().parents[1]
def epoch(s): return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
blob=(ROOT/'runs/paper-1h/trades.jsonl').read_bytes()
trades=[json.loads(l) for l in blob.splitlines() if l.strip()]
journal=[json.loads(l) for l in (ROOT/'runs/paper-1h/candidates.jsonl').read_bytes().splitlines() if l.strip()]
obs=defaultdict(list); veto=defaultdict(list)
for j in journal:
 key=(j.get('chain'),j.get('mint'))
 if j.get('event')=='candidate': obs[key].append(j)
 if j.get('event')=='slip_veto': veto[key].append(j['ts_epoch'])
def stamp(e,c):
 wall=epoch(e['ts']); near=sorted(obs[(infer_chain(e,c),e['mint'])],key=lambda r:abs(epoch(r['ts'])-wall))
 if near and abs(epoch(near[0]['ts'])-wall)<=1800:
  return wall+round((near[0]['ts_epoch']-epoch(near[0]['ts']))/3600)*3600
 if '2026-09-28 19:51:29'<=e['ts']<'2026-09-29 00:00:00':return wall+14400
 return wall
q=defaultdict(deque); seen=Counter(); rows=[]; unmatched=[]
for i,t in enumerate(trades):
 key=(t.get('chain'),t.get('mint'))
 if t.get('type')=='entry':
  seen[t['mint']]+=1;q[key].append((t,seen[t['mint']],i))
 elif t.get('type')=='close':
  if not q[key]: unmatched.append(t);continue
  e,number,idx=q[key].popleft();ts=stamp(e,t)
  rows.append(dict(e=e,c=t,number=number,idx=idx,ts=ts,pnl=raw_usd(e,t),cost=costed_usd(e,t),chain=infer_chain(e,t),readmit=any(v<ts for v in veto[key])))
bsc=[r for r in rows if r['chain']=='bsc']; post=[r for r in bsc if r['ts']>=epoch('2026-09-28 16:34:00')]
def stat(rr,field='pnl'):
 v=[r[field] for r in rr if r[field] is not None]; n=len(v);w=sum(x for x in v if x>0);l=-sum(x for x in v if x<0)
 return dict(n=n,net=round(sum(v),4),pf=round(w/l,4) if l else 'inf' if w else None,wr=round(100*sum(x>0 for x in v)/n,2) if n else None,exp=round(sum(v)/n,4) if n else None)
sections={}
def group(name,rr,fn,labels=()):
 d={k:[] for k in labels}
 for r in rr:d.setdefault(fn(r),[]).append(r)
 sections[name]={k:stat(v) for k,v in d.items()}
 return d
def slip(r):
 x=r['e'].get('slip_from_signal_pct')
 return 'missing' if x is None else 'negative' if x<0 else 'zero' if x==0 else 'positive'
def band(r):
 x=r['e'].get('slip_from_signal_pct')
 return 'missing' if x is None else '<=-0.5%' if x<=-.005 else '(-0.5%,0]' if x<=0 else '(0,0.25%]' if x<=.0025 else '(0.25%,1%]' if x<=.01 else '>1%'
group('slip micro-bands',bsc,band,['<=-0.5%','(-0.5%,0]','(0,0.25%]','(0.25%,1%]','>1%','missing'])
group('slip sign',bsc,slip)
group('first/re-entry',bsc,lambda r:'first' if r['number']==1 else 're-entry')
group('first/re-entry x era',bsc,lambda r:('post' if r in post else 'pre')+' / '+('first' if r['number']==1 else 're-entry'))
group('source',bsc,lambda r:r['e'].get('source') or 'other',['geckoterminal','dexscreener','other'])
group('source x window',bsc,lambda r:(r['e'].get('source') or 'other')+' / '+str(r['e'].get('window')),[s+' / '+w for s in ['geckoterminal','dexscreener','other'] for w in ['15m','5m']])
group('post veto',post,lambda r:'re-admission' if r['readmit'] else 'fresh')
group('post veto x window',post,lambda r:('re-admission' if r['readmit'] else 'fresh')+' / '+r['e']['window'])
group('post veto x gain',post,lambda r:('re-admission' if r['readmit'] else 'fresh')+' / '+('<100%' if r['e']['signal_gain_pct']<100 else '>=100%'))
vals=sorted(r['e']['entry_latency_ms'] for r in bsc if r['e'].get('entry_latency_ms') is not None)
cuts=[vals[math.ceil(len(vals)*k/5)-1] for k in range(1,5)]
def quint(r):
 x=r['e'].get('entry_latency_ms');return 'missing' if x is None else 'Q'+str(1+sum(x>v for v in cuts))
group('latency',bsc,quint)
group('latency x slip',bsc,lambda r:quint(r)+' / '+slip(r),[q+' / '+s for q in ['Q1','Q2','Q3','Q4','Q5','missing'] for s in ['negative','zero','positive','missing']])
group('hour UTC BSC',bsc,lambda r:f'{datetime.fromtimestamp(r["ts"],timezone.utc).hour:02}')
group('six-hour UTC BSC',bsc,lambda r:str(datetime.fromtimestamp(r['ts'],timezone.utc).hour//6*6))
group('hour UTC all pairs costed',[dict(r,pnl=r['cost']) for r in rows],lambda r:f'{datetime.fromtimestamp(r["ts"],timezone.utc).hour:02}')
group('post veto outcomes',post,lambda r:('re-admission' if r['readmit'] else 'fresh')+' / '+('win' if r['pnl']>0 else 'loss'))
features={}
for k,rr in [('readmit',[r for r in post if r['readmit']]),('fresh',[r for r in post if not r['readmit']]),('readmit losers',[r for r in post if r['readmit'] and r['pnl']<0]),('readmit winners',[r for r in post if r['readmit'] and r['pnl']>0]),('fresh losers',[r for r in post if not r['readmit'] and r['pnl']<0])]:
 features[k]={'n':len(rr),'wipes':sum(r['pnl']<=-3.5 for r in rr),'wipe_net':sum(r['pnl'] for r in rr if r['pnl']<=-3.5),'repeat_entries':sum(r['number']>1 for r in rr)}
 for f in ['signal_gain_pct','liquidity_usd','entry_latency_ms','slip_from_signal_pct','m15_buys','m15_sells']:
  vv=[r['e'][f] for r in rr if r['e'].get(f) is not None]; features[k][f]={'n':len(vv),'median':statistics.median(vv) if vv else None}
pilot=[dict(age_min=(r['ts']-r['e']['pool_created_at'])/60,pnl=r['pnl'],cost=r['cost'],ts=r['e']['ts']) for r in bsc if r['e'].get('pool_created_at')]
result=dict(sha256=hashlib.sha256(blob).hexdigest(),row_count=len(trades),close_count=sum(t.get('type')=='close' for t in trades),pairs=len(rows),unmatched_closes=len(unmatched),open_entries=sum(map(len,q.values())),baseline=stat(bsc),costed_bsc=stat(bsc,'cost'),costed_all=stat(rows,'cost'),post=stat(post),latency_cuts=cuts,latency_range=[min(vals),max(vals)],features=features,pilot=pilot,sections=sections)
print(json.dumps(result,indent=2))
