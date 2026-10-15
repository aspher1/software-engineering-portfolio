"""Sweep-cache tests (2026-10-06 cadence fix).

The full end-of-day Kalshi sweep is served from data/sweep_cache.json and
refreshed by a detached background worker; the 9 crypto series are ALWAYS
fetched fresh. These tests pin: cold start, TTL expiry, sweep-failure
fallback, crypto-coverage freshness, and the worker's locking/atomicity.
"""
import fcntl
import json
from unittest.mock import Mock

import pytest
import requests
import same_day_loop as sdl


def _paths(monkeypatch, tmp_path):
    monkeypatch.setattr(sdl, "SWEEP_CACHE_PATH", str(tmp_path / "sweep_cache.json"))
    monkeypatch.setattr(sdl, "SWEEP_REFRESH_LOCK_PATH", str(tmp_path / "sweep_refresh.lock"))
    monkeypatch.setattr(sdl, "SWEEP_REFRESH_STATE_PATH", str(tmp_path / "sweep_refresh_state.json"))
    monkeypatch.setattr(sdl, "DATA", str(tmp_path))


def _series_get(monkeypatch, px=0.5):
    """Mock kalshi_get serving one market per crypto series."""
    def get(url, params, **kwargs):
        assert "series_ticker" in params
        return Mock(json=lambda: {"markets": [{
            "ticker": params["series_ticker"] + "-T",
            "close_time": "2026-10-06T18:00:00Z",
            "yes_bid_dollars": px - 0.01, "yes_ask_dollars": px + 0.01,
            "title": "fresh"}]})
    monkeypatch.setattr(sdl, "kalshi_get", get)


def test_cold_cache_no_file(monkeypatch, tmp_path):
    _paths(monkeypatch, tmp_path)
    assert sdl._load_sweep_cache("2026-10-06") == (None, None)


def test_corrupt_cache_treated_as_cold(monkeypatch, tmp_path):
    _paths(monkeypatch, tmp_path)
    (tmp_path / "sweep_cache.json").write_text("{not json")
    assert sdl._load_sweep_cache("2026-10-06") == (None, None)


def test_wrong_day_cache_treated_as_cold(monkeypatch, tmp_path):
    _paths(monkeypatch, tmp_path)
    sdl._save_sweep_cache("2026-10-05", [{"ticker": "OLD"}])
    assert sdl._load_sweep_cache("2026-10-06") == (None, None)


def test_save_load_roundtrip_trims_fields(monkeypatch, tmp_path):
    _paths(monkeypatch, tmp_path)
    bulky = {"ticker": "T1", "close_time": "2026-10-06T18:00:00Z",
             "title": "t", "yes_bid_dollars": 0.4, "yes_ask_dollars": 0.45,
             "strike_type": "x" * 5000, "extra_blob": list(range(500))}
    sdl._save_sweep_cache("2026-10-06", [bulky])
    markets, age = sdl._load_sweep_cache("2026-10-06")
    assert len(markets) == 1
    assert set(markets[0]) <= set(sdl.SWEEP_CACHE_FIELDS)
    assert "ticker" in markets[0] and "title" in markets[0]
    assert "strike_type" not in markets[0] and "extra_blob" not in markets[0]
    assert not (tmp_path / "sweep_cache.json.tmp").exists()  # atomic rename
    assert 0 <= age < 5


def test_stale_cache_served_while_refresh_spawned(monkeypatch, tmp_path):
    _series_get(monkeypatch)
    stale = [{"ticker": "STALE-1", "close_time": "2026-10-06T18:00:00Z"}]
    monkeypatch.setattr(sdl, "_load_sweep_cache",
                        lambda today: (stale, sdl.SWEEP_CACHE_TTL_S + 1))
    spawned = Mock(return_value=True)
    monkeypatch.setattr(sdl, "_spawn_sweep_refresh", spawned)
    rows = sdl.kalshi_same_day("2026-10-06")
    assert spawned.call_count == 1
    assert any(t == "STALE-1" for t in (m["ticker"] for m in rows))
    # Stale-but-served is a diagnostic note, not a fetch failure.
    assert any("sweep_cache=stale" in n for n in rows.cache_notes)
    assert not any("sweep_cache=stale" in f for f in rows.failures)
    # Crypto coverage is still fresh even when the sweep is stale.
    assert sum("15M" in m["ticker"] for m in rows) == 9


def test_ttl_boundary(monkeypatch, tmp_path):
    _series_get(monkeypatch)
    cached = [{"ticker": "C1", "close_time": "2026-10-06T18:00:00Z"}]
    monkeypatch.setattr(sdl, "_spawn_sweep_refresh", lambda today: True)
    monkeypatch.setattr(sdl, "_load_sweep_cache",
                        lambda today: (cached, sdl.SWEEP_CACHE_TTL_S - 1))
    rows = sdl.kalshi_same_day("2026-10-06")
    assert rows.cache_notes == [
        f"sweep_cache=hit age_s={sdl.SWEEP_CACHE_TTL_S - 1} markets=1"]
    assert not rows.failures
    monkeypatch.setattr(sdl, "_load_sweep_cache",
                        lambda today: (cached, sdl.SWEEP_CACHE_TTL_S + 1))
    rows = sdl.kalshi_same_day("2026-10-06")
    assert any("sweep_cache=stale" in n for n in rows.cache_notes)
    assert not any("sweep_cache=stale" in f for f in rows.failures)


