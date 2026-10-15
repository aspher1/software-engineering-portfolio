"""Hermetic Z4 capture tests; no bot process or network access."""
import json
import statistics
import sys
import threading
import time
from collections import deque
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import fomo_trader as ft
import test_z3_obs as z3

ON = {"research": {"enabled": True, "capture_quote_path": True,
                   "capture_failed_quotes": True}}
OFF = [z3.ABSENT, None, {"enabled": False,
                         "capture_quote_path": True,
                         "capture_failed_quotes": True},
       {"enabled": "yes", "capture_quote_path": True},
       {"enabled": True, "capture_quote_path": "yes",
        "capture_failed_quotes": True}]


@pytest.fixture(autouse=True)
def reset_capture():
    ft._research_warned.clear()
    ft._quote_path_counts.clear()
    yield
    ft._research_warned.clear()
    ft._quote_path_counts.clear()


@pytest.mark.parametrize("chain", ["solana", "bsc"])
@pytest.mark.parametrize("research", OFF)
def test_off_state_golden_bytes(tmp_path, chain, research):
    entry, pos = z3.run_entry(tmp_path / "entry", chain, research)
    assert entry == z3.GOLDEN_ENTRY[chain]
    assert json.dumps(pos) == json.dumps(z3.golden_position(chain))
    close, _ = z3.run_close(tmp_path / "close", chain, research)
    assert close == z3.GOLDEN_CLOSE[chain]
    assert not list(tmp_path.rglob("failed_quotes.jsonl"))
    assert not list(tmp_path.rglob("quote_paths"))


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("chain,native", [("solana", 100.0), ("bsc", 800.0)])
def test_manage_tick_cadence_price_and_liquidity(tmp_path, chain, native):
    t = ft.Trader.__new__(ft.Trader)
    t.cfg = {"research": ON["research"], "exit": {"stale_exit_min": 0},
             "risk": {}}
    t.state_path = str(tmp_path / "state.json")
    t.state = {"positions": {"MINT": {"name": "TEST", "chain": chain,
                                       "entry": 1.0, "peak": 1.0,
                                       "rungs_fired": [], "opened_at": time.time()}},
               "day": ft._bot_today_str(), "trades_today": [],
               "trades_this_hour": [], "realized_sol": 0.0}
    t.lock = threading.RLock()
    t.save = lambda: None
    t._px_hist = {}
    t._kill_switch_tripped = lambda: False
    t.price_native_fast = lambda mint, c: 1.0
    t.native_usd = lambda c: native
    setattr(t, "_bnb_usd" if chain == "bsc" else "_sol_usd", native)
    setattr(t, "_bnb_usd_ts" if chain == "bsc" else "_sol_usd_ts", time.time())
    t.venue_m5_dump = lambda *a: None
    for _ in range(3):
        assert t._manage_once("MINT", [], 50, 50) is False
    path = tmp_path / "quote_paths" / "MINT.jsonl"
    assert len(rows(path)) == 3
    assert all(r["price_usd"] == native and r["liquidity_usd"] is None
               and r["chain"] == chain and r["mint"] == "MINT" and r["ts"]
               for r in rows(path))


def test_manage_without_cached_fx_does_not_fetch(tmp_path):
    t = ft.Trader.__new__(ft.Trader)
    t.cfg = {"research": ON["research"], "exit": {}, "risk": {}}
    t.state_path = str(tmp_path / "state.json")
    t.state = {"positions": {"M": {"name": "M", "entry": 1, "peak": 1,
                                    "rungs_fired": []}},
               "day": ft._bot_today_str(), "trades_today": [],
               "trades_this_hour": [], "realized_sol": 0}
    t.lock = threading.RLock()
    t.save = lambda: None
    t._px_hist = {}
    t._kill_switch_tripped = lambda: False
    t.price_native_fast = lambda *a: 1
    t.native_usd = lambda *a: (_ for _ in ()).throw(AssertionError("new network read"))
    t.venue_m5_dump = lambda *a: None
    assert t._manage_once("M", [], 50, 50) is False
    assert rows(tmp_path / "quote_paths" / "M.jsonl")[0]["price_usd"] is None


def test_caps_and_sanitized_filename(tmp_path, monkeypatch):
    cfg = ON
    path = tmp_path / "quote_paths" / "bad_mint.jsonl"
    monkeypatch.setattr(ft, "QUOTE_PATH_MAX_TICKS", 2)
    for _ in range(4):
        ft.maybe_capture_quote_tick(cfg, tmp_path / "state.json",
                                    "bad/mint", "solana", 1.0, None)
    assert len(rows(path)) == 2
    assert len([k for k in ft._research_warned if k[0] == "quote_cap"]) == 1
    ft._quote_path_counts.clear()
    path.unlink()
    monkeypatch.setattr(ft, "QUOTE_PATH_MAX_TICKS", 5000)
    monkeypatch.setattr(ft, "QUOTE_PATH_MAX_BYTES", 150)
    for _ in range(4):
        ft.maybe_capture_quote_tick(cfg, tmp_path / "state.json",
                                    "bad/mint", "solana", 1.0, None)
    assert path.stat().st_size <= 150
    assert len(rows(path)) == 1


