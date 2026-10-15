"""Grading from Kalshi's own published settlement (`result` field) ONLY.

Covers: result mapping, Kalshi-only truth (no Kraken proxy fallback ever),
indefinite deferral when settlement isn't published yet (at any window age),
exact-once grading, one settlement fetch per ticker, and accuracy computed
over Kalshi-settled rows only (legacy Kraken-proxy rows excluded).
No network (all I/O monkeypatched).
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest

import same_day_loop as sdl
from same_day_loop import kalshi_settlement, grade_pending, accuracy_stats


def _closed_window(now, closed_ago_s=600, win_s=900):
    wc = now - closed_ago_s
    return wc - win_s, wc


def _entry(ticker, side, wo, wc, edge=0.10, mid=0.40):
    return {f"{ticker}:{side}": {
        "ticker": ticker, "side": side, "edge": edge, "market_mid": mid,
        "window_open_utc": wo, "window_close_utc": wc}}


def _no_kraken(monkeypatch):
    """Guard: grading must never touch Kraken."""
    def _boom(*a, **k):
        raise AssertionError("grading must not touch Kraken")
    monkeypatch.setattr(sdl, "kraken_candles", _boom)


# ---------------- kalshi_settlement mapping ----------------

@pytest.mark.parametrize("result,expected", [
    ("yes", "YES"), ("no", "NO"), ("YES", "YES"), ("No", "NO"),
    ("  yes  ", "YES"),
    (None, None), ("", None), ("void", None), ("pending", None),
])
def test_settlement_mapping(monkeypatch, result, expected):
    monkeypatch.setattr(sdl, "kalshi_market", lambda ticker: {"result": result})
    assert kalshi_settlement("KXBTC15M-26SEP301700-00") == expected


def test_settlement_fetch_error_is_none(monkeypatch):
    def _boom(ticker):
        raise RuntimeError("429")
    monkeypatch.setattr(sdl, "kalshi_market", _boom)
    assert kalshi_settlement("KXBTC15M-26SEP301700-00") is None


def test_settlement_empty_market_is_none(monkeypatch):
    monkeypatch.setattr(sdl, "kalshi_market", lambda ticker: {})
    assert kalshi_settlement("KXBTC15M-26SEP301700-00") is None


# ---------------- grade_pending: Kalshi-only truth ----------------

def test_kalshi_settlement_grades_row(tmp_path, monkeypatch):
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    _no_kraken(monkeypatch)
    now = int(time.time())
    wo, wc = _closed_window(now)
    ticker = "KXBTC15M-26SEP301700-00"
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda t: "NO")
    alerted = _entry(ticker, "YES", wo, wc)

    new, _ = grade_pending(alerted, now, mark=True)
    assert len(new) == 1
    row = new[0]
    assert row["actual"] == "NO"
    assert row["correct"] is False
    assert row["proxy"] == "kalshi"
    assert row["p_open"] is None and row["p_close"] is None


def test_kalshi_win_marks_correct(tmp_path, monkeypatch):
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    _no_kraken(monkeypatch)
    now = int(time.time())
    wo, wc = _closed_window(now)
    ticker = "KXBTC15M-26SEP301700-00"
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda t: "YES")
    alerted = _entry(ticker, "YES", wo, wc)

    new, _ = grade_pending(alerted, now, mark=True)
    assert len(new) == 1 and new[0]["correct"] is True
    assert new[0]["proxy"] == "kalshi"


@pytest.mark.parametrize("closed_ago_s", [600, 2000, 12 * 3600, 72 * 3600])
def test_unsettled_window_defers_indefinitely(tmp_path, monkeypatch,
                                             closed_ago_s):
    # Settlement unpublished -> NOTHING written, at any window age; the
    # window is retried on later runs forever (no proxy fallback, no cutoff).
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    _no_kraken(monkeypatch)
    now = int(time.time())
    wo, wc = _closed_window(now, closed_ago_s=closed_ago_s)
    ticker = "KXBTC15M-26SEP301700-00"
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda t: None)
    alerted = _entry(ticker, "YES", wo, wc)

    new, _ = grade_pending(alerted, now, mark=True)
    assert new == []
    assert not gp.exists()  # nothing written; retried next run


def test_deferred_window_grades_once_settled(tmp_path, monkeypatch):
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    _no_kraken(monkeypatch)
    now = int(time.time())
    wo, wc = _closed_window(now)
    ticker = "KXBTC15M-26SEP301700-00"
    alerted = _entry(ticker, "YES", wo, wc)

    monkeypatch.setattr(sdl, "kalshi_settlement", lambda t: None)
    assert grade_pending(alerted, now, mark=True)[0] == []

    monkeypatch.setattr(sdl, "kalshi_settlement", lambda t: "YES")
    new, _ = grade_pending(alerted, now, mark=True)
    assert len(new) == 1 and new[0]["proxy"] == "kalshi"

    # idempotent: already graded, no duplicate
    assert grade_pending(alerted, now, mark=True)[0] == []
    lines = [l for l in gp.read_text().splitlines() if l.strip()]
    assert len(lines) == 1


def test_one_settlement_fetch_per_ticker(tmp_path, monkeypatch):
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    _no_kraken(monkeypatch)
    now = int(time.time())
    wo, wc = _closed_window(now)
    ticker = "KXBTC15M-26SEP301700-00"
    calls = []
    monkeypatch.setattr(sdl, "kalshi_settlement",
                        lambda t: calls.append(t) or "YES")
    alerted = {}
    alerted.update(_entry(ticker, "YES", wo, wc))
    alerted.update(_entry(ticker, "NO", wo, wc))

    new, _ = grade_pending(alerted, now, mark=True)
    assert len(new) == 2
    assert calls == [ticker]  # fetched once, shared by both sides


def test_grade_row_has_no_kraken_prices(tmp_path, monkeypatch):
    # p_open/p_close are always None: no proxy is ever computed.
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    _no_kraken(monkeypatch)
    now = int(time.time())
    wo, wc = _closed_window(now)
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda t: "YES")
    new, _ = grade_pending(_entry("KXBTC15M-26SEP301700-00", "YES", wo, wc),
                           now, mark=True)
    assert new[0]["p_open"] is None and new[0]["p_close"] is None


# ---------------- accuracy: Kalshi-settled rows only ----------------

def _grade_row(proxy, correct=True):
    return {"key": f"k-{proxy}-{correct}", "proxy": proxy, "correct": correct,
            "side": "YES", "market_mid": 0.4}


def test_accuracy_note_all_kalshi():
    acc = accuracy_stats([_grade_row("kalshi") for _ in range(3)])
    assert "Kalshi's published result" in acc["note"]
    assert "legacy Kraken-proxy rows excluded" in acc["note"]


def test_accuracy_excludes_kraken_proxy_rows():
    rows = ([_grade_row("kalshi", True) for _ in range(3)]
            + [_grade_row("kalshi", False)]
            + [_grade_row("kraken", True) for _ in range(10)]
            + [_grade_row("kraken", False) for _ in range(10)])
    acc = accuracy_stats(rows)
    assert acc["graded"] == 4  # only the 4 Kalshi-settled rows count
    assert acc["wins"] == 3
    assert acc["win_rate"] == pytest.approx(0.75)


def test_accuracy_excludes_flagged_rows():
    rows = [_grade_row("kalshi", True),
            {"key": "k-flag", "proxy": "kalshi", "correct": False,
             "excluded_from_accuracy": True, "side": "YES", "market_mid": 0.4}]
    acc = accuracy_stats(rows)
    assert acc["graded"] == 1 and acc["wins"] == 1


def test_accuracy_note_empty():
    acc = accuracy_stats([])
    assert acc["win_rate"] is None
