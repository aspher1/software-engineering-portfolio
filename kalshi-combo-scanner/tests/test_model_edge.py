"""Offline tests for Tier 3 model-edge (scripts/model_edge.py + combo_scan wiring)."""

import io
import json
import math
import os
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import backtest_model_edge as bt  # noqa: E402
import combo_scan as cs  # noqa: E402
import model_edge as me  # noqa: E402

W_OPEN = 1790870400            # 2026-10-01T16:00:00Z
W_CLOSE = W_OPEN + 900
NOW = W_OPEN + 600             # 10 minutes in, 5 minutes left
ARGS = SimpleNamespace(max_outlay_cents=2000, min_outlay_cents=1000, min_roi=0.0)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def candles(n=80, end=NOW - 60, step=0.001, base=100.0):
    """n one-minute candles, the last one starting at `end` (older than the
    last synthetic trade), log returns alternating +/-step."""
    out, px = [], base
    for i in range(n):
        out.append((end - (n - 1 - i) * 60, px))
        px *= math.exp(step if i % 2 == 0 else -step)
    return out


def trades(p_ref=100.0, p_now=100.0, now=NOW, w_open=W_OPEN):
    return [[str(p_ref), "1", w_open - 50], [str(p_ref), "1", w_open - 10],
            [str(p_now), "1", now - 5]]


def ticker_stamp(w_close):
    """Kalshi 15-min ticker date/time part for a window closing at w_close
    (W_CLOSE -> "26OCT011215"), so listing times match the ticker's window."""
    et = datetime.fromtimestamp(w_close, ZoneInfo("America/New_York"))
    mon = {v: k for k, v in me.MONTHS.items()}[et.month]
    return f"{et:%y}{mon}{et:%d%H%M}", f"{et:%M}"


def window(sym="BTC", ya=0.62, na=0.40, w_open=W_OPEN, w_close=W_CLOSE, size=500, **extra):
    stamp, mi = ticker_stamp(w_close)
    m = {"ticker": f"KX{sym}15M-{stamp}-{mi}", "event_ticker": f"KX{sym}15M-{stamp}",
         "title": f"{sym} price up in next 15 mins?",
         "open_time": iso(w_open), "close_time": iso(w_close),
         "expected_expiration_time": iso(w_close + 300),
         "yes_ask_dollars": f"{ya:.4f}", "no_ask_dollars": f"{na:.4f}",
         "yes_ask_size_fp": str(size), "yes_bid_size_fp": str(size)}
    m.update(extra)
    return m


def kraken_with(*pairs):
    k = me.KrakenCache()
    for p in pairs:
        k.put(p, trades(), candles())
    return k


@pytest.fixture
def fixed_fair(monkeypatch):
    """Pin the model's raw fair value so screening tests control the edge."""
    box = {"fair": 0.70}
    monkeypatch.setattr(cs.me, "fair_15m",
                        lambda *a: (box["fair"], 100.0, 101.0, 0.001, 5.0))
    return box


# ------------------------------------------------------------- pure model ----

def test_phi_spot_values():
    assert me.phi(0) == 0.5
    assert me.phi(1.959963985) == pytest.approx(0.975, abs=1e-6)
    assert me.phi(-1) == pytest.approx(1 - me.phi(1))
    assert me.phi(8) == pytest.approx(1.0) and me.phi(-8) == pytest.approx(0.0)


def test_fair_15m_zero_log_return_is_half():
    fr = me.fair_15m("XBTUSD", W_OPEN, W_CLOSE, NOW, trades(100, 100), candles())
    fair, p_ref, p_now, sigma, t = fr
    assert fair == pytest.approx(0.5) and p_ref == 100 and p_now == 100
    assert sigma == pytest.approx(0.001, rel=1e-6) and t == pytest.approx(5.0)


def test_fair_15m_matches_formula_and_direction():
    fr = me.fair_15m("XBTUSD", W_OPEN, W_CLOSE, NOW, trades(100, 100.3), candles())
    want = me.phi(math.log(100.3 / 100) / (0.001 * math.sqrt(5.0)))
    assert fr[0] == pytest.approx(want) and fr[0] > 0.5
    down = me.fair_15m("XBTUSD", W_OPEN, W_CLOSE, NOW, trades(100, 99.7), candles())
    assert down[0] < 0.5


def test_fair_15m_more_time_left_is_closer_to_half():
    near = me.fair_15m("XBTUSD", W_OPEN, W_CLOSE, NOW, trades(100, 100.2), candles())
    far = me.fair_15m("XBTUSD", W_OPEN, W_CLOSE + 3600, NOW, trades(100, 100.2), candles())
    assert 0.5 < far[0] < near[0]
    # t is floored at 0.5 minutes
    late = me.fair_15m("XBTUSD", W_OPEN, W_CLOSE, W_CLOSE - 1,
                       trades(100, 100.2, now=W_CLOSE - 1), candles(end=W_CLOSE - 61))
    assert late[4] == 0.5


def test_fair_15m_ref_is_mean_of_last_minute_before_open():
    tr = [["90", "1", W_OPEN - 200], ["100", "1", W_OPEN - 60], ["102", "1", W_OPEN - 1],
          ["500", "1", W_OPEN], ["101", "1", NOW - 5]]
    assert me.fair_15m("XBTUSD", W_OPEN, W_CLOSE, NOW, tr, candles())[1] == pytest.approx(101)


