"""Seed-issue coverage: ticker parsing, --no-mark immutability, grade
idempotency, opposite-side same-window alerts, unsettled deferral, legacy
dedupe entries, and pnl_per_dollar_staked normalization. Paper/analysis
only; no network (all I/O monkeypatched to temp paths / stubs)."""
import json
import sys
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest

import same_day_loop as sdl
from same_day_loop import (
    parse_15m_window, grade_pending, accuracy_stats, main,
)


@pytest.fixture(autouse=True)
def _kalshi_settlement_stub(monkeypatch):
    # Grade-path tests stub Kalshi's settlement fetch so no test hits the
    # network. Default: YES. Individual tests override for the outcome they
    # need. Grading is Kalshi-settlement-only (fix 2c): there is no
    # Kraken-proxy fallback, so tests never stub prices.
    # Tests for the settlement path itself live in test_kalshi_settlement.py.
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda ticker: "YES")


# ---------------- ticker parsing ----------------

def test_parse_valid_ticker_sep_edt():
    # 13:00 ET on 2026-09-30 = 17:00 UTC (EDT)
    sym, wo, wc = parse_15m_window("KXBTC15M-26SEP301300-00")
    assert sym == "BTC"
    assert wc == int(datetime(2026, 9, 30, 17, 0, tzinfo=timezone.utc).timestamp())
    assert wc - wo == 900


def test_parse_valid_ticker_jan_est():
    # 13:00 ET on 2026-01-15 = 18:00 UTC (EST, no DST)
    sym, wo, wc = parse_15m_window("KXETH15M-26JAN151300-00")
    assert sym == "ETH"
    assert wc == int(datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc).timestamp())
    assert wc - wo == 900


def test_parse_multi_letter_sym():
    sym, wo, wc = parse_15m_window("KXHYPE15M-26SEP301900-00")
    assert sym == "HYPE"
    assert wc - wo == 900


@pytest.mark.parametrize("bad", [
    "", None, "garbage", "KXBTC15M-26XXX301300-00", "KXBTC15M-26SEP301300",
    "KXBTC99-26SEP301300-00", "KXBTC15M-26SEP321300-00",
])
def test_parse_invalid_tickers(bad):
    assert parse_15m_window(bad) is None


def test_parse_roundtrip_close_minute():
    now = int(time.time())
    et = datetime.fromtimestamp(now - 3600, timezone.utc).astimezone(
        ZoneInfo("America/New_York"))
    ticker = f"KXSOL15M-{et:%y}{et:%b}{et:%d}{et:%H}{et:%M}-00".upper()
    sym, wo, wc = parse_15m_window(ticker)
    assert sym == "SOL"
    # close must land on the same ET minute the ticker names
    got = datetime.fromtimestamp(wc, timezone.utc).astimezone(
        ZoneInfo("America/New_York"))
    assert (got.year, got.month, got.day, got.hour, got.minute) == \
        (et.year, et.month, et.day, et.hour, et.minute)


# ---------------- grading helpers ----------------

def _closed_window(now, closed_ago_s=600, win_s=900):
    wc = now - closed_ago_s
    return wc - win_s, wc


def _entry(ticker, side, wo, wc, edge=0.10, mid=0.40):
    return {f"{ticker}:{side}": {
        "ticker": ticker, "side": side, "edge": edge, "market_mid": mid,
        "window_open_utc": wo, "window_close_utc": wc}}


# ---------------- grade idempotency ----------------

def test_grade_idempotent_no_duplicates(tmp_path, monkeypatch):
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    now = int(time.time())
    wo, wc = _closed_window(now)
    ticker = "KXBTC15M-26SEP301700-00"
    alerted = _entry(ticker, "YES", wo, wc)

    new1, _ = grade_pending(alerted, now, mark=True)
    assert len(new1) == 1 and new1[0]["correct"] is True  # settlement YES
    new2, _ = grade_pending(alerted, now, mark=True)
    assert new2 == []
    lines = [l for l in gp.read_text().splitlines() if l.strip()]
    assert len(lines) == 1  # no duplicate row


