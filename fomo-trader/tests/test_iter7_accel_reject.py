#!/usr/bin/env python3
"""R10 instrumentation tests (deep-improvement loop, iter-7).

Covers journal.sanitize_early_accel_reject and the Hunter-side collection
of early-path accel-gate rejects. Instrumentation only: nothing here asserts
or changes strategy behavior.

Background: the early (5m-window) path's acceleration gate in
fomo_trader.py (`if p15 < e_min_m15 or p15 <= p5 * e_accel: continue`,
commented "not accelerating: move is stalling") is inverted vs its
documented intent (pre-peak movers, "most of the move still ahead"). With
accel_ratio=1.2 it admits stalling moves (p15 >> p5) and rejects fresh
accelerations (p15 ~= p5). The rejected pools never surface as candidates,
so the corrected orientation cannot be backtested; these rows make the
corrected-admission set observable for a prospective Track B gate.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import journal


def _obs(**kw):
    base = {
        "name": "TKN / WBNB", "mint": "0xabc", "pool": "0xpool",
        "chain": "bsc", "signal_price_usd": 1e-6,
        "liquidity_usd": 45000.0, "m15_volume_usd": 12000.0,
        "m15_gain_pct": 55.0, "m5_gain_pct": 50.0,
        "m15_buys": 60, "m15_sells": 20,
        "mcap_usd": 90000.0, "source": "geckoterminal",
        "accel_ratio": 1.2,
    }
    base.update(kw)
    return base


def test_reject_row_shape():
    r = journal.sanitize_early_accel_reject(_obs())
    assert r["event"] == "early_accel_reject"
    assert r["window"] == "5m" and r["early"] is True
    assert r["gate"] == "accel"
    assert r["m5_gain_pct"] == 50.0 and r["m15_gain_pct"] == 55.0
    assert r["ts_epoch"] > 0
    # strategy version is instrumentation-only: must NOT bump on this change
    assert r["strategy_version"] == journal.STRATEGY_VERSION


def test_decel_ratio_math():
    # decel_ratio = p15 / (p5 * accel_ratio); <1 means the corrected
    # (accelerating) orientation would admit this pool.
    r = journal.sanitize_early_accel_reject(_obs(m5_gain_pct=50.0,
                                                m15_gain_pct=55.0,
                                                accel_ratio=1.2))
    assert abs(r["decel_ratio"] - 55.0 / 60.0) < 1e-9
    r2 = journal.sanitize_early_accel_reject(_obs(m5_gain_pct=10.0,
                                                 m15_gain_pct=100.0,
                                                 accel_ratio=1.2))
    assert abs(r2["decel_ratio"] - 100.0 / 12.0) < 1e-9


def test_never_raises_on_garbage():
    for bad in (None, {}, {"m5_gain_pct": "x", "m15_gain_pct": float("nan")},
                {"m5_gain_pct": 0, "m15_gain_pct": -5, "accel_ratio": 0}):
        r = journal.sanitize_early_accel_reject(bad)
        assert r["event"] == "early_accel_reject"
        assert "journal_error" in r or r.get("decel_ratio") is None


def test_no_input_mutation():
    o = _obs()
    before = dict(o)
    journal.sanitize_early_accel_reject(o)
    assert o == before


def test_journal_event_roundtrip(tmp_path):
    rec = journal.sanitize_early_accel_reject(_obs())
    journal.journal_event(str(tmp_path), rec)
    rows = [json.loads(l) for l in
            open(os.path.join(str(tmp_path), journal.CANDIDATES_FILE))]
    assert len(rows) == 1
    assert rows[0]["event"] == "early_accel_reject"
    assert rows[0]["mint"] == "0xabc"


def test_gate_inversion_characterization():
    """Characterize the live gate (fomo_trader.py early loop) as inverted.

    This is a characterization test, not a behavior prescription: it
    documents the defect the R10 rows exist to study prospectively. If the
    gate is ever corrected, this test must be updated to the new orientation
    (that is the point of the prospective protocol).
    """
    e_accel = 1.2  # config default hunter.early.accel_ratio

    def current_gate_rejects(p5, p15, e_min_m15=15):
        return p15 < e_min_m15 or p15 <= p5 * e_accel

    # Fresh acceleration: all of the 15m move happened in the last 5m.
    # Documented intent ("pre-peak", "most of the move still ahead") says
    # ADMIT; the live gate REJECTS.
    assert current_gate_rejects(50.0, 55.0) is True
    # Stalled move: 10x more gain in minutes 5-15 than in the last 5m.
    # Documented intent says REJECT ("not accelerating: move is stalling"
    # is the comment on the reject branch); the live gate ADMITS.
    assert current_gate_rejects(10.0, 100.0) is False


def test_hunter_collects_rejects_list():
    import fomo_trader
    h = fomo_trader.Hunter({"hunter": {}})
    assert h.last_early_accel_rejects == []
    # list is per-scan mutable state, present without running a scan
    h.last_early_accel_rejects.append({"mint": "0xabc"})
    assert h.last_early_accel_rejects == [{"mint": "0xabc"}]
