"""Iter-11 A/B decision-neutrality replay.

Pre-patch module (vendored iter-11 pre-patch backup at
tests/fixtures/pre_patch_iter11_same_day_loop.py, md5
c084867bb89062987a4857dafe3a4adf) vs the patched scripts/same_day_loop.py,
frozen clock, multi-scenario fixture:

  W1 YES fire   (BTC 01:00-01:15 ET, edge +0.08, agrees) -> new signal
  W2 contrarian (ETH 01:00-01:15 ET, edge +0.06 post-shrink, disagrees) -> drop
  W3 no-edge    (SOL 01:00-01:15 ET, edge +0.015) -> skipped
  W4 warmup     (XRP 01:09-01:24 ET, 60 s elapsed) -> skipped
  legacy string entry, closed window -> graded YES/correct
  seeded open XRP signal -> exit fires (edge_reversed)

Two runs per module: mark=True (full scan) and --no-mark. Printed payloads,
state (+.bak) and grades must match modulo the documented new metadata keys
-> 0 divergence. --no-mark state files must be byte-identical.
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

PRE_PATH = os.path.join(os.path.dirname(__file__), "fixtures",
                        "pre_patch_iter11_same_day_loop.py")
POST_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts",
                         "same_day_loop.py")

# New additive metadata keys, allowed to differ (must be the ONLY diffs).
# iter-12 D1a added p_now_candle / p_now_candle_ts / p_now_ts /
# spot_age_candle_s (all None on legacy string entries); same shape-pinning
# contract, extended key set. iter-16 added volume (E.2) and
# cluster_rank_at_entry / cluster_size_at_entry to state entries + grade
# rows; the printed signal's "volume" predates both modules and is pinned
# separately below.
NEW_KEYS = {"jump_flag", "jump_stat", "bid", "ask",
            "pnl_1usd_stake_gross", "pnl_1usd_stake_net",
            "p_now_candle", "p_now_candle_ts", "p_now_ts", "spot_age_candle_s",
            "volume", "cluster_rank_at_entry", "cluster_size_at_entry",
            # iter-19 R1 additive reporting keys (matched-denominator
            # companions): reporting only, never read by any decision.
            "no_mid_rows", "win_rate_priced",
            # Prioritized-improvements (2026-10-04, Codex): shadow/log-only
            # additive keys. #1 shadow calibration on state entries + grade
            # rows; #3 quote-side exit prices on exit records; #4 XRP
            # suppression shadow flag on state entries + grade rows; #6
            # price-aware accuracy reporting keys. Reporting/logging only,
            # never read by any entry/exit/grading decision.
            "fair_calibrated_taken", "xrp_shadow_suppressed",
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
    if not os.path.exists(path):
        pytest.skip(f"module not present: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k != "kalshi_requests" and k not in NEW_KEYS}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def _count_diffs(a, b):
    """Leaf-level divergence count between two JSON-shaped structures."""
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
    _m("KXETH15M-26OCT020115-00", NOW_TS - 600, NOW_TS + 300, "0.385", "0.415"),
    _m("KXSOL15M-26OCT020115-00", NOW_TS - 600, NOW_TS + 300, "0.535", "0.565"),
    _m("KXXRP15M-26OCT020124-00", NOW_TS - 60, NOW_TS + 840, "0.585", "0.615"),
    _m("KXXRP15M-26OCT020115-00", NOW_TS - 600, NOW_TS + 300, "0.49", "0.51"),
]
FAIR = {"XBTUSD": 0.68, "ETHUSD": 0.48, "SOLUSD": 0.57, "XRPUSD": 0.44}
SETTLE = {"KXBTC15M-26OCT020045-00": "YES"}


def _candles():
    out, px = [], 100.0
    for i, r in enumerate([0.0] + [0.0003] * 8 + [0.025, 0.0003, -0.0002]):
        if i:
            px *= math.exp(r)
        out.append((NOW_TS - 660 + 60 * i, px))
    return out


CANDLES = _candles()


def _fake_fair(pair, w_open, w_close, now_ts, meta=None):
    if meta is not None:
        meta["candles"] = CANDLES
        meta["trades_fetch_ts"] = float(now_ts)
        meta["trades_rows"] = 500
    return (FAIR[pair], 100.0, 100.5, 0.0004, 5.0, float(now_ts - 5))


SEED = {"alerted": {
    "KXBTC15M-26OCT020045-00:YES": "KXBTC15M-26OCT020045-00:YES",
    "KXXRP15M-26OCT020115-00:YES": {
        "ts": "2026-10-02T05:00:00+00:00",
        "ticker": "KXXRP15M-26OCT020115-00", "side": "YES",
        "edge": 0.06, "market_mid": 0.55,
        "window_open_utc": NOW_TS - 600, "window_close_utc": NOW_TS + 300},
}}


def _run_case(mod, datadir, argv, monkeypatch):
    datadir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "DATA", str(datadir))
    monkeypatch.setattr(mod, "STATE_PATH", str(datadir / "state.json"))
    monkeypatch.setattr(mod, "GRADE_PATH", str(datadir / "grades.jsonl"))
    (datadir / "state.json").write_text(json.dumps(SEED))
    monkeypatch.setattr(mod, "datetime", _FrozenDateTime)
    monkeypatch.setattr(mod, "kalshi_same_day",
                        lambda today: [dict(m) for m in MARKETS])
    monkeypatch.setattr(mod, "poly_same_day", lambda today: [])
    monkeypatch.setattr(mod, "fair_15m", _fake_fair)
    monkeypatch.setattr(mod, "entry_ob_imbalance", lambda t: 0.12)
    monkeypatch.setattr(mod, "kalshi_settlement", lambda t: SETTLE.get(t))
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py"] + argv)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    mod.main()
    payload = json.loads(buf.getvalue())
    state = json.loads((datadir / "state.json").read_text())
    bak_p = datadir / "state.json.bak"
    bak = json.loads(bak_p.read_text()) if bak_p.exists() else None
    gp = datadir / "grades.jsonl"
    grades = [json.loads(l) for l in gp.read_text().splitlines()] if gp.exists() else []
    shadow = datadir / "contrarian_drops.jsonl"
    return {
        "payload": payload, "state": state, "bak": bak, "grades": grades,
        "shadow_exists": shadow.exists(),
        "shadow_lines": (shadow.read_text().splitlines()
                         if shadow.exists() else []),
        "state_bytes": (datadir / "state.json").read_bytes(),
    }


@pytest.fixture(scope="module")
def mods():
    return _load("sdl11_pre", PRE_PATH), _load("sdl11_post", POST_PATH)


def test_ab_mark_run_zero_divergence(mods, tmp_path, monkeypatch):
    pre, post = mods
    r_pre = _run_case(pre, tmp_path / "pre", [], monkeypatch)
    r_post = _run_case(post, tmp_path / "post", [], monkeypatch)
    div = {}
    for k in ("payload", "state", "grades"):
        div[k] = _count_diffs(_strip(r_pre[k]), _strip(r_post[k]))
    div["bak"] = _count_diffs(_strip(r_pre["bak"]), _strip(r_post["bak"])) \
        if r_pre["bak"] is not None else (0 if r_post["bak"] is None else 1)
    assert div == {"payload": 0, "state": 0, "bak": 0, "grades": 0}, div
    # scenario sanity: the fixture actually exercised every path
    assert r_post["payload"]["signals_new"] != []
    assert any(s["ticker"] == "KXBTC15M-26OCT020115-00"
               for s in r_post["payload"]["new_signals"])
    assert r_post["payload"]["exits_new"] == 1
    assert r_post["payload"]["exits"][0]["reason"] == "edge_reversed"
    assert r_post["payload"]["graded_new"] == 1
    assert r_post["payload"]["new_grades"][0]["correct"] is True
    # positive checks: new metadata present ONLY in the patched module
    rec = r_post["state"]["alerted"]["KXBTC15M-26OCT020115-00:YES"]
    assert rec["jump_flag"] is True and rec["jump_stat"] > 4.0
    assert rec["bid"] == 0.585 and rec["ask"] == 0.615
    assert "jump_flag" not in r_pre["state"]["alerted"]["KXBTC15M-26OCT020115-00:YES"]
    # printed signal volume is not new: identical pre/post; the state entry
    # now persists that same value (iter-16 E.2)
    vol = lambda r: [s["volume"] for s in r["payload"]["new_signals"]]
    assert vol(r_pre) == vol(r_post) and vol(r_post)
    sig = next(s for s in r_post["payload"]["new_signals"]
               if s["ticker"] == "KXBTC15M-26OCT020115-00")
    assert rec["volume"] == sig["volume"] == 1000.0
    assert "volume" not in r_pre["state"]["alerted"]["KXBTC15M-26OCT020115-00:YES"]
    assert r_post["shadow_exists"] is True
    assert len(r_post["shadow_lines"]) == 1  # the W2 contrarian drop
    assert json.loads(r_post["shadow_lines"][0])["ticker"] == \
        "KXETH15M-26OCT020115-00"
    assert r_pre["shadow_exists"] is False
    assert "pnl_1usd_stake_gross" in r_post["payload"]["accuracy"]
    assert "pnl_1usd_stake_gross" not in r_pre["payload"]["accuracy"]


def test_ab_no_mark_zero_divergence_and_byte_identical_state(
        mods, tmp_path, monkeypatch):
    pre, post = mods
    r_pre = _run_case(pre, tmp_path / "pre_nm", ["--no-mark"], monkeypatch)
    r_post = _run_case(post, tmp_path / "post_nm", ["--no-mark"], monkeypatch)
    assert _count_diffs(_strip(r_pre["payload"]),
                        _strip(r_post["payload"])) == 0
    assert r_pre["state_bytes"] == r_post["state_bytes"]  # byte-identical
    assert r_pre["grades"] == r_post["grades"] == []
    assert r_pre["shadow_exists"] is False and r_post["shadow_exists"] is False
    # the would-be signal is still reported (diagnostic), just not marked
    assert any(s["ticker"] == "KXBTC15M-26OCT020115-00"
               for s in r_post["payload"]["new_signals"])