def test_fair_15m_uses_newer_candle_when_trades_are_truncated():
    # Kraken returns at most 1000 trades from `since`: the last trade can be minutes old.
    tr = trades(100, 100, now=W_OPEN + 60)
    cn = candles()
    cn[-1] = (cn[-1][0], 100.5)
    fr = me.fair_15m("XBTUSD", W_OPEN, W_CLOSE, NOW, tr, cn)
    assert fr[2] == 100.5 and fr[0] > 0.5


@pytest.mark.parametrize("tr,cn", [
    ([], candles()),                                       # no trades
    ([["x", "1", W_OPEN - 10]], candles()),                # unparseable
    ([["100", "1", W_OPEN + 5]], candles()),               # no reference trades
    (trades(), candles(n=69)),                             # too few candles
    (trades(), candles(step=0.0)),                         # zero volatility
    (trades(100, 100, now=NOW - 400), candles(end=NOW - 400)),   # stale spot
    (trades(0, 100), candles()),                           # non-positive price
    (None, candles()),
    (trades(), [("a", "b")] * 80),
])
def test_fair_15m_garbage_is_none(tr, cn):
    assert me.fair_15m("XBTUSD", W_OPEN, W_CLOSE, NOW, tr, cn) is None


def test_shrink_midrange():
    assert me.shrink_midrange(0.60, 0.52) == pytest.approx(0.58)
    assert me.shrink_midrange(0.40, 0.48) == pytest.approx(0.42)
    assert me.shrink_midrange(0.61, 0.50) == 0.61 and me.shrink_midrange(0.39, 0.50) == 0.39
    for fair in (0.40, 0.45, 0.5, 0.55, 0.60, 0.2, 0.9):
        for mid in (0.1, 0.45, 0.5, 0.58, 0.95):
            s = me.shrink_midrange(fair, mid)
            assert min(fair, mid) - 1e-12 <= s <= max(fair, mid) + 1e-12
            assert (s > mid) == (fair > mid) or s == mid


def test_agrees_with_lean_boundary():
    assert me.agrees_with_lean("YES", 0.5) and me.agrees_with_lean("NO", 0.5)
    assert me.agrees_with_lean("YES", 0.7) and not me.agrees_with_lean("YES", 0.49)
    assert me.agrees_with_lean("NO", 0.3) and not me.agrees_with_lean("NO", 0.51)


def test_in_warmup():
    assert me.in_warmup(W_OPEN, W_OPEN) and me.in_warmup(W_OPEN, W_OPEN + 179)
    assert not me.in_warmup(W_OPEN, W_OPEN + 180)


def test_parse_15m_window():
    sym, wo, wc = me.parse_15m_window("KXBTC15M-26SEP301300-00")
    assert sym == "BTC" and wc - wo == 900
    assert wc == int(datetime(2026, 9, 30, 17, 0, tzinfo=timezone.utc).timestamp())  # 13:00 EDT
    assert me.parse_15m_window("KXBTC15M-26DEC011300-00")[2] == \
        int(datetime(2026, 12, 1, 18, 0, tzinfo=timezone.utc).timestamp())       # 13:00 EST
    for bad in ("KXBTC15M-26XYZ301300-00", "KXBTCD-26SEP3017-T80000", "", None,
                "KXBTC15M-26FEB301300-00"):
        assert me.parse_15m_window(bad) is None


def test_market_window_prefers_listing_times_and_falls_back_to_ticker():
    assert me.market_window(window()) == ("BTC", W_OPEN, W_CLOSE)
    m = window(open_time=None)
    m["ticker"] = "KXETH15M-26SEP301300-00"
    sym, wo, wc = me.market_window(m)
    assert sym == "ETH" and wc == int(datetime(2026, 9, 30, 17, tzinfo=timezone.utc).timestamp())
    assert me.market_window(window(sym="PEPE")) is None
    assert me.market_window({"ticker": "KXBTCD-26SEP3017-T80000"}) is None


def test_market_window_fails_closed_on_listing_ticker_mismatch():
    assert window()["ticker"] == "KXBTC15M-26OCT011215-15"
    assert me.market_window(window()) == ("BTC", W_OPEN, W_CLOSE)
    # 900s span, but the next window's times on this ticker: refused
    shifted = window(open_time=iso(W_OPEN + 900), close_time=iso(W_CLOSE + 900))
    assert me.market_window(shifted) is None
    assert me.market_window(dict(window(), open_time=iso(W_OPEN - 1),
                                 close_time=iso(W_CLOSE - 1))) is None
    # unparseable ticker date with a 900s listing span: refused
    assert me.market_window(dict(window(), ticker="KXBTC15M-26XYZ011215-15")) is None
    # not a 900s span: ticker fallback, unchanged
    assert me.market_window(window(close_time=iso(W_CLOSE + 60))) == ("BTC", W_OPEN, W_CLOSE)
    assert me.market_window(window(open_time=None, close_time=None)) == ("BTC", W_OPEN, W_CLOSE)


