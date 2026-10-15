#!/usr/bin/env python3
"""LANE-VALIDATION iter-11 part 2: FIFO pairing (mint-fallback), F2 pool-age,
LP-lock dataset, fresh cohort, walk-forward OOS gating. Read-only."""
import json
from collections import defaultdict

BD = '/home/hatch/workspace/fomo-trader'
rows = [json.loads(l) for l in open(f'{BD}/runs/paper-1h/trades.jsonl') if l.strip()]
closes = [r for r in rows if r.get('type') == 'close']
entries = [r for r in rows if r.get('type') == 'entry']

def pnl(r):
    v = r.get('realized_usd')
    return v if v is not None else 0.0

def stats_p(pnls):
    w = sum(p for p in pnls if p > 0); l = sum(p for p in pnls if p < 0)
    n = len(pnls)
    return dict(n=n, net=w+l, w=w, l=l, pf=(w/abs(l) if l else float('inf')),
                wr=sum(1 for p in pnls if p > 0)/n if n else 0)

# ---- FIFO pairing with mint-fallback (prefer exact (chain,mint), else same-mint) ----
openq = defaultdict(list)
pairs, unpaired = [], 0
for r in rows:  # file order
    if r.get('type') == 'entry':
        openq[(r.get('chain'), r.get('mint'))].append(r)
    elif r.get('type') == 'close':
        if r.get('realized_usd') is None:
            continue
        ch, m = r.get('chain'), r.get('mint')
        e = None
        if openq.get((ch, m)):
            e = openq[(ch, m)].pop(0)
        else:
            for k in list(openq):
                if k[1] == m and openq[k]:
                    e = openq[k].pop(0); break
        if e is None:
            unpaired += 1; continue
        pairs.append((e, r))
# drain remaining entries (needed only for pairing coverage)
print(f'pairs={len(pairs)} unpaired_closes={unpaired}')
assert len(pairs) + unpaired == len([r for r in closes if r.get('realized_usd') is not None])

# ---- F2 pool-age: scan-time pool_created_at from candidates.jsonl ----
cands = [json.loads(l) for l in open(f'{BD}/runs/paper-1h/candidates.jsonl') if l.strip()]
by_mint = defaultdict(list)
for c in cands:
    if c.get('event') in ('candidate', 'slip_veto') and c.get('pool_created_at') and c.get('ts_epoch'):
        by_mint[c['mint']].append((c['ts_epoch'], c['pool_created_at']))
for m in by_mint: by_mint[m].sort()

ages, noage = [], 0
for e, c in pairs:
    te = e.get('ts_epoch')
    if te is None: noage += 1; continue
    hist = [pc for t, pc in by_mint.get(e.get('mint'), []) if t <= te]
    if not hist: noage += 1; continue
    ages.append((e, c, te - hist[-1], hist[-1], te))
print(f'pool-age-computable pairs={len(ages)} no-age={noage}')

young = [(e, c, a) for e, c, a, pc, te in ages if a <= 900]
old = [(e, c, a) for e, c, a, pc, te in ages if a > 900]
print('\n== F2 pool-age (900s threshold) ==')
sy = stats_p([pnl(c) for e, c, a in young]); so = stats_p([pnl(c) for e, c, a in old])
print(f"young<=900s: n={sy['n']} net={sy['net']:+.2f} PF={sy['pf']:.3f} WR={sy['wr']*100:.1f}% exp={sy['net']/sy['n']:+.3f}")
print(f"old>900s:    n={so['n']} net={so['net']:+.2f} PF={so['pf']:.3f} WR={so['wr']*100:.1f}% exp={so['net']/so['n']:+.3f}")
# regime span of computable pairs
span = [te for e, c, a, pc, te in ages]
import datetime
print('computable regime span:', datetime.datetime.utcfromtimestamp(min(span)), '->', datetime.datetime.utcfromtimestamp(max(span)))
# drop-top-2 robustness on young
py = sorted([pnl(c) for e, c, a in young], reverse=True)
sd2 = stats_p(py[2:])
print(f"young drop-top-2: n={sd2['n']} net={sd2['net']:+.2f} PF={sd2['pf']:.3f}")
# 3x-fee stress: -$0.30/trade
sf = stats_p([pnl(c)-0.30 for e, c, a in young])
print(f"young 3x-fee(-$0.30/trade): n={sf['n']} net={sf['net']:+.2f} PF={sf['pf']:.3f} exp={sf['net']/sf['n']:+.3f}")
# single-regime check: how many distinct days
days = defaultdict(int)
for e, c, a, pc, te in ages:
    days[datetime.datetime.utcfromtimestamp(te).date()] += 1
