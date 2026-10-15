import json
import threading
import time
from unittest.mock import patch

import pytest

from analysis import replay
from fomo_trader import Trader, _bot_today_str


def paper_trader(tmp_path):
    trader = object.__new__(Trader)
    trader.cfg = {"risk": {"max_open_positions": 3, "max_trades_per_day": 10,
                            "max_trades_per_hour": 3, "cooldown_min_per_mint": 10,
                            "buy_sol_per_trade": .06},
                  "exit": {"take_profits": []}}
    trader.dry_run = True
    trader.state_path = str(tmp_path / "state.json")
    trader.state = {"positions": {}, "cooldown": {}, "trades_today": [],
                    "trades_this_hour": [], "day": _bot_today_str(),
                    "realized_sol": 0, "realized_bnb": 0,
                    "realized_usd": 0}
    trader.lock = threading.RLock()
    trader._px_hist = {}
    return trader


def test_dump_cooldown_veto_expiry_and_persistence(tmp_path):
    trader = paper_trader(tmp_path)
    signal = {"mint": "mint", "name": "COIN", "ts": time.time()}
    pos = {"name": "COIN", "entry": 1, "peak": 1.1, "buy_sol": .06,
           "rungs_fired": []}
    trader.state["positions"]["mint"] = pos
    with patch("fomo_trader.log") as logger, \
         patch.object(trader, "native_usd", return_value=100), \
         patch.object(trader, "sol_usd", return_value=100), \
         patch.object(trader, "_journal"):
        trader.close_trade("mint", pos, .8, "dump detector -20% in 60s",
                           dump_exit=True)
        saved = json.loads((tmp_path / "state.json").read_text())
        assert saved["dump_cooldown"]["mint"] > 0
        assert not trader.guardrails_ok(signal)
        logger.assert_any_call("DUMP-COOLDOWN SKIP COIN")
        assert not trader._entry_commit_ok(signal)
        restored = paper_trader(tmp_path)
        restored.state = saved
        assert not restored.guardrails_ok(signal)
        restored.state["dump_cooldown"]["mint"] = time.time() - 24 * 3600 - 1
        assert restored.guardrails_ok(signal)


def test_venue_dump_records_but_other_close_does_not(tmp_path):
    trader = paper_trader(tmp_path)
    with patch.object(trader, "native_usd", return_value=100), \
         patch.object(trader, "sol_usd", return_value=100), \
         patch.object(trader, "_journal"):
        for mint, reason, dump_exit in (
                ("venue", "venue dump: m5 -100%", True),
                ("normal", "trailing stop -25% from peak", False)):
            pos = {"name": mint, "entry": 1, "peak": 1, "buy_sol": .06,
                   "rungs_fired": []}
            trader.state["positions"][mint] = pos
            trader.close_trade(mint, pos, .9, reason, dump_exit=dump_exit)
    saved = json.loads((tmp_path / "state.json").read_text())
    assert "venue" in saved["dump_cooldown"]
    assert "normal" not in saved["dump_cooldown"]
    assert trader.guardrails_ok({"mint": "normal", "name": "normal"})


def test_counterfactual_dump_remaining_bag_and_non_dump():
    config = {"exit": {"take_profits": [[50, 50]]}}
    trade = {"chain": "bsc", "entry": 100, "peak": 160, "exit": 80,
             "buy_sol": .01, "realized_bnb": .001, "rungs": [[0, 50]],
             "reason": "dump detector -50% in 60s"}
    changed = replay.counterfactual_dump_trade(trade, 10, config)
    assert changed["exit"] == pytest.approx(144)
    assert changed["realized_bnb"] == pytest.approx(.0042)
    assert trade["exit"] == 80
    assert replay.counterfactual_dump_trade(dict(trade, reason="hard stop"), 10, config)["exit"] == 80
    assert replay.counterfactual_dump_trade(dict(trade, exit=150), 10, config)["exit"] == 150


def test_threefold_total_cost_stress():
    trade = {"chain": "solana", "buy_sol": .1, "entry": 1, "exit": 1,
             "realized_sol": .01, "sol_usd": 100}
    base = replay.net_pnl(trade)[0]
    stressed = replay.net_pnl(trade, cost_multiplier=3)[0]
    assert stressed == pytest.approx(base - 2 * replay.estimated_cost(trade))


def test_filter_sweep_uses_is_only_and_keeps_missing():
    rows = [{"chain": "solana", "buy_sol": .1, "entry": 1, "exit": 1,
             "realized_sol": pnl, "sol_usd": 100, "signal_gain_pct": gain}
            for pnl, gain in [(-.1, 300), (.1, 100), (-.05, None),
                              (.1, 200), (-.1, 300), (.1, 100)]]
    baseline, results = replay.filter_sweep(
        rows, "signal_gain_pct", [150, 250], lambda value, cap: value <= cap)
    assert baseline[0]["n"] == 4 and baseline[1]["n"] == 2
    assert results[0]["metrics"][0]["n"] == 2  # winner + missing entry
    assert results[0]["metrics"][1]["n"] == 1
    assert results[0]["edge_per_trade"][0] > 0
    assert results[0]["stress"][1]["usd"] < results[0]["metrics"][1]["usd"]


def test_journal_append_order_survives_mixed_clock_labels(tmp_path):
    path = tmp_path / "trades.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in [
        {"type": "close", "mint": "a", "ts": "2026-09-27 18:20:21"},
        {"type": "close", "mint": "b", "ts": "2026-09-27 14:23:59"},
    ]) + "\n")
    trades, _ = replay.load_journal(path)
    assert [t["mint"] for t in trades] == ["a", "b"]
