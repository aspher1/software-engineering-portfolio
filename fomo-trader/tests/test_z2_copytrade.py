"""Hermetic E1 validation; no bot import and no network."""
import json
from math import nan
from analysis.z2_copytrade.wallets import screen_wallet, CANDIDATES
from analysis.z2_copytrade.sources import LeaderTrade, TapeSource, CachedSource, SolscanSource, TokenBucket
from analysis.z2_copytrade.copytrade import CopyConfig, generate_opportunities, delay_decay
from analysis.z2_copytrade.pnl import paper_pnl, summarize
from analysis.z2_copytrade.e1 import run_e1
from analysis.z2_copytrade.prices import GeckoTerminalPricePath


class Prices:
    def price_at(self, mint, ts):
        return None if mint == 'missing' else (2 if ts >= 200 else 1)


def trade(side, ts, sig, mint='M', wallet='A' * 32, price=1):
    return LeaderTrade(wallet, mint, side, ts, price, 1, sig)


def vetted():
    return {'round_trips': 100, 'window_days': 3, 'distinct_creators': 120,
            'net_sol': 10, 'top3_net_sol': 2}


def test_screen_fail_closed():
    assert screen_wallet(vetted())[0]
    for value in (None, nan, -1, 19):
        s = vetted() | {'distinct_creators': value}
        assert not screen_wallet(s)[0]
    assert 'insider_pattern' in screen_wallet(vetted() | {'distinct_creators': 19})[1]
    assert 'top3_over_half_net' in screen_wallet(vetted() | {'top3_net_sol': 6})[1]
    assert all(w.address is None and w.status == 'address_pending_resolution' for w in CANDIDATES)


def test_adversarial_generation():
    trades = [trade('SELL', 240, 's'), trade('BUY', 100, 'b'),
              trade('BUY', 100, 'b'), trade('BUY', 100, 'x', 'missing'),
              trade('SELL', 240, 'y', 'missing'), trade('BUY', nan, 'bad'),
              trade('SELL', 250, 'orphan')]
    got = generate_opportunities(trades, Prices(), CopyConfig(30))
    assert len(got.opportunities) == 1
    assert got.counts['out_of_order'] == 1
    assert got.counts['duplicate_tx_sig'] == 1
    assert got.counts['invalid_trade'] == 1
    assert got.counts['missing_or_invalid_price'] == 1
    assert got.counts['unmatched_sell'] == 1
    assert generate_opportunities([], Prices(), CopyConfig(30)).opportunities == []
    class NanPrices:
        def price_at(self, mint, ts): return nan
    assert generate_opportunities([trade('BUY', 100, 'b'), trade('SELL', 240, 's')], NanPrices(), CopyConfig(30)).counts['missing_or_invalid_price'] == 1


def test_minute_close_is_after_decision():
    def fake(url):
        return {'data': {'attributes': {'ohlcv_list': [[60, 1, 1, 1, 5, 1], [120, 1, 1, 1, 7, 1]]}}}
    path = GeckoTerminalPricePath({'M': 'pool'}, requester=fake)
    assert path.price_at('M', 121) == 7  # close at 180, not open at 120
    assert path.price_at('M', 181) is None
    assert path.missing == 1


def test_network_adapters_are_fail_open_and_bucket_capped():
    source = SolscanSource(requester=lambda url: (_ for _ in ()).throw(OSError('offline')))
    assert source.get_trades('A'*32, 0, 100) == []
    assert source.errors == 1
    assert source.get_trades('A'*32, 0, 100) == []
    assert source.errors == 2
    assert TokenBucket(50).capacity == 20
    path = GeckoTerminalPricePath({'M': 'pool'}, requester=lambda url: (_ for _ in ()).throw(OSError('offline')))
    assert path.price_at('M', 100) is None
    assert path.errors == 1


def test_costs_and_decay():
    op = generate_opportunities([trade('BUY', 100, 'b'), trade('SELL', 240, 's')], Prices(), CopyConfig(30)).opportunities[0]
    assert round(paper_pnl(op, CopyConfig(30), 100), 3) == round(7 - (7 + 14)*.023 - .4, 3)
    assert paper_pnl(op, CopyConfig(30), nan) is None
    assert summarize([2, -3, 4])['max_dd'] == 3
    decay = delay_decay({5: {'n': 1, 'edge_per_trade': 2}, 15: {'n': 1, 'edge_per_trade': 1}})
    assert decay['edge_half_life_s'] == 25


def test_tape_cache_and_e1(tmp_path):
    address = 'A' * 32
    tape = tmp_path / 'hour.jsonl'
    rows = []
    for i in range(110):
        ts = 1000 + i*200
        rows += [dict(wallet=address, mint='M', side='BUY', ts=ts, price_sol=1, size_sol=1, tx_sig=f'b{i}'),
                 dict(wallet=address, mint='M', side='SELL', ts=ts+100, price_sol=1, size_sol=1, tx_sig=f's{i}')]
    tape.write_text('\n'.join(json.dumps(r) for r in rows) + '\n{bad json}\n')
    source = CachedSource(TapeSource(tape), tmp_path / 'cache')
    assert len(source.get_trades(address, 0, 1e9)) == 220
    assert len(source.get_trades(address, 0, 1e9)) == 220
    out = tmp_path / 'e1_results.json'
    result = run_e1([address], source, Prices(), {address: vetted()}, 0, 1e9, 100, out)
    assert out.exists() and result['status'] == 'runnable'
    assert result['wallets'][address]['delays']['30']['OOS']['n'] == 30  # killed after first 30 losing closes
    assert result['wallets'][address]['delays']['30']['decision'] == 'KILL_WALLET'
    assert result['global']['30']['stop_all'] is True  # $21 drawdown threshold


def test_no_verdict_without_history(tmp_path):
    class Empty:
        def get_trades(self, address, since, until): return []
    result = run_e1(['A'*32], Empty(), Prices(), {'A'*32: vetted()}, 0, 100, 100)
    assert result['status'] == 'not_runnable'
    assert result['wallets']['A'*32]['status'] == 'insufficient_history'