def test_kraken_pairs_map():
    assert me.KRAKEN_PAIRS == {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD",
                               "XRP": "XRPUSD", "DOGE": "DOGEUSD", "HYPE": "HYPEUSD",
                               "ZEC": "ZECUSD", "NEAR": "NEARUSD", "BNB": "BNBUSD"}


def test_kraken_fetchers_parse_and_raise_on_api_error():
    calls = []

    def http(url, params):
        calls.append((url, dict(params)))
        if url.endswith("/Trades"):
            return {"error": [], "result": {"XXBTZUSD": [["100.0", "1", 5.0]], "last": "9"}}
        return {"error": [], "result": {"XXBTZUSD": [[60, "1", "2", "0.5", "1.5", "1", "3", 4]],
                                        "last": 60}}
    assert me.kraken_trades("XBTUSD", 1000.7, http) == [["100.0", "1", 5.0]]
    assert me.kraken_candles("XBTUSD", http) == [(60, 1.5)]
    assert calls[0][1] == {"pair": "XBTUSD", "since": 1000}
    assert calls[1][1] == {"pair": "XBTUSD", "interval": 1}
    with pytest.raises(RuntimeError):
        me.kraken_trades("XBTUSD", 0, lambda u, p: {"error": ["EQuery:Unknown asset pair"]})


def test_kraken_cache_fetches_once_and_records_failures():
    n = {"calls": 0}

    def http(url, params):
        n["calls"] += 1
        return {"error": [], "result": {"X": []}}
    k = me.KrakenCache()
    assert k.fetch("XBTUSD", 0, http) is None and k.fetch("XBTUSD", 0, http) is None
    assert n["calls"] == 2 and k.get("XBTUSD") == ([], [])

    def boom(url, params):
        raise ConnectionError("proxy refused")
    err = k.fetch("ETHUSD", 0, boom)
    assert "ETHUSD" in err and "proxy refused" in err and k.get("ETHUSD") is None


# -------------------------------------------------------------- screening ----

def test_screen_flags_edge_vs_ask(fixed_fair):
    [c] = cs.screen_model_edge([window(ya=0.62, na=0.40)], NOW, kraken_with("XBTUSD"), [])
    assert c["combo_type"] == "M" and c["tier"] == "model-edge" and c["side"] == "YES"
    assert c["legs"] == [{"ticker": c["ticker"], "side": "yes", "price_cents": 62,
                          "venue": "kalshi"}]
    assert c["fair_taken_cc"] == 7000 and c["title"].startswith("BTC")
    assert c["markets"][0]["ticker"] == c["ticker"]


def test_screen_edge_boundary_is_against_the_ask_not_the_mid(fixed_fair):
    k = kraken_with("XBTUSD")
    assert len(cs.screen_model_edge([window(ya=0.65, na=0.37)], NOW, k, [])) == 1   # 5.00c
    assert cs.screen_model_edge([window(ya=0.6501, na=0.37)], NOW, k, []) == []     # 4.99c
    # mid 0.65 is 5c under fair, but the 0.66 ask is only 4c under: rejected
    assert cs.screen_model_edge([window(ya=0.66, na=0.36)], NOW, k, []) == []


def test_screen_no_side(fixed_fair):
    fixed_fair["fair"] = 0.25
    [c] = cs.screen_model_edge([window(ya=0.32, na=0.70)], NOW, kraken_with("XBTUSD"), [])
    assert c["side"] == "NO" and c["legs"][0]["side"] == "no"
    assert c["legs"][0]["price_cents"] == 70 and c["fair_taken_cc"] == 7500


def test_screen_drops_contrarian_takes(fixed_fair):
    fixed_fair["fair"] = 0.30          # model says NO; the market leans YES at 0.61
    assert cs.screen_model_edge([window(ya=0.62, na=0.40)], NOW, kraken_with("XBTUSD"), []) == []


def test_screen_applies_midrange_shrink(fixed_fair):
    fixed_fair["fair"] = 0.60          # in band: shrunk toward mid before the ask screen
    k = kraken_with("XBTUSD")
    # mid 0.52 -> fair 0.58 -> 5c over the 0.53 ask
    assert len(cs.screen_model_edge([window(ya=0.53, na=0.49)], NOW, k, [])) == 1
    # mid 0.53 -> fair 0.5825: 4.25c over the 0.54 ask (raw fair would show 6c)
    assert cs.screen_model_edge([window(ya=0.54, na=0.48)], NOW, k, []) == []


def test_screen_drops_warmup_closed_future_and_unsupported(fixed_fair):
    k = kraken_with("XBTUSD")
    m = window()
    assert cs.screen_model_edge([m], W_OPEN + 179, k, []) == []
    assert len(cs.screen_model_edge([m], W_OPEN + 180, k, [])) == 1
    assert cs.screen_model_edge([m], W_CLOSE, k, []) == []
    assert cs.screen_model_edge([m], W_OPEN - 1, k, []) == []
    assert cs.screen_model_edge([window(sym="PEPE")], NOW, kraken_with("PEPEUSD"), []) == []
    plain = dict(m, ticker="KXBTCD-26OCT0112-T84000")
    assert cs.screen_model_edge([plain], NOW, k, []) == []


