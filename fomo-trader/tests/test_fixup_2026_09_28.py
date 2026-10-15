"""Fix-up tests for the 2026-09-28 follow-up pass.

Covers:
  Task A (journal.py instrumentation): observation sanitizing, pool-age
  extraction, the observation tracker, veto/entry record builders, the
  append-only event journal, and the no-mutation / never-raise guarantees
  that keep journaling from ever influencing strategy decisions.
  Task B (allocator back to shadow): config mode and _allocate shadow
  behavior (decisions logged, no live effect).
  Task C (positive-slip veto, user-ordered): slip>0 -> vetoed + journaled;
  slip<=0 -> proceeds; slip unknown -> proceeds (fail open); flag off ->
  never vetoes.
"""
import copy
import json
import os
import threading
import time

import pytest

import journal
from journal import (ObservationTracker, finalize_entry_record, journal_event,
                     pool_created_at, sanitize_observation, sanitize_veto,
                     STRATEGY_VERSION)
from fomo_trader import Trader, slip_from_signal


# ---------------- helpers ----------------

def sig(**extra):
    s = {"chain": "bsc", "mint": "0xMINT", "pool": "0xPOOL", "name": "TEST",
         "m15_gain_pct": 150.0, "m5_gain_pct": 40.0, "buy_sell_ratio": 12.0,
         "m15_buys": 60, "m15_sells": 5, "liquidity_usd": 25000,
         "m15_volume_usd": 9000, "mcap_usd": 400000,
         "signal_price_usd": 0.00001, "source": "geckoterminal",
         "window_label": "15m", "ts": time.time()}
    s.update(extra)
    return s


def trader(tmp_path, veto=True):
    t = Trader.__new__(Trader)  # no wallet file is loaded
    t.cfg = {"hunter": {"entry": {"veto_positive_slip": veto},
                        "rug_guard": {}},
             "risk": {"max_open_positions": 3,
                      "buy_sol_per_trade": .06, "buy_bnb_per_trade": .009},
             "exit": {"slippage_bps": 500}}
    t.dry_run = True
    t.state_path = str(tmp_path / "state.json")
    t.state = {"positions": {}, "cooldown": {}, "trades_today": [],
               "trades_this_hour": []}
    t.lock = threading.RLock()
    t.pending_entries = set()
    return t


def read_candidates(tmp_path):
    p = tmp_path / "candidates.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


# ---------------- Task A: pool age ----------------

def test_pool_created_at_ms_normalized():
    # DexScreener pairCreatedAt is milliseconds -> normalized to seconds
    assert pool_created_at({"pairCreatedAt": 1727445600000}) == 1727445600.0


def test_pool_created_at_seconds_passthrough():
    assert pool_created_at({"pool_created_at": 1727445600}) == 1727445600.0


def test_pool_created_at_missing_is_none_not_crash():
    assert pool_created_at({}) is None
    assert pool_created_at(None) is None
    assert pool_created_at({"pairCreatedAt": "garbage"}) is None
    assert pool_created_at({"pairCreatedAt": -5}) is None


# ---------------- Task A: pool age, ISO-8601 strings (GeckoTerminal) ---------
# Added 2026-09-29 by the 6h improvement loop: GT pool payloads carry
# created_at as an ISO-8601 string, which previously journaled as null
# (100% null pool_created_at in the deep-research lane). Pure instrumentation:
# pool age never feeds entry/exit decisions, and STRATEGY_VERSION stays "1".

def test_pool_created_at_iso_zulu():
    assert pool_created_at({"pool_created_at": "2026-09-28T16:25:16Z"}) == 1790612716.0


def test_pool_created_at_iso_with_offset():
    # +02:00 offset must normalize to the same UTC instant
    assert pool_created_at({"created_at": "2026-09-28T18:25:16+02:00"}) == 1790612716.0


def test_pool_created_at_iso_key_priority():
    # First recognized key wins; ISO and numeric paths mix freely
    assert pool_created_at({"pairCreatedAt": 1790612716000}) == 1790612716.0
    assert pool_created_at({"poolCreatedAt": 1790612716}) == 1790612716.0


def test_pool_created_at_iso_bad_or_naive_is_none_not_crash():
    assert pool_created_at({"created_at": "bad-date"}) is None
    # naive (no tz) strings are skipped rather than guessed
    assert pool_created_at({"created_at": "2026-09-28T16:25:16"}) is None
    assert pool_created_at({"created_at": ""}) is None
    assert pool_created_at({"created_at": None}) is None


def test_pool_created_at_iso_does_not_bump_strategy_version():
    # Instrumentation-only change: decisions must not see a version bump
    assert STRATEGY_VERSION == "1"


# ---------------- Task A: observation sanitizing ----------------

