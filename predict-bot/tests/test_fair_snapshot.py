import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import same_day_loop as sdl


def test_write_fair_snapshot_roundtrip(tmp_path):
    snap = {"ETH": {"ticker": "KXETH15M-X", "mid": 0.52, "fair": 0.58,
                    "edge": 0.06, "abs_edge": 0.06, "model_side": "YES",
                    "has_signal": True, "spread": 0.02, "mins_left": 9.5}}
    sdl.write_fair_snapshot(str(tmp_path), snap, "2026-10-02T19:30:00+00:00",
                            1790969400)
    d = json.loads((tmp_path / "latest_fair.json").read_text())
    assert d["as_of_ts"] == 1790969400
    assert d["coins"]["ETH"]["edge"] == 0.06
    assert d["coins"]["ETH"]["has_signal"] is True
    # no temp file left behind
    assert not (tmp_path / "latest_fair.json.tmp").exists()


def test_write_fair_snapshot_never_raises(tmp_path):
    # unwritable dir must not break the scan
    sdl.write_fair_snapshot(str(tmp_path / "nope"), {}, "x", 0)


def test_write_fair_snapshot_empty_coins(tmp_path):
    sdl.write_fair_snapshot(str(tmp_path), {}, "2026-10-02T19:30:00+00:00",
                            1790969400)
    d = json.loads((tmp_path / "latest_fair.json").read_text())
    assert d["coins"] == {}
