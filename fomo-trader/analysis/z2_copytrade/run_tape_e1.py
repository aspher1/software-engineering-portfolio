"""E1 offline paper replay over the staged, wallet-filtered pumpapi tape.

Research only: no bot imports, no order path, no network. From the repo root:

    PYTHONPATH=. .venv/bin/python -m analysis.z2_copytrade.run_tape_e1

Only hours the downloader has reported complete (``ok=True`` without a curl
WARN) are processed; the exact list is written to the results JSON and RUNS.md.
"""
import argparse
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
import re

from .copytrade import CopyConfig
from .e1 import SEED, run_e1
from .tape_adapter import KNOWN_WALLETS, FilteredTapeSource, TapePrintPricePath

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
TAPE_BASE = REPO.parent / 'e1-tape'
STAGING = REPO / '.e1_tape_staging'
STATUS = TAPE_BASE / 'DOWNLOAD_STATUS.md'
DONE = TAPE_BASE / 'DOWNLOAD_DONE'
TARGET_SIGS = TAPE_BASE / 'target_sigs.json'
RESEARCH_DOC = TAPE_BASE / 'CLAUDE.md'
BASELINE = REPO / 'runs' / 'paper-1h' / 'trades.jsonl'
OUTPUT = HERE / 'e1_tape_results.json'
RUNS_MD = HERE / 'RUNS.md'
TAPE_URL = 'https://replay.pumpapi.io/{YYYY}/{MM}/{DD}/{HH}.jsonl.zst'
RESEARCH_URL = 'https://github.com/d3ad-e/solana-sniper-bot/blob/HEAD/CLAUDE.md'
TARGET, LEADER, DRAFTER = KNOWN_WALLETS
LABELS = {TARGET: 'target', LEADER: 'leader', DRAFTER: 'drafter'}
# CoinGecko daily SOL/USD; 08-23 was not retrieved and stays null.
SOL_USD_BY_DAY = {'2026-08-13': 75.56, '2026-08-20': 85.33, '2026-08-23': None, '2026-08-24': 95.41}
ANCHOR_DELAY = '30'
HOUR_OK = re.compile(r'hour (\d{4})/(\d{2})/(\d{2})/(\d{2}): ok=True')
HOUR_BAD = re.compile(r'(?:WARN|ERROR) (\d{4})/(\d{2})/(\d{2})/(\d{2}):')


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if _num(ts) else None


def _ratio(num, den):
    return num / den if den else None


def completed_hours(status_text):
    """Return (complete, truncated) relative hour paths parsed from DOWNLOAD_STATUS.md."""
    ok = {f'{m[1]}-{m[2]}-{m[3]}/{m[4]}.jsonl' for m in HOUR_OK.finditer(status_text)}
    bad = {f'{m[1]}-{m[2]}-{m[3]}/{m[4]}.jsonl' for m in HOUR_BAD.finditer(status_text)}
    return ok - bad, bad


def hour_start(relative):
    return datetime.strptime(relative, '%Y-%m-%d/%H.jsonl').replace(tzinfo=timezone.utc).timestamp()


class DailySolUsd:
    """Per-UTC-day SOL/USD; a null day returns None so the close is skipped and counted."""

    def __init__(self, table):
        self.table = dict(table)
        self.missing = Counter()

    def __call__(self, ts):
        day = datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d')
        rate = self.table.get(day)
        if rate is None:
            self.missing[day] += 1
        return rate


def tape_screen_stats(trades):
    open_buys = defaultdict(deque)
    round_trips = 0
    for t in sorted(trades, key=lambda t: (t.ts, t.tx_sig)):
        if t.side == 'BUY':
            open_buys[t.mint].append(t)
        elif open_buys[t.mint]:
            open_buys[t.mint].popleft()
            round_trips += 1
    times = [t.ts for t in trades]
    return {'round_trips': round_trips,
            'window_days': (max(times) - min(times)) / 86400 if times else None,
            'distinct_creators': None, 'net_sol': None, 'top3_net_sol': None,
            'provenance': 'round_trips (FIFO buy->sell per mint) and window_days from processed tape hours; '
                          'distinct_creators null-carried (no verified creator field in tape schema); '
                          'net_sol/top3_net_sol not derived (laddered exits need full position accounting)'}