def test_sanitize_observation_full_signal():
    s = sig()
    before = copy.deepcopy(s)
    o = sanitize_observation(s, verdict="enter")
    assert o["event"] == "candidate"
    assert o["strategy_version"] == STRATEGY_VERSION
    assert o["name"] == "TEST" and o["mint"] == "0xMINT"
    assert o["pool"] == "0xPOOL" and o["chain"] == "bsc"
    assert o["price_usd"] == 0.00001
    assert o["liquidity_usd"] == 25000
    assert o["m15_gain_pct"] == 150.0
    assert o["guardrail_verdict"] == "enter"
    assert o["pool_created_at"] is None  # GT payloads carry none
    assert s == before  # never mutates the input signal


def test_sanitize_observation_missing_fields_degrade_to_null():
    o = sanitize_observation({"name": "X"})
    assert o["price_usd"] is None
    assert o["liquidity_usd"] is None
    assert o["mint"] is None
    assert o["event"] == "candidate"


def test_sanitize_observation_never_raises():
    o = sanitize_observation(None)
    assert o["event"] == "candidate"
    o2 = sanitize_observation({"signal_price_usd": float("nan")})
    assert o2["price_usd"] is None


# ---------------- Task A: observation tracker ----------------

def test_tracker_first_then_repeat():
    tr = ObservationTracker(max_entries=10)
    s = sig()
    assert tr.observe(s) is None
    rep = tr.observe(s)
    assert rep is not None
    assert rep["sightings"] == 2
    assert rep["second_price_usd"] == 0.00001
    assert rep["first_ts"] <= time.time()


def test_tracker_bounded_eviction():
    tr = ObservationTracker(max_entries=150)
    for i in range(200):
        tr.observe(sig(mint="0x%d" % i))
    assert len(tr._seen) <= 150


def test_tracker_never_raises_or_mutates():
    tr = ObservationTracker()
    assert tr.observe(None) is None or isinstance(tr.observe(None), dict)
    s = sig()
    before = copy.deepcopy(s)
    tr.observe(s)
    assert s == before


# ---------------- Task A: veto record ----------------

def test_sanitize_veto_fields():
    s = sig()
    before = copy.deepcopy(s)
    v = sanitize_veto(s, entry_native=0.0095, native_usd=800.0,
                      slip=0.06, chain="bsc")
    assert v["event"] == "slip_veto"
    assert v["strategy_version"] == STRATEGY_VERSION
    assert v["name"] == "TEST" and v["chain"] == "bsc"
    assert v["signal_price_usd"] == 0.00001
    assert v["would_be_entry_native"] == 0.0095
    assert v["would_be_entry_usd"] == pytest.approx(7.6)
    assert v["slip_from_signal"] == pytest.approx(0.06)
    assert v["liquidity_usd"] == 25000
    assert v["signal_gain_pct"] == 150.0
    assert s == before


# ---------------- Task A: entry record finalizer ----------------

def test_finalize_entry_record_adds_version_and_pool_age():
    rec = {"type": "entry", "name": "T"}
    out = finalize_entry_record(rec, sig(pairCreatedAt=1727445600000))
    assert out["strategy_version"] == STRATEGY_VERSION
    assert out["pool_created_at"] == 1727445600.0
    assert out["signal_gain_pct"] == 150.0
    assert out["liquidity_usd"] == 25000


def test_finalize_entry_record_fills_slip_gap():
    rec = {"type": "entry"}
    out = finalize_entry_record(rec, sig(slip_from_signal_pct=0.03))
    assert out["slip_from_signal_pct"] == 0.03


def test_finalize_entry_record_never_clobbers():
    rec = {"type": "entry", "signal_gain_pct": 999.0,
           "strategy_version": "0"}
    out = finalize_entry_record(rec, sig())
    assert out["signal_gain_pct"] == 999.0
    assert out["strategy_version"] == "0"


def test_finalize_entry_record_never_raises():
    # garbage normalizes into a valid record, never raises
    out = finalize_entry_record(None, None)
    assert isinstance(out, dict) and out["strategy_version"] == "1"
    out2 = finalize_entry_record("nope", None)
    assert isinstance(out2, dict)


# ---------------- Task A: event journal ----------------

def test_journal_event_appends(tmp_path):
    journal_event(str(tmp_path), {"event": "candidate", "name": "T"})
    rows = read_candidates(tmp_path)
    assert len(rows) == 1 and rows[0]["name"] == "T"


def test_journal_event_never_raises(tmp_path):
    journal_event(str(tmp_path), None)
    journal_event("/nonexistent-dir-xyz", {"event": "x"})
    journal_event(str(tmp_path), {"event": object()})  # unserializable
    assert read_candidates(tmp_path) == []


# ---------------- Task A/C: parity — journaling never touches decisions ----------------

def test_journal_hooks_do_not_mutate_signal(tmp_path):
    t = trader(tmp_path)
    s = sig()
    before = copy.deepcopy(s)
    t._journal_candidate(s, "enter")
    t._journal_candidate(s, "guardrail_skip")
    journal.sanitize_veto(s, 0.009, 800.0, 0.05, "bsc")
    journal.finalize_entry_record({"type": "entry"}, s)
    assert s == before


def test_journal_candidate_never_raises(tmp_path):
    t = trader(tmp_path)
    t._journal_candidate(None, "enter")  # garbage in -> degraded record, no raise
    t._journal_candidate(sig(), "enter")
    rows = read_candidates(tmp_path)
    assert len(rows) == 2
    assert rows[1]["guardrail_verdict"] == "enter"


