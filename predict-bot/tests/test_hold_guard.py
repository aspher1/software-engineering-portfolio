import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest

import hold_guard as hg
from hold_guard import (taken_price, entry_taken, open_signals, decide,
                        may_fire, HG_BIG_DROP, HG_FAST_DROP)


# ---------- taken-side conversions ----------

def test_taken_price_yes():
    assert taken_price("YES", 0.76) == 0.76


def test_taken_price_no():
    assert taken_price("NO", 0.16) == pytest.approx(0.84)


def test_entry_taken_yes():
    assert entry_taken("YES", 0.76, 0.064) == (0.76, 0.064)


def test_entry_taken_no():
    p, e = entry_taken("NO", 0.16, -0.07)
    assert p == pytest.approx(0.84) and e == pytest.approx(0.07)


def test_entry_taken_missing():
    assert entry_taken("NO", None, None) == (None, None)


# ---------- decide(): pure trigger logic ----------

def _hist(now, pts):
    """pts: [(seconds_ago, price)] -> chronological [[ts, px], ...]."""
    return [[now - ago, px] for ago, px in sorted(pts, reverse=True)]


def test_decide_big_drop_fires():
    now = time.time()
    h = _hist(now, [(60, 0.90), (30, 0.86), (0, 0.79)])  # 11c drop in 60s
    hit = decide(h, now)
    assert hit is not None
    drop, span, tier = hit
    assert tier == "big" and drop == pytest.approx(0.11, abs=1e-3)


def test_decide_fast_drop_fires():
    now = time.time()
    h = _hist(now, [(30, 0.80), (15, 0.76), (0, 0.725)])  # 7.5c in 30s
    hit = decide(h, now)
    assert hit is not None
    drop, span, tier = hit
    assert tier == "fast" and drop == pytest.approx(0.075, abs=1e-3)


def test_decide_instant_drop_fires():
    now = time.time()
    h = _hist(now, [(12, 0.80), (6, 0.775), (0, 0.745)])  # 5.5c in 12s
    hit = decide(h, now)
    assert hit is not None
    drop, span, tier = hit
    assert tier == "instant" and drop == pytest.approx(0.055, abs=1e-3)


def test_decide_below_threshold_holds():
    now = time.time()
    h = _hist(now, [(60, 0.90), (0, 0.815)])  # 8.5c in 60s: below both tiers
    assert decide(h, now) is None


def test_decide_favourable_run_never_fires():
    now = time.time()
    h = _hist(now, [(60, 0.50), (0, 0.90)])  # 40c UP: favourable, not adverse
    assert decide(h, now) is None


def test_decide_min_span_guard():
    now = time.time()
    h = _hist(now, [(3, 0.90), (0, 0.70)])  # 20c but only 3s of history
    assert decide(h, now) is None


def test_decide_uses_peak_not_first():
    now = time.time()
    h = _hist(now, [(60, 0.80), (40, 0.86), (20, 0.84), (0, 0.75)])
    hit = decide(h, now)  # 11c from the 40s-ago peak
    assert hit is not None and hit[2] == "big"


def test_decide_steady_price_holds():
    now = time.time()
    h = _hist(now, [(60, 0.70), (30, 0.701), (0, 0.699)])
    assert decide(h, now) is None


# ---------- may_fire(): dedupe / rearm / cooldown ----------

def test_may_fire_first_alert():
    assert may_fire({"hist": [], "last_alert_ts": 0,
                     "last_alert_price": None}, time.time(), 0.70)


def test_may_fire_suppresses_repeat():
    now = time.time()
    ent = {"hist": [], "last_alert_ts": now - 400, "last_alert_price": 0.70}
    assert not may_fire(ent, now, 0.68)   # only 2c further down: no rearm
    assert not may_fire(ent, now, 0.70)   # flat: no rearm


def test_may_fire_rearms_on_further_collapse():
    now = time.time()
    ent = {"hist": [], "last_alert_ts": now - 400, "last_alert_price": 0.70}
    assert may_fire(ent, now, 0.60)       # another 10c down: rearm


def test_may_fire_cooldown_blocks_early_rearm():
    now = time.time()
    ent = {"hist": [], "last_alert_ts": now - 100, "last_alert_price": 0.70}
    assert not may_fire(ent, now, 0.50)   # cooldown still active


# ---------- open_signals(): held-position selection ----------

def _entry(side, exit_ts=None, close_off=600, market_mid=0.7, edge=0.06,
           ticker="KXETH15M-26OCT021415-15"):
    now = time.time()
    v = {"side": side, "ts": "2026-10-02T17:00:00+00:00", "ticker": ticker,
         "market_mid": market_mid, "edge": edge,
         "window_close_utc": now + close_off}
    if exit_ts:
        v["exit_ts"] = exit_ts
    return v


def test_open_signals_selects_held():
    now = time.time()
    al = {"K1:NO": _entry("NO"), "K2:YES": _entry("YES", ticker="KXBTC15M-26OCT021415-15")}
    got = open_signals(al, now)
    assert {g["signal_key"] for g in got} == {"K1:NO", "K2:YES"}
    no = [g for g in got if g["side"] == "NO"][0]
    assert no["entry_price_taken"] == pytest.approx(0.30)
    assert no["entry_edge_taken"] == pytest.approx(-0.06)


def test_open_signals_skips_exited_closed_and_malformed():
    now = time.time()
    al = {
        "A:NO": _entry("NO", exit_ts="2026-10-02T17:01:00+00:00"),
        "B:YES": _entry("YES", close_off=-60),          # window closed
        "C:YES": _entry("YES", close_off=30),           # < 1 min left
        "D:str": "2026-10-02T17:00:00+00:00",           # legacy string value
        "E:noside": {"ticker": "KXX"},                 # no side
    }
    assert open_signals(al, now) == []


# ---------- end-to-end trigger on a synthetic held position ----------

def test_end_to_end_big_drop_fires_and_dedupes():
    """A YES holder whose taken price collapses 12c in 55s fires once,
    then stays quiet on the next poll at the same level."""
    now = time.time()
    key = "KXETH15M-26OCT021415-15:YES"
    hist = _hist(now - 1, [(55, 0.90), (40, 0.88), (20, 0.84), (1, 0.78)])
    ent = {"hist": hist, "last_alert_ts": 0, "last_alert_price": None}
    px = 0.78
    hit = decide(ent["hist"] + [[now, px]], now)
    assert hit is not None and hit[2] == "big"
    assert may_fire(ent, now, px)
    ent["last_alert_ts"] = now
    ent["last_alert_price"] = px
    # next poll, flat at the same level: no second alert
    assert decide(ent["hist"] + [[now + 5, 0.781]], now + 5) is None or \
        not may_fire(ent, now + 5, 0.781)


def test_end_to_end_no_side_rising_yes_mid_is_adverse():
    """A NO holder sees the YES mid rise 12c in 55s -> taken (NO) price
    falls 12c -> big-tier fire."""
    now = time.time()
    mids = _hist(now - 1, [(55, 0.10), (40, 0.14), (20, 0.18), (1, 0.22)])
    taken_hist = [[t, taken_price("NO", m)] for t, m in mids]
    hit = decide(taken_hist + [[now, taken_price("NO", 0.22)]], now)
    assert hit is not None and hit[2] == "big"
