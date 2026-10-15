#!/usr/bin/env python3
"""iter12_lane_validation.py — LANE-VALIDATION iteration 12 (independent gatekeeper).
Read-only vs bot state. Never touches trades.jsonl / state.json / bot.log / deaths.log /
keypair.json / wrapper.log. No CLI agents, no subagents. File order only (never sort by ts;
mixed UTC/EDT naive ts). P&L = bot-recorded realized_usd/realized_sol on raw close rows
(headlines); costed FIFO via iter-10 validated pricing for gating."""
import json, math
from collections import defaultdict, deque

PROJ = '/home/hatch/workspace/fomo-trader'

def load(path):
    return [json.loads(l) for l in open(path) if l.strip()]

trades = load(f'{PROJ}/runs/paper-1h/trades.jsonl')
closes = [t for t in trades if t.get('type') == 'close']
entries = [t for t in trades if t.get('type') == 'entry']

def infer_chain(row):
    ch = row.get('chain')
    if ch: return ch
    return 'bsc' if str(row.get('mint', '')).startswith('0x') else 'solana'

def raw_usd(e, c):
    if c.get('realized_usd') is not None: return float(c['realized_usd'])
    nat = c.get('realized_sol')
    if nat is None: nat = c.get('realized_bnb')
    if nat is None: return None
    rate = (c.get('sol_usd') or (e or {}).get('sol_usd')
            or (115 if infer_chain(c) == 'solana' else 600))
    return float(nat) * float(rate)

def costed_usd(e, c, slippage=0.0015):
    r = raw_usd(e, c)
    if r is None or not (e and e.get('entry')) or not c.get('exit'): return None
    chain = infer_chain(c)
    rate = c.get('sol_usd') or e.get('sol_usd') or (115 if chain == 'solana' else 600)
    stake = float(e.get('buy_sol') or 0)
    fixed = 0.004 if chain == 'solana' else 0.00004
    ratio = float(c['exit']) / float(e['entry'])
    return r - float(rate) * (stake * (1 + ratio) * (0.0025 + slippage) + fixed)

def stats_of(vals):
    n = len(vals)
    if n == 0: return dict(n=0, net=0.0, pf=float('nan'), wr=float('nan'), avg=float('nan'), gw=0.0, gl=0.0)
    gw = sum(v for v in vals if v > 0); gl = -sum(v for v in vals if v < 0)
    return dict(n=n, net=sum(vals), pf=(gw / gl if gl > 0 else float('inf')),
                wr=sum(1 for v in vals if v > 0) / n, avg=sum(vals) / n, gw=gw, gl=gl)

def max_dd(vals):
    peak = cum = mdd = 0.0
    for v in vals:
        cum += v; peak = max(peak, cum); mdd = max(mdd, peak - cum)
    return mdd

def show(label, vals, cur='USD'):
    s = stats_of(vals)
    u = '$' if cur == 'USD' else cur + ' '
    print(f'{label}: n={s["n"]} net={u}{s["net"]:.4f} PF={s["pf"]:.4f} WR={s["wr"]:.4f} avg={u}{s["avg"]:.4f} maxDD={u}{max_dd(vals):.2f} (W={u}{s["gw"]:.2f} L={u}{s["gl"]:.2f})')
    return s

def reason_bucket(c):
    r = str(c.get('reason', ''))
    if 'trailing stop' in r: return 'trailing'
    if r.startswith('venue dump'): return 'venue-dump'
    if 'dump detector' in r: return 'dump'
    if 'stale exit' in r: return 'stale'
    if 'take profit' in r: return 'take-profit'
    return 'other'

# FIFO pairs (per-chain+mint queues, close append order)
openq, pairs = {}, []
for t in trades:
    key = ((t.get('chain') or ('bsc' if str(t.get('mint', '')).startswith('0x') else 'solana')), t.get('mint'))
    if t.get('type') == 'entry': openq.setdefault(key, deque()).append(t)
    elif t.get('type') == 'close' and openq.get(key): pairs.append((openq[key].popleft(), t))
print(f'rows={len(trades)} closes={len(closes)} entries={len(entries)} FIFO-pairs={len(pairs)}')

