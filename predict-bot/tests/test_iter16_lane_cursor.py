"""Iter-16 lane-cursor: three decision-neutral changes.

E.2  `volume` persisted on the alert-time state entry (same value as the
     printed signal's "volume") and copied to the grade row.
CR   `cluster_rank_at_entry` / `cluster_size_at_entry`: the entry's 1-based
     ticker-ascending rank among the run's pending 15m entries sharing its
     window_close_utc, plus the group size. Stamped in the lock-free scan
     phase, copied to the grade row, never read by any decision.
FX   pre-patch A/B baselines vendored under tests/fixtures/ so the suite is
     hermetic (no reads outside the repo).
"""
import hashlib
import importlib.util
import io
import json
import os
import sys
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import same_day_loop as sdl  # noqa: E402

FROZEN_NOW = datetime(2026, 10, 2, 5, 10, 0, tzinfo=timezone.utc)
NOW_TS = int(FROZEN_NOW.timestamp())


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW if tz is None else FROZEN_NOW.astimezone(tz)


def _m(ticker, wo, wc, bid, ask, volume, title="up?"):
    return {
        "ticker": ticker, "title": title,
        "open_time": datetime.fromtimestamp(wo, timezone.utc).isoformat(),
        "close_time": datetime.fromtimestamp(wc, timezone.utc).isoformat(),
        "yes_bid_dollars": bid, "yes_ask_dollars": ask,
        "volume_fp": volume, "floor_strike": "1",
    }


A_WO, A_WC = NOW_TS - 600, NOW_TS + 300   # window A: 3 concurrent signals
B_WO, B_WC = NOW_TS - 300, NOW_TS + 600   # window B: 1 signal
BTC, ETH, SOL = ("KXBTC15M-26OCT020115-00", "KXETH15M-26OCT020115-00",
                 "KXSOL15M-26OCT020115-00")
DOGE = "KXDOGE15M-26OCT020120-00"
RAIN = "KXRAINNYC-26OCT02-T1"

# Listed out of ticker order on purpose: rank must follow ticker order,
# not scan order. mid 0.60, fair 0.68 (outside the shrink band) -> YES,
# edge +0.08, agrees with the lean.
MARKETS = [
    _m(SOL, A_WO, A_WC, "0.585", "0.615", "3000"),
    _m(BTC, A_WO, A_WC, "0.585", "0.615", "1234.5"),
    _m(DOGE, B_WO, B_WC, "0.585", "0.615", "777"),
    _m(ETH, A_WO, A_WC, "0.585", "0.615", "2000"),
    # cross-venue only (no "15M"): kalshi 0.32 vs poly 0.50 -> xvenue signal
    {"ticker": RAIN, "title": "NYC rain today above 1 inch",
     "yes_bid_dollars": "0.30", "yes_ask_dollars": "0.34", "volume_fp": "900"},
]
POLY = [{"event": "NYC rain today", "question": "above 1 inch",
         "yes": 0.50, "volume": 10000}]
FAIR = {"XBTUSD": 0.68, "ETHUSD": 0.68, "SOLUSD": 0.68, "DOGEUSD": 0.68}


def _fake_fair(pair, w_open, w_close, now_ts, meta=None):
    if meta is not None:
        meta["trades_fetch_ts"] = float(now_ts)
        meta["trades_rows"] = 500
    return (FAIR[pair], 100.0, 100.5, 0.0004, 5.0, float(now_ts - 5))


def _run(datadir, argv, monkeypatch, seed=None, markets=MARKETS):
    datadir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sdl, "DATA", str(datadir))
    monkeypatch.setattr(sdl, "STATE_PATH", str(datadir / "state.json"))
    monkeypatch.setattr(sdl, "GRADE_PATH", str(datadir / "grades.jsonl"))
    (datadir / "state.json").write_text(json.dumps(seed or {"alerted": {}}))
    monkeypatch.setattr(sdl, "datetime", _FrozenDateTime)
    monkeypatch.setattr(sdl, "kalshi_same_day",
                        lambda today: [dict(m) for m in markets])
    monkeypatch.setattr(sdl, "poly_same_day", lambda today: [dict(p) for p in POLY])
    monkeypatch.setattr(sdl, "fair_15m", _fake_fair)
    monkeypatch.setattr(sdl, "entry_ob_imbalance", lambda t: 0.12)
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda t: "YES")
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py"] + argv)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    sdl.main()
    gp = datadir / "grades.jsonl"
    return {
        "payload": json.loads(buf.getvalue()),
        "state": json.loads((datadir / "state.json").read_text()),
        "state_bytes": (datadir / "state.json").read_bytes(),
        "grades": ([json.loads(l) for l in gp.read_text().splitlines()]
                   if gp.exists() else []),
    }


def _sig(payload, ticker):
    return next(s for s in payload["new_signals"] if s["ticker"] == ticker)


