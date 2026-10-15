"""Offline tests for combo pricing, fees, depth gating, dedup and parsing."""

import io
import json
import os
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import combo_scan as cs  # noqa: E402


def legs_of(*prices, side="yes"):
    return [{"ticker": f"T{i}", "side": side, "price_cents": p} for i, p in enumerate(prices)]


def yes_no(yes, no, ticker="MKT"):
    return [{"ticker": ticker, "side": "yes", "price_cents": yes},
            {"ticker": ticker, "side": "no", "price_cents": no}]


def listing(ticker, yes_ask, no_ask=None, event="EV", **kw):
    m = {"ticker": ticker, "event_ticker": event, "title": ticker,
         "yes_ask_dollars": f"{yes_ask / 100:.4f}",
         "no_ask_dollars": f"{(no_ask if no_ask is not None else 0) / 100:.4f}",
         "yes_ask_size_fp": "100.00", "yes_bid_size_fp": "100.00",
         "close_time": "2026-10-02T00:00:00Z"}
    m.update(kw)
    return m


# ------------------------------------------------------------------ fees ----

@pytest.mark.parametrize("price,contracts,fee", [
    # Kalshi General Trading Fees Table (fee schedule effective 2026-07-07)
    (1, 1, 1), (20, 1, 2), (50, 1, 2), (85, 1, 1), (99, 1, 1),
    (1, 100, 7), (5, 100, 34), (10, 100, 63), (15, 100, 90), (45, 100, 174),
    (50, 100, 175), (55, 100, 174), (90, 100, 63), (99, 100, 7),
])
def test_fee_matches_official_table(price, contracts, fee):
    assert cs.taker_fee_cents(price, contracts) == fee


def test_fee_boundaries_near_zero_and_max_at_half():
    assert cs.taker_fee_cents(0) == 0 and cs.taker_fee_cents(100) == 0
    assert cs.taker_fee_exact_cents(0) == 0 and cs.taker_fee_exact_cents(100) == 0
    assert cs.taker_fee_exact_cents(1) < 0.1 and cs.taker_fee_exact_cents(99) < 0.1
    assert cs.taker_fee_exact_cents(50) == pytest.approx(1.75)
    assert max(range(1, 100), key=cs.taker_fee_exact_cents) == 50
    assert cs.taker_fee_exact_cents(30) == pytest.approx(cs.taker_fee_exact_cents(70))


def test_fee_multiplier_and_zero_contracts():
    assert cs.taker_fee_cents(50, 100, multiplier=0) == 0
    assert cs.taker_fee_cents(50, 100, multiplier=2) == 350
    assert cs.taker_fee_cents(50, 0) == 0


def test_poly_fee_rate_and_rounding():
    leg = {"ticker": "poly:x", "side": "no", "price_cents": 50,
           "venue": "polymarket", "fee_rate": 0.05}
    assert cs.leg_fee_cc(leg, 100) == 12500           # $1.25 per docs table
    assert cs.leg_fee_cc(dict(leg, fee_rate=0.0), 100) == 0
    assert cs._poly_fee_rate({"feesEnabled": False}) == 0.0
    assert cs._poly_fee_rate({"feesEnabled": True, "feeSchedule": {"rate": 0.04}}) == 0.04
    assert cs._poly_fee_rate({"feesEnabled": True}) == cs.POLY_DEFAULT_TAKER_RATE


def test_poly_fee_rate_fails_closed():
    default = cs.POLY_DEFAULT_TAKER_RATE
    assert cs._poly_fee_rate({}) == default
    assert cs._poly_fee_rate({"feesEnabled": None}) == default
    assert cs._poly_fee_rate({"feesEnabled": 0}) == default
    assert cs._poly_fee_rate({"feesEnabled": False}) == 0.0
    assert cs._poly_fee_rate({"feesEnabled": True, "feeSchedule": {"rate": 0.04}}) == 0.04
    assert cs._poly_fee_rate(
        {"feesEnabled": True, "feeSchedule": {"rate": 0.04, "exponent": 2}}) == default
    assert cs._poly_fee_rate(
        {"feesEnabled": True, "feeSchedule": {"rate": 0.04, "exponent": 0.5}}) == default
    assert cs._poly_fee_rate(
        {"feesEnabled": True, "feeSchedule": {"rate": 0.04, "exponent": "1"}}) == 0.04
    assert cs._poly_fee_rate(
        {"feesEnabled": True, "feeSchedule": {"rate": 0.04, "exponent": "x"}}) == default
    assert cs._poly_fee_rate(
        {"feesEnabled": True, "feeSchedule": '{"rate": 0.05, "exponent": 1}'}) == 0.05
    assert cs._poly_fee_rate({"feesEnabled": True, "feeSchedule": "not json"}) == default
    assert cs._poly_fee_rate({"feesEnabled": True, "feeSchedule": [0.04]}) == default
    assert cs._poly_fee_rate(
        {"feesEnabled": True, "feeSchedule": {"rate": "nan"}}) == default


def _old_poly_fee_rate(m):
    if not m.get("feesEnabled"):
        return 0.0
    fs = m.get("feeSchedule")
    if isinstance(fs, str):
        try:
            fs = json.loads(fs)
        except Exception:
            fs = None
    rate = cs.num((fs or {}).get("rate"))
    return rate if rate > 0 else cs.POLY_DEFAULT_TAKER_RATE


@pytest.mark.parametrize("m", [
    {}, {"feesEnabled": None}, {"feesEnabled": False}, {"feesEnabled": True},
    {"feesEnabled": True, "feeSchedule": {"rate": 0.04}},
    {"feesEnabled": True, "feeSchedule": {"rate": 0.04, "exponent": 1}},
    {"feesEnabled": True, "feeSchedule": {"rate": 0.04, "exponent": 2}},
    {"feesEnabled": True, "feeSchedule": {"rate": 0.04, "exponent": 0.5}},
    {"feesEnabled": True, "feeSchedule": '{"rate": 0.05, "exponent": 1}'},
    {"feesEnabled": True, "feeSchedule": {"rate": 0}},
])
def test_poly_fee_rate_only_tightens(m):
    old, new = _old_poly_fee_rate(m), cs._poly_fee_rate(m)
    assert new >= old
    kleg = {"ticker": "K-1", "side": "yes", "price_cents": 40}
    pleg_old = {"ticker": "poly:x", "side": "no", "price_cents": 55,
                "venue": "polymarket", "fee_rate": old}
    pleg_new = dict(pleg_old, fee_rate=new)
    assert cs.poly_fee_cc(55, 100, new) >= cs.poly_fee_cc(55, 100, old)
    assert cs.screen_edge_cents([kleg, pleg_new]) <= cs.screen_edge_cents([kleg, pleg_old])


def test_poly_fee_rate_no_fee_info_raises_type_c_fee():
    m = {}
    assert _old_poly_fee_rate(m) == 0.0
    assert cs.poly_fee_cc(55, 100, cs._poly_fee_rate(m)) > cs.poly_fee_cc(55, 100, 0.0)


# ---------------------------------------------------------------- type A ----

def test_type_a_par_is_not_a_combo():
    assert cs.evaluate_combo(yes_no(50, 50), 1000, 2000, 1000) is None
    assert cs.screen_type_a([listing("M", 50, 50)]) == []