def load_reference_signatures(path):
    """Return {signature: (blockTime, failed)} from an RPC-style signature dump."""
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        for key in ('result', 'signatures', 'sigs', 'data'):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = next((v for v in data.values() if isinstance(v, list)), [])
    out = {}
    for item in data if isinstance(data, list) else []:
        if isinstance(item, str):
            out[item] = (None, False)
        elif isinstance(item, dict):
            sig = item.get('signature') or item.get('sig')
            if isinstance(sig, str):
                out[sig] = (item.get('blockTime'), item.get('err') is not None)
    return out


def signature_crosscheck(source, reference, windows, wallet):
    """Recall of reference signatures inside processed hours against the wallet's tape rows."""
    tape = source.all_sigs.get(wallet, {})
    tape_trade = {t.tx_sig for t in source.trades if t.wallet == wallet}

    def inside(ts):
        return _num(ts) and any(a <= ts < b for a, b in windows)

    ref_times = [bt for bt, _ in reference.values() if _num(bt)]
    lo, hi = (min(ref_times), max(ref_times)) if ref_times else (None, None)
    ref_in = {s for s, (bt, _) in reference.items() if inside(bt)}
    ref_ok_in = {s for s in ref_in if not reference[s][1]}
    tape_in = {s for s, ts in tape.items() if inside(ts)}
    tape_in_range = {s for s in tape_in if lo is not None and lo <= tape[s] <= hi}
    return {'wallet': wallet,
            'reference_sigs': len(reference),
            'reference_blocktime_range_utc': [_iso(lo), _iso(hi)],
            'reference_sigs_in_processed_hours': len(ref_in),
            'reference_successful_sigs_in_processed_hours': len(ref_ok_in),
            'tape_sigs_in_processed_hours': len(tape_in),
            'matched': len(ref_in & tape_in),
            'recall_all_rows': _ratio(len(ref_in & tape_in), len(ref_in)),
            'recall_successful_only': _ratio(len(ref_ok_in & tape_in), len(ref_ok_in)),
            'recall_trade_rows': _ratio(len(ref_in & tape_trade), len(ref_in)),
            'tape_sigs_found_in_reference': _ratio(len(tape_in_range & set(reference)), len(tape_in_range)),
            'status': 'ok' if ref_in else 'no_overlap'}


def journal_baseline(path):
    """Bot paper-journal net; a labeled baseline, never an E1 input."""
    out = {'label': 'baseline: bot paper journal runs/paper-1h/trades.jsonl; NOT an E1 input',
           'path': str(path), 'closes': 0, 'closes_by_chain': Counter(), 'net_sol_solana': 0.0,
           'net_bnb_bsc': 0.0, 'net_usd': 0.0, 'usd_reported_rows': 0, 'usd_derived_rows': 0,
           'unconverted_rows': 0, 'unconverted_sol': 0.0, 'invalid_lines': 0}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        out['status'] = 'missing'
        return out
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            out['invalid_lines'] += 1
            continue
        if not isinstance(row, dict) or row.get('type') != 'close':
            continue
        chain = row.get('chain') or ('bsc' if str(row.get('mint', '')).startswith('0x') else 'solana')
        out['closes'] += 1
        out['closes_by_chain'][chain] += 1
        r_sol, r_bnb, r_usd, rate = (row.get(k) for k in ('realized_sol', 'realized_bnb', 'realized_usd', 'sol_usd'))
        if chain == 'solana' and _num(r_sol):
            out['net_sol_solana'] += r_sol
        if chain == 'bsc' and _num(r_bnb):
            out['net_bnb_bsc'] += r_bnb
        if _num(r_usd):
            out['net_usd'] += r_usd
            out['usd_reported_rows'] += 1
        elif chain == 'solana' and _num(r_sol) and _num(rate) and rate > 0:
            out['net_usd'] += r_sol * rate
            out['usd_derived_rows'] += 1
        else:
            out['unconverted_rows'] += 1
            if chain == 'solana' and _num(r_sol):
                out['unconverted_sol'] += r_sol
    out['closes_by_chain'] = dict(out['closes_by_chain'])
    out['status'] = 'ok'
    return out


