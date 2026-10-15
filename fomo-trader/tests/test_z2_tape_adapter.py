"""Hermetic tests for the staged pumpapi tape adapter and runner helpers; no network."""
import base64
from datetime import datetime, timezone
import json
from math import nan

import pytest

from analysis.z2_copytrade.e1 import run_e1
from analysis.z2_copytrade.sources import LeaderTrade
from analysis.z2_copytrade.tape_adapter import (FilteredTapeSource, TapePrintPricePath, _signature,
                                                _timestamp, attributed_wallet)
from analysis.z2_copytrade.run_tape_e1 import (DailySolUsd, completed_hours, hour_start, journal_baseline,
                                               load_reference_signatures, signature_crosscheck,
                                               tape_screen_stats)

TARGET = '24678QKx2Dy8ZCw6Ra8o9DeTqPLL5GR9ZQKxt5FddHmq'
LEADER = 'E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz'
DRAFTER = '57stAMFvwctAjkBS76RXGoK4QKyS1QoxbGMbzFFe4DyZ'
OTHER = 'TEMPaMeCRFAS9EKF53Jd6KpHxgL47uWLcpFArU1Fanq'
SIG = '23Tj34wt2rxCAUy9RCHixNCPc5QeUZRaZzNdfeECboAep6BzS3XnPiCrwigyVKGFxX84FotaiZysC5CXRzxFeUz3'
ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
TS_MS = 1786579232902  # 2026-08-13T00:00:32.902Z, from the staged tape


def sig(i):
    return SIG[:-2] + ALPHABET[i // 58] + ALPHABET[i % 58]


def row(action='buy', signer=TARGET, ts_ms=TS_MS, signature=SIG, mint='MintA',
        price=4.654162201415498e-08, quote=0.765522488, **extra):
    r = {'signature': signature, 'action': action, 'txSigner': signer, 'mint': mint, 'price': price,
         'quoteAmount': quote, 'tokenAmount': 16779996.964059, 'timestamp': ts_ms, 'block': 438910735}
    r.update(extra)
    return r


def write_hour(root, relative, rows, tail=''):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows) + tail)
    return path


def test_verified_schema_maps_to_leader_trade(tmp_path):
    write_hour(tmp_path, '2026-08-13/00.jsonl', [row()])
    source = FilteredTapeSource(tmp_path)
    [trade] = source.get_trades(TARGET, 0, 2e9)
    assert (trade.wallet, trade.mint, trade.side, trade.tx_sig, trade.slot) == (TARGET, 'MintA', 'BUY', SIG, 438910735)
    assert trade.ts == pytest.approx(1786579232.902)
    assert source.creator_by_wallet == {}


def test_price_and_size_units(tmp_path):
    write_hour(tmp_path, '2026-08-13/00.jsonl', [row()])
    [trade] = FilteredTapeSource(tmp_path).get_trades(TARGET, 0, 2e9)
    effective = 0.765522488 / 16779996.964059
    assert trade.price_sol == 4.654162201415498e-08  # curve spot `price`, SOL per token
    assert abs(trade.price_sol - effective) / effective > 0.01  # not quoteAmount / tokenAmount
    assert trade.size_sol == 0.765522488  # quoteAmount, SOL


def test_millisecond_and_second_timestamps():
    assert _timestamp(TS_MS) == pytest.approx(1786579232.902)
    assert _timestamp(1786579232) == 1786579232
    assert _timestamp('2026-08-13T00:00:32Z') == 1786579232
    assert datetime.fromtimestamp(_timestamp(TS_MS), timezone.utc).strftime('%Y-%m-%d') == '2026-08-13'