def test_screen_drops_missing_kraken_data_and_unquoted_sides(fixed_fair):
    assert cs.screen_model_edge([window()], NOW, kraken_with("ETHUSD"), []) == []
    k = kraken_with("XBTUSD")
    assert cs.screen_model_edge([window(na=0.0)], NOW, k, []) == []
    assert cs.screen_model_edge([window(ya=0.0)], NOW, k, []) == []
    assert cs.screen_model_edge([window(ya=1.0)], NOW, k, []) == []


def test_screen_counts_windows_without_fair_value(monkeypatch):
    monkeypatch.setattr(cs.me, "fair_15m", lambda *a: None)
    stats = {}
    assert cs.screen_model_edge([window()], NOW, kraken_with("XBTUSD"), [], stats) == []
    assert stats["M_no_fair"] == 1


def test_screen_end_to_end_with_real_model():
    k = me.KrakenCache()
    k.put("XBTUSD", trades(100, 100.3), candles())     # fair ~0.91
    [c] = cs.screen_model_edge([window(ya=0.80, na=0.22)], NOW, k, [])
    assert c["side"] == "YES" and c["fair_taken_cc"] > 8500
    k.put("ETHUSD", trades(100, 100.0), candles())     # fair 0.5: no edge anywhere
    assert cs.screen_model_edge([window(sym="ETH", ya=0.52, na=0.50)], NOW, k, []) == []


def test_live_model_windows_and_kraken_fetch_bounds(monkeypatch):
    live = window(sym="BTC")
    future = window(sym="ETH", w_open=W_CLOSE, w_close=W_CLOSE + 900)
    warm = window(sym="SOL", w_open=NOW - 60, w_close=NOW + 840)
    rows = cs.live_model_windows([live, future, warm], NOW)
    assert [(r[1], r[2], r[3]) for r in rows] == [("XBTUSD", W_OPEN, W_CLOSE)]
    calls = []

    def fake_get(url, params=None, **kw):
        calls.append((url, dict(params)))
        return {"error": [], "result": {"X": []}}
    monkeypatch.setattr(cs, "http_get_json", fake_get)
    k, errors = me.KrakenCache(), []
    assert cs.fetch_kraken_for_windows([live, future, warm], NOW, k, errors) == 1
    assert len(calls) == 2 and all(p["pair"] == "XBTUSD" for _, p in calls)
    assert calls[0][1]["since"] == W_OPEN - 300 and errors == []
    # a decided window (no take can clear 5c under 100c) costs no Kraken calls
    calls.clear()
    assert cs.fetch_kraken_for_windows([window(ya=0.97, na=0.04)], NOW,
                                       me.KrakenCache(), []) == 0
    assert calls == []


@pytest.mark.parametrize("ya,na,possible", [
    (0.62, 0.40, True),      # leans YES, YES ask 62c
    (0.32, 0.70, True),      # leans NO, NO ask 70c
    (0.95, 0.06, True),      # YES ask exactly 95c
    (0.96, 0.05, False),     # YES ask 96c: fair would need > 100c
    (0.05, 0.96, False),
    (0.62, 0.0, False),      # NO side unquoted
])
def test_model_take_possible(ya, na, possible):
    assert cs.model_take_possible(window(ya=ya, na=na)) is possible


def test_take_possible_never_hides_a_screen_hit(fixed_fair):
    k = kraken_with("XBTUSD")
    for fair in (0.01, 0.2, 0.45, 0.55, 0.8, 0.99, 1.0):
        fixed_fair["fair"] = fair
        for ya in range(2, 99, 4):
            for spread in (1, 2, 5):
                m = window(ya=ya / 100, na=(100 - ya + spread) / 100)
                if cs.screen_model_edge([m], NOW, k, []):
                    assert cs.model_take_possible(m)


# ------------------------------------------------------------- evaluation ----

def leg(price=60, mult=1.0):
    return {"ticker": "KXBTC15M-X", "side": "yes", "price_cents": price, "venue": "kalshi",
            "fee_mult": mult}


def test_evaluate_model_edge_ev_math_with_fees():
    out = cs.evaluate_model_edge(leg(60), 500, 7000, 2000, 1000)
    # 32 sets at 60c: 1920c + ceil(0.07*32*0.6*0.4*100)=54c fee = 1974c
    assert out["sets"] == 32 and out["outlay_cents"] == 1974 and out["fees_cents"] == 54
    assert out["fair_cents"] == 70.0
    assert out["edge_cents_per_set"] == round((32 * 7000 - 197400) / 32 / 100, 2) == 8.31
    assert out["expected_roi_pct"] == round(26600 / 197400 * 100, 2)
    assert out["roi_if_right_pct"] == round((320000 - 197400) / 197400 * 100, 2)
    assert set(cs._sizing([leg(60)], 500, 32, 32, 197400)) <= set(out)
    assert "locked_profit_cents" not in out and "roi_pct" not in out


def test_evaluate_model_edge_needs_5c_after_fees():
    assert cs.evaluate_model_edge(leg(60), 500, 6669, 2000, 1000)["edge_cents_per_set"] >= 5
    assert cs.evaluate_model_edge(leg(60), 500, 6668, 2000, 1000) is None
    # 5c over the ask before fees is not enough once fees are paid
    assert cs.evaluate_model_edge(leg(60), 500, 6500, 2000, 1000) is None


