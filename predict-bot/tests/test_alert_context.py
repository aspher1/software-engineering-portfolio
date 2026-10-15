"""Alert-context persistence: spread and mins_left are captured into each
15-min alerted state entry at alert time. Purely additive metadata — entry,
exit, and grading logic never read these fields, so no decision can change.
Paper/analysis only; no network (all I/O monkeypatched)."""
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
        "yes_bid_dollars": "0.60", "yes_ask_dollars": "0.62",
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
    # raw fair 0.75 (outside the shrink band) vs mid 0.61 ->
    # edge = 0.75 - 0.61 = 0.14 -> YES fires on the agreement side
    # (taken-side price 0.61 >= 0.5), t = 5.0 mins left,
    # spread = 0.62 - 0.60 = 0.02
    monkeypatch.setattr(sdl, "fair_15m",
                        lambda *a: (0.75, 100.0, 100.0, 0.001, 5.0, a[3]))
    return sp, now


def test_alerted_entry_carries_spread_and_mins_left(tmp_path, monkeypatch,
                                                   capsys):
    sp, _ = _patch(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py"])  # mark mode
    main()
    capsys.readouterr()
    state = json.loads(sp.read_text())
    key = "KXBTC15M-26SEP301900-00:YES"
    assert key in state["alerted"]
    e = state["alerted"][key]
    assert e["spread"] == 0.02
    assert e["mins_left"] == 5.0
    # original fields untouched
    assert e["side"] == "YES"
    assert e["edge"] == 0.14
    assert e["market_mid"] == 0.61


def test_no_mark_still_writes_nothing(tmp_path, monkeypatch, capsys):
    sp, _ = _patch(tmp_path, monkeypatch)
    before = sp.read_bytes()
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py", "--no-mark"])
    main()
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert sp.read_bytes() == before  # context fields add nothing w/o mark


def test_extra_fields_do_not_affect_grading(tmp_path, monkeypatch):
    # grade_pending copies alert-time context (spread/mins_left) onto the
    # grade row as additive metadata for future regime analysis; grading
    # math reads only ticker/side/windows/edge/mid/ts. Settlement is
    # Kalshi-only: stub "NO" for the NO take -> correct.
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda ticker: "NO")
    now = int(time.time())
    alerted = {"KXBTC15M-26SEP301700-00:NO": {
        "ts": datetime.fromtimestamp(now - 1200, timezone.utc).isoformat(),
        "ticker": "KXBTC15M-26SEP301700-00", "side": "NO",
        "edge": -0.10, "market_mid": 0.60,
        "window_open_utc": now - 2100, "window_close_utc": now - 1200,
        "spread": 0.03, "mins_left": 4.5,
    }}
    new, acc = sdl.grade_pending(alerted, now, mark=True)
    assert len(new) == 1
    assert new[0]["correct"] is True  # settlement NO -> NO correct
    assert new[0]["spread"] == 0.03     # copied through, grading untouched
    assert new[0]["mins_left"] == 4.5
