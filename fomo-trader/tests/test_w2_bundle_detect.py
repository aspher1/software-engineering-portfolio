import json
import logging
from pathlib import Path
from time import perf_counter_ns

import pytest

from analysis.w2_bundle_detect import CANDIDATE_RULES, detect, wash_score


@pytest.mark.parametrize("rule,values,expected", [
    ("top1_gt_20", {"holder_top1_pct": 21}, True),
    ("top1_gt_20", {"holder_top1_pct": 20}, False),
    ("top1_gt_30", {"holder_top1_pct": 31}, True),
    ("top1_gt_40", {"holder_top1_pct": 41}, True),
    ("top5_gt_50", {"holder_top5_pct": 51}, True),
    ("top5_gt_60", {"holder_top5_pct": 61}, True),
    ("top5_gt_70", {"holder_top5_pct": 71}, True),
    ("spread_gt_20", {"holder_top1_pct": 15, "holder_top5_pct": 36}, True),
    ("spread_gt_30", {"holder_top1_pct": 15, "holder_top5_pct": 46}, True),
    ("spread_gt_40", {"holder_top1_pct": 15, "holder_top5_pct": 56}, True),
    ("top5_gt_60_top1_le_20", {"holder_top1_pct": 20, "holder_top5_pct": 61}, True),
    ("top5_gt_60_top1_le_20", {"holder_top1_pct": 21, "holder_top5_pct": 61}, False),
    ("first10_gt_35", {"first10_buyers_pct": 36}, True),
    ("launch_block_gt_20", {"launch_block_buyers_pct": 21}, True),
])
def test_candidate_rules(rule, values, expected):
    result = detect({"chain": "solana", **values}, rule)
    assert result["veto"] is expected
    assert result["rule"] == (CANDIDATE_RULES[rule] if expected else None)
    assert 0 <= result["risk_score"] <= 1


def test_default_never_vetoes_and_spread_is_derived():
    result = detect({"holder_top1_pct": 10, "holder_top5_pct": 70})
    assert result["features"]["top5_minus_top1_pct"] == 60
    assert not result["veto"] and result["rule"] is None


@pytest.mark.parametrize("snapshot,rule", [
    ({}, "top5_gt_60"),
    ({"holder_top1_pct": 20}, "top5_gt_60"),
    ({"holder_top1_pct": 50, "holder_top5_pct": 20}, "spread_gt_20"),
    ({"holder_top5_pct": "NaN"}, "top5_gt_60"),
    ({"holder_top5_pct": float("inf")}, "top5_gt_60"),
    ({"holder_top5_pct": -1}, "top5_gt_60"),
    ({"holder_top5_pct": True}, "top5_gt_60"),
    ({"chain": "bsc", "holder_top5_pct": 90}, "top5_gt_60"),
    (None, "top5_gt_60"),
    ([], "top5_gt_60"),
    ({"holder_top5_pct": 90}, "unknown"),
])
def test_missing_malformed_and_bsc_fail_open_once(snapshot, rule, caplog):
    with caplog.at_level(logging.WARNING):
        result = detect(snapshot, rule)
    assert result["status"] == "fallback"
    assert result["veto"] is False
    assert len([r for r in caplog.records if "W2 FALLBACK" in r.message]) == 1


def test_partial_evidence_and_wash_stub():
    result = detect({"holder_top1_pct": 22, "holder_top5_pct": None}, "top1_gt_20")
    assert result["veto"] and result["features"]["top5_minus_top1_pct"] is None
    assert wash_score([{"side": "buy", "amount": 1}]) is None
    assert result["features"]["wash_score"] is None


def test_latency_under_50_ms_per_call():
    snapshot = {"chain": "solana", "holder_top1_pct": 12, "holder_top5_pct": 66}
    samples = []
    for _ in range(200):
        start = perf_counter_ns()
        detect(snapshot, "top5_gt_60_top1_le_20")
        samples.append(perf_counter_ns() - start)
    assert max(samples) < 50_000_000


def test_verdict_schema_and_no_unsupported_shipping():
    path = Path(__file__).resolve().parents[1] / "analysis/w2_bundle_detect/verdict.json"
    verdict = json.loads(path.read_text())
    assert verdict["scope"] == "Solana only"
    assert verdict["validation"] == "INCONCLUSIVE"
    assert verdict["ship_recommend"] is False and verdict["ship_rule"] is None
    assert verdict["minimum_enriched_completed_solana_trades"] == 60
    assert verdict["completed_solana_with_holder_evidence"] == 0
    assert len(verdict["thresholds_tested"]) == len(CANDIDATE_RULES)
    for candidate in verdict["thresholds_tested"]:
        assert candidate["rule"] == CANDIDATE_RULES[candidate["id"]]
        assert candidate["is"] is None and candidate["oos"] is None
        assert candidate["stress_3x_slippage"] == {"is": None, "oos": None}
    for split in ("is", "oos"):
        assert set(verdict["baseline"][split]) >= {"n", "usd", "pf", "win_rate", "max_dd_usd"}
