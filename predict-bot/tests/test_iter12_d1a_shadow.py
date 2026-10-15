"""Iter-12 D1a: p_now staleness shadow (log-only).

Decision-neutral infra: fair_15m's meta gains p_now_candle/p_now_candle_ts
(latest 1-min candle close + its exchange open-ts); the scan records
p_now_candle / p_now_candle_ts / p_now_ts / spot_age_candle_s on the alert-time
state entry; grade_pending copies them to the grade row. Nothing reads these
fields for any entry/exit/grading decision.

A future iteration can replay, on Kalshi-settled rows without refetching:
    p_now' = p_now_candle  iff  p_now_candle_ts > p_now_ts  (else p_now)
    age'   = spot_age_candle_s iff candle newer          (else spot_age_s)
    refuse the row's spot iff age' > 180
and compare Brier vs the live fair. Guard ships only on >=30 Kalshi-settled
full-page rows showing a Brier gain.
"""
import importlib.util
import io
import json
import math
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

# Vendored iter-12 pre-patch backup (A/B baseline for the D1a shadow).
PRE_PATH = os.path.join(os.path.dirname(__file__), "fixtures",
                        "pre_patch_iter12_same_day_loop.py")
POST_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts",
                         "same_day_loop.py")

# New additive shadow keys, allowed to differ pre/post (the ONLY diffs).
NEW_KEYS = {"p_now_candle", "p_now_candle_ts", "p_now_ts", "spot_age_candle_s"}
# iter-16 additive keys (state entries + grade rows), also allowed to differ.
# Kept separate from NEW_KEYS: the positive check below pins only D1a.
ITER16_KEYS = {"volume", "cluster_rank_at_entry", "cluster_size_at_entry"}
# iter-19 R1 additive reporting keys (accuracy_stats payload only, appended
# at the END of the dict). Reporting only, never read by any decision.
# Kept separate: the D1a positive check below pins state-entry keys only.
ITER19_KEYS = {"no_mid_rows", "win_rate_priced"}
# Prioritized-improvements (2026-10-04, Codex): shadow/log-only additive
# keys. #1 shadow calibration on state entries + grade rows; #3 quote-side
# exit prices on exit records; #4 XRP suppression shadow flag on state
# entries + grade rows; #6 price-aware accuracy reporting keys. Reporting/
# logging only, never read by any entry/exit/grading decision. Kept
# separate like ITER16_KEYS/ITER19_KEYS.
PRIORITIZED_KEYS = {"fair_calibrated_taken", "xrp_shadow_suppressed",
                    "gate_edge5_pass", "gate_edge7_pass", "gate_edge10_pass",
                    "executable_edge", "gate_executable_edge_pass",
                    "entry_policy_cohort", "signal_key", "entry_ask_taken",
                    "exit_yes_bid", "exit_yes_ask", "exit_bid_taken",
                    "quoted_spread_taken",
                    "mean_calibrated_probability",
                    "mean_paper_mid_payoff", "independent_windows"}

FROZEN_NOW = datetime(2026, 10, 2, 5, 10, 0, tzinfo=timezone.utc)
NOW_TS = int(FROZEN_NOW.timestamp())


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW if tz is None else FROZEN_NOW.astimezone(tz)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


POST = _load("sdl12_post", POST_PATH)


# ---------- fair_15m meta shadow ----------

def _trades(w_open, last_ts):
    rows = [[100.0 + i * 0.01, 1.0, float(w_open - 50 + i), "", "", ""]
            for i in range(10)]
    rows.append([100.5, 2.0, float(last_ts), "", "", ""])
    return rows


def _candles(n=70, last_ts=None, last_close=101.25):
    last_ts = NOW_TS - 45 if last_ts is None else last_ts
    out, px = [], 100.0
    for i in range(n):
        px *= math.exp(0.0003)
        out.append((last_ts - 60 * (n - 1 - i), px))
    out[-1] = (last_ts, last_close)
    return out


def test_meta_candle_shadow_populated(monkeypatch):
    w_open = NOW_TS - 600
    monkeypatch.setattr(POST, "kraken_trades", lambda p, s: _trades(w_open, NOW_TS - 5))
    monkeypatch.setattr(POST, "kraken_candles", lambda p: _candles())
    meta = {}
    res = POST.fair_15m("XBTUSD", w_open, NOW_TS + 300, NOW_TS, meta)
    assert res is not None
    assert meta["p_now_candle"] == 101.25
    assert meta["p_now_candle_ts"] == NOW_TS - 45


