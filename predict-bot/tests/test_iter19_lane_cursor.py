"""Iter-19 lane-cursor shipped fixes: R1 accuracy_stats denominators, R2 copilot
gated on mark. Paper/analysis only; no network (all I/O monkeypatched)."""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest

import same_day_loop as sdl
from same_day_loop import accuracy_stats, check_exits
import exit_copilot


def _g(side, mid, correct):
    return {"side": side, "market_mid": mid, "correct": correct,
            "proxy": "kalshi"}


# ---------------- R1: matched-denominator companions ----------------

def test_r1_win_rate_priced_ignores_midless_rows():
    acc = accuracy_stats([_g("YES", None, True), _g("YES", 0.50, False)])
    # legacy key untouched (all scored rows)
    assert acc["win_rate"] == 0.5
    # matched-denominator companions use the priced population only
    assert acc["no_mid_rows"] == 1
    assert acc["win_rate_priced"] == 0.0


def test_r1_win_rate_priced_all_priced():
    acc = accuracy_stats([_g("YES", 0.40, True), _g("NO", 0.70, True),
                          _g("YES", 0.60, False)])
    assert acc["no_mid_rows"] == 0
    assert acc["win_rate_priced"] == pytest.approx(0.667)
    assert acc["win_rate_priced"] == acc["win_rate"]


def test_r1_win_rate_priced_none_when_no_priced_rows():
    acc = accuracy_stats([_g("YES", None, True)])
    assert acc["no_mid_rows"] == 1
    assert acc["win_rate_priced"] is None
    assert acc["win_rate"] == 1.0  # legacy semantics unchanged


def test_r1_win_rate_priced_matches_implied_pnl_population():
    # win_rate_priced now shares the population of mean_taken_price and the
    # _pnl_* helpers (all skip mid-less rows)
    rows = [_g("YES", None, True), _g("YES", 0.50, True), _g("YES", 0.50, False)]
    acc = accuracy_stats(rows)
    assert acc["win_rate_priced"] == 0.5
    assert acc["mean_taken_price"] == 0.5
    assert acc["no_mid_rows"] == 1


def test_r1_new_keys_appended_after_legacy_keys():
    acc = accuracy_stats([_g("YES", 0.40, True)])
    keys = list(acc)
    assert keys.index("no_mid_rows") > keys.index("pnl_1usd_stake_net")
    assert keys.index("win_rate_priced") > keys.index("pnl_1usd_stake_net")


# ---------------- R2: copilot silent under --no-mark ----------------

def _alerted(now):
    return {
        "KXBTC15M-26SEP301800-00:YES": {
            "ts": "2026-09-30T21:50:00+00:00", "ticker": "KXBTC15M-26SEP301800-00",
            "side": "YES", "edge": 0.10, "market_mid": 0.50,
            "window_open_utc": now - 300, "window_close_utc": now + 600,
        }
    }


def _live(bid=0.595, ask=0.605):
    now = int(time.time())
    m = {"ticker": "KXBTC15M-26SEP301800-00", "title": "BTC price up?",
         "yes_bid_dollars": bid, "yes_ask_dollars": ask}
    return {"KXBTC15M-26SEP301800-00": (m, "BTC", now - 300, now + 600)}


def _stub_fair(fair):
    def _f(pair, w_open, w_close, now_ts):
        return (fair, 100.0, 100.0, 0.001, 10.0, now_ts)
    return _f


def test_r2_copilot_silent_when_mark_false(monkeypatch):
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    calls = []
    monkeypatch.setattr(
        exit_copilot, "maybe_copilot_review",
        lambda exit_dict: calls.append(exit_dict) or {"decision": "HOLD"})
    alerted = _alerted(int(time.time()))
    exits = check_exits(alerted, _live(), int(time.time()), mark=False)
    assert len(exits) == 1            # the exit itself still fires (decision)
    assert calls == []               # but the copilot was never consulted
    assert "copilot" not in exits[0]  # and nothing was attached


def test_r2_copilot_not_called_inside_check_exits(monkeypatch):
    # iter-20 R2 residual: the copilot is attached by callers AFTER the state
    # lock is released, so check_exits itself must never consult it (the
    # decision layer stays pure); attach_copilot_advice does the attaching.
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    calls = []
    monkeypatch.setattr(
        exit_copilot, "maybe_copilot_review",
        lambda exit_dict: calls.append(exit_dict) or
        {"decision": "EXIT", "reason": "test", "latency_ms": 1,
         "backend": "stub"})
    for mark in (True, False):
        exits = check_exits(_alerted(int(time.time())), _live(),
                            int(time.time()), mark=mark)
        assert len(exits) == 1            # the exit itself still fires
        assert calls == []               # decision layer never consults
        assert "copilot" not in exits[0]


def test_r2_attach_copilot_advice_attaches(monkeypatch):
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    calls = []
    monkeypatch.setattr(
        exit_copilot, "maybe_copilot_review",
        lambda exit_dict: calls.append(exit_dict) or
        {"decision": "EXIT", "reason": "test", "latency_ms": 1,
         "backend": "stub"})
    exits = check_exits(_alerted(int(time.time())), _live(),
                        int(time.time()), mark=True)
    assert len(calls) == 0
    sdl.attach_copilot_advice(exits)     # caller-side, post-lock
    assert len(calls) == 1
    assert exits[0]["copilot"]["decision"] == "EXIT"


# ---------------- R4: test hermeticity (no writes to the real data/ dir) ---

def test_r4_sdl_data_redirected_off_repo():
    # The conftest autouse fixture must keep sdl.DATA off the repo's real
    # data/ dir: a mark-mode main() run writes data/latest_fair.json, and
    # the live flip analyst trusts that file for 180 s. A pytest run must
    # never inject a fake model snapshot into the live analyst's context.
    repo_data = os.path.realpath(
        os.path.join(os.path.dirname(sdl.__file__), "..", "data"))
    assert os.path.realpath(sdl.DATA) != repo_data


def test_r4_mark_run_writes_fair_snapshot_to_tmp_data(tmp_path, monkeypatch, capsys):
    # end-to-end: a mark-mode scan writes latest_fair.json under the
    # redirected DATA, not the repo dir.
    import json as _json
    repo_data = os.path.realpath(
        os.path.join(os.path.dirname(sdl.__file__), "..", "data"))
    before = os.path.getmtime(os.path.join(repo_data, "latest_fair.json")) \
        if os.path.exists(os.path.join(repo_data, "latest_fair.json")) else None
    sp = tmp_path / "state.json"
    sp.write_text(_json.dumps({"alerted": {}}))
    gp = tmp_path / "grades.jsonl"
    monkeypatch.setattr(sdl, "STATE_PATH", str(sp))
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    monkeypatch.setattr(sdl, "kalshi_same_day", lambda today: [])
    monkeypatch.setattr(sdl, "poly_same_day", lambda today: [])
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py"])
    sdl.main()
    capsys.readouterr()
    snap = os.path.join(os.path.realpath(sdl.DATA), "latest_fair.json")
    assert os.path.exists(snap)
    after = os.path.getmtime(os.path.join(repo_data, "latest_fair.json")) \
        if before is not None else None
    assert after == before  # repo file untouched
