"""Offline tests for the Tier 4 combo picker (scripts/combo_picker.py) and its
additive wiring in combo_scan.py."""

import io
import json
import math
import os
import random
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import combo_picker as cp  # noqa: E402
import combo_scan as cs  # noqa: E402

NOW = 1_800_000_000


def leg(ticker, ask, fair, event=None, side="YES", category="other", game=None,
        direction=0, depth=10_000, ttl=3600, source="kraken_model", **kw):
    return cp.PickLeg(ticker=ticker, side=side, ask_cents=ask, fair_cents=fair,
                      fair_source=source, event_ticker=event or f"EV-{ticker}",
                      game_key=game, category=category, expiry_ts=NOW + ttl,
                      depth_contracts=depth, direction=direction, **kw)


# --------------------------------------------------------------- game_key ----

@pytest.mark.parametrize("et,want", [
    ("KXNFLGAME-26OCT04INDWAS", "KXNFL|20261004|IND|WAS"),
    ("KXEPLGAME-26SEP19BRIARS", "KXEPL|20260919|ARS|BRI"),
    ("KXNBAGAME-26NOV12LALBOS", "KXNBA|20261112|BOS|LAL"),
    ("KXMLBGAME-26OCT02NYYBOS", "KXMLB|20261002|BOS|NYY"),
    ("KXNFLSPREAD-26OCT04WASIND", "KXNFL|20261004|IND|WAS"),   # same game, other market type
    ("KXNFLGAME-26OCT041300INDWAS", None),     # time in ticker: not the strict shape
    ("KXNFLGAME-26XYZ04INDWAS", None),         # bad month
    ("KXNFLGAME-26FEB30INDWAS", None),         # bad day
    ("KXNFLGAME-26OCT04INDIND", None),         # same team twice
    ("KXBTC15M-26OCT011300", None),
    ("", None), (None, None),
])
def test_game_key(et, want):
    assert cp.game_key(et) == want


def test_category_and_league():
    assert cp.category_of("KXNFLGAME-26OCT04INDWAS-IND") == "sports"
    assert cp.category_of("KXBUNDESLIGAGAME-26OCT04BAYBVB") == "sports"
    assert cp.category_of("KXETH15M-26OCT011300-00") == "crypto15m"
    assert cp.category_of("KXFEDDECISION-26OCT") == "other"
    assert cp.league_of("KXNFLGAME-26OCT04INDWAS") == cp.league_of("KXNFLTOTAL-26OCT04INDWAS") == "KXNFL"
    assert cp.league_of("KXFOO-1") == "KXFOO"


# ------------------------------------------------------------- joint_fair ----

def test_joint_fair_rejects_same_event_and_dup_ticker():
    with pytest.raises(cp.PickRejected, match="same_event"):
        cp.joint_fair([leg("A", 40, 60, event="E1"), leg("B", 40, 60, event="E1")])
    with pytest.raises(cp.PickRejected, match="dup_ticker"):
        cp.joint_fair([leg("A", 40, 60, event="E1"), leg("A", 40, 60, event="E2", side="NO")])


def test_same_game_flag_and_haircut():
    g = cp.game_key("KXNFLGAME-26OCT04INDWAS")
    a = leg("A", 50, 60, event="KXNFLGAME-26OCT04INDWAS", category="sports", game=g)
    b = leg("B", 60, 70, event="KXNFLSPREAD-26OCT04INDWAS", category="sports",
            game=cp.game_key("KXNFLSPREAD-26OCT04INDWAS"))
    joint, flagged = cp.joint_fair([a, b])
    assert flagged == [(0, 1, "same_game")]
    assert joint == pytest.approx(0.60 * 0.70 - 0.04)      # 0.42 - 4pts = 0.38


