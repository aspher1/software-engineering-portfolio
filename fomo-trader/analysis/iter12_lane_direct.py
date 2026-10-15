"""iter-12 lane-codex-astra: direct-shell analysis. Codex billing-walled (no probes).
Read-only. FIFO pairing + pricing mirrors iter-10/iter-11 exactly (validated methodology)."""
import json, re, math
from collections import defaultdict, deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PROJ = '/home/hatch/workspace/fomo-trader'
ET = ZoneInfo('America/New_York')
ITER10_END_UTC = datetime(2026, 9, 30, 9, 0, 0, tzinfo=timezone.utc)  # iter-10 lane ~04:07-04:50 EDT
ITER11_END_UTC = datetime(2026, 9, 30, 12, 55, 0, tzinfo=timezone.utc)  # iter-11 lane ~08:07-08:50 EDT

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

print('\n=== 3a. fresh since iter-10 ===')
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

print('\n=== 11. fresh since iter-11 ===')
ITER11_END = ITER11_END_UTC
fresh12_pairs = [p for p in pairs if ts_utc(p[1]) > ITER11_END]
fresh12_closes = [c for c in closes if ts_utc(c) > ITER11_END]
print('fresh pairs since iter-11:', len(fresh12_pairs), 'fresh closes:', len(fresh12_closes))
show('FRESH12 pairs costed', [costed_usd(e, c) for e, c in fresh12_pairs if costed_usd(e, c) is not None])
for e, c in fresh12_pairs:
    print('  ', c.get('ts'), c.get('name'), 'raw', round(raw_usd(e,c) or 0,2),
          'costed', round(costed_usd(e,c) or 0,2), '|', str(c.get('reason'))[:60])

print('\n=== 12. entry-observable gradients (NEW candidate mining) ===')
def qcuts(vals, qs=(0.25,0.5,0.75)):
    vs = sorted(vals)
    if not vs: return []
    return [vs[min(len(vs)-1, int(q*len(vs)))] for q in qs]

# (a) sell dominance: m15_sells vs m15_buys
sd = []
for e, c in pairs:
    v = costed_usd(e, c)
    if v is None: continue
    b, s_ = e.get('m15_buys'), e.get('m15_sells')
    if b is None or s_ is None: continue
    dom = s_ >= b  # sell-dominant flow at entry
    sd.append((dom, v, b, s_))
print('sell-dominance coverage:', len(sd))
for dom, lab in ((True,'sell-dominant s>=b'), (False,'buy-dominant s<b')):
    sub = [v for d,v,b,s_ in sd if d==dom]
    show('  '+lab, sub)
# wipe rate by dominance
for dom, lab in ((True,'sell-dom'), (False,'buy-dom')):
    sub = [v for d,v,b,s_ in sd if d==dom]
    wipes = sum(1 for v in sub if v <= -2.5)
    print('  ', lab, 'wipe<=%-2.50 rate:', round(wipes/max(1,len(sub)),3))

# (b) liquidity tiers
liqv = [(float(e.get('liquidity_usd') or 0), costed_usd(e,c)) for e,c in pairs]
liqv = [(l,v) for l,v in liqv if v is not None and l>0]
liqv.sort()
n=len(liqv); q1,q2,q3 = (liqv[int(0.25*n)][0], liqv[int(0.5*n)][0], liqv[int(0.75*n)][0]) if n else (0,0,0)
print('liquidity quartiles:', round(q1), round(q2), round(q3))
for lo,hi,lab in ((0,q1,'LQ1 thin'),(q1,q2,'LQ2'),(q2,q3,'LQ3'),(q3,1e18,'LQ4 deep')):
    sub=[v for l,v in liqv if lo<l<=hi]
    show('  liq '+lab, sub)

# (c) signal_gain_pct quartiles (R11 gradient re-check)
sgv = [(float(e.get('signal_gain_pct') or 0), costed_usd(e,c)) for e,c in pairs]
sgv = [(g,v) for g,v in sgv if v is not None]; sgv.sort()
n=len(sgv); g1,g2,g3 = (sgv[int(0.25*n)][0], sgv[int(0.5*n)][0], sgv[int(0.75*n)][0]) if n else (0,0,0)
print('signal_gain quartiles:', round(g1,1), round(g2,1), round(g3,1))
for lo,hi,lab in ((-1e9,g1,'GQ1'),(g1,g2,'GQ2'),(g2,g3,'GQ3'),(g3,1e9,'GQ4')):
    sub=[v for g,v in sgv if lo<g<=hi]
    show('  gain '+lab, sub)

# (d) mcap tiers (BSC only rows have it)
mcv = [(float(e.get('mcap_usd') or 0), costed_usd(e,c)) for e,c in pairs if e.get('mcap_usd')]
mcv = [(m,v) for m,v in mcv if v is not None]; mcv.sort()
n=len(mcv)
if n:
    m1,m2,m3 = mcv[int(0.25*n)][0], mcv[int(0.5*n)][0], mcv[int(0.75*n)][0]
    print('mcap quartiles:', int(m1), int(m2), int(m3), 'n=', n)
    for lo,hi,lab in ((0,m1,'MQ1'),(m1,m2,'MQ2'),(m2,m3,'MQ3'),(m3,1e18,'MQ4')):
        sub=[v for m,v in mcv if lo<m<=hi]
        show('  mcap '+lab, sub)

# (e) entry latency tiers
lat = [(float(e.get('entry_latency_ms') or 0), costed_usd(e,c)) for e,c in pairs if e.get('entry_latency_ms')]
lat = [(l,v) for l,v in lat if v is not None]; lat.sort()
n=len(lat)
if n:
    l1,l2,l3 = lat[int(0.25*n)][0], lat[int(0.5*n)][0], lat[int(0.75*n)][0]
    print('latency quartiles ms:', int(l1), int(l2), int(l3), 'n=', n)
    for lo,hi,lab in ((0,l1,'LAT1'),(l1,l2,'LAT2'),(l2,l3,'LAT3'),(l3,1e18,'LAT4')):
        sub=[v for l,v in lat if lo<l<=hi]
        show('  lat '+lab, sub)

print('\n=== 13. same-mint rug re-pump re-entry cohort (prior -95%+ wipe on same mint) ===')
def mintkey(t):
    return (t.get('chain') or ('bsc' if str(t.get('mint','')).startswith('0x') else 'solana'), t.get('mint'))
wiped_mints = set()
rrv = []
for e, c in pairs:
    k = mintkey(e)
    v = costed_usd(e, c)
    if v is None: continue
    if k in wiped_mints:
        rrv.append(v)
    if v <= -2.5:
        wiped_mints.add(k)
print('rug-repump re-entry pairs:', len(rrv), 'mints ever wiped:', len(wiped_mints))
show('rug-repump re-entries costed', rrv)