def drafter_leg():
    parquet = REPO / 'analysis' / 'data' / 'w57st_positions.parquet'
    refs = []
    try:
        refs = [i for i, line in enumerate(RESEARCH_DOC.read_text().splitlines(), 1) if 'w57st_positions' in line]
    except OSError:
        pass
    return {'status': 'present_not_ingested' if parquet.exists() else 'not_runnable',
            'parquet_path': str(parquet), 'parquet_present': parquet.exists(),
            'research_doc': str(RESEARCH_DOC), 'research_doc_reference_lines': refs,
            'reason': 'The research doc publishes only aggregate drafter statistics (754 positions, +960.7 SOL, '
                      '66% win, ROI quantiles, entry offsets). The per-position table it references '
                      '(analysis/data/w57st_positions.parquet) is local to the researcher and absent here. '
                      'No per-trade timestamps/prices are available; no data was invented.'}


def wallet_verdicts(result):
    out = {}
    for address, row in result['wallets'].items():
        per = {}
        for delay, v in (row.get('delays') or {}).items():
            per[delay] = {'oos_net_usd': v['OOS']['net_usd'], 'oos_closes': v['OOS']['n'],
                          'oos_edge_per_trade': v['OOS']['edge_per_trade'], 'oos_pf': v['OOS']['pf'],
                          'is_net_usd': v['IS']['net_usd'], 'is_closes': v['IS']['n'],
                          'decision': v['decision'], 'stop_all': v['stop_all']}
        anchor = per.get(ANCHOR_DELAY)
        verdict = f"NO_VERDICT_{row['status'].upper()}" if anchor is None else anchor['decision']
        if anchor is not None and anchor['stop_all']:
            verdict += '+STOP_ALL'
        out[address] = {'label': LABELS.get(address), 'status': row['status'],
                        'screen_pass': row['screen_pass'], 'screen_reasons': row['screen_reasons'],
                        'leader_trades': row.get('leader_trades'),
                        'verdict_30s_anchor': verdict,
                        'kill_delays': sorted((d for d, v in per.items() if v['decision'] == 'KILL_WALLET'), key=int),
                        'decision_capable': bool(row['screen_pass']) and anchor is not None and anchor['oos_closes'] >= 30,
                        'per_delay': per}
    return out


DEVILS_ADVOCATE = """### Devil's advocate: {label} (`{address}`) survived the kill rule at the 30 s anchor

1. **Fills come from a wallet-conditioned, sparse tape.** The staged files keep only rows that touch the three
   wallets, so the "first print at/after decision time" is usually one of the leaders' own ladder legs, not the
   full market. A real copier competes with every other taker in the same blocks; the fill it would get is not
   observed here, and the leader's own prints can bias the copier's entry and exit in either direction.
2. **The wallet is unvetted.** The screen fails closed (distinct_creators null-carried; net and top-3 net not
   derived), so this is `paper_only_unvetted` and not decision-capable under the pre-registration. The research
   doc says a few outsized positions drive these wallets' net (the drafter's 11 whale positions are 76% of his net);
   the OOS net may rest on a handful of closes that a $7 copier cannot reproduce.
3. **Sample, regime and cost realism.** A few tape hours from four August days are not a regime sample; 30 OOS closes
   is a kill threshold, not evidence of edge. Costs omit Jito tips, failed/landed-late transactions, and the copier's
   own bonding-curve impact, and delays of 5-15 s are not retail-achievable.
"""


def _fmt(value, digits=2):
    return '—' if not _num(value) else f'{value:.{digits}f}'