@pytest.mark.parametrize("ea,eb", [
    ("KXAFCONGAME-26OCT06ANGMAW", "KXAFCONSPREAD-26OCT06ANGMAW"),     # live 2026-10-02
    ("KXARGPREMDIVBTTS-26OCT02BOCUNI", "KXARGPREMDIVTOTAL-26OCT02BOCUNI"),
    ("KXCFLGAME-26OCT04HAMTOR", "KXCFLSPREAD-26OCT04TORHAM"),           # team order differs
])
def test_same_game_flags_market_types_outside_known_leagues(ea, eb):
    a = leg("A", 50, 60, event=ea, game=cp.game_key(ea))
    b = leg("B", 50, 60, event=eb, game=cp.game_key(eb))
    assert cp.category_of(ea) == cp.category_of(eb) == "other"
    assert a.game_key != b.game_key and cp.game_tail(a.game_key) == cp.game_tail(b.game_key)
    joint, flagged = cp.joint_fair([a, b])
    assert flagged == [(0, 1, "same_game")]
    assert joint == pytest.approx(0.36 - 0.04)


def test_same_game_tail_never_matches_other_games_or_none():
    a = leg("A", 50, 60, event="KXCFLGAME-26OCT04HAMTOR",
            game=cp.game_key("KXCFLGAME-26OCT04HAMTOR"))
    other_day = leg("B", 50, 60, event="KXCFLSPREAD-26OCT11HAMTOR",
                    game=cp.game_key("KXCFLSPREAD-26OCT11HAMTOR"))
    other_team = leg("C", 50, 60, event="KXCFLSPREAD-26OCT04HAMMTL",
                     game=cp.game_key("KXCFLSPREAD-26OCT04HAMMTL"))
    unknown = leg("D", 50, 60, event="KXCFLWINNER-26", game=None)
    assert cp.joint_fair([a, other_day])[1] == []
    assert cp.joint_fair([a, other_team])[1] == []
    assert cp.joint_fair([a, unknown])[1] == []          # "other" category: no unknown-game flag
    assert cp.game_tail(None) is None and cp.game_tail("X") == "X"
    assert cp.game_key("KXBRASILEIRO1HSPREAD-26OCT04PALFLA") is None   # digit series: unparsed


def test_same_league_unknown_game_flags():
    a = leg("A", 50, 60, event="KXNBAGAME-26NOV12LALBOS", category="sports",
            game=cp.game_key("KXNBAGAME-26NOV12LALBOS"))
    b = leg("B", 50, 60, event="KXNBASERIES-WEIRD", category="sports", game=None)
    c = leg("C", 50, 60, event="KXNFLGAME-26OCT04INDWAS", category="sports", game=None)
    joint, flagged = cp.joint_fair([a, b, c])
    assert flagged == [(0, 1, "same_game_unknown")]         # NBA vs NFL: not flagged
    assert joint == pytest.approx(0.6 ** 3 - 0.04)


def test_crypto_opposite_direction_flags_same_direction_does_not():
    up = leg("BTC", 40, 60, category="crypto15m", direction=1)
    down = leg("ETH", 40, 60, side="NO", category="crypto15m", direction=-1)
    up2 = leg("SOL", 40, 60, category="crypto15m", direction=1)
    joint, flagged = cp.joint_fair([up, down])
    assert flagged == [(0, 1, "crypto_opp")] and joint == pytest.approx(0.36 - 0.04)
    joint, flagged = cp.joint_fair([up, up2])
    assert flagged == [] and joint == pytest.approx(0.36)


def test_too_correlated_and_joint_too_small():
    sports = [leg(t, 50, 60, event=f"KXNFLX-{t}", category="sports") for t in "ABC"]
    with pytest.raises(cp.PickRejected, match="too_correlated"):   # 3 flagged pairs > 2
        cp.joint_fair(sports)
    assert len(cp.joint_fair(sports[:2])[1]) == 1
    with pytest.raises(cp.PickRejected, match="joint_too_small"):
        cp.joint_fair([leg(t, 20, 30) for t in "ABCD"])            # 0.3^4 = 0.0081
    # the haircut alone can push a joint under the floor
    with pytest.raises(cp.PickRejected, match="joint_too_small"):
        cp.joint_fair([leg("A", 10, 20, category="crypto15m", direction=1),
                       leg("B", 10, 25, category="crypto15m", direction=-1)])  # 0.05-0.04


# ---------------------------------------------------------------- pricing ----