print('young-pool regime days:', {str(k): v for k, v in sorted(days.items())})

# ---- walk-forward OOS for F2 keep-young filter (event-time split on computable pairs) ----
ages_sorted = sorted(ages, key=lambda x: x[4])  # by entry ts_epoch
def gate(pnls_all, keep):
    return stats_p([p for p, k in zip(pnls_all, keep) if k])
print('\n== F2 walk-forward: baseline vs keep-young-only ==')
base_all = [pnl(c) for e, c, a, pc, te in ages_sorted]
keep_y = [a <= 900 for e, c, a, pc, te in ages_sorted]
for f in (0.5, 0.6, 0.7):
    k = int(len(ages_sorted)*f)
    oos_p, oos_k = base_all[k:], keep_y[k:]
    sb = stats_p(oos_p); sk = stats_p([p for p, kk in zip(oos_p, oos_k) if kk])
    nk = sum(oos_k)
    print(f"f={f}: OOS n={len(oos_p)} base PF={sb['pf']:.3f} | keep-young n={nk} PF={sk['pf']:.3f} dPF={sk['pf']-sb['pf']:+.3f}")
    # 3x-fee on keep-young OOS
    skf = stats_p([p-0.30 for p, kk in zip(oos_p, oos_k) if kk])
    print(f"     keep-young OOS 3x-fee: PF={skf['pf']:.3f} exp={skf['net']/skf['n']:+.3f}" if skf['n'] else "     keep-young OOS 3x-fee: n=0")
sb = stats_p(base_all); sk = stats_p([p for p, k in zip(base_all, keep_y) if k])
print(f"FULL: baseline PF={sb['pf']:.3f} | keep-young PF={sk['pf']:.3f} dPF={sk['pf']-sb['pf']:+.3f} n={sk['n']}")

# ---- LP-lock dataset growth ----
lp = [(e, c) for e, c in pairs if e.get('lp_lock_verdict')]
ver = defaultdict(list)
for e, c in lp: ver[e['lp_lock_verdict']].append(pnl(c))
print(f'\n== LP-lock measure dataset: {len(lp)} measured pairs ==')
for v, ps in ver.items():
    s = stats_p(ps); print(f"  {v}: n={s['n']} net={s['net']:+.2f} PF={s['pf']:.3f} WR={s['wr']*100:.1f}%")
print('frozen protocol needs >=100 IS / >=30 OOS measured')

# ---- fresh cohort (closes after iter-10 snapshot: file idx >= 270th close) ----
n10 = 270
fresh = closes[n10:]
fu = [r for r in fresh if r.get('realized_usd') is not None]
sfu = stats_p([pnl(r) for r in fu])
print(f'\n== post-iter-10 cohort (file-order closes #{n10+1}+): n={sfu["n"]} net={sfu["net"]:+.2f} PF={sfu["pf"]:.3f} WR={sfu["wr"]*100:.1f}%')
print('  reasons:', [((r.get('reason') or '?')[:40], f"{pnl(r):+.2f}") for r in fu])
# extended first-profitable cohort: iter-10 window was closes #248..269 (+$8.08 n=23); now #248..end
w = [r for r in closes[247:] if r.get('realized_usd') is not None]
sw = stats_p([pnl(r) for r in w])
print(f'== extended anomaly window (closes #248+): n={sw["n"]} net={sw["net"]:+.2f} PF={sw["pf"]:.3f}')
p3 = sorted([pnl(r) for r in w], reverse=True)[:3]
sw3 = stats_p(sorted([pnl(r) for r in w], reverse=True)[3:])
print(f'  drop-top-3: n={sw3["n"]} net={sw3["net"]:+.2f} PF={sw3["pf"]:.3f}')
