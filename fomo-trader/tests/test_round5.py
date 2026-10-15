import json
import pytest

from analysis import replay
from analysis.round5 import round5 as r5


def row(kind, mint='M', chain='bsc', **extra):
    x = {'type': kind, 'mint': mint, 'chain': chain, 'ts': '2026-09-27 00:00:00'}
    x.update(extra)
    return x


def pair(i, pnl=0.01):
    entry = row('entry', str(i), slip_from_signal_pct=.01, liquidity_usd=50000,
                signal_gain_pct=50, m15_buys=20, m15_sells=10, mcap_usd=100000)
    close = row('close', str(i), entry=1, exit=1, buy_sol=.01,
                realized_bnb=pnl, sol_usd=800)
    return {**entry, **close, 'entry_record': entry, 'close_record': close, 'chain': 'bsc'}


def test_fifo_pair_by_chain_and_mint(tmp_path):
    p = tmp_path / 'journal.jsonl'
    rows = [row('entry', slip_from_signal_pct=.01, marker=1),
            row('entry', slip_from_signal_pct=.02, marker=2),
            row('entry', chain='solana', slip_from_signal_pct=.03, marker=3),
            row('close', marker=10), row('close', chain='solana', marker=11),
            row('close', marker=12), row('close', mint='orphan')]
    p.write_text('\n'.join(map(json.dumps, rows)))
    pairs, counts = r5.load_pairs(p)
    assert [(x['entry_record']['marker'], x['close_record']['marker']) for x in pairs] == [(1, 10), (3, 11), (2, 12)]
    assert counts['unmatched_closes'] == 1


def test_only_enriched_entry_qualifies(tmp_path):
    p = tmp_path / 'journal.jsonl'
    p.write_text('\n'.join(map(json.dumps, [row('entry'), row('close')])))
    assert r5.load_pairs(p)[0] == []


def test_replay_cost_model_reused_and_all_costs_tripled():
    t = pair(1)
    assert r5.summarize([t]) == replay.metrics([t])
    normal = replay.net_pnl(t)[1]
    stress = replay.net_pnl(t, cost_multiplier=3)[1]
    assert r5.summarize([t], cost_multiplier=3)['usd'] == stress
    assert normal - stress == pytest.approx(2 * replay.estimated_cost(t) * 800)


def test_chronological_split_never_reorders():
    rows = [pair(i) for i in range(31)]
    a, b = r5.split_chronological(rows)
    assert [t['mint'] for t in a] == list(map(str, range(20)))
    assert [t['mint'] for t in b] == list(map(str, range(20, 31)))


def test_data_trigger_gates_and_ship_freeze():
    assert r5.significance_ok(19, 10) == 'awaiting_data'
    assert r5.significance_ok(20, 10) == 'provisional'
    assert r5.significance_ok(99, 50) == 'provisional'
    assert r5.significance_ok(100, 30) == 'verdict_capable'
    assert r5.evaluate([pair(i) for i in range(29)])['candidates'] == {}
    assert r5.evaluate([pair(i) for i in range(150)])['ship_recommend'] is False


def test_retention_and_stress_math():
    assert r5.oos_retention_ok(1, .6)
    assert not r5.oos_retention_ok(1, .599)
    assert not r5.oos_retention_ok(0, 1)
    assert r5.three_x_cost_stress([pair(1, 1)])
    assert not r5.three_x_cost_stress([pair(1, 0)])


def test_frozen_candidates_veto_only_strict_cutoffs():
    assert r5.CANDIDATES['chase'] == ('slip_from_signal_pct', .10)
    assert not r5.veto('chase', {'slip_from_signal_pct': .10})
    assert r5.veto('chase', {'slip_from_signal_pct': .10001})
    assert r5.veto('commit_liquidity', {'liquidity_usd': 39999})
    assert not r5.veto('commit_liquidity', {'liquidity_usd': 40000})
    assert not r5.veto('chase', {})
    assert r5.PREREGISTRATION.exists()
    r5.verify_preregistration()
    assert 'Frozen **2026-09-28 00:06:52 UTC' in r5.PREREGISTRATION.read_text()


def test_kill_and_stop_all_precedence():
    assert r5.kill_decision(-100, 29, -100) == 'INSUFFICIENT_OOS_HISTORY'
    assert r5.kill_decision(1, 30, -21) == r5.STOP_ALL
    assert r5.kill_decision(0, 30, 0) == r5.KILL_VARIANT
    assert r5.kill_decision(1, 30, 1) == 'CONTINUE'


def test_holder_not_runnable_without_observed_fields():
    result = r5.evaluate([pair(i) for i in range(30)])
    assert result['candidates']['holder_concentration']['status'] == 'not_runnable'


def test_preregistration_tamper_fails_closed(tmp_path, monkeypatch):
    altered = tmp_path / 'prereg.md'
    altered.write_text(r5.PREREGISTRATION.read_text() + '\nchanged')
    monkeypatch.setattr(r5, 'PREREGISTRATION', altered)
    with pytest.raises(RuntimeError, match='pre-registration changed'):
        r5.evaluate([])
