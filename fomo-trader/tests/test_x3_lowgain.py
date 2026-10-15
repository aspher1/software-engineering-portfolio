"""Hermetic X3 paper-study and fail-open evaluator checks."""

from datetime import datetime
import json

import pytest

from analysis import replay, signal_filter
from analysis.x3_lowgain import x3_lowgain as x3


def _row(i, gain=50, chain="solana", offset=0):
    stamp = f"2026-01-01 12:{i:02d}:00"
    entry = {"type": "entry", "ts": stamp, "name": f"T{i}", "mint": f"T{i}",
             "signal_gain_pct": gain, "signal_ratio": 2}
    trade = {"entry_record": entry, "close_record": {"type": "close"},
             "entry_ts": stamp, "name": f"T{i}", "mint": f"T{i}",
             "chain": chain, "buy_sol": .1, "entry": 1, "exit": 1,
             "realized_sol": .01, "sol_usd": 100}
    return {"trade": trade, "signal": {"name": f"T{i}", "chain": chain,
                                      "m15_gain_pct": gain, "buy_sell_ratio": 2},
            "signal_ts": datetime.fromisoformat(stamp), "utc_offset_hours": offset,
            "line_index": i}


def test_split_floor_and_utc_order():
    rows = [_row(i) for i in range(5)]
    rows[0]["utc_offset_hours"] = 4
    train, holdout = x3.split_rows(list(reversed(rows)))
    assert len(train) == 3 and len(holdout) == 2
    assert [r["trade"]["name"] for r in train] == ["T1", "T2", "T3"]
    assert [r["trade"]["name"] for r in holdout] == ["T4", "T0"]
    assert x3.split_rows([]) == ([], [])


def test_join_population_and_latest_signal(tmp_path):
    lines = ["[11:58:00] FOMO SIGNAL [solana]: CAT +75% 15m, buys/sells 2 (liq $20000)\n",
             "[11:59:00] FOMO SIGNAL [solana]: CAT +99% 15m, buys/sells 2 (liq $20000)\n",
             "[12:00:00] entered CAT @ 1 SOL/token\n"]
    path = tmp_path / "bot.log"
    path.write_text("".join(lines))
    trade = _row(0)["trade"]
    trade["name"] = trade["entry_record"]["name"] = "CAT"
    joined, _ = signal_filter.join_signals(replay.parse_signals(path), [trade], lines)
    eligible, excluded = x3.population(joined)
    assert len(eligible) == 1 and eligible[0]["signal"]["m15_gain_pct"] == 99
    assert excluded["no_close"] == 1
    assert eligible[0]["signal_ts"] < datetime.fromisoformat(trade["entry_ts"])


def test_veto_boundary_and_combo():
    params = {"ship_recommend": True, "rule": {"gain_lt_pct": 100, "ratio_lt": None}}
    assert x3.approve({"m15_gain_pct": 99.9}, params=params)
    assert not x3.approve({"m15_gain_pct": 100}, params=params)
    params["rule"]["ratio_lt"] = 4
    assert not x3.approve({"m15_gain_pct": 99, "buy_sell_ratio": 4}, params=params)
    assert x3.approve({"m15_gain_pct": 99, "buy_sell_ratio": 3.9}, params=params)


@pytest.mark.parametrize("signal", [None, [], {}, {"m15_gain_pct": None},
                                           {"m15_gain_pct": float("nan")},
                                           {"m15_gain_pct": -1},
                                           {"m15_gain_pct": object()}])
def test_bad_signal_fails_open_and_counts(signal):
    params = {"ship_recommend": True, "rule": {"gain_lt_pct": 100, "ratio_lt": None}}
    count = {}
    result = x3.evaluate(signal, params=params, counters=count)
    assert result == {"approved": True, "fallback": True, "reason": "fallback"}
    assert count == {"fallbacks": 1}


@pytest.mark.parametrize("params", [{}, {"ship_recommend": 1, "rule": {"gain_lt_pct": 100}},
                                           {"ship_recommend": True, "rule": {"gain_lt_pct": "oops"}},
                                           {"ship_recommend": True, "rule": {"gain_lt_pct": 100,
                                                                                 "ratio_lt": float("nan")}}])
def test_corrupt_params_fail_open(params):
    count = {}
    assert x3.approve({"m15_gain_pct": 999}, params=params, counters=count)
    assert count["fallbacks"] == 1


def test_bad_file_and_not_shipped(tmp_path):
    path = tmp_path / "params.json"
    path.write_text("{")
    count = {}
    assert x3.approve({"m15_gain_pct": 999}, path=path, counters=count)
    assert count["fallbacks"] == 1
    path.write_text(json.dumps({"ship_recommend": False,
                                "rule": {"gain_lt_pct": 100, "ratio_lt": None}}))
    assert x3.evaluate({"m15_gain_pct": 999}, path=path)["reason"] == "not_shipped"


def test_empty_and_invalid_population_never_raise():
    result = x3.run_study([])
    assert result["ship_recommend"] is False
    assert result["is_n"] == result["oos_n"] == 0
    row = _row(0, gain=float("nan"))
    assert x3.population([row])[1]["invalid_gain"] == 1
    row["signal"]["m15_gain_pct"] = -1
    assert x3.population([row])[1]["invalid_gain"] == 1