def test_grade_no_mark_writes_nothing(tmp_path, monkeypatch):
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    now = int(time.time())
    wo, wc = _closed_window(now)
    alerted = _entry("KXBTC15M-26SEP301700-00", "YES", wo, wc)
    new, _ = grade_pending(alerted, now, mark=False)
    assert len(new) == 1  # computed...
    assert not gp.exists()  # ...but never written


# ---------------- opposite-side same-window alerts ----------------

def test_opposite_sides_same_window_grade_independently(tmp_path, monkeypatch):
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    now = int(time.time())
    wo, wc = _closed_window(now)
    ticker = "KXETH15M-26SEP301700-00"
    # settlement YES: YES take correct, NO take wrong
    alerted = {}
    alerted.update(_entry(ticker, "YES", wo, wc))
    alerted.update(_entry(ticker, "NO", wo, wc))
    new, _ = grade_pending(alerted, now, mark=True)
    by_side = {r["side"]: r for r in new}
    assert len(new) == 2
    assert by_side["YES"]["correct"] is True
    assert by_side["NO"]["correct"] is False


# ---------------- unsettled deferral ----------------

def test_deferred_unsettled_writes_nothing_retries_later(tmp_path, monkeypatch):
    # No published settlement -> nothing written; when it publishes, the
    # window grades normally (PUSH is impossible under Kalshi-only truth).
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    now = int(time.time())
    wo, wc = _closed_window(now)
    alerted = _entry("KXBTC15M-26SEP301700-00", "YES", wo, wc)

    monkeypatch.setattr(sdl, "kalshi_settlement", lambda ticker: None)
    new, acc = grade_pending(alerted, now, mark=True)
    assert new == []
    assert not gp.exists()
    assert acc["graded"] == 0
    assert acc["win_rate"] is None

    monkeypatch.setattr(sdl, "kalshi_settlement", lambda ticker: "YES")
    new, _ = grade_pending(alerted, now, mark=True)
    assert len(new) == 1 and new[0]["correct"] is True


# ---------------- legacy dedupe entries ----------------

def _recent_ticker(now, sym="BTC", closed_ago_s=600):
    et = datetime.fromtimestamp(now - closed_ago_s, timezone.utc).astimezone(
        ZoneInfo("America/New_York"))
    return f"KX{sym}15M-{et:%y}{et:%b}{et:%d}{et:%H}{et:%M}-00".upper()


def test_legacy_string_entry_grades_via_ticker_parse(tmp_path, monkeypatch):
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda ticker: "NO")
    now = int(time.time())
    ticker = _recent_ticker(now)
    sym, wo, wc = parse_15m_window(ticker)
    assert wo is not None and wc < now - 120  # sanity: gradeable
    alerted = {f"{ticker}:YES": "legacy-string-entry"}  # non-dict, old format
    new, _ = grade_pending(alerted, now, mark=True)
    assert len(new) == 1
    assert new[0]["actual"] == "NO" and new[0]["correct"] is False
    assert new[0]["edge"] is None and new[0]["market_mid"] is None


def test_legacy_entry_without_colon_skipped(tmp_path, monkeypatch):
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    new, _ = grade_pending({"weirdkey": "x"}, int(time.time()), mark=True)
    assert new == []


def test_legacy_entry_unparseable_ticker_skipped(tmp_path, monkeypatch):
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    new, _ = grade_pending({"KXBTC99-26SEP301300-00:YES": "x"},
                           int(time.time()), mark=True)
    assert new == []


def test_xvenue_keys_never_graded(tmp_path, monkeypatch):
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    new, _ = grade_pending({"xvenue:KXBTC15M-26SEP301700-00": {"ticker": "t"}},
                           int(time.time()), mark=True)
    assert new == []


# ---------------- --no-mark state immutability ----------------

