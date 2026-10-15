"""Iter-20 lane-codex-astra proposed fixes (decision-neutral):
R2 residual — copilot advisory attached AFTER the state lock is released;
flip_analyst.py:117 — derive entry_price_taken from side + market_mid
(as hold_guard.entry_taken does) instead of reading a field state records
never store."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import same_day_loop as sdl
import exit_copilot
import flip_analyst as fa


def _stub_fair(fair):
    def _f(pair, w_open, w_close, now_ts):
        return (fair, 100.0, 100.0, 0.001, 10.0, now_ts)
    return _f


def _write_open_state(path, now):
    with open(path, "w") as f:
        json.dump({"alerted": {
            "KXBTC15M-26OCT021900-00:YES": {
                "ts": "2026-10-02T21:50:00+00:00",
                "ticker": "KXBTC15M-26OCT021900-00",
                "side": "YES", "edge": 0.10, "market_mid": 0.50,
                "window_open_utc": now - 300, "window_close_utc": now + 600,
            }}}, f)


def _quote(ticker):
    return ({"ticker": ticker, "yes_bid_dollars": 0.595,
             "yes_ask_dollars": 0.605}, None)


# ---------------- R2 residual: copilot out of the state lock ----------------

def test_r2_copilot_called_after_state_save_not_during_lock(tmp_path, monkeypatch):
    """exits_only_locked: the ~4s LLM call must happen strictly after the
    state lock is released. save_state is the last statement inside the lock,
    so asserting copilot-after-save asserts copilot-after-release."""
    now = int(time.time())
    st = tmp_path / "state.json"
    _write_open_state(st, now)
    monkeypatch.setattr(sdl, "STATE_PATH", str(st))
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    monkeypatch.setattr(sdl, "_fetch_quote", _quote)
    order = []
    real_save = sdl.save_state

    def spy_save(payload):
        order.append("save")
        return real_save(payload)

    monkeypatch.setattr(sdl, "save_state", spy_save)
    monkeypatch.setattr(
        exit_copilot, "maybe_copilot_review",
        lambda e: order.append("copilot") or {"decision": "EXIT"})
    payload = sdl.exits_only_locked(now)
    assert payload["ok"] and payload["exits_new"] == 1
    assert order == ["save", "copilot"], order
    assert payload["exits"][0]["copilot"]["decision"] == "EXIT"


def test_r2_attach_copilot_never_raises(monkeypatch):
    def boom(e):
        raise RuntimeError("llm down")

    monkeypatch.setattr(exit_copilot, "maybe_copilot_review", boom)
    sdl.attach_copilot_advice([{"ticker": "X"}])  # no raise, no attachment
    sdl.attach_copilot_advice([])                 # empty: never consulted
    sdl.attach_copilot_advice(None)               # None: never consulted


def test_r2_attach_copilot_attaches_to_most_urgent(monkeypatch):
    calls = []
    monkeypatch.setattr(
        exit_copilot, "maybe_copilot_review",
        lambda e: calls.append(e) or {"decision": "HOLD", "reason": "r"})
    exits = [{"ticker": "A", "now_edge": -0.05}, {"ticker": "B", "now_edge": -0.01}]
    sdl.attach_copilot_advice(exits)
    assert len(calls) == 1 and calls[0]["ticker"] == "A"
    assert exits[0]["copilot"]["decision"] == "HOLD"
    assert "copilot" not in exits[1]


# ---------------- flip_analyst.py:117 entry_price_taken ----------------

def _write_scan_state(path, now, entries):
    with open(path, "w") as f:
        json.dump({"alerted": {e["key"]: e for e in entries}}, f)


def test_fa_entry_price_taken_derived_from_mid_and_side(tmp_path, monkeypatch):
    now = int(time.time())
    p = tmp_path / "scan_state.json"
    _write_scan_state(p, now, [
        {"key": "KXSOL15M-26OCT022000-00:NO",
         "ticker": "KXSOL15M-26OCT022000-00", "side": "NO",
         "market_mid": 0.375, "window_close_utc": now + 600},
        {"key": "KXBTC15M-26OCT022000-00:YES",
         "ticker": "KXBTC15M-26OCT022000-00", "side": "YES",
         "market_mid": 0.60, "window_close_utc": now + 600},
        {"key": "KXETH15M-26OCT022000-00:NO",
         "ticker": "KXETH15M-26OCT022000-00", "side": "NO",
         "window_close_utc": now + 600},  # no mid logged -> None, never crash
    ])
    monkeypatch.setattr(fa, "SCAN_STATE_PATH", str(p))
    pos = {x["ticker"]: x for x in fa.get_open_positions(now)}
    assert pos["KXSOL15M-26OCT022000-00"]["entry_price_taken"] == pytest.approx(0.625)
    assert pos["KXBTC15M-26OCT022000-00"]["entry_price_taken"] == pytest.approx(0.60)
    assert pos["KXETH15M-26OCT022000-00"]["entry_price_taken"] is None


def test_fa_render_shows_derived_entry_price():
    def user_content(positions):
        msgs = fa.render_messages([], positions, {}, None)
        return msgs[1]["content"]

    txt = user_content([{"coin": "SOL", "side": "NO", "entry_price_taken": 0.625,
                           "ticker": "KXSOL15M-26OCT022000-00"}])
    assert "SOL NO (entry 62.5c)" in txt
    txt2 = user_content([{"coin": "ETH", "side": "NO", "entry_price_taken": None,
                            "ticker": "KXETH15M-26OCT022000-00"}])
    assert "ETH NO (entry ?)" in txt2
