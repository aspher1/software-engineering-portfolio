import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.improve import (maybe_improve, propose_candidates, replay_pnl,
                         save_strategy, load_champion_strategy)


def _cfg():
    return {
        "strategy": {
            "mispricing_threshold": 0.08, "kelly_fraction": 0.25,
            "max_risk_per_trade": 0.06, "max_open_positions": 10,
            "daily_trade_cap": 20, "bankroll": 1000.0,
        },
        "scanner": {},
        "improvement": {"min_resolved_trades": 4, "retune_every_resolved": 4},
    }


def _grade(gap, pnl, kelly=0.25):
    return {"type": "grade", "gap": gap, "pnl": pnl,
            "kelly_fraction_used": kelly}


def test_proposals_only_tighten():
    cur = _cfg()["strategy"]
    for name, cand in propose_candidates(cur):
        assert cand["mispricing_threshold"] >= cur["mispricing_threshold"]
        assert cand["kelly_fraction"] <= cur["kelly_fraction"]
        assert cand["max_risk_per_trade"] <= cur["max_risk_per_trade"]


def test_replay_filters_by_threshold():
    grades = [_grade(0.05, 100.0), _grade(0.15, 100.0)]
    p = replay_pnl(grades, {"mispricing_threshold": 0.08, "kelly_fraction": 0.25})
    assert p == 100.0  # only the 0.15-gap trade counts


def test_improve_waits_for_min_trades(tmp_path):
    rep = maybe_improve(_cfg(), str(tmp_path), [_grade(0.2, 10.0)])
    assert rep["accepted"] is False


def test_improve_accepts_better_safer_config(tmp_path):
    cfg = _cfg()
    # Small-gap trades lost money; raising the threshold drops them in replay.
    grades = [_grade(0.085, -50.0) for _ in range(3)] + [_grade(0.20, 60.0)]
    rep = maybe_improve(cfg, str(tmp_path), grades)
    assert rep["accepted"] is True
    assert rep["change"] == "threshold_up"
    ver, merged = load_champion_strategy(str(tmp_path), cfg)
    assert ver == rep["to_version"]
    assert merged["mispricing_threshold"] > 0.08


def test_improve_rejects_when_current_best(tmp_path):
    cfg = _cfg()
    grades = [_grade(0.20, 60.0) for _ in range(4)]
    rep = maybe_improve(cfg, str(tmp_path), grades)
    assert rep["accepted"] is False