def test_txsigner_attribution_ignores_guessed_keys(tmp_path):
    assert attributed_wallet(row(transfers=[{'from': LEADER, 'to': OTHER}])) == TARGET
    decoy = row(signer=OTHER, wallet=TARGET, trader=TARGET, signature=sig(1))
    decoy['signer'] = TARGET
    write_hour(tmp_path, '2026-08-13/00.jsonl', [decoy])
    source = FilteredTapeSource(tmp_path)
    assert source.get_trades(TARGET, 0, 2e9) == []
    assert source.counts['unattributed_trade_row'] == 1
    assert len(source.market_prints) == 1  # still a usable market print


def test_transfer_party_fallback(tmp_path):
    to_leader = row(signer=OTHER, signature=sig(1),
                    transfers=[{'from': OTHER, 'to': 'X' * 32}, {'from': OTHER, 'to': LEADER}])
    from_drafter = row(action='sell', signer=OTHER, signature=sig(2),
                       transfers=[{'from': DRAFTER, 'to': TARGET}])
    assert attributed_wallet(to_leader) == LEADER
    assert attributed_wallet(from_drafter) == DRAFTER  # first known party, `from` before `to`
    assert attributed_wallet(row(signer=OTHER, transfers='bad')) is None
    write_hour(tmp_path, '2026-08-13/00.jsonl', [to_leader, from_drafter])
    source = FilteredTapeSource(tmp_path)
    assert [t.side for t in source.get_trades(LEADER, 0, 2e9)] == ['BUY']
    assert [t.side for t in source.get_trades(DRAFTER, 0, 2e9)] == ['SELL']


def test_unattributed_rows_skipped_and_counted(tmp_path):
    only_breakdown = row(signer=OTHER, breakdown=[{'action': 'buy', 'trader': TARGET}],
                         transfers=[{'from': OTHER, 'to': 'X' * 32}])
    write_hour(tmp_path, '2026-08-13/00.jsonl', [only_breakdown])
    source = FilteredTapeSource(tmp_path)
    assert source.get_trades(TARGET, 0, 2e9) == []
    assert source.counts['unattributed_trade_row'] == 1
    assert source.counts['valid_trades'] == 0


def test_non_trade_actions_skipped(tmp_path):
    rows = [row(action='transfer', transfers=[{'from': TARGET, 'to': OTHER}]),  # same signature as the buy
            row(), row(action='create', signature=sig(1)), row(action='migrate', signature=sig(2)),
            row(action=None, signature=sig(3))]
    write_hour(tmp_path, '2026-08-13/00.jsonl', rows)
    source = FilteredTapeSource(tmp_path)
    assert [t.side for t in source.get_trades(TARGET, 0, 2e9)] == ['BUY']
    assert source.counts['non_trade_action'] == 4
    assert source.counts['non_trade_action:transfer'] == 1
    assert source.counts['non_trade_action:missing'] == 1
    assert source.all_sigs[TARGET].keys() == {SIG, sig(1), sig(2), sig(3)}


def test_incomplete_rows_counted(tmp_path):
    rows = [row(price=None, signature=sig(1)), row(price=0, signature=sig(2)),
            row(quote=None, signature=sig(3)), row(ts_ms=None, signature=sig(4)),
            row(signature='23Tj34wt2rxC...'), row(mint=None, signature=sig(5)),
            row(ts_ms='not-a-time', signature=sig(6)), row(signature=sig(7))]
    write_hour(tmp_path, '2026-08-13/00.jsonl', rows, tail='[1, 2]\n{"signature": "23Tj')
    source = FilteredTapeSource(tmp_path)
    assert len(source.get_trades(TARGET, 0, 2e9)) == 1
    assert source.counts['incomplete_trade'] == 7
    assert source.counts['invalid_object'] == 1
    assert source.counts['incomplete_or_invalid_json'] == 1