def test_type_a_profitable_exact_cents():
    r = cs.evaluate_combo(yes_no(40, 50), 1000, 2000, 1000)
    # 22 sets would cost 1980 + fees 37 + 39 = 2056 > 2000, so 21 sets:
    # 21*90 = 1890, fees ceil(35.28)=36 + ceil(36.75)=37 = 73
    assert r["sets"] == 21 and r["affordable_sets"] == 21
    assert r["fees_cents"] == 73
    assert r["outlay_cents"] == 1963
    assert r["locked_profit_cents"] == 137
    assert r["roi_pct"] == 6.98


def test_fee_subtraction_reduces_roi():
    r = cs.evaluate_combo(yes_no(40, 50), 1000, 2000, 1000)
    gross_roi = (100 - 90) / 90 * 100
    assert r["roi_pct"] < gross_roi
    assert r["locked_profit_cents"] < r["sets"] * 10


def test_type_a_screen_flags_crossed_listing_only():
    found = cs.screen_type_a([listing("X", 40, 50), listing("Y", 52, 49), listing("Z", 3, None)])
    assert [c["legs"][0]["ticker"] for c in found] == ["X"]


# ---------------------------------------------------------------- type B ----

def bracket_event(prices, event="EVB"):
    """less / between... / greater markets that tile the number line."""
    n = len(prices)
    ms = []
    for i, p in enumerate(prices):
        if i == 0:
            kw = {"strike_type": "less", "cap_strike": 10}
        elif i == n - 1:
            kw = {"strike_type": "greater", "floor_strike": 10 + 2 * (n - 2) - 1}
        else:
            kw = {"strike_type": "between", "floor_strike": 10 + 2 * (i - 1),
                  "cap_strike": 11 + 2 * (i - 1)}
        ms.append(listing(f"{event}-{i}", p, event=event, **kw))
    return ms


def test_type_b_three_outcomes_under_100_flags():
    ms = bracket_event([30, 30, 30])
    assert cs.brackets_exhaustive(ms)
    found = cs.screen_type_b(ms)
    assert len(found) == 1 and found[0]["event_ticker"] == "EVB"
    r = cs.evaluate_combo(found[0]["legs"], 100, 2000, 1000)
    assert r is not None and r["locked_profit_cents"] > 0


def test_type_b_sum_over_100_does_not_flag():
    assert cs.screen_type_b(bracket_event([35, 35, 35])) == []
    # 97c of asks is under par, but fees (3 x ~1.6c) push it over
    assert cs.screen_type_b(bracket_event([33, 32, 32])) == []


def test_type_b_unverifiable_events_are_skipped_and_counted():
    ladder = [listing(f"L-{i}", 10, event="LAD", strike_type="greater", floor_strike=k)
              for i, k in enumerate((1, 2, 3))]
    named = [listing(f"N-{i}", 10, event="NAM", strike_type="custom") for i in range(3)]
    stats = {}
    assert cs.screen_type_b(ladder + named, stats) == []
    assert stats["B_unverified_exhaustive"] == 2


def test_type_b_too_many_outcomes_is_skipped_and_counted():
    assert cs.MAX_EVENT_LEGS == 40
    stats = {}
    big = bracket_event([1] * 41, event="BIG")
    assert cs.brackets_exhaustive(big)
    assert cs.screen_type_b(big, stats) == [] and stats == {"B_too_many_outcomes": 1}
    stats = {}
    found = cs.screen_type_b(bracket_event([1] * 40, event="MAX"), stats)
    assert len(found) == 1 and "B_too_many_outcomes" not in stats
    stats = {}
    assert cs.screen_type_b([listing("ONE", 1, event="SOLO")], stats) == [] and stats == {}
    assert cs.screen_type_b(big) == []                  # skipped=None is fine


def test_type_b_missing_ask_skips_event():
    ms = bracket_event([30, 30, 30])
    ms[1]["yes_ask_dollars"] = "0.0000"
    assert cs.screen_type_b(ms) == []


def test_brackets_exhaustive_gaps():
    def mk(st, lo=None, hi=None):
        return {"strike_type": st, "floor_strike": lo, "cap_strike": hi}
    weather = [mk("less", hi=81), mk("between", 81, 82), mk("between", 83, 84),
               mk("greater", lo=84)]
    assert cs.brackets_exhaustive(weather)
    gap = [mk("less", hi=81), mk("between", 81, 82), mk("between", 85, 86), mk("greater", lo=86)]
    assert not cs.brackets_exhaustive(gap)
    cents = [mk("less", hi=3.2), mk("between", 3.2, 3.29), mk("greater", lo=3.29)]
    assert cs.brackets_exhaustive(cents)
    assert not cs.brackets_exhaustive([mk("less", hi=3.19), mk("between", 3.25, 3.29),
                                       mk("greater", lo=3.29)])
    assert not cs.brackets_exhaustive([mk("between", 1, 2), mk("greater", lo=2)])


def test_brackets_exhaustive_strict_bounds_need_no_gap():
    def mk(st, lo=None, hi=None):
        return {"strike_type": st, "floor_strike": lo, "cap_strike": hi}
    # Live Kalshi shapes (2026-10-02): KXHIGHNY "less" cap 81 = "80° or below",
    # "greater" floor 88 = "89° or above"; KXINXY "less" cap 4600 = "4,599.99 or below".
    highny = [mk("less", hi=81), mk("between", 81, 82), mk("between", 83, 84),
              mk("between", 85, 86), mk("between", 87, 88), mk("greater", lo=88)]
    assert cs.brackets_exhaustive(highny)
    inxy = [mk("less", hi=4600), mk("between", 4600, 4799.99), mk("between", 4800, 4999.99),
            mk("between", 5000, 5200), mk("greater", lo=5200)]
    assert cs.brackets_exhaustive(inxy)
    # value < 3.19 or 3.20..3.29 or > 3.29: exactly 3.19 pays nothing
    assert not cs.brackets_exhaustive([mk("less", hi=3.19), mk("between", 3.2, 3.29),
                                       mk("greater", lo=3.29)])
    # 81 or below, then > 82 strictly: 82 pays nothing
    assert not cs.brackets_exhaustive([mk("less", hi=81), mk("between", 81, 82),
                                       mk("greater", lo=83)])
    # < 81 and > 81: exactly 81 pays nothing; inclusive on either side closes it
    assert not cs.brackets_exhaustive([mk("less", hi=81), mk("greater", lo=81)])
    assert cs.brackets_exhaustive([mk("less_or_equal", hi=81), mk("greater", lo=81)])
    assert cs.brackets_exhaustive([mk("less", hi=81), mk("greater_or_equal", lo=81)])
    assert cs.brackets_exhaustive([mk("less", hi=82), mk("greater", lo=81)])
    # two inclusive bounds still get the one-step reporting-unit allowance
    assert cs.brackets_exhaustive([mk("less_or_equal", hi=80), mk("between", 81, 82),
                                   mk("greater_or_equal", lo=83)])


