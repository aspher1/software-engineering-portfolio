"""Hermetic coverage for the offline LLM allocator pass."""
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))
import llm_analyst as llm  # noqa: E402


def fixture(tmp_path, count=75):
    journal = tmp_path / "trades.jsonl"
    config = tmp_path / "config.json"
    out = tmp_path / "analysis"
    out.mkdir()
    config.write_text(json.dumps({"dry_run": True, "allocator": {
        "weights": dict(llm.allocator.DEFAULT_WEIGHTS), "take_threshold": .35}}))
    rows = []
    for i in range(count):
        chain = "solana" if i % 2 else "bsc"
        mint = "mint%d" % i
        ts = "2026-09-%02d %02d:00:00" % (1 + i // 24, i % 24)
        rows.extend([
            {"type": "entry", "mint": mint, "chain": chain, "ts": ts,
             "signal_gain_pct": 25 + i, "liquidity_usd": 10000 + i * 1000,
             "signal_ratio": 1.5 + i / 30, "allocator_score": .52,
             "allocator_multiplier": 1.1},
            {"type": "close", "mint": mint, "chain": chain, "ts": ts,
             "realized_usd": -1.0},
        ])
    journal.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    prior = {"verdict": "fail", "weights": {"intercept": 0}}
    (out / "allocator_proposal.json").write_text(json.dumps(prior))
    return journal, config, out


def args(paths):
    journal, config, out = paths
    return ["--journal", str(journal), "--config", str(config),
            "--out-dir", str(out)]


def response(intercept):
    return json.dumps({"weights": {"intercept": intercept},
                       "take_threshold": None,
                       "rationale": [{"change": "adjust intercept",
                                      "evidence": ["signal_gain_pct:low", "0:mint0"]}]})


def test_evidence_pack_on_journal(tmp_path):
    paths = fixture(tmp_path)
    rows, bad = llm.analyst.load_journal(str(paths[0]))
    trades, stats = llm.analyst.join_trades(rows)
    pack = llm.build_evidence_pack(trades, json.loads(paths[1].read_text())["allocator"])
    assert bad == 0 and stats["closes"] == 75
    assert pack["trade_summary"]["count"] == 75
    assert pack["trade_summary"]["realized_usd"] == -75
    assert sum(x["count"] for x in pack["by_chain"].values()) == 75
    assert sum(x["count"] for x in pack["buckets_by_normalized_feature"]["signal_gain_pct"].values()) == 75
    assert len(pack["last_priced_closes"]) == 30
    recent = pack["last_priced_closes"][-1]
    assert recent["normalized_features"] == llm.allocator.normalize(trades[-1]["features"])
    assert recent["allocator_score_at_entry"] == .52
    assert recent["allocator_multiplier_at_entry"] == 1.1
    assert len(llm.build_prompt(pack)) < 48000


def test_strict_json_parser():
    current = dict(llm.allocator.DEFAULT_WEIGHTS)
    parsed = llm.parse_proposal(response(-.2), current, .35)
    assert parsed["weights"]["intercept"] == -.2
    assert parsed["weights"]["liquidity_usd"] == current["liquidity_usd"]
    full = json.loads(response(0))
    full["weights"] = {k: -.1 for k in current}
    assert llm.parse_proposal(json.dumps(full), current, .35)["weights"] == full["weights"]
    for output in ("garbage", response(4), response(float("nan")),
                   response(True), response(0)[:-1] + ', "extra": 1}',
                   response(0).replace('"intercept": 0', '"unknown": 0'),
                   response(0).replace('"intercept": 0', '"intercept": 0, "intercept": 0')):
        with pytest.raises((ValueError, TypeError)):
            llm.parse_proposal(output, current, .35)


def test_noop_under_25_new_closes(tmp_path, capsys):
    paths = fixture(tmp_path, 24)
    with patch.object(llm.subprocess, "run") as run:
        assert llm.main(args(paths)) == 0
        run.assert_not_called()
    assert "no-op:" in capsys.readouterr().out
    assert not list(paths[2].glob("llm_*"))


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired("codex", 600), OSError("missing"),
    subprocess.CompletedProcess([], 1, "", "error"),
    subprocess.CompletedProcess([], 0, "", "quota exhausted"),
    subprocess.CompletedProcess([], 0, "not json", "")])
def test_fail_closed(tmp_path, failure):
    paths = fixture(tmp_path)
    original = (paths[2] / "allocator_proposal.json").read_bytes()
    with patch.object(llm.subprocess, "run", side_effect=failure if isinstance(failure, Exception) else None,
                      return_value=None if isinstance(failure, Exception) else failure):
        assert llm.main(args(paths)) == 0
    assert (paths[2] / "allocator_proposal.json").read_bytes() == original
    assert not list(paths[2].glob("llm_*"))


def test_insufficient_data_noop(tmp_path):
    paths = fixture(tmp_path)
    (paths[2] / "allocator_proposal.json").write_text('{"verdict":"insufficient_data"}')
    with patch.object(llm.subprocess, "run") as run:
        assert llm.main(args(paths)) == 0
        run.assert_not_called()


@pytest.mark.parametrize("delta,verdict", [(-3, "pass"), (3, "fail")])
def test_gate_wiring_and_outputs(tmp_path, delta, verdict):
    paths = fixture(tmp_path)
    original = (paths[2] / "allocator_proposal.json").read_bytes()
    with patch.object(llm.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, response(delta), "")) as run:
        assert llm.main(args(paths)) == 0
    assert run.call_args.args[0] == ["/usr/bin/codex", "exec", "--model", "gpt-6-sol"]
    assert "Evidence pack:" in run.call_args.kwargs["input"]
    assert run.call_args.kwargs["timeout"] == 600
    reports = list(paths[2].glob("llm_analyst_report_*.md"))
    assert len(reports) == 1 and "Gate metrics" in reports[0].read_text()
    if verdict == "pass":
        proposal = json.loads((paths[2] / "allocator_proposal.json").read_text())
        assert proposal["source"] == "llm_analyst"
        assert proposal["verdict"] == "pass"
        assert all(c["passed"] for c in proposal["gate"]["checks"])
        assert (paths[2] / "llm_analyst_watermark.json").exists()
    else:
        assert (paths[2] / "allocator_proposal.json").read_bytes() == original
        assert not (paths[2] / "llm_analyst_watermark.json").exists()
        assert "is_edge_positive" in reports[0].read_text()
    assert json.loads(paths[1].read_text())["dry_run"] is True
