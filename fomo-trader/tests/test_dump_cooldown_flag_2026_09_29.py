"""Dump cooldown is armed by an explicit dump_exit flag, not reason text.

Drives the real exit path (_manage_once -> close_trade) on a hermetic,
tmp_path-backed Trader: no keys, no network, no real runs/ state.
"""
import json
import threading
import time
from collections import deque
from unittest.mock import patch

import pytest

import fomo_trader
from fomo_trader import Trader

ENTRY = 1.0e-9
DAY = 24 * 60 * 60
MINT = "CD1111111111111111111111111111111111111111"


def make_trader(tmp_path, price, venue_m5=None):
    t = object.__new__(Trader)
    t.cfg = {
        "exit": {"take_profits": [], "trailing_stop_pct": 25,
                 "trail_after_tp_pct": 12, "hard_stop_pct": 35,
                 "dump_drop_pct": 12, "dump_window_sec": 60,
                 "venue_dump_m5_pct": -30, "venue_check_sec": 30,
                 "stale_exit_min": 30, "stale_exit_max_gain_pct": 10},
        "risk": {},
    }
    t.dry_run = True
    t.state_path = str(tmp_path / "state.json")
    t.state = {"positions": {}, "realized_sol": 0.0, "realized_bnb": 0.0,
               "realized_usd": 0.0}
    t.lock = threading.RLock()
    t._px_hist = {}
    t.journaled = []
    t._roll_day = lambda: None
    t._risk_halted = lambda: False
    t.price_native_fast = lambda mint, chain="solana": price
    t.venue_m5_dump = lambda mint, chain="solana": venue_m5
    t.sell_pct_of_balance = lambda mint, pct, label: ("paper-sig", 0.05)
    t.token_balance_raw = lambda mint: 0
    t.native_usd = lambda chain="solana": 100.0
    t.sol_usd = lambda: 100.0
    t.bnb_usd = lambda: 800.0
    t._journal = t.journaled.append
    return t


def add_position(t, peak=ENTRY, age_min=1, rungs=None):
    t.state["positions"][MINT] = {
        "name": "TEST/CD", "entry": ENTRY, "peak": peak, "buy_sol": 0.06,
        "tokens_raw": 0, "sold_sol": 0.05, "decimals": 9,
        "opened_at": time.time() - age_min * 60, "rungs_fired": rungs or [],
    }


def manage(t, tps=(), trail=25, hard=35):
    with patch.object(fomo_trader.time, "sleep", lambda s: None), \
         patch("fomo_trader.log"):
        closed = t._manage_once(MINT, list(tps), trail, hard)
    assert closed and MINT not in t.state["positions"]
    return t.journaled[-1]["reason"]


def dump_detector_trader(tmp_path):
    high = ENTRY * 2.0
    t = make_trader(tmp_path, high * 0.78)
    add_position(t, peak=high)
    now = time.time()
    t._px_hist[MINT] = deque([(now - 30, high), (now - 5, high)], maxlen=300)
    return t


def venue_dump_trader(tmp_path):
    t = make_trader(tmp_path, ENTRY * 1.10, venue_m5=-45.0)
    add_position(t, peak=ENTRY * 1.10)
    return t


def assert_armed_24h(t):
    armed_at = t.state["dump_cooldown"][MINT]
    assert time.time() - armed_at < 5
    saved = json.loads(open(t.state_path).read())
    assert saved["dump_cooldown"][MINT] == armed_at
    sig = {"mint": MINT, "name": "TEST/CD"}
    with patch("fomo_trader.log"):
        assert t._dump_cooldown_active(sig)
        t.state["dump_cooldown"][MINT] = time.time() - DAY + 60
        assert t._dump_cooldown_active(sig)
        t.state["dump_cooldown"][MINT] = time.time() - DAY - 1
        assert not t._dump_cooldown_active(sig)


def test_dump_detector_close_arms_cooldown(tmp_path):
    t = dump_detector_trader(tmp_path)
    reason = manage(t)
    assert reason.startswith("dump detector -22.0% in 60s")
    assert_armed_24h(t)


def test_venue_dump_close_arms_cooldown(tmp_path):
    t = venue_dump_trader(tmp_path)
    reason = manage(t)
    assert reason == "venue dump: DexScreener m5 -45.0%, selling all"
    assert_armed_24h(t)


@pytest.mark.parametrize("label, setup, kwargs, prefix", [
    ("trailing", lambda t: add_position(t, peak=ENTRY * 1.40), {},
     "trailing stop"),
    ("hard", lambda t: add_position(t), {"trail": 50}, "hard stop"),
    ("stale", lambda t: add_position(t, age_min=60), {}, "stale exit"),
    ("take_profit", lambda t: add_position(t), {"tps": [[50, 100]]},
     "take profit"),
])
def test_non_dump_close_does_not_arm(tmp_path, label, setup, kwargs, prefix):
    price = {"trailing": 1.02, "hard": 0.60, "stale": 1.0,
             "take_profit": 1.60}[label] * ENTRY
    t = make_trader(tmp_path, price)
    setup(t)
    reason = manage(t, **kwargs)
    assert reason.startswith(prefix)
    assert MINT not in t.state.get("dump_cooldown", {})


@pytest.mark.parametrize("build, old, new", [
    (dump_detector_trader, "dump detector", "rapid decline"),
    (venue_dump_trader, "venue dump", "tape collapse"),
])
def test_reworded_dump_reason_still_arms(tmp_path, build, old, new):
    t = build(tmp_path)
    real_close = t.close_trade

    def reworded_close(mint, pos, exit_price, reason, *a, **kw):
        assert old in reason
        return real_close(mint, pos, exit_price, reason.replace(old, new),
                          *a, **kw)
    t.close_trade = reworded_close
    reason = manage(t)
    assert new in reason and old not in reason
    assert_armed_24h(t)


def test_close_trade_arms_only_on_flag(tmp_path):
    t = make_trader(tmp_path, ENTRY)
    with patch("fomo_trader.log"):
        for mint, reason, flag in (("flagged", "rapid decline", True),
                                   ("text-only", "dump detector -20%", False)):
            add_position(t)
            pos = t.state["positions"].pop(MINT)
            t.state["positions"][mint] = pos
            t.close_trade(mint, pos, ENTRY * 0.8, reason, dump_exit=flag)
    assert "flagged" in t.state["dump_cooldown"]
    assert "text-only" not in t.state["dump_cooldown"]
