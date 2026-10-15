import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import flip_analyst as fa


def _ts(offset_s=0):
    return time.time() + offset_s


# ---------------------------------------------------------------- parser

def test_parse_call_good():
    a, c, v, r, w = fa.parse_call(
        "CALL: BUY_NO SOL - TAPE-ONLY\n"
        "REASON: 19c flush, no model edge\n"
        "WATCH: a reclaim of 65c")
    assert (a, c, v) == ("BUY_NO", "SOL", "TAPE-ONLY")
    assert r == "19c flush, no model edge"
    assert w == "a reclaim of 65c"


def test_parse_call_model_tape():
    a, c, v, _, _ = fa.parse_call(
        "CALL: BUY_YES ETH - MODEL+TAPE\nREASON: x\nWATCH: y")
    assert (a, c, v) == ("BUY_YES", "ETH", "MODEL+TAPE")


def test_parse_call_missing_line_fails():
    assert fa.parse_call("CALL: SKIP BTC - TAPE-ONLY\nREASON: x") == (
        None, None, None, None, None)
    assert fa.parse_call("") == (None, None, None, None, None)
    assert fa.parse_call(None) == (None, None, None, None, None)


def test_parse_call_bad_action_fails():
    assert fa.parse_call(
        "CALL: YOLO BTC - TAPE-ONLY\nREASON: x\nWATCH: y")[0] is None


def test_parse_call_truncates_long_lines():
    _, _, _, r, _ = fa.parse_call(
        "CALL: HOLD BTC - TAPE-ONLY\nREASON: " + "x" * 500 + "\nWATCH: w")
    assert len(r) == fa.MAX_REASON_CHARS


# ---------------------------------------------------------------- gating

def test_should_analyze_no_alerts():
    ok, reason = fa.should_analyze({}, {}, _ts())
    assert ok is False and reason == "no_alerts"


def test_should_analyze_cooldown_elapsed():
    state = {"last_read_ts": _ts(-200), "dirs": {}}
    ok, reason = fa.should_analyze({"ETH": "up"}, state, _ts())
    assert ok is True and reason == "cooldown_elapsed"


def test_should_analyze_cooling_down():
    state = {"last_read_ts": _ts(-10), "dirs": {"ETH": {"dir": "up", "ts": _ts(-10)}}}
    ok, reason = fa.should_analyze({"ETH": "up"}, state, _ts())
    assert ok is False and reason == "cooling_down"


def test_should_analyze_reversal_bypasses_cooldown():
    state = {"last_read_ts": _ts(-10), "dirs": {"ETH": {"dir": "up", "ts": _ts(-10)}}}
    ok, reason = fa.should_analyze({"ETH": "down"}, state, _ts())
    assert ok is True and reason.startswith("reversal:")


def test_should_analyze_new_coin_not_a_reversal():
    # a coin absent from last_dirs is expansion, not reversal: cooldown holds
    state = {"last_read_ts": _ts(-10), "dirs": {"ETH": {"dir": "up", "ts": _ts(-10)}}}
    ok, _ = fa.should_analyze({"ETH": "up", "SOL": "up"}, state, _ts())
    assert ok is False


# ---------------------------------------------------------------- tape

def _hook_state(now):
    return {"tickers": {
        "KXETH15M-X": {
            "hist": [[now - 20, 0.43], [now - 10, 0.50], [now, 0.55]],
            "last_alert_ts": now - 5, "last_alert_dir": "up"},
        "KXBTC15M-X": {
            "hist": [[now - 20, 0.60], [now, 0.615]],
            "last_alert_ts": 0},
        "KXXRP15M-X": {
            "hist": [[now - 20, 0.50], [now, 0.505]],
            "last_alert_ts": 0},
    }}


