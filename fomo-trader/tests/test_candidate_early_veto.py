"""Hermetic checks for the unshipped early-entry gain veto replay."""

import pytest

from analysis.candidate_early_veto import (
    Pair, cohort, net_usd, pair_rows, veto, wipe,
)


def pair(gain=99.99, chain="bsc", realized=-0.001, reason="dump detector"):
    entry = {"mint": "0xabc", "chain": chain, "buy_sol": 0.01,
             "entry": 2.0, "signal_gain_pct": gain, "bnb_usd": 1000.0}
    close = {"mint": "0xabc", "chain": chain, "entry": 2.0, "exit": 1.0,
             "realized_bnb": realized, "bnb_usd": 1200.0, "reason": reason}
    return Pair(entry, close, chain, 0, 1)


def test_fifo_chain_inference_and_close_order():
    sol = "S" * 41
    rows = [
        {"type": "entry", "mint": "0xabc", "tag": 1},
        {"type": "entry", "mint": sol, "tag": 2},
        {"type": "entry", "mint": "0xabc", "tag": 3},
        {"type": "close", "mint": "0xabc"},
        {"type": "close", "mint": sol},
        {"type": "close", "mint": "0xabc"},
        {"type": "close", "mint": "0xmissing"},
    ]
    pairs, unmatched_closes, unmatched_entries = pair_rows(rows)
    assert [(p.entry["tag"], p.chain, p.close_index) for p in pairs] == [
        (1, "bsc", 3), (2, "solana", 4), (3, "bsc", 5)]
    assert (unmatched_closes, unmatched_entries) == (1, 0)


def test_base_and_stress_cost_math_and_rate_precedence():
    p = pair()
    # q=.5; native R=-.001; stake=.01; fixed=.00004; close rate=1200.
    assert net_usd(p) == pytest.approx(1200 * (-.001 - .01 * 1.5 * .004 - .00004))
    assert net_usd(p, stress=True) == pytest.approx(
        1200 * (-.001 - .01 * 1.5 * .007 - .00004))
    sol = Pair({"mint": "S" * 41, "buy_sol": .1, "entry": 2},
               {"mint": "S" * 41, "entry": 2, "exit": 1,
                "realized_sol": .02}, "solana", 0, 1)
    assert net_usd(sol) == pytest.approx(115 * (.02 - .1 * 1.5 * .004 - .004))


def test_edge_uses_original_cohort_denominator_and_skipped_zero():
    kept = pair(gain=99.99, realized=.001, reason="trailing")
    skipped = pair(gain=100.0, realized=-.005)
    result = cohort([kept, skipped])
    assert result["kept"] == 1
    assert result["kept_net"] == pytest.approx(net_usd(kept))
    assert result["edge"] == pytest.approx(-net_usd(skipped) / 2)


def test_wipe_requires_native_loss_and_reason():
    assert wipe(pair(realized=-.0085))
    assert wipe(pair(realized=-.009, reason="venue dump"))
    assert not wipe(pair(realized=-.0084))
    assert not wipe(pair(realized=-.009, reason="trailing stop"))


def test_frozen_veto_boundary():
    assert not veto(pair(gain=99.99))
    assert veto(pair(gain=100.0))
    assert not veto(pair(gain=None))  # `(None or 0) >= 100.0` is false.
