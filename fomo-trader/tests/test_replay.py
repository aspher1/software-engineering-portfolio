import json

import pytest

from analysis import replay


def test_pairing_preserves_records_and_unmatched_close(tmp_path):
    rows = [
        {"type": "close", "mint": "orphan", "ts": "2026-01-01 00:00:00", "entry": 1, "peak": 2, "exit": 1},
        {"type": "entry", "mint": "same", "ts": "2026-01-01 00:01:00", "source": "gecko", "signal_ratio": 2},
        {"type": "close", "mint": "same", "ts": "2026-01-01 00:02:00", "realized_sol": .1},
    ]
    path = tmp_path / "journal.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    trades, counts = replay.load_journal(path)
    assert counts == {"close": 2, "entry": 1, "unmatched_entries": 0, "unmatched_closes": 1}
    assert trades[0]["unmatched_close"]
    assert trades[1]["source"] == "gecko"
    assert trades[1]["entry_record"] == rows[1]
    assert trades[1]["close_record"] == rows[2]
    assert "signal_price_usd" not in trades[1]
    assert replay.attributes(trades[1])["holder_top1_pct"] == "missing"
    assert replay.attributes(trades[1])["lp_evidence"] == "unknown"


def test_new_entry_evidence_survives_pairing_and_buckets(tmp_path):
    rows = [
        {"type": "entry", "mint": "x", "ts": "2026-01-01 00:00:00",
         "signal_price_usd": .01, "commit_price_native": .0001,
         "commit_price_usd": .011, "slip_from_signal_pct": 10,
         "holder_top1_pct": 25, "holder_top5_pct": 60,
         "lp_burn_pct": None, "lp_locked": True, "entry_latency_ms": 200,
         "m15_buys": 20, "m15_sells": 5, "m15_volume_usd": 5000,
         "mcap_usd": 100000},
        {"type": "close", "mint": "x", "ts": "2026-01-01 01:00:00",
         "entry": .0001, "peak": .0002, "exit": .00015,
         "buy_sol": .1, "realized_sol": .05},
    ]
    path = tmp_path / "journal.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    trades, _ = replay.load_journal(path)
    trade = trades[0]
    for key, value in rows[0].items():
        if key not in ("type", "ts"):
            assert trade[key] == value
    assert replay.attributes(trade)["holder_top1_pct"] == "25-40"
    assert replay.attributes(trade)["lp_evidence"] == "locked"
    assert replay.attribution(trades)["holder_top1_pct"]["25-40"]["n"] == 1
    assert replay.attribution(trades)["lp_evidence"]["locked"]["n"] == 1


@pytest.mark.parametrize("top1,bucket", [(None, "missing"), (9.9, "<10"),
                                           (10, "10-25"), (40, "25-40"),
                                           (40.1, ">40")])
def test_holder_bucket_boundaries(top1, bucket):
    assert replay.attributes({"holder_top1_pct": top1})["holder_top1_pct"] == bucket


@pytest.mark.parametrize("burn,locked,bucket", [(None, None, "unknown"),
                                                  (20, None, "burned"),
                                                  (None, True, "locked"),
                                                  (0, False, "unknown")])
def test_lp_bucket_nulls(burn, locked, bucket):
    assert replay.attributes({"lp_burn_pct": burn,
                              "lp_locked": locked})["lp_evidence"] == bucket


def test_signal_formats(tmp_path):
    path = tmp_path / "bot.log"
    path.write_text("[12:00:00] FOMO SIGNAL [solana]: CAT +123.4% 15m, buys/sells 2.1 (liq $15000) [geckoterminal] https://example.test/a\n"
                    "[12:01:00] PRE-PUMP [bsc]: DOG 5m +12% m5 / +45% m15, buys/sells 1.8 (liq $20000) https://example.test/b\n")
    rows = replay.parse_signals(path)
    assert len(rows) == 2
    assert rows[0]["m15_gain_pct"] == 123.4
    assert rows[1]["early"] and rows[1]["m5_gain_pct"] == 12
    assert rows[1]["chain"] == "bsc"


def test_counterfactual_trail_stop_and_rung():
    t = {"entry": 100, "peak": 200, "exit": 130, "reason": "trailing stop -35% from peak"}
    assert replay.counterfactual_exit(t, [], 20, 40) == 160
    assert replay.counterfactual_exit(t, [(50, 50)], 20, 40) == 155
    gap = {"entry": 100, "peak": 110, "exit": 20, "reason": "venue dump"}
    assert replay.counterfactual_exit(gap, [], 30, 35) == 65


def test_costs_and_stress():
    t = {"chain": "solana", "buy_sol": .1, "entry": 1, "exit": 1}
    config = {"exit": {"max_priority_fee_lamports": 2_000_000}}
    assert replay.estimated_cost(t, config) == pytest.approx(.0048)
    assert replay.estimated_cost(t, config, stress=True) == pytest.approx(.0054)
    bsc = {"chain": "bsc", "buy_bnb": .01, "entry": 1, "exit": 1}
    assert replay.estimated_cost(bsc) == pytest.approx(.00012)


def test_attribution_bucket_and_rug_gap():
    t = {"mint": "x", "entry_ts": "2026-01-01 13:00:00", "liquidity_usd": 50000,
         "signal_gain_pct": 200, "signal_ratio": 4, "source": "geckoterminal",
         "entry": 1, "peak": 10, "exit": 3, "buy_sol": .1, "realized_sol": -.05}
    assert replay.attributes(t) == {"chain": "solana", "liquidity": ">50k", "gain": "200-400",
                                    "ratio": ">4", "hour_utc": "13", "source": "geckoterminal",
                                    "holder_top1_pct": "missing", "lp_evidence": "unknown"}
    assert replay.rug_gap(t)
    assert replay.attribution([t])["loss_reason"]["rug-gap"]["n"] == 1
