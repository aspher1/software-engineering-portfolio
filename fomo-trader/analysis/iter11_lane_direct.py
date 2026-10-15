"""iter-11 lane-codex-astra: direct-shell analysis. Read-only. FIFO pairing + pricing
mirrors analysis/iter10_lane_direct.py exactly (validated methodology)."""
import json, re, math
from collections import defaultdict, deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PROJ = '/home/hatch/workspace/fomo-trader'
ET = ZoneInfo('America/New_York')
ITER10_END_UTC = datetime(2026, 9, 30, 9, 0, 0, tzinfo=timezone.utc)  # iter-10 lane ~04:07-04:50 EDT

def load_trades():
    return [json.loads(l) for l in open(f'{PROJ}/runs/paper-1h/trades.jsonl') if l.strip()]

def infer_chain(e, c):
    ch = (c or {}).get('chain') or (e or {}).get('chain')
    if ch: return ch
    m = str(((c or {}).get('mint')) or ((e or {}).get('mint')) or '')
    return 'bsc' if m.startswith('0x') else 'solana'

def raw_usd(e, c):
    if c.get('realized_usd') is not None: return float(c['realized_usd'])
    nat = c.get('realized_sol')
    if nat is None: nat = c.get('realized_bnb')
    if nat is None: return None
    rate = c.get('sol_usd') or (e or {}).get('sol_usd')
    if rate is None: rate = 115 if infer_chain(e, c) == 'solana' else 600
    return float(nat) * float(rate)

def costed_usd(e, c, slippage=0.0015):
    r = raw_usd(e, c)
    if r is None or not (e and e.get('entry')) or not c.get('exit'): return None
    chain = infer_chain(e, c)
    rate = c.get('sol_usd') or e.get('sol_usd') or (115 if chain == 'solana' else 600)
    stake = float(e.get('buy_sol') or 0)
    fixed = 0.004 if chain == 'solana' else 0.00004
    ratio = float(c['exit']) / float(e['entry'])
    return r - float(rate) * (stake * (1 + ratio) * (0.0025 + slippage) + fixed)

def stats_of(vals):
    n = len(vals)
    if n == 0: return dict(n=0, net=0.0, pf=float('nan'), wr=float('nan'), avg=float('nan'))
    gp = sum(v for v in vals if v > 0); gl = -sum(v for v in vals if v < 0)
    return dict(n=n, net=sum(vals), pf=(gp / gl if gl > 0 else float('inf')),
                wr=sum(1 for v in vals if v > 0) / n, avg=sum(vals) / n)

def max_drawdown(vals):
    peak = cum = mdd = 0.0
    for v in vals:
        cum += v; peak = max(peak, cum); mdd = max(mdd, peak - cum)
    return mdd

def ts_utc(row):
    te = row.get('ts_epoch')
    if te: return datetime.fromtimestamp(float(te), tz=timezone.utc)
    s = str(row.get('ts') or '')
    try: return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except: return datetime.min.replace(tzinfo=timezone.utc)

def show(label, vals):
    s = stats_of(vals)
    print(f'{label}: n={s["n"]} net=${s["net"]:.2f} PF={s["pf"]:.4f} WR={s["wr"]:.3f} avg=${s["avg"]:.3f} maxDD=${max_drawdown(vals):.2f}')
    return s

trades = load_trades()
closes = [t for t in trades if t.get('type') == 'close']
entries = [t for t in trades if t.get('type') == 'entry']
openq, pairs = {}, []
for t in trades:
    key = (t.get('chain') or ('bsc' if str(t.get('mint','')).startswith('0x') else 'solana'), t.get('mint'))
    if t.get('type') == 'entry': openq.setdefault(key, deque()).append(t)
    elif t.get('type') == 'close' and openq.get(key): pairs.append((openq[key].popleft(), t))
print(f'rows={len(trades)} closes={len(closes)} entries={len(entries)} FIFO pairs={len(pairs)}')

# --- pinned 215 universe check (canonical: 169 USD-stamped -$175.52 PF 0.2747 WR 26.6%) ---
pin_c = closes[:215]
pin_u = [c for c in pin_c if c.get('realized_usd') is not None]
print('\n=== PINNED-215 universe check ===')
print('pinned closes:', len(pin_c), 'usd-stamped:', len(pin_u))
show('pinned-215 USD-stamped raw', [raw_usd(None, c) for c in pin_u])

print('\n=== 1. BASELINE full book ===')
raw_all = [raw_usd(None, c) for c in closes]
raw_all = [v for v in raw_all if v is not None]
show('RAW full-book', raw_all)
show('COSTED base-fee', [costed_usd(e, c) for e, c in pairs if costed_usd(e, c) is not None])
show('COSTED 3x-fee', [costed_usd(e, c, 0.0045) for e, c in pairs if costed_usd(e, c, 0.0045) is not None])

print('\n=== 2. ET-day regime (costed, UTC close day) ===')
byd = defaultdict(list)
for e, c in pairs:
    v = costed_usd(e, c)
    if v is None: continue
    byd[ts_utc(c).astimezone(ET).strftime('%Y-%m-%d')].append(v)
for d in sorted(byd):
    s = stats_of(byd[d]); print(f'{d}: n={s["n"]} net=${s["net"]:.2f} PF={s["pf"]:.4f} WR={s["wr"]:.3f} avg=${s["avg"]:.3f}')

print('\n=== 3. fresh since iter-10 ===')
fresh_pairs = [p for p in pairs if ts_utc(p[1]) > ITER10_END_UTC]
fresh_closes = [c for c in closes if ts_utc(c) > ITER10_END_UTC]
print('fresh pairs:', len(fresh_pairs), 'fresh closes:', len(fresh_closes))
show('FRESH pairs costed', [costed_usd(e, c) for e, c in fresh_pairs if costed_usd(e, c) is not None])
for e, c in fresh_pairs:
    print('  ', c.get('ts'), c.get('name'), 'raw', round(raw_usd(e,c) or 0,2), '|', str(c.get('reason'))[:60])

