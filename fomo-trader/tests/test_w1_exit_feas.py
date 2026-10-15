"""Hermetic tests for W1 exit-feasibility screen. No I/O, no network."""

import time

import pytest

from analysis.w1_exit_feas import exit_feas as ef


def test_impact_math_constant_product():
    # $7 into $40k pool: ~0.035%
    v = ef.estimate_exit_impact_pct(7.0, 40000.0)
    assert 0.03 < v < 0.04


def test_impact_scales_with_size():
    small = ef.estimate_exit_impact_pct(7.0, 40000.0)
    big = ef.estimate_exit_impact_pct(700.0, 40000.0)
    assert big > 10 * small


def test_impact_volume_bound():
    # thin 15m volume dominates the bound
    v = ef.estimate_exit_impact_pct(7.0, 1_000_000.0, volume_usd=100.0)
    assert v > ef.estimate_exit_impact_pct(7.0, 1_000_000.0)


def test_impact_fail_open_bad_inputs():
    for bad in [(None, 40000.0), (7.0, None), (0, 40000.0), (7.0, 0),
                (-5, 40000.0), ("x", 40000.0), (float("inf"), 40000.0)]:
        assert ef.estimate_exit_impact_pct(*bad) == 0.0


def test_approve_vetoes_above_threshold():
    entry = {"buy_sol": 1.0, "sol_usd": 1000.0, "liquidity_usd": 100.0}
    # $1000 into $100 pool -> huge impact -> veto at 25%
    assert ef.approve(entry, ef.Params(threshold_pct=25.0)) is False
    assert ef.approve(entry, ef.Params(threshold_pct=99.9)) is True


def test_approve_passes_realistic_trade():
    entry = {"buy_sol": 0.087, "sol_usd": 80.0, "liquidity_usd": 40000.0}
    assert ef.approve(entry, ef.Params(threshold_pct=10.0)) is True


def test_approve_fail_open():
    p = ef.Params(threshold_pct=10.0)
    assert ef.approve(None, p) is True
    assert ef.approve({}, p) is True
    assert ef.approve({"liquidity_usd": None}, p) is True
    assert ef.approve({"liquidity_usd": 100.0}, p) is True  # no size -> approve
    assert ef.approve("garbage", p) is True
    assert ef.approve({"liquidity_usd": 1.0}, None) is True  # loads params from disk, fail-open


def test_approve_disabled_params():
    entry = {"buy_sol": 1.0, "sol_usd": 1000.0, "liquidity_usd": 1.0}
    assert ef.approve(entry, ef.Params(threshold_pct=0.001, enabled=False)) is True


def test_approve_never_raises():
    for e in [None, {}, {"liquidity_usd": "nan"}, {"buy_sol": float("nan")}]:
        assert ef.approve(e, ef.Params()) is True


def test_approve_timing_under_50ms():
    entry = {"buy_sol": 0.087, "sol_usd": 80.0, "liquidity_usd": 40000.0,
             "m15_volume_usd": 15884.0}
    p = ef.Params(threshold_pct=25.0)
    t0 = time.perf_counter()
    for _ in range(200):
        ef.approve(entry, p)
    dt = (time.perf_counter() - t0) / 200 * 1000
    assert dt < 50, f"{dt:.3f}ms per call"


# ---- adversarial self-review: try to break it ----

def test_nan_fields_counted_not_silent():
    ef.reset_fallback_counts()
    p = ef.Params(threshold_pct=25.0)
    assert ef.approve({"buy_sol": 0.087, "sol_usd": 80.0,
                       "liquidity_usd": float("nan")}, p) is True
    assert ef.approve({"buy_sol": float("nan"), "sol_usd": 80.0,
                       "liquidity_usd": 40000.0}, p) is True
    assert ef.approve({"buy_sol": 0.087, "sol_usd": float("inf"),
                       "liquidity_usd": 40000.0}, p) is True
    c = ef.fallback_counts
    assert c["missing_liquidity"] >= 1
    assert c["missing_position"] >= 2


def test_string_numbers_still_score():
    # payloads sometimes stringify numbers; must not crash or mis-veto
    e = {"buy_sol": "0.087", "sol_usd": "80.0", "liquidity_usd": "40000"}
    assert ef.approve(e, ef.Params(threshold_pct=10.0)) is True
    assert ef.position_usd_of(e) == pytest.approx(6.96)


