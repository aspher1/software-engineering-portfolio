"""BSC client reconnects with backoff instead of latching off forever.

A failed BscSwap construction used to set a permanent _bsc_down flag,
disabling all BSC entries until the next restart. Now bscswap() retries
with exponential backoff (60s, doubling, 30min cap) and logs at most one
line per retry window. Hermetic: BscSwap and the clock are faked.
"""
import sys
import types
from unittest.mock import patch

import pytest

import fomo_trader
from fomo_trader import Trader


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def time(self):
        return self.now

    def advance(self, s):
        self.now += s


def make_trader(monkeypatch, clock):
    t = object.__new__(Trader)
    t.cfg = {"wallets": {}, "bsc_rpc_urls": ["http://fake-rpc"]}
    t._bscswap = None
    t._bsc_retry_at = 0.0
    t._bsc_backoff_s = 60.0
    t._bsc_down_logged = False
    monkeypatch.setattr(fomo_trader.time, "time", clock.time)
    return t


def test_first_failure_schedules_retry_and_logs_once(tmp_path, monkeypatch):
    clock = FakeClock()
    t = make_trader(monkeypatch, clock)
    lines = []
    monkeypatch.setattr(fomo_trader, "log", lines.append)
    monkeypatch.setitem(sys.modules, "bsc_swap",
                        types.SimpleNamespace(BscSwap=_boom()))
    assert t.bscswap() is None
    assert t._bscswap is None
    assert t._bsc_retry_at == pytest.approx(clock.now + 60.0)
    assert t._bsc_backoff_s == pytest.approx(120.0)
    assert len([l for l in lines if "BSC unavailable" in l]) == 1


def test_no_reattempt_before_retry_at(tmp_path, monkeypatch):
    clock = FakeClock()
    t = make_trader(monkeypatch, clock)
    lines = []
    monkeypatch.setattr(fomo_trader, "log", lines.append)
    attempts = {"n": 0}

    def BscSwap(urls, key_file=None):
        attempts["n"] += 1
        raise ConnectionError("down")

    monkeypatch.setitem(sys.modules, "bsc_swap",
                        types.SimpleNamespace(BscSwap=BscSwap))
    assert t.bscswap() is None
    clock.advance(30)
    assert t.bscswap() is None
    assert attempts["n"] == 1  # no second construction attempt
    assert len([l for l in lines if "BSC unavailable" in l]) == 1


def test_retry_after_window_succeeds_and_resets_backoff(tmp_path, monkeypatch):
    clock = FakeClock()
    t = make_trader(monkeypatch, clock)
    monkeypatch.setattr(fomo_trader, "log", lambda *a: None)
    state = {"fail": True}

    class FakeSwap:
        pass

    def BscSwap(urls, key_file=None):
        if state["fail"]:
            raise ConnectionError("down")
        return FakeSwap()

    monkeypatch.setitem(sys.modules, "bsc_swap",
                        types.SimpleNamespace(BscSwap=BscSwap))
    assert t.bscswap() is None
    assert t._bsc_backoff_s == pytest.approx(120.0)
    state["fail"] = False
    clock.advance(61)
    client = t.bscswap()
    assert isinstance(client, FakeSwap)
    assert t._bscswap is client
    assert t._bsc_backoff_s == pytest.approx(60.0)  # reset on success
    # cached: no further construction attempts
    clock.advance(3600)
    assert t.bscswap() is client


def test_backoff_doubles_and_caps(tmp_path, monkeypatch):
    clock = FakeClock()
    t = make_trader(monkeypatch, clock)
    monkeypatch.setattr(fomo_trader, "log", lambda *a: None)
    monkeypatch.setitem(sys.modules, "bsc_swap",
                        types.SimpleNamespace(BscSwap=_boom()))
    expected = [60.0, 120.0, 240.0, 480.0, 960.0, 1800.0, 1800.0]
    for wait in expected:
        assert t.bscswap() is None
        assert t._bsc_retry_at == pytest.approx(clock.now + wait)
        clock.advance(wait + 1)


def test_log_rate_limited_to_one_per_window(tmp_path, monkeypatch):
    clock = FakeClock()
    t = make_trader(monkeypatch, clock)
    lines = []
    monkeypatch.setattr(fomo_trader, "log", lines.append)
    monkeypatch.setitem(sys.modules, "bsc_swap",
                        types.SimpleNamespace(BscSwap=_boom()))
    t.bscswap()  # failure #1, window 60s
    clock.advance(61)
    t.bscswap()  # failure #2, window 120s -> one more line allowed
    clock.advance(10)
    t.bscswap()  # still inside window 2: no attempt, no log
    unavailable = [l for l in lines if "BSC unavailable" in l]
    assert len(unavailable) == 2


def _boom():
    def BscSwap(urls, key_file=None):
        raise ConnectionError("down")
    return BscSwap