def _live_market(now):
    return {
        "ticker": "KXBTC15M-26SEP301900-00", "title": "BTC up?",
        "open_time": datetime.fromtimestamp(now - 600,
                                           timezone.utc).isoformat(),
        "close_time": datetime.fromtimestamp(now + 300,
                                            timezone.utc).isoformat(),
        "yes_bid_dollars": "0.49", "yes_ask_dollars": "0.51",
        "volume_fp": "1000",
    }


def _patch_scan_env(tmp_path, monkeypatch, state_bytes):
    sp = tmp_path / "state.json"
    gp = tmp_path / "grades.jsonl"
    sp.write_bytes(state_bytes)
    monkeypatch.setattr(sdl, "STATE_PATH", str(sp))
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    now = int(time.time())
    monkeypatch.setattr(sdl, "kalshi_same_day",
                        lambda today: [_live_market(now)])
    monkeypatch.setattr(sdl, "poly_same_day", lambda today: [])
    # fair 0.60 vs mid 0.50 -> post-shrink edge 0.075 -> YES fires on the
    # agreement side (taken-side price 0.50 >= 0.5)
    monkeypatch.setattr(sdl, "fair_15m",
                        lambda *a: (0.60, 100.0, 100.0, 0.001, 5.0, a[3]))
    return sp, gp, now


def test_no_mark_leaves_state_and_grades_untouched(tmp_path, monkeypatch,
                                                   capsys):
    seed = json.dumps({"alerted": {}}).encode()
    sp, gp, _ = _patch_scan_env(tmp_path, monkeypatch, seed)
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py", "--no-mark"])
    main()
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert sp.read_bytes() == seed          # state file byte-identical
    assert not gp.exists()                  # no grades written
    assert out["signals_total"] >= 1        # scan still evaluated the market


def test_mark_run_does_write_state(tmp_path, monkeypatch, capsys):
    seed = json.dumps({"alerted": {}}).encode()
    sp, gp, _ = _patch_scan_env(tmp_path, monkeypatch, seed)
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py"])  # mark mode
    main()
    out = json.loads(capsys.readouterr().out)
    assert out["signals_new"] >= 1
    assert sp.read_bytes() != seed          # state gained the alerted key
    assert b"KXBTC15M-26SEP301900-00:YES" in sp.read_bytes()


# ---------------- pnl_per_dollar_staked normalization ----------------

def _g(side, mid, correct):
    return {"side": side, "market_mid": mid, "correct": correct,
            "proxy": "kalshi"}


def test_pnl_per_dollar_staked_normalized_by_total_staked():
    # YES win @0.40 -> +0.60 on $0.40 staked
    # YES loss @0.40 -> -0.40 on $0.40 staked
    # NO win @0.70  -> +0.70 on $0.30 staked
    # total profit +0.90 / total staked $1.10 = 0.8182 per dollar staked
    acc = accuracy_stats([_g("YES", 0.40, True), _g("YES", 0.40, False),
                          _g("NO", 0.70, True)])
    assert acc["pnl_per_dollar_staked"] == pytest.approx(0.8182)  # rounded 4dp


def test_pnl_per_dollar_differs_from_per_signal_average():
    # one cheap longshot win: per-signal average would be +0.90, but per
    # dollar staked it is +0.90 / $0.10 = 9.0
    acc = accuracy_stats([_g("YES", 0.10, True)])
    assert acc["pnl_per_dollar_staked"] == pytest.approx(9.0)


def test_pnl_ignores_rows_without_mid():
    acc = accuracy_stats([_g("YES", None, True), _g("YES", 0.50, True)])
    # only the 0.50 row counts: +0.50 profit / $0.50 staked = 1.0
    assert acc["pnl_per_dollar_staked"] == pytest.approx(1.0)


def test_pnl_none_when_no_priced_rows():
    acc = accuracy_stats([_g("YES", None, True)])
    assert acc["pnl_per_dollar_staked"] is None