def test_price_combo_hand_computed():
    p = cp.price_combo([leg("A", 60, 65), leg("B", 70, 75)])
    assert p.p_combo_cents == pytest.approx(43.31)                 # 100 * .61 * .71
    assert p.contracts_10 == 23                                    # floor(1000 / 43.31)
    assert p.stake_cents_10 == pytest.approx(996.13)
    assert cs.taker_fee_cents(43.31, 23) == 40                     # one combo order
    assert cs.taker_fee_cents(60, 23) == 39 and cs.taker_fee_cents(70, 23) == 34
    assert p.fee_cents_10 == 73                                    # max(40, 39 + 34)
    assert p.joint_fair == pytest.approx(0.4875)
    assert p.edge_cents_10 == pytest.approx(0.4875 * 2300 - 996.13 - 73)   # 52.12
    assert p.expected_roi_pct == pytest.approx(100 * 52.12 / 996.13)
    assert p.payout_ask_10 == 23.0 and p.payout_fair_10 == pytest.approx(10 / 0.4875)
    assert p.guarantee == cp.PICK_GUARANTEE and "NOT guaranteed" in p.guarantee
    assert cp.price_passes(p)                                      # 52.12 >= 50


def test_contracts_10_exact_when_price_divides_ten_dollars():
    p = cp.price_combo([leg("A", 4, 15), leg("B", 79, 90)])      # 100 * .05 * .80 = 4c
    assert p.p_combo_cents == 4.0
    assert p.contracts_10 == 250 and p.payout_ask_10 == 250.0    # float product gave 249
    assert p.stake_cents_10 == 1000.0


def test_contracts_10_matches_exact_arithmetic_and_never_exceeds_stake():
    from fractions import Fraction
    for a in range(1, 99):
        for b in range(a, 99):
            try:
                p = cp.price_combo([leg("A", a, 100), leg("B", b, 100)])
            except cp.PickRejected:
                continue
            exact = Fraction(a + 1) * Fraction(b + 1) / 100
            assert p.contracts_10 == math.floor(Fraction(1000) / exact), (a, b)
            assert p.stake_cents_10 <= 1000.0
            assert p.p_combo_cents == float(exact)


def test_fee_is_never_below_either_branch():
    for asks in [(60, 70), (20, 20), (90, 55), (95, 95), (5, 50, 40), (30, 30, 30, 30)]:
        legs = [leg(f"T{i}", a, min(100, a + 10), fee_mult=m)
                for i, (a, m) in enumerate(zip(asks, (1, 2, 1, 1.5)))]
        p = cp.price_combo(legs)
        one = cs.taker_fee_cents(p.p_combo_cents, p.contracts_10, max(l.fee_mult for l in legs))
        per = sum(cs.taker_fee_cents(l.ask_cents, p.contracts_10, l.fee_mult) for l in legs)
        assert p.fee_cents_10 >= one and p.fee_cents_10 >= per
        assert p.fee_cents_10 == max(one, per)


def test_fee_one_order_branch_wins_when_leg_fees_are_small():
    # Polymarket plumbing: a 0-rate poly leg adds no per-leg fee, so the single
    # combo-order fee is the larger (binding) branch.
    legs = [leg("K", 90, 97), leg("P", 55, 65, venue="polymarket", fee_rate=0.0)]
    p = cp.price_combo(legs)
    one = cs.taker_fee_cents(p.p_combo_cents, p.contracts_10, 1)
    assert one > cs.taker_fee_cents(90, p.contracts_10) and p.fee_cents_10 == one


def test_price_passes_gates():
    p = cp.price_combo([leg("A", 60, 65, depth=22), leg("B", 70, 75)])
    assert p.contracts_10 == 23 and not cp.price_passes(p)        # depth 22 < 23 contracts
    p = cp.price_combo([leg("A", 60, 64), leg("B", 70, 75)])
    assert p.edge_cents_10 < 50 and not cp.price_passes(p)


def test_paper_stake_clamps():
    p = cp.price_combo([leg("A", 40, 60), leg("B", 40, 60)])       # big edge
    assert p.paper_stake_dollars == cp.PAPER_MAX_STAKE_DOLLARS == 10
    assert cp.paper_stake(0.1682, 16.81) == cp.PAPER_MIN_STAKE_DOLLARS == 2   # tiny edge
    assert cp.paper_stake(0.10, 16.81) == 2                         # negative Kelly
    o = 100 / 43.31
    f = cp.KELLY_FRACTION * (0.445 * o - 1) / (o - 1) * cp.PAPER_BANKROLL_DOLLARS
    assert 2 < f < 10 and cp.paper_stake(0.445, 43.31) == pytest.approx(f)


