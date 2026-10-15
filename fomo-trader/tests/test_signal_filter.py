import json
import time
from datetime import datetime

import pytest

from analysis import replay, signal_filter as sf


def _trade(name, ts, realized, *, closed=True):
    entry = {"type": "entry", "name": name, "mint": name, "ts": ts,
             "signal_gain_pct": 40, "signal_ratio": 2, "liquidity_usd": 20000}
    trade = {"name": name, "mint": name, "entry_record": entry, "entry_ts": ts,
             "chain": "solana", "buy_sol": .1, "entry": 1, "exit": 1,
             "realized_sol": realized}
    trade["close_record"] = {"type": "close"} if closed else None
    return trade


def test_join_normalization_window_and_unmatched(tmp_path):
    lines = [
        "[11:59:00] FOMO SIGNAL [solana]:  CAT   / SOL +40% 15m, buys/sells 2.0 (liq $20000) [geckoterminal]\n",
        "[12:00:00] entered cat / sol @ 1 SOL/token\n",
        "[12:00:00] FOMO SIGNAL [solana]: DOG +40% 15m, buys/sells 2.0 (liq $20000) [geckoterminal]\n",
        "[12:01:00] entered DOG @ 1 SOL/token\n",
        "[12:02:00] FOMO SIGNAL [solana]: GUARD +40% 15m, buys/sells 2.0 (liq $20000) [geckoterminal]\n",
        "[12:02:05] RUG-GUARD SKIP GUARD: no sell route\n",
        "[12:03:00] FOMO SIGNAL [solana]: ORPHAN +40% 15m, buys/sells 2.0 (liq $20000) [geckoterminal]\n",
        "[12:04:00] FOMO SIGNAL [solana]: LATE +40% 15m, buys/sells 2.0 (liq $20000) [geckoterminal]\n",
        "[12:35:00] entered LATE @ 1 SOL/token\n",
    ]
    path = tmp_path / "bot.log"
    path.write_text("".join(lines))
    signals = replay.parse_signals(path)
    trades = [_trade("cat / sol", "2026-01-01 12:00:00", .02),
              _trade("DOG", "2026-01-01 12:01:00", -.02, closed=False),
              _trade("LATE", "2026-01-01 12:35:00", .02)]
    rows, meta = sf.join_signals(signals, trades, lines)
    assert [r["status"] for r in rows] == ["entered-winner", "entered-unmatched-close",
                                              "skipped-by-guard", "never-entered", "never-entered"]
    assert meta["entry_anchors"] == 3


def test_midnight_join_and_latest_signal(tmp_path):
    lines = ["[23:58:00] FOMO SIGNAL [solana]: CAT +40% 15m, buys/sells 2.0 (liq $20000)\n",
             "[23:59:00] FOMO SIGNAL [solana]: CAT +45% 15m, buys/sells 2.0 (liq $20000)\n",
             "[00:01:00] entered CAT @ 1 SOL/token\n"]
    path = tmp_path / "bot.log"
    path.write_text("".join(lines))
    rows, _ = sf.join_signals(replay.parse_signals(path),
                              [_trade("CAT", "2026-01-02 00:01:00", .02)], lines)
    assert [r["status"] for r in rows] == ["never-entered", "entered-winner"]
    assert rows[1]["signal_ts"].isoformat() == "2026-01-01T23:59:00"


def test_buckets_and_attribution():
    base = {"name": "X", "chain": "solana", "early": False, "source": "unknown"}
    rows = [
        {"signal": {**base, "m15_gain_pct": 99, "buy_sell_ratio": 1.9,
                    "liquidity_usd": 9999}, "status": "entered-winner",
         "trade": _trade("X", "2026-01-01 01:00:00", .02), "signal_ts": None},
        {"signal": {**base, "m15_gain_pct": 100, "buy_sell_ratio": 4,
                    "liquidity_usd": 10000}, "status": "entered-loser",
         "trade": _trade("X", "2026-01-01 02:00:00", -.02), "signal_ts": None},
        {"signal": {**base, "m15_gain_pct": 100, "buy_sell_ratio": 4,
                    "liquidity_usd": 10000}, "status": "never-entered",
         "trade": None, "signal_ts": None},
    ]
    out = sf.attribution_rows(rows, {})
    assert out["m15_gain_pct"]["<100"]["entered"] == 1
    assert out["m15_gain_pct"]["100-200"]["never"] == 1
    assert out["buy_sell_ratio"][">=4"]["metrics"]["win_rate"] == 0
    assert sf.feature_bucket("hour_utc", 23) == "18-23"
    assert sf.feature_bucket("m15_buys", None) == "missing"
    rows[0]["signal_ts"] = datetime(2026, 9, 24, 20, 0)
    rows[0]["utc_offset_hours"] = 4
    assert sf.signal_values(rows[0])["hour_utc"] == 0


def test_launcher_timezone_markers():
    lines = ["[14:20:46] wallet sol=x | DRY RUN: True\n",
             "[14:21:00] FOMO SIGNAL [solana]: CAT +40% 15m, buys/sells 2 (liq $20000)\n",
             "[18:26:19] wallet sol=x | DRY RUN: True\n"]
    deaths = ["[2026-09-24 14:20:45 EDT] bot started pid 1\n",
              "[2026-09-24 18:26:18 UTC] bot started pid 2\n"]
    assert sf.timezone_regimes(lines, deaths) == [(0, 4), (2, 0)]


def test_approve_veto_missing_and_loaded_once(tmp_path):
    sf._params_cache.clear()
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps({"thresholds": {"buy_sell_ratio": 4}, "ship_recommend": True}))
    assert sf.approve({"buy_sell_ratio": 3.9}, path=path)
    assert not sf.approve({"buy_sell_ratio": 4}, path=path)
    assert sf.approve({}, path=path)
    path.write_text("{broken")
    assert not sf.approve({"buy_sell_ratio": 5}, path=path)
    assert len(sf._params_cache) == 1


@pytest.mark.parametrize("content", [None, "{broken", "{}"])
def test_fail_open_file(content, tmp_path):
    sf._params_cache.clear()
    path = tmp_path / "params.json"
    if content is not None:
        path.write_text(content)
    logs = []
    assert sf.approve({"buy_sell_ratio": 9}, path=path, log=logs.append)
    assert logs == ["SIGNAL FILTER FALLBACK"]


@pytest.mark.parametrize("signal", [None, [], {"buy_sell_ratio": object()}])
def test_fail_open_malformed_signal(signal, tmp_path):
    sf._params_cache.clear()
    path = tmp_path / "params.json"
    path.write_text(json.dumps({"thresholds": {"buy_sell_ratio": 4}, "ship_recommend": True}))
    logs = []
    assert sf.approve(signal, path=path, log=logs.append)
    assert logs == ["SIGNAL FILTER FALLBACK"]


def test_approve_timing(tmp_path):
    sf._params_cache.clear()
    path = tmp_path / "params.json"
    path.write_text(json.dumps({"thresholds": {"buy_sell_ratio": 4}, "ship_recommend": True}))
    cold_start = time.perf_counter()
    assert sf.approve({"buy_sell_ratio": 2}, path=path)
    assert time.perf_counter() - cold_start < .05
    start = time.perf_counter()
    for _ in range(1000):
        assert sf.approve({"buy_sell_ratio": 2}, path=path)
    assert (time.perf_counter() - start) / 1000 < .05
