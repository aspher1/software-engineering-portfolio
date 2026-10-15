"""Tests for the cross-venue (Type C) live feed: the binary-label
``same_proposition`` fix and the ``xvenue_matches.json`` export builder."""

import json

import combo_scan as cs


def _km(sub="Yes", title_words="bitcoin price above 100k"):
    return {"ticker": "KXBTC-1", "yes_sub_title": sub,
            "event_ticker": "KXBTC", "title": title_words}


def test_same_proposition_binary_label_matches():
    # Generic "Yes"/"No" sub-titles name no outcome: must not reject a true
    # binary match.
    assert cs.same_proposition(_km("Yes"), "bitcoin price above 100k",
                               "bitcoin price above 100k") is True
    assert cs.same_proposition(_km("No"), "bitcoin price above 100k",
                               "bitcoin price above 100k") is True
    assert cs.same_proposition(_km(""), "bitcoin price above 100k",
                               "bitcoin price above 100k") is True


def test_same_proposition_named_outcome_still_guarded():
    # "Yes, Trump" still requires its outcome words on the Polymarket side.
    assert cs.same_proposition(_km("Yes, Trump"), "trump win", "trump win") is True
    assert cs.same_proposition(_km("Yes, Trump"), "trump win", "biden win") is False
    # Digit and qualifier guards are untouched by the label fix.
    assert cs.same_proposition(_km("Yes"), "price above 100",
                               "price above 101") is False
    assert cs.same_proposition(_km("Yes"), "presidential winner",
                               "presidential nominee") is False


def _combo(side="kalshi_yes+poly_no", slug="btc-100k", ticker="KXBTC-1"):
    return {"combo_type": "C", "side": side, "tier": "edge",
            "edge": 5, "match_score": 0.857,
            "_xvenue": {"kalshi_ticker": ticker, "kalshi_yes_sub_title": "Yes",
                        "kalshi_yes_ask_cents": 52.0, "kalshi_no_ask_cents": 48.0,
                        "kalshi_expiry_ts": 1700000000, "kalshi_volume": 900.0,
                        "poly_slug": slug, "poly_event": "Bitcoin",
                        "poly_question": "BTC above 100k?",
                        "poly_yes_ask_cents": 55.0, "poly_no_ask_cents": 46.0,
                        "poly_yes_token": "yt", "poly_no_token": "nt",
                        "poly_fee_rate": 0.05, "poly_end_ts": 1700000000,
                        "poly_volume": 9000.0}}


def test_build_xvenue_export_dedupes_pair_and_drops_non_c():
    combos = [_combo("kalshi_yes+poly_no"), _combo("kalshi_no+poly_yes"),
              {"combo_type": "A", "_xvenue": {}},
              {"combo_type": "C"},  # no snapshot: dropped
              _combo("kalshi_yes+poly_no", slug="eth-5k", ticker="KXETH-1")]
    recs = cs.build_xvenue_export(combos)
    assert len(recs) == 2
    assert recs[0]["poly_slug"] == "btc-100k"
    assert recs[0]["kalshi_ticker"] == "KXBTC-1"
    assert recs[0]["edge_cents"] == 5
    assert recs[1]["poly_slug"] == "eth-5k"


def test_write_xvenue_export_roundtrip(tmp_path):
    errors = []
    path = str(tmp_path / "xvenue_matches.json")
    cs.write_xvenue_export([{"poly_slug": "s"}], 1700000000, "2026-10-02T10:00:00Z",
                           errors, path=path)
    assert errors == []
    payload = json.loads(open(path, encoding="utf-8").read())
    assert payload["export_version"] == cs.XVENUE_EXPORT_VERSION
    assert payload["as_of_ts"] == 1700000000
    assert payload["matches"] == [{"poly_slug": "s"}]


def test_write_xvenue_export_failure_recorded_not_raised():
    errors = []
    # Unwritable path: recorded, never raised.
    cs.write_xvenue_export([], 1.0, "t", errors, path="/proc/x.json")
    assert len(errors) == 1 and "xvenue export failed" in errors[0]