def test_meta_candle_shadow_none_on_empty_candles(monkeypatch):
    w_open = NOW_TS - 600
    monkeypatch.setattr(POST, "kraken_trades", lambda p, s: _trades(w_open, NOW_TS - 5))
    monkeypatch.setattr(POST, "kraken_candles", lambda p: [])
    meta = {}
    res = POST.fair_15m("XBTUSD", w_open, NOW_TS + 300, NOW_TS, meta)
    assert res is None  # <70 candles
    assert meta["p_now_candle"] is None
    assert meta["p_now_candle_ts"] is None


# ---------- scan entry + grade row carry-through ----------

def _m(ticker, wo, wc, bid, ask):
    return {
        "ticker": ticker, "title": "up?",
        "open_time": datetime.fromtimestamp(wo, timezone.utc).isoformat(),
        "close_time": datetime.fromtimestamp(wc, timezone.utc).isoformat(),
        "yes_bid_dollars": bid, "yes_ask_dollars": ask,
        "volume_fp": "1000", "floor_strike": "1",
    }


MARKETS = [
    _m("KXBTC15M-26OCT020115-00", NOW_TS - 600, NOW_TS + 300, "0.585", "0.615"),
]
FAIR = {"XBTUSD": 0.68}


def _fake_fair(pair, w_open, w_close, now_ts, meta=None):
    if meta is not None:
        meta["trades_fetch_ts"] = float(now_ts)
        meta["trades_rows"] = 1000
        meta["p_now_candle"] = 101.25
        meta["p_now_candle_ts"] = NOW_TS - 45
    return (FAIR[pair], 100.0, 100.5, 0.0004, 5.0, float(now_ts - 5))


def _run_case(mod, datadir, argv, monkeypatch, seed):
    datadir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "DATA", str(datadir))
    monkeypatch.setattr(mod, "STATE_PATH", str(datadir / "state.json"))
    monkeypatch.setattr(mod, "GRADE_PATH", str(datadir / "grades.jsonl"))
    (datadir / "state.json").write_text(json.dumps(seed))
    monkeypatch.setattr(mod, "datetime", _FrozenDateTime)
    monkeypatch.setattr(mod, "kalshi_same_day", lambda today: [dict(m) for m in MARKETS])
    monkeypatch.setattr(mod, "poly_same_day", lambda today: [])
    monkeypatch.setattr(mod, "fair_15m", _fake_fair)
    monkeypatch.setattr(mod, "entry_ob_imbalance", lambda t: 0.12)
    monkeypatch.setattr(mod, "kalshi_settlement", lambda t: "YES")
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py"] + argv)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    mod.main()
    payload = json.loads(buf.getvalue())
    state = json.loads((datadir / "state.json").read_text())
    gp = datadir / "grades.jsonl"
    grades = [json.loads(l) for l in gp.read_text().splitlines()] if gp.exists() else []
    return {"payload": payload, "state": state, "grades": grades,
            "state_bytes": (datadir / "state.json").read_bytes()}


KEY = "KXBTC15M-26OCT020115-00:YES"


def test_entry_records_candle_shadow(tmp_path, monkeypatch):
    r = _run_case(POST, tmp_path / "e", [], monkeypatch, {"alerted": {}})
    entry = r["state"]["alerted"][KEY]
    assert entry["p_now_candle"] == 101.25
    assert entry["p_now_candle_ts"] == NOW_TS - 45
    assert entry["p_now_ts"] == int(NOW_TS - 5)
    assert entry["spot_age_candle_s"] == 45
    assert r["payload"]["signals_new"] == 1  # decision unchanged


def test_grade_row_copies_candle_shadow(tmp_path, monkeypatch):
    seed = {"alerted": {KEY: {
        "ts": "2026-10-02T05:00:00+00:00",
        "ticker": "KXBTC15M-26OCT020045-00", "side": "YES",
        "edge": 0.06, "market_mid": 0.60,
        "window_open_utc": NOW_TS - 1500, "window_close_utc": NOW_TS - 600,
        "p_now_candle": 101.25, "p_now_candle_ts": NOW_TS - 900,
        "p_now_ts": NOW_TS - 950, "spot_age_candle_s": 300,
    }}}
    # window is closed; kalshi_settlement is mocked to "YES" -> graded
    r = _run_case(POST, tmp_path / "g", [], monkeypatch, seed)
    assert len(r["grades"]) == 1
    row = r["grades"][0]
    assert row["correct"] is True
    assert row["p_now_candle"] == 101.25
    assert row["p_now_candle_ts"] == NOW_TS - 900
    assert row["p_now_ts"] == NOW_TS - 950
    assert row["spot_age_candle_s"] == 300


