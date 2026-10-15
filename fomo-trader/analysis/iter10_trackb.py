#!/usr/bin/env python3
"""iter10_trackb.py — LANE-CODEX-ASTRA iteration 10 Track B gates (lane-direct).

Candidates (structural, conservative, constraint-respecting):
  C1: drop BSC entries
  C2: sticky 24h per-token veto (exclude entry if mint had slip_veto in prior 24h)
  C3: never re-enter a token that already wiped (-100%-class close) [measured first]

Gates: 2 walk-forward folds (IS>=100/OOS>=30); significance = OOS PF gain >= +0.25
vs same-OOS baseline AND/OR OOS PnL/trade > 0 on >=30 OOS; 3x-fee stress OOS PF >= 1;
retention OOS_delta >= 60% of IS_delta; red flags: WR>90%, OOS decay>70%,
single-fold-only, drop-top-2 robustness.
All filters computed in event time (no look-ahead).
"""
import json, re
from collections import defaultdict, deque
from datetime import datetime, timezone

PROJ = '/home/hatch/workspace/fomo-trader'

def load():
    trades = [json.loads(l) for l in open(f'{PROJ}/runs/paper-1h/trades.jsonl') if l.strip()]
    openq, pairs = {}, []
    for t in trades:
        key = (t.get('chain') or ('bsc' if str(t.get('mint','')).startswith('0x') else 'solana'), t.get('mint'))
        if t.get('type') == 'entry': openq.setdefault(key, deque()).append(t)
        elif t.get('type') == 'close' and openq.get(key): pairs.append((openq[key].popleft(), t))
    vetoes = []
    for l in open(f'{PROJ}/runs/paper-1h/candidates.jsonl'):
        r = json.loads(l)
        if r.get('event') == 'slip_veto' and r.get('mint') and r.get('ts_epoch'):
            vetoes.append((float(r['ts_epoch']), str(r['mint'])))
    vetoes.sort()
    return pairs, vetoes

def infer_chain(e, c):
    ch = c.get('chain') or e.get('chain')
    if ch: return ch
    return 'bsc' if str(e.get('mint','')).startswith('0x') else 'solana'

def raw_usd(e, c):
    if c.get('realized_usd') is not None: return float(c['realized_usd'])
    nat = c.get('realized_sol')
    if nat is None: nat = c.get('realized_bnb')
    if nat is None: return None
    rate = c.get('sol_usd') or e.get('sol_usd') or (115 if infer_chain(e,c)=='solana' else 600)
    return float(nat) * float(rate)

def costed(e, c, slip=0.0015):
    r = raw_usd(e, c)
    if r is None or not e.get('entry') or not c.get('exit'): return None
    chain = infer_chain(e, c)
    rate = c.get('sol_usd') or e.get('sol_usd') or (115 if chain=='solana' else 600)
    stake = float(e.get('buy_sol') or 0)
    fixed = 0.004 if chain=='solana' else 0.00004
    ratio = float(c['exit'])/float(e['entry'])
    return r - float(rate)*(stake*(1+ratio)*(0.0025+slip) + fixed)

def stats(vals):
    n=len(vals)
    if n==0: return dict(n=0,net=0.0,pf=float('nan'),wr=float('nan'),avg=float('nan'))
    gp=sum(v for v in vals if v>0); gl=-sum(v for v in vals if v<0)
    return dict(n=n,net=sum(vals),pf=(gp/gl if gl>0 else float('inf')),
               wr=sum(1 for v in vals if v>0)/n, avg=sum(vals)/n)