def test_brackets_exhaustive_malformed_strikes_fail_closed():
    def mk(st, lo=None, hi=None):
        return {"strike_type": st, "floor_strike": lo, "cap_strike": hi}

    def ladder(cap=81, b_lo=81, b_hi=82, floor=82):
        return [mk("less", hi=cap), mk("between", b_lo, b_hi), mk("greater", lo=floor)]

    assert cs.brackets_exhaustive(ladder())
    assert cs.brackets_exhaustive(ladder(cap=81.0, b_lo=81.0, b_hi=82.0, floor=82.0))
    for bad in ("abc", True, float("nan"), float("inf"), "81", [81]):
        assert cs.brackets_exhaustive(ladder(cap=bad)) is False
        assert cs.brackets_exhaustive(ladder(floor=bad)) is False
        assert cs.brackets_exhaustive(ladder(b_lo=bad)) is False
        assert cs.brackets_exhaustive(ladder(b_hi=bad)) is False
    assert cs.brackets_exhaustive([mk("less_or_equal", hi=False), mk("greater", lo=0)]) is False


def test_type_b_malformed_strike_counts_unverified_not_raises():
    ms = bracket_event([30, 30, 30])
    ms[0]["cap_strike"] = "abc"
    stats = {}
    assert cs.screen_type_b(ms, stats) == [] and stats["B_unverified_exhaustive"] == 1


def test_type_b_strict_bound_gap_is_not_flagged():
    ms = bracket_event([30, 30, 30])
    assert len(cs.screen_type_b(ms)) == 1
    ms[0]["cap_strike"] = 9                      # "< 9" leaves 9 uncovered before "10-11"
    stats = {}
    assert cs.screen_type_b(ms, stats) == [] and stats["B_unverified_exhaustive"] == 1


# --------------------------------------------------------- depth gating ----

def test_shallow_book_rejects():
    assert cs.evaluate_combo(yes_no(40, 50), 5, 2000, 1000) is None


def test_deep_book_passes():
    r = cs.evaluate_combo(yes_no(40, 50), 10_000, 2000, 1000)
    assert r is not None and r["sets"] == r["affordable_sets"] == 21


def test_partial_fill_uses_min_of_depth_and_affordable():
    r = cs.evaluate_combo(yes_no(40, 50), 15, 2000, 1000)
    assert r["depth_sets"] == 15 and r["affordable_sets"] == 21
    assert r["sets"] == min(15, 21) == 15
    # 15*90 = 1350 + fees ceil(25.2)=26 + ceil(26.25)=27
    assert r["outlay_cents"] == 1403 and r["locked_profit_cents"] == 97


def test_affordable_sets_matches_floor_of_per_set_cost():
    legs = yes_no(40, 50)
    s = cs.affordable_sets(legs, 2000)
    per_set = cs.combo_cost_cc(legs, s) / s / 100
    assert s == int(2000 // per_set)


def test_min_roi_gate():
    assert cs.evaluate_combo(yes_no(40, 50), 1000, 2000, 1000, min_roi=7.0) is None
    assert cs.evaluate_combo(yes_no(40, 50), 1000, 2000, 1000, min_roi=6.9) is not None


# -------------------------------------------------------------- rounding ----

def test_cent_rounding_never_flags_non_positive_profit():
    legs = yes_no(2, 97)
    assert cs.screen_edge_cents(legs) > 0          # unrounded fees: looks profitable
    assert cs.evaluate_combo(legs, 1, 2000, 0) is None   # 99 + 1 + 1 = 101
    assert cs.evaluate_combo(legs, 2, 2000, 0) is None   # profit exactly 0
    r = cs.evaluate_combo(legs, 3, 2000, 0)
    assert r["locked_profit_cents"] == 1 and r["roi_pct"] > 0


@pytest.mark.parametrize("yes", range(1, 100))
def test_no_flag_ever_has_non_positive_profit(yes):
    for no in range(1, 100 - yes + 1):
        for depth in (1, 7, 1000):
            r = cs.evaluate_combo(yes_no(yes, no), depth, 2000, 0)
            if r is not None:
                assert r["locked_profit_cents"] > 0 and r["roi_pct"] > 0
                assert r["outlay_cents"] <= 2000


# ----------------------------------------------------------------- dedup ----

def combo(tickers=("B", "A"), ctype="B", side="yes", roi=5.0, profit=10):
    return {"combo_type": ctype, "side": side, "roi_pct": roi, "locked_profit_cents": profit,
            "expiry": "2099-01-01T00:00:00Z",
            "legs": [{"ticker": t, "side": "yes", "price_cents": 30} for t in tickers]}


def test_dedup_second_run_is_repeat(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "STATE_PATH", str(tmp_path / "state" / "s.json"))
    alerted = cs.load_state()["alerted"]
    new, rep = cs.split_new([combo()], alerted, "t1", mark=True)
    assert len(new) == 1 and rep == 0
    cs.save_state({"alerted": alerted})
    alerted2 = cs.load_state()["alerted"]
    new2, rep2 = cs.split_new([combo(tickers=("A", "B"))], alerted2, "t2", mark=True)
    assert new2 == [] and rep2 == 1


def test_dedup_key_includes_type_and_side():
    assert cs.combo_key("B", ["b", "a"], "yes") == cs.combo_key("B", ["a", "b"], "yes")
    assert cs.combo_key("B", ["a"], "yes") != cs.combo_key("A", ["a"], "yes")
    assert cs.combo_key("C", ["a"], "kalshi_yes+poly_no") != cs.combo_key("C", ["a"], "kalshi_no+poly_yes")


def test_no_mark_never_stamps():
    alerted = {}
    new, rep = cs.split_new([combo()], alerted, "t", mark=False)
    assert len(new) == 1 and alerted == {}


def test_state_backup_restores_on_corrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "STATE_PATH", str(tmp_path / "s.json"))
    cs.save_state({"alerted": {"k1": {}}})
    cs.save_state({"alerted": {"k1": {}, "k2": {}}})
    assert os.path.exists(cs.STATE_PATH + ".bak")
    with open(cs.STATE_PATH, "w") as f:
        f.write("{corrupt")
    assert set(cs.load_state()["alerted"]) == {"k1"}


def test_prune_drops_long_expired():
    alerted = {"old": {"expiry": "2020-01-01T00:00:00Z"}, "live": {"expiry": "2099-01-01T00:00:00Z"},
               "noexp": {}}
    cs.prune_alerted(alerted, 1_790_000_000)
    assert set(alerted) == {"live", "noexp"}


# --------------------------------------------------------------- ranking ----

def test_rank_by_roi_then_profit():
    cs_ = [combo(("a",), roi=5, profit=10), combo(("b",), roi=9, profit=1),
           combo(("c",), roi=5, profit=50)]
    assert [c["legs"][0]["ticker"] for c in cs.rank_combos(cs_)] == ["b", "c", "a"]


# ------------------------------------------------------ parsing / sweep ----

def test_orderbook_top_derives_asks_from_opposite_bids(monkeypatch):
    book = {"orderbook_fp": {"no_dollars": [["0.9600", "148.87"]],
                             "yes_dollars": [["0.0300", "23.12"]]}}
    monkeypatch.setattr(cs, "http_get_json", lambda *a, **k: book)
    top = cs.kalshi_orderbook_top("T")
    assert top["yes"] == (400, 148.87) and top["no"] == (9700, 23.12)


def test_listing_quote_treats_zero_and_par_as_no_offer():
    ya, _, na, _ = cs.quote({"yes_ask_dollars": "0.0000", "no_ask_dollars": "1.0000"})
    assert ya is None and na is None


def test_split_range_isolates_point_mass():
    a, b = cs.split_range(0, 1000, [500] * 10)
    assert a == (0, 499) and b == (500, 1000)
    a, b = cs.split_range(500, 1000, [500] * 10)
    assert a == (500, 500) and b == (501, 1000)
    assert cs.split_range(500, 500, [500]) is None
    assert all(isinstance(x, int) for x in a + b)


