"""Pure candidate checks and live-decision parity, with synthetic prices."""
import builtins
import socket
import time
from collections import deque
from unittest.mock import patch

from shadow_dump import evaluate


def test_threshold_and_window():
    assert evaluate([(70, 100), (100, 92)], 100, 92)["source"] == "quote"
    assert evaluate([(69.9, 100), (100, 92)], 100, 92) is None
    assert evaluate([(70, 100), (100, 92.01)], 100, 92.01) is None
    assert evaluate([(100, 100)], 100, 100, venue_m5=-20)["source"] == "venue_m5"
    assert evaluate([(100, 100)], 100, 100, venue_m5=-19.9) is None


def test_slow_bleed_and_malformed_never_raise():
    assert evaluate([(0, 100), (30, 95), (60, 90)], 60, 90) is None
    weird = [None, (), ("x", None), (float("nan"), 100),
             (100, float("inf")), {}, 42]
    for history in (None, [], weird, deque(weird), ["garbage"]):
        for price in (None, "x", -1, float("nan"), 1):
            evaluate(history, 100, price, venue_m5="bad")


def test_pure_and_fast():
    history = deque(((i, 100 - i / 100) for i in range(300)), maxlen=300)
    with patch.object(builtins, "open", side_effect=AssertionError("file")), \
         patch.object(socket, "socket", side_effect=AssertionError("network")):
        start = time.perf_counter()
        for _ in range(100):
            evaluate(history, 299, 90)
        assert (time.perf_counter() - start) / 100 < .05


def test_shadow_does_not_change_live_exit(monkeypatch):
    from tests.test_manage_cycle import (ENTRY, PriceFeed, add_position,
                                         make_trader, manage_params, wire)
    monkeypatch.setattr("fomo_trader.time.sleep", lambda _: None)
    decisions = []
    for enabled in (False, True):
        trader, _ = make_trader()
        trader.cfg["exit"]["shadow_dump_detector"] = enabled
        mint = "shadow-parity-mint"
        add_position(trader, mint)
        feed = PriceFeed(ENTRY * .85)
        wire(trader, feed)
        trader._px_hist[mint] = deque([(time.time(), ENTRY)], maxlen=300)
        trader._manage_once(mint, *manage_params(trader))
        decisions.append([swap[0] for swap in feed.swaps])
    assert decisions[0] == decisions[1]
    assert decisions[0]


def test_live_mode_cannot_arm_shadow(monkeypatch):
    from tests.test_manage_cycle import (ENTRY, PriceFeed, add_position,
                                         make_trader, manage_params, wire)
    monkeypatch.setattr("fomo_trader.time.sleep", lambda _: None)
    trader, _ = make_trader()
    trader.dry_run = False
    trader.cfg["exit"]["shadow_dump_detector"] = True
    mint = "shadow-live-gate"
    add_position(trader, mint)
    wire(trader, PriceFeed(ENTRY))
    trader._manage_once(mint, *manage_params(trader))
    assert "shadow_dump_first" not in trader.state["positions"][mint]


def test_shadow_records_without_selling():
    from tests.test_manage_cycle import (ENTRY, PriceFeed, add_position,
                                         make_trader, manage_params, wire)
    trader, _ = make_trader()
    mint = "shadow-only-signal"
    add_position(trader, mint)
    feed = PriceFeed(ENTRY * .91)
    wire(trader, feed)
    trader._px_hist[mint] = deque([(time.time(), ENTRY)], maxlen=300)
    assert trader._manage_once(mint, *manage_params(trader)) is False
    signal = trader.state["positions"][mint]["shadow_dump_first"]
    assert signal["source"] == "quote"
    assert signal["price"] == feed.px
    assert signal["window_sec"] == 30
    assert feed.swaps == []
