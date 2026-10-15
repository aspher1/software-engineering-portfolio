"""Hermetic E3 validation; no bot import, no network, synthetic Z4 paths only."""
from datetime import datetime, timedelta
import json
import random

import pytest

from analysis.z5_exit_arch.e3 import (
    AWAITING_DATA, CONTROL, INSUFFICIENT_OOS_HISTORY, KILL_VARIANT, PATH_END, SEED, VARIANTS,
    CostModel, LadderSpec, Position, QuoteRow, RatchetSpec, TimeBoxSpec, VolScaledSpec,
    kill_decisions, load_quote_path, run_e3, simulate_variant, split_is_oos, ts_seconds)

T0 = datetime(2026, 9, 24, 7, 56, 11)
ENTRY_TS = ts_seconds(T0)
COSTS = CostModel(sol_usd=150.0)


def z4_row(offset_s, price, mint='MINT', chain='solana'):
    return {'ts': (T0 + timedelta(seconds=offset_s)).strftime('%Y-%m-%d %H:%M:%S'),
            'mint': mint, 'chain': chain, 'price_usd': price, 'liquidity_usd': None}


def marks(prices, step=120.0, start=0.0):
    """Synthetic marks; the default 120 s spacing keeps the 60 s dump window to one tick."""
    return [QuoteRow(T0 + timedelta(seconds=start + i * step), ENTRY_TS + start + i * step,
                     'MINT', 'solana', float(p)) for i, p in enumerate(prices)]


def sim(path, spec, entry=1.0, costs=COSTS):
    return simulate_variant(path, entry, ENTRY_TS, 10.0, spec, costs)


def reasons(report):
    return [(e.reason, round(e.fraction, 6), e.mark) for e in report.exits]


def test_loader_exact_schema_and_fail_closed_stats(tmp_path):
    good = [z4_row(i * 2, 1.0 + i / 100) for i in range(5)]
    missing = z4_row(20, 1.0)
    del missing['price_usd']
    extra = dict(z4_row(22, 1.5), unexpected='ignored')
    lines = [json.dumps(r) for r in good]
    lines += ['{not json', '[1, 2]', json.dumps(z4_row(12, None)), json.dumps(z4_row(14, None)),
              json.dumps(missing), json.dumps(z4_row(16, 'NaN')), json.dumps(z4_row(18, -1)),
              json.dumps(dict(z4_row(19, 1.0), ts='yesterday')),
              json.dumps(z4_row(19, 1.0, chain='eth')), json.dumps(z4_row(19, 1.0, mint='OTHER')),
              json.dumps(z4_row(1, 1.0)), '', json.dumps(extra)]
    path = tmp_path / 'MINT.jsonl'
    path.write_text('\n'.join(lines) + '\n')
    rows, stats = load_quote_path(path, expected_mint='MINT')
    assert [r.price_usd for r in rows] == [1.0, 1.01, 1.02, 1.03, 1.04, 1.5]
    assert rows[0].mint == 'MINT' and rows[0].chain == 'solana' and rows[0].ts == T0
    assert stats == {'lines': 18, 'rows_ok': 6, 'blank': 1, 'malformed_json': 1, 'not_object': 1,
                     'bad_ts': 1, 'bad_mint': 0, 'mint_mismatch': 1, 'bad_chain': 1,
                     'missing_price': 1, 'null_price': 2, 'invalid_price': 2, 'out_of_order': 1,
                     'extra_fields_ignored': 1, 'file_error': 0}
    rows, stats = load_quote_path(tmp_path / 'absent.jsonl')
    assert rows == [] and stats['file_error'] == 1
    binary = tmp_path / 'bin.jsonl'
    binary.write_bytes(b'\xff\xfe\x00garbage\n' + json.dumps(z4_row(0, 2.0)).encode())
    rows, stats = load_quote_path(binary)
    assert len(rows) == 1 and stats['malformed_json'] == 1


def test_control_ladder_rungs_trail_and_reasons():
    report = sim(marks([1.0, 1.5, 2.0, 2.5, 3.0, 2.6]), LadderSpec())
    assert reasons(report) == [('tp+100', 0.5, 2.0), ('tp+200', 0.125, 3.0),
                               ('trailing_stop', 0.375, 2.6)]
    assert not report.censored
    # Without a fired rung the 30% trail applies, not the 12% post-TP trail.
    assert reasons(sim(marks([1.0, 1.5, 1.2, 1.04]), LadderSpec())) == [('trailing_stop', 1.0, 1.04)]
    # Peak starts at entry, so the 30% trail always pre-empts the 40% hard stop (as live).
    assert reasons(sim(marks([1.0, 0.9, 0.59]), LadderSpec())) == [('trailing_stop', 1.0, 0.59)]
    assert reasons(sim(marks([1.0, 0.9, 0.59]), RatchetSpec())) == [('hard_stop', 1.0, 0.59)]
    dumped = sim(marks([1.0, 1.1, 0.95], step=10), LadderSpec())
    assert reasons(dumped) == [('dump', 1.0, 0.95)]
    stale = sim(marks([1.0, 1.02] * 25, step=60), LadderSpec())
    assert stale.exits[0].reason == 'stale' and stale.exits[0].ts - ENTRY_TS == 45 * 60