pinned = closes[:215]
usd = [c for c in closes if c.get('realized_usd') is not None]
leg = [c for c in closes if c.get('realized_usd') is None]

print('\n=== 1a. PINNED universe (first 215 file-order closes) ===')
pusd = [c for c in pinned if c.get('realized_usd') is not None]
show('pinned USD-stamped', [float(c['realized_usd']) for c in pusd])
pbsc = [c for c in pusd if infer_chain(c) == 'bsc']; psolusd = [c for c in pusd if infer_chain(c) == 'solana']
show('pinned BSC', [float(c['realized_usd']) for c in pbsc])
show('pinned SOL-usd', [float(c['realized_usd']) for c in psolusd])
pleg = [c for c in pinned if c.get('realized_usd') is None]
show('pinned legacy SOL (raw SOL)', [float(c.get('realized_sol') or 0) for c in pleg], cur='SOL')
cur9 = [c for c in closes[:247] if c.get('realized_usd') is not None]
show('iter-9-lifetime cut (closes 1-247, USD)', [float(c['realized_usd']) for c in cur9])

print('\n=== 1b. FULL LIFETIME (all closes) ===')
show('USD-stamped all', [float(c['realized_usd']) for c in usd])
ubsc = [c for c in usd if infer_chain(c) == 'bsc']; usol = [c for c in usd if infer_chain(c) == 'solana']
show('USD BSC', [float(c['realized_usd']) for c in ubsc])
show('USD SOL-usd', [float(c['realized_usd']) for c in usol])
show('legacy SOL (raw SOL)', [float(c.get('realized_sol') or 0) for c in leg], cur='SOL')
costed = [v for v in (costed_usd(e, c) for e, c in pairs) if v is not None]
show('COSTED FIFO base-fee', costed)
show('COSTED FIFO 3x-fee (slip=0.0045)', [v for v in (costed_usd(e, c, 0.0045) for e, c in pairs) if v is not None])

print('\n=== 1c. Exit-reason families (USD closes) ===')
groups = defaultdict(list)
for c in usd: groups[reason_bucket(c)].append(float(c['realized_usd']))
for k in sorted(groups): show(f'  exit={k}', groups[k])
print('--- all closes ---')
groups2 = defaultdict(list)
for c in closes: groups2[reason_bucket(c)].append(raw_usd(None, c))
for k in sorted(groups2): show(f'  exit={k}', [v for v in groups2[k] if v is not None])

print('\n=== 1d. Strict price-based wipes (exit/entry <= 0.05) on USD closes ===')
wipes = [c for c in usd if c.get('entry') and c.get('exit') and float(c['exit'])/float(c['entry']) <= 0.05]
show('wipes', [float(c['realized_usd']) for c in wipes])
print('share of USD net =', sum(float(c['realized_usd']) for c in wipes)/sum(float(c['realized_usd']) for c in usd))

# ---- F2 pool-age: candidate-event pool_created_at lookup, no look-ahead ----
cand = load(f'{PROJ}/runs/paper-1h/candidates.jsonl')
per_mint = defaultdict(list)
for d in cand:
    if d.get('event') in ('candidate', 'slip_veto') and d.get('mint') and d.get('pool_created_at'):
        per_mint[d['mint']].append((float(d['ts_epoch']), float(d['pool_created_at'])))
for m in per_mint: per_mint[m].sort()

def pool_age_at(entry):
    evs = per_mint.get(entry.get('mint'))
    if not evs: return None
    te = float(entry.get('ts_epoch') or 0)
    best = None
    for tse, pca in evs:
        if tse <= te: best = (tse, pca)
        else: break
    return (te - best[1]) if best else None

comp = []  # (age, raw, costed, close-file-idx, pair)
for e, c in pairs:
    age = pool_age_at(e)
    r = raw_usd(e, c); co = costed_usd(e, c)
    if age is None or r is None: continue
    comp.append(dict(age=age, raw=r, costed=co, ci=closes.index(c)))
