"""Hermetic tests for the Y1 regime gate. Pure stdlib, synthetic journals only."""
import math
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from analysis.y1_regime.regime import (  # noqa: E402
    build_features, evaluate_gate, gate_cold_restart, gate_drawdown,
    gate_half, gate_thin, gate_trail12_pnl, gate_trail24_pnl, split_is_oos,
    benchmark_gate, CANDIDATE_GATES,
)


def synth(rows):
    """rows: list of (type, ts, mint, realized_usd). Entry rows need no pnl."""
    out = []
    for typ, ts, mint, pnl in rows:
        r = {"type": typ, "ts": ts, "mint": mint, "name": "t"}
        if typ == "close":
            r["realized_sol"] = pnl / 115.0 if pnl is not None else None
            r["sol_usd"] = 115.0
            r["chain"] = "solana"
        else:
            r["chain"] = "solana"
        out.append(r)
    return out


def feats(rows):
    entries = [r for r in rows if r["type"] == "entry"]
    closes = sorted([r for r in rows if r["type"] == "close"],
                    key=lambda r: r.get("ts") or "")
    return closes, build_features(entries, closes)


def test_no_lookahead_features_use_only_prior_closes():
    # A huge win closes AFTER entry E2; E2's trailing features must exclude it.
    rows = synth([
        ("entry", "2026-09-24 08:00:00", "m1", None),
        ("close", "2026-09-24 08:05:00", "m1", -5.0),
        ("entry", "2026-09-24 09:00:00", "m2", None),
        ("close", "2026-09-24 09:05:00", "m2", 1000.0),  # future whale, must not leak
    ])
    closes, f = feats(rows)
    # only the prior -5 close may contribute (plus standard cost deductions);
    # the +1000 future whale must not leak in
    assert f[1]["trail_12h_n"] == 1
    assert -10.0 < f[1]["trail_12h_usd"] < 0.0


def test_close_never_in_own_feature_set_on_ts_tie():
    rows = synth([
        ("entry", "2026-09-24 08:00:00", "m1", None),
        ("close", "2026-09-24 08:05:00", "m1", -5.0),
        ("entry", "2026-09-24 08:05:00", "m2", None),  # entry ties close ts
        ("close", "2026-09-24 08:06:00", "m2", -1.0),
    ])
    closes, f = feats(rows)
    # entry m2 at exactly 08:05:00: prior requires ts < entry ts, so the 08:05
    # close of m1 is excluded (strict inequality)
    assert f[1]["trail_12h_n"] == 0
    assert f[1]["trail_12h_usd"] is None


def test_orphan_close_fails_open():
    rows = synth([
        ("close", "2026-09-24 08:05:00", "ghost", -50.0),
    ])
    closes, f = feats(rows)
    assert f[0]["orphan"] is True
    for g in CANDIDATE_GATES:
        assert g(f[0]) is True  # every gate fails open on orphans


def test_first_trade_fails_open_for_pnl_gates():
    rows = synth([
        ("entry", "2026-09-24 08:00:00", "m1", None),
        ("close", "2026-09-24 08:05:00", "m1", -5.0),
    ])
    closes, f = feats(rows)
    assert f[0]["trail_12h_usd"] is None
    assert gate_trail12_pnl(0)(f[0]) is True
    assert gate_trail24_pnl(-40)(f[0]) is True
    assert gate_drawdown(-50)(f[0]) is True
    assert gate_cold_restart()(f[0]) is True  # gap_hours None -> trade


def test_thin_gate_only_fires_with_existing_window():
    rows = synth([
        ("entry", "2026-09-24 08:00:00", "m1", None),
        ("close", "2026-09-24 08:05:00", "m1", -5.0),
        ("entry", "2026-09-24 08:10:00", "m2", None),
        ("close", "2026-09-24 08:15:00", "m2", -1.0),
    ])
    closes, f = feats(rows)
    g = gate_thin(10)
    assert g(f[0]) is True   # no window at all -> fail open
    assert g(f[1]) is False  # window of 1 < 10 -> stand down


def test_half_gate():
    rows = synth([
        ("entry", "2026-09-24 08:00:00", "m1", None),
        ("close", "2026-09-24 08:05:00", "m1", -1.0),
        ("entry", "2026-09-24 15:00:00", "m2", None),
        ("close", "2026-09-24 15:05:00", "m2", -1.0),
    ])
    closes, f = feats(rows)
    assert f[0]["entry_half"] == "AM" and f[1]["entry_half"] == "PM"
    assert gate_half("PM")(f[0]) is True and gate_half("PM")(f[1]) is False


def test_malformed_inputs_never_raise_and_fail_open():
    bad = {"orphan": False, "trail_12h_usd": float("nan"),
           "trail_24h_usd": float("inf"), "trail_12h_n": None,
           "gap_hours": "junk", "cum_usd_before": object(),
           "entry_half": None, "entry_dow": "Thu"}
    for g in CANDIDATE_GATES:
        assert g(bad) is True
    assert gate_trail12_pnl(0)({"orphan": False}) is True  # all-None features


def test_gates_do_not_mutate_features():
    rows = synth([
        ("entry", "2026-09-24 08:00:00", "m1", None),
        ("close", "2026-09-24 08:05:00", "m1", -5.0),
    ])
    closes, f = feats(rows)
    before = dict(f[0])
    for g in CANDIDATE_GATES:
        g(f[0])
    assert f[0] == before


def test_split_is_oos_by_time():
    rows = synth([
        ("close", "2026-09-24 08:05:00", "m1", -1.0),
        ("close", "2026-09-27 08:05:00", "m2", -1.0),
    ])
    closes = sorted([r for r in rows], key=lambda r: r["ts"])
    is_c, oos_c = split_is_oos(closes)
    assert [c["mint"] for c in is_c] == ["m1"]
    assert [c["mint"] for c in oos_c] == ["m2"]


def test_evaluate_gate_edge_math():
    rows = synth([
        ("entry", "2026-09-24 08:00:00", "m1", None),
        ("close", "2026-09-24 08:05:00", "m1", -10.0),
        ("entry", "2026-09-24 09:00:00", "m2", None),
        ("close", "2026-09-24 09:05:00", "m2", 4.0),
    ])
    closes = sorted([r for r in rows if r["type"] == "close"],
                    key=lambda r: r["ts"])
    entries = [r for r in rows if r["type"] == "entry"]
    f = build_features(entries, closes)
    indexed = list(enumerate(closes))
    # gate that keeps everything: edge must be 0
    r = evaluate_gate(lambda ff: True, indexed, f)
    assert r["edge_per_trade"] == 0.0 and r["n_retained"] == 2


def test_gates_thread_safe():
    rows = synth([
        ("entry", "2026-09-24 08:00:00", "m1", None),
        ("close", "2026-09-24 08:05:00", "m1", -5.0),
    ])
    closes, f = feats(rows)
    errs = []

    def hammer():
        try:
            for _ in range(2000):
                for g in CANDIDATE_GATES:
                    g(f[0])
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=hammer) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs


def test_benchmark_within_budget():
    b = benchmark_gate(2000)
    assert b["p99_us"] < 50_000  # 50ms budget in microseconds
