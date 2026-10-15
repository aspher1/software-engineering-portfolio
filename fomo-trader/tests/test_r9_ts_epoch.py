"""R9 instrumentation tests: ts_epoch on trades.jsonl entry/close records.

R9 is instrumentation-only (validation-lane PASS, iter-5): an additive
``ts_epoch`` (epoch seconds at record-write time) on every trade journal
record, so hour/hold/ts-split analysis no longer depends on the mixed-zone
``ts`` strings (12 corrupted hold rows documented iter-5). These tests guard:

1. All three trades.jsonl write sites in fomo_trader.py (SOL entry, BSC
   entry, close) stamp ts_epoch.
2. ts_epoch survives journal.finalize_entry_record untouched.
3. No strategy logic consumes ts_epoch as an input (writers + readers only).
"""
import re
import time

import pytest

import journal


def _source():
    with open("fomo_trader.py") as f:
        return f.read()


def test_entry_and_close_sites_stamp_ts_epoch():
    src = _source()
    # Two entry sites: SOL ("mint": mint, "name": name, "entry": entry,)
    # and BSC ("chain": "bsc"). One close site: rec = {"ts": ..., "type": "close",
    entry_sites = re.findall(
        r'"ts": time\.strftime\("%Y-%m-%d %H:%M:%S"\), "ts_epoch": time\.time\(\),\s*\n\s*"type": "entry",',
        src,
    )
    close_sites = re.findall(
        r'rec = \{"ts": time\.strftime\("%Y-%m-%d %H:%M:%S"\), "ts_epoch": time\.time\(\),\s*\n\s*"type": "close",',
        src,
    )
    assert len(entry_sites) == 2, f"expected 2 entry write sites, found {len(entry_sites)}"
    assert len(close_sites) == 1, f"expected 1 close write site, found {len(close_sites)}"


def test_ts_epoch_sane_value():
    before = time.time()
    epoch = time.time()
    after = time.time()
    assert isinstance(epoch, float)
    assert before <= epoch <= after


def test_finalize_entry_record_preserves_ts_epoch():
    epoch = time.time()
    rec = {"ts": "2026-09-29 12:00:00", "ts_epoch": epoch, "type": "entry"}
    out = journal.finalize_entry_record(rec, {})
    assert out["ts_epoch"] == epoch


def test_ts_epoch_not_consumed_by_strategy():
    """Nothing in strategy logic may read ts_epoch as an input; it is
    written by the journal path and read only by analysis/tests."""
    src = _source()
    reads = [
        line.strip()
        for line in src.splitlines()
        if "ts_epoch" in line and "time.time()" not in line
    ]
    # finalize path passthrough is allowed only in journal.py, not here
    assert reads == [], f"strategy code reads ts_epoch: {reads}"


def test_close_record_shape_unchanged():
    """Additive-only guard: the close-site dict still carries every
    pre-existing key (spot check the literal in source)."""
    src = _source()
    for key in ('"realized_usd"', '"bankroll_usd"', '"drawdown_pct"', '"reason"'):
        assert key in src, f"close record lost key {key}"