def test_series_wins_ticker_tie_over_cached_sweep(monkeypatch, tmp_path):
    # A stale cached copy of a 15M market must not shadow the fresh series row.
    def get(url, params, **kwargs):
        return Mock(json=lambda: {"markets": [{
            "ticker": "KXBTC15M-26OCT061200", "close_time": "2026-10-06T16:15:00Z",
            "yes_bid_dollars": 0.89, "yes_ask_dollars": 0.91, "title": "fresh"}]})
    monkeypatch.setattr(sdl, "kalshi_get", get)
    cached = [{"ticker": "KXBTC15M-26OCT061200",
               "close_time": "2026-10-06T16:15:00Z",
               "yes_bid_dollars": 0.10, "yes_ask_dollars": 0.12,
               "title": "stale"}]
    monkeypatch.setattr(sdl, "_load_sweep_cache", lambda today: (cached, 10.0))
    rows = sdl.kalshi_same_day("2026-10-06")
    winners = [m for m in rows if m["ticker"] == "KXBTC15M-26OCT061200"]
    assert len(winners) == 1
    assert winners[0]["title"] == "fresh"
    assert winners[0]["yes_bid_dollars"] == 0.89


def _sweep_markets(n=3):
    rows = sdl.KalshiMarkets()
    for i in range(n):
        rows.append({"ticker": f"SWEEP-{i}",
                     "close_time": "2026-10-06T18:00:00Z",
                     "title": "sweep market", "yes_bid_dollars": 0.4,
                     "yes_ask_dollars": 0.45, "bulky": "y" * 1000})
    return rows


def test_refresh_worker_writes_cache(monkeypatch, tmp_path, capsys):
    _paths(monkeypatch, tmp_path)
    monkeypatch.setattr(sdl, "_kalshi_sweep_fresh",
                        lambda today, polite_sleep=0.0: _sweep_markets())
    sdl._refresh_sweep_cache("2026-10-06")
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["markets"] == 3
    markets, age = sdl._load_sweep_cache("2026-10-06")
    assert len(markets) == 3
    assert all(set(m) <= set(sdl.SWEEP_CACHE_FIELDS) for m in markets)


def test_refresh_worker_failure_preserves_old_cache(monkeypatch, tmp_path, capsys):
    _paths(monkeypatch, tmp_path)
    sdl._save_sweep_cache("2026-10-06", [{"ticker": "GOOD"}])
    before = (tmp_path / "sweep_cache.json").read_text()
    bad = sdl.KalshiMarkets()
    bad.failures.append("sweep page 1: boom")
    monkeypatch.setattr(sdl, "_kalshi_sweep_fresh",
                        lambda today, polite_sleep=0.0: bad)
    sdl._refresh_sweep_cache("2026-10-06")
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert (tmp_path / "sweep_cache.json").read_text() == before


def test_refresh_worker_lock_prevents_overlap(monkeypatch, tmp_path, capsys):
    _paths(monkeypatch, tmp_path)
    sdl._save_sweep_cache("2026-10-06", [{"ticker": "GOOD"}])
    before = (tmp_path / "sweep_cache.json").read_text()
    lock = open(tmp_path / "sweep_refresh.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        def fail_if_called(today, polite_sleep=0.0):
            raise AssertionError("must not sweep while locked")
        monkeypatch.setattr(sdl, "_kalshi_sweep_fresh", fail_if_called)
        sdl._refresh_sweep_cache("2026-10-06")
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    out = json.loads(capsys.readouterr().out)
    assert out["skipped"] == "refresh already running"
    assert (tmp_path / "sweep_cache.json").read_text() == before


def test_spawn_cooldown(monkeypatch, tmp_path):
    _paths(monkeypatch, tmp_path)
    popen = Mock()
    monkeypatch.setattr(sdl.subprocess, "Popen", popen)
    assert sdl._spawn_sweep_refresh("2026-10-06") is True
    assert popen.call_count == 1
    # Second attempt inside the cooldown: no new process.
    assert sdl._spawn_sweep_refresh("2026-10-06") is False
    assert popen.call_count == 1
    args = popen.call_args[0][0]
    assert "--refresh-sweep-cache" in args and "2026-10-06" in args
    assert popen.call_args[1]["start_new_session"] is True


def test_sweep_fetch_still_pages_and_caps(monkeypatch):
    # The background worker's sweep keeps the original 61-page cap behavior.
    calls = []
    def get(url, params, **kwargs):
        calls.append(dict(params))
        return Mock(json=lambda: {"markets": [{
            "ticker": f"P{len(calls)}", "close_time": "2026-10-06T18:00:00Z"}],
            "cursor": "more"})
    monkeypatch.setattr(sdl, "kalshi_get", get)
    rows = sdl._kalshi_sweep_fresh("2026-10-06")
    assert len(calls) == 61
    assert len(rows) == 61
    assert not rows.failures


def test_sweep_fetch_failure_records_page(monkeypatch):
    monkeypatch.setattr(sdl, "kalshi_get",
                        Mock(side_effect=requests.HTTPError("429")))
    rows = sdl._kalshi_sweep_fresh("2026-10-06")
    assert rows.failures == ["sweep page 1: 429"]
    assert len(rows) == 0
