import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hold_guard as hg
import same_day_loop as sdl


def test_calibrated_taken_price_both_sides():
    assert sdl.calibrated_taken_price(.60, .10, "YES") == .64
    assert sdl.calibrated_taken_price(.40, -.10, "NO") == .64


@pytest.mark.parametrize("edge,pass7,pass10", [
    (.07, True, False), (.0699, False, False),
    (-.07, True, False), (-.0699, False, False),
    (.10, True, True), (-.10, True, True),
])
def test_edge_gate_ladder_boundaries_both_sides(edge, pass7, pass10):
    flags = sdl.edge_gate_ladder_shadow(edge)
    assert flags == {"gate_edge5_pass": True, "gate_edge7_pass": pass7,
                     "gate_edge10_pass": pass10}


def test_edge_gate_flags_copy_to_settled_grade(monkeypatch, tmp_path):
    monkeypatch.setattr(sdl, "GRADE_PATH", str(tmp_path / "grades.jsonl"))
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda ticker: "NO")
    val = {"ticker": "KXBTC15M-26OCT050000-00", "side": "NO",
           "window_open_utc": 100, "window_close_utc": 200,
           "edge": -.07, "market_mid": .60,
           **sdl.edge_gate_ladder_shadow(-.07)}
    rows, _ = sdl.grade_pending({"KXBTC15M-26OCT050000-00:NO": val},
                                200 + sdl.GRADE_DELAY_S, mark=False)
    assert len(rows) == 1
    assert {k: rows[0][k] for k in sdl.edge_gate_ladder_shadow(-.07)} == {
        "gate_edge5_pass": True, "gate_edge7_pass": True,
        "gate_edge10_pass": False}


def test_persistence_shadow_accept_reject_and_no_delay():
    state = {}
    obs = {("TK", 200): {"mid": .60, "edge": .08, "mins_left": 3}}
    assert sdl.update_persistence_shadow(state, obs, 0) == []
    assert sdl.update_persistence_shadow(state, obs, 89) == []
    accepted = sdl.update_persistence_shadow(state, obs, 90)
    assert accepted[0]["decision"] == "would_accept"
    assert accepted[0]["first_scan"] == {"edge_taken": .08, "mid_taken": .6,
                                         "mins_left": 3}
    assert accepted[0]["second_scan"] == accepted[0]["first_scan"]
    assert sdl.update_persistence_shadow(state, obs, 91) == []

    other = {("TN", 300): {"mid": .40, "edge": -.08, "mins_left": 3}}
    assert sdl.update_persistence_shadow(state, other, 100) == []
    weaker = {("TN", 300): {"mid": .46, "edge": -.03, "mins_left": 1.5}}
    rejected = sdl.update_persistence_shadow(state, weaker, 190)
    assert rejected[0]["decision"] == "would_reject"
    assert rejected[0]["side"] == "NO"
    assert rejected[0]["second_scan"] == {"edge_taken": .03, "mid_taken": .54,
                                         "mins_left": 1.5}


@pytest.mark.parametrize("side,entry_yes_mid,peak_yes_mid,now_yes_mid,wording,pnl", [
    ("YES", .60, .80, .70, "protect paper profit", .10),
    ("YES", .75, .80, .65, "cut the paper loss", -.10),
    ("NO", .40, .20, .30, "protect paper profit", .10),
    ("NO", .25, .20, .35, "cut the paper loss", -.10),
])
def test_hold_guard_pullback_fields(side, entry_yes_mid, peak_yes_mid,
                                    now_yes_mid, wording, pnl):
    entry = hg.taken_price(side, entry_yes_mid)
    peak = hg.taken_price(side, peak_yes_mid)
    now = hg.taken_price(side, now_yes_mid)
    hit = hg.decide([[0, peak], [10, now]], 10)
    assert hit is not None
    fields = hg.pullback_fields(entry, now, hit[0])
    assert fields["peak_to_now_drop"] == pytest.approx(peak - now)
    assert fields["entry_to_now_pnl"] == pytest.approx(pnl)
    assert fields["alert_wording"] == wording


def test_accuracy_price_aware_cohort_and_kalshi_only():
    rows = [
        {"key": "TK:YES", "ticker": "TK", "side": "YES", "proxy": "kalshi",
         "correct": True, "market_mid": .6, "edge": .1,
         "fair_calibrated_taken": .64, "entry_policy_cohort": "post-agreement"},
        {"key": "TK:NO", "ticker": "TK", "side": "NO", "proxy": "kalshi",
         "correct": False, "market_mid": .4, "edge": -.1,
         "fair_calibrated_taken": .64, "entry_policy_cohort": "post-agreement"},
        {"key": "OTHER:YES", "ticker": "OTHER", "side": "YES",
         "proxy": "kraken", "correct": True, "market_mid": .1, "edge": .3},
    ]
    acc = sdl.accuracy_stats(rows)
    assert acc["win_rate"] == .5
    assert acc["mean_taken_price"] == .6
    assert acc["mean_calibrated_probability"] == .64
    assert acc["mean_paper_mid_payoff"] == -.1
    assert acc["independent_windows"] == 1
    assert acc["entry_policy_cohort"] == "post-agreement"