def test_evaluate_model_edge_uses_scanner_fee_math():
    out = cs.evaluate_model_edge(leg(60, 2.0), 500, 9000, 2000, 1000)
    assert out["fees_cents"] == cs.cc_to_cents(cs.leg_fee_cc(leg(60, 2.0), out["sets"]))
    assert out["fees_cents"] == cs.taker_fee_cents(60, out["sets"], 2.0)
    assert out["fees_cents"] > cs.evaluate_model_edge(leg(60), 500, 9000, 2000, 1000)["fees_cents"]


def test_evaluate_model_edge_depth_and_outlay_gates():
    assert cs.evaluate_model_edge(leg(60), 0, 9000, 2000, 1000) is None
    assert cs.evaluate_model_edge(leg(60), 10, 9000, 2000, 1000) is None      # $6.17
    assert cs.evaluate_model_edge(leg(60), 16, 9000, 2000, 1000) is None      # $9.87
    out = cs.evaluate_model_edge(leg(60), 17, 9000, 2000, 1000)               # $10.49
    assert out["sets"] == 17 and out["depth_sets"] == 17 and out["affordable_sets"] == 32
    assert cs.evaluate_model_edge(leg(60), 500, 9000, 500, 100)["outlay_cents"] <= 500


# ------------------------------------------------------- finalize / labels ----

def model_cand(**kw):
    return cs.screen_model_edge([window(**kw)], NOW, kraken_with("XBTUSD"), [])[0]


def test_combo_key_tier_prefix():
    assert cs.combo_key("M", ["KXBTC15M-X"], "YES", "model-edge") == "model-edge|M|KXBTC15M-X|YES"
    keys = {cs.combo_key("M", ["T"], "YES", t) for t in ("locked", "edge", "model-edge")}
    assert len(keys) == 3


def test_finalize_model_edge_labels(fixed_fair):
    c = model_cand()
    books = {c["ticker"]: {"yes": (6200, 500.0), "no": (4000, 500.0)}}
    out = cs.finalize(c, books, {"KXBTC15M": 1.0}, ARGS, "now")
    assert out["tier"] == "model-edge" and out["combo_type"] == "M" and out["side"] == "YES"
    assert out["guarantee"] == cs.MODEL_EDGE_GUARANTEE and "NOT guaranteed" in out["guarantee"]
    assert "locked" not in json.dumps(out).lower()
    assert out["legs"] == [{"ticker": c["ticker"], "side": "yes", "price_cents": 62,
                            "venue": "kalshi"}]
    assert out["expiry"] == iso(W_CLOSE + 300) and out["detected_at"] == "now"
    assert out["title"] == c["title"] and out["sets"] == 31 and out["edge_cents_per_set"] >= 5
    for k in ("fair_cents", "edge_cents_per_set", "expected_roi_pct", "roi_if_right_pct",
              "outlay_cents", "fees_cents", "depth_sets", "affordable_sets", "model"):
        assert k in out


def test_finalize_model_edge_reprices_from_book(fixed_fair):
    tk = model_cand()["ticker"]
    # ask moved up to 66c: fair 70c leaves < 5c after fees
    assert cs.finalize(model_cand(), {tk: {"yes": (6600, 500.0), "no": (3600, 500.0)}},
                       {}, ARGS, "now") is None
    # taken side missing / empty
    assert cs.finalize(model_cand(), {}, {}, ARGS, "now") is None
    assert cs.finalize(model_cand(), {tk: {"yes": (None, 0.0), "no": (4000, 500.0)}},
                       {}, ARGS, "now") is None
    # opposite side gone: the fresh lean can't be verified
    assert cs.finalize(model_cand(), {tk: {"yes": (6200, 500.0), "no": (None, 0.0)}},
                       {}, ARGS, "now") is None
    # shallow book
    assert cs.finalize(model_cand(), {tk: {"yes": (6200, 5.0), "no": (4000, 500.0)}},
                       {}, ARGS, "now") is None


def test_finalize_model_edge_drops_when_fresh_book_flips_the_lean(fixed_fair):
    fixed_fair["fair"] = 0.25
    c = model_cand(ya=0.32, na=0.70)                    # NO take, market leans NO at 0.31
    books = {c["ticker"]: {"yes": (5300, 500.0), "no": (4900, 500.0)}}   # now leans YES
    assert cs.finalize(c, books, {}, ARGS, "now") is None


def test_finalize_model_edge_drops_when_fresh_book_flips_the_signal_side(fixed_fair, monkeypatch):
    fixed_fair["fair"] = 0.30
    c = model_cand(ya=0.39, na=0.63)                    # mid 0.38: NO take, fair 70c vs 63c ask
    assert c["side"] == "NO" and c["model"]["fair_raw"] == 0.30
    # fresh mid 0.29: the NO lean (0.71) still passes and the NO ask is quotable,
    # but fair_raw 0.30 > 0.29 now signals YES
    books = {c["ticker"]: {"yes": (3000, 500.0), "no": (7200, 500.0)}}
    assert me.agrees_with_lean("NO", cs.listing_mid(3000, 7200))
    calls = []
    monkeypatch.setattr(cs, "evaluate_model_edge", lambda *a: calls.append(a) or {})
    assert cs.finalize(c, books, {}, ARGS, "now") is None
    assert calls == []                                  # dropped by the side gate, not by EV