def test_seed_ranges_are_disjoint_and_cover():
    rs = cs.seed_ranges(1_790_000_000)
    assert rs[0][0] == cs.CLOSE_TS_MIN and rs[-1][1] == cs.CLOSE_TS_MAX
    for (_, hi), (lo, _) in zip(rs, rs[1:]):
        assert lo == hi + 1


def test_same_proposition_guards():
    km = {"title": "Who will win the Ballon d'Or in 2026?", "yes_sub_title": "Lamine Yamal"}
    kn = cs.kalshi_match_text(km)
    good = cs.normalize_title("Ballon d'Or Winner 2026: Will Lamine Yamal win the 2026 Ballon d'Or?")
    wrong = cs.normalize_title("Ballon d'Or Winner 2026: Will Vitinha win the 2026 Ballon d'Or?")
    assert cs.same_proposition(km, kn, good) and not cs.same_proposition(km, kn, wrong)
    k1 = {"title": "Will Lula win the first round of the 2026 Brazilian presidential election?",
          "yes_sub_title": ""}
    p1 = cs.normalize_title("Will Lula win the 2026 Brazilian presidential election?")
    assert not cs.same_proposition(k1, cs.kalshi_match_text(k1), p1)


def test_nfkd_fold_strips_accents_in_both_forms():
    assert cs._nfkd_fold("Mbapp\u00e9") == "Mbappe"
    assert cs._nfkd_fold("Mbappe\u0301") == "Mbappe"
    assert cs._nfkd_fold(None) == ""


def _poly_texts(raw):
    return cs.normalize_title(raw), cs.normalize_title(cs._nfkd_fold(raw))


def test_same_proposition_accent_form_mismatch():
    km = {"title": "Who will win the Ballon d'Or in 2026?", "yes_sub_title": "Kylian Mbappe\u0301"}
    kn = cs.kalshi_match_text(km)
    pn, pn_fold = _poly_texts("Ballon d'Or Winner 2026: Will Kylian Mbapp\u00e9 win the 2026 Ballon d'Or?")
    assert not cs.same_proposition(km, kn, pn)
    assert cs.same_proposition(km, kn, pn, pn_fold)
    ka = dict(km, yes_sub_title="Kylian Mbappe")  # live Kalshi form: plain ASCII
    assert cs.same_proposition(ka, cs.kalshi_match_text(ka), pn, pn_fold)
    wn, wn_fold = _poly_texts("Ballon d'Or Winner 2026: Will Ousmane Demb\u00e9l\u00e9 win the 2026 Ballon d'Or?")
    assert not cs.same_proposition(km, kn, wn, wn_fold)


def test_same_proposition_ascii_pair_unchanged_by_fold():
    km = {"title": "Who will win the Ballon d'Or in 2026?", "yes_sub_title": "Lamine Yamal"}
    kn = cs.kalshi_match_text(km)
    pn, pn_fold = _poly_texts("Ballon d'Or Winner 2026: Will Lamine Yamal win the 2026 Ballon d'Or?")
    assert pn == pn_fold
    assert cs.same_proposition(km, kn, pn) and cs.same_proposition(km, kn, pn, pn_fold)


def test_num_tokens_counts_ordinals_as_numbers():
    assert cs._num_tokens({"finish", "4th", "2026"}) == {"4th", "2026"}
    assert cs._num_tokens({"1st", "2nd", "3rd", "21st", "place", "third"}) == \
        {"1st", "2nd", "3rd", "21st"}
    assert cs._num_tokens({"th", "4x", "st"}) == set()


_BR_ROUND = "in the first round of the 2026 Brazilian presidential election?"
_BR_POLY_3RD = ("Brazil presidential election first round 3rd place: Will Pablo Mar\u00e7al "
                "finish in third place " + _BR_ROUND)


def test_same_proposition_rejects_ordinal_placement_mismatch():
    km = {"title": "Will Pablo Mar\u00e7al finish 4th " + _BR_ROUND,
          "yes_sub_title": "Pablo Mar\u00e7al"}
    kn = cs.kalshi_match_text(km)
    pn, pn_fold = _poly_texts(_BR_POLY_3RD)
    assert not cs.same_proposition(km, kn, pn)
    assert not cs.same_proposition(km, kn, pn, pn_fold)


def test_same_proposition_keeps_matching_ordinal():
    km = {"title": "Will Pablo Mar\u00e7al finish 3rd " + _BR_ROUND,
          "yes_sub_title": "Pablo Mar\u00e7al"}
    kn = cs.kalshi_match_text(km)
    pn, pn_fold = _poly_texts(_BR_POLY_3RD)
    assert cs.same_proposition(km, kn, pn, pn_fold)


def test_type_c_matches_across_accent_forms(monkeypatch):
    no_fee_screen(monkeypatch)
    km, pm = xvenue_pair(50)
    km[0]["title"] = "Who will win the Ballon d'Or in 2026?"
    km[0]["yes_sub_title"] = "Kylian Mbappe\u0301"
    pm[0]["event"] = "Ballon d'Or Winner 2026"
    pm[0]["question"] = "Will Kylian Mbapp\u00e9 win the 2026 Ballon d'Or?"
    found = cs.screen_type_c(km, pm, edge_floor=cs.XVENUE_EDGE_MIN_CENTS)
    assert found and found[0]["poly_market"].endswith("Mbapp\u00e9 win the 2026 Ballon d'Or?")


def test_finalize_refreshes_prices_from_book_and_gates_depth():
    m = listing("MKT", 40, 50, expected_expiration_time="2026-10-03T00:00:00Z")
    cand = cs.screen_type_a([m])[0]
    args = SimpleNamespace(max_outlay_cents=2000, min_outlay_cents=1000, min_roi=0.0)
    books = {"MKT": {"yes": (4000, 500.0), "no": (5000, 15.0)}}
    out = cs.finalize(cand, books, {"MKT": 1.0}, args, "now")
    assert out["sets"] == 15 and out["depth_sets"] == 15 and out["combo_type"] == "A"
    assert out["expiry"] == "2026-10-03T00:00:00Z" and out["detected_at"] == "now"
    assert {l["side"] for l in out["legs"]} == {"yes", "no"}
    # book moved against us since the listing: no longer a combo
    cand = cs.screen_type_a([m])[0]
    assert cs.finalize(cand, {"MKT": {"yes": (5000, 500.0), "no": (5000, 500.0)}},
                       {}, args, "now") is None


def test_main_prints_json_and_survives_scan_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "STATE_PATH", str(tmp_path / "s.json"))

    def boom(args, errors):
        raise RuntimeError("network down")
    monkeypatch.setattr(cs, "run_scan", boom)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cs.main(["--no-mark"]) == 0
    d = json.loads(buf.getvalue())
    assert d["markets_scanned"] == 0 and d["new_combos"] == []
    assert d["fees_model"].startswith("kalshi-taker-")
    assert any("network down" in e for e in d["errors"])
    assert not os.path.exists(cs.STATE_PATH)


# ---------------------------------------------------------- tier 2: edge ----

def no_fee_screen(monkeypatch):
    """Zero screening fees so combined prices hit tier boundaries exactly."""
    monkeypatch.setattr(cs, "leg_fee_lb_cents", lambda leg: 0.0)