# ---------- E.2: volume ----------

def test_entry_volume_equals_alert_signal_volume(tmp_path, monkeypatch):
    r = _run(tmp_path / "v", [], monkeypatch)
    alerted = r["state"]["alerted"]
    for tk, expect in ((BTC, 1234.5), (ETH, 2000.0), (SOL, 3000.0), (DOGE, 777.0)):
        rec = alerted[f"{tk}:YES"]
        assert rec["volume"] == _sig(r["payload"], tk)["volume"] == expect
    # xvenue entries keep their minimal record shape
    assert alerted[f"xvenue:{RAIN}"] == {"ts": FROZEN_NOW.isoformat(),
                                         "ticker": RAIN}


def test_grade_row_copies_volume_and_cluster_fields(tmp_path, monkeypatch):
    closed = "KXBTC15M-26OCT020045-00"
    seed = {"alerted": {
        f"{closed}:YES": {
            "ts": "2026-10-02T04:40:00+00:00", "ticker": closed, "side": "YES",
            "edge": 0.06, "market_mid": 0.60,
            "window_open_utc": NOW_TS - 1500, "window_close_utc": NOW_TS - 600,
            "volume": 4321.0,
            "cluster_rank_at_entry": 2, "cluster_size_at_entry": 3,
        },
        # legacy string entry: graded, all iter-16 fields None
        "KXETH15M-26OCT020045-00:NO": "KXETH15M-26OCT020045-00:NO",
    }}
    r = _run(tmp_path / "g", [], monkeypatch, seed=seed, markets=[])
    rows = {g["key"]: g for g in r["grades"]}
    row = rows[f"{closed}:YES"]
    assert row["correct"] is True
    assert row["volume"] == 4321.0
    assert row["cluster_rank_at_entry"] == 2
    assert row["cluster_size_at_entry"] == 3
    legacy = rows["KXETH15M-26OCT020045-00:NO"]
    for k in ("volume", "cluster_rank_at_entry", "cluster_size_at_entry"):
        assert k in legacy and legacy[k] is None


def test_grade_row_none_on_dict_entry_without_new_fields(tmp_path, monkeypatch):
    closed = "KXBTC15M-26OCT020045-00"
    seed = {"alerted": {f"{closed}:YES": {
        "ts": "2026-10-02T04:40:00+00:00", "ticker": closed, "side": "YES",
        "edge": 0.06, "market_mid": 0.60,
        "window_open_utc": NOW_TS - 1500, "window_close_utc": NOW_TS - 600,
    }}}
    r = _run(tmp_path / "g0", [], monkeypatch, seed=seed, markets=[])
    row = r["grades"][0]
    assert row["volume"] is None
    assert row["cluster_rank_at_entry"] is None
    assert row["cluster_size_at_entry"] is None


# ---------- cluster_rank_at_entry ----------

def test_cluster_rank_by_ticker_within_window(tmp_path, monkeypatch):
    r = _run(tmp_path / "c", [], monkeypatch)
    alerted = r["state"]["alerted"]
    ranks = {tk: (alerted[f"{tk}:YES"]["cluster_rank_at_entry"],
                  alerted[f"{tk}:YES"]["cluster_size_at_entry"])
             for tk in (BTC, ETH, SOL, DOGE)}
    assert ranks == {BTC: (1, 3), ETH: (2, 3), SOL: (3, 3), DOGE: (1, 1)}
    # xvenue entry carries no window -> not annotated (reads as None)
    xv = alerted[f"xvenue:{RAIN}"]
    assert xv.get("cluster_rank_at_entry") is None
    assert xv.get("cluster_size_at_entry") is None
    # never surfaced in the printed payload
    for s in r["payload"]["new_signals"]:
        assert "cluster_rank_at_entry" not in s
        assert "cluster_size_at_entry" not in s
    assert r["payload"]["signals_new"] == 5  # 4 x 15m + 1 xvenue


def test_annotate_cluster_rank_unit():
    recs = [
        ("k1", {}, {"ticker": "B", "window_close_utc": 100}),
        ("k2", {}, {"ticker": "A", "window_close_utc": 100}),
        ("k3", {}, {"ticker": "C", "window_close_utc": 200}),
        ("xvenue:X", {}, {"ts": "t", "ticker": "X"}),
        ("k4", {}, {"ticker": "D", "window_close_utc": "100"}),  # not int
        ("k5", {}, {"ticker": "E", "window_close_utc": True}),   # bool != int
    ]
    sigs_before = [dict(s) for _, s, _ in recs]
    sdl.annotate_cluster_rank(recs)
    by_key = {k: rec for k, _, rec in recs}
    assert (by_key["k2"]["cluster_rank_at_entry"],
            by_key["k1"]["cluster_rank_at_entry"]) == (1, 2)
    assert by_key["k1"]["cluster_size_at_entry"] == 2
    assert (by_key["k3"]["cluster_rank_at_entry"],
            by_key["k3"]["cluster_size_at_entry"]) == (1, 1)
    for k in ("xvenue:X", "k4", "k5"):
        assert "cluster_rank_at_entry" not in by_key[k]
        assert "cluster_size_at_entry" not in by_key[k]
    assert [s for _, s, _ in recs] == sigs_before  # sig dicts untouched
    sdl.annotate_cluster_rank([])  # no-op, no raise