def runs_md_section(result):
    run = result['run']
    lines = [f"## Run {run['generated_utc']}: tape E1 replay (PAPER only, verdict-only)", '',
             f"- Runner: `analysis/z2_copytrade/run_tape_e1.py`; seed {run['seed']}; DOWNLOAD_DONE present: {run['download_done']}",
             f"- Tape source: {run['tape_url_template']} (filtered to the three wallets by `~/workspace/e1-tape/dl_filter.py`)",
             f"- Hours processed ({len(run['hours_processed'])}): {', '.join(h[:-6] for h in run['hours_processed'])}",
             f"- Hours excluded (in progress / not reported complete): {', '.join(run['hours_excluded']) or 'none'}",
             f"- Hours excluded (truncated download): {', '.join(run['hours_truncated_download']) or 'none'}",
             f"- Window: {run['since_utc']} to {run['until_utc']}",
             f"- Adapter counts: `{json.dumps(run['adapter_counts'], sort_keys=True)}`",
             f"- Price path: {run['price_path']['rule']}; missing lookups {run['price_path']['missing_lookups']}",
             f"- SOL/USD by day: `{json.dumps(run['sol_usd_by_day'], sort_keys=True)}`; missing lookups "
             f"(one per opportunity per delay): `{json.dumps(run['sol_usd_missing_lookups_by_day'], sort_keys=True)}`",
             '', '| wallet | label | screen | status | leader trades | delay s | OOS net $ | OOS closes | IS net $ | decision |',
             '|---|---|---|---|---|---|---|---|---|---|']
    for address, v in result['verdicts'].items():
        screen = 'pass' if v['screen_pass'] else 'FAIL: ' + ', '.join(v['screen_reasons'])
        if not v['per_delay']:
            lines.append(f"| `{address[:6]}…` | {v['label']} | {screen} | {v['status']} | {v['leader_trades']} | — | — | — | — | {v['verdict_30s_anchor']} |")
        for delay in sorted(v['per_delay'], key=int):
            d = v['per_delay'][delay]
            lines.append(f"| `{address[:6]}…` | {v['label']} | {screen} | {v['status']} | {v['leader_trades']} | {delay} | "
                         f"{_fmt(d['oos_net_usd'])} | {d['oos_closes']} | {_fmt(d['is_net_usd'])} | {d['decision']} |")
    lines += ['', 'Global per delay: ' + '; '.join(
        f"{d}s stop_all={g['stop_all']} cum_oos=${_fmt(g['cumulative_oos_net_usd'])}"
        for d, g in sorted(result['global'].items(), key=lambda kv: int(kv[0]))), '']
    lines.append('Per-wallet verdicts (30 s retail anchor): ' + '; '.join(
        f"{v['label']}={v['verdict_30s_anchor']} (decision_capable={v['decision_capable']})"
        for v in result['verdicts'].values()))
    x = result['target_signature_crosscheck']
    lines += ['', f"- Target signature cross-check ({x['status']}): reference {x['reference_sigs']} sigs spanning "
                  f"{x['reference_blocktime_range_utc'][0]} to {x['reference_blocktime_range_utc'][1]}; "
                  f"{x['reference_sigs_in_processed_hours']} in processed hours; matched {x['matched']}; "
                  f"recall all rows {_fmt(x['recall_all_rows'], 3)}, successful only {_fmt(x['recall_successful_only'], 3)}, "
                  f"trade rows {_fmt(x['recall_trade_rows'], 3)}; tape sigs found in reference {_fmt(x['tape_sigs_found_in_reference'], 3)}"]
    d = result['drafter_published_trades_leg']
    lines.append(f"- Drafter published-trades leg: **{d['status']}**. {d['reason']}")
    b = result['baseline_not_e1_input']
    lines.append(f"- Baseline (NOT an E1 input): {b['closes']} closes {b['closes_by_chain']}; net {_fmt(b['net_sol_solana'], 6)} SOL "
                 f"(Solana), {_fmt(b['net_bnb_bsc'], 6)} BNB (BSC); net ${_fmt(b['net_usd'])} over "
                 f"{b['usd_reported_rows'] + b['usd_derived_rows']} USD-valued closes "
                 f"({b['usd_derived_rows']} derived from realized_sol x sol_usd); {b['unconverted_rows']} closes "
                 f"without a USD value ({_fmt(b['unconverted_sol'], 6)} SOL)")
    for address, v in result['verdicts'].items():
        if v['verdict_30s_anchor'].startswith('CONTINUE'):
            lines += ['', DEVILS_ADVOCATE.format(label=v['label'], address=address)]
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=str(OUTPUT))
    parser.add_argument('--no-runs-md', action='store_true')
    args = parser.parse_args(argv)
    status_text = STATUS.read_text() if STATUS.exists() else ''
    complete, truncated = completed_hours(status_text)
    present = {p.relative_to(STAGING).as_posix() for p in STAGING.rglob('*.jsonl')} if STAGING.exists() else set()
    processed = sorted(complete & present)
    if not processed:
        raise SystemExit('no completed tape hours are staged; nothing to replay')
    windows = [(hour_start(h), hour_start(h) + 3600) for h in processed]
    since, until = min(a for a, _ in windows), max(b for _, b in windows)
    source = FilteredTapeSource(STAGING, include=processed)
    source.scan()
    price_path = TapePrintPricePath(source.market_prints)
    stats = {a: tape_screen_stats([t for t in source.trades if t.wallet == a]) for a in KNOWN_WALLETS}
    sol_usd = DailySolUsd(SOL_USD_BY_DAY)
    result = run_e1(list(KNOWN_WALLETS), source, price_path, stats, since, until, sol_usd, allow_unvetted=True)
    cost = CopyConfig(int(ANCHOR_DELAY))
    result['run'] = {
        'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'paper_only': True, 'seed': SEED, 'research_url': RESEARCH_URL,
        'tape_url_template': TAPE_URL, 'staging_root': str(STAGING),
        'download_done': DONE.exists(),
        'hours_processed': processed,
        'hours_excluded': sorted(present - set(processed) - truncated),
        'hours_truncated_download': sorted(truncated & present),
        'since': since, 'until': until, 'since_utc': _iso(since), 'until_utc': _iso(until),
        'rows_by_file': dict(source.rows_by_file), 'adapter_counts': dict(source.counts),
        'wallets': {a: LABELS[a] for a in KNOWN_WALLETS},
        'attribution': 'txSigner if a known wallet, else first known transfers[].from/to party, else skipped and counted',
        'price_field': 'price = post-trade curve spot SOL/token (~vQuoteInBondingCurve/vTokensInBondingCurve); '
                       'effective quoteAmount/tokenAmount not used; size = quoteAmount SOL',
        'price_path': {'rule': 'first same-mint staged print (any wallet) at/after decision ts, within '
                               f'{price_path.max_wait_s} s', 'max_wait_s': price_path.max_wait_s,
                       'missing_lookups': price_path.missing, 'mints': len(price_path.data)},
        'sol_usd_by_day': SOL_USD_BY_DAY, 'sol_usd_missing_lookups_by_day': dict(sol_usd.missing),
        'screen_inputs': stats, 'allow_unvetted': True,
        'costs': {'size_usd_cap': cost.size_usd_cap, 'copy_fee_bps_per_side': cost.copy_fee_bps,
                  'venue_fee_bps_per_side': cost.venue_fee_bps, 'slippage_bps_per_side': cost.slippage_bps,
                  'priority_fee_lamports_per_side': cost.priority_fee_lamports},
    }
    result['verdicts'] = wallet_verdicts(result)
    try:
        reference = load_reference_signatures(TARGET_SIGS)
    except (OSError, ValueError):
        reference = {}
    result['target_signature_crosscheck'] = signature_crosscheck(source, reference, windows, TARGET)
    result['drafter_published_trades_leg'] = drafter_leg()
    result['baseline_not_e1_input'] = journal_baseline(BASELINE)
    output = Path(args.output)
    output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + '\n')
    section = runs_md_section(result)
    if not args.no_runs_md:
        with RUNS_MD.open('a') as stream:
            stream.write('\n' + section)
    print(section)
    return result


if __name__ == '__main__':
    main()