def test_build_tape_includes_fast_legs(tmp_path, monkeypatch):
    now = _ts()
    p = tmp_path / "hook.json"
    p.write_text(json.dumps(_hook_state(now)))
    monkeypatch.setattr(fa, "HOOK_STATE_PATH", str(p))
    # isolate from the live 5s poll loop's real stash file
    monkeypatch.setattr(fa, "LATEST_FLIPS_PATH",
                        str(tmp_path / "nope.json"))
    lines, dirs, _bid, _fl = fa.build_tape(now)
    joined = " ".join(lines)
    assert "ETH" in joined and "UP" in joined
    assert "crossed 50c" in joined
    assert "XRP" not in joined  # 0.5c move < 10c floor
    assert dirs == {"ETH": "up"}  # BTC moved but never alerted


def test_build_tape_empty_state():
    lines, dirs, _bid, _fl = fa.build_tape(_ts())
    # real hook state file may or may not exist; must never raise
    assert isinstance(lines, list) and isinstance(dirs, dict)


def _latest_batch(now):
    return {
        "stashed_at": now - 5,
        "flips": [
            {"coin": "XRP", "direction": "toward NO", "from_mid": 0.82,
             "to_mid": 0.76, "move_pts": 0.06, "span_s": 23,
             "crossed_50": False, "mins_left": 4.6},
            {"coin": "NEAR", "direction": "toward NO", "from_mid": 0.20,
             "to_mid": 0.096, "move_pts": 0.105, "span_s": 23,
             "crossed_50": False, "mins_left": 4.6},
        ],
    }


def test_build_tape_prefers_stashed_batch(tmp_path, monkeypatch):
    now = _ts()
    p = tmp_path / "latest.json"
    p.write_text(json.dumps(_latest_batch(now)))
    monkeypatch.setattr(fa, "LATEST_FLIPS_PATH", str(p))
    lines, dirs, _bid, _fl = fa.build_tape(now)
    assert len(lines) == 2
    assert any("XRP DOWN 82.0c->76.0c" in l for l in lines)
    assert dirs == {"XRP": "down", "NEAR": "down"}


def _write_batch_file(tmp_path, monkeypatch, batch_id, flips, age_s=5):
    d = tmp_path / "batches"
    d.mkdir(exist_ok=True)
    now = _ts()
    (d / (batch_id + ".json")).write_text(json.dumps({
        "stashed_at": now - age_s, "batch_id": batch_id, "flips": flips}))
    monkeypatch.setattr(fa, "HOOK_BATCH_DIR", str(d))
    return now


def test_build_tape_batch_id_selects_exact_batch(tmp_path, monkeypatch):
    now = _write_batch_file(tmp_path, monkeypatch, "111-222",
                            [_flip("SOL", "toward YES", frm=0.30, to=0.45)])
    # a different (newer) batch sits in latest.json — must NOT win
    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps(_latest_batch(now)))
    monkeypatch.setattr(fa, "LATEST_FLIPS_PATH", str(latest))
    lines, dirs, bid, flips = fa.build_tape(now, batch_id="111-222")
    assert bid == "111-222"
    assert dirs == {"SOL": "up"}
    assert any("SOL UP 30.0c->45.0c" in l for l in lines)


def test_build_tape_bad_batch_id_falls_back(tmp_path, monkeypatch):
    now = _write_batch_file(tmp_path, monkeypatch, "111-222",
                            [_flip("SOL", "toward YES", frm=0.30, to=0.45)])
    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps(_latest_batch(now)))
    monkeypatch.setattr(fa, "LATEST_FLIPS_PATH", str(latest))
    # missing file -> latest.json fallback
    lines, dirs, bid, _ = fa.build_tape(now, batch_id="999-888")
    assert bid is None  # latest.json test batch has no batch_id
    assert dirs == {"XRP": "down", "NEAR": "down"}


def test_load_batch_rejects_path_traversal(tmp_path, monkeypatch):
    d = tmp_path / "batches"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(fa, "HOOK_BATCH_DIR", str(d))
    assert fa._load_batch("../../etc/passwd") is None
    assert fa._load_batch("abc") is None
    assert fa._load_batch(None) is None
    assert fa._load_batch("") is None