def test_malformed_one_warning_and_unwritable_silent(tmp_path, monkeypatch):
    cfg = {"research": {"enabled": True, "capture_quote_path": "yes",
                        "capture_failed_quotes": True}}
    logs = []
    monkeypatch.setattr(ft, "log", logs.append)
    for _ in range(3):
        assert ft.capture_settings(cfg) == ft.CAPTURE_OFF
        ft.maybe_capture_quote_tick(cfg, tmp_path / "state.json", "M", "solana", 1, None)
    assert len(logs) == 1
    monkeypatch.setattr(ft.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    for _ in range(3):
        ft.maybe_capture_quote_tick(ON, tmp_path / "state.json", "M", "solana", 1, None)
        ft.maybe_capture_failed_quote(ON, tmp_path / "state.json", "M", "solana", "dust_quote", {})
    assert len(logs) == 3


def test_write_failures_do_not_change_commit_decision(tmp_path, monkeypatch):
    t = z3.entry_trader(tmp_path, "solana", ON["research"])
    t._entry_commit_ok = ft.Trader._entry_commit_ok.__get__(t)
    t.state["trades_today"] = [time.time()] * 10
    monkeypatch.setattr(ft.os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    assert t._entry_commit_ok(z3.signal("solana")) is False
    monkeypatch.setattr(ft.os, "makedirs", lambda *a, **k: None)
    monkeypatch.setattr(ft, "open",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError()),
                        raising=False)
    assert ft.maybe_capture_failed_quote(ON, t.state_path, "M", "solana",
                                         "dust_quote", {}) is None
    assert t._entry_commit_ok(z3.signal("solana")) is False
    assert t.state["positions"] == {}


@pytest.mark.parametrize("chain,site,reason", [
    ("solana", "entry", "entry_quote_failed"),
    ("solana", "dust", "dust_quote"),
    ("solana", "honeypot", "honeypot_no_sell_route"),
    ("solana", "unreachable", "honeypot_quote_unreachable"),
    ("bsc", "entry", "entry_quote_failed"),
    ("bsc", "dust", "dust_quote"),
    ("bsc", "honeypot", "bsc_honeypot"),
    ("solana", "cap", "commit_cap"),
    ("solana", "kill", "commit_kill_switch"),
])
def test_failed_quote_sites(tmp_path, chain, site, reason):
    t = z3.entry_trader(tmp_path, chain, ON["research"])
    sig = z3.signal(chain)
    if site in ("cap", "kill"):
        t._entry_commit_ok = ft.Trader._entry_commit_ok.__get__(t)
        if site == "cap":
            t.state["trades_today"] = [time.time()] * 10
        else:
            t._kill_switch_tripped = lambda: True
        assert t._entry_commit_ok(sig) is False
    elif site in ("honeypot", "unreachable") and chain == "solana":
        if site == "honeypot":
            quote = lambda *a: {"outAmount": "0"}
        else:
            def quote(*a):
                raise ValueError("unreachable")
        with patch.object(ft, "quote_with_retry", side_effect=lambda label, fn: fn()), \
             patch.object(ft, "jup_quote", side_effect=quote):
            assert t.honeypot_check("MINT", "TEST", 12345) is False
    else:
        if chain == "solana":
            quote = (ValueError("failed") if site == "entry" else
                     {"outAmount": "9"})
            with patch.object(ft, "jup_quote", side_effect=quote if isinstance(quote, Exception) else None,
                              return_value=quote if isinstance(quote, dict) else None):
                t.enter(sig)
        else:
            bsc = t.bscswap()
            if site == "honeypot":
                bsc.honeypot_check = lambda *a: (False, "trap")
            elif site == "entry":
                bsc.quote_buy = lambda *a: (_ for _ in ()).throw(ValueError("failed"))
            else:
                bsc.quote_buy = lambda *a: 9
            t.bscswap = lambda: bsc
            t.enter_bsc(sig)
    captured = rows(tmp_path / "failed_quotes.jsonl")
    assert len(captured) == 1
    row = captured[0]
    assert all(k in row for k in ("ts", "mint", "chain", "reason"))
    assert (row["mint"], row["chain"], row["reason"]) == (sig["mint"], chain, reason)
    if site == "dust":
        assert row["out_amount_raw"] == 9


def test_capture_helper_latency(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ft, "QUOTE_PATH_MAX_TICKS", 20000)
    monkeypatch.setattr(ft, "QUOTE_PATH_MAX_BYTES", 10 * 1024 * 1024)
    state = tmp_path / "state.json"
    cases = [
        ("quote_off", lambda: ft.maybe_capture_quote_tick({}, state, "M", "solana", 1, None)),
        ("failed_off", lambda: ft.maybe_capture_failed_quote({}, state, "M", "solana", "dust_quote", {})),
        ("quote_on", lambda: ft.maybe_capture_quote_tick(ON, state, "M", "solana", 1, None)),
        ("failed_on", lambda: ft.maybe_capture_failed_quote(ON, state, "M", "solana", "dust_quote", {"out_amount_raw": 9})),
    ]
    for name, fn in cases:
        samples = []
        for _ in range(10000):
            start = time.perf_counter_ns()
            fn()
            samples.append((time.perf_counter_ns() - start) / 1e6)
        samples.sort()
        p50, p99 = samples[5000], samples[9900]
        print(f"{name}: p50={p50:.6f} ms p99={p99:.6f} ms")
        assert p99 < 50