def _fake_fair_garbage_shadow(pair, w_open, w_close, now_ts, meta=None):
    """Same as _fake_fair but with ABSURD shadow values: if any decision
    read the shadow, outputs would change."""
    if meta is not None:
        meta["trades_fetch_ts"] = float(now_ts)
        meta["trades_rows"] = 1000
        meta["p_now_candle"] = 1e9
        meta["p_now_candle_ts"] = 0
    return (FAIR[pair], 100.0, 100.5, 0.0004, 5.0, float(now_ts - 5))


def test_shadow_never_read_by_decision(tmp_path, monkeypatch):
    """Absurd candle-shadow values must not move any decision output."""
    def run(fake):
        monkeypatch.setattr(POST, "fair_15m", fake)
        datadir = tmp_path / ("g" if fake is _fake_fair else "b")
        datadir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(POST, "DATA", str(datadir))
        monkeypatch.setattr(POST, "STATE_PATH", str(datadir / "state.json"))
        monkeypatch.setattr(POST, "GRADE_PATH", str(datadir / "grades.jsonl"))
        (datadir / "state.json").write_text(json.dumps({"alerted": {}}))
        monkeypatch.setattr(POST, "datetime", _FrozenDateTime)
        monkeypatch.setattr(POST, "kalshi_same_day",
                            lambda today: [dict(m) for m in MARKETS])
        monkeypatch.setattr(POST, "poly_same_day", lambda today: [])
        monkeypatch.setattr(POST, "entry_ob_imbalance", lambda t: 0.12)
        monkeypatch.setattr(POST, "kalshi_settlement", lambda t: "YES")
        monkeypatch.setattr(sys, "argv", ["same_day_loop.py"])
        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf)
        POST.main()
        payload = json.loads(buf.getvalue())
        entry = json.loads((datadir / "state.json").read_text())["alerted"][KEY]
        return payload, entry

    p_good, e_good = run(_fake_fair)
    p_bad, e_bad = run(_fake_fair_garbage_shadow)
    assert p_good["signals_new"] == p_bad["signals_new"] == 1
    for k in ("side", "edge", "market_mid"):
        assert e_good[k] == e_bad[k]
    # shadow values differ (they were recorded) but decisions did not
    assert e_bad["p_now_candle"] == 1e9
    assert e_good["p_now_candle"] == 101.25


# ---------- A/B: pre vs post, 0 divergence modulo NEW_KEYS ----------

def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items()
                if k != "kalshi_requests" and k not in NEW_KEYS and k not in ITER16_KEYS
                and k not in ITER19_KEYS and k not in PRIORITIZED_KEYS}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def _count_diffs(a, b):
    if type(a) is not type(b):
        return 1
    if isinstance(a, dict):
        diff = len(set(a) ^ set(b))
        for k in set(a) & set(b):
            diff += _count_diffs(a[k], b[k])
        return diff
    if isinstance(a, list):
        if len(a) != len(b):
            return 1
        return sum(_count_diffs(x, y) for x, y in zip(a, b))
    return 0 if a == b else 1


@pytest.fixture(scope="module")
def mods():
    return _load("sdl12_pre", PRE_PATH), POST


def test_ab_mark_zero_divergence(mods, tmp_path, monkeypatch):
    pre, post = mods
    r_pre = _run_case(pre, tmp_path / "pre", [], monkeypatch, {"alerted": {}})
    r_post = _run_case(post, tmp_path / "post", [], monkeypatch, {"alerted": {}})
    for part in ("payload", "state", "grades"):
        d = _count_diffs(_strip(r_pre[part]), _strip(r_post[part]))
        assert d == 0, f"divergence in {part}: {d}"
    # positive check: the shadow keys exist post-patch and not pre-patch
    entry = r_post["state"]["alerted"][KEY]
    assert all(k in entry for k in NEW_KEYS)
    assert {k: entry[k] for k in ("gate_edge5_pass", "gate_edge7_pass",
                                    "gate_edge10_pass")} == {
        "gate_edge5_pass": True, "gate_edge7_pass": True,
        "gate_edge10_pass": False}
    entry_pre = r_pre["state"]["alerted"][KEY]
    assert not any(k in entry_pre for k in NEW_KEYS)


def test_ab_no_mark_byte_identical_state(mods, tmp_path, monkeypatch):
    pre, post = mods
    r_pre = _run_case(pre, tmp_path / "pre_nm", ["--no-mark"], monkeypatch, {"alerted": {}})
    r_post = _run_case(post, tmp_path / "post_nm", ["--no-mark"], monkeypatch, {"alerted": {}})
    assert r_pre["state_bytes"] == r_post["state_bytes"]
    d = _count_diffs(_strip(r_pre["payload"]), _strip(r_post["payload"]))
    assert d == 0