def test_build_tape_ignores_stale_batch(tmp_path, monkeypatch):
    now = _ts()
    batch = _latest_batch(now)
    batch["stashed_at"] = now - 500  # older than the 90s lookback
    p = tmp_path / "latest.json"
    p.write_text(json.dumps(batch))
    hook = tmp_path / "hook.json"
    hook.write_text(json.dumps(_hook_state(now)))
    monkeypatch.setattr(fa, "LATEST_FLIPS_PATH", str(p))
    monkeypatch.setattr(fa, "HOOK_STATE_PATH", str(hook))
    lines, dirs, _bid, _fl = fa.build_tape(now)
    # falls back to histories: ETH leg only
    assert any("ETH" in l for l in lines)
    assert "XRP" not in " ".join(lines)


def test_build_tape_bad_tick_guard(tmp_path, monkeypatch):
    now = _ts()
    state = {"tickers": {
        "KXETH15M-X": {
            # 44c single-leg print: stale last_price, not flow
            "hist": [[now - 57, 0.485], [now, 0.045]],
            "last_alert_ts": now - 5, "last_alert_dir": "down"},
    }}
    p = tmp_path / "hook.json"
    p.write_text(json.dumps(state))
    monkeypatch.setattr(fa, "LATEST_FLIPS_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(fa, "HOOK_STATE_PATH", str(p))
    lines, dirs, _bid, _fl = fa.build_tape(now)
    assert lines == [] and dirs == {}


# ---------------------------------------------------------------- positions

def _scan_state(now):
    return {"alerted": {
        "KXETH15M-X:YES": {"side": "YES", "ticker": "KXETH15M-X",
                           "entry_price_taken": 0.52,
                           "window_close_utc": now + 600},
        "KXBTC15M-X:NO": {"side": "NO", "ticker": "KXBTC15M-X",
                          "window_close_utc": now + 600,
                          "exit_ts": now - 10},
        "KXSOL15M-X:YES": {"side": "YES", "ticker": "KXSOL15M-X",
                           "window_close_utc": now - 10},
        "crossvenue:key": {"pair": "x"},
        "KXXRP15M-X:YES": "2026-09-29T21:06:59+00:00",
    }}


def test_get_open_positions_filters(tmp_path, monkeypatch):
    now = _ts()
    p = tmp_path / "scan.json"
    p.write_text(json.dumps(_scan_state(now)))
    monkeypatch.setattr(fa, "SCAN_STATE_PATH", str(p))
    pos = fa.get_open_positions(now)
    assert len(pos) == 1
    assert pos[0]["coin"] == "ETH" and pos[0]["side"] == "YES"
    assert pos[0]["entry_price_taken"] == 0.52


# ---------------------------------------------------------------- prompt

def test_prompt_has_call_contract_and_conviction():
    msgs = fa.render_messages(["SOL DOWN 73.0c->54.0c in 28s"], [], {}, None)
    blob = " ".join(m["content"] for m in msgs)
    assert "CALL:" in blob and "BUY_YES | BUY_NO | CLOSE | HOLD | SKIP" in blob
    assert "MODEL+TAPE" in blob and "TAPE-ONLY" in blob
    assert "Never invent an edge number" in blob


def test_prompt_includes_positions_and_model_snap():
    msgs = fa.render_messages(
        ["ETH UP 45.0c->53.0c in 12s"],
        [{"coin": "ETH", "side": "NO", "entry_price_taken": 0.585,
          "ticker": "KXETH15M-X"}],
        {"ETH": {"fair": 0.55, "edge": 0.03, "has_signal": False}}, 42.0)
    user = msgs[1]["content"]
    assert "ETH NO" in user and "58.5c" in user
    assert "fair 55c" in user and "edge +3c" in user


# ---------------------------------------------------------------- end to end

class _OkBackend:
    def complete(self, messages, timeout_s=4.0):
        return ("CALL: BUY_NO SOL - TAPE-ONLY\n"
                "REASON: 19c flush, no model edge\n"
                "WATCH: reclaim of 65c",
                1234)


class _BadBackend:
    def complete(self, messages, timeout_s=4.0):
        raise RuntimeError("boom")


def test_analyze_flips_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FLIP_ANALYST_ENABLED", raising=False)
    assert fa.analyze_flips(backend=_OkBackend()) is None


def test_analyze_flips_never_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("FLIP_ANALYST_ENABLED", "1")
    monkeypatch.setattr(fa, "STATE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(fa, "LOG_PATH", str(tmp_path / "l.jsonl"))
    # empty tape -> gated, no backend call, no raise
    assert fa.analyze_flips(backend=_BadBackend()) is None


def test_analyze_flips_happy_path(monkeypatch, tmp_path):
    now = _ts()
    hook = tmp_path / "hook.json"
    hook.write_text(json.dumps(_hook_state(now)))
    monkeypatch.setattr(fa, "HOOK_STATE_PATH", str(hook))
    monkeypatch.setattr(fa, "STATE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(fa, "LOG_PATH", str(tmp_path / "l.jsonl"))
    monkeypatch.setenv("FLIP_ANALYST_ENABLED", "1")
    out = fa.analyze_flips(backend=_OkBackend(), now_ts=now)
    assert out["call"] == "BUY_NO"
    assert out["coin"] == "SOL"
    assert out["conviction"] == "TAPE-ONLY"
    assert out["reason"] == "19c flush, no model edge"
    assert out["watch"] == "reclaim of 65c"
    assert out["latency_ms"] == 1234
    # log written
    assert (tmp_path / "l.jsonl").read_text().strip() != ""


def test_analyze_flips_kill_switch(monkeypatch, tmp_path):
    kill = tmp_path / "kill"
    kill.write_text("")
    monkeypatch.setattr(fa, "KILL_SWITCH_PATH", str(kill))
    monkeypatch.setenv("FLIP_ANALYST_ENABLED", "1")
    assert fa.analyze_flips(backend=_OkBackend()) is None


# ---------------------------------------------------------------- batch idempotency + per-coin memory

def _batch_file(tmp_path, now, flips, batch_id):
    p = tmp_path / ("batch_%s.json" % batch_id)
    p.write_text(json.dumps({
        "stashed_at": now, "batch_id": batch_id, "flips": flips}))
    return p


def _flip(coin, direction, frm=0.55, to=0.60):
    return {"coin": coin, "direction": direction, "from_mid": frm,
            "to_mid": to, "span_s": 12, "crossed_50": False,
            "mins_left": 10.0}


class _CountingBackend:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, timeout_s=4.0):
        self.calls += 1
        return ("CALL: HOLD DOGE - TAPE-ONLY\nREASON: r%d\nWATCH: w"
                % self.calls, 100)


def _analyst_env(monkeypatch, tmp_path):
    monkeypatch.setattr(fa, "LATEST_FLIPS_PATH",
                        str(tmp_path / "missing.json"))
    monkeypatch.setattr(fa, "STATE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(fa, "LOG_PATH", str(tmp_path / "l.jsonl"))
    monkeypatch.setenv("FLIP_ANALYST_ENABLED", "1")


def test_same_batch_id_returns_cached_without_recall(monkeypatch, tmp_path):
    _analyst_env(monkeypatch, tmp_path)
    now = _ts()
    monkeypatch.setattr(
        fa, "LATEST_FLIPS_PATH",
        str(_batch_file(tmp_path, now, [_flip("DOGE", "toward YES")], "b1")))
    be = _CountingBackend()
    first = fa.analyze_flips(backend=be, now_ts=now)
    assert first["reason"] == "r1" and be.calls == 1
    # same batch seen again (second runner): cached, no new model call
    second = fa.analyze_flips(backend=be, now_ts=now + 5)
    assert second["reason"] == "r1" and be.calls == 1


def test_new_batch_after_cooldown_fires_again(monkeypatch, tmp_path):
    _analyst_env(monkeypatch, tmp_path)
    now = _ts()
    monkeypatch.setattr(
        fa, "LATEST_FLIPS_PATH",
        str(_batch_file(tmp_path, now, [_flip("DOGE", "toward YES")], "b1")))
    be = _CountingBackend()
    assert fa.analyze_flips(backend=be, now_ts=now)["reason"] == "r1"
    # different batch, cooldown elapsed -> fires again
    monkeypatch.setattr(
        fa, "LATEST_FLIPS_PATH",
        str(_batch_file(tmp_path, now + 200, [_flip("SOL", "toward YES")],
                        "b2")))
    out = fa.analyze_flips(backend=be, now_ts=now + 200)
    assert out["reason"] == "r2" and be.calls == 2


def test_per_coin_dirs_merge_and_reversal_survives_other_batches(
        monkeypatch, tmp_path):
    _analyst_env(monkeypatch, tmp_path)
    now = _ts()
    be = _CountingBackend()
    # batch 1: DOGE down -> fires (cooldown elapsed)
    monkeypatch.setattr(
        fa, "LATEST_FLIPS_PATH",
        str(_batch_file(tmp_path, now, [_flip("DOGE", "toward NO",
                                             frm=0.60, to=0.55)], "b1")))
    assert fa.analyze_flips(backend=be, now_ts=now) is not None
    # batch 2: NEAR down (single-coin batch must NOT wipe DOGE's memory)
    monkeypatch.setattr(
        fa, "LATEST_FLIPS_PATH",
        str(_batch_file(tmp_path, now + 200, [_flip("NEAR", "toward NO",
                                                   frm=0.60, to=0.55)],
                        "b2")))
    assert fa.analyze_flips(backend=be, now_ts=now + 200) is not None
    # batch 3: DOGE reverses up within cooldown -> bypasses via reversal
    monkeypatch.setattr(
        fa, "LATEST_FLIPS_PATH",
        str(_batch_file(tmp_path, now + 250, [_flip("DOGE", "toward YES")],
                        "b3")))
    out = fa.analyze_flips(backend=be, now_ts=now + 250)
    assert out is not None and out["reason"] == "r3"


def test_stale_dir_does_not_trigger_reversal(monkeypatch, tmp_path):
    _analyst_env(monkeypatch, tmp_path)
    now = _ts()
    state = {"last_read_ts": now - 10,
             "dirs": {"DOGE": {"dir": "down", "ts": now - 900}}}
    sp = tmp_path / "s.json"
    sp.write_text(json.dumps(state))
    ok, reason = fa.should_analyze({"DOGE": "up"},
                                   json.loads(sp.read_text()), now)
    assert ok is False and reason == "cooling_down"


# ---------------------------------------------------------------- model snapshot

def test_get_model_snap_fresh(tmp_path, monkeypatch):
    now = _ts()
    p = tmp_path / "fair.json"
    p.write_text(json.dumps({
        "as_of_ts": now - 30,
        "coins": {"ETH": {"fair": 0.55, "edge": 0.06,
                          "has_signal": True}}}))
    monkeypatch.setattr(fa, "FAIR_SNAP_PATH", str(p))
    coins, age = fa.get_model_snap(now)
    assert coins["ETH"]["edge"] == 0.06
    assert 29 <= age <= 31


def test_get_model_snap_stale(tmp_path, monkeypatch):
    now = _ts()
    p = tmp_path / "fair.json"
    p.write_text(json.dumps({"as_of_ts": now - 500, "coins": {"ETH": {}}}))
    monkeypatch.setattr(fa, "FAIR_SNAP_PATH", str(p))
    assert fa.get_model_snap(now) == ({}, None)


def test_get_model_snap_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(fa, "FAIR_SNAP_PATH", str(tmp_path / "nope.json"))
    assert fa.get_model_snap(_ts()) == ({}, None)


def test_parse_call_defaults_to_tape_only():
    a, c, v, _, _ = fa.parse_call(
        "CALL: SKIP BTC\nREASON: nothing\nWATCH: nothing")
    # never claim model backing without evidence
    assert (a, c, v) == ("SKIP", "BTC", "TAPE-ONLY")


def test_call_log_has_gradeable_fields(monkeypatch, tmp_path):
    _analyst_env(monkeypatch, tmp_path)
    now = _ts()
    flips = [{**_flip("SOL", "toward NO", frm=0.73, to=0.545),
              "ticker": "KXSOL15M-26OCT021545-45",
              "close_time": "2026-10-02T19:45:00Z"}]
    monkeypatch.setattr(
        fa, "LATEST_FLIPS_PATH",
        str(_batch_file(tmp_path, now, flips, "grade1")))

    class _CallBackend:
        def complete(self, messages, timeout_s=4.0):
            return ("CALL: BUY_NO SOL - MODEL+TAPE\nREASON: dump\nWATCH: w",
                    100)

    out = fa.analyze_flips(backend=_CallBackend(), now_ts=now)
    assert out["call"] == "BUY_NO"
    entry = json.loads((tmp_path / "l.jsonl").read_text().strip())
    assert entry["call"] == "BUY_NO"
    assert entry["call_coin"] == "SOL"
    assert entry["call_ticker"] == "KXSOL15M-26OCT021545-45"
    assert entry["call_window_close"] == "2026-10-02T19:45:00Z"
    assert entry["conviction"] == "MODEL+TAPE"


def test_backend_error_backoff_gates_recent_failures(monkeypatch, tmp_path):
    # A recent full-chain backend error gates for BACKEND_ERROR_BACKOFF_S:
    # hammering dead backends on every batch burned the keyed quotas
    # overnight on 2026-10-06. The lane self-recovers after the backoff.
    _analyst_env(monkeypatch, tmp_path)
    now = _ts()
    monkeypatch.setattr(
        fa, "LATEST_FLIPS_PATH",
        str(_batch_file(tmp_path, now, [_flip("DOGE", "toward YES")], "b1")))
    (tmp_path / "s.json").write_text(
        json.dumps({"last_backend_error_ts": now - 60}))
    be = _CountingBackend()
    out = fa.analyze_flips(backend=be, now_ts=now)
    assert out is None and be.calls == 0


def test_backend_backoff_expires(monkeypatch, tmp_path):
    # Once the backoff window passes, the analyst tries the backend again.
    _analyst_env(monkeypatch, tmp_path)
    now = _ts()
    monkeypatch.setattr(
        fa, "LATEST_FLIPS_PATH",
        str(_batch_file(tmp_path, now, [_flip("DOGE", "toward YES")], "b1")))
    (tmp_path / "s.json").write_text(
        json.dumps({"last_backend_error_ts": now - fa.BACKEND_ERROR_BACKOFF_S - 1}))
    be = _CountingBackend()
    out = fa.analyze_flips(backend=be, now_ts=now)
    assert out is not None and be.calls == 1


class _CoinlessBackend:
    # Mimics a fallback model that returns a valid CALL line but omits
    # the coin token (seen live on Pollinations 2026-10-06).
    def complete(self, messages, timeout_s=4.0):
        return ("CALL: BUY_YES TAPE-ONLY\nREASON: tape surged\nWATCH: a drop",
                100)


def test_single_coin_batch_attributes_coinless_call(monkeypatch, tmp_path):
    _analyst_env(monkeypatch, tmp_path)
    now = _ts()
    monkeypatch.setattr(
        fa, "LATEST_FLIPS_PATH",
        str(_batch_file(tmp_path, now, [_flip("SOL", "toward YES")], "b1")))
    out = fa.analyze_flips(backend=_CoinlessBackend(), now_ts=now)
    assert out is not None and out["coin"] == "SOL"


def test_multi_coin_batch_leaves_coinless_call_unattributed(monkeypatch,
                                                           tmp_path):
    _analyst_env(monkeypatch, tmp_path)
    now = _ts()
    monkeypatch.setattr(
        fa, "LATEST_FLIPS_PATH",
        str(_batch_file(tmp_path, now,
                        [_flip("SOL", "toward YES"),
                         _flip("ETH", "toward YES")], "b1")))
    out = fa.analyze_flips(backend=_CoinlessBackend(), now_ts=now)
    assert out is not None and out["coin"] is None
