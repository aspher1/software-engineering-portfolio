"""Deterministic offline E1 runner; no bot imports or order path."""
import json
from pathlib import Path
import re
from .wallets import screen_wallet, SOURCE_URL
from .copytrade import CopyConfig, DELAYS, generate_opportunities, delay_decay
from .pnl import paper_pnl, summarize
from .sources import valid_trade

SEED = 20260927


def run_e1(wallets, source, price_path, stats_by_address, since, until, sol_usd,
           output_path=None, allow_unvetted=False):
    """wallets are resolved addresses. Source and price path are injected.

    All results are paper estimates. A missing price skips a close and is counted.
    STOP_ALL and KILL_WALLET stop *new entries* in chronological OOS replay;
    a triggering close itself remains included.
    """
    result = {'seed': SEED, 'source_url': SOURCE_URL, 'wallets': {}, 'delays_s': list(DELAYS),
              'status': 'not_runnable', 'global': {}}
    prepared = {}
    for address in wallets:
        stats = stats_by_address.get(address)
        passed, reasons = screen_wallet(stats)
        row = {'address': address, 'screen_pass': passed, 'screen_reasons': reasons,
               'status': 'not_vetted' if not passed else 'insufficient_history'}
        result['wallets'][address] = row
        if (not passed and not allow_unvetted) or not isinstance(address, str) or not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', address):
            row['status'] = 'address_pending_resolution' if passed else row['status']
            continue
        try:
            trades = source.get_trades(address, since, until)
        except Exception:
            row['status'] = 'source_error'
            continue
        invalid = sum(not valid_trade(t) for t in trades)
        row['invalid_leader_trades'] = invalid
        trades = [t for t in trades if valid_trade(t)]
        trades = sorted(trades, key=lambda t: (t.ts, t.tx_sig))
        row['leader_trades'] = len(trades)
        if len(trades) < 100:
            continue
        cutoff = (2 * len(trades)) // 3
        is_sigs = {t.tx_sig for t in trades[:cutoff]}
        prepared[address] = (trades, is_sigs)
        row['status'] = 'runnable' if passed else 'paper_only_unvetted'
    if prepared:
        result['status'] = 'runnable'
    for delay in DELAYS if prepared else ():
        config = CopyConfig(delay)
        all_oos = []
        for address, (trades, is_sigs) in prepared.items():
            generated = generate_opportunities(trades, price_path, config)
            row = result['wallets'][address]
            row.setdefault('delays', {})[str(delay)] = {'counts': generated.counts}
            is_values = []
            for op in generated.opportunities:
                rate = sol_usd(op.buy_ts) if callable(sol_usd) else sol_usd
                value = paper_pnl(op, config, rate)
                if value is None:
                    row['delays'][str(delay)]['counts']['invalid_pnl'] = row['delays'][str(delay)]['counts'].get('invalid_pnl', 0) + 1
                elif op.buy_sig in is_sigs:
                    is_values.append(value)
                else:
                    all_oos.append((op.buy_ts, op.sell_ts, address, op.buy_sig, value))
            row['delays'][str(delay)]['IS'] = summarize(is_values)
        all_oos.sort(key=lambda x: (x[0], x[3]))
        # Entry eligibility uses only prior closed OOS results. Closes are processed
        # by time before each candidate entry; overlapping positions remain open.
        pending = []
        accepted = {address: [] for address in prepared}
        killed = set()
        stopped = False
        cumulative = 0.0
        def settle(before):
            nonlocal cumulative, stopped
            ready = sorted((p for p in pending if p[0] <= before), key=lambda p: (p[0], p[2]))
            for close_ts, address, sig, value in ready:
                pending.remove((close_ts, address, sig, value))
                accepted[address].append(value)
                cumulative += value
                if len(accepted[address]) >= 30 and sum(accepted[address]) <= 0:
                    killed.add(address)
                if cumulative <= -21:
                    stopped = True
        skipped_stop = skipped_kill = 0
        for entry_ts, close_ts, address, sig, value in all_oos:
            settle(entry_ts)
            if stopped:
                skipped_stop += 1
            elif address in killed:
                skipped_kill += 1
            else:
                pending.append((close_ts, address, sig, value))
        settle(float('inf'))
        result['global'][str(delay)] = {'stop_all': stopped, 'cumulative_oos_net_usd': cumulative,
                                         'skipped_after_stop': skipped_stop, 'skipped_after_kill': skipped_kill}
        for address in prepared:
            row = result['wallets'][address]['delays'][str(delay)]
            row['OOS'] = summarize(accepted[address])
            row['decision'] = ('KILL_WALLET' if address in killed else
                               'INSUFFICIENT_OOS_CLOSES' if len(accepted[address]) < 30 else 'CONTINUE')
            row['stop_all'] = stopped
    for address in prepared:
        row = result['wallets'][address]
        row['delay_decay_oos'] = delay_decay({int(k): v['OOS'] for k, v in row['delays'].items()})
    if output_path is not None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + '\n')
    return result


def write_run_log(path, resolved_wallets):
    """Record address provenance before an experiment. No address inference."""
    Path(path).write_text(json.dumps({'source_url': SOURCE_URL, 'resolved_wallets': resolved_wallets,
                                      'seed': SEED}, indent=2, sort_keys=True) + '\n')