def test_finalize_model_edge_reshrinks_fair_on_the_fresh_mid(fixed_fair):
    fixed_fair["fair"] = 0.60                           # in the shrink band
    c = model_cand(ya=0.52, na=0.48)                    # screen mid 0.52 -> fair 0.58
    assert c["fair_taken_cc"] == 5800
    # fresh mid 0.50 (YES ask 51c): the fresh shrink gives 0.575
    books = {c["ticker"]: {"yes": (5100, 500.0), "no": (5100, 500.0)}}
    assert me.shrink_midrange(0.60, cs.listing_mid(5100, 5100)) == pytest.approx(0.575)
    stale = cs.evaluate_model_edge(leg(51), 500, 5800, 2000, 1000)
    assert stale["edge_cents_per_set"] == 5.24          # the stale fair would clear 5c
    assert cs.evaluate_model_edge(leg(51), 500, 5750, 2000, 1000) is None
    assert cs.finalize(c, books, {}, ARGS, "now") is None
    # at a 50c ask the fresh fair still clears, and is what gets reported
    c = model_cand(ya=0.52, na=0.48)
    out = cs.finalize(c, {c["ticker"]: {"yes": (5000, 500.0), "no": (5000, 500.0)}},
                      {}, ARGS, "now")
    assert out["fair_cents"] == 57.5 and out["edge_cents_per_set"] == 5.74
    assert c["fair_taken_cc"] == 5750                   # Tier 4 reads the fresh fair


@pytest.mark.parametrize("fair,ya,na", [
    (0.70, 0.62, 0.40),          # outside the shrink band
    (0.60, 0.50, 0.50),          # in band, mid 0.50 -> 0.575
    (0.5987654, 0.50, 0.50),     # in band, fair_raw is stored rounded to 4 places
])
def test_finalize_model_edge_unchanged_book_matches_screen_values(fixed_fair, fair, ya, na):
    fixed_fair["fair"] = fair
    c = model_cand(ya=ya, na=na)
    screen_fair = c["fair_taken_cc"]
    want = cs.evaluate_model_edge(leg(round(ya * 100)), 500, screen_fair, 2000, 1000)
    books = {c["ticker"]: {"yes": (round(ya * 10000), 500.0), "no": (round(na * 10000), 500.0)}}
    out = cs.finalize(c, books, {"KXBTC15M": 1.0}, ARGS, "now")
    assert want is not None and c["fair_taken_cc"] == screen_fair
    assert out["fair_cents"] == want["fair_cents"]
    assert out["edge_cents_per_set"] == want["edge_cents_per_set"]


def test_finalize_model_edge_without_fair_raw_uses_screen_fair(fixed_fair):
    fixed_fair["fair"] = 0.60
    books = {"yes": (5100, 500.0), "no": (5100, 500.0)}   # the moved book from above
    c = model_cand(ya=0.52, na=0.48)
    del c["model"]["fair_raw"]
    out = cs.finalize(c, {c["ticker"]: books}, {}, ARGS, "now")
    assert out["fair_cents"] == 58.0 and out["edge_cents_per_set"] == 5.24
    assert c["fair_taken_cc"] == 5800
    c = model_cand(ya=0.52, na=0.48)
    c["model"] = None
    assert cs.finalize(c, {c["ticker"]: books}, {}, ARGS, "now")["fair_cents"] == 58.0
    # the lean check still applies on the fallback path
    c = model_cand(ya=0.52, na=0.48)
    del c["model"]["fair_raw"]
    assert cs.finalize(c, {c["ticker"]: {"yes": (4900, 500.0), "no": (5300, 500.0)}},
                       {}, ARGS, "now") is None


def test_finalize_model_edge_series_fee_multiplier(fixed_fair):
    c1, c2 = model_cand(), model_cand()
    books = {c1["ticker"]: {"yes": (6000, 500.0), "no": (4200, 500.0)}}
    base = cs.finalize(c1, books, {"KXBTC15M": 1.0}, ARGS, "now")
    tripled = cs.finalize(c2, books, {"KXBTC15M": 3.0}, ARGS, "now")
    assert tripled is None or tripled["fees_cents"] > base["fees_cents"]


def test_tier1_path_untouched_by_model_branch():
    m = {"ticker": "MKT", "title": "t", "yes_ask_dollars": "0.4000", "no_ask_dollars": "0.5000",
         "yes_ask_size_fp": "500", "yes_bid_size_fp": "500"}
    cand = cs.screen_type_a([m])[0]
    out = cs.finalize(cand, {"MKT": {"yes": (4000, 500.0), "no": (5000, 500.0)}}, {}, ARGS, "now")
    assert out["tier"] == "locked" and "locked_profit_cents" in out and "model" not in out


def test_constants():
    assert cs.MODEL_EDGE_MIN_CENTS == 5 and cs.MAX_MODEL_ORDERBOOK_FETCHES == 60
    assert cs.MODEL_EDGE_GUARANTEE == \
        "MODEL estimate — NOT guaranteed; fair-value model vs live spot"
    assert cs.DEFAULT_MAX_OUTLAY_DOLLARS == 20.0 and cs.DEFAULT_MIN_OUTLAY_DOLLARS == 10.0