@pytest.mark.parametrize("no,tier", [
    (50.0, "locked"),     # 90c pre-fee + ~3.4c fees: Tier 1 stays locked
    (59.9, "edge"),       # 99.9c pre-fee: near-arb (fees ~3.4c make it a loss)
    (60.0, None),         # 100.0c pre-fee: rejected (strict bound)
    (61.5, None),         # 101.5c: the old degenerate edge bound, now rejected
])
def test_type_a_tier_boundary_pre_fee(no, tier):
    found = cs.screen_type_a([listing("M", 40, no)], near_arb=True)
    assert [c["tier"] for c in found] == ([tier] if tier else [])
    if tier == "edge":
        assert found[0]["edge"] == pytest.approx(0.1)
        assert cs.screen_edge_cents(found[0]["legs"]) <= 0


def test_type_a_near_arb_with_real_fees():
    ms = [listing("LOCK", 40, 50),     # 90c + ~3.4c fees -> locked
          listing("NEAR", 49, 50),     # 99c pre-fee + ~3.5c fees -> near-arb edge
          listing("PAR", 2, 98),       # 100c pre-fee -> rejected
          listing("OLD", 2, 99),       # 101c: guaranteed loss, rejected
          listing("WIDE", 50, 51)]     # 101c + fees -> rejected
    found = cs.screen_type_a(ms, near_arb=True)
    assert {c["legs"][0]["ticker"]: c["tier"] for c in found} == {"LOCK": "locked", "NEAR": "edge"}
    near = next(c for c in found if c["tier"] == "edge")
    assert near["edge"] == 1.0 and cs.screen_edge_cents(near["legs"]) < 0


def test_type_a_near_arb_candidates_rank_by_pre_fee_edge():
    ms = [listing("A1", 49, 50.5), listing("A2", 48, 50), listing("A3", 49, 50)]
    found = sorted(cs.screen_type_a(ms, near_arb=True), key=lambda c: -c["edge"])
    assert [(c["legs"][0]["ticker"], c["edge"]) for c in found] == [
        ("A2", 2.0), ("A3", 1.0), ("A1", 0.5)]


def test_type_a_without_near_arb_is_tier1_only():
    ms = [listing("LOCK", 40, 50), listing("NEAR", 49, 50)]
    found = cs.screen_type_a(ms)
    assert [(c["legs"][0]["ticker"], c["tier"]) for c in found] == [("LOCK", "locked")]


def xvenue_pair(poly_no_cents):
    km = listing("KXALPHA-26", 40, None, title="Will Team Alpha win the 2026 championship?",
                 volume_fp="1000")
    pm = {"event": "Team Alpha championship 2026",
          "question": "Will Team Alpha win the 2026 championship?", "slug": "alpha-2026",
          "yes_ask_cc": None, "no_ask_cc": cs.to_cc(poly_no_cents / 100),
          "yes_token": "ytok", "no_token": "ntok", "fee_rate": 0.0,
          "end_ts": cs._parse_ts("2026-10-02T00:00:00Z"), "volume": 10_000}
    return [km], [pm]


@pytest.mark.parametrize("poly_no,tier,tier1_only", [
    (52.0, "locked", "locked"),   # 8.0c gap: locked bar is unchanged
    (52.1, "edge", None),         # 7.9c gap: edge, not locked
    (56.0, "edge", None),         # 4.0c gap: inclusive edge bound
    (56.1, None, None),           # 3.9c gap: rejected
])
def test_type_c_tier_boundaries(monkeypatch, poly_no, tier, tier1_only):
    no_fee_screen(monkeypatch)
    km, pm = xvenue_pair(poly_no)
    found = cs.screen_type_c(km, pm, edge_floor=cs.XVENUE_EDGE_MIN_CENTS)
    assert [c["tier"] for c in found] == ([tier] if tier else [])
    assert [c["tier"] for c in cs.screen_type_c(km, pm)] == ([tier1_only] if tier1_only else [])


def test_edge_constants():
    assert cs.XVENUE_EDGE_MIN_CENTS == 4 and cs.XVENUE_MIN_EDGE_CENTS == 8
    assert cs.A_NEAR_ARB_MAX_ASK_CENTS == 100 and cs.MAX_CANDIDATES == 40
    assert cs.EDGE_EVAL_MIN_CENTS == {"A": 0.0, "C": 0.0}


def test_evaluate_edge_pre_fee_reports_positive_pre_fee_edge():
    r = cs.evaluate_edge(yes_no(49, 50), 500, 2000, 1000, cs.EDGE_EVAL_MIN_CENTS["A"],
                         pre_fee=True)
    # 19 sets: 19*99 = 1881 + fees ceil(33.24)=34 + ceil(33.25)=34 = 1949
    assert r["sets"] == 19 and r["outlay_cents"] == 1949 and r["fees_cents"] == 68
    assert r["edge_cents_per_set"] == 1          # 100c - 99c, before fees
    assert 19 * 100 < r["outlay_cents"]          # taker fees erase it
    assert "locked_profit_cents" not in r and "roi_pct" not in r
    assert cs.evaluate_combo(yes_no(49, 50), 500, 2000, 1000) is None


def test_evaluate_edge_pre_fee_rejects_par_loss_and_shallow_books():
    a = cs.EDGE_EVAL_MIN_CENTS["A"]
    assert cs.evaluate_edge(yes_no(50, 50), 500, 2000, 1000, a, pre_fee=True) is None
    assert cs.evaluate_edge(yes_no(2, 99), 500, 2000, 1000, a, pre_fee=True) is None
    assert cs.evaluate_edge(yes_no(49, 50), 5, 2000, 1000, a, pre_fee=True) is None
    r = cs.evaluate_edge(yes_no(49.95, 50), 500, 2000, 1000, a, pre_fee=True)
    assert r["edge_cents_per_set"] == 0.05


def test_evaluate_edge_after_fee_mode_unchanged_for_type_c():
    legs = [{"ticker": "K", "side": "yes", "price_cents": 40},
            {"ticker": "poly:x", "side": "no", "price_cents": 54, "venue": "polymarket",
             "fee_rate": 0.0}]
    r = cs.evaluate_edge(legs, 500, 2000, 1000, cs.EDGE_EVAL_MIN_CENTS["C"])
    # 21 sets: 21*94 = 1974 + Kalshi fee ceil(35.28)=36 = 2010 > 2000 -> 20 sets:
    # 20*94 = 1880 + ceil(33.6)=34 = 1914; edge = (2000-1914)/20 = 4.3
    assert r["sets"] == 20 and r["edge_cents_per_set"] == 4.3
    assert cs.evaluate_edge(legs, 500, 2000, 1000, 5.0) is None


def test_evaluate_edge_after_fee_rejects_exact_zero_edge():
    # 1 set: 48+48 = 96 + fees ceil(1.7472)=2 per leg = 100 -> profit exactly 0
    assert cs.evaluate_edge(yes_no(48, 48), 1, 2000, 100, 0.0) is None


def test_evaluate_edge_after_fee_keeps_one_cent_edge():
    # 1 set: 47+48 = 95 + fees 2+2 = 99 -> profit 1c (min_outlay 99 so _fill keeps it)
    r = cs.evaluate_edge(yes_no(47, 48), 1, 2000, 99, 0.0)
    assert r is not None and r["edge_cents_per_set"] == 1.0