print(f'\n=== F2: computable pool-age pairs = {len(comp)} ===')
young = [p for p in comp if p['age'] <= 900]; old = [p for p in comp if p['age'] > 900]
show('young<=900s raw', [p['raw'] for p in young])
show('young<=900s costed', [p['costed'] for p in young if p['costed'] is not None])
show('old>900s raw', [p['raw'] for p in old])
ANOM_START = 247  # 0-based: closes #248+
ya = [p for p in young if p['ci'] >= ANOM_START]; ye = [p for p in young if p['ci'] < ANOM_START]
show('young IN-anomaly raw', [p['raw'] for p in ya])
show('young EX-anomaly raw', [p['raw'] for p in ye])
show('young EX-anomaly costed', [p['costed'] for p in ye if p['costed'] is not None])
# drop-top-2
ys = sorted((p['raw'] for p in young), reverse=True)
show('young drop-top-2 raw', ys[2:])
flat3x = lambda vs: [v - 0.30 for v in vs]
show('young flat-3xfee (-$0.30/tr)', flat3x([p['raw'] for p in young]))
show('young costed-3xfee', [p['costed'] for p in comp if False] or [costed_usd(p[0], p[1], 0.0045) for p in pairs if pool_age_at(p[0]) is not None and pool_age_at(p[0]) <= 900 and costed_usd(p[0], p[1], 0.0045) is not None])
# walk-forward on computable universe, file order
comp_sorted = sorted(comp, key=lambda p: p['ci'])
for f in (0.5, 0.6, 0.7):
    k = int(len(comp_sorted) * f); oos = comp_sorted[k:]
    allv = [p['raw'] for p in oos]; yv = [p['raw'] for p in oos if p['age'] <= 900]
    sa, sy = stats_of(allv), stats_of(yv)
    s3 = stats_of(flat3x(yv))
    print(f'  f={f}: OOS n={len(oos)} basePF={sa["pf"]:.3f} youngPF={sy["pf"]:.3f} dPF={sy["pf"]-sa["pf"]:+.3f} | 3xfee-keepyoung PF={s3["pf"]:.3f}')

print('\n=== Anomaly window (closes #248+, 0-based >=247) raw USD ===')
anom = closes[247:]
show('anomaly', [float(c['realized_usd']) for c in anom if c.get('realized_usd') is not None])
av = sorted((float(c['realized_usd']) for c in anom if c.get('realized_usd') is not None), reverse=True)
show('anomaly drop-top-3', av[3:])
new5 = closes[-5:]
show('newest-5 closes USD', [float(c['realized_usd']) for c in new5 if c.get('realized_usd') is not None])

print('\n=== LP-lock ===')
meas = [(e, c) for e, c in pairs if e.get('lp_lock_verdict') is not None]
unl = [raw_usd(e, c) for e, c in meas if e.get('lp_lock_verdict') == 'unlocked']
brn = [raw_usd(e, c) for e, c in meas if e.get('lp_lock_verdict') != 'unlocked']
print(f'measured outcomes={len(meas)} (unlocked={len(unl)} burned/other={len(brn)}) verdicts=', {e.get('lp_lock_verdict') for e, c in meas})
show('unlocked raw', [v for v in unl if v is not None])
show('burned/other raw', [v for v in brn if v is not None])

print('\n=== slip_veto ===')
sv = [d for d in cand if d.get('event') == 'slip_veto']
print('slip_veto events =', len(sv))
post = usd[169:]  # post-pinned window
show('post-pinned window (USD idx169+)', [float(c['realized_usd']) for c in post])
pwipes = [c for c in post if c.get('entry') and c.get('exit') and float(c['exit'])/float(c['entry']) <= 0.05]
print('strict wipes in post-pinned window =', len(pwipes))

print('\n=== R11 signal-gain quartiles (paired, raw) ===')
qg = []
for e, c in pairs:
    g = e.get('signal_gain_pct')
    r = raw_usd(e, c)
    if g is None or r is None: continue
    qg.append((float(g), r))
qg.sort(key=lambda x: x[0]); n = len(qg)
for i, nm in enumerate(('Q1','Q2','Q3','Q4')):
    seg = [v for _, v in qg[i*n//4:(i+1)*n//4]]
    show(f'  {nm}', seg)

print('\n=== drop-top-2 / top-3 robustness on full USD book ===')
allv = sorted((float(c['realized_usd']) for c in usd), reverse=True)
show('USD drop-top-2', allv[2:]); show('USD drop-top-3', allv[3:])