# ------------------------------------------------------------ beam search ----

def pool_legs():
    out = [leg(f"L{i}", 40 + i, 70 + i, category="crypto15m", direction=1) for i in range(8)]
    out += [leg("THIN", 40, 44),                    # edge 4 < 5: pre-filtered
            leg("SOON", 30, 60, ttl=200),           # expires < 5 min out
            leg("NOFAIR", 30, 60, source="none"),
            leg("NODEPTH", 30, 60, depth=0)]
    return out


def test_beam_search_deterministic_unique_and_sized():
    legs = pool_legs()
    picks = cp.build_combo_picks(legs, NOW)
    keys = [cp.combo_key(p) for p in picks]
    assert picks and len(keys) == len(set(keys))
    shuffled = legs[:]
    random.Random(7).shuffle(shuffled)
    assert [cp.combo_key(p) for p in cp.build_combo_picks(shuffled, NOW)] == keys
    assert {len(p.legs) for p in picks} <= {2, 3, 4} and {len(p.legs) for p in picks} == {2, 3, 4}
    banned = {"THIN", "SOON", "NOFAIR", "NODEPTH"}
    assert not any(l.ticker in banned for p in picks for l in p.legs)
    for p in picks:
        assert cp.price_passes(p)
        assert len({l.ticker for l in p.legs}) == len(p.legs)
    edges = [(p.edge_cents_10, p.joint_fair) for p in picks]
    assert edges == sorted(edges, key=lambda e: (-e[0], -e[1]))


def test_beam_search_empty_and_single_leg_pools():
    assert cp.build_combo_picks([], NOW) == []
    assert cp.build_combo_picks([leg("A", 40, 60)], NOW) == []
    assert cp.build_combo_picks([leg("A", 40, 60, event="E"), leg("B", 40, 60, event="E")], NOW) == []


# -------------------------------------------------------- keys and guards ----

def test_combo_key_format_and_no_tier_collision():
    p = cp.price_combo([leg("ZED", 40, 60, side="NO"), leg("ABC", 40, 60)])
    assert cp.combo_key(p) == "P|ABC:YES,ZED:NO"
    pk = cs._picker_key({"combo_key": cp.combo_key(p)})
    assert pk == "picker|P|ABC:YES,ZED:NO"
    tier_keys = {cs.combo_key("M", ["ABC"], "YES", "model-edge"),
                 cs.combo_key("A", ["ABC", "ABC"], "yes+no"),
                 cs.combo_key("C", ["ABC", "poly:x"], "kalshi_yes+poly_no", "edge")}
    assert pk not in tier_keys
    assert not any(k.startswith("picker|") for k in tier_keys)


@pytest.mark.parametrize("url,method", [
    ("https://api.elections.kalshi.com/trade-api/v2/multivariate_event_collections/KXMVE-1", "POST"),
    ("https://api.elections.kalshi.com/trade-api/v2/multivariate_event_collections/KXMVE-1/lookup", "post"),
    ("https://api.elections.kalshi.com/trade-api/v2/communications/rfqs", "POST"),
    ("https://api.elections.kalshi.com/trade-api/v2/communications/rfqs", "GET"),
    ("https://api.elections.kalshi.com/trade-api/v2/communications/quotes", "POST"),
    ("https://combos-rfq-api.polymarket.com/v1/maker/quotes", "POST"),
    ("https://api.elections.kalshi.com/trade-api/v2/markets", "POST"),     # any write at all
    ("https://api.elections.kalshi.com/trade-api/v2/portfolio/orders", "DELETE"),
])
def test_guard_no_write_blocks(url, method):
    with pytest.raises(cp.WriteBlocked):
        cp.guard_no_write(url, method)


def test_guard_no_write_allows_reads():
    for url in ("https://api.elections.kalshi.com/trade-api/v2/markets",
                "https://api.elections.kalshi.com/trade-api/v2/multivariate_event_collections"):
        assert cp.guard_no_write(url, "GET") == url
    assert cp._WRITE_BLACKLIST and all(isinstance(s, str) for s in cp._WRITE_BLACKLIST)