print('\n=== 4. dump-detector retrace cohort (dump-positive) ===')
ddp = [(e, c) for e, c in pairs if 'dump' in str(c.get('reason')).lower()]
show('dump exits costed (all)', [costed_usd(e, c) for e, c in ddp if costed_usd(e, c) is not None])
pos = [(e, c) for e, c in ddp if (costed_usd(e, c) or 0) > 0]
show('dump-POSITIVE costed', [costed_usd(e, c) for e, c in pos])
fresh_pos = [(e, c) for e, c in pos if ts_utc(c) > ITER10_END_UTC]
print('dump-positive fresh:', len(fresh_pos), [(c.get('ts'), round(costed_usd(e,c) or 0,2)) for e, c in fresh_pos])
# dump detector as pullback exit vs wipe: -100% rows
dd100 = [costed_usd(e, c) for e, c in ddp if '-100.0%' in str(c.get('reason'))]
show('dump -100% (venue-dump/LP-pull) costed', [v for v in dd100 if v is not None])

print('\n=== 5. trailing forensics (gap vs design) ===')
trail = [(e, c) for e, c in pairs if 'trail' in str(c.get('reason')).lower()]
def dd_of(e, c):
    try: return float(c['exit'])/float(e['entry']) - 1
    except: return None
gap = [costed_usd(e, c) for e, c in trail if (dd_of(e, c) or 0) < -0.40]
des = [costed_usd(e, c) for e, c in trail if (dd_of(e, c) or 0) >= -0.40]
gap = [v for v in gap if v is not None]; des = [v for v in des if v is not None]
print('trail n:', len(trail))
show('gap-bleed dd<-40%', gap)
show('near-design dd>=-40%', des)

print('\n=== 6. F2 pool-age (young<=900s at entry) ===')
f2 = []
for e, c in pairs:
    v = costed_usd(e, c)
    if v is None: continue
    pca, tse = e.get('pool_created_at'), e.get('ts_epoch')
    if pca and tse and float(tse) - float(pca) >= 0:
        f2.append((e, c, v, float(tse) - float(pca)))
print('F2 paired outcomes:', len(f2))
young = [v for _,_,v,a in f2 if a <= 900]; old = [v for _,_,v,a in f2 if a > 900]
show('young<=900s costed', young)
show('old>900s costed', old)
y3 = [costed_usd(e, c, 0.0045) for e, c, v, a in f2 if a <= 900]; y3=[x for x in y3 if x is not None]
show('young 3x-fee', y3)
ys = sorted(young)
show('young drop-top-2', ys[:-2] if len(ys)>2 else [])
days = sorted(set(ts_utc(c).astimezone(ET).strftime('%m-%d %H:%M') for e,c,v,a in f2))
print('F2 first/last:', days[0] if days else None, '->', days[-1] if days else None)
newf2 = [(e,c,v,a) for e,c,v,a in f2 if ts_utc(c) > ITER10_END_UTC]
print('F2 outcomes fresh since iter-10:', len(newf2), [(round(v,2), int(a)) for e,c,v,a in newf2])

print('\n=== 7. LP-lock measure dataset ===')
meas = [(e, c) for e, c in pairs if e.get('lp_lock_verdict') in ('unlocked','burned')]
show('measured costed', [costed_usd(e, c) for e, c in meas if costed_usd(e, c) is not None])
for verdict in ('unlocked','burned'):
    sub = [costed_usd(e, c) for e, c in meas if e.get('lp_lock_verdict')==verdict]
    sub = [v for v in sub if v is not None]
    print(' ', verdict, f'n={len(sub)}', stats_of(sub))
newm = [(e,c) for e,c in meas if ts_utc(c) > ITER10_END_UTC]
print('measured fresh:', len(newm), [(e.get('lp_lock_verdict'), round(costed_usd(e,c) or 0,2)) for e,c in newm])

print('\n=== 8. re-admission (vetoed-then-entered) cohort ===')
veto_mints = set()
for line in open(f'{PROJ}/runs/paper-1h/candidates.jsonl'):
    try: r = json.loads(line)
    except: continue
    if r.get('event') == 'slip_veto' and r.get('mint'): veto_mints.add(r['mint'])
re = [(e, c) for e, c in pairs if c.get('mint') in veto_mints]
show('re-admissions costed', [costed_usd(e, c) for e, c in re if costed_usd(e, c) is not None])
newre = [(e,c) for e,c in re if ts_utc(c) > ITER10_END_UTC]
print('fresh re-admissions:', len(newre), [(c.get('ts'), round(costed_usd(e,c) or 0,2)) for e,c in newre])

print('\n=== 9. instant wipes (-95% or worse) ===')
w = [(e, c) for e, c in pairs if (costed_usd(e, c) or 0) <= -2.5]
wv = [costed_usd(e, c) for e, c in w if costed_usd(e, c) is not None]
s = show('wipes costed', wv)
net_all = sum(costed_usd(e, c) or 0 for e, c in pairs)
print('wipes share of net:', round(abs(s['net'])/abs(net_all),3))

print('\n=== 10. chain regime ===')
for ch in ('bsc','solana'):
    sub = [costed_usd(e, c) for e, c in pairs if infer_chain(e, c)==ch]
    sub = [v for v in sub if v is not None]
    print(' ', ch, f'n={len(sub)}', stats_of(sub))
recent = pairs[-133:]
print('last-133 pairs all-bsc check:', all(infer_chain(e,c)=='bsc' for e,c in recent))