def te_of(r):
    te = r.get('ts_epoch')
    if te: return float(te)
    return datetime.strptime(str(r['ts'])[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()

WIPE_RE = re.compile(r'-(100\.0|9[4-9]\.\d|8[3-9]\.\d)%')

pairs, vetoes = load()
print(f'pairs={len(pairs)} vetoes={len(vetoes)}')
veto_by_mint = defaultdict(list)
for te, m in vetoes: veto_by_mint[m].append(te)

# event-time wipe history per mint (for C3)
wipe_hist = defaultdict(list)   # mint -> list of wipe close epochs (built in close-append order)
pair_meta = []  # (e, c, is_reentry_of_wipe)
for e, c in pairs:
    m = str(e.get('mint')); ee = te_of(e)
    rw = any(t < ee for t in wipe_hist[m])
    pair_meta.append((e, c, rw))
    if WIPE_RE.search(str(c.get('reason',''))):
        wipe_hist[m].append(te_of(c))
n_rw = sum(1 for _,_,rw in pair_meta if rw)
print(f're-entries of previously-wiped mints: {n_rw}')

def filt_C1(e, c, rw): return infer_chain(e, c) != 'bsc'
def filt_C2(e, c, rw):
    ee = te_of(e); vt = veto_by_mint.get(str(e.get('mint')), [])
    return not any(ee - 86400 < t < ee for t in vt)
def filt_C3(e, c, rw): return not rw

CANDS = {'C1-noBSC': filt_C1, 'C2-stickyVeto24h': filt_C2, 'C3-noReenterWipe': filt_C3}
n = len(pairs)
FOLDS = {'A': (pairs[:177], pairs[177:]), 'B': (pairs[:133], pairs[133:])}

def vals(ps, slip=0.0015):
    out = [costed(e, c, slip) for e, c in ps]
    return [v for v in out if v is not None]

for name, f in CANDS.items():
    print(f'\n===== {name} =====')
    # measure coverage first
    kept = [(e,c) for (e,c,rw) in pair_meta if f(e,c,rw)]
    excl = len(pair_meta) - len(kept)
    print(f'excluded trades (full book): {excl} / {len(pair_meta)}')
    fold_res = []
    for fn, (isp, oosp) in FOLDS.items():
        b_is = stats(vals(isp)); b_oos = stats(vals(oosp))
        # map pairs to meta via index
        idx = {id(p[0]): i for i, p in enumerate(pairs)}
        c_isp = [p for p in isp if f(p[0], p[1], pair_meta[idx[id(p[0])]][2])]
        c_oosp = [p for p in oosp if f(p[0], p[1], pair_meta[idx[id(p[0])]][2])]
        s_is = stats(vals(c_isp)); s_oos = stats(vals(c_oosp))
        s_oos_3x = stats(vals(c_oosp, 0.0045))
        d_is = s_is['pf'] - b_is['pf']; d_oos = s_oos['pf'] - b_oos['pf']
        retain = (d_oos / d_is) if d_is and d_is != 0 else float('nan')
        decay = 1 - retain if retain == retain else float('nan')
        # drop-top-2 robustness on candidate OOS
        ov = sorted(vals(c_oosp), reverse=True)
        s_dt2 = stats(ov[2:]) if len(ov) > 2 else stats([])
        print(f' fold {fn}: IS n={b_is["n"]}/{s_is["n"]} OOS n={b_oos["n"]}/{s_oos["n"]}')
        print(f'   base OOS: PF={b_oos["pf"]:.4f} avg=${b_oos["avg"]:.3f}')
        print(f'   cand OOS: PF={s_oos["pf"]:.4f} avg=${s_oos["avg"]:.3f} WR={s_oos["wr"]:.3f} | dPF={d_oos:+.4f} (IS dPF={d_is:+.4f}, retain={retain:.2f}, decay={decay:.2f})')
        print(f'   cand OOS 3x-fee: PF={s_oos_3x["pf"]:.4f} | drop-top-2 OOS PF={s_dt2["pf"]:.4f} n={s_dt2["n"]}')
        fold_res.append(dict(d_oos=d_oos, retain=retain, decay=decay, s_oos=s_oos,
                             s_oos_3x=s_oos_3x, s_dt2=s_dt2, b_oos=b_oos))
    # verdict
    verdict, why = 'FAIL', []
    sig = [fr['d_oos'] >= 0.25 or (fr['s_oos']['avg'] > 0 and fr['s_oos']['n'] >= 30) for fr in fold_res]
    ok3x = [fr['s_oos_3x']['pf'] >= 1.0 for fr in fold_res]
    okret = [fr['retain'] >= 0.60 for fr in fold_res]
    okdt2 = [fr['s_dt2']['pf'] >= 1.0 for fr in fold_res]
    okwr = [fr['s_oos']['wr'] <= 0.90 for fr in fold_res]
    if all(sig): why.append('significance bar met both folds')
    else: why.append(f'significance: fold gains {[round(fr["d_oos"],3) for fr in fold_res]} (< +0.25 and/or avg<=0)')
    if not all(ok3x): why.append(f'3x-fee stress: {[round(fr["s_oos_3x"]["pf"],3) for fr in fold_res]} (<1.0)')
    if not all(okret): why.append(f'retention: {[round(fr["retain"],2) for fr in fold_res]} (<60%)')
    if not all(okdt2): why.append(f'drop-top-2: {[round(fr["s_dt2"]["pf"],3) for fr in fold_res]} (<1.0)')
    if not all(okwr): why.append('WR>90% red flag')
    if all(sig) and all(ok3x) and all(okret) and all(okdt2) and all(okwr): verdict = 'PASS'
    print(f' VERDICT: {verdict} — ' + '; '.join(why))