def test_picker_module_has_no_http_client():
    src = open(cp.__file__).read()
    for bad in ("import requests", "urllib.request", "http.client", "Thread(", "requests."):
        assert bad not in src


# ------------------------------------------------------- tier 1 regression ----

def test_tier1_spot_probe_48_locked_49_not():
    assert cs.evaluate_combo([{"ticker": "M", "side": "yes", "price_cents": 48},
                              {"ticker": "M", "side": "no", "price_cents": 48}],
                             500, 2000, 1000) is not None
    assert cs.evaluate_combo([{"ticker": "M", "side": "yes", "price_cents": 49},
                              {"ticker": "M", "side": "no", "price_cents": 49}],
                             500, 2000, 1000) is None
    m48 = {"ticker": "M48", "yes_ask_dollars": "0.4800", "no_ask_dollars": "0.4800"}
    m49 = {"ticker": "M49", "yes_ask_dollars": "0.4900", "no_ask_dollars": "0.4900"}
    found = cs.screen_type_a([m48, m49], near_arb=True)
    assert [(c["legs"][0]["ticker"], c["tier"]) for c in found] == [("M48", "locked"),
                                                                      ("M49", "edge")]


# ---------------------------------------------------------------- output ----

LOG_KEYS = {"priced_at", "combo_key", "legs", "joint_fair", "flagged_pairs", "p_combo_cents",
            "contracts_10", "stake_cents_10", "fee_cents_10", "edge_cents_10",
            "expected_roi_pct", "payout_ask_10", "payout_fair_10", "paper_stake_dollars",
            "guarantee", "commit"}
LEG_KEYS = {"ticker", "side", "ask_cents", "fair_cents", "fair_source", "event_ticker",
            "category", "expiry_ts", "model_version"}


def test_pick_to_json_round_trip_and_log_append(tmp_path):
    p = cp.price_combo([leg("A", 60, 65, model_version="kraken_brownian_v1"),
                        leg("B", 70, 75, model_version="xvenue_poly_v1")])
    d = cp.pick_to_json(p)
    assert json.loads(json.dumps(d)) == d
    assert LOG_KEYS <= set(d) and all(LEG_KEYS <= set(l) for l in d["legs"])
    assert d["model_versions"] == ["kraken_brownian_v1", "xvenue_poly_v1"]
    assert d["combo_key"] == "P|A:YES,B:YES" and d["guarantee"] == cp.PICK_GUARANTEE
    assert d["p_combo_cents"] == 43.31 and d["edge_cents_10"] == 52.12
    assert isinstance(d["commit"], str) and d["commit"]
    path = tmp_path / "data" / "picker_picks.jsonl"
    cp.log_pick(p, str(path))
    assert path.read_text().count("\n") == 1
    cp.log_pick(d, str(path))
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert len(rows) == 2 and rows[1] == d
    line = cp.pick_summary_line(d)
    assert line.startswith("PICK: A YES @ 60c + B YES @ 70c -> $10 pays $23")
    assert "not guaranteed" in line


# -------------------------------------------------------------- wiring ----

def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def model_entry(ticker, event, side="YES", ask=40, fair_cc=5500, depth=1000, ttl=600):
    m = {"ticker": ticker, "event_ticker": event, "close_time": iso(int(time.time()) + ttl)}
    cand = {"tier": "model-edge", "ticker": ticker, "side": side, "fair_taken_cc": fair_cc,
            "markets": [m], "legs": [{"ticker": ticker, "side": side.lower(),
                                      "price_cents": ask, "venue": "kalshi", "fee_mult": 1.0}]}
    return {"combo_type": "M", "tier": "model-edge", "side": side,
            "legs": [{"ticker": ticker, "side": side.lower(), "price_cents": ask, "venue": "kalshi"}],
            "depth_sets": depth, "sets": 10, "fair_cents": fair_cc / 100,
            "expected_roi_pct": 10.0, "_cand": cand}