def test_ratchet_arms_partials_and_never_arms_below_trigger():
    report = sim(marks([1.0, 1.1, 1.25, 1.4, 1.25, 1.3, 1.16, 1.3, 0.97]), RatchetSpec())
    assert reasons(report) == [('ratchet_partial_1', 0.5, 1.25), ('ratchet_partial_2', 0.25, 1.16),
                               ('ratchet_deep_trail', 0.25, 0.97)]
    flat = sim(marks([1.0, 1.1, 1.19, 1.05, 1.0]), RatchetSpec())
    assert reasons(flat) == [(PATH_END, 1.0, 1.0)] and flat.censored


def test_time_box_exits_at_t_and_trail_fires_earlier():
    flat = sim(marks([1.0] * 26, step=60), TimeBoxSpec())
    assert reasons(flat) == [('time_box', 1.0, 1.0)]
    assert flat.exits[0].ts - ENTRY_TS == 20 * 60
    dropping = sim(marks([1.0, 1.3, 1.2, 1.03, 1.0] + [1.0] * 20), TimeBoxSpec())
    assert reasons(dropping) == [('mark_trail', 1.0, 1.03)]
    assert dropping.exits[0].ts - ENTRY_TS == 360 < 20 * 60


def test_vol_scaled_selects_schedule_by_regime():
    high = [1.0 if i % 2 == 0 else 1.1 for i in range(30)] + [1.12, 1.25]
    report = sim(marks(high, step=10), VolScaledSpec())
    assert report.regime == 'high' and report.vol_per_min > 0.05
    assert [e.reason for e in report.exits[:2]] == ['vol_high_rung_1', 'vol_high_rung_2']
    assert report.exits[0].fraction == pytest.approx(0.20) and report.exits[0].ts - ENTRY_TS == 300
    low = [1.0 if i % 2 == 0 else 1.001 for i in range(30)] + [1.12, 1.31]
    report = sim(marks(low, step=10), VolScaledSpec())
    assert report.regime == 'low' and report.vol_per_min < 0.05
    assert report.exits[0].reason == 'vol_low_rung_1'
    assert report.exits[0].fraction == pytest.approx(0.30) and report.exits[0].mark == 1.31
    sparse = sim(marks([1.0, 1.0, 1.0] + [1.0] * 20, step=120), VolScaledSpec())
    assert sparse.regime == 'low_default'


def test_no_lookahead_prefix_equals_full_path():
    rng = random.Random(SEED)
    for trial in range(40):
        price, prices = 1.0, []
        for _ in range(120):
            price *= 1 + rng.uniform(-0.06, 0.07)
            prices.append(price)
        path = marks(prices, step=rng.choice([2, 10, 30]))
        for name, spec in VARIANTS.items():
            full = sim(path, spec).exits
            for cut in (5, 20, 60, 100):
                t = path[cut - 1].t
                prefix = [e for e in sim(path[:cut], spec).exits if e.reason != PATH_END]
                assert prefix == [e for e in full if e.ts <= t], (trial, name, cut)


def test_cost_accounting_to_the_cent():
    report = sim(marks([1.1] * 12, step=120), TimeBoxSpec())
    # gross 11.00; entry 10*(30+100)bps 0.13; exit 11*(30+500)bps 0.583; priority 2*0.002*150 0.60
    assert report.gross_proceeds_usd == pytest.approx(11.0)
    assert report.costs_usd['priority_fee'] == pytest.approx(0.60)
    assert round(report.net_usd, 2) == round(11.0 - 10 - 0.13 - 0.583 - 0.60, 2) == -0.31
    ladder = sim(marks([1.0, 2.0, 1.6]), LadderSpec())
    # 10.00 @2.0 then 5.00*1.6 = 8.00: gross 18.00; entry 0.13; exit 18*0.053=0.954; priority 3*0.30
    assert [e.reason for e in ladder.exits] == ['tp+100', 'trailing_stop']
    assert ladder.costs_usd['transactions'] == 3
    assert round(ladder.net_usd, 2) == round(18.0 - 10 - 0.13 - 0.954 - 0.90, 2) == 6.02
    stressed = sim(marks([1.1] * 12, step=120), TimeBoxSpec(), costs=COSTS.scaled(3))
    assert round(stressed.net_usd, 2) == round(1.0 - 3 * (0.13 + 0.583 + 0.60), 2)
    with pytest.raises(ValueError):
        CostModel(sol_usd=float('nan'))


