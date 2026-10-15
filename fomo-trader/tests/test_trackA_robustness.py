"""Hermetic reproducers and golden cases for Track A."""
import json
import math
import threading
import time
from unittest.mock import patch

import pytest

from analysis.replay import load_journal
from fomo_trader import ApiThrottled, DexScreenerSource, Hunter, SOL_MINT, Trader, _bot_today_str, to_f


CFG = {"hunter": {"solana_pages_trending": 1, "solana_pages_new": 0,
                  "bsc_pages_trending": 0, "bsc_pages_new": 0,
                  "min_m15_gain_pct": 20, "min_buy_sell_ratio": 1.5,
                  "min_liquidity_usd": 8000, "min_m15_volume_usd": 5000,
                  "min_m15_buys": 10, "min_m15_sells": 5,
                  "early": {"enabled": False}},
       "risk": {"max_trades_per_day": 10, "max_trades_per_hour": 3}}


def pool(addr="POOL", **changes):
    attrs = {"address": addr, "name": "TEST / SOL",
             "price_change_percentage": {"m15": 30},
             "reserve_in_usd": 20000, "volume_usd": {"m15": 6000},
             "transactions": {"m15": {"buys": 20, "sells": 10}}}
    attrs.update(changes)
    return {"attributes": attrs,
            "relationships": {"base_token": {"data": {"id": "solana_" + addr}},
                              "quote_token": {"data": {"id": "solana_" + SOL_MINT}}}}


def gt_scan(items):
    with patch("fomo_trader.gt_get", return_value={"data": items}):
        return Hunter(CFG)._scan_gt()


def pair(**changes):
    p = {"chainId": "solana", "pairAddress": "POOL",
         "baseToken": {"address": "MINT", "name": "TEST"},
         "quoteToken": {"address": SOL_MINT},
         "priceChange": {"m5": 10}, "volume": {"m5": 3000},
         "txns": {"m5": {"buys": 20, "sells": 10}},
         "liquidity": {"usd": 20000}}
    p.update(changes)
    return p


def bare_trader():
    t = Trader.__new__(Trader)
    t.cfg = CFG
    t.lock = threading.RLock()
    t.state = {"positions": {}, "cooldown": {}, "trades_today": [],
               "trades_this_hour": [], "day": _bot_today_str(),
               "realized_sol": 0.0}
    return t


def test_gt_normal_signal_golden():
    rows, healthy = gt_scan([pool()])
    assert healthy and len(rows) == 1
    assert {k: rows[0][k] for k in ("m15_gain_pct", "liquidity_usd", "m15_volume_usd", "buy_sell_ratio")} == {
        "m15_gain_pct": 30.0, "liquidity_usd": 20000, "m15_volume_usd": 6000, "buy_sell_ratio": 2.0}


@pytest.mark.parametrize("field,value", [
    ("price_change_percentage", {"m15": float("nan")}),
    ("reserve_in_usd", float("nan"))])
def test_gt_nan_fails_closed(field, value):
    rows, healthy = gt_scan([pool(**{field: value})])
    assert healthy and rows == []


def test_to_f_finite_golden_and_nonfinite_reproducer():
    assert [to_f(x) for x in ("1.25", -4, None, "bad")] == [1.25, -4.0, 0.0, 0.0]
    assert to_f(float("nan")) == 0.0
    assert to_f(float("inf")) == 0.0


def test_ds_normal_golden_and_nan_fails_closed():
    source = DexScreenerSource(CFG)
    normal = source._signal(pair())
    assert (normal["m15_gain_pct"], normal["liquidity_usd"], normal["m15_volume_usd"]) == (10.0, 20000, 3000)
    assert source._signal(pair(priceChange={"m5": float("nan")})) is None
    assert source._signal(pair(liquidity={"usd": float("nan")})) is None


def test_gt_null_page_does_not_abort_scan():
    with patch("fomo_trader.gt_get", return_value={"data": None}):
        assert Hunter(CFG)._scan_gt() == ([], True)


def test_gt_bad_pool_does_not_hide_good_pool():
    bad = pool("BAD", transactions={"m15": {"buys": "abc", "sells": 10}})
    rows, healthy = gt_scan([bad, pool("GOOD")])
    assert healthy and [r["mint"] for r in rows] == ["GOOD"]


def test_gt_non_object_pool_does_not_hide_good_pool():
    rows, healthy = gt_scan([None, pool("GOOD")])
    assert healthy and [r["mint"] for r in rows] == ["GOOD"]


def test_gt_bad_early_pool_does_not_hide_good_early_pool():
    cfg = {**CFG, "hunter": {**CFG["hunter"], "early": {"enabled": True,
           "min_m15_gain_pct": 15, "min_m5_gain_pct": 5, "accel_ratio": 1.2}}}
    bad = pool("BAD", price_change_percentage={"m15": 18, "m5": 10},
               transactions={"m15": {"buys": "abc", "sells": 10}})
    good = pool("GOOD", price_change_percentage={"m15": 18, "m5": 10})
    with patch("fomo_trader.gt_get", return_value={"data": [bad, good]}):
        hunter = Hunter(cfg)
        rows, healthy = hunter._scan_gt()
    assert healthy and rows == []
    assert [r["mint"] for r in hunter.last_early] == ["GOOD"]


