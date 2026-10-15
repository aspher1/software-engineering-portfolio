"""Raw-fair logging: the pre-shrink fair is captured into each 15-min alerted
state entry at alert time and copied onto the grade row at grade time, so
future iterations can re-validate the shrink_midrange upgrade on fresh data
(inverting the shrink from the recorded edge is not exact when the market
mid sits outside the shrink band).

Purely additive metadata — entry, exit, grading, and delivery logic never
read these fields, so no decision can change. Paper/analysis only; no
network (all I/O monkeypatched)."""
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


def _patch(tmp_path, monkeypatch):
    sp = tmp_path / "state.json"
    gp = tmp_path / "grades.jsonl"
    sp.write_text(json.dumps({"alerted": {}}))
    monkeypatch.setattr(sdl, "STATE_PATH", str(sp))
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    now = int(time.time())
    monkeypatch.setattr(sdl, "kalshi_same_day",
                        lambda today: [_live_market(now)])
    monkeypatch.setattr(sdl, "poly_same_day", lambda today: [])
    # raw fair 0.60, mid 0.52 -> shrink pulls fair to 0.58 ->
    # edge 0.06 -> YES fires on the agreement side
    # (taken-side price 0.52 >= 0.5). fair_raw must stay 0.60.
    monkeypatch.setattr(sdl, "fair_15m",
                        lambda *a: (0.60, 100.0, 100.0, 0.001, 5.0, a[3]))
    return sp, now


def test_alerted_entry_carries_pre_shrink_fair(tmp_path, monkeypatch, capsys):
    sp, _ = _patch(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py"])  # mark mode
    main()
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    state = json.loads(sp.read_text())
    key = "KXBTC15M-26SEP301900-00:YES"
    e = state["alerted"][key]
    assert e["fair_raw"] == 0.6            # pre-shrink fair, exact
    assert e["edge"] == 0.06                 # shrunk edge
    assert e["market_mid"] == 0.52
    # delivery payload unchanged: the new field stays in state, never printed
    sig = [s for s in out["new_signals"] if s["ticker"] == key.split(":")[0]][0]
    assert "fair_raw" not in sig
    assert sig["fair"] == 0.58                  # printed fair is shrunk


def test_no_mark_still_writes_nothing(tmp_path, monkeypatch, capsys):
    sp, _ = _patch(tmp_path, monkeypatch)
    before = sp.read_bytes()
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py", "--no-mark"])
    main()
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert sp.read_bytes() == before  # fair_raw adds nothing w/o mark


def test_grade_row_carries_fair_raw_through(tmp_path, monkeypatch):
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    # Kalshi-only settlement: NO take settles NO -> correct; YES take
    # settles NO -> wrong.
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda ticker: "NO")
    now = int(time.time())
    alerted = {
        "KXBTC15M-26SEP301700-00:NO": {
            "ts": datetime.fromtimestamp(now - 1200, timezone.utc).isoformat(),
            "ticker": "KXBTC15M-26SEP301700-00", "side": "NO",
            "edge": -0.10, "market_mid": 0.60, "fair_raw": 0.50,
            "window_open_utc": now - 2100, "window_close_utc": now - 1200,
        },
        # legacy entry without the new field -> None, grading unaffected
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
    assert by_key["KXBTC15M-26SEP301700-00:NO"]["fair_raw"] == 0.50
    assert by_key["KXETH15M-26SEP301700-00:YES"]["fair_raw"] is None
    # grading math untouched by the new field
    assert by_key["KXBTC15M-26SEP301700-00:NO"]["correct"] is True
    assert by_key["KXETH15M-26SEP301700-00:YES"]["correct"] is False
