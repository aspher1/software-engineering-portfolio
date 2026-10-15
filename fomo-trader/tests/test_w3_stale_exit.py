"""Hermetic W3 replay checks; no live bot import."""

from collections import Counter
from datetime import datetime, timezone
import json

import pytest

from analysis import replay
from analysis.w3_stale_exit import w3_stale_exit as w3


def row(minute, duration, chain="solana", realized=0.01, mint=None):
    base = datetime(2026, 1, 1, 0, 0)
    from datetime import timedelta
    entry = base + timedelta(minutes=minute)
    close = entry + timedelta(seconds=duration)
    return {"chain": chain, "mint": mint or ("0xabc" if chain == "bsc" else "abc"),
            "entry_ts": entry.isoformat(sep=" "), "close_ts": close.isoformat(sep=" "),
            "entry": 1, "peak": 1.2, "exit": 0.9, "buy_sol": 0.1,
            "realized_bnb" if chain == "bsc" else "realized_sol": realized,
            "sol_usd": 100}


def test_conservative_pnl_and_stress_use_replay_costs():
    for chain in ("solana", "bsc"):
        for realized in (0.02, -0.02):
            trade = row(0, 600, chain, realized)
            for multiplier in (1, 3):
                adjusted = w3._adjust(trade, True, multiplier)
                actual = replay.net_pnl(trade, cost_multiplier=multiplier)[0]
                got = replay.net_pnl(adjusted, cost_multiplier=multiplier)[0]
                assert got == pytest.approx(min(0, actual))
                assert trade["realized_bnb" if chain == "bsc" else "realized_sol"] == realized
                assert w3._adjust(trade, False, multiplier) is trade


def test_sweep_shape_metrics_and_duplicate_mints():
    trades = [row(i * 10, 700 if i % 2 else 100, "bsc" if i % 3 else "solana",
                  0.02 if i % 2 else -0.02, "0xrepeat" if i % 3 else "repeat")
              for i in range(12)]
    result = w3.sweep(trades, datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert result["split_n"] == {"is": 8, "oos": 4}
    assert [x["timeout_s"] for x in result["results"]] == list(w3.TIMEOUTS)
    for candidate in result["results"]:
        assert set(candidate["is"]) == set(w3.METRIC_KEYS)
        assert set(candidate["oos"]) == set(w3.METRIC_KEYS)
        assert set(candidate["stress_oos"]) == set(w3.METRIC_KEYS)
        assert set(candidate["chain_oos"]) == {"solana", "bsc"}
        assert isinstance(candidate["pass"], bool)
    assert result["counters"] == {}


def test_adversarial_fallbacks_are_counted_and_logged(caplog):
    good = row(0, 600)
    cases = []
    cases.append(None)  # malformed
    for field, value in (("entry", None), ("peak", float("nan")), ("exit", "bad"),
                         ("realized_sol", None)):
        bad = dict(good); bad[field] = value; cases.append(bad)
    for field in ("entry_ts", "close_ts"):
        bad = dict(good); bad.pop(field); cases.append(bad)
    bad = dict(good); bad["entry_ts"] = "2026-01-01 01:00:00"; cases.append(bad)
    bad = dict(good); bad["close_ts"] = "2099-01-01 00:00:00"; cases.append(bad)
    bad = dict(good); bad["close_ts"] = "2026-01-01 04:00:00"; cases.append(bad)
    bad = dict(good); bad["entry_ts"] = "2026-01-01T00:00:00+00:00"; cases.append(bad)
    ordered, counters, valid = w3.prepare(cases, datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert counters == Counter(malformed_trade=1, invalid_economics=4,
                               missing_or_mixed_timestamp=3, clock_skew=1,
                               future_timestamp=1, ambiguous_four_hour_clock=1)
    assert len(caplog.records) == sum(counters.values())
    assert valid == 6 and len(ordered) == 5  # missing entry time cannot enter the split
    assert all(duration is None for _, _, duration in ordered)


def test_bsc_stake_alias_and_same_mint_independent():
    a, b = row(0, 600, "bsc", 0.02, "0xduplicate"), row(1, 30, "bsc", -0.02, "0xduplicate")
    c = row(2, 20, "bsc", -0.01, "0xother")
    c["buy_bnb"] = c.pop("buy_sol")
    ordered, counts, _ = w3.prepare([a, b, c], datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert not counts
    assert [duration for _, _, duration in ordered] == [600, 30, 20]
    assert w3._score(ordered, 180)["n"] == 3


def test_params_schema_and_missing_corrupt_fallback(tmp_path, caplog):
    path = w3.PARAMS
    data = json.loads(path.read_text())
    assert set(data) == {"track", "ship_recommend", "rule", "timeouts_swept",
                         "best_timeout", "is_metrics", "oos_metrics", "gates", "notes"}
    assert data["track"] == "W3"
    assert type(data["ship_recommend"]) is bool
    assert isinstance(data["rule"], str)
    assert data["timeouts_swept"] == list(w3.TIMEOUTS)
    assert w3.load_params(path) == data
    counts = Counter()
    assert w3.load_params(tmp_path / "missing.json", counts)["ship_recommend"] is False
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{oops")
    assert w3.load_params(corrupt, counts)["ship_recommend"] is False
    corrupt.write_text('{"ship_recommend": 1, "rule": "x"}')
    assert w3.load_params(corrupt, counts)["ship_recommend"] is False
    assert counts == {"params_unavailable": 3}
    assert len(caplog.records) == 3