def test_evaluate_combo_unchanged_by_refactor():
    r = cs.evaluate_combo(yes_no(40, 50), 1000, 2000, 1000)
    assert list(r) == ["sets", "depth_sets", "affordable_sets", "outlay_cents", "fees_cents",
                       "combo_cost_cents", "locked_profit_cents", "roi_pct"]


def test_finalize_edge_entry_fields():
    m = listing("EDGE", 49, 50)
    cand = cs.screen_type_a([m], near_arb=True)[0]
    args = SimpleNamespace(max_outlay_cents=2000, min_outlay_cents=1000, min_roi=5.0)
    out = cs.finalize(cand, {"EDGE": {"yes": (4900, 500.0), "no": (5000, 500.0)}}, {}, args, "now")
    assert out["tier"] == "edge" and out["guarantee"] == cs.A_EDGE_GUARANTEE
    assert out["guarantee"].startswith("near-arb — profitable before taker fees")
    assert out["edge_cents_per_set"] == 1 and "locked_profit_cents" not in out


def test_finalize_near_arb_dropped_when_book_is_at_par():
    cand = cs.screen_type_a([listing("EDGE", 49, 50)], near_arb=True)[0]
    args = SimpleNamespace(max_outlay_cents=2000, min_outlay_cents=1000, min_roi=0.0)
    books = {"EDGE": {"yes": (5000, 500.0), "no": (5000, 500.0)}}
    assert cs.finalize(cand, books, {}, args, "now") is None


def test_dedup_key_separates_tiers_and_keeps_old_locked_format():
    assert cs.combo_key("A", ["X"], "yes+no") == "A|X|yes+no"
    assert cs.combo_key("A", ["X"], "yes+no", "locked") == "A|X|yes+no"
    edge_key = cs.combo_key("A", ["X"], "yes+no", "edge")
    assert edge_key == "edge|A|X|yes+no" and edge_key != cs.combo_key("A", ["X"], "yes+no")
    alerted = {}
    locked_c = combo(("X",), ctype="A", side="yes+no")
    edge_c = dict(combo(("X",), ctype="A", side="yes+no"), tier="edge")
    new, rep = cs.split_new([locked_c, edge_c], alerted, "t1", mark=True)
    assert len(new) == 2 and rep == 0 and set(alerted) == {"A|X|yes+no", "edge|A|X|yes+no"}
    assert cs.split_new([edge_c], alerted, "t2", mark=True) == ([], 1)


def test_rank_keeps_locked_order_and_sorts_edge_by_size():
    locked = [combo(("a",), roi=5, profit=10), combo(("b",), roi=9, profit=1),
              combo(("c",), roi=5, profit=50)]
    assert [c["legs"][0]["ticker"] for c in cs.rank_combos(locked)] == ["b", "c", "a"]
    edges = [{"tier": "edge", "edge_cents_per_set": e, "legs": [{"ticker": t}]}
             for t, e in (("x", -1.2), ("y", 4.5), ("z", 0.3))]
    assert [c["legs"][0]["ticker"] for c in cs.rank_combos(edges)] == ["y", "z", "x"]
    mixed = [{"tier": "edge", "combo_type": t, "edge_cents_per_set": e, "legs": [{"ticker": k}]}
             for k, t, e in (("a1", "A", 0.5), ("c1", "C", 4.2), ("a2", "A", 6.0),
                             ("c2", "C", 7.5))]
    assert [c["legs"][0]["ticker"] for c in cs.rank_combos(mixed)] == ["c2", "c1", "a2", "a1"]


def run_main_stubbed(monkeypatch, tmp_path, argv, fee_mult=lambda s: 1.0):
    """main() against canned listings/books: one locked A, one edge A, one edge C."""
    monkeypatch.setattr(cs, "STATE_PATH", str(tmp_path / "s.json"))
    km, pm = xvenue_pair(54.0)
    markets = [listing("LOCK", 40, 50), listing("EDGE", 49, 50), listing("OLD", 2, 99)] + km
    books = {"LOCK": {"yes": (4000, 500.0), "no": (5000, 500.0)},
             "EDGE": {"yes": (4900, 500.0), "no": (5000, 500.0)},
             "KXALPHA-26": {"yes": (4000, 500.0), "no": (None, 0.0)}}
    monkeypatch.setattr(cs, "kalshi_open_markets", lambda now_ts, errors, deadline: markets)
    monkeypatch.setattr(cs, "poly_markets", lambda: pm)
    monkeypatch.setattr(cs, "kalshi_orderbook_top", lambda t: books[t])
    monkeypatch.setattr(cs, "poly_best_ask", lambda tok: (5400, 1000.0))
    monkeypatch.setattr(cs, "kalshi_fee_multiplier", fee_mult)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cs.main(argv) == 0
    return json.loads(buf.getvalue())


def test_unknown_fee_multiplier_drops_candidate_before_finalize(monkeypatch, tmp_path):
    def fee_mult(s):
        if s == "LOCK":
            cs._FEE_MULT_FAILED.add(s)
        return 1.0
    d = run_main_stubbed(monkeypatch, tmp_path, ["--no-mark"], fee_mult)
    assert [c["legs"][0]["ticker"] for c in d["new_combos"]] == ["KXALPHA-26", "EDGE"]
    assert d["combos_found"] == 0 and d["edge_combos_found"] == 2
    assert d["stats"]["fee_mult_unknown"] == ["LOCK"]
    assert d["stats"]["fee_mult_unknown_rejects"] == {"cands": 1, "edge_cands": 0,
                                                      "model_cands": 0}
    cs._FEE_MULT_FAILED.clear()   # the stub never clears; real successes do
    clean = run_main_stubbed(monkeypatch, tmp_path, ["--no-mark"])
    assert "fee_mult_unknown" not in clean["stats"]
    assert "fee_mult_unknown_rejects" not in clean["stats"]


TIER1_FIELDS = {"combo_type", "tier", "side", "legs", "sets", "depth_sets", "affordable_sets",
                "outlay_cents", "fees_cents", "combo_cost_cents", "locked_profit_cents",
                "roi_pct", "expiry", "detected_at", "title"}


def test_no_edge_tier_suppresses_all_tier2_output(monkeypatch, tmp_path):
    d = run_main_stubbed(monkeypatch, tmp_path, ["--no-mark", "--no-edge-tier"])
    assert d["combos_found"] == 1 and d["edge_combos_found"] == 0
    assert d["new_edge_combos"] == []
    assert [c["tier"] for c in d["new_combos"]] == ["locked"]
    assert d["screen_candidates"]["A_edge"] == 0 and d["screen_candidates"]["C_edge"] == 0
    assert d["screen_candidates"]["A"] == 1 and d["screen_candidates"]["C"] == 0
    assert not any("tier 2" in e for e in d["errors"])