def test_signature_normalization(tmp_path):
    assert _signature(SIG) == SIG
    one = bytes(63) + b'\x01'
    assert _signature(bytes(64).hex()) == '1' * 64
    assert _signature(base64.b64encode(one).decode()) == '1' * 63 + '2'
    assert _signature(one.hex()) == '1' * 63 + '2'
    raw = bytes([7]) * 64
    assert _signature(raw.hex()) == _signature(base64.b64encode(raw).decode())
    for bad in ('23Tj34wt2rxC...', '23Tj…', '', None, 'a b', base64.b64encode(bytes(62)).decode()):
        with pytest.raises(ValueError):
            _signature(bad)
    write_hour(tmp_path, '2026-08-13/00.jsonl', [row(signature=raw.hex())])
    [trade] = FilteredTapeSource(tmp_path).get_trades(TARGET, 0, 2e9)
    assert trade.tx_sig == _signature(raw.hex()) and set(trade.tx_sig) <= set(ALPHABET)


def test_rescan_and_include_filter(tmp_path):
    write_hour(tmp_path, '2026-08-13/00.jsonl', [row()])
    source = FilteredTapeSource(tmp_path)
    assert len(source.get_trades(TARGET, 0, 2e9)) == 1
    write_hour(tmp_path, '2026-08-13/01.jsonl', [row(action='sell', signature=sig(1), ts_ms=TS_MS + 3_600_000)])
    assert len(source.get_trades(TARGET, 0, 2e9)) == 2
    assert source.rows_by_file == {'2026-08-13/00.jsonl': 1, '2026-08-13/01.jsonl': 1}
    assert len(FilteredTapeSource(tmp_path, include={'2026-08-13/00.jsonl'}).get_trades(TARGET, 0, 2e9)) == 1
    assert source.get_trades(TARGET, 0, 1786579232) == []  # window filter


def test_price_path_first_print_at_or_after_within_cap():
    prints = [LeaderTrade('w', 'M', 'BUY', 100, 1.0, 1, 'a'), LeaderTrade('w', 'M', 'SELL', 110, 2.0, 1, 'b'),
              LeaderTrade('w', 'M', 'BUY', 300, 3.0, 1, 'c')]
    path = TapePrintPricePath(prints)
    assert path.price_at('M', 100) == 1.0
    assert path.price_at('M', 100.5) == 2.0  # no lookback
    assert path.price_at('M', 111) is None  # next print is 189 s later
    assert path.price_at('M', 250) == 3.0
    assert path.price_at('M', 301) is None
    assert path.price_at('N', 100) is None
    assert path.price_at('M', nan) is None
    assert path.missing == 4
    assert TapePrintPricePath(prints, max_wait_s=500).price_at('M', 111) == 3.0


class AlwaysOne:
    def price_at(self, mint, ts):
        return 1.0


def test_run_e1_unvetted_paper_replay(tmp_path):
    rows = []
    for i in range(60):
        t = TS_MS + i * 100_000
        rows += [row(signature=sig(2 * i), ts_ms=t), row(action='sell', signature=sig(2 * i + 1), ts_ms=t + 20_000)]
    write_hour(tmp_path, '2026-08-13/00.jsonl', rows)
    source = FilteredTapeSource(tmp_path)
    stats = {TARGET: tape_screen_stats([t for t in source.scan() if t.wallet == TARGET])}
    assert stats[TARGET]['round_trips'] == 60 and stats[TARGET]['distinct_creators'] is None

    blocked = run_e1([TARGET], source, AlwaysOne(), stats, 0, 2e9, 80)
    assert blocked['status'] == 'not_runnable'
    assert blocked['wallets'][TARGET]['status'] == 'not_vetted' and 'delays' not in blocked['wallets'][TARGET]

    result = run_e1([TARGET], source, AlwaysOne(), stats, 0, 2e9, lambda ts: 80.0, allow_unvetted=True)
    wallet = result['wallets'][TARGET]
    assert wallet['status'] == 'paper_only_unvetted' and wallet['leader_trades'] == 120
    assert not wallet['screen_pass'] and 'invalid_distinct_creators' in wallet['screen_reasons']
    assert wallet['delays']['30']['IS']['n'] == 40 and wallet['delays']['30']['OOS']['n'] == 20
    assert wallet['delays']['30']['decision'] == 'INSUFFICIENT_OOS_CLOSES'

    null_rate = run_e1([TARGET], source, AlwaysOne(), stats, 0, 2e9, DailySolUsd({'2026-08-13': None}),
                       allow_unvetted=True)
    assert null_rate['wallets'][TARGET]['delays']['30']['counts']['invalid_pnl'] == 60