@pytest.mark.parametrize("payload", [[], {}])
def test_valid_json_wrong_state_schema_falls_back(tmp_path, payload):
    state = tmp_path / "state.json"
    state.write_text(json.dumps(payload))
    class Key:
        def pubkey(self):
            return "owner"
    cfg = {"wallets": {"solana_key_file": "unused"}, "dry_run": True}
    with patch("keystore.load_solana_keypair", return_value=Key()):
        trader = Trader(cfg, str(state))
    assert trader.state["positions"] == {}
    assert trader.state["cooldown"] == {}
    assert trader.state["trades_today"] == []
    assert trader.dry_run is True


def test_replay_skips_corrupt_line_and_preserves_pairing(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text('{"type":"entry","mint":"x"}\n{bad\n{"type":"close","mint":"x"}\n')
    rows, counts = load_journal(path)
    assert len(rows) == 1 and not rows[0]["unmatched_close"]
    assert counts == {"entry": 1, "close": 1, "malformed_lines": 1,
                      "unmatched_entries": 0, "unmatched_closes": 0}


def test_unlocked_prune_can_lose_commit_append():
    t = bare_trader()
    start = threading.Event()
    appended = threading.Event()
    def append():
        assert start.wait(1)
        with t.lock:
            t.state["trades_this_hour"].append(time.time())
        appended.set()
    class PausedList(list):
        def __iter__(self):
            snapshot = iter(tuple(list.__iter__(self)))
            start.set()
            appended.wait(.05)
            return snapshot
    t.state["trades_this_hour"] = PausedList()
    worker = threading.Thread(target=append)
    worker.start()
    t._prune_hour_trades()
    worker.join(1)
    assert not worker.is_alive()
    assert len(t.state["trades_this_hour"]) == 1


def test_commit_cap_golden():
    t = bare_trader()
    signal = {"mint": "x", "name": "x", "ts": time.time()}
    assert t._entry_commit_ok(signal)
    t.state["trades_today"] = [time.time()] * 10
    assert not t._entry_commit_ok(signal)


def test_commit_check_and_append_one_slot_for_two_threads():
    t = bare_trader()
    t.cfg = {**CFG, "risk": {"max_trades_per_day": 1,
                            "max_trades_per_hour": 1}}
    barrier = threading.Barrier(2)
    accepted = []
    def commit(mint):
        signal = {"mint": mint, "name": mint, "ts": time.time()}
        barrier.wait()
        with t.lock:
            if t._entry_commit_ok(signal):
                t.state["trades_today"].append(time.time())
                t.state["trades_this_hour"].append(time.time())
                accepted.append(mint)
    threads = [threading.Thread(target=commit, args=(x,)) for x in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(1)
        assert not thread.is_alive()
    assert len(accepted) == len(t.state["trades_today"]) == len(t.state["trades_this_hour"]) == 1


def test_roll_day_and_prune_preserve_normal_values():
    t = bare_trader()
    now = time.time()
    t.state["trades_today"] = [now - 100]
    t.state["trades_this_hour"] = [now - 3700, now - 100]
    assert t._prune_hour_trades() == [now - 100]
    t._roll_day()
    assert t.state["trades_today"] == [now - 100]
    t.state["day"] = "old day"
    t._roll_day()
    assert t.state["trades_today"] == []
    assert t.state["trades_this_hour"] == [now - 100]


def test_direct_save_waits_for_state_lock(tmp_path):
    t = bare_trader()
    t.state_path = str(tmp_path / "state.json")
    started = threading.Event()
    finished = threading.Event()
    def save():
        started.set()
        t.save()
        finished.set()
    with t.lock:
        worker = threading.Thread(target=save)
        worker.start()
        assert started.wait(1)
        assert not finished.wait(.05)
    worker.join(1)
    assert finished.is_set()
    assert json.loads((tmp_path / "state.json").read_text()) == t.state
    # Existing nested save sites remain valid with a reentrant lock.
    with t.lock:
        t.save()


@pytest.mark.parametrize("mode,expect_fallback", [("all_fail", True), ("partial", False), ("throttle", True)])
def test_gt_fallback_only_zero_pages(mode, expect_fallback):
    calls = []
    def gt(*_args, **_kwargs):
        calls.append(1)
        if mode == "throttle":
            raise ApiThrottled("test")
        if mode == "all_fail" or len(calls) == 1:
            raise OSError("test")
        return {"data": []}
    cfg = {**CFG, "hunter": {**CFG["hunter"], "solana_pages_trending": 2}}
    with patch("fomo_trader.gt_get", side_effect=gt), patch.object(DexScreenerSource, "scan", return_value=[{"source": "dexscreener"}]) as ds:
        result = Hunter(cfg).scan()
    assert ds.called == expect_fallback
    assert result == ([{"source": "dexscreener"}] if expect_fallback else [])