def test_edge_tier_output_locked_first_and_tier1_unchanged(monkeypatch, tmp_path):
    off = run_main_stubbed(monkeypatch, tmp_path, ["--no-mark", "--no-edge-tier"])
    on = run_main_stubbed(monkeypatch, tmp_path, ["--no-mark"])
    assert on["combos_found"] == off["combos_found"] == 1
    assert on["edge_combos_found"] == 2 and len(on["new_edge_combos"]) == 2
    assert [c["tier"] for c in on["new_combos"]] == ["locked", "edge", "edge"]
    assert [c["combo_type"] for c in on["new_edge_combos"]] == ["C", "A"]   # edge desc
    assert on["screen_candidates"]["A_edge"] == 1 and on["screen_candidates"]["C_edge"] == 1

    def strip(c):
        return {k: v for k, v in c.items() if k != "detected_at"}
    lock_on, lock_off = on["new_combos"][0], off["new_combos"][0]
    assert set(lock_on) == TIER1_FIELDS | {"depth_reverified"} and strip(lock_on) == strip(lock_off)
    sized = cs.evaluate_combo(yes_no(40, 50), 500, 2000, 1000)
    assert {k: lock_on[k] for k in sized} == sized
    for e in on["new_edge_combos"]:
        want = cs.A_EDGE_GUARANTEE if e["combo_type"] == "A" else cs.EDGE_GUARANTEE
        assert "locked_profit_cents" not in e and e["guarantee"] == want
        assert e["outlay_cents"] >= 1000 and e["edge_cents_per_set"] > 0
    assert [e["legs"][0]["ticker"] for e in on["new_edge_combos"]
            if e["combo_type"] == "A"] == ["EDGE"]
    old_top = {"as_of", "markets_scanned", "combos_found", "new_combos", "repeat_combos_count",
               "sweep_seconds", "fees_model", "max_outlay_cents", "min_outlay_cents",
               "min_roi_pct", "screen_candidates", "errors"}
    assert old_top <= set(on) and old_top <= set(off)


def test_edge_dedup_end_to_end_with_mark(monkeypatch, tmp_path):
    first = run_main_stubbed(monkeypatch, tmp_path, [])
    second = run_main_stubbed(monkeypatch, tmp_path, [])
    assert len(first["new_combos"]) == 3 and second["new_combos"] == []
    assert second["repeat_combos_count"] == 3
    with open(cs.STATE_PATH) as f:
        keys = set(json.load(f)["alerted"])
    assert keys == {"A|LOCK,LOCK|yes+no", "edge|A|EDGE,EDGE|yes+no",
                    "edge|C|KXALPHA-26,poly:alpha-2026|kalshi_yes+poly_no"}


# ------------------------------------------------- type D: ladder pairs ----

LADDER_GUARANTEE = ("cumulative threshold ladder; the two legs always pay >= $1 "
                    "on the same settlement value")


def ladder(st, quotes, event="LAD", title="Highest temperature in NYC today?"):
    """One event of same-title markets; quotes = {strike: (yes_ask, no_ask)}."""
    field = "cap_strike" if st == "less" else "floor_strike"
    return [listing(f"{event}-{k}", y, n, event=event, title=title, strike_type=st, **{field: k})
            for k, (y, n) in quotes.items()]


def leg_sides(c):
    return [(l["ticker"], l["side"]) for l in c["legs"]]


def test_type_d_below_ladder_buys_yes_hi_and_no_lo():
    stats = {}
    found = cs.screen_type_d(ladder("less", {10: (40, 40), 20: (40, 40)}), stats)
    assert [leg_sides(c) for c in found] == [[("LAD-20", "yes"), ("LAD-10", "no")]]
    c = found[0]
    assert (c["combo_type"], c["side"], c["tier"], c["event_ticker"]) == ("D", "yes+no", "locked", "LAD")
    assert [m["ticker"] for m in c["markets"]] == ["LAD-20", "LAD-10"]
    assert [l["price_cents"] for l in c["legs"]] == [40, 40] and c["edge"] > 0
    assert stats["D_ladder_events"] == 1


def test_type_d_below_ladder_never_emits_reverse_pair():
    # NO(20) + YES(10) is only 40c, but it pays 0 when 10 <= value < 20
    ms = ladder("less", {10: (20, 70), 20: (70, 20)})
    assert cs.screen_type_d(ms) == []


def test_type_d_above_ladder_buys_yes_lo_and_no_hi():
    found = cs.screen_type_d(ladder("greater", {80: (40, 40), 90: (40, 40)}))
    assert [leg_sides(c) for c in found] == [[("LAD-80", "yes"), ("LAD-90", "no")]]
    # YES(90) + NO(80) pays 0 when 80 < value <= 90
    assert cs.screen_type_d(ladder("greater", {80: (70, 20), 90: (20, 70)})) == []


def test_type_d_three_rungs_give_every_locked_pair():
    found = cs.screen_type_d(ladder("less", {10: (40, 40), 20: (40, 40), 30: (40, 40)}))
    assert sorted(leg_sides(c) for c in found) == [
        [("LAD-20", "yes"), ("LAD-10", "no")],
        [("LAD-30", "yes"), ("LAD-10", "no")],
        [("LAD-30", "yes"), ("LAD-20", "no")]]


def test_type_d_mixed_or_between_strike_types_skip_event():
    mixed = ladder("less", {10: (40, 40)}) + ladder("greater", {20: (40, 40)})
    stats = {}
    assert cs.screen_type_d(mixed, stats) == [] and stats["D_ladder_events"] == 0
    between = ladder("less", {10: (40, 40), 20: (40, 40)})
    between[0].update(strike_type="between", floor_strike=5)
    assert cs.screen_type_d(between) == []


def test_type_d_differing_titles_skip_event():
    ms = ladder("less", {10: (40, 40), 20: (40, 40)})
    ms[1]["title"] = "Highest temperature in Chicago today?"
    assert cs.screen_type_d(ms) == []
    assert cs.screen_type_d(ladder("less", {10: (40, 40), 20: (40, 40)}, title=None)) == []


def test_type_d_differing_expiries_skip_event():
    # the locked >= $1 guarantee needs both legs to settle on the same
    # value at the same time; a mixed-expiry event is not a ladder
    ms = ladder("less", {10: (40, 40), 20: (40, 40)})
    ms[1]["close_time"] = "2026-10-03T00:00:00Z"
    stats = {}
    assert cs.screen_type_d(ms, stats) == [] and stats["D_ladder_events"] == 0
    noexp = ladder("less", {10: (40, 40), 20: (40, 40)})
    for m in noexp:
        m.pop("close_time")
        m.pop("expected_expiration_time", None)
        m.pop("expiration_time", None)
    assert cs.screen_type_d(noexp) == []


def test_type_d_bad_strikes_skip_event():
    dup = ladder("less", {10: (40, 40), 20: (40, 40)})
    dup[1]["cap_strike"] = 10
    assert cs.screen_type_d(dup) == []
    for bad in (None, "20", True, float("nan")):
        ms = ladder("less", {10: (40, 40), 20: (40, 40)})
        ms[1]["cap_strike"] = bad
        assert cs.screen_type_d(ms) == [], bad
    assert cs.screen_type_d(ladder("less", {10: (40, 40)})) == []   # one market


def test_type_d_non_positive_edge_skips_pair():
    assert cs.screen_type_d(ladder("less", {10: (50, 50), 20: (50, 50)})) == []
    # 98c of asks is under par, but fees (2 x ~1.75c) push it over
    assert cs.screen_type_d(ladder("less", {10: (49, 49), 20: (49, 49)})) == []


def confirm_with(monkeypatch, cand, ev_markets, mutually_exclusive=False):
    ev = {"series_ticker": "KXHIGHNY", "title": "NYC high", "mutually_exclusive": mutually_exclusive}
    monkeypatch.setattr(cs, "kalshi_event", lambda et: {"event": ev, "markets": ev_markets})
    errors = []
    return cs.confirm_ladder(cand, errors), errors


