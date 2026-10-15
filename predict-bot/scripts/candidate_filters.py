"""Shadow-only filter validation on recorded one-contract paper rows."""
import argparse
import json
import math
import random
import statistics as st
from collections import defaultdict
from pathlib import Path


def read(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def safe_output(path):
    if Path(path).name not in {'candidate_filters_paper.jsonl', 'candidate_filters_summary.json'}:
        raise ValueError('protected output path')
    return Path(path)


def load(grades, paper):
    by_key = {p['key']: p for p in read(paper) if p['executable_pnl_net'] is not None}
    rows = []
    for g in read(grades):
        p = by_key.get(g.get('key'))
        if g.get('proxy') != 'kalshi' or p is None or not isinstance(g.get('edge'), (int, float)):
            continue
        rows.append({'key': g['key'], 'sym': g['sym'], 'edge': abs(g['edge']),
                     'time': g['window_close_utc'], 'price': p['entry_executable_taken'],
                     'pnl': p['executable_pnl_net']})
    return sorted(rows, key=lambda r: (r['time'], r['key']))


def calibrate(rows):
    by_coin = defaultdict(list)
    for r in rows:
        by_coin[r['sym']].append(r['pnl'])
    coins = sorted(by_coin, key=lambda c: (-st.mean(by_coin[c]), c))[:3]
    thresholds = [i / 100 for i in range(5, 31)]
    # Minimum 10% of calibration sample avoids optimizing on a handful of trades.
    threshold = max(thresholds, key=lambda x: (st.mean([r['pnl'] for r in rows if r['edge'] >= x])
                    if sum(r['edge'] >= x for r in rows) >= max(10, math.ceil(.1 * len(rows))) else -1e9, -x))
    return coins, threshold


def selected(r, coins, threshold, kind):
    return (r['sym'] in coins if kind == 'coin' else r['edge'] >= threshold if kind == 'edge'
            else r['sym'] in coins and r['edge'] >= threshold)


def placebo(block, chosen, draws=2000, seed=1):
    if not chosen:
        return {'p': None, 'placebo_mean': None, 'effect': None}
    ordered = sorted(block, key=lambda r: r['price'])
    # Rank deciles of taken-side price; sampling only from the same block.
    bins = [ordered[i * len(ordered) // 10:(i + 1) * len(ordered) // 10] for i in range(10)]
    membership = {r['key']: i for i, b in enumerate(bins) for r in b}
    counts = [sum(membership[r['key']] == i for r in chosen) for i in range(10)]
    observed = st.mean(r['pnl'] for r in chosen)
    rng = random.Random(seed)
    null = [st.mean(r['pnl'] for i, b in enumerate(bins) for r in rng.sample(b, counts[i])) for _ in range(draws)]
    return {'p': (1 + sum(x >= observed for x in null)) / (draws + 1),
            'placebo_mean': st.mean(null), 'effect': observed - st.mean(null)}


def evaluate(block, coins, threshold, kind, seed):
    chosen = [r for r in block if selected(r, coins, threshold, kind)]
    q = placebo(block, chosen, seed=seed)
    return {'n': len(block), 'kept_n': len(chosen), 'all_mean': st.mean(r['pnl'] for r in block),
            'kept_mean': st.mean(r['pnl'] for r in chosen) if chosen else None, **q}


def holm(pvals):
    result = {}
    running = 0
    for rank, (name, p) in enumerate(sorted(pvals.items(), key=lambda x: x[1])):
        running = max(running, min(1, (len(pvals) - rank) * p))
        result[name] = running
    return result


def split(rows, fraction=.4):
    n = len(rows)
    targets = [int(n * fraction), int(n*(fraction+(1-fraction)/3)),
               int(n*(fraction+2*(1-fraction)/3))]
    bounds = []
    for target in targets:
        while target < n and rows[target-1]['time'] == rows[target]['time']:
            target += 1
        bounds.append(target)
    a = bounds[0]
    bounds.append(n)
    return rows[:a], [rows[bounds[i]:bounds[i+1]] for i in range(3)]


def run(rows, seed=20261005):
    calibration, blocks = split(rows)
    coins, threshold = calibrate(calibration)
    kinds = ('coin', 'edge', 'combo')
    forward = {k: [evaluate(b, coins, threshold, k, seed+i*10+j) for i,b in enumerate(blocks)]
               for j,k in enumerate(kinds)}
    pooled = [r for b in blocks for r in b]
    pooled_result = {k: evaluate(pooled, coins, threshold, k, seed+100+j) for j,k in enumerate(kinds)}
    corrected = holm({k: pooled_result[k]['p'] for k in kinds})
    for k in kinds:
        pooled_result[k]['holm_p'] = corrected[k]
    # Standard two-sided normal approximation; uses observed forward variance and n.
    power = {}
    for i,b in enumerate(blocks):
        subset = [r['pnl'] for r in b if selected(r, coins, threshold, 'edge')]
        power[str(i+1)] = 2.802 * st.stdev(subset) / math.sqrt(len(subset)) if len(subset)>1 else None
    pooled_edge = [r['pnl'] for r in pooled if selected(r, coins, threshold, 'edge')]
    power['pooled'] = 2.802 * st.stdev(pooled_edge) / math.sqrt(len(pooled_edge))
    days = defaultdict(list)
    for r in rows:
        if r['sym'] in coins:
            from datetime import datetime, timezone
            days[datetime.fromtimestamp(r['time'], timezone.utc).date().isoformat()].append(r['pnl'])
    sensitivity = {}
    for f in (.3, .5):
        c,t = calibrate(rows[:int(len(rows)*f)])
        sensitivity[str(f)] = {'coins': c, 'edge_threshold': t}
    rng = random.Random(seed)
    random_splits=[]
    width=int(len(rows)*.8)
    for _ in range(5):
        start=rng.randrange(len(rows)-width+1)
        sub=rows[start:start+width]
        train, fw=split(sub)
        c,t=calibrate(train)
        random_splits.append({'start':start,'coins':c,'threshold':t,
                              'blocks':{k:[evaluate(b,c,t,k,seed+start+i+j) for i,b in enumerate(fw)] for j,k in enumerate(kinds)},
                              'boundary':{'train_max':train[-1]['time'],'forward_min':fw[0][0]['time']}})
    expanding=[]
    for i,b in enumerate(blocks):
        train=calibration+[r for prior in blocks[:i] for r in prior]
        c,t=calibrate(train)
        expanding.append({'coins':c,'threshold':t,'boundary':{'train_max':train[-1]['time'],'forward_min':b[0]['time']},
                          'results':{k:evaluate(b,c,t,k,seed+300+i*10+j) for j,k in enumerate(kinds)}})
    interactions={}
    for label,predicate in [('selected',lambda r:r['sym'] in coins),('excluded',lambda r:r['sym'] not in coins)]:
        group=[r for r in pooled if predicate(r)]
        high=[r['pnl'] for r in group if r['edge']>=threshold]
        low=[r['pnl'] for r in group if r['edge']<threshold]
        interactions[label]={'high_n':len(high),'high_mean':st.mean(high) if high else None,
                             'low_n':len(low),'low_mean':st.mean(low) if low else None}
    return {'n':len(rows),'calibration':{'n':len(calibration),'coins':coins,'threshold':threshold,
             'max_timestamp':calibration[-1]['time'],'forward_min_timestamp':blocks[0][0]['time']},
            'boundaries':[{'train_max':calibration[-1]['time'],'forward_min':b[0]['time']} for b in blocks],
            'forward':forward,'pooled':pooled_result,'h2_mde_80pct':power,
            'days':{d:{'n':len(v),'mean':st.mean(v)} for d,v in days.items()},
            'sensitivity':sensitivity,'random_contiguous_80pct':random_splits,
            'expanding':expanding,'interaction':interactions}


def shadow_rows(rows, coins, threshold, status='exploratory'):
    return [{'key':r['key'],'filter_coin_pass':r['sym'] in coins,
             'filter_edge_pass':r['edge']>=threshold,'filter_combo_pass':r['sym'] in coins and r['edge']>=threshold,
             'filter_status':status,'calibration_coins':coins,'calibration_edge_threshold':threshold}
            for r in rows]


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--grades',default='data/signal_grades.jsonl')
    p.add_argument('--paper',default='data/quote_side_paper.jsonl')
    p.add_argument('--output',default='data/candidate_filters_paper.jsonl')
    p.add_argument('--summary',default='data/candidate_filters_summary.json')
    a=p.parse_args()
    rows=load(a.grades,a.paper)
    result=run(rows)
    c=result['calibration']
    safe_output(a.output); safe_output(a.summary)
    with open(a.output,'w') as out:
        for row in shadow_rows(rows,c['coins'],c['threshold']): out.write(json.dumps(row)+'\n')
    safe_output(a.summary).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'n':result['n'],'calibration':c,'pooled':result['pooled']},indent=2))
