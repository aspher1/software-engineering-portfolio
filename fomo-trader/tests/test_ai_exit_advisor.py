"""Hermetic tests for the AI exit-manager prototype (analysis/ai_exit/).

No bot imports, no network, no subprocess. Guardrail invariants, latency
budget, no-look-ahead, and the shadow evaluator on synthetic paths.
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis.ai_exit.advisor import (
    EXIT, HOLD, ExitAdvice, PositionState, QuoteTick,
    combine_with_deterministic, exit_now, filter_quotes, hold,
    load_z4_quote_path)
from analysis.ai_exit.local_policy import LocalExitPolicy, POLICY_DEFAULTS
from analysis.ai_exit.llm_advisor import LLMAdvisor
from analysis.ai_exit import shadow_eval as se


def _pos(**kw):
    d = dict(mint="mint123", chain="solana", entry_price=1.0,
             peak_price=1.0, current_price=1.0, opened_ts=0.0, now_ts=600.0,
             rungs_fired=0, exit_cfg=dict(se.LIVE_EXIT_CFG))
    d.update(kw)
    return PositionState(**d)


def _ticks(prices, t0=0.0, step=5.0, null_at=()):
    return [QuoteTick(t0 + i * step, None if i in null_at else p)
            for i, p in enumerate(prices)]


def _ramp_then_fade():
    # rise to +30% by t=300, fade back to +2% by t=600 (round-trip signature)
    px = []
    for i in range(61):
        px.append(1.0 + 0.30 * (i / 60))
    for i in range(1, 61):
        px.append(1.30 - 0.28 * (i / 60))
    return _ticks(px)


# ---------------------------------------------------------------------------
# Floor invariant: the deterministic layer is unsuppressible
# ---------------------------------------------------------------------------
def test_combine_deterministic_always_wins():
    advice = hold("no opinion", "local_policy")
    fired, reason = combine_with_deterministic(True, "hard-stop", advice)
    assert fired is True
    assert reason == "hard-stop"  # advisor reason must not leak in


def test_combine_advisor_can_only_add_exit():
    advice = exit_now("round-trip from peak", "local_policy")
    fired, reason = combine_with_deterministic(False, "", advice)
    assert fired is True
    assert "ai-advisor(local_policy)" in reason


def test_combine_hold_is_silent():
    advice = hold("no fade signature", "local_policy")
    assert combine_with_deterministic(False, "", advice) == (False, "")


def test_advice_vocabulary_has_no_veto():
    with pytest.raises(ValueError):
        ExitAdvice("STAY", "veto attempt", 0.9, "x", 0.0)
    with pytest.raises(ValueError):
        ExitAdvice("EXIT", "bad conf", 1.5, "x", 0.0)


def test_advisor_cannot_suppress_hard_stop_in_replay():
    # advisor that always says HOLD must not change a hard-stop exit
    from analysis.ai_exit.advisor import ExitAdvisor

    class AlwaysHold(ExitAdvisor):
        name = "always_hold"

        def advise(self, pos, quotes, as_of_ts=None):
            return hold("nothing", self.name)
    marks = se.gen_rug(seed=7)
    base = se.replay(marks, se.LIVE_EXIT_CFG, None, "rug")
    with_hold = se.replay(marks, se.LIVE_EXIT_CFG, AlwaysHold(), "rug")
    assert base.reason == with_hold.reason
    assert base.net_usd == pytest.approx(with_hold.net_usd)


# ---------------------------------------------------------------------------
# Local policy: fail-closed, signatures, latency, no-look-ahead
# ---------------------------------------------------------------------------
def test_empty_quote_path_is_hold():
    pol = LocalExitPolicy()
    a = pol.advise(_pos(), [])
    assert a.decision == HOLD


def test_all_null_marks_is_hold():
    pol = LocalExitPolicy()
    a = pol.advise(_pos(), _ticks([1.0] * 20, null_at=set(range(20))))
    assert a.decision == HOLD


def test_too_few_ticks_is_hold():
    pol = LocalExitPolicy()
    a = pol.advise(_pos(), _ticks([1.0] * 5))
    assert a.decision == HOLD
    assert "insufficient marks" in a.reason


def test_round_trip_from_peak_fires():
    pol = LocalExitPolicy()
    ticks = _ramp_then_fade()
    a = pol.advise(_pos(current_price=ticks[-1].price_usd,
                        peak_price=1.30, now_ts=ticks[-1].ts), ticks)
    assert a.decision == EXIT
    assert "round-trip" in a.reason
    assert 0.0 <= a.confidence <= 1.0
    assert a.latency_ms >= 0


def test_moon_hold_does_not_fire():
    pol = LocalExitPolicy()
    marks = se.gen_moon_hold(seed=3)
    ticks = [QuoteTick(t, p) for t, p in marks]
    a = pol.advise(_pos(current_price=marks[-1][1],
                        peak_price=max(p for _, p in marks),
                        now_ts=marks[-1][0]), ticks)
    assert a.decision == HOLD


def test_no_look_ahead():
    pol = LocalExitPolicy()
    ticks = _ramp_then_fade()
    t_cut = 300.0  # at the peak: no fade visible yet
    a_cut = pol.advise(_pos(current_price=1.30, peak_price=1.30,
                             now_ts=t_cut), ticks, as_of_ts=t_cut)
    a_full = pol.advise(_pos(current_price=ticks[-1].price_usd,
                              peak_price=1.30, now_ts=ticks[-1].ts), ticks)
    assert a_cut.decision == HOLD      # future fade must not leak in
    assert a_full.decision == EXIT


def test_filter_quotes_cutoff():
    ticks = _ticks([1.0, 2.0, 3.0], t0=100.0, step=10.0)
    assert [q.price_usd for q in filter_quotes(ticks, 105.0)] == [1.0]
    assert len(filter_quotes(ticks, None)) == 3


def test_local_policy_latency_budget():
    pol = LocalExitPolicy()
    prices = [1.0 + 0.001 * (i % 50) for i in range(5000)]
    ticks = _ticks(prices)
    pos = _pos(current_price=prices[-1], peak_price=max(prices),
               now_ts=ticks[-1].ts)
    # warm up, then measure p99-ish over 50 calls
    for _ in range(5):
        pol.advise(pos, ticks)
    samples = []
    for _ in range(50):
        t0 = time.perf_counter()
        pol.advise(pos, ticks)
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    p99 = samples[int(0.99 * (len(samples) - 1))]
    assert p99 < 50.0, "p99 advise() latency %.2fms exceeds 50ms budget" % p99


def test_policy_exception_is_hold_not_exit(monkeypatch):
    pol = LocalExitPolicy()
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr("analysis.ai_exit.local_policy.valid_marks", boom)
    a = pol.advise(_pos(), _ticks([1.0] * 20))
    assert a.decision == HOLD


# ---------------------------------------------------------------------------
# LLM advisor: default off, fail-closed, strict parsing
# ---------------------------------------------------------------------------
def test_llm_default_off():
    llm = LLMAdvisor()
    assert llm.enabled is False
    a = llm.advise(_pos(), _ticks([1.0] * 20))
    assert a.decision == HOLD
    assert "disabled" in a.reason


def test_llm_parse_contract_strict():
    ok = LLMAdvisor.parse_output(
        "DECISION: EXIT | CONFIDENCE: 0.72 | REASON: accelerating drawdown")
    assert ok == ("EXIT", 0.72, "accelerating drawdown")
    assert LLMAdvisor.parse_output("i think we should exit now") is None
    assert LLMAdvisor.parse_output("") is None
    assert LLMAdvisor.parse_output(
        "DECISION: HOLD | CONFIDENCE: 1.5 | REASON: x") is None
    assert LLMAdvisor.parse_output(
        "DECISION: MOON | CONFIDENCE: 0.9 | REASON: x") is None


def test_llm_failure_is_hold_never_exit(monkeypatch):
    llm = LLMAdvisor(enabled=True, backend="nope", min_interval_s=0)
    a = llm.advise(_pos(), _ticks([1.0 + 0.01 * i for i in range(20)]))
    assert a.decision == HOLD
    assert "fail-closed" in a.reason or "unknown backend" in a.reason

    llm2 = LLMAdvisor(enabled=True, backend="codex", min_interval_s=0)
    monkeypatch.setattr(llm2, "_run_cli",
                        lambda prompt: (False, "timeout after 90s"))
    a2 = llm2.advise(_pos(), _ticks([1.0 + 0.01 * i for i in range(20)]))
    assert a2.decision == HOLD

    monkeypatch.setattr(llm2, "_run_cli",
                        lambda prompt: (True, "EXIT THE POSITION NOW!!!"))
    a3 = llm2.advise(_pos(), _ticks([1.0 + 0.01 * i for i in range(20)]))
    assert a3.decision == HOLD  # unparseable -> HOLD
    assert "unparseable" in a3.reason


def test_llm_advisory_cadence_throttle():
    llm = LLMAdvisor(enabled=True, min_interval_s=3600)
    llm._last_call_ts = time.time()
    a = llm.advise(_pos(), _ticks([1.0 + 0.01 * i for i in range(20)]))
    assert a.decision == HOLD
    assert "cadence" in a.reason


# ---------------------------------------------------------------------------
# Z4 loader: never raises on bad data
# ---------------------------------------------------------------------------
def test_load_z4_quote_path_skips_bad_lines(tmp_path):
    fp = tmp_path / "abc.jsonl"
    rows = [
        {"ts": "2026-09-27 12:00:00", "mint": "m", "chain": "solana",
         "price_usd": 1.5, "liquidity_usd": None},
        {"ts": "2026-09-27 12:00:05", "mint": "m", "chain": "solana",
         "price_usd": None, "liquidity_usd": None},
        "not json at all",
        {"ts": "bogus", "mint": "m", "price_usd": 2.0},
        {"ts": "2026-09-27 12:00:10", "mint": "m", "price_usd": -3},
    ]
    fp.write_text("\n".join(r if isinstance(r, str) else json.dumps(r)
                            for r in rows))
    ticks = load_z4_quote_path(str(fp))
    assert len(ticks) == 3  # good, null-price, negative->null
    assert ticks[0].price_usd == 1.5
    assert ticks[1].price_usd is None
    assert load_z4_quote_path(str(tmp_path / "missing.jsonl")) == []


# ---------------------------------------------------------------------------
# Shadow evaluator on synthetic paths
# ---------------------------------------------------------------------------
def test_shadow_eval_synthetic_sane_report():
    paths = {name: gen(11) for name, gen in se.GENERATORS.items()}
    rep = se.evaluate(paths, se.LIVE_EXIT_CFG, LocalExitPolicy())
    assert set(rep) == {"baseline", "advisor(local_policy)"}
    for side, s in rep.items():
        assert s["closes"] == len(paths)
        assert s["win_rate"] >= 0.0
        assert "reasons" in s and s["reasons"]
    assert "delta_net_usd_vs_baseline" in rep["advisor(local_policy)"]


def test_shadow_eval_deterministic_triggers_fire():
    # rug: dump detector or trailing/hard stop must catch it, advisor or not
    marks = se.gen_rug(seed=5)
    r = se.replay(marks, se.LIVE_EXIT_CFG, None, "rug")
    assert r.reason in ("dump-detector", "trailing-stop", "hard-stop")
    assert r.net_usd < 0  # rugs lose money even with instant exits (marks)


def test_shadow_eval_tp_ladder_partial():
    marks = se.gen_moon_hold(seed=3)
    r = se.replay(marks, se.LIVE_EXIT_CFG, None, "moon")
    assert r.rungs_fired >= 1  # +50% rung banked half
    assert r.reason == "path-end (forced)"  # the rest rode


def test_evaluate_without_advisor():
    paths = {"chop": se.gen_chop(9)}
    rep = se.evaluate(paths, se.LIVE_EXIT_CFG, None)
    assert set(rep) == {"baseline"}


# ---------------------------------------------------------------------------
# Latency guard: a late EXIT is downgraded to HOLD (fail-closed)
# ---------------------------------------------------------------------------

def _slow_inner(exit=True, delay=0.06):
    def inner(self, pos, quotes, as_of_ts, t0):
        time.sleep(delay)
        if exit:
            return exit_now("slow exit", self.name, 0.0, {}, confidence=0.9)
        return hold("slow hold", self.name, 0.0, {})
    return inner


def test_latency_guard_downgrades_slow_exit():
    pol = LocalExitPolicy(cfg={"max_latency_ms": 50.0})
    pol._advise_inner = _slow_inner(exit=True).__get__(pol, LocalExitPolicy)
    pos = _pos()
    quotes = _ticks([1.0] * 20)
    advice = pol.advise(pos, quotes)
    assert advice.decision == HOLD
    assert "latency budget exceeded" in advice.reason
    assert pol.budget_breaches == 1


def test_latency_guard_slow_hold_not_counted():
    pol = LocalExitPolicy(cfg={"max_latency_ms": 50.0})
    pol._advise_inner = _slow_inner(exit=False).__get__(pol, LocalExitPolicy)
    advice = pol.advise(_pos(), _ticks([1.0] * 20))
    assert advice.decision == HOLD
    assert pol.budget_breaches == 0  # only EXIT downgrades count


def test_latency_guard_fast_exit_unaffected():
    pol = LocalExitPolicy()  # real inner; 121-tick fade path is ~ms
    quotes = _ramp_then_fade()
    pos = _pos(peak_price=1.30, current_price=1.02, now_ts=600.0)
    advice = pol.advise(pos, quotes)
    assert advice.latency_ms < 50.0
    assert pol.budget_breaches == 0


def test_latency_guard_configurable_budget():
    pol = LocalExitPolicy(cfg={"max_latency_ms": 10000.0})
    pol._advise_inner = _slow_inner(exit=True, delay=0.06).__get__(pol, LocalExitPolicy)
    advice = pol.advise(_pos(), _ticks([1.0] * 20))
    assert advice.decision == EXIT  # generous budget: no downgrade
    assert pol.budget_breaches == 0