def _garbage_annotate(pending_entries):
    for _k, _s, rec in pending_entries:
        rec["cluster_rank_at_entry"] = -999
        rec["cluster_size_at_entry"] = 1e9


@pytest.mark.parametrize("variant", ["absent", "garbage"])
def test_decisions_independent_of_cluster_rank(variant, tmp_path, monkeypatch):
    """Ranks present, absent, or absurd: no decision output may move."""
    real = _run(tmp_path / "real", [], monkeypatch)
    monkeypatch.setattr(sdl, "annotate_cluster_rank",
                        (lambda pe: None) if variant == "absent"
                        else _garbage_annotate)
    alt = _run(tmp_path / variant, [], monkeypatch)
    pr, pa = real["payload"], alt["payload"]
    assert pr["signals_new"] == pa["signals_new"] == 5
    for k in ("signals_total", "new_signals", "exits_new", "exits",
              "graded_new", "new_grades"):
        assert pr[k] == pa[k], k
    ar, aa = real["state"]["alerted"], alt["state"]["alerted"]
    assert set(ar) == set(aa)
    strip = {"cluster_rank_at_entry", "cluster_size_at_entry"}
    for key in ar:
        for k in ("side", "edge", "market_mid"):
            assert ar[key].get(k) == aa[key].get(k)
        assert ({k: v for k, v in ar[key].items() if k not in strip}
                == {k: v for k, v in aa[key].items() if k not in strip})
    rec = aa[f"{BTC}:YES"]
    if variant == "absent":
        assert "cluster_rank_at_entry" not in rec
    else:
        assert rec["cluster_rank_at_entry"] == -999


def test_no_mark_writes_nothing_new(tmp_path, monkeypatch):
    seed = {"alerted": {}}
    r = _run(tmp_path / "nm", ["--no-mark"], monkeypatch, seed=seed)
    assert r["state_bytes"] == json.dumps(seed).encode()  # byte-identical
    assert r["grades"] == []
    assert not (tmp_path / "nm" / "state.json.bak").exists()
    # the would-be signals are still reported, decisions intact
    assert r["payload"]["signals_new"] == 5
    for s in r["payload"]["new_signals"]:
        assert "cluster_rank_at_entry" not in s
    assert _sig(r["payload"], BTC)["volume"] == 1234.5
    # same decisions as the marking run
    m = _run(tmp_path / "mk", [], monkeypatch)
    assert r["payload"]["new_signals"] == m["payload"]["new_signals"]


def test_no_mark_annotation_sees_no_entries(tmp_path, monkeypatch):
    seen = []
    real = sdl.annotate_cluster_rank
    monkeypatch.setattr(sdl, "annotate_cluster_rank",
                        lambda pe: (seen.append(len(pe)), real(pe)))
    _run(tmp_path / "nm2", ["--no-mark"], monkeypatch)
    assert seen == [0]


# ---------- vendored pre-patch fixtures ----------

ITER11_MD5 = "c084867bb89062987a4857dafe3a4adf"


def test_vendored_iter11_md5():
    p = os.path.join(FIXTURES, "pre_patch_iter11_same_day_loop.py")
    with open(p, "rb") as f:
        assert hashlib.md5(f.read()).hexdigest() == ITER11_MD5


def _load_test_module(fname, modname):
    spec = importlib.util.spec_from_file_location(
        modname, os.path.join(HERE, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("fname,vendored", [
    ("test_iter11_ab.py", "pre_patch_iter11_same_day_loop.py"),
    ("test_iter12_d1a_shadow.py", "pre_patch_iter12_same_day_loop.py"),
])
def test_ab_load_paths_resolve_under_fixtures(fname, vendored):
    mod = _load_test_module(fname, f"_iter16_probe_{fname[:-3]}")
    pre = os.path.realpath(mod.PRE_PATH)
    assert os.path.dirname(pre) == os.path.realpath(FIXTURES)
    assert os.path.basename(pre) == vendored
    assert os.path.isfile(pre)


def test_tests_never_reference_goals_dir():
    hits = []
    for name in os.listdir(HERE):
        if name.endswith(".py") and name != os.path.basename(__file__):
            with open(os.path.join(HERE, name), encoding="utf-8") as f:
                if "workspace/goals" in f.read():
                    hits.append(name)
    assert hits == []