def ladder_cand():
    ms = ladder("less", {10: (40, 40), 20: (40, 40)})
    return cs.screen_type_d(ms)[0], [dict(m, result="") for m in ms]


def test_confirm_ladder_clean_event_passes(monkeypatch):
    cand, ev_ms = ladder_cand()
    ok, errors = confirm_with(monkeypatch, cand, ev_ms)
    assert ok and errors == [] and cand["event_title"] == "NYC high"
    cand, ev_ms = ladder_cand()
    assert confirm_with(monkeypatch, cand, ev_ms, mutually_exclusive=None)[0]


def test_confirm_ladder_rejects_mutually_exclusive_event(monkeypatch):
    cand, ev_ms = ladder_cand()
    assert not confirm_with(monkeypatch, cand, ev_ms, mutually_exclusive=True)[0]


def test_confirm_ladder_rejects_resolved_or_missing_leg(monkeypatch):
    for res in ("yes", "no", "YES"):
        cand, ev_ms = ladder_cand()
        ev_ms[0]["result"] = res
        assert not confirm_with(monkeypatch, cand, ev_ms)[0], res
    cand, ev_ms = ladder_cand()
    assert not confirm_with(monkeypatch, cand, ev_ms[1:])[0]


def test_confirm_ladder_rejects_changed_strikes(monkeypatch):
    cand, ev_ms = ladder_cand()
    ev_ms[0]["cap_strike"] = 15
    assert not confirm_with(monkeypatch, cand, ev_ms)[0]
    cand, ev_ms = ladder_cand()
    ev_ms[1]["strike_type"] = "between"
    assert not confirm_with(monkeypatch, cand, ev_ms)[0]


def test_confirm_ladder_rejects_expiry_changed_since_screen(monkeypatch):
    cand, ev_ms = ladder_cand()
    ev_ms[0]["close_time"] = "2026-10-03T00:00:00Z"
    assert not confirm_with(monkeypatch, cand, ev_ms)[0]
    cand, ev_ms = ladder_cand()
    ev_ms[1]["expected_expiration_time"] = "2026-10-02T06:00:00Z"   # outranks close_time
    assert not confirm_with(monkeypatch, cand, ev_ms)[0]
    cand, ev_ms = ladder_cand()   # whole event rescheduled: legs agree, but not with the screen
    for m in ev_ms:
        m["close_time"] = "2026-10-03T00:00:00Z"
    assert not confirm_with(monkeypatch, cand, ev_ms)[0]


def test_confirm_ladder_rejects_live_legs_with_differing_expiries(monkeypatch):
    # each live leg matches its own screen-seen expiry, but the two legs
    # no longer settle at the same time
    cand, ev_ms = ladder_cand()
    later = "2026-10-03T00:00:00Z"
    cand["markets"][1]["close_time"] = later
    next(m for m in ev_ms if m["ticker"] == cand["legs"][1]["ticker"])["close_time"] = later
    assert not confirm_with(monkeypatch, cand, ev_ms)[0]


def test_confirm_ladder_rejects_missing_live_expiry(monkeypatch):
    cand, ev_ms = ladder_cand()
    ev_ms[0].pop("close_time")
    assert not confirm_with(monkeypatch, cand, ev_ms)[0]
    cand, ev_ms = ladder_cand()   # missing on both sides: None == None must not pass
    for m in cand["markets"] + ev_ms:
        m.pop("close_time", None)
    assert not confirm_with(monkeypatch, cand, ev_ms)[0]


def test_confirm_ladder_fetch_failure_fails_closed(monkeypatch):
    cand, _ = ladder_cand()

    def boom(et):
        raise RuntimeError("timeout")
    monkeypatch.setattr(cs, "kalshi_event", boom)
    errors = []
    assert not cs.confirm_ladder(cand, errors) and any("timeout" in e for e in errors)


def test_type_d_locked_profit_is_min_payout_case():
    cand = cs.screen_type_d(ladder("less", {10: (40, 40), 20: (40, 40)}))[0]
    r = cs.evaluate_combo(cand["legs"], 1000, 2000, 1000)
    # 23 sets: 23*80 = 1840 + fees ceil(38.64)=39 per leg = 1918 (24 sets: 2002 > 2000)
    assert r["sets"] == 23 and r["outlay_cents"] == 1918 and r["fees_cents"] == 78

    def payout(value):   # YES(cap 20) pays when value < 20, NO(cap 10) when value >= 10
        return 100 * ((value < 20) + (value >= 10))
    payouts = [payout(v) for v in (5, 10, 15, 20, 25)]
    assert min(payouts) == 100 and max(payouts) == 200
    assert r["locked_profit_cents"] == r["sets"] * min(payouts) - r["outlay_cents"] == 382


def test_finalize_type_d_sets_ladder_guarantee():
    cand = cs.screen_type_d(ladder("less", {10: (40, 40), 20: (40, 40)}))[0]
    args = SimpleNamespace(max_outlay_cents=2000, min_outlay_cents=1000, min_roi=0.0)
    books = {"LAD-20": {"yes": (4000, 500.0), "no": (6100, 500.0)},
             "LAD-10": {"yes": (6100, 500.0), "no": (4000, 500.0)}}
    out = cs.finalize(cand, books, {}, args, "now")
    assert out["combo_type"] == "D" and out["tier"] == "locked" and out["event_ticker"] == "LAD"
    assert out["guarantee"] == LADDER_GUARANTEE and out["locked_profit_cents"] == 382
    books["LAD-10"]["no"] = (6100, 500.0)   # book moved: 40 + 61 + fees >= 100
    cand = cs.screen_type_d(ladder("less", {10: (40, 40), 20: (40, 40)}))[0]
    assert cs.finalize(cand, books, {}, args, "now") is None


def test_type_d_end_to_end_through_main(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "STATE_PATH", str(tmp_path / "s.json"))
    ms = ladder("less", {10: (70, 40), 20: (40, 70)})   # no Type A/B combo on its own
    books = {"LAD-20": {"yes": (4000, 500.0), "no": (7000, 500.0)},
             "LAD-10": {"yes": (7000, 500.0), "no": (4000, 500.0)}}
    ev = {"series_ticker": "KXHIGHNY", "title": "NYC high", "mutually_exclusive": False}
    monkeypatch.setattr(cs, "kalshi_open_markets", lambda now_ts, errors, deadline: ms)
    monkeypatch.setattr(cs, "poly_markets", lambda: [])
    monkeypatch.setattr(cs, "kalshi_orderbook_top", lambda t: books[t])
    monkeypatch.setattr(cs, "kalshi_event", lambda et: {"event": ev, "markets": ms})
    monkeypatch.setattr(cs, "kalshi_fee_multiplier", lambda s: 1.0)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cs.main(["--no-mark", "--no-model-edge", "--no-combo-picks"]) == 0
    d = json.loads(buf.getvalue())
    sc = d["screen_candidates"]
    assert (sc["D"], sc["D_ladder_events"], sc["D_event_confirmed"]) == (1, 1, 1)
    assert "D_edge" not in sc and d["combos_found"] == 1 and d["edge_combos_found"] == 0
    c = d["new_combos"][0]
    assert c["combo_type"] == "D" and c["guarantee"] == LADDER_GUARANTEE
    assert c["depth_reverified"] is True and c["title"] == "NYC high"
    assert leg_sides(c) == [("LAD-20", "yes"), ("LAD-10", "no")]
    assert not os.path.exists(cs.STATE_PATH)