def position(i, ts_offset, n_marks=25, prices=None, closed=True):
    prices = prices or [1.0] * n_marks
    return Position(mint='M%02d' % i, chain='solana', entry_ts=ENTRY_TS + ts_offset,
                    entry_price_usd=1.0, marks=tuple(marks(prices, start=ts_offset)), closed=closed)


def test_is_oos_split_chronological_deterministic_no_leakage():
    positions = [position(i, (i // 3) * 600) for i in range(30)]  # ties in groups of three
    is_set, oos_set = split_is_oos(positions)
    assert (len(is_set), len(oos_set)) == (20, 10)
    assert max(p.entry_ts for p in is_set) <= min(p.entry_ts for p in oos_set)
    shuffled = positions[:]
    random.Random(1).shuffle(shuffled)
    again = split_is_oos(shuffled)
    assert [p.mint for p in again[0]] == [p.mint for p in is_set]
    assert [p.mint for p in again[1]] == [p.mint for p in oos_set]


def closes(n, net, start=0):
    return [(start + i * 10.0, start + i * 10.0 + 5, 'k%d' % i, net) for i in range(n)]


def test_kill_stop_all_and_insufficient_history():
    kd = kill_decisions({'a': closes(30, -0.1)})
    assert kd['variants']['a']['decision'] == KILL_VARIANT and not kd['stop_all']
    kd = kill_decisions({'a': closes(29, -0.1)})
    assert kd['variants']['a']['decision'] == INSUFFICIENT_OOS_HISTORY
    kd = kill_decisions({'a': closes(30, 0.1)})
    assert kd['variants']['a']['decision'] == 'CONTINUE'
    kd = kill_decisions({'a': closes(40, -0.1)})
    assert kd['variants']['a']['n'] == 30 and kd['skipped_after_kill'] == 10
    kd = kill_decisions({'a': closes(25, -1.0), 'b': closes(25, 0.0)})
    assert kd['stop_all'] and kd['cumulative_oos_net_usd'] == -21.0
    assert kd['variants']['a']['n'] == 21 and kd['skipped_after_stop'] > 0
    assert kd['variants']['a']['decision'] == INSUFFICIENT_OOS_HISTORY


def test_run_e3_awaiting_data_when_trigger_unmet():
    assert run_e3([], VARIANTS, COSTS).verdict == AWAITING_DATA
    thin = [position(i, i * 600, n_marks=19) for i in range(40)]
    result = run_e3(thin, VARIANTS, COSTS)
    assert result.verdict == AWAITING_DATA and result.disqualified == {'too_few_marks': 40}
    almost = [position(i, i * 600) for i in range(29)] + [position(99, 0, closed=False)]
    result = run_e3(almost, VARIANTS, COSTS)
    assert result.verdict == AWAITING_DATA and result.qualifying == 29
    assert result.disqualified == {'not_closed': 1}


def test_run_e3_runnable_but_not_decision_capable_on_first_trigger():
    # Pump, plateau past 20 minutes, then a slow fade the control rides down its 30% trail.
    pump = [1.0, 1.1, 1.2, 1.3, 1.4] + [1.5] * 10 + [1.4, 1.3, 1.2, 1.1, 1.04]
    positions = [position(i, i * 3600, prices=pump) for i in range(30)]
    result = run_e3(positions, VARIANTS, COSTS)
    assert result.status == 'runnable' and (result.n_is, result.n_oos) == (20, 10)
    assert set(result.variants) == set(VARIANTS)
    assert result.variants[CONTROL]['IS']['net_per_close_usd'] < 0
    assert result.variants['ratchet']['IS']['censored_share'] == 1.0  # ineligible (> 50%)
    assert result.frozen_variant == 'time_box'
    assert result.gates['decision'] == INSUFFICIENT_OOS_HISTORY
    assert result.verdict.startswith('RUNNABLE_NOT_DECISION_CAPABLE: insufficient OOS history (10/30')
    assert json.dumps(result.to_dict())
    flat = [position(i, i * 3600) for i in range(30)]  # every variant loses $1.26/close to costs
    assert run_e3(flat, VARIANTS, COSTS).verdict.startswith('STOP_ALL')
    level = [position(i, i * 3600, prices=[1.2] * 25) for i in range(30)]
    result = run_e3(level, VARIANTS, COSTS)
    assert result.frozen_variant is None and not result.stop_all
    assert result.verdict == 'NO_VARIANT_BEATS_CONTROL: ship nothing'
    with pytest.raises(ValueError):
        run_e3(flat, {k: v for k, v in VARIANTS.items() if k != CONTROL}, COSTS)
