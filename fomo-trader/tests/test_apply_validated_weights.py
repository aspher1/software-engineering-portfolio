"""Hermetic coverage for the offline validated-weights applier.

Everything external (bot process, kill, subprocess launch) is mocked.
The real filesystem is used only inside tmp_path fixtures; the real bot,
its config, and its journal are never touched.
"""
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))
import apply_validated_weights as avw  # noqa: E402


def weights(**over):
    base = {k: 0.1 for k in avw.allocator.DEFAULT_WEIGHTS}
    base.update(over)
    return base


def passing_proposal(**over):
    checks = [
        {"name": "is_edge_positive", "passed": True, "threshold": "> 0", "value": 1.5},
        {"name": "oos_edge_positive", "passed": True, "threshold": "> 0", "value": 1.0},
        {"name": "oos_retention", "passed": True, "threshold": ">= 0.60", "value": 0.66},
        {"name": "oos_decay", "passed": True, "threshold": "<= 0.70", "value": 0.34},
        {"name": "is_win_rate_not_overfit", "passed": True, "threshold": "<= 0.90",
         "value": 0.55},
        {"name": "edge_not_single_chain", "passed": True, "threshold": "<= 0.80",
         "value": 0.60},
        {"name": "edge_not_single_time_regime", "passed": True, "threshold": "<= 0.80",
         "value": 0.55},
    ]
    proposal = {
        "verdict": "pass",
        "source": "allocator_analyst",
        "weights": weights(),
        "gate": {
            "verdict": "pass", "n": 100, "n_is": 70, "n_oos": 30,
            "checks": checks,
            "is": {"edge_per_trade": 1.5}, "oos": {"edge_per_trade": 1.0},
            "retention": 0.66, "decay": 0.34,
            "chain_concentration": {"evaluable": True, "share": 0.60},
            "time_concentration": {"evaluable": True, "share": 0.55},
        },
    }
    proposal.update(over)
    return proposal


def fixture(tmp_path, proposal=None, dry_run=True):
    analysis = tmp_path / "analysis"
    analysis.mkdir()
    run = tmp_path / "runs" / "paper-1h"
    run.mkdir(parents=True)
    (analysis / "allocator_proposal.json").write_text(
        json.dumps(proposal if proposal is not None else passing_proposal()))
    config = {
        "dry_run": dry_run,
        "risk_per_trade_pct": 1.0,
        "daily_loss_pct": 6.0,
        "allocator": {
            "mode": "live",
            "take_threshold": 0.35,
            "weights": {k: 0.0 for k in avw.allocator.DEFAULT_WEIGHTS},
        },
    }
    (run / "config.json").write_text(json.dumps(config))
    (run / "bot.pid").write_text("4242")
    (run / "state.json").write_text(json.dumps(
        {"positions": {"pos1": {"name": "AAA / WBNB"}}}))
    return tmp_path


def read_config(root):
    return json.loads((root / "runs" / "paper-1h" / "config.json").read_text())


def live_mocks(old_pid=4242, new_pid=99999, before={"pos1"}, after={"pos1"}):
    """Patch the three process-touching helpers."""
    p1 = patch.object(avw, "bot_alive", side_effect=lambda pid: pid in (old_pid, new_pid))
    p2 = patch.object(avw, "stop_bot", return_value=True)
    p3 = patch.object(avw, "start_bot", return_value=new_pid)
    p4 = patch.object(avw, "position_ids", side_effect=[set(before), set(after)])
    return p1, p2, p3, p4


def run_all(*patches):
    for p in patches:
        p.start()
    return patches


def stop_all(patches):
    for p in patches:
        p.stop()


# -- gate refusal ----------------------------------------------------------

def test_fail_verdict_is_noop(tmp_path):
    root = fixture(tmp_path, proposal=passing_proposal(verdict="fail"))
    assert avw.main(["--root", str(root)]) == 0
    assert read_config(root)["allocator"]["weights"] == {
        k: 0.0 for k in avw.allocator.DEFAULT_WEIGHTS}
    assert not list((root / "runs" / "paper-1h").glob("config.json.bak.*"))


def test_insufficient_data_is_noop(tmp_path):
    root = fixture(tmp_path, proposal={"verdict": "insufficient_data"})
    assert avw.main(["--root", str(root)]) == 0
    assert not list((root / "runs" / "paper-1h").glob("config.json.bak.*"))


def test_partial_gate_is_noop(tmp_path):
    bad = passing_proposal()
    bad["gate"]["checks"] = bad["gate"]["checks"][:6]  # one check missing
    root = fixture(tmp_path, proposal=bad)
    assert avw.main(["--root", str(root)]) == 0
    assert not list((root / "runs" / "paper-1h").glob("config.json.bak.*"))


def test_missing_proposal_is_noop(tmp_path):
    root = fixture(tmp_path)
    (root / "analysis" / "allocator_proposal.json").unlink()
    assert avw.main(["--root", str(root)]) == 0


# -- safety preconditions ---------------------------------------------------

def test_refuses_when_not_dry_run(tmp_path):
    root = fixture(tmp_path, dry_run=False)
    patches = run_all(*live_mocks())
    try:
        assert avw.main(["--root", str(root)]) == 1
    finally:
        stop_all(patches)
    assert not list((root / "runs" / "paper-1h").glob("config.json.bak.*"))


