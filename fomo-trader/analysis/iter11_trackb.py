"""iter-11 Track B gates. Read-only. Event-time filters (entry observables only).
Folds: A IS pairs[0:181]/OOS pairs[181:272]; B IS pairs[0:136]/OOS pairs[136:272].
Bars: OOS dPF>=+0.25; 3x-fee OOS PF>=1; retention>=60% of IS dPF; red flags:
WR>90%, decay>70%, single-fold-only, drop-top-2 OOS PF<1; sample gates IS>=100/OOS>=30.
"""
import json, sys
from collections import deque
sys.path.insert(0, '/home/hatch/workspace/fomo-trader/analysis')
from iter11_lane_direct import (load_trades, raw_usd, costed_usd, stats_of, max_drawdown)

PROJ = '/home/hatch/workspace/fomo-trader'
trades = load_trades()
openq, pairs = {}, []
for t in trades:
    key = (t.get('chain') or ('bsc' if str(t.get('mint','')).startswith('0x') else 'solana'), t.get('mint'))
    if t.get('type') == 'entry': openq.setdefault(key, deque()).append(t)
    elif t.get('type') == 'close' and openq.get(key): pairs.append((openq[key].popleft(), t))
print('pairs:', len(pairs))

def pool_age(e):
    pca, tse = e.get('pool_created_at'), e.get('ts_epoch')
    if pca and tse and float(tse) - float(pca) >= 0: return float(tse) - float(pca)
    return None

def gate(name, excl_fn, desc):
    """excl_fn(e) -> True means EXCLUDE the pair (entry-time observable)."""
    print(f'\n==== {name}: {desc} ====')
    folds = {'A': (pairs[:181], pairs[181:]), 'B': (pairs[:136], pairs[136:])}
    res = {}
    for fn, (IS, OOS) in folds.items():
        def kept(blk):
            out = []
            for e, c in blk:
                v = costed_usd(e, c)
                if v is None: continue
                if not excl_fn(e): out.append(v)
            return out
        def costed3(blk):
            out = []
            for e, c in blk:
                v = costed_usd(e, c, 0.0045)
                if v is None: continue
                if not excl_fn(e): out.append(v)
            return out
        b_is, b_oos = stats_of(kept(IS)) if False else None, None
        # baseline OOS (same block, no exclusion)
        bo = [costed_usd(e, c) for e, c in OOS]; bo = [v for v in bo if v is not None]
        ko = kept(OOS); k3 = costed3(OOS)
        sb, sk, s3 = stats_of(bo), stats_of(ko), stats_of(k3)
        dpf = (sk['pf'] - sb['pf']) if sk['pf'] != float('inf') and sb['pf'] != float('inf') else float('nan')
        bi = [costed_usd(e, c) for e, c in IS]; bi = [v for v in bi if v is not None]
        ki = kept(IS)
        sbi, ski = stats_of(bi), stats_of(ki)
        dpf_is = (ski['pf'] - sbi['pf']) if ski['pf'] != float('inf') and sbi['pf'] != float('inf') else float('nan')
        ret = (dpf / dpf_is) if dpf_is and dpf_is != 0 and dpf == dpf else float('nan')
        so = sorted(ko)
        drop2 = stats_of(so[:-2]) if len(so) > 2 else stats_of([])
        print(f' fold {fn}: IS n={sbi["n"]}/{ski["n"]} dPF_IS={dpf_is:+.4f} | OOS kept n={sk["n"]}(base {sb["n"]}) '
              f'basePF={sb["pf"]:.4f} keptPF={sk["pf"]:.4f} dPF_OOS={dpf:+.4f} | 3x-fee keptPF={s3["pf"]:.4f} '
              f'| retention={ret:.3f} | OOS keptWR={sk["wr"]:.3f} | drop-top-2 PF={drop2["pf"]:.4f} net=${drop2["net"]:.2f}')
        res[fn] = dict(dpf=dpf, kept3pf=s3['pf'], ret=ret, wr=sk['wr'], drop2pf=drop2['pf'],
                       n_is_kept=ski['n'], n_oos_kept=sk['n'])
    return res

# C1: F2 — exclude entries with KNOWN pool age > 900s (unknown age fail open)
gate('C1', lambda e: (pool_age(e) is not None and pool_age(e) > 900),
     'F2 young-pool screen: veto entries with known pool age >900s (unknowns fail open)')

# C2: exclude whale-bait m15_buys>200 (unknown fail open)
gate('C2', lambda e: (e.get('m15_buys') is not None and e.get('m15_buys') > 200),
     'exclude m15_buys>200 whale-bait entries (unknowns fail open)')

# C3: exclude burned-LP entries (F1 proxy, unknowns fail open)
gate('C3', lambda e: e.get('lp_lock_verdict') == 'burned',
     'exclude burned-LP measured entries (F1 proxy)')

# C4: exclude known-old pools AND burned: combined conservative screen
gate('C4', lambda e: (pool_age(e) is not None and pool_age(e) > 900) or e.get('lp_lock_verdict') == 'burned',
     'combined: veto known-old pools + burned-LP entries')
