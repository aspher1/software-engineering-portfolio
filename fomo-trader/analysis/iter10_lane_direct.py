#!/usr/bin/env python3
"""iter10_lane_direct.py — LANE-CODEX-ASTRA iteration 10 (lane-direct: Codex billing-walled).

Read-only vs bot state. Never touches trades.jsonl / state.json / bot.log / deaths.log /
keypair.json / wrapper.log.

1. Baseline repro (raw bot-recorded headline, full book) vs iter-9 (247 / -$242.99 / PF 0.3342).
2. Regime characterization: ET day, chain, hour-of-entry, exit-reason bucket, gain decile,
   hold duration, mcap/buys at entry.
3. Pressure tests of iter-9 findings: gap-bleed trailing forensics on fresh data, fresh cohort,
   slip-veto forward window extension, LP-lock outcomes, T=20 light check.
4. Track B gating for NEW structural candidates:
     C1: drop BSC entries (conservative harm-reduction)
     C2: sticky 24h per-token veto (exclude entries with slip_veto in prior 24h)
     C3: never re-enter a previously -100% wiped token (structural blacklist)
   Gates per frozen Track B: replay w/ realistic costs (0.0025+slip/leg + fixed),
   walk-forward IS/OOS (2 folds, >=100 IS / >=30 OOS each), significance bar
   (OOS PF gain >= +0.25 vs same-OOS baseline and/or OOS PnL/trade>0 on >=30 OOS),
   3x-fee stress (slip 0.0045/leg) OOS PF>=1, red flags: WR>90%, OOS decay>70%,
   single-fold-only, drop-top-2 robustness.
"""
import json, re, math
from collections import defaultdict, deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PROJ = '/home/hatch/workspace/fomo-trader'
ET = ZoneInfo('America/New_York')
ITER9_END_UTC = datetime(2026, 9, 30, 5, 0, 0, tzinfo=timezone.utc)  # iter-9 lane ~00:07-01:00 EDT

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
    return datetime.strptime(str(row.get('ts'))[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)

def et_day(row):
    return ts_utc(row).astimezone(ET).strftime('%Y-%m-%d')

def et_hour(row):
    return ts_utc(row).astimezone(ET).hour

def show(label, vals):
    s = stats_of(vals)
    print(f'{label}: n={s["n"]} net=${s["net"]:.2f} PF={s["pf"]:.4f} WR={s["wr"]:.3f} avg=${s["avg"]:.3f} maxDD=${max_drawdown(vals):.2f}')
    return s

trades = load_trades()
closes = [t for t in trades if t.get('type') == 'close']
entries = [t for t in trades if t.get('type') == 'entry']

# FIFO pairs (close append order)
openq, pairs = {}, []
for t in trades:
    key = (t.get('chain') or ('bsc' if str(t.get('mint', '')).startswith('0x') else 'solana'), t.get('mint'))
    if t.get('type') == 'entry': openq.setdefault(key, deque()).append(t)
    elif t.get('type') == 'close' and openq.get(key): pairs.append((openq[key].popleft(), t))
print(f'rows={len(trades)} closes={len(closes)} entries={len(entries)} FIFO pairs={len(pairs)}')

print('\n=== 1. BASELINE (raw bot-recorded, full book, all closes) ===')
raw_all = [raw_usd(None, c) for c in closes]
raw_all = [v for v in raw_all if v is not None]
show('RAW full-book', raw_all)
print('\n=== 1b. BASELINE (costed, FIFO pairs) ===')
show('COSTED base-fee', [costed_usd(e, c) for e, c in pairs if costed_usd(e, c) is not None])
show('COSTED 3x-fee', [costed_usd(e, c, 0.0045) for e, c in pairs if costed_usd(e, c, 0.0045) is not None])

# fresh cohort = pairs appended after iter-9 ended (pair index >= 247) + closes after cutoff
fresh_pairs = [p for p in pairs if ts_utc(p[1]) > ITER9_END_UTC]
fresh_closes = [c for c in closes if ts_utc(c) > ITER9_END_UTC]
print(f'\n=== fresh since iter-9 (pair-idx>=247 check: {len([p for p in pairs if pairs.index(p)>=247])}) ===')
show('FRESH pairs costed', [costed_usd(e, c) for e, c in fresh_pairs if costed_usd(e, c) is not None])
show('FRESH closes raw', [raw_usd(None, c) for c in fresh_closes if raw_usd(None, c) is not None])

print('\n=== 2. REGIME CUTS (costed pairs) ===')
def cut(label, keyfn):
    groups = defaultdict(list)
    for e, c in pairs:
        v = costed_usd(e, c)
        if v is None: continue
        groups[keyfn(e, c)].append(v)
    for k in sorted(groups):
        show(f'  {label}={k}', groups[k])

cut('ET-day', lambda e, c: et_day(c))
cut('chain', lambda e, c: infer_chain(e, c))
cut('ET-hour', lambda e, c: et_hour(e))

def reason_bucket(c):
    r = str(c.get('reason', ''))
    if 'trailing stop' in r: return 'trailing'
    if r.startswith('venue dump'): return 'venue-dump'
    if 'dump detector' in r: return 'dump'
    if 'stale exit' in r: return 'stale'
    if 'take profit' in r: return 'take-profit'
    return 'other'
cut('exit', lambda e, c: reason_bucket(c))

def gain_dec(e, c):
    g = e.get('signal_gain_pct')
    if g is None: return 'none'
    g = float(g)
    for i, b in enumerate([40, 61, 94, 125, 156, 187, 224, 264, 308]):
        if g <= b: return f'D{i+1}<={b}'
    return 'D10>308'
cut('gain-dec', gain_dec)

def hold_bucket(e, c):
    try: m = (ts_utc(c) - ts_utc(e)).total_seconds() / 60
    except Exception: return '?'
    if m <= 5: return '0-5m'
    if m <= 20: return '5-20m'
    if m <= 60: return '20-60m'
    return '>60m'
cut('hold', hold_bucket)

def mcap_bucket(e, c):
    m = e.get('mcap_usd')
    if m is None: return 'none'
    m = float(m)
    if m < 200_000: return '<200k'
    if m < 1_000_000: return '200k-1M'
    return '>1M'
cut('mcap', mcap_bucket)

def buys_bucket(e, c):
    b = e.get('m15_buys')
    if b is None: return 'none'
    b = float(b)
    if b < 50: return '<50'
    if b < 200: return '50-200'
    return '>200'
cut('m15_buys', buys_bucket)

print('\n=== 3a. TRAILING FORENSICS (parse dd% from peak) ===')
trail = []
for e, c in pairs:
    m = re.search(r'trailing stop (-?[\d.]+)% from peak', str(c.get('reason', '')))
    if m:
        dd = float(m.group(1))
        v = costed_usd(e, c)
        if v is not None: trail.append((dd, v, et_day(c), c.get('mint'), ts_utc(c) > ITER9_END_UTC))
near = [(d, v) for d, v, day, mint, fr in trail if d >= -40]
gap = [(d, v) for d, v, day, mint, fr in trail if d < -40]
print(f'trailing closes: {len(trail)}; near-design(dd>=-40%): {len(near)}; gap-bleed(dd<-40%): {len(gap)}')
show('  near-design', [v for _, v in near])
show('  gap-bleed', [v for _, v in gap])
fr_trail = [(d, v) for d, v, day, mint, fr in trail if fr]
print(f'  fresh trailing ({len(fr_trail)}): ' + ', '.join(f'{d:.1f}%:${v:.2f}' for d, v in fr_trail))
gap_fresh = [v for d, v in fr_trail if d < -40]
if gap_fresh: show('  fresh gap-bleed', gap_fresh)

print('\n=== 3b. SLIP-VETO FORWARD WINDOW (extended to now) ===')
vetoes = []
for l in open(f'{PROJ}/runs/paper-1h/candidates.jsonl'):
    r = json.loads(l)
    if r.get('event') == 'slip_veto' and r.get('mint'):
        te = r.get('ts_epoch')
        vetoes.append((float(te) if te else ts_utc(r).timestamp(), str(r['mint'])))
vetoes.sort()
print('veto events:', len(vetoes))
# readmission: entry whose mint had veto within prior 24h
veto_by_mint = defaultdict(list)
for te, m in vetoes: veto_by_mint[m].append(te)
def entry_epoch(e):
    te = e.get('ts_epoch')
    return float(te) if te else ts_utc(e).timestamp()
readmit, clean = [], []
for e, c in pairs:
    v = costed_usd(e, c)
    if v is None: continue
    ee = entry_epoch(e)
    vt = veto_by_mint.get(str(e.get('mint')), [])
    (readmit if any(ee - 86400 < t < ee for t in vt) else clean).append(v)
show('re-admitted(24h-veto) ', readmit)
show('clean', clean)
# forward window >= 2026-09-28 16:24 UTC
FW0 = datetime(2026, 9, 28, 16, 24, 0, tzinfo=timezone.utc).timestamp()
fw = [(e, c) for e, c in pairs if entry_epoch(e) >= FW0]
fw_vals = [costed_usd(e, c) for e, c in fw]
fw_vals = [v for v in fw_vals if v is not None]
wipes = [(e, c) for e, c in fw if re.search(r'-100\.0%|-(9[4-9]|8[3-9])\.\d%', str(c.get('reason','')))]
print(f'forward window: n={len(fw_vals)} wipes~={len(wipes)}')
show('FW costed', fw_vals)
for e, c in wipes:
    ee = entry_epoch(e); vt = veto_by_mint.get(str(e.get('mint')), [])
    ra = any(ee - 86400 < t < ee for t in vt)
    print(f'  wipe {c.get("name","?")[:28]:28} {c.get("reason","")[:44]:44} readmit={ra}')

print('\n=== 3c. LP-LOCK measure outcomes ===')
lp = [(e, c) for e, c in pairs if e.get('lp_lock_verdict')]
ver = defaultdict(list)
for e, c in lp:
    v = costed_usd(e, c)
    if v is not None: ver[str(e.get('lp_lock_verdict'))].append(v)
print('measured entries w/ outcomes:', len(lp))
for k in sorted(ver): show(f'  verdict={k}', ver[k])

print('\n=== 3d. T=20 light check (descriptive; candidate stays retired) ===')
vals_all, vals_le20, vals_gt20 = [], [], []
for e, c in pairs:
    v = costed_usd(e, c)
    if v is None: continue
    vals_all.append(v)
    m = (ts_utc(c) - ts_utc(e)).total_seconds() / 60
    (vals_gt20 if m > 20 else vals_le20).append(v)
show('hold<=20m', vals_le20); show('hold>20m', vals_gt20)