def test_refuses_out_of_range_weights(tmp_path):
    bad = passing_proposal()
    bad["weights"]["signal_gain_pct"] = 99.0
    root = fixture(tmp_path, proposal=bad)
    patches = run_all(*live_mocks())
    try:
        assert avw.main(["--root", str(root)]) == 1
    finally:
        stop_all(patches)
    assert not list((root / "runs" / "paper-1h").glob("config.json.bak.*"))


def test_refuses_incomplete_weight_set(tmp_path):
    bad = passing_proposal()
    del bad["weights"]["intercept"]
    root = fixture(tmp_path, proposal=bad)
    patches = run_all(*live_mocks())
    try:
        assert avw.main(["--root", str(root)]) == 1
    finally:
        stop_all(patches)


def test_refuses_when_bot_dead(tmp_path):
    root = fixture(tmp_path)
    p1 = patch.object(avw, "bot_alive", return_value=False)
    p1.start()
    try:
        assert avw.main(["--root", str(root)]) == 1
    finally:
        p1.stop()
    assert not list((root / "runs" / "paper-1h").glob("config.json.bak.*"))


# -- happy path --------------------------------------------------------------

def test_apply_writes_weights_only_and_restarts(tmp_path):
    root = fixture(tmp_path)
    patches = run_all(*live_mocks())
    try:
        assert avw.main(["--root", str(root)]) == 0
    finally:
        stop_all(patches)
    cfg = read_config(root)
    assert cfg["allocator"]["weights"] == weights()
    assert cfg["allocator"]["mode"] == "live"          # untouched
    assert cfg["allocator"]["take_threshold"] == 0.35  # untouched
    assert cfg["risk_per_trade_pct"] == 1.0           # untouched
    assert cfg["dry_run"] is True
    backups = list((root / "runs" / "paper-1h").glob("config.json.bak.*"))
    assert len(backups) == 1
    journal = (root / "analysis" / "weight_apply_log.jsonl").read_text().strip()
    record = json.loads(journal)
    assert record["result"] == "applied"
    assert record["old_pid"] == 4242 and record["new_pid"] == 99999
    assert record["position_check"] == "match"
    assert record["source"] == "allocator_analyst"


def test_idempotent_second_run(tmp_path):
    root = fixture(tmp_path)
    patches = run_all(*live_mocks())
    try:
        assert avw.main(["--root", str(root)]) == 0
        assert avw.main(["--root", str(root)]) == 0  # already applied
    finally:
        stop_all(patches)
    backups = list((root / "runs" / "paper-1h").glob("config.json.bak.*"))
    assert len(backups) == 1  # no second apply


def test_new_proposal_applies_again(tmp_path):
    root = fixture(tmp_path)
    p1 = patch.object(avw, "bot_alive", side_effect=lambda pid: pid in (4242, 99999))
    p2 = patch.object(avw, "stop_bot", return_value=True)
    p3 = patch.object(avw, "start_bot", return_value=99999)
    p4 = patch.object(avw, "position_ids",
                      side_effect=[{"pos1"}, {"pos1"}, {"pos1"}, {"pos1"}])
    patches = run_all(p1, p2, p3, p4)
    try:
        assert avw.main(["--root", str(root)]) == 0
        other = passing_proposal()
        other["weights"]["signal_gain_pct"] = 0.5  # distinct proposal hash
        (root / "analysis" / "allocator_proposal.json").write_text(json.dumps(other))
        assert avw.main(["--root", str(root)]) == 0
    finally:
        stop_all(patches)
    assert read_config(root)["allocator"]["weights"]["signal_gain_pct"] == 0.5


# -- rollback -----------------------------------------------------------------

def test_restart_failure_rolls_back_config(tmp_path):
    root = fixture(tmp_path)
    original = read_config(root)
    p1 = patch.object(avw, "bot_alive", side_effect=lambda pid: pid == 4242)
    p2 = patch.object(avw, "stop_bot", return_value=True)
    p3 = patch.object(avw, "start_bot", return_value=None)  # launch fails
    p4 = patch.object(avw, "position_ids", return_value={"pos1"})
    patches = run_all(p1, p2, p3, p4)
    try:
        assert avw.main(["--root", str(root)]) == 1
    finally:
        stop_all(patches)
    cfg = read_config(root)
    assert cfg["allocator"]["weights"] == original["allocator"]["weights"]
    journal = (root / "analysis" / "weight_apply_log.jsonl").read_text().strip()
    record = json.loads(journal)
    assert record["result"] == "rollback"
    assert record["rollback"] == "config_restored"


def test_position_mismatch_rolls_back(tmp_path):
    root = fixture(tmp_path)
    patches = run_all(*live_mocks(before={"pos1"}, after={"pos2"}))
    try:
        assert avw.main(["--root", str(root)]) == 1
    finally:
        stop_all(patches)
    assert read_config(root)["allocator"]["weights"] == {
        k: 0.0 for k in avw.allocator.DEFAULT_WEIGHTS}
    record = json.loads(
        (root / "analysis" / "weight_apply_log.jsonl").read_text().strip())
    assert record["result"] == "rollback"
    assert record["position_check"] == "mismatch"


# -- forbidden files -----------------------------------------------------------

def test_never_writes_forbidden_files(tmp_path):
    root = fixture(tmp_path)
    patches = run_all(*live_mocks())
    try:
        assert avw.main(["--root", str(root)]) == 0
    finally:
        stop_all(patches)
    run = root / "runs" / "paper-1h"
    for name in ("trades.jsonl", "state.json", "bot.log", "deaths.log",
                 "keypair.json"):
        path = run / name
        if name == "state.json":
            continue  # read-only input, must still exist
        assert not path.exists(), name