def test_bsc_vs_solana_payloads():
    p = ef.Params(threshold_pct=10.0)
    sol = {"buy_sol": 0.087, "sol_usd": 80.0, "liquidity_usd": 40000.0,
           "holder_top1_pct": 2.1, "chain": "solana"}
    bsc_commit = {"buy_bnb": 0.009, "commit_price_native": 1.76e-7,
                  "commit_price_usd": 1.37e-4, "liquidity_usd": 40199.0,
                  "holder_top1_pct": None, "holder_top5_pct": None,
                  "chain": "bsc"}
    bsc_quoted = {"buy_bnb": 0.009, "bnb_usd": 780.0, "liquidity_usd": 40199.0,
                  "chain": "bsc"}
    for e in (sol, bsc_commit, bsc_quoted):
        assert ef.approve(e, p) is True
    assert ef.position_usd_of(bsc_commit) == pytest.approx(7.0, rel=0.01)
    assert ef.position_usd_of(bsc_quoted) == pytest.approx(7.02)


def test_empty_15m_window():
    e = {"buy_sol": 0.087, "sol_usd": 80.0, "liquidity_usd": 40000.0,
         "m15_volume_usd": 0, "m15_buys": 0, "m15_sells": 0}
    assert ef.approve(e, ef.Params(threshold_pct=10.0)) is True
    assert ef.estimate_exit_impact_pct(7.0, 40000.0, 0) == \
        ef.estimate_exit_impact_pct(7.0, 40000.0)


def test_duplicate_mints_evaluated_independently():
    mk = lambda liq: {"entry_record": {"buy_sol": 0.087, "sol_usd": 80.0,
                                       "liquidity_usd": liq}}
    trades = [mk(40000.0), mk(40000.0), mk(40000.0)]
    res = ef.evaluate(trades, [10.0], lambda t, stress=False: (0.0, 1.0))
    assert res[10.0]["is"]["n"] + res[10.0]["oos"]["n"] == 3


def test_hostile_entry_object_counted():
    class Hostile:
        def get(self, k, default=None):
            raise RuntimeError("boom")
    ef.reset_fallback_counts()
    assert ef.approve(Hostile(), ef.Params(threshold_pct=10.0)) is True
    assert ef.fallback_counts["exception"] >= 1


def test_corrupt_params_file_fails_open_and_counts(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    ef.reset_fallback_counts()
    params = ef.load_params(bad)
    assert params.threshold_pct == ef.DEFAULT_THRESHOLD_PCT
    assert ef.fallback_counts["params_unavailable"] >= 1
    missing = tmp_path / "nope.json"
    assert ef.load_params(missing).threshold_pct == ef.DEFAULT_THRESHOLD_PCT


def test_fallback_counters_thread_safe():
    import threading
    ef.reset_fallback_counts()
    p = ef.Params(threshold_pct=10.0)
    def hammer():
        for _ in range(500):
            ef.approve({"liquidity_usd": None}, p)
    ts = [threading.Thread(target=hammer) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert ef.fallback_counts["missing_liquidity"] == 4000


def test_evaluate_out_of_order_is_positional_not_temporal():
    # evaluate() splits by list position; ordering is the harness's job.
    # Documented here so a future caller can't assume timestamp sorting.
    mk = lambda: {"entry_record": {"buy_sol": 0.087, "sol_usd": 80.0,
                                   "liquidity_usd": 40000.0}}
    trades = [mk() for _ in range(6)]
    res = ef.evaluate(trades, [10.0], lambda t, stress=False: (0.0, 2.0))
    assert res[10.0]["is"]["n"] == 4 and res[10.0]["oos"]["n"] == 2


def test_params_from_dict_sanitizes():
    assert ef.Params.from_dict({"threshold_pct": -5}).threshold_pct == ef.DEFAULT_THRESHOLD_PCT
    assert ef.Params.from_dict({"threshold_pct": "abc"}).threshold_pct == ef.DEFAULT_THRESHOLD_PCT
    assert ef.Params.from_dict(None).threshold_pct == ef.DEFAULT_THRESHOLD_PCT


def test_evaluate_splits_chronologically():
    mk = lambda i: {"entry_record": {"buy_sol": 0.01, "sol_usd": 100.0,
                                    "liquidity_usd": 50000.0}}
    trades = [mk(i) for i in range(9)]
    res = ef.evaluate(trades, [10.0], lambda t, stress=False: (0.0, 1.0))
    assert res[10.0]["is"]["n"] == 6
    assert res[10.0]["oos"]["n"] == 3