# ---------------- Task C: slip measurement ----------------

def test_slip_from_signal_positive():
    s = sig(signal_price_usd=100.0)
    assert slip_from_signal(s, 1.1, 100.0) == pytest.approx(0.1)


def test_slip_from_signal_negative_and_zero():
    s = sig(signal_price_usd=100.0)
    assert slip_from_signal(s, 0.9, 100.0) == pytest.approx(-0.1)
    assert slip_from_signal(s, 1.0, 100.0) == pytest.approx(0.0)


def test_slip_from_signal_unknown_is_none():
    assert slip_from_signal({}, 1.1, 100.0) is None
    assert slip_from_signal(sig(), 1.1, None) is None
    assert slip_from_signal(sig(signal_price_usd=0), 1.1, 100.0) is None
    assert slip_from_signal(sig(signal_price_usd=None), 1.1, 100.0) is None


# ---------------- Task C: veto behavior ----------------

def test_veto_positive_slip_vetoes_and_journals(tmp_path):
    t = trader(tmp_path, veto=True)
    s = sig(signal_price_usd=100.0)
    assert t._slip_veto(s, 1.1, 100.0, "bsc") is True
    rows = read_candidates(tmp_path)
    assert len(rows) == 1
    assert rows[0]["event"] == "slip_veto"
    assert rows[0]["slip_from_signal"] == pytest.approx(0.1)


def test_veto_nonpositive_slip_proceeds(tmp_path):
    t = trader(tmp_path, veto=True)
    s = sig(signal_price_usd=100.0)
    assert t._slip_veto(s, 0.9, 100.0, "bsc") is False
    assert t._slip_veto(s, 1.0, 100.0, "solana") is False
    assert read_candidates(tmp_path) == []


def test_veto_unknown_slip_proceeds_fail_open(tmp_path, capsys):
    t = trader(tmp_path, veto=True)
    assert t._slip_veto({}, 1.1, 100.0, "bsc") is False
    assert read_candidates(tmp_path) == []
    assert "SLIP-VETO GAP" in capsys.readouterr().out


def test_veto_flag_off_never_vetoes(tmp_path):
    t = trader(tmp_path, veto=False)
    s = sig(signal_price_usd=100.0)
    assert t._slip_veto(s, 1.5, 100.0, "bsc") is False
    assert read_candidates(tmp_path) == []


def test_veto_accepts_precomputed_slip(tmp_path):
    t = trader(tmp_path, veto=True)
    s = sig()  # no signal price -> slip unmeasurable from signal...
    # ...but a precomputed slip (e.g. from wipe_drift_cap_native) works
    assert t._slip_veto(s, 0.009, 800.0, "bsc", slip=0.02) is True
    rows = read_candidates(tmp_path)
    assert rows[0]["slip_from_signal"] == pytest.approx(0.02)


# ---------------- Task B: allocator back to shadow ----------------

def test_allocator_mode_shadow_in_live_config():
    import allocator
    cfg = json.load(open("/home/hatch/workspace/fomo-trader/"
                         "runs/paper-1h/config.json"))
    assert allocator.mode_of(cfg.get("allocator")) == "shadow"


def test_allocate_shadow_logs_but_does_not_act():
    import allocator
    t = Trader.__new__(Trader)
    t.cfg = {"allocator": {"mode": "shadow", "take_threshold": 0.35},
             "money": {"max_drawdown_pct": 15.0}}
    t.state = {"bankroll_usd": 1000.0, "equity_peak_usd": 1000.0}
    t._bankroll_ticket = lambda configured_native, native_usd: 0.009
    t._risk_ceiling_native = lambda native_usd: 0.009
    feats_signal = sig()
    buy, fields, skip = t._allocate(feats_signal, "bsc", 0.009, 800.0,
                                    entry=0.000011)
    # shadow: trades the plain 1.0x ticket, never skips...
    assert skip is False
    assert buy == 0.009
    # ...but journals the full decision for research
    assert fields["allocator_mode"] == "shadow"
    assert "allocator_score" in fields
    assert "allocator_take" in fields
    assert "allocator_multiplier" in fields
    assert "allocator_would_be_native" in fields
    assert "allocator_applied_native" not in fields


def test_allocate_live_still_applies_when_configured():
    import allocator
    t = Trader.__new__(Trader)
    t.cfg = {"allocator": {"mode": "live", "take_threshold": 0.99},
             "money": {"max_drawdown_pct": 15.0}}
    t.state = {"bankroll_usd": 1000.0, "equity_peak_usd": 1000.0}
    t._bankroll_ticket = lambda configured_native, native_usd: 0.009
    t._risk_ceiling_native = lambda native_usd: 0.009
    # absurdly high threshold -> allocator votes skip; live mode applies it
    buy, fields, skip = t._allocate(sig(), "bsc", 0.009, 800.0,
                                   entry=0.000011)
    assert skip is True and buy is None
    assert fields["allocator_mode"] == "live"
