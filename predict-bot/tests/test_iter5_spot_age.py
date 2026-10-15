"""Iter-5: alert-time spot-age + entry-context logging (C2/C4).

C2: fair_15m returns a 6-tuple with the exchange timestamp of the last
trade used as p_now; the scan logs `spot_age_s` (seconds, at alert time)
into each marked 15-min entry; grading copies it onto the grade row.
C4: grading also copies the already-logged alert-time `spread` and
`mins_left` onto the grade row so future iterations can slice accuracy by
entry-time market context.

All purely additive metadata — entry, exit, grading, and delivery logic
never read these fields, so no decision can change. Paper/analysis only;
no network (all I/O monkeypatched).
"""
import json
import sys
import os
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import same_day_loop as sdl
from same_day_loop import main


def _live_market(now):
    return {
        "ticker": "KXBTC15M-26SEP301900-00", "title": "BTC up?",
        "open_time": datetime.fromtimestamp(now - 600,
                                           timezone.utc).isoformat(),
        "close_time": datetime.fromtimestamp(now + 300,
                                            timezone.utc).isoformat(),
        "yes_bid_dollars": "0.51", "yes_ask_dollars": "0.53",
        "volume_fp": "1000",
    }


def _patch(tmp_path, monkeypatch, spot_lag_s=45):
    sp = tmp_path / "state.json"
    gp = tmp_path / "grades.jsonl"
    sp.write_text(json.dumps({"alerted": {}}))
    monkeypatch.setattr(sdl, "STATE_PATH", str(sp))
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    now = int(time.time())
    monkeypatch.setattr(sdl, "kalshi_same_day",
                        lambda today: [_live_market(now)])
    monkeypatch.setattr(sdl, "poly_same_day", lambda today: [])
    # raw fair 0.60, mid 0.52 -> shrunk 0.58 -> edge 0.06 -> YES fires;
    # p_now is `spot_lag_s` seconds old at alert time.
    monkeypatch.setattr(sdl, "fair_15m",
                        lambda *a: (0.60, 100.0, 100.0, 0.001, 5.0,
                                    a[3] - spot_lag_s))
    return sp, gp, now


def test_alerted_entry_carries_spot_age(tmp_path, monkeypatch, capsys):
    sp, _, _ = _patch(tmp_path, monkeypatch, spot_lag_s=45)
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py"])  # mark mode
    main()
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    state = json.loads(sp.read_text())
    key = "KXBTC15M-26SEP301900-00:YES"
    e = state["alerted"][key]
    assert e["spot_age_s"] == 45
    assert isinstance(e["spot_age_s"], int)
    # pre-existing alert-time context still logged
    assert e["spread"] == 0.02
    assert e["mins_left"] == 5.0
    # delivery payload unchanged: the new field stays in state, never printed
    sig = [s for s in out["new_signals"] if s["ticker"] == key.split(":")[0]][0]
    assert "spot_age_s" not in sig


def test_no_mark_still_writes_nothing(tmp_path, monkeypatch, capsys):
    sp, _, _ = _patch(tmp_path, monkeypatch)
    before = sp.read_bytes()
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py", "--no-mark"])
    main()
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert sp.read_bytes() == before  # spot_age_s adds nothing w/o mark


def test_grade_row_carries_spot_age_spread_mins_left(tmp_path, monkeypatch):
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda ticker: "NO")
    now = int(time.time())
    alerted = {
        "KXBTC15M-26SEP301700-00:NO": {
            "ts": datetime.fromtimestamp(now - 1200, timezone.utc).isoformat(),
            "ticker": "KXBTC15M-26SEP301700-00", "side": "NO",
            "edge": -0.10, "market_mid": 0.60,
            "window_open_utc": now - 2100, "window_close_utc": now - 1200,
            "spot_age_s": 12, "spread": 0.03, "mins_left": 7.5,
        },
        # legacy entry without the new fields -> None, grading unaffected
        "KXETH15M-26SEP301700-00:YES": {
            "ts": datetime.fromtimestamp(now - 1200, timezone.utc).isoformat(),
            "ticker": "KXETH15M-26SEP301700-00", "side": "YES",
            "edge": 0.08, "market_mid": 0.30,
            "window_open_utc": now - 2100, "window_close_utc": now - 1200,
        },
    }
    new, acc = sdl.grade_pending(alerted, now, mark=True)
    assert len(new) == 2
    by_key = {r["key"]: r for r in new}
    assert by_key["KXBTC15M-26SEP301700-00:NO"]["spot_age_s"] == 12
    assert by_key["KXBTC15M-26SEP301700-00:NO"]["spread"] == 0.03
    assert by_key["KXBTC15M-26SEP301700-00:NO"]["mins_left"] == 7.5
    legacy = by_key["KXETH15M-26SEP301700-00:YES"]
    assert legacy["spot_age_s"] is None
    assert legacy["spread"] is None
    assert legacy["mins_left"] is None
    # grading math untouched by the new fields
    assert by_key["KXBTC15M-26SEP301700-00:NO"]["correct"] is True
    assert legacy["correct"] is False


def _candles(now, n=75, px=100.0):
    return [(now - 60 * (n - i), px * (1 + 0.0001 * i)) for i in range(n)]


def test_fair_15m_returns_spot_ts(monkeypatch):
    now = int(time.time())
    trades = [[100.0 + 0.01 * i, 1.0, now - 600 + i] for i in range(570)]
    # last trade is 30s old
    trades[-1][2] = now - 30
    monkeypatch.setattr(sdl, "kraken_trades", lambda pair, since: trades)
    monkeypatch.setattr(sdl, "kraken_candles", lambda pair: _candles(now))
    res = sdl.fair_15m("XXBTZUSD", now - 300, now, now)
    assert res is not None and len(res) == 6
    fair, p_ref, p_now, sigma, t, p_now_ts = res
    assert p_now_ts == now - 30
    assert p_now == trades[-1][0]
    assert 0.0 <= fair <= 1.0 and sigma > 0 and t == 0.5


def test_fair_15m_first_five_unchanged_vs_pristine_shape(monkeypatch):
    # The 6-tuple extends the old 5-tuple: first five elements keep their
    # meaning (fair, p_ref, p_now, sigma, t).
    now = int(time.time())
    trades = [[100.0, 1.0, now - 600 + i] for i in range(570)]
    monkeypatch.setattr(sdl, "kraken_trades", lambda pair, since: trades)
    monkeypatch.setattr(sdl, "kraken_candles", lambda pair: _candles(now))
    res = sdl.fair_15m("XXBTZUSD", now - 300, now, now)
    fair, p_ref, p_now, sigma, t, p_now_ts = res
    assert p_ref == 100.0 and p_now == 100.0 and fair == 0.5
