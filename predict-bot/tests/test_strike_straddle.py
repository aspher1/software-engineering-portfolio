import sys, os, json, time
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest

import same_day_loop as sdl
from same_day_loop import (
    straddles_strike, STRIKE_STRADDLE_GATE, EDGE_THRESHOLD, WINDOW_WARMUP_MIN,
    main,
)


def test_gate_ships_disabled():
    assert STRIKE_STRADDLE_GATE is False


@pytest.mark.parametrize("p_now,p_ref,strike,expected", [
    (100.0, 99.0, 101.0, True),    # above Kraken ref, below strike
    (100.0, 101.0, 99.0, True),    # below Kraken ref, above strike
    (100.0, 99.0, 98.0, False),    # above both
    (100.0, 101.0, 102.0, False),  # below both
    (100.0, 100.0, 100.0, False),  # ties count as "at or above" for both
    (100.0, 100.0, 100.5, True),   # at ref (above) but below strike
    (100.0, 99.5, 100.0, False),   # at strike counts as above (greater_or_equal)
])
def test_straddle_truth_table(p_now, p_ref, strike, expected):
    assert straddles_strike(p_now, p_ref, strike) is expected


@pytest.mark.parametrize("strike", [None, "", "abc", 0, -5.0, float("nan")])
def test_missing_or_invalid_strike_never_straddles(strike):
    assert straddles_strike(100.0, 99.0, strike) is False


def test_numeric_string_strike_accepted():
    assert straddles_strike(100.0, 99.0, "101.25") is True


def test_regression_other_gates_unchanged():
    assert EDGE_THRESHOLD == 0.05
    assert WINDOW_WARMUP_MIN == 3.0


# ---------- scan-loop wiring ----------

TICKER = "KXBTC15M-26SEP301900-00"


def _market(now, strike):
    m = {
        "ticker": TICKER, "title": "BTC up?",
        "open_time": datetime.fromtimestamp(now - 600, timezone.utc).isoformat(),
        "close_time": datetime.fromtimestamp(now + 300, timezone.utc).isoformat(),
        "yes_bid_dollars": "0.60", "yes_ask_dollars": "0.62",
        "volume_fp": "1000",
    }
    if strike is not None:
        m["floor_strike"] = strike
    return m


def _run(tmp_path, monkeypatch, capsys, gate, strike, p_ref=99.0, p_now=100.0):
    sp, gp = tmp_path / "state.json", tmp_path / "grades.jsonl"
    sp.write_text(json.dumps({"alerted": {}}))
    monkeypatch.setattr(sdl, "STATE_PATH", str(sp))
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    monkeypatch.setattr(sdl, "STRIKE_STRADDLE_GATE", gate)
    now = int(time.time())
    monkeypatch.setattr(sdl, "kalshi_same_day", lambda today: [_market(now, strike)])
    monkeypatch.setattr(sdl, "poly_same_day", lambda today: [])
    # fair 0.70 is outside the shrink band: edge = 0.70 - 0.61 = +0.09 YES,
    # taken-side price 0.61 >= 0.5 so the agreement filter keeps it
    monkeypatch.setattr(sdl, "fair_15m", lambda *a: (0.70, p_ref, p_now, 0.001, 5.0, a[3]))
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py"])
    main()
    out = json.loads(capsys.readouterr().out)
    return out, json.loads(sp.read_text())["alerted"]


def test_gate_off_straddle_still_fires(tmp_path, monkeypatch, capsys):
    out, alerted = _run(tmp_path, monkeypatch, capsys, gate=False, strike=101.0)
    assert out["signals_new"] == 1
    assert f"{TICKER}:YES" in alerted


def test_gate_on_straddle_suppressed_and_not_marked(tmp_path, monkeypatch, capsys):
    out, alerted = _run(tmp_path, monkeypatch, capsys, gate=True, strike=101.0)
    assert out["signals_total"] == 0
    assert alerted == {}   # unmarked: the next scan re-evaluates it


def test_gate_on_no_straddle_fires(tmp_path, monkeypatch, capsys):
    out, alerted = _run(tmp_path, monkeypatch, capsys, gate=True, strike=98.0)
    assert out["signals_new"] == 1
    assert alerted[f"{TICKER}:YES"]["edge"] == pytest.approx(0.09)


def test_gate_on_missing_strike_fires(tmp_path, monkeypatch, capsys):
    out, _ = _run(tmp_path, monkeypatch, capsys, gate=True, strike=None)
    assert out["signals_new"] == 1
