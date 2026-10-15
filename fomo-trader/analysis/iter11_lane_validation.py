#!/usr/bin/env python3
"""LANE-VALIDATION iter-11: independent baseline reproduction + candidate gating.
File order only (mixed UTC/EDT naive ts). Raw bot-recorded P&L. Read-only.
"""
import json, math
from collections import Counter, defaultdict

BD = '/home/hatch/workspace/fomo-trader'
rows = [json.loads(l) for l in open(f'{BD}/runs/paper-1h/trades.jsonl') if l.strip()]
closes = [r for r in rows if r.get('type') == 'close']
entries = [r for r in rows if r.get('type') == 'entry']
print(f'rows={len(rows)} closes={len(closes)} entries={len(entries)}')

def usd(r):  # USD-stamped close
    v = r.get('realized_usd')
    return v if v is not None else None

def pnl_usd(r):
    v = usd(r)
    return v if v is not None else 0.0

def stats(rs):
    pnls = [pnl_usd(r) for r in rs]
    w = sum(p for p in pnls if p > 0); l = sum(p for p in pnls if p < 0)
    n = len(pnls); nw = sum(1 for p in pnls if p > 0)
    net = w + l
    pf = w / abs(l) if l else float('inf')
    wr = nw / n if n else 0
    return dict(n=n, net=net, w=w, l=l, pf=pf, wr=wr, nw=nw)

def show(name, s):
    print(f"{name}: n={s['n']} net={s['net']:+.2f} W={s['w']:+.2f} L={s['l']:+.2f} PF={s['pf']:.4f} WR={s['wr']*100:.1f}%")

# ---------- §1 pinned 215 universe ----------
pin = closes[:215]
pin_usd = [r for r in pin if usd(r) is not None]
pin_bsc = [r for r in pin_usd if r.get('chain') == 'bsc']
pin_solusd = [r for r in pin_usd if r.get('chain') == 'solana']
pin_leg = [r for r in pin if usd(r) is None]
print('\n== PINNED 215 universe (first 215 file-order closes) ==')
print('usd-stamped:', len(pin_usd), 'bsc:', len(pin_bsc), 'sol-usd:', len(pin_solusd), 'legacy-nochain:', len(pin_leg))
show('USD', stats(pin_usd))
show('BSC', stats(pin_bsc))
show('SOL-usd', stats(pin_solusd))
sols = [r.get('realized_sol', 0) for r in pin_leg]
sw = sum(p for p in sols if p > 0); sl = sum(p for p in sols if p < 0)
print(f"legacy SOL: n={len(pin_leg)} net={sum(sols):+.6f} SOL PF={sw/abs(sl):.4f} WR={sum(1 for p in sols if p>0)/len(sols)*100:.1f}%")
# iter-9 check: cursor cut closes 1-247
c247 = [r for r in closes[:247] if usd(r) is not None]
show('cursor-1-247-USD', stats(c247))

# ---------- current lifetime ----------
usd_all = [r for r in closes if usd(r) is not None]
bsc_all = [r for r in usd_all if r.get('chain') == 'bsc']
solusd_all = [r for r in usd_all if r.get('chain') == 'solana']
leg_all = [r for r in closes if usd(r) is None]
print('\n== CURRENT LIFETIME ==')
show('USD', stats(usd_all))
show('BSC', stats(bsc_all))
show('SOL-usd', stats(solusd_all))
ls = [r.get('realized_sol', 0) for r in leg_all]
print(f"legacy SOL: n={len(leg_all)} net={sum(ls):+.6f} SOL")

# max DD on cumulative USD curve (file order)
cum, peak, dd = 0.0, 0.0, 0.0
for r in usd_all:
    cum += pnl_usd(r); peak = max(peak, cum); dd = max(dd, peak - cum)
print(f'maxDD_USD = ${dd:.2f} (peak ${peak:.2f}, final ${cum:.2f})')
# costed basis: -$0.15/trade single-side fee proxy used in prior costed numbers? use 3x-fee = +$0.30/trade
fee = 0.30
costed = [pnl_usd(r) - fee for r in usd_all]
cw = sum(p for p in costed if p > 0); cl = sum(p for p in costed if p < 0)
print(f"costed(3x-fee ${fee}/trade): net=${sum(costed):+.2f} PF={cw/abs(cl):.4f}")

# ---------- P&L by exit reason ----------
print('\n== P&L by exit-reason family (USD) ==')
fams = defaultdict(list)
for r in usd_all:
    rsn = (r.get('reason') or '').lower()
    if 'pinned' in rsn and '-100' in rsn: fam = 'pinned -100% wipe'
    elif 'dump' in rsn and 'venue' in rsn: fam = 'venue dump <100%'
    elif 'dump' in rsn: fam = 'dump detector <100%'
    elif 'trailing' in rsn or 'trail' in rsn: fam = 'trailing stop <100%'
    elif 'stale' in rsn: fam = 'stale exit'
    elif 'manual' in rsn: fam = 'manual'
    else: fam = 'other: ' + (r.get('reason') or '?')[:40]
    fams[fam].append(r)
for fam, rs in sorted(fams.items(), key=lambda x: sum(pnl_usd(r) for r in x[1])):
    s = stats(rs); print(f"  {fam}: n={s['n']} net={s['net']:+.2f} PF={s['pf']:.3f} WR={s['wr']*100:.0f}%")
# strict price-based wipes: exit/entry <= 5%
wipes = [r for r in usd_all if r.get('exit') and r.get('entry') and r['exit']/r['entry'] <= 0.05]
sw_ = stats(wipes)
print(f"strict price-wipes (exit/entry<=5%): n={sw_['n']} net={sw_['net']:+.2f} share={abs(sw_['net'])/abs(stats(usd_all)['net'])*100:.1f}% of USD net")

# ---------- trailing detail ----------
print('\n== trailing/dump exit split ==')
print(Counter((r.get('reason') or '?')[:60] for r in usd_all).most_common(12))