def xvenue_entry(ticker, event, k_ask, p_ask, kside="yes", depth=500, tier="edge"):
    m = {"ticker": ticker, "event_ticker": event, "close_time": iso(int(time.time()) + 86400)}
    pside = "no" if kside == "yes" else "yes"
    legs = [{"ticker": ticker, "side": kside, "price_cents": k_ask, "venue": "kalshi"},
            {"ticker": "poly:x", "side": pside, "price_cents": p_ask, "venue": "polymarket"}]
    cand = {"combo_type": "C", "tier": tier, "markets": [m], "side": f"kalshi_{kside}+poly_{pside}",
            "poly_market": "x", "match_score": 1.0,
            "legs": [dict(legs[0], fee_mult=1.0), dict(legs[1], fee_rate=0.03, token="tok")]}
    return {"combo_type": "C", "tier": tier, "side": f"kalshi_{kside}+poly_{pside}",
            "legs": legs, "depth_sets": depth, "edge_cents_per_set": 5.0, "_cand": cand}


def test_screen_picker_legs_maps_confirmed_entries():
    now = int(time.time())
    legs = cs.screen_picker_legs(
        [model_entry("KXBTC15M-26OCT011300-00", "KXBTC15M-26OCT011300", side="NO")],
        [xvenue_entry("KXNFLGAME-26OCT04INDWAS-IND", "KXNFLGAME-26OCT04INDWAS", 40, 50),
         xvenue_entry("KXFOO-1", "KXFOO", 46, 50)],        # 100-50-46 = 4 < 5: dropped
        now)
    assert [l.ticker for l in legs] == ["KXBTC15M-26OCT011300-00", "KXNFLGAME-26OCT04INDWAS-IND"]
    k, x = legs
    assert (k.side, k.ask_cents, k.fair_cents, k.fair_source, k.category, k.direction,
            k.depth_contracts, k.model_version) == \
        ("NO", 40.0, 55.0, "kraken_model", "crypto15m", -1, 1000, "kraken_brownian_v1")
    assert (x.side, x.ask_cents, x.fair_cents, x.fair_source, x.category, x.direction,
            x.game_key, x.model_version) == \
        ("YES", 40.0, 50.0, "xvenue_poly", "sports", 0, "KXNFL|20261004|IND|WAS", "xvenue_poly_v1")


def test_screen_picker_legs_includes_locked_type_c():
    now = int(time.time())
    legs = cs.screen_picker_legs(
        [],
        [xvenue_entry("KXNFLGAME-26OCT04INDWAS-IND", "KXNFLGAME-26OCT04INDWAS", 40, 50,
                      tier="locked"),
         xvenue_entry("KXFOO-1", "KXFOO", 46, 50, tier="locked")],   # 4 < 5: dropped
        now)
    assert [l.ticker for l in legs] == ["KXNFLGAME-26OCT04INDWAS-IND"]
    assert legs[0].fair_source == "xvenue_poly" and legs[0].fair_cents == 50.0


def test_screen_picker_legs_fails_closed():
    now = int(time.time())
    bad_side = model_entry("KXBTC15M-A", "EVA")
    bad_side["legs"][0]["side"] = "maybe"
    no_event = model_entry("KXBTC15M-B", "")
    no_close = model_entry("KXBTC15M-C", "EVC")
    del no_close["_cand"]["markets"][0]["close_time"]
    soon = model_entry("KXBTC15M-D", "EVD", ttl=60)
    no_cand = model_entry("KXBTC15M-E", "EVE")
    del no_cand["_cand"]
    two_kalshi = xvenue_entry("KXFOO-1", "KXFOO", 30, 50)
    two_kalshi["legs"][1]["venue"] = "kalshi"
    assert cs.screen_picker_legs([bad_side, no_event, no_close, soon, no_cand],
                                 [two_kalshi], now) == []


def run_main_with(monkeypatch, tmp_path, argv, combos):
    monkeypatch.setattr(cs, "STATE_PATH", str(tmp_path / "s.json"))
    t0 = time.time()
    monkeypatch.setattr(cs, "run_scan", lambda args, errors: (
        [{"ticker": "x"}], [dict(c) for c in combos], {}, cs.iso_now(), int(t0), t0,
        {"coverage_complete": True}))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        assert cs.main(argv) == 0
    return json.loads(out.getvalue()), err.getvalue()


TWO_MODEL = [model_entry("KXBTC15M-26OCT011300-00", "KXBTC15M-26OCT011300"),
             model_entry("KXETH15M-26OCT011300-00", "KXETH15M-26OCT011300")]