# ---------------------------------------------------------- main / JSON ----

def test_model_take_possible_is_implied_by_the_screen(fixed_fair):
    assert cs.model_take_possible(window(ya=0.62, na=0.40))
    assert cs.model_take_possible(window(ya=0.32, na=0.70))      # NO side
    assert not cs.model_take_possible(window(ya=0.97, na=0.04))  # YES ask > 95c, NO contrarian
    assert not cs.model_take_possible(window(ya=0.04, na=0.97))
    assert cs.model_take_possible(window(ya=0.95, na=0.07))      # exactly 5c under 100
    assert not cs.model_take_possible(window(na=0.0))
    fixed_fair["fair"] = 1.0
    k = kraken_with("XBTUSD")
    for ya, na in ((0.97, 0.04), (0.9501, 0.06), (0.95, 0.07)):
        m = window(ya=ya, na=na)
        if not cs.model_take_possible(m):
            assert cs.screen_model_edge([m], NOW, k, []) == []


def test_kraken_fetch_skips_windows_that_cannot_pass(monkeypatch):
    calls = []
    monkeypatch.setattr(cs, "http_get_json",
                        lambda url, params=None, **kw: calls.append(params) or
                        {"error": [], "result": {"X": []}})
    decided = window(ya=0.98, na=0.03)
    assert cs.fetch_kraken_for_windows([decided], NOW, me.KrakenCache(), []) == 0
    assert calls == []


def run_main(monkeypatch, tmp_path, argv, kraken_fail=False, book_log=None, block=None,
             kraken_delay=0.0):
    """main() with one locked Type A combo and one live BTC 15-min window."""
    monkeypatch.setattr(cs, "STATE_PATH", str(tmp_path / "s.json"))
    real_time = time.time
    # Wall-clock-independent window alignment: shift the clock so "now" sits
    # mid-window. Without this the suite is flaky — in the first ~4 minutes of
    # a 15-minute window no window is both live and past warmup.
    delta = (600 - int(real_time()) % 900) % 900
    monkeypatch.setattr(time, "time", lambda: real_time() + delta)
    now = int(time.time())
    wo = now - now % 900
    if now - wo < 240:                 # keep the window past warmup and live
        wo -= 900
    w = window(w_open=wo, w_close=wo + 900)
    lock = {"ticker": "LOCK", "title": "lock", "yes_ask_dollars": "0.4000",
            "no_ask_dollars": "0.5000", "yes_ask_size_fp": "500", "yes_bid_size_fp": "500"}
    books = {"LOCK": {"yes": (4000, 500.0), "no": (5000, 500.0)},
             w["ticker"]: {"yes": (6200, 500.0), "no": (4000, 500.0)}}
    calls = []

    def fake_trades(pair, since, http=None):
        calls.append(("Trades", pair))
        time.sleep(kraken_delay)
        if kraken_fail:
            raise ConnectionError("kraken down")
        return []

    def fake_candles(pair, http=None):
        calls.append(("OHLC", pair))
        return []
    monkeypatch.setattr(cs, "kalshi_open_markets", lambda now_ts, errors, deadline: [lock, w])
    monkeypatch.setattr(cs, "poly_markets", lambda: [])
    monkeypatch.setattr(cs, "kalshi_orderbook_top", lambda t: books[t])
    monkeypatch.setattr(cs, "kalshi_fee_multiplier", lambda s: 1.0)
    monkeypatch.setattr(me, "kraken_trades", fake_trades)
    monkeypatch.setattr(me, "kraken_candles", fake_candles)
    monkeypatch.setattr(cs.me, "fair_15m", lambda *a: (0.70, 100.0, 101.0, 0.001, 5.0))
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cs.main(argv) == 0
    return json.loads(buf.getvalue()), calls, w["ticker"]


VOLATILE = {"as_of", "sweep_seconds", "detected_at"}


def strip(d):
    if isinstance(d, dict):
        return {k: strip(v) for k, v in d.items() if k not in VOLATILE}
    if isinstance(d, list):
        return [strip(x) for x in d]
    return d


def test_json_adds_model_fields_without_disturbing_existing(monkeypatch, tmp_path):
    on, calls, tk = run_main(monkeypatch, tmp_path, ["--no-mark"])
    off, off_calls, _ = run_main(monkeypatch, tmp_path, ["--no-mark", "--no-model-edge"])
    assert on["model_edge_combos_found"] == 1 and off["model_edge_combos_found"] == 0
    [m] = on["new_model_edge_combos"]
    assert m["tier"] == "model-edge" and m["legs"][0]["ticker"] == tk
    assert off["new_model_edge_combos"] == [] and off_calls == []
    assert calls == [("Trades", "XBTUSD"), ("OHLC", "XBTUSD")]
    # existing fields: same names, same values with or without Tier 3
    assert on["combos_found"] == 1 and on["edge_combos_found"] == 0
    assert [c["tier"] for c in on["new_combos"]] == ["locked"]
    old = set(off) - {"model_edge_combos_found", "new_model_edge_combos"}
    for k in old - {"screen_candidates", "errors"} - VOLATILE:
        a, b = strip(on[k]), strip(off[k])
        if k == "stats" and isinstance(a, dict):
            # fee-mult counters legitimately differ: the model tier adds its
            # series to the multiplier fetch. Everything else must match.
            a = {kk: v for kk, v in a.items()
                 if kk not in ("fee_mult_cache_hits", "fee_mult_fetched")}
            b = {kk: v for kk, v in b.items()
                 if kk not in ("fee_mult_cache_hits", "fee_mult_fetched")}
        assert a == b, k
    assert {k: v for k, v in on["screen_candidates"].items() if not k.startswith("M")} == \
        off["screen_candidates"]
    assert on["screen_candidates"]["M"] == 1 and on["screen_candidates"]["M_live_windows"] == 1
    assert not any(k.startswith("M") for k in off["screen_candidates"])
    assert on["errors"] == off["errors"] == []


