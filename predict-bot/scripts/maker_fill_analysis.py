"""Counterfactual top-of-book maker scenarios; no orders or live imports."""
import argparse
import json
import math
import statistics as st
from pathlib import Path
from datetime import datetime


def read(path):
    return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]


def safe_output(path):
    if Path(path).name != 'maker_fill_summary.json':
        raise ValueError('protected output path')
    return Path(path)


def fee(price):
    # Source checked against same_day_loop._kalshi_taker_fee_dollars:
    # ceil(0.07 * C * P * (1-P) * 100) / 100, C=1.
    return math.ceil(.07 * price * (1-price) * 100) / 100


def snapshot(yes_bid, yes_ask, side):
    try:
        bid, ask = float(yes_bid), float(yes_ask)
        if not (0 < bid < ask < 1): return None
    except (ValueError, TypeError): return None
    return (bid, ask) if side == 'YES' else (1-ask, 1-bid)


def fillable(limit, quote, scenario):
    bid, ask = quote
    if scenario == 'optimistic': return bid >= limit
    if scenario == 'mid': return ask <= limit
    if scenario == 'pessimistic': return ask < limit
    raise ValueError(scenario)


def pnl(entry, exit_price, early):
    return exit_price - entry - fee(entry) - (fee(exit_price) if early else 0)


def analyze(grades, paper, exits):
    gmap={g['key']:g for g in read(grades) if g.get('proxy')=='kalshi'}
    emap={e['signal_key']:e for e in read(exits)}
    records=[]
    for p in read(paper):
        if p['executable_pnl_net'] is None or p['key'] not in gmap: continue
        g=gmap[p['key']]; e=emap.get(p['key'])
        entry=snapshot(g.get('bid'),g.get('ask'),g['side'])
        if entry is None: continue
        later=snapshot(e.get('exit_yes_bid'),e.get('exit_yes_ask'),g['side']) if e else None
        records.append((g,p,e,entry,later))
    result={'paired_n':len(records),'taker_mean':st.mean(x[1]['executable_pnl_net'] for x in records),
            'exit_snapshot_n':sum(x[4] is not None for x in records),'scenarios':{},
            'scenario_type':'quote-rule arithmetic counterfactual; snapshots do not prove fills or executable timing',
            'fee_source':'same_day_loop._kalshi_taker_fee_dollars: ceil(0.07*C*P*(1-P)*100)/100; maker schedule absent; identical fee assumed',
            'latency':{'30s':None,'60s':None,'120s':None,'reason':'No entry timestamped quote sequence with snapshots at requested offsets.'}}
    for ladder in ('join_bid','improve_1c'):
        for scenario in ('optimistic','mid','pessimistic'):
            eligible=[]; filled=[]; winner_n=winner_missed=0; missed_payoff=0
            full=[]; half_fallback=[]; half_unfilled=[]; capture=[]; drift=[]
            for g,p,e,entry,later in records:
                limit=min(entry[0]+(.01 if ladder=='improve_1c' else 0),entry[1])
                # Only a future snapshot can establish a nontrivial crossing.
                if scenario=='optimistic':
                    q=entry if entry[0]>=limit else later
                    available=q is not None
                else:
                    q=later; available=later is not None
                if not available: continue
                eligible.append(p)
                ok=fillable(limit,q,scenario)
                winner=p['executable_pnl_net']>0
                if winner: winner_n+=1
                if not ok:
                    if winner:
                        winner_missed+=1; missed_payoff+=p['executable_pnl_net']
                    full.append(0)
                    half_fallback.append(p['executable_pnl_net'])
                    half_unfilled.append(0)
                    continue
                filled.append(p)
                exit_price=p['exit_executable_taken']
                maker=pnl(limit,exit_price,e is not None)
                full.append(maker)
                half_fallback.append(.5*maker+.5*p['executable_pnl_net'])
                half_unfilled.append(.5*maker)
                capture.append(entry[1]-limit)
                # Later quote is an exit-time observation, not 30/60s drift.
                if later is not None: drift.append((later[0]+later[1])/2-(entry[0]+entry[1])/2)
            key=f'{ladder}/{scenario}'
            result['scenarios'][key]={'eligible_n':len(eligible),'fill_n':len(filled),
               'fill_rate':len(filled)/len(eligible) if eligible else None,
               'taker_mean_on_eligible':st.mean(p['executable_pnl_net'] for p in eligible) if eligible else None,
               'maker_per_signal_mean':st.mean(full) if full else None,
               'half_fallback_mean':st.mean(half_fallback) if half_fallback else None,
               'half_unfilled_mean':st.mean(half_unfilled) if half_unfilled else None,
               'unfilled_winner_n':winner_missed,'taker_winner_n':winner_n,
               'unfilled_winner_fraction':winner_missed/winner_n if winner_n else None,
               'missed_winner_pnl_total':missed_payoff,
               'quoted_spread_capture_mean_filled':st.mean(capture) if capture else None,
               'later_mid_drift_mean_filled':st.mean(drift) if drift else None}
    return result


if __name__=='__main__':
    a=argparse.ArgumentParser()
    a.add_argument('--grades',default='data/signal_grades.jsonl')
    a.add_argument('--paper',default='data/quote_side_paper.jsonl')
    a.add_argument('--exits',default='data/exit_events.jsonl')
    a.add_argument('--summary',default='data/maker_fill_summary.json')
    x=a.parse_args()
    result=analyze(x.grades,x.paper,x.exits)
    safe_output(x.summary).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