def test_main_emits_picks_and_off_flag_omits_keys(monkeypatch, tmp_path):
    on, err = run_main_with(monkeypatch, tmp_path, ["--no-mark"], TWO_MODEL)
    off, _ = run_main_with(monkeypatch, tmp_path, ["--no-mark", "--no-combo-picks"], TWO_MODEL)
    assert len(on["combo_picks_found"]) == 1 and on["new_combo_picks"] == on["combo_picks_found"]
    [pk] = on["new_combo_picks"]
    assert pk["combo_key"] == "P|KXBTC15M-26OCT011300-00:YES,KXETH15M-26OCT011300-00:YES"
    assert pk["guarantee"] == cp.PICK_GUARANTEE and pk["flagged_pairs"] == []
    assert "combo_picks_found" not in off and "new_combo_picks" not in off
    for k in set(off) - {"as_of", "sweep_seconds"}:
        assert on[k] == off[k], k
    assert "PICK: KXBTC15M-26OCT011300-00 YES @ 40c + KXETH15M-26OCT011300-00 YES @ 40c" in err
    assert not os.path.exists(cp.PICK_LOG_PATH)                      # --no-mark never logs


def test_main_locked_type_c_leg_builds_pick(monkeypatch, tmp_path):
    combos = [xvenue_entry("KXNFLGAME-26OCT04INDWAS-IND", "KXNFLGAME-26OCT04INDWAS", 40, 50,
                           depth=500, tier="locked"),
              model_entry("KXBTC15M-26OCT011300-00", "KXBTC15M-26OCT011300",
                          side="YES", ask=40, fair_cc=5500, depth=1000)]
    # The new locked Type C combo is re-verified before output; serve the same
    # top of book offline so it survives (an unverifiable combo is dropped).
    monkeypatch.setattr(cs, "kalshi_orderbook_top",
                        lambda t: {"yes": (4000, 500.0), "no": (6100, 500.0)})
    monkeypatch.setattr(cs, "poly_best_ask", lambda tok: (5000, 500.0))
    d, _ = run_main_with(monkeypatch, tmp_path, ["--no-mark"], combos)
    assert d["stats"]["reverify_unverified"] == 0 and d["stats"]["reverify_dropped"] == 0
    [pk] = d["combo_picks_found"]
    assert "KXNFLGAME-26OCT04INDWAS-IND" in pk["combo_key"]
    assert "KXBTC15M-26OCT011300-00" in pk["combo_key"]
    assert all(l["fair_source"] in ("kraken_model", "xvenue_poly") for l in pk["legs"])


def test_main_no_legs_gives_empty_lists(monkeypatch, tmp_path):
    d, _ = run_main_with(monkeypatch, tmp_path, ["--no-mark"], TWO_MODEL[:1])
    assert d["combo_picks_found"] == [] and d["new_combo_picks"] == [] and d["errors"] == []


def test_main_picker_dedup_and_log_with_mark(monkeypatch, tmp_path):
    first, _ = run_main_with(monkeypatch, tmp_path, [], TWO_MODEL)
    second, _ = run_main_with(monkeypatch, tmp_path, [], TWO_MODEL)
    assert len(first["new_combo_picks"]) == 1 and second["new_combo_picks"] == []
    assert len(second["combo_picks_found"]) == 1
    with open(cs.STATE_PATH) as f:
        keys = set(json.load(f)["alerted"])
    assert "picker|" + first["new_combo_picks"][0]["combo_key"] in keys
    with open(cp.PICK_LOG_PATH) as f:
        rows = [json.loads(x) for x in f]
    assert len(rows) == 1 and LOG_KEYS <= set(rows[0])


def test_main_picker_skipped_past_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "RUN_HARD_BUDGET_S", -1)
    d, _ = run_main_with(monkeypatch, tmp_path, ["--no-mark"], TWO_MODEL)
    assert d["combo_picks_found"] == [] and d["stats"]["picker_skipped_budget"] == 1


def test_main_picker_failure_is_contained(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(cp, "build_combo_picks", boom)
    d, _ = run_main_with(monkeypatch, tmp_path, [], TWO_MODEL)
    assert d["combo_picks_found"] == [] and any("combo picker failed" in e for e in d["errors"])
    assert d["new_model_edge_combos"] and os.path.exists(cs.STATE_PATH)