def test_daily_sol_usd_and_hours():
    rate = DailySolUsd({'2026-08-13': 75.56, '2026-08-23': None})
    assert rate(_timestamp(TS_MS)) == 75.56
    assert rate(datetime(2026, 8, 23, 12, tzinfo=timezone.utc).timestamp()) is None
    assert rate.missing == {'2026-08-23': 1}
    text = ('t hour 2026/08/13/00: ok=True kept=411\n'
            't WARN 2026/08/13/01: curl rc=28, kept=1/2\nt hour 2026/08/13/01: ok=True kept=1\n'
            't hour 2026/08/13/02: ok=False kept=0\n')
    assert completed_hours(text) == ({'2026-08-13/00.jsonl'}, {'2026-08-13/01.jsonl'})
    assert hour_start('2026-08-13/00.jsonl') == 1786579200


def test_reference_signature_shapes_and_crosscheck(tmp_path):
    for payload in ([{'signature': sig(1), 'blockTime': 5, 'err': None}],
                    {'result': [{'signature': sig(1), 'blockTime': 5, 'err': None}]}):
        path = tmp_path / 'sigs.json'
        path.write_text(json.dumps(payload))
        assert load_reference_signatures(path) == {sig(1): (5, False)}
    path.write_text(json.dumps([sig(2)]))
    assert load_reference_signatures(path) == {sig(2): (None, False)}

    t = TS_MS / 1000
    write_hour(tmp_path / 'tape', '2026-08-13/00.jsonl',
               [row(signature=sig(1)), row(action='transfer', signature=sig(2), ts_ms=TS_MS + 1000)])
    source = FilteredTapeSource(tmp_path / 'tape')
    source.scan()
    reference = {sig(1): (t, False), sig(3): (t + 2, True), sig(4): (t + 5000, False)}
    check = signature_crosscheck(source, reference, [(t - 10, t + 3590)], TARGET)
    assert check['reference_sigs_in_processed_hours'] == 2 and check['matched'] == 1
    assert check['recall_all_rows'] == 0.5 and check['recall_successful_only'] == 1.0
    assert check['recall_trade_rows'] == 0.5 and check['tape_sigs_found_in_reference'] == 0.5


def test_journal_baseline(tmp_path):
    rows = [{'type': 'entry', 'mint': 'A', 'buy_sol': 0.06},
            {'type': 'close', 'mint': 'A', 'realized_sol': 0.01},
            {'type': 'close', 'mint': 'B', 'realized_sol': -0.02, 'sol_usd': 100},
            {'type': 'close', 'mint': 'C', 'chain': 'solana', 'realized_sol': -0.008, 'realized_usd': -1.0, 'sol_usd': 120},
            {'type': 'close', 'mint': '0xabc', 'chain': 'bsc', 'realized_sol': None, 'realized_bnb': -0.002,
             'realized_usd': -1.5, 'sol_usd': 776}]
    path = tmp_path / 'trades.jsonl'
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows) + '{bad\n')
    b = journal_baseline(path)
    assert b['closes'] == 4 and b['closes_by_chain'] == {'solana': 3, 'bsc': 1}
    assert b['net_sol_solana'] == pytest.approx(-0.018) and b['net_bnb_bsc'] == pytest.approx(-0.002)
    assert b['net_usd'] == pytest.approx(-4.5)
    assert (b['usd_reported_rows'], b['usd_derived_rows'], b['unconverted_rows']) == (2, 1, 1)
    assert b['unconverted_sol'] == pytest.approx(0.01) and b['invalid_lines'] == 1
    assert 'NOT an E1 input' in b['label']
