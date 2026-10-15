#!/usr/bin/env python3
"""Hermetic tests for the exit-path price race (_race_price, price_sol_fast).

No network: legs are fake callables with controlled delays. Validates that
the common case costs one request, the slow-Jupiter case falls through to
DexScreener inside the headstart, and total worst case is the budget - not
the ~74s the patient retry chain could burn.
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fomo_trader import _race_price


def _leg(px, delay=0.0, raise_=None, calls=None):
    def fn():
        if calls is not None:
            calls.append(1)
        if delay:
            time.sleep(delay)
        if raise_ is not None:
            raise raise_
        return px
    return fn


def test_fast_jupiter_wins_and_ds_never_fires():
    ds_calls = []
    t0 = time.monotonic()
    px = _race_price(_leg(0.001, delay=0.05),
                     _leg(0.002, calls=ds_calls),
                     budget=4.0, headstart=0.8)
    dt = time.monotonic() - t0
    assert px == 0.001
    assert ds_calls == []          # headstart covered it: zero added load
    assert dt < 0.8


def test_slow_jupiter_falls_through_to_ds():
    t0 = time.monotonic()
    px = _race_price(_leg(0.001, delay=2.0),   # Jupiter dying (dumps)
                     _leg(0.002, delay=0.1),  # DexScreener answers
                     budget=4.0, headstart=0.8)
    dt = time.monotonic() - t0
    assert px == 0.002
    assert dt < 1.5  # ~headstart + ds delay, not Jupiter's 2s


def test_both_fail_returns_none_within_budget():
    t0 = time.monotonic()
    px = _race_price(_leg(None, raise_=RuntimeError("jup down")),
                     _leg(None, raise_=RuntimeError("ds down")),
                     budget=0.5, headstart=0.2)
    dt = time.monotonic() - t0
    assert px is None
    assert dt < 1.0  # bounded by budget, fail-fast to blind handling


def test_invalid_jupiter_price_ignored():
    # 0/None/NaN/inf must not win the race or poison the tick
    for bad in (0, 0.0, None, float("nan"), float("inf")):
        px = _race_price(_leg(bad, delay=0.05),
                         _leg(0.007, delay=0.3),
                         budget=4.0, headstart=0.8)
        assert px == 0.007, bad


def test_jupiter_invalid_then_ds_also_invalid():
    px = _race_price(_leg(0.0), _leg(None), budget=0.5, headstart=0.2)
    assert px is None


def test_first_valid_wins_when_both_answer():
    # DS answers first overall only if Jupiter is past its headstart
    px = _race_price(_leg(0.001, delay=1.0),
                     _leg(0.002, delay=0.05),
                     budget=4.0, headstart=0.3)
    assert px == 0.002
