"""Delivery-safety and state/grade-file robustness (iter-2 audit fixes).

Covers the highest-impact non-decision findings:
- B6: a Kalshi settlement-fetch failure during grading must not swallow
      alert delivery (signals were already stamped+saved; stdout must still
      print JSON). Grading defers to a later run instead of raising.
- B8: atomic save_state + load_state restores the last good backup on a
      corrupt read instead of wiping dedupe history.
- B7: load_grades survives one bad line (and dedupes); append_grades
      repairs a missing trailing newline.
- A3: grading is Kalshi-settlement-only at any window age — there is no
      Kraken proxy and no candle-history cutoff; an unsettled window simply
      defers.
Paper/analysis only; no network (all I/O monkeypatched)."""
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import same_day_loop as sdl
from same_day_loop import main


def _paths(tmp_path, monkeypatch):
    sp = tmp_path / "state.json"
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "STATE_PATH", str(sp))
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    return sp, gp


# ---- B8: atomic save_state + backup restore --------------------------------

def test_save_state_atomic_and_load_recovers_backup(tmp_path, monkeypatch):
    sp, _ = _paths(tmp_path, monkeypatch)
    sdl.save_state({"alerted": {"a": 1}})
    sdl.save_state({"alerted": {"a": 1, "b": 2}})
    # no stray temp file, state parses
    assert not os.path.exists(str(sp) + ".tmp")
    assert json.loads(sp.read_text()) == {"alerted": {"a": 1, "b": 2}}
    # corrupt the live state file -> load_state must restore the backup
    sp.write_text("not json{")
    assert sdl.load_state() == {"alerted": {"a": 1}}


def test_load_state_missing_file_is_empty(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    assert sdl.load_state() == {"alerted": {}}


# ---- B7: grade-file robustness ----------------------------------------------

def test_load_grades_survives_bad_line_and_dedupes(tmp_path, monkeypatch):
    _, gp = _paths(tmp_path, monkeypatch)
    gp.write_text(
        '{"key": "k1", "correct": true}\n'
        'this is not json\n'
        '{"key": "k2", "correct": false}\n'
        '{"key": "k1", "correct": true}\n'
    )
    grades = sdl.load_grades()
    assert [g["key"] for g in grades] == ["k1", "k2"]


def test_append_grades_repairs_missing_trailing_newline(tmp_path, monkeypatch):
    _, gp = _paths(tmp_path, monkeypatch)
    gp.write_text('{"key": "x"}')  # no trailing newline (torn write)
    sdl.append_grades([{"key": "y"}])
    lines = [ln for ln in gp.read_text().splitlines() if ln.strip()]
    assert [json.loads(ln)["key"] for ln in lines] == ["x", "y"]


# ---- B6: grading failure must not swallow delivery ---------------------------

def test_settlement_fetch_failure_defers_not_raises(tmp_path, monkeypatch):
    # Kalshi's settlement fetch fails (429 etc.) -> kalshi_settlement is
    # None -> the window defers to a later run; grading never raises and
    # never touches Kraken.
    _, gp = _paths(tmp_path, monkeypatch)
    def _boom(ticker):
        raise Exception("429 rate limited")
    monkeypatch.setattr(sdl, "kalshi_market", _boom)
    def _no_kraken(*a, **k):
        raise AssertionError("grading must not touch Kraken")
    monkeypatch.setattr(sdl, "kraken_candles", _no_kraken)
    now = int(time.time())
    wc = now - 4000  # past GRADE_DELAY_S; settlement just not published
    alerted = {"KXBTC15M-26SEP301700-00:NO": {
        "ts": datetime.fromtimestamp(now - 5000, timezone.utc).isoformat(),
        "ticker": "KXBTC15M-26SEP301700-00", "side": "NO",
        "edge": -0.10, "market_mid": 0.60,
        "window_open_utc": wc - 900, "window_close_utc": wc,
    }}
    new, _acc = sdl.grade_pending(alerted, now, mark=True)  # must not raise
    assert new == []
    assert not gp.exists() or gp.read_text() == ""  # nothing appended; retry next run


def test_old_window_graded_from_settlement_at_any_age(tmp_path, monkeypatch):
    # A3: Kalshi truth is fetchable at any age; old windows grade normally,
    # and grading never consults Kraken.
    _, gp = _paths(tmp_path, monkeypatch)
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda ticker: "YES")
    def _boom(*a, **k):
        raise AssertionError("Kraken must not be consulted on the settle path")
    monkeypatch.setattr(sdl, "kraken_candles", _boom)
    now = int(time.time())
    wc = now - 12 * 3600  # far beyond any candle history
    alerted = {"KXBTC15M-26SEP301400-00:YES": {
        "ts": datetime.fromtimestamp(now - 13 * 3600, timezone.utc).isoformat(),
        "ticker": "KXBTC15M-26SEP301400-00", "side": "YES",
        "edge": 0.12, "market_mid": 0.55,
        "window_open_utc": wc - 900, "window_close_utc": wc,
    }}
    new, _acc = sdl.grade_pending(alerted, now, mark=True)
    assert len(new) == 1
    assert new[0]["proxy"] == "kalshi"
    assert new[0]["correct"] is True


def test_grading_failure_in_main_still_delivers_alerts(tmp_path, monkeypatch,
                                                       capsys):
    # End-to-end B6: grade_pending raises AFTER save_state; main must still
    # print the JSON payload (signals already stamped as sent).
    sp, _ = _paths(tmp_path, monkeypatch)
    sp.write_text(json.dumps({"alerted": {}}))
    now = int(time.time())
    market = {
        "ticker": "KXBTC15M-26SEP301900-00", "title": "BTC up?",
        "open_time": datetime.fromtimestamp(now - 600,
                                           timezone.utc).isoformat(),
        "close_time": datetime.fromtimestamp(now + 300,
                                            timezone.utc).isoformat(),
        "yes_bid_dollars": "0.49", "yes_ask_dollars": "0.51",
        "volume_fp": "1000",
    }
    monkeypatch.setattr(sdl, "kalshi_same_day", lambda today: [market])
    monkeypatch.setattr(sdl, "poly_same_day", lambda today: [])
    # fair 0.60 vs mid 0.50 -> post-shrink edge 0.075 -> YES fires on the
    # agreement side (taken-side price 0.50 >= 0.5)
    monkeypatch.setattr(sdl, "fair_15m",
                        lambda *a: (0.60, 100.0, 100.0, 0.001, 5.0, a[3]))
    def _raise(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(sdl, "grade_pending", _raise)
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py"])  # mark mode
    main()
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["signals_new"] == 1
    assert any("grading_failed=" in n for n in out["notes"])
    assert out["accuracy"]["graded"] == 0
    state = json.loads(sp.read_text())
    assert "KXBTC15M-26SEP301900-00:YES" in state["alerted"]  # stamped, sent