def test_model_edge_dedup_with_mark(monkeypatch, tmp_path):
    first, _, tk = run_main(monkeypatch, tmp_path, [])
    second, _, _ = run_main(monkeypatch, tmp_path, [])
    assert len(first["new_model_edge_combos"]) == 1 and second["new_model_edge_combos"] == []
    assert first["repeat_combos_count"] == 0 and second["repeat_combos_count"] == 1
    with open(cs.STATE_PATH) as f:
        keys = set(json.load(f)["alerted"])
    assert keys == {"A|LOCK,LOCK|yes+no", f"model-edge|M|{tk}|YES"}


def test_kraken_failure_skips_model_tier_only(monkeypatch, tmp_path):
    d, calls, _ = run_main(monkeypatch, tmp_path, ["--no-mark"], kraken_fail=True)
    assert d["model_edge_combos_found"] == 0 and d["new_model_edge_combos"] == []
    assert d["combos_found"] == 1 and any("kraken XBTUSD" in e for e in d["errors"])
    assert calls == [("Trades", "XBTUSD")]


def test_slow_kraken_is_cut_off_without_touching_tier1(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "MODEL_KRAKEN_BUDGET_S", 0)
    d, _, _ = run_main(monkeypatch, tmp_path, ["--no-mark"], kraken_delay=0.5)
    assert d["model_edge_combos_found"] == 0 and d["combos_found"] == 1
    assert any("kraken fetch unfinished" in e for e in d["errors"])
    assert [c["tier"] for c in d["new_combos"]] == ["locked"]


def test_bad_args_fallback_json_has_model_fields():
    import subprocess
    script = os.path.join(os.path.dirname(cs.__file__), "combo_scan.py")
    r = subprocess.run([sys.executable, script, "--bogus"], capture_output=True, text=True)
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert r.returncode == 0 and d["model_edge_combos_found"] == 0
    assert d["new_model_edge_combos"] == [] and d["combos_found"] == 0


# --------------------------------------------------------------- backtest ----

def test_backtest_fee_and_rule(tmp_path):
    assert bt.taker_fee_dollars(0.6, 32) == 0.54
    assert bt.taker_fee_dollars(0.5, 1) == 0.02 and bt.taker_fee_dollars(1.0, 5) == 0
    for c in (1, 7, 25, 100):
        for p in (0.01, 0.3, 0.55, 0.99):
            assert round(bt.taker_fee_dollars(p, c) * 100) == cs.taker_fee_cents(p * 100, c)
    rows = [
        {"proxy": "kalshi", "side": "YES", "market_mid": 0.55, "edge": 0.10, "correct": True,
         "graded_at_utc": "2026-10-01T01"},                     # 8c over ask: taken, wins
        {"proxy": "kalshi", "side": "NO", "market_mid": 0.40, "edge": -0.09, "correct": False,
         "graded_at_utc": "2026-10-01T02"},                     # 7c over ask: taken, loses
        {"proxy": "kalshi", "side": "YES", "market_mid": 0.45, "edge": 0.20, "correct": True,
         "graded_at_utc": "2026-10-01T03"},                     # contrarian: skipped
        {"proxy": "kalshi", "side": "YES", "market_mid": 0.60, "edge": 0.06, "correct": True,
         "graded_at_utc": "2026-10-01T04"},                     # 4c over ask: skipped
        {"proxy": "kraken", "side": "YES", "market_mid": 0.6, "edge": 0.3, "correct": True,
         "graded_at_utc": "2026-10-01T05", "excluded_from_accuracy": True},
        {"proxy": "kalshi", "side": "YES", "market_mid": None, "edge": 0.3, "correct": True,
         "graded_at_utc": "2026-10-01T06"},
    ]
    p = tmp_path / "g.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    loaded = bt.load_rows(str(p))
    assert len(loaded) == 4
    res = bt.simulate(loaded, 4.0, 15.0, 5.0)
    assert res["n"] == 2 and res["wins"] == 1
    c1, c2 = math.floor(15 / 0.57), math.floor(15 / 0.62)
    cost = c1 * 0.57 + bt.taker_fee_dollars(0.57, c1) + c2 * 0.62 + bt.taker_fee_dollars(0.62, c2)
    assert res["outlay"] == pytest.approx(cost) and res["pnl"] == pytest.approx(c1 - cost)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert bt.main(["--grades", str(p)]) == 0
    assert "Placebo" in buf.getvalue() and "Threshold sweep" in buf.getvalue()
