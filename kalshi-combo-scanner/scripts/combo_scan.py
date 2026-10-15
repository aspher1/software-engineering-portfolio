#!/usr/bin/env python3
"""All-market Kalshi combo / arbitrage scanner (paper/analysis only).

Sweeps every open Kalshi market (multivariate "combo" markets excluded) and
looks for multi-leg positions that lock in a profit after taker fees:
  A. single-market Yes+No: yes_ask + no_ask + fees < 100c.
  B. event books: buy one Yes of every outcome of a mutually-exclusive,
     exhaustive event (exactly one outcome pays 100c) for < 100c after fees.
  C. cross-venue: Kalshi Yes ask + Polymarket No ask (or the mirror) at
     least XVENUE_MIN_EDGE_CENTS under 100c after fees, matched on title
     similarity (normalized titles, word-set Jaccard) plus outcome guards.
  D. cumulative-threshold ladders: in one event whose markets share a title
     and are all "less" (or all "greater") with distinct strikes, YES on the
     looser threshold + NO on the stricter one always pays >= 100c; flagged
     when the two asks + fees < 100c.

Screening runs on the /markets listing only (it carries best asks and sizes).
Orderbooks, events and series fee multipliers are fetched only for the small
set of flagged candidates. Each flagged combo is then sized against top-of-
book depth and a small outlay (default $10-$20).

Fees (verified 2026-10-01 against https://kalshi.com/docs/kalshi-fee-schedule.pdf,
effective July 7, 2026): taker fee = round up(M x 0.07 x C x P x (1-P)),
rounded here to the next whole cent per order leg (Kalshi rounds to a centicent,
so this is conservative). No settlement fee on Kalshi; see README.

Two tiers. Tier 1 "locked" is everything above. Tier 2 "edge" (on by default)
also flags combos that are NOT guaranteed: Type A "near-arbs" whose combined
PRE-fee ask is strictly below 100c (taker fees erase the edge; they'd only
lock with lower fees, e.g. maker orders) and Type C gaps of
XVENUE_EDGE_MIN_CENTS up to the 8c locked bar. Types B and D are Tier 1 only. Edge
entries carry "edge_cents_per_set" (pre-fee for A, after-fee for C) instead
of "locked_profit_cents" and never displace Tier 1 candidates or orderbooks.

Tier 3 "model-edge" (on by default) is a directional bet, NOT an arbitrage:
live Kalshi 15-minute crypto up/down windows (KX{SYM}15M) priced against
public Kraken spot with a zero-drift fair-value model (scripts/model_edge.py).
A window is flagged when the model agrees with the market's lean (taken-side
mid >= 0.5) and the fair value of the taken side beats its ASK by
MODEL_EDGE_MIN_CENTS before fees and by the same amount per set after
cent-rounded taker fees at the orderbook price. Entries are combo_type "M",
carry "fair_cents", "edge_cents_per_set", "expected_roi_pct" and
"roi_if_right_pct", and the guarantee MODEL_EDGE_GUARANTEE. They use the
already-fetched listing, Kraken data only for pairs with live windows (two
calls per pair) and orderbooks fetched after all Tier 1/2 books.

Tier 4 "combo-pick" (on by default, scripts/combo_picker.py) runs last, inside
RUN_HARD_BUDGET_S, with no HTTP of its own: confirmed Tier 3 legs and the
Kalshi legs of confirmed Type C cross-venue pairs (tiers "edge" and "locked")
(fair = 100 - Polymarket opposite ask) become 2-4 leg parlay picks ("$10 pays $X") priced at asks + 1c/leg with
a correlation-haircut joint fair. Picks are MODEL estimates, never
guaranteed; they are reported in "combo_picks_found" / "new_combo_picks"
(keys prefixed "picker|") and, on marking runs, appended to
data/picker_picks.jsonl (COMBO_SCANNER_PICK_LOG).

Dedupes against a local state file (COMBO_SCANNER_STATE_DIR, default
data/state/) and prints one JSON object.
A combo is "new" when its (combo_type, sorted tickers, side) key was never
alerted before (Tier 2 keys are prefixed "edge|", Tier 3 "model-edge|").
Tier 3 entries are reported only in "new_model_edge_combos"; "new_combos" and
"repeat_combos_count" cover Tiers 1 and 2. New Tier 1 and Type C edge combos
get one fresh orderbook re-check right before output ("depth_reverified");
ones that fail it or can't be re-checked in time are not output.
Exits 0 always; partial failures land in "errors", and "coverage_complete" /
"stats" say whether every listing range was swept (one summary line also
goes to stderr).

Flags: --no-mark (diagnostic read-only: never writes state), --max-outlay /
--min-outlay (dollars), --min-roi (percent, after fees; Tier 1 only),
--edge-tier / --no-edge-tier (Tier 2 on/off, default on),
--model-edge / --no-model-edge (Tier 3 on/off, default on),
--combo-picks / --no-combo-picks (Tier 4 on/off, default on).

No orders are placed anywhere. No credentials are used.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import random
import re
import sys
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fractions import Fraction
from urllib.parse import urlsplit

import requests

import combo_picker as cp
import model_edge as me
import scanner_env as env

STATE_PATH = os.path.join(
    env.env_path("COMBO_SCANNER_STATE_DIR", os.path.join(env.REPO_ROOT, "data", "state")),
    "combo_scanner_state.json")
CACHE_DIR = env.env_path("COMBO_SCANNER_CACHE_DIR", os.path.join(env.REPO_ROOT, "data", "cache"))
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
IO_WORKERS = 4                 # detail-fetch threads (orderbook/event/series); kept low
LISTING_WORKERS = 10           # listing-sweep threads (~6 pages/s, far under Kalshi's 20 req/s)
HTTP_TIMEOUT = 30
LISTING_TIMEOUT = 10           # one listing page is 0.3-3s; a stalled one is retried, not waited on
LISTING_RETRIES = 3
HTTP_RETRIES = 4               # retries after the first attempt on 429/5xx/network
BACKOFF_BASE_S = 0.5           # backoff = base * 2^attempt + jitter, capped
BACKOFF_CAP_S = 8.0
SWEEP_BUDGET_S = 30            # main listing pass; unfinished ranges then get a rescue pass
RESCUE_BUDGET_S = 10           # extra listing time to retry unfinished ranges
REVERIFY_BUDGET_S = 10         # fresh-book re-check of NEW combos right before output
RUN_HARD_BUDGET_S = 58         # nothing optional starts past this (whole run < 60s)
FEE_MULT_TTL_S = 86400         # series fee-multiplier disk cache
PLAN_TTL_S = 86400             # sweep plan (close-time shards from the last complete sweep)

KALSHI_TAKER_RATE = Fraction(7, 100)
FEES_MODEL = "kalshi-taker-2026-07-07"   # fee schedule effective date (verified 2026-10-01)
POLY_DEFAULT_TAKER_RATE = 0.07           # used when a fee-enabled market omits its rate

DEFAULT_MAX_OUTLAY_DOLLARS = 20.0
DEFAULT_MIN_OUTLAY_DOLLARS = 10.0
XVENUE_MIN_EDGE_CENTS = 8      # min after-fee cross-venue gap (cents per $1 set)
XVENUE_EDGE_MIN_CENTS = 4      # Tier 2 cross-venue bar (gaps below the locked bar)
A_NEAR_ARB_MAX_ASK_CENTS = 100   # Tier 2 Yes+No: combined PRE-fee ask strictly below this
EDGE_EVAL_MIN_CENTS = {"A": 0.0, "C": 0.0}   # A: pre-fee edge must exceed; C: after-fee edge must exceed
EDGE_GUARANTEE = "edge only — NOT a locked profit; edge can vanish with fees/slippage"
A_EDGE_GUARANTEE = ("near-arb — profitable before taker fees; current taker fees erase the "
                    "edge; needs lower fees (e.g. maker) to be actionable")
EDGE_DETAIL_BUDGET_S = 55      # skip Tier 2/3 orderbook fetches past this (sweep < 60s)
MODEL_EDGE_MIN_CENTS = 5       # Tier 3: fair minus ask, and expected edge per set after fees
MODEL_EDGE_GUARANTEE = "MODEL estimate — NOT guaranteed; fair-value model vs live spot"
MAX_MODEL_ORDERBOOK_FETCHES = 60   # Tier 3 books, fetched after all Tier 1/2 books
MODEL_KRAKEN_BUDGET_S = 53     # stop waiting for Kraken here; unfinished pairs are skipped
XVENUE_MATCH_MIN = 0.5         # min title jaccard (same as same_day_loop.py)
XVENUE_MIN_KALSHI_VOLUME = 500
XVENUE_MIN_POLY_VOLUME = 5000
XVENUE_MAX_EXPIRY_GAP_S = 2 * 86400   # matched markets must end within 2 days
XVENUE_QUALIFIERS = {          # words that change what settles; must match on both sides
    "first", "second", "round", "runoff", "primary", "nominee", "nomination",
    "margin", "popular", "above", "below", "over", "under", "more", "less",
    "least", "most", "before", "after", "between", "exactly", "seat", "seats",
    "map", "half", "quarter", "period", "set", "inning", "lead", "top", "not",
    "vice",
}
POLY_PAGE = 500
POLY_MAX_OFFSET = 3000

# Open markets are paged in disjoint close-time ranges so LISTING_WORKERS can
# paginate in parallel (one cursor chain is ~100s for ~130k markets). A
# complete sweep saves balanced shards (the "plan") for the next run; without
# one, coarse seeds are split adaptively.
CLOSE_TS_MIN = 0
CLOSE_TS_MAX = 4102444800      # 2100-01-01; Kalshi lists closes out to 2099
SEED_SPLIT_HOURS = [6, 48, 120, 336, 1440, 4320, 8760]
POINT_MASS_CLOSE_ISO = ("2027-11-03T14:00:00Z",    # ~10k markets share this close
                        "2027-11-03T15:00:00Z")    # ~9k (live, 2026-10-01)
MAX_RANGE_SPLITS = 128
RESCUE_RANGE_SPLITS = 64
RESPLIT_AFTER_PAGES = 8        # planned shards still paginating after this get split
RANGE_SLICE_S = 8              # ...as do ranges still paginating after this long
PLAN_SHARD_MARKETS = 3000      # target markets per planned shard (~3 pages)
MAX_PAGES_PER_RANGE = 60
MAX_PAGES_TOTAL = 400          # ~400k markets: far above today's ~130k

MAX_CANDIDATES = 40            # combos sent to depth checks per (type, tier)
MAX_EVENT_LEGS = 40            # skip Type B events with more outcomes than this
MAX_ORDERBOOK_FETCHES = 200
MAX_EDGE_ORDERBOOK_FETCHES = 120   # Tier 2 books, fetched on top of Tier 1's budget

DETAIL_TIMEOUT = 10           # orderbook/event/series calls: fail fast, scan on
DETAIL_RETRIES = 2

_HTTP_SEM = threading.BoundedSemaphore(LISTING_WORKERS + 1)   # +1: the Polymarket thread
_TLS = threading.local()
_FEE_MULT_LIVE = {}            # series -> multiplier fetched live this run (cacheable)
_FEE_MULT_FAILED = set()       # series whose live fetch failed (multiplier unknown)
_FEE_MULT_LOCK = threading.Lock()


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def to_cc(dollars):
    """Dollar price string/float -> integer centicents (1c = 100cc)."""
    return int(round(num(dollars) * 10000))


def cc_to_cents(cc):
    """Integer centicents -> cents (int when whole, else 2-decimal float)."""
    cc = int(cc)
    return cc // 100 if cc % 100 == 0 else round(cc / 100, 2)


def iso_now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- fees ----

def taker_fee_cents(price_cents, contracts=1, multiplier=1):
    """Kalshi taker fee in whole cents for one order of `contracts` at `price_cents`.

    Official schedule: fees = round up(M x 0.07 x C x P x (1-P)), P in dollars.
    Rounded up to the next whole cent (Kalshi rounds to a centicent), so this
    never understates the fee. 0 for C<=0, M<=0 or P outside (0, 100).
    """
    cc = int(round(num(price_cents) * 100))
    if contracts <= 0 or cc <= 0 or cc >= 10000:
        return 0
    m = Fraction(str(multiplier))
    if m <= 0:
        return 0
    # fee in cents = 0.07 * M * C * (cc/1e4) * (1 - cc/1e4) * 100
    fee = KALSHI_TAKER_RATE * m * contracts * cc * (10000 - cc) / 10 ** 6
    return math.ceil(fee)


def taker_fee_exact_cents(price_cents, multiplier=1):
    """Unrounded per-contract Kalshi taker fee in cents (lower bound, screening)."""
    p = num(price_cents) / 100
    if not 0 < p < 1:
        return 0.0
    return 0.07 * float(multiplier) * p * (1 - p) * 100


def poly_fee_cc(price_cents, contracts, rate):
    """Polymarket taker fee in centicents: C x rate x p x (1-p), rounded up."""
    p = num(price_cents) / 100
    if contracts <= 0 or rate <= 0 or not 0 < p < 1:
        return 0
    return math.ceil(contracts * rate * p * (1 - p) * 10000 - 1e-9)


def leg_fee_cc(leg, contracts):
    """Taker fee in centicents for buying `contracts` of one leg."""
    if leg.get("venue", "kalshi") == "polymarket":
        return poly_fee_cc(leg["price_cents"], contracts, leg.get("fee_rate", 0.0))
    return 100 * taker_fee_cents(leg["price_cents"], contracts, leg.get("fee_mult", 1))


def leg_fee_lb_cents(leg):
    """Per-contract fee lower bound in cents (no rounding) for cheap screening."""
    if leg.get("venue", "kalshi") == "polymarket":
        p = num(leg["price_cents"]) / 100
        return leg.get("fee_rate", 0.0) * p * (1 - p) * 100
    return taker_fee_exact_cents(leg["price_cents"], leg.get("fee_mult", 1))


# --------------------------------------------------------- combo math ----

def combo_cost_cc(legs, sets):
    """All-in cost (asks + per-order taker fees) in centicents for `sets` sets."""
    return sum(sets * int(round(num(l["price_cents"]) * 100)) + leg_fee_cc(l, sets)
               for l in legs)


def screen_edge_cents(legs):
    """Optimistic per-set edge (100 - asks - unrounded fees); <=0 can't profit."""
    return 100 - sum(num(l["price_cents"]) + leg_fee_lb_cents(l) for l in legs)


def affordable_sets(legs, max_outlay_cents):
    """Largest set count whose all-in cost fits in max_outlay_cents.

    Equals floor(max_outlay / combo_cost) where combo_cost is the per-set
    all-in cost at that size (fees are per order, so per-set cost falls
    slightly as size grows).
    """
    gross_cc = sum(int(round(num(l["price_cents"]) * 100)) for l in legs)
    budget_cc = int(round(max_outlay_cents * 100))
    if gross_cc <= 0 or budget_cc <= 0:
        return 0
    s = budget_cc // gross_cc
    while s > 0 and combo_cost_cc(legs, s) > budget_cc:
        s -= 1
    return s


def _fill(legs, depth_sets, max_outlay_cents, min_outlay_cents):
    """(sets, affordable_sets, outlay_cc) at min(depth, affordable), or None when
    the book can't fill min_outlay at quoted prices."""
    if not legs:
        return None
    afford = affordable_sets(legs, max_outlay_cents)
    fill = min(int(depth_sets), afford)
    if fill <= 0:
        return None
    outlay_cc = combo_cost_cc(legs, fill)
    if outlay_cc < int(round(min_outlay_cents * 100)):
        return None
    return fill, afford, outlay_cc


def _sizing(legs, depth_sets, fill, afford, outlay_cc):
    return {
        "sets": fill,
        "depth_sets": int(depth_sets),
        "affordable_sets": afford,
        "outlay_cents": cc_to_cents(outlay_cc),
        "fees_cents": cc_to_cents(sum(leg_fee_cc(l, fill) for l in legs)),
        "combo_cost_cents": round(outlay_cc / fill / 100, 4),
    }


def evaluate_combo(legs, depth_sets, max_outlay_cents, min_outlay_cents, min_roi=0.0):
    """Size and price a combo whose legs pay exactly 100c per set at settlement.

    Returns a sizing dict, or None when it must not be flagged: book can't
    fill min_outlay at quoted prices, after-fee profit <= 0 (including from
    cent rounding), or after-fee ROI below min_roi.
    """
    f = _fill(legs, depth_sets, max_outlay_cents, min_outlay_cents)
    if f is None:
        return None
    fill, afford, outlay_cc = f
    profit_cc = fill * 10000 - outlay_cc
    if profit_cc <= 0:
        return None
    roi = profit_cc / outlay_cc * 100
    if roi < min_roi:
        return None
    return _sizing(legs, depth_sets, fill, afford, outlay_cc) | {
        "locked_profit_cents": cc_to_cents(profit_cc),
        "roi_pct": round(roi, 2),
    }


def pre_fee_edge_cc(legs):
    """Per-set edge before any fees, in centicents: 100c minus the summed asks."""
    return 10000 - sum(int(round(num(l["price_cents"]) * 100)) for l in legs)


def evaluate_edge(legs, depth_sets, max_outlay_cents, min_outlay_cents, min_edge_cents,
                  pre_fee=False):
    """Tier 2 sizing: same depth/outlay gates as evaluate_combo. By default the
    cent-rounded after-fee edge per set has to be > min_edge_cents. With
    pre_fee (Type A near-arbs) the PRE-fee edge per set has to be strictly
    > min_edge_cents and is what edge_cents_per_set reports. No locked profit
    is claimed."""
    f = _fill(legs, depth_sets, max_outlay_cents, min_outlay_cents)
    if f is None:
        return None
    fill, afford, outlay_cc = f
    if pre_fee:
        gross_cc = pre_fee_edge_cc(legs)
        if gross_cc <= min_edge_cents * 100:
            return None
        return _sizing(legs, depth_sets, fill, afford, outlay_cc) | {
            "edge_cents_per_set": cc_to_cents(gross_cc),
        }
    profit_cc = fill * 10000 - outlay_cc
    if profit_cc <= min_edge_cents * 100 * fill:
        return None
    return _sizing(legs, depth_sets, fill, afford, outlay_cc) | {
        "edge_cents_per_set": round(profit_cc / fill / 100, 2),
    }


def evaluate_model_edge(leg, depth_sets, fair_taken_cc, max_outlay_cents, min_outlay_cents):
    """Tier 3 sizing for one directional leg worth fair_taken_cc per contract
    under the model. Same depth/outlay gates as the other tiers; the expected
    value after cent-rounded taker fees has to be >= MODEL_EDGE_MIN_CENTS per
    set. A model estimate, never a locked profit."""
    legs = [leg]
    f = _fill(legs, depth_sets, max_outlay_cents, min_outlay_cents)
    if f is None:
        return None
    fill, afford, outlay_cc = f
    ev_cc = fill * int(fair_taken_cc) - combo_cost_cc(legs, fill)
    if ev_cc < MODEL_EDGE_MIN_CENTS * 100 * fill:
        return None
    return _sizing(legs, depth_sets, fill, afford, outlay_cc) | {
        "fair_cents": round(fair_taken_cc / 100, 2),
        "edge_cents_per_set": round(ev_cc / fill / 100, 2),
        "expected_roi_pct": round(ev_cc / outlay_cc * 100, 2),
        "roi_if_right_pct": round((fill * 10000 - outlay_cc) / outlay_cc * 100, 2),
    }


def rank_combos(combos):
    """roi_pct desc, then locked_profit_cents desc; edge entries (which carry
    neither) by edge_cents_per_set desc, with Type A pre-fee near-arbs after
    the after-fee edges."""
    return sorted(combos, key=lambda c: (-c.get("roi_pct", 0), -c.get("locked_profit_cents", 0),
                                         c.get("tier") == "edge" and c.get("combo_type") == "A",
                                         -c.get("edge_cents_per_set", 0)))


def combo_key(combo_type, tickers, side, tier="locked"):
    key = f"{combo_type}|{','.join(sorted(tickers))}|{side}"
    return key if tier == "locked" else f"{tier}|{key}"


# ------------------------------------------------------- listing quotes ----

def quote(m):
    """(yes_ask_cc, yes_ask_size, no_ask_cc, no_ask_size) from a listing row.

    An ask of 0 or >= $1 means "no offer". The NO ask is 1 - best YES bid, so
    its size is the YES-bid size.
    """
    ya, na = to_cc(m.get("yes_ask_dollars")), to_cc(m.get("no_ask_dollars"))
    ya = ya if 0 < ya < 10000 else None
    na = na if 0 < na < 10000 else None
    return (ya, num(m.get("yes_ask_size_fp")), na, num(m.get("yes_bid_size_fp")))


def expiry_of(m):
    return m.get("expected_expiration_time") or m.get("close_time") or m.get("expiration_time")


def screen_type_a(markets, near_arb=False):
    """Single-market Yes+No candidates whose optimistic after-fee edge > 0
    (tier "locked"). With near_arb, pairs that aren't locked but whose
    combined PRE-fee ask is strictly below A_NEAR_ARB_MAX_ASK_CENTS are
    returned too as tier "edge", with "edge" = the pre-fee edge in cents."""
    out = []
    for m in markets:
        ya, _, na, _ = quote(m)
        if ya is None or na is None:
            continue
        legs = [{"ticker": m["ticker"], "side": "yes", "price_cents": cc_to_cents(ya)},
                {"ticker": m["ticker"], "side": "no", "price_cents": cc_to_cents(na)}]
        edge = screen_edge_cents(legs)
        if edge > 0:
            tier = "locked"
        elif near_arb and ya + na < A_NEAR_ARB_MAX_ASK_CENTS * 100:
            tier, edge = "edge", pre_fee_edge_cc(legs) / 100
        else:
            continue
        out.append({"combo_type": "A", "side": "yes+no", "legs": legs,
                    "edge": edge, "markets": [m], "tier": tier})
    return out


def event_yes_legs(ms):
    """One Yes leg at the listing ask per market, or None if any market has no Yes ask."""
    legs = []
    for m in ms:
        ya = quote(m)[0]
        if ya is None:
            return None
        legs.append({"ticker": m["ticker"], "side": "yes", "price_cents": cc_to_cents(ya)})
    return legs


def screen_type_b(markets, skipped=None):
    """Event-book candidates: every open outcome has a Yes ask, sum + fees < 100,
    and the outcomes' strikes tile the number line (exactly one must pay).

    Events that pass the price screen but whose exhaustiveness can't be
    proven from strikes (named outcomes, cumulative ladders) are counted in
    skipped["B_unverified_exhaustive"] and never flagged. Events with more
    than MAX_EVENT_LEGS outcomes are skipped and counted in
    skipped["B_too_many_outcomes"].
    """
    by_event = {}
    for m in markets:
        by_event.setdefault(m.get("event_ticker"), []).append(m)
    out = []
    for et, ms in by_event.items():
        if not et or len(ms) < 2:
            continue
        if len(ms) > MAX_EVENT_LEGS:
            if skipped is not None:
                skipped["B_too_many_outcomes"] = skipped.get("B_too_many_outcomes", 0) + 1
            continue
        legs = event_yes_legs(ms)
        if legs is None:
            continue
        edge = screen_edge_cents(legs)
        if edge <= 0:
            continue
        if not brackets_exhaustive(ms):
            if skipped is not None:
                skipped["B_unverified_exhaustive"] = skipped.get("B_unverified_exhaustive", 0) + 1
            continue
        out.append({"combo_type": "B", "side": "yes", "legs": legs,
                    "edge": edge, "markets": ms, "event_ticker": et, "tier": "locked"})
    return out


def _strike_value(x):
    """Finite numeric strike as float, else None (strings/bools fail closed)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return float(x)


def ladder_kind(markets):
    """(strike_type, strike_field) when the markets form a cumulative ladder:
    >= 2 markets, one shared non-empty title, one shared expiry (the locked
    pair's >= $1 guarantee requires the legs to settle on the same value at
    the same time), ALL strike_type "less" with a numeric cap_strike or ALL
    "greater" with a numeric floor_strike, strikes pairwise distinct.
    Anything else -> None."""
    if len(markets) < 2:
        return None
    titles = {m.get("title") for m in markets}
    if len(titles) != 1 or not next(iter(titles)):
        return None
    expiries = {expiry_of(m) for m in markets}
    if len(expiries) != 1 or not next(iter(expiries)):
        return None
    st = markets[0].get("strike_type")
    field = {"less": "cap_strike", "greater": "floor_strike"}.get(st)
    if field is None or any(m.get("strike_type") != st for m in markets):
        return None
    strikes = [_strike_value(m.get(field)) for m in markets]
    if None in strikes or len(set(strikes)) != len(strikes):
        return None
    return st, field


def screen_type_d(markets, stats=None):
    """Cumulative-ladder pairs. Below ladder ("less", cap_lo < cap_hi):
    YES(hi) + NO(lo) pays 100c or 200c on any settlement value. Above ladder
    ("greater", floor_lo < floor_hi): YES(lo) + NO(hi) likewise. Keeps pairs
    whose optimistic after-fee edge > 0; the reverse pairing (which can pay
    0) is never emitted. stats["D_ladder_events"] counts detected ladders."""
    by_event = {}
    for m in markets:
        by_event.setdefault(m.get("event_ticker"), []).append(m)
    out, n_ladders = [], 0
    for et, ms in by_event.items():
        kind = ladder_kind(ms) if et else None
        if kind is None:
            continue
        n_ladders += 1
        st, field = kind
        ms = sorted(ms, key=lambda m: m[field])
        for i, lo in enumerate(ms):
            for hi in ms[i + 1:]:
                m_yes, m_no = (hi, lo) if st == "less" else (lo, hi)
                ya, na = quote(m_yes)[0], quote(m_no)[2]
                if ya is None or na is None:
                    continue
                legs = [{"ticker": m_yes["ticker"], "side": "yes", "price_cents": cc_to_cents(ya)},
                        {"ticker": m_no["ticker"], "side": "no", "price_cents": cc_to_cents(na)}]
                edge = screen_edge_cents(legs)
                if edge <= 0:
                    continue
                out.append({"combo_type": "D", "side": "yes+no", "legs": legs, "edge": edge,
                            "markets": [m_yes, m_no], "event_ticker": et, "tier": "locked"})
    if stats is not None:
        stats["D_ladder_events"] = n_ladders
    return out


def listing_mid(ya_cc, na_cc):
    """YES-oriented mid in dollars from the YES ask and NO ask (YES bid = 1 - NO ask)."""
    return (ya_cc + 10000 - na_cc) / 2 / 10000


def live_model_windows(markets, now_ts):
    """[(market, kraken_pair, w_open, w_close)] for supported 15-min crypto
    windows that are live (w_open <= now < w_close) and past warmup."""
    out = []
    for m in markets:
        w = me.market_window(m)
        if w is None:
            continue
        sym, wo, wc = w
        if not wo <= now_ts < wc or me.in_warmup(wo, now_ts):
            continue
        out.append((m, me.KRAKEN_PAIRS[sym], wo, wc))
    return out


def screen_model_edge(markets, now_ts, kraken, errors, stats=None):
    """Tier 3 candidates from listing asks: model fair value (shrunk toward the
    mid in the 0.40-0.60 band) picks the side; the take must agree with the
    market's lean and fair_taken - ask_taken >= MODEL_EDGE_MIN_CENTS.
    Windows whose pair has no Kraken data in `kraken` are skipped (the fetch
    failure was recorded in `errors` when it happened)."""
    out, no_fair = [], 0
    for m, pair, wo, wc in live_model_windows(markets, now_ts):
        ya, _, na, _ = quote(m)
        data = kraken.get(pair)
        if ya is None or na is None or data is None:
            continue
        fr = me.fair_15m(pair, wo, wc, now_ts, *data)
        if fr is None:
            no_fair += 1
            continue
        fair_raw, p_ref, p_now, sigma, t = fr
        mid = listing_mid(ya, na)
        fair = me.shrink_midrange(fair_raw, mid)
        side = "YES" if fair > mid else "NO"
        if not me.agrees_with_lean(side, mid):
            continue
        ask_cc = ya if side == "YES" else na
        fair_taken_cc = int(round((fair if side == "YES" else 1 - fair) * 10000))
        if fair_taken_cc - ask_cc < MODEL_EDGE_MIN_CENTS * 100:
            continue
        out.append({
            "combo_type": "M", "tier": "model-edge", "side": side, "ticker": m["ticker"],
            "legs": [{"ticker": m["ticker"], "side": side.lower(),
                      "price_cents": cc_to_cents(ask_cc), "venue": "kalshi"}],
            "fair_taken_cc": fair_taken_cc, "edge": (fair_taken_cc - ask_cc) / 100,
            "model": {"pair": pair, "fair_raw": round(fair_raw, 4), "fair_yes": round(fair, 4),
                      "listing_mid": round(mid, 4), "p_ref": p_ref, "p_now": p_now,
                      "sigma_per_min": round(sigma, 6), "minutes_left": round(t, 2)},
            "markets": [m], "title": m.get("title"),
        })
    if stats is not None:
        stats["M_no_fair"] = stats.get("M_no_fair", 0) + no_fair
    return out


def _strike_step(values):
    """Finest decimal step the strikes are quoted in (82 -> 1, 3.25 -> 0.01)."""
    places = 0
    for v in values:
        s = repr(float(v)).rstrip("0").rstrip(".")
        if "." in s:
            places = max(places, len(s.split(".")[1]))
    return 10 ** -places


def brackets_exhaustive(markets):
    """True when the markets' strikes tile the whole number line.

    Needs one open-below leg (less/less_or_equal), one open-above leg
    (greater/greater_or_equal) and between-legs chained with no gaps.
    Kalshi quotes "between" brackets inclusively in the underlying's
    reporting unit (81-82, 83-84 for whole-degree temperatures), so two
    inclusive bounds may be one strike step apart. "less" (value < cap) and
    "greater" (value > floor) are strict: cap 81 is "80 or below", so a
    strict bound must meet its neighbour with no gap (two strict bounds must
    overlap). Anything else -> False (unverifiable), including a needed
    strike that is not a finite int/float (strings, bools, NaN/inf fail
    closed via _strike_value instead of raising or coercing).
    """
    ivs = []    # (lo, hi, lo_inclusive, hi_inclusive)
    for m in markets:
        st = m.get("strike_type")
        lo, hi = m.get("floor_strike"), m.get("cap_strike")
        if st in ("less", "less_or_equal") and hi is not None:
            iv = _strike_value(hi)
            if iv is None:
                return False
            ivs.append((-math.inf, iv, False, st == "less_or_equal"))
        elif st in ("greater", "greater_or_equal") and lo is not None:
            iv = _strike_value(lo)
            if iv is None:
                return False
            ivs.append((iv, math.inf, st == "greater_or_equal", False))
        elif st == "between" and lo is not None and hi is not None:
            lov, hiv = _strike_value(lo), _strike_value(hi)
            if lov is None or hiv is None:
                return False
            ivs.append((lov, hiv, True, True))
        else:
            return False
    ivs.sort(key=lambda iv: iv[:2])
    if not ivs or ivs[0][0] != -math.inf or ivs[-1][1] != math.inf:
        return False
    step = _strike_step([v for iv in ivs for v in iv[:2] if math.isfinite(v)])
    for (_, prev_hi, _, hi_inc), (lo, _, lo_inc, _) in zip(ivs, ivs[1:]):
        if hi_inc and lo_inc:
            if lo - prev_hi > step + 1e-9:
                return False
        elif hi_inc or lo_inc:
            if lo > prev_hi:
                return False
        elif lo >= prev_hi:
            return False
    return True


# ----------------------------------------------------------------- state ----

def _state_backup_path():
    # Derived dynamically: STATE_PATH is monkeypatched to tmp dirs in tests.
    return STATE_PATH + ".bak"


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"alerted": {}}
    except Exception:
        # Corrupt read: restore the last good backup instead of defaulting
        # to empty (which would wipe dedupe history and re-alert everything
        # on the next mark run).
        try:
            with open(_state_backup_path()) as f:
                return json.load(f)
        except Exception:
            return {"alerted": {}}


def save_state(s):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    try:
        os.replace(STATE_PATH, _state_backup_path())  # keep last good copy
    except FileNotFoundError:
        pass
    os.replace(tmp, STATE_PATH)  # atomic: readers never see a half-written file


def split_new(combos, alerted, now_iso, mark, key_fn=None):
    """(new_combos, repeat_count). With mark, new keys are stamped in alerted.
    key_fn(c) overrides the Tier 1/2/3 key (Tier 4 picks)."""
    new, repeats = [], 0
    for c in combos:
        key = key_fn(c) if key_fn else combo_key(
            c["combo_type"], [l["ticker"] for l in c["legs"]], c["side"], c.get("tier", "locked"))
        if key in alerted:
            repeats += 1
            continue
        new.append(c)
        if mark:
            alerted[key] = {"ts": now_iso, "expiry": c.get("expiry"),
                            "roi_pct": c.get("roi_pct")}
    return new, repeats


def prune_alerted(alerted, now_ts):
    """Drop keys whose combo expired over a day ago (keeps the file small)."""
    for k in list(alerted):
        exp = _parse_ts((alerted[k] or {}).get("expiry"))
        if exp is not None and exp < now_ts - 86400:
            del alerted[k]


def _parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ------------------------------------------------------------------ http ----

def _host_session(url):
    """Per-thread, per-host Session (saves a TLS handshake per call). Never
    shared across hosts: a Session reused across hosts through the egress
    proxy was seen to stall until read timeout."""
    sessions = getattr(_TLS, "sessions", None)
    if sessions is None:
        sessions = _TLS.sessions = {}
    host = urlsplit(url).netloc
    s = sessions.get(host)
    if s is None:
        s = sessions[host] = requests.Session()
    return s


def http_get_json(url, params=None, timeout=HTTP_TIMEOUT, retries=HTTP_RETRIES, pooled=True,
                  deadline=None):
    """GET -> JSON with backoff + jitter on 429/5xx/network errors.

    At most LISTING_WORKERS + 1 requests are in flight process-wide. Raises
    the last error once retries are exhausted (or a retry would start past
    `deadline`); callers record it and move on. pooled=True (default) uses
    the calling thread's Session for the URL's host.
    """
    get = _host_session(url).get if pooled else requests.get
    last = None
    for attempt in range(retries + 1):
        wait = None
        try:
            with _HTTP_SEM:
                r = get(url, params=params, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                last = RuntimeError(f"HTTP {r.status_code} {url}")
                wait = num(r.headers.get("Retry-After")) or None
            else:
                r.raise_for_status()
                return r.json()
        except requests.HTTPError:
            raise
        except (requests.RequestException, ValueError) as e:
            last = e
        if attempt < retries:
            backoff = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** attempt)
            pause = min(BACKOFF_CAP_S, wait or backoff) + random.uniform(0, backoff / 2)
            if deadline is not None and time.time() + pause > deadline:
                break
            time.sleep(pause)
    raise last


_DETAIL_POOL = None
_DETAIL_POOL_LOCK = threading.Lock()


def _parallel_map(fn, items):
    """list(map(fn, items)) on a small persistent thread pool (its per-thread
    Sessions survive across phases); result order is preserved. fn must not
    call _parallel_map itself."""
    global _DETAIL_POOL
    items = list(items)
    if not items:
        return []
    with _DETAIL_POOL_LOCK:
        if _DETAIL_POOL is None:
            _DETAIL_POOL = ThreadPoolExecutor(max_workers=IO_WORKERS)
    return list(_DETAIL_POOL.map(fn, items))


# ---------------------------------------------------------------- cache ----

def _cache_path(name):
    # Derived at call time: CACHE_DIR is monkeypatched to tmp dirs in tests.
    return os.path.join(CACHE_DIR, name)


def read_cache(name):
    """Parsed JSON cache file, or None on any error (missing, corrupt, ...)."""
    try:
        with open(_cache_path(name)) as f:
            return json.load(f)
    except Exception:
        return None


def write_cache(name, obj):
    """Atomic best-effort write; returns False instead of raising."""
    try:
        path = _cache_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# --------------------------------------------------------------- kalshi ----

def seed_ranges(now_ts):
    """Disjoint inclusive [min_close_ts, max_close_ts] ranges covering all closes."""
    pts = [CLOSE_TS_MIN] + [now_ts + h * 3600 for h in SEED_SPLIT_HOURS] + [CLOSE_TS_MAX]
    return [(lo if i == 0 else lo + 1, hi) for i, (lo, hi) in enumerate(zip(pts[:-1], pts[1:]))]


def split_range(lo, hi, close_ts):
    """Split inclusive [lo, hi] at the median sampled close time.

    Returns two disjoint inclusive ranges, or None when lo == hi. A median
    sitting on lo isolates that second ([lo, lo] + [lo+1, hi]), so a point
    mass of markets sharing one close time ends up in its own range.
    """
    if lo >= hi:
        return None
    inside = sorted(int(t) for t in close_ts if lo <= t <= hi)
    s = inside[len(inside) // 2] if inside else (lo + hi) // 2
    s = int(min(max(s, lo + 1), hi))
    return (lo, s - 1), (s, hi)


def point_mass_ts():
    return [int(_parse_ts(s)) for s in POINT_MASS_CLOSE_ISO]


def presplit_point_masses(ranges, points):
    """Give each known point-mass close time its own one-second range: a range
    containing ts becomes [lo, ts-1], [ts, ts], [ts+1, hi] (empty pieces
    dropped). Coverage and disjointness are preserved."""
    out = list(ranges)
    for ts in points:
        nxt = []
        for lo, hi in out:
            if lo <= ts <= hi and lo < hi:
                nxt.extend(r for r in ((lo, ts - 1), (ts, ts), (ts + 1, hi)) if r[0] <= r[1])
            else:
                nxt.append((lo, hi))
        out = nxt
    return out


def plan_ranges(close_ts, target=PLAN_SHARD_MARKETS):
    """Contiguous [lo, hi, count] shards tiling CLOSE_TS_MIN..CLOSE_TS_MAX, each
    holding about `target` of the given close times; a single second with
    >= target markets gets its own shard."""
    counts = Counter(int(t) for t in close_ts if CLOSE_TS_MIN <= t <= CLOSE_TS_MAX)
    out, lo, acc = [], CLOSE_TS_MIN, 0
    for t in sorted(counts):
        c = counts[t]
        if c >= target:
            if t > lo:
                out.append([lo, t - 1, acc])
            out.append([t, t, c])
            lo, acc = t + 1, 0
            continue
        if acc and acc + c > target:
            out.append([lo, t - 1, acc])
            lo, acc = t, 0
        acc += c
    if lo <= CLOSE_TS_MAX:
        out.append([lo, CLOSE_TS_MAX, acc])
    return out


def valid_plan(ranges):
    """True when ranges are int [lo, hi, count] triples tiling CLOSE_TS_MIN..CLOSE_TS_MAX."""
    try:
        if not ranges or ranges[0][0] != CLOSE_TS_MIN or ranges[-1][1] != CLOSE_TS_MAX:
            return False
        prev = CLOSE_TS_MIN - 1
        for lo, hi, n in ranges:
            if any(type(v) is not int for v in (lo, hi, n)) or lo != prev + 1 or hi < lo or n < 0:
                return False
            prev = hi
        return True
    except Exception:
        return False


def load_sweep_plan(now_ts):
    """Cached shards [(lo, hi, count)] from the last complete sweep, or None."""
    d = read_cache("sweep_plan.json")
    try:
        if 0 <= now_ts - d["saved_ts"] <= PLAN_TTL_S and valid_plan(d["ranges"]):
            return [tuple(r) for r in d["ranges"]]
    except Exception:
        pass
    return None


def save_sweep_plan(markets, now_ts):
    ts = [t for t in (_parse_ts(m.get("close_time")) for m in markets) if t is not None]
    return write_cache("sweep_plan.json", {"saved_ts": int(now_ts), "markets": len(markets),
                                           "ranges": plan_ranges(ts)})


def ordered_remainder(lo, close_ts):
    """The listing returns a range's rows by close time, descending (checked
    live 2026-10-01, and re-checked here on every range). When the rows so
    far are non-increasing, every market closing after the last row's close
    time c has been seen and only [lo, c] is left: returns c. None when any
    row lacks a close time or the rows aren't monotone."""
    if not close_ts or None in close_ts or any(a < b for a, b in zip(close_ts, close_ts[1:])):
        return None
    return max(lo, int(close_ts[-1]))


def halve(lo, hi):
    """Inclusive [lo, hi] -> two disjoint halves at the midpoint, or None when lo == hi."""
    if lo >= hi:
        return None
    m = (lo + hi + 1) // 2
    return (lo, m - 1), (m, hi)


def remainder_split(lo, c):
    """Unseen remainder [lo, c] -> work ranges, or None when c <= lo. The last
    row's second c may hold more markets than were seen (many share a close
    time), so it gets its own range [c, c] (re-paged once, never split again)
    and [lo, c-1] is halved. Without that, every child would re-fetch c."""
    if c <= lo:
        return None
    return [*(halve(lo, c - 1) or [(lo, c - 1)]), (c, c)]


class _Counter:
    def __init__(self, n):
        self.n, self.lock, self.kinds = n, threading.Lock(), Counter()

    def take(self):
        with self.lock:
            if self.n <= 0:
                return False
            self.n -= 1
            return True


def _item(lo, hi, split_after=1, cursor=None):
    return {"lo": int(lo), "hi": int(hi), "split_after": split_after, "cursor": cursor}


def fetch_range(item, deadline, pages_left, splits_left):
    """Page one close-time range -> (markets, sub_items, unfinished, error, pages).

    Once the range has paged `split_after` times (or for RANGE_SLICE_S) with
    the cursor still live, it stops and hands its unseen remainder to other
    workers as two halves (sub_items), until it is a single second or the
    split budget is spent. When the deadline, a page cap or an HTTP failure
    stops it early, `unfinished` is a work item for the rest and `error`
    says why; otherwise both are None.
    """
    lo, hi, cursor = item["lo"], item["hi"], item.get("cursor")
    fresh = cursor is None          # ts then covers the chain from its start
    out, ts, pages, seen, started = [], [], 0, set(), time.time()

    def rest():
        c = ordered_remainder(lo, ts) if fresh else None
        return _item(lo, c) if c is not None else dict(item, cursor=cursor)

    while True:
        if time.time() > deadline:
            return out, [], rest(), f"sweep time budget hit after {pages} pages", pages
        if pages >= MAX_PAGES_PER_RANGE or not pages_left.take():
            return out, [], rest(), f"page cap hit after {pages} pages", pages
        params = {"limit": 1000, "status": "open", "mve_filter": "exclude",
                  "min_close_ts": lo, "max_close_ts": hi}
        if cursor:
            params["cursor"] = cursor
        try:
            d = http_get_json(f"{KALSHI_API}/markets", params, timeout=LISTING_TIMEOUT,
                              retries=LISTING_RETRIES, deadline=deadline)
        except Exception as e:
            return out, [], rest(), f"{type(e).__name__}: {e}", pages
        mkts = d.get("markets") or []
        out.extend(mkts)
        ts.extend(_parse_ts(m.get("close_time")) for m in mkts)
        pages += 1
        cursor = d.get("cursor")
        if not mkts or not cursor or cursor in seen:
            return out, [], None, None, pages
        seen.add(cursor)
        if lo < hi and (pages >= item["split_after"] or time.time() - started > RANGE_SLICE_S):
            c = ordered_remainder(lo, ts) if fresh else None
            if c is not None:
                parts = remainder_split(lo, c)      # c == lo: one second left, keep paging
            else:
                parts = split_range(lo, hi, [t for t in ts if t is not None])
            if parts and splits_left.take():
                with splits_left.lock:
                    splits_left.kinds["ordered" if c is not None else "restart"] += 1
                return out, [_item(a, b, item["split_after"]) for a, b in parts], None, None, pages


def _work_queue(seeds, fn, workers=LISTING_WORKERS):
    """Run fn(item) -> (result, more_items) on `workers` threads until the
    queue drains; more_items are queued ahead of older work."""
    pending, results = list(seeds), []
    cond, active = threading.Condition(), [0]

    def worker():
        while True:
            with cond:
                while not pending and active[0]:
                    cond.wait()
                if not pending:
                    return
                item = pending.pop(0)
                active[0] += 1
            res, more = fn(item)
            with cond:
                results.append(res)
                pending[:0] = more
                active[0] -= 1
                cond.notify_all()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


class SweepResult(list):
    """Ticker-deduped markets; `.stats` is the sweep's coverage proof."""
    stats = None


def sweep_seeds(plan, now_ts):
    """Work items for the main pass, longest expected chains first. With a
    plan its shards are used (re-split only past RESPLIT_AFTER_PAGES);
    otherwise coarse seeds split after their first page."""
    points = point_mass_ts()
    if plan:
        ranges = presplit_point_masses([(lo, hi) for lo, hi, _ in sorted(plan, key=lambda r: -r[2])],
                                       points)
        return [_item(lo, hi, RESPLIT_AFTER_PAGES) for lo, hi in ranges]
    ranges = sorted(presplit_point_masses(seed_ranges(now_ts), points), key=lambda r: r[0] != r[1])
    return [_item(lo, hi, 1) for lo, hi in ranges]


def kalshi_open_markets(now_ts, errors, deadline):
    """All open non-multivariate Kalshi markets, deduped by ticker, as a
    SweepResult. Ranges unfinished at `deadline` get one rescue pass (split
    finer) until deadline + RESCUE_BUDGET_S; whatever is still unfinished is
    listed in stats["ranges_budget_hit"] and errors, never dropped silently."""
    t_start = time.time()
    plan = load_sweep_plan(now_ts)
    pages_left, lock = _Counter(MAX_PAGES_TOTAL), threading.Lock()
    out, seen = SweepResult(), set()
    st = {"markets_scanned": 0, "markets_unique": 0, "pages_fetched": 0,
          "ranges_completed": 0, "ranges_retried": 0, "ranges_budget_hit": [],
          "sweep_seconds": 0.0, "sweep_plan": "cached" if plan else "seeded"}

    def run_pass(items, until, splits):
        unfinished = []

        def job(item):
            try:
                mkts, more, rest, err, pages = fetch_range(item, until, pages_left, splits)
            except Exception as e:
                mkts, more, rest, err, pages = [], [], item, f"{type(e).__name__}: {e}", 0
            with lock:
                st["pages_fetched"] += pages
                st["markets_scanned"] += len(mkts)
                for m in mkts:
                    t = m.get("ticker")
                    if t and t not in seen:
                        seen.add(t)
                        out.append(m)
                if rest is not None:
                    unfinished.append((rest, err))
                elif not more:
                    st["ranges_completed"] += 1
            return None, more

        _work_queue(items, job)
        return unfinished

    main_splits, rescue_splits = _Counter(MAX_RANGE_SPLITS), _Counter(RESCUE_RANGE_SPLITS)
    unfinished = run_pass(sweep_seeds(plan, now_ts), deadline, main_splits)
    if unfinished and time.time() < deadline + RESCUE_BUDGET_S:
        st["ranges_retried"] = len(unfinished)
        unfinished = run_pass([dict(it, split_after=1) for it, _ in unfinished],
                              deadline + RESCUE_BUDGET_S, rescue_splits)
    st["range_splits"] = dict(main_splits.kinds + rescue_splits.kinds)
    for it, err in unfinished:
        msg = f"range ({it['lo']}, {it['hi']}): {err}"
        errors.append(msg)
        st["ranges_budget_hit"].append(msg)
    st["markets_unique"] = len(out)
    st["sweep_seconds"] = round(time.time() - t_start, 1)
    st["coverage_complete"] = not unfinished
    if unfinished:
        st["markets_unscanned_est"] = (max(0, sum(r[2] for r in plan) - len(out))
                                       if plan else None)
    else:
        st["plan_saved"] = save_sweep_plan(out, now_ts)
    out.stats = st
    return out


def kalshi_orderbook_top(ticker):
    """{'yes': (ask_cc, size), 'no': (ask_cc, size)} from the top of book.

    The book lists bids only: YES ask = 1 - best NO bid (size = that bid's
    size) and vice versa. Missing side -> (None, 0).
    """
    d = http_get_json(f"{KALSHI_API}/markets/{ticker}/orderbook", {"depth": 1},
                      timeout=DETAIL_TIMEOUT, retries=DETAIL_RETRIES)
    ob = d.get("orderbook_fp") or {}

    def best(levels):
        lv = [(to_cc(p), num(s)) for p, s in (levels or [])]
        lv = [x for x in lv if 0 < x[0] < 10000 and x[1] > 0]
        return max(lv) if lv else None

    yb, nb = best(ob.get("yes_dollars")), best(ob.get("no_dollars"))
    return {"yes": (10000 - nb[0], nb[1]) if nb else (None, 0.0),
            "no": (10000 - yb[0], yb[1]) if yb else (None, 0.0)}


def kalshi_event(event_ticker):
    return http_get_json(f"{KALSHI_API}/events/{event_ticker}",
                         {"with_nested_markets": "true"},
                         timeout=DETAIL_TIMEOUT, retries=DETAIL_RETRIES)


def kalshi_fee_multiplier(series_ticker):
    """Series taker multiplier, never below the standard 1 (conservative).
    Successful live fetches are recorded for the disk cache; a failed fetch
    returns 1.0, is never cached and is recorded in _FEE_MULT_FAILED so
    callers can drop candidates whose real multiplier may be higher."""
    try:
        s = http_get_json(f"{KALSHI_API}/series/{series_ticker}",
                          timeout=DETAIL_TIMEOUT, retries=DETAIL_RETRIES).get("series") or {}
        m = max(1.0, num(s.get("fee_multiplier")) if s.get("fee_multiplier") is not None else 1.0)
    except Exception:
        with _FEE_MULT_LOCK:
            _FEE_MULT_FAILED.add(series_ticker)
        return 1.0
    with _FEE_MULT_LOCK:
        _FEE_MULT_FAILED.discard(series_ticker)
        _FEE_MULT_LIVE[series_ticker] = m
    return m


def cached_fee_multipliers(entries, series, now_ts):
    """{series: multiplier} for cache entries younger than FEE_MULT_TTL_S;
    malformed entries are skipped and values are floored at 1.0."""
    out = {}
    for s in series:
        try:
            e = entries[s]
            if 0 <= now_ts - e["ts"] <= FEE_MULT_TTL_S:
                out[s] = max(1.0, float(e["mult"]))
        except Exception:
            continue
    return out


def fee_multipliers(series, now_ts, stats=None):
    """{series: multiplier}: fresh disk-cache hits, live fetches for the rest.
    Any cache problem falls back to live fetches and never breaks the scan.
    Misses whose live fetch failed are listed in stats["fee_mult_unknown"]
    (key present only when non-empty)."""
    series = list(series)
    try:
        d = read_cache("series_fee_mult.json")
        entries = d.get("series") if isinstance(d, dict) else None
        entries = entries if isinstance(entries, dict) else {}
    except Exception:
        entries = {}
    hits = cached_fee_multipliers(entries, series, now_ts)
    misses = [s for s in series if s not in hits]
    out = dict(hits)
    out.update(zip(misses, _parallel_map(kalshi_fee_multiplier, misses)))
    with _FEE_MULT_LOCK:
        fetched = {s: _FEE_MULT_LIVE[s] for s in misses if s in _FEE_MULT_LIVE}
        unknown = sorted(s for s in misses if s in _FEE_MULT_FAILED)
    if stats is not None:
        stats["fee_mult_cache_hits"], stats["fee_mult_fetched"] = len(hits), len(misses)
        if unknown:
            stats["fee_mult_unknown"] = unknown
    if fetched:
        try:
            entries.update({s: {"mult": m, "ts": int(now_ts)} for s, m in fetched.items()})
            write_cache("series_fee_mult.json", {"series": entries})
        except Exception:
            pass
    return out


def series_of(m):
    return (m.get("event_ticker") or m.get("ticker") or "").split("-")[0]


def _drop_unknown_fee_mult(cands, unknown):
    """(kept, n_dropped): drops candidates with a Kalshi leg whose series fee
    multiplier is unknown (fetch failed), since 1.0 could understate fees."""
    if not unknown:
        return cands, 0
    kept = [c for c in cands
            if not any(l.get("venue", "kalshi") == "kalshi" and
                       series_of({"ticker": l["ticker"]}) in unknown for l in c["legs"])]
    return kept, len(cands) - len(kept)


# ----------------------------------------------------------- polymarket ----

def _poly_fee_rate(m):
    # Only an affirmative feesEnabled=False means fee-free. Absent fee info is
    # unknown -> conservative default (max category rate). A non-identity
    # feeSchedule exponent is likewise unknown (exponent < 1 would understate
    # the fee) -> conservative default. `not rate > 0` also catches NaN.
    if m.get("feesEnabled") is False:
        return 0.0
    fs = m.get("feeSchedule")
    if isinstance(fs, str):
        try:
            fs = json.loads(fs)
        except Exception:
            fs = None
    if not isinstance(fs, dict):
        fs = {}
    rate = num(fs.get("rate"))
    exponent = fs.get("exponent")
    if not rate > 0 or (exponent is not None and num(exponent) != 1):
        return POLY_DEFAULT_TAKER_RATE
    return rate


def poly_markets():
    """Liquid binary Yes/No Polymarket markets with live best bid/ask."""
    out, offset = [], 0
    while True:
        batch = http_get_json(f"{GAMMA_API}/events",
                              {"active": "true", "closed": "false",
                               "limit": POLY_PAGE, "offset": offset,
                               "order": "volume24hr", "ascending": "false"})
        if not batch:
            break
        for e in batch:
            for m in e.get("markets", []):
                try:
                    outcomes, tokens = m.get("outcomes"), m.get("clobTokenIds")
                    if isinstance(outcomes, str):
                        outcomes = json.loads(outcomes)
                    if isinstance(tokens, str):
                        tokens = json.loads(tokens)
                    if outcomes != ["Yes", "No"] or not tokens or len(tokens) != 2:
                        continue
                    if m.get("closed") or m.get("acceptingOrders") is False:
                        continue
                    vol = num(m.get("volume")) or num(e.get("volume"))
                    if vol < XVENUE_MIN_POLY_VOLUME:
                        continue
                    bid, ask = num(m.get("bestBid")), num(m.get("bestAsk"))
                    out.append({
                        "event": e.get("title"), "question": m.get("question"),
                        "slug": m.get("slug"),
                        "yes_ask_cc": to_cc(ask) if 0 < ask < 1 else None,
                        "no_ask_cc": to_cc(1 - bid) if 0 < bid < 1 else None,
                        "yes_token": tokens[0], "no_token": tokens[1],
                        "fee_rate": _poly_fee_rate(m),
                        "end_ts": _parse_ts(m.get("endDate") or e.get("endDate")),
                        "volume": vol,
                    })
                except Exception:
                    continue
        offset += POLY_PAGE
        if len(batch) < POLY_PAGE or offset >= POLY_MAX_OFFSET:
            break
    return out


def poly_best_ask(token_id):
    """(ask_cc, size) at the best ask of a Polymarket CLOB token, or (None, 0)."""
    d = http_get_json(f"{CLOB_API}/book", {"token_id": token_id},
                      timeout=DETAIL_TIMEOUT, retries=DETAIL_RETRIES)
    asks = [(to_cc(a.get("price")), num(a.get("size"))) for a in d.get("asks") or []]
    asks = [a for a in asks if 0 < a[0] < 10000 and a[1] > 0]
    return min(asks) if asks else (None, 0.0)


def normalize_title(title):
    t = re.sub(r"[^a-z0-9 ]", " ", (title or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    stop = {"will", "the", "a", "an", "be", "by", "of", "in", "on", "to",
            "for", "is", "it", "its", "at", "or", "and", "vs"}
    return " ".join(w for w in t.split() if w not in stop)


def _nfkd_fold(s):
    """NFKD-decompose and strip combining marks (é -> e), stdlib only."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def jaccard(a, b):
    sa, sb = set(a.split()), set(b.split())
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def kalshi_match_text(m):
    """Title plus the outcome sub-title: in multi-outcome events the title is
    the shared event question ("Who will win X?") and only the sub-title
    names the outcome this market pays on."""
    title, sub = m.get("title") or "", m.get("yes_sub_title") or ""
    kn = normalize_title(title)
    extra = [w for w in normalize_title(sub).split() if w not in set(kn.split())]
    return " ".join([kn] + extra).strip()


# Ordinals ("4th", "3rd") are placement-bearing numbers: "finish 4th" vs
# "3rd place" settle differently. Counting them in the number guard only ADDS
# rejections (a pair rejected today stays rejected); pairs whose ordinals
# agree, e.g. Kalshi "finish 3rd" vs Poly "3rd place", still pass.
_ORDINAL_RE = re.compile(r"^\d+(st|nd|rd|th)$")


def _num_tokens(words):
    return {w for w in words if w.isdigit() or _ORDINAL_RE.match(w)}


_GENERIC_SUB_LABELS = frozenset({"yes", "no"})  # binary-market sub-titles name no outcome


def same_proposition(km, kn, pn, pn_fold=None):
    """Guards on top of jaccard against near-identical titles that settle
    differently: identical numbers, the Kalshi outcome named on Polymarket,
    and no meaning-changing qualifier on only one side. pn_fold is pn built
    from accent-folded text, used only by the outcome-subset guard.

    The outcome-subset guard is skipped when the sub-title is a generic
    yes/no label (or empty): live binary markets use "Yes", which names no
    outcome, so requiring the label in the Polymarket title rejected every
    true binary match while discriminating nothing. A sub-title naming an
    outcome alongside the label ("Yes, Trump") still has its outcome words
    enforced. (Ported 2026-10-02 from the PolySentry xvenue_legs validation,
    where the defect was proven: 0 legs before, 1 correct leg after.)"""
    kw, pw = set(kn.split()), set(pn.split())
    if _num_tokens(kw) != _num_tokens(pw):
        return False
    # Accent-fold only this guard: normalize_title keeps a combining-form "é"
    # as "e" but turns a precomposed one into a space, so the same name can
    # differ by venue. Both sides get the same fold and subsets survive any
    # elementwise map (A <= B implies fold(A) <= fold(B)), so this only admits
    # pairs whose words differ solely by diacritics. (The one exception: a
    # word that today matches only as a precomposed-accent fragment, e.g.
    # "José" -> "jos" vs a literal "Jos".) A full fold of normalize_title would
    # shift jaccard scores and drop real matches below XVENUE_MATCH_MIN.
    sub_fold = (set(normalize_title(_nfkd_fold(km.get("yes_sub_title"))).split())
                - _GENERIC_SUB_LABELS)
    if sub_fold and not sub_fold <= set((pn_fold if pn_fold is not None else pn).split()):
        return False
    return not (kw ^ pw) & XVENUE_QUALIFIERS


def screen_type_c(kmarkets, pmarkets, edge_floor=None):
    """Cross-venue candidates: best title match (jaccard >= XVENUE_MATCH_MIN,
    same_proposition, ends within XVENUE_MAX_EXPIRY_GAP_S) whose optimistic
    after-fee edge >= XVENUE_MIN_EDGE_CENTS in either direction (tier
    "locked"). With edge_floor, gaps in [edge_floor, XVENUE_MIN_EDGE_CENTS)
    are returned too as tier "edge"."""
    pref = [(p, normalize_title(raw), normalize_title(_nfkd_fold(raw)))
            for p, raw in ((p, f"{p['event']}: {p['question']}") for p in pmarkets)]
    index = {}
    for i, (_, pn, _) in enumerate(pref):
        for w in set(pn.split()):
            index.setdefault(w, []).append(i)
    # Very common words ("win", "2026") make candidate sets huge; a real
    # match at jaccard >= 0.5 shares plenty of rarer words too.
    rare_max = max(5, len(pref) // 50)
    index = {w: ids for w, ids in index.items() if len(ids) <= rare_max}
    out, best_by_text = [], {}
    for m in kmarkets:
        if num(m.get("volume_fp")) < XVENUE_MIN_KALSHI_VOLUME:
            continue
        ya, _, na, _ = quote(m)
        if ya is None and na is None:
            continue
        kn = kalshi_match_text(m)
        if kn not in best_by_text:
            cand = {i for w in set(kn.split()) for i in index.get(w, ())}
            best, bs = None, 0.0
            for i in cand:
                s = jaccard(kn, pref[i][1])
                if s > bs:
                    best, bs = i, s
            best_by_text[kn] = (best, bs)
        bi, bs = best_by_text[kn]
        if bi is None or bs < XVENUE_MATCH_MIN or \
                not same_proposition(m, kn, pref[bi][1], pref[bi][2]):
            continue
        best = pref[bi][0]
        kexp = _parse_ts(expiry_of(m))
        if kexp is None or best["end_ts"] is None or \
                abs(kexp - best["end_ts"]) > XVENUE_MAX_EXPIRY_GAP_S:
            continue
        # Snapshot for the PolySentry live caller: the cross-venue leg
        # producer needs both venues' raw quotes, not the combo pricing.
        # Attached under a private key (stripped from stdout); the export
        # writer dedupes the two direction-combos into one record per pair.
        _xvsnap = {
            "kalshi_ticker": m.get("ticker"),
            "kalshi_title": m.get("title"),
            "kalshi_yes_sub_title": m.get("yes_sub_title"),
            "kalshi_yes_ask_cents": cc_to_cents(ya) if ya is not None else None,
            "kalshi_no_ask_cents": cc_to_cents(na) if na is not None else None,
            "kalshi_expiry_ts": kexp,
            "kalshi_volume": num(m.get("volume_fp")),
            "poly_slug": best.get("slug"),
            "poly_event": best.get("event"),
            "poly_question": best.get("question"),
            "poly_yes_ask_cents": cc_to_cents(best["yes_ask_cc"])
            if best.get("yes_ask_cc") is not None else None,
            "poly_no_ask_cents": cc_to_cents(best["no_ask_cc"])
            if best.get("no_ask_cc") is not None else None,
            "poly_yes_token": best.get("yes_token"),
            "poly_no_token": best.get("no_token"),
            "poly_fee_rate": best.get("fee_rate"),
            "poly_end_ts": best.get("end_ts"),
            "poly_volume": best.get("volume"),
        }
        for kside, kcc, pside, pcc, ptok in (
                ("yes", ya, "no", best["no_ask_cc"], best["no_token"]),
                ("no", na, "yes", best["yes_ask_cc"], best["yes_token"])):
            if kcc is None or pcc is None:
                continue
            legs = [{"ticker": m["ticker"], "side": kside, "price_cents": cc_to_cents(kcc)},
                    {"ticker": f"poly:{best['slug']}", "side": pside,
                     "price_cents": cc_to_cents(pcc), "venue": "polymarket",
                     "fee_rate": best["fee_rate"], "token": ptok}]
            edge = screen_edge_cents(legs)
            if edge >= XVENUE_MIN_EDGE_CENTS:
                tier = "locked"
            elif edge_floor is not None and edge >= edge_floor:
                tier = "edge"
            else:
                continue
            out.append({"combo_type": "C", "side": f"kalshi_{kside}+poly_{pside}",
                        "legs": legs, "edge": edge, "markets": [m],
                        "poly_market": f"{best['event']}: {best['question']}",
                        "match_score": round(bs, 3), "tier": tier,
                        "_xvenue": _xvsnap})
    return out


XVENUE_EXPORT_VERSION = 1


def xvenue_export_path():
    """Stable path of the cross-venue match feed consumed by PolySentry's
    paper loop (its ``xvenue_poly`` leg pool). Lives next to the dedupe
    state so host .env pinning applies; it is a data feed, not dedupe
    state, so it is written on every completed run."""
    return os.path.join(os.path.dirname(STATE_PATH), "xvenue_matches.json")


def build_xvenue_export(combos):
    """Type C combos -> one export record per matched (kalshi, poly) pair.

    Snapshots are screen-time quotes attached by ``screen_type_c`` under the
    private ``_xvenue`` key; the consumer re-fetches the Polymarket book
    itself and treats the Kalshi quotes as the (stale-bounded) fair anchor.
    """
    seen, out = set(), []
    for c in combos:
        if c.get("combo_type") != "C":
            continue
        snap = c.get("_xvenue")
        if not isinstance(snap, dict):
            continue
        key = (snap.get("kalshi_ticker"), snap.get("poly_slug"))
        if key in seen or not all(key):
            continue
        seen.add(key)
        out.append({"match_score": c.get("match_score"),
                    "edge_cents": c.get("edge"),
                    "tier": c.get("tier"),
                    **snap})
    return out


def write_xvenue_export(matches, as_of_ts, as_of_iso, errors, path=None):
    """Atomic write of the export feed. Failures are recorded, never raised:
    a broken feed must not fail the scan."""
    try:
        path = path or xvenue_export_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"export_version": XVENUE_EXPORT_VERSION,
                       "as_of": as_of_iso, "as_of_ts": as_of_ts,
                       "matches": matches}, f)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001 - feed must never break the scan
        errors.append(f"xvenue export failed: {type(e).__name__}: {e}")


# ------------------------------------------------------------ confirm ----

def confirm_event_book(cand, errors):
    """Type B gate: event is mutually exclusive, every live outcome is in the
    candidate, nothing has resolved YES, and brackets tile the number line."""
    try:
        d = kalshi_event(cand["event_ticker"])
    except Exception as e:
        errors.append(f"event {cand['event_ticker']}: {e}")
        return False
    ev = d.get("event") or {}
    if not ev.get("mutually_exclusive"):
        return False
    have = {l["ticker"] for l in cand["legs"]}
    live = []
    for m in d.get("markets") or ev.get("markets") or []:
        res = (m.get("result") or "").lower()
        if res == "yes":
            return False
        if res == "no":
            continue
        if m.get("ticker") not in have:
            return False  # an unresolved outcome we can't buy
        live.append(m)
    if {m.get("ticker") for m in live} != have:
        return False
    cand["series_ticker"] = ev.get("series_ticker")
    cand["event_title"] = ev.get("title")
    return brackets_exhaustive(live)


def confirm_ladder(cand, errors):
    """Type D gate: the event is NOT mutually exclusive (a cumulative ladder
    can't be), both legs are present and unresolved, each leg's strike_type,
    strike and expiry still match what the screen saw, and the two live legs
    share one non-empty expiry (same settlement value at the same time)."""
    try:
        d = kalshi_event(cand["event_ticker"])
    except Exception as e:
        errors.append(f"event {cand['event_ticker']}: {e}")
        return False
    ev = d.get("event") or {}
    if ev.get("mutually_exclusive") is True:
        return False
    live = {m.get("ticker"): m for m in d.get("markets") or ev.get("markets") or []}
    for leg, seen in zip(cand["legs"], cand["markets"]):
        m = live.get(leg["ticker"])
        if m is None or (m.get("result") or "").lower() in ("yes", "no"):
            return False
        st = seen.get("strike_type")
        field = {"less": "cap_strike", "greater": "floor_strike"}.get(st)
        if field is None or m.get("strike_type") != st:
            return False
        v = _strike_value(m.get(field))
        if v is None or v != _strike_value(seen.get(field)):
            return False
        if not expiry_of(m) or expiry_of(m) != expiry_of(seen):
            return False
    exp = [expiry_of(live.get(leg["ticker"]) or {}) for leg in cand["legs"]]
    if len(exp) < 2 or not exp[0] or any(e != exp[0] for e in exp):
        return False
    cand["series_ticker"] = ev.get("series_ticker")
    cand["event_title"] = ev.get("title")
    return True


def model_take_possible(m):
    """False when no fair value could pass the Tier 3 screen on this listing:
    a take needs both sides quoted, the taken side's mid >= 0.5 and its ask
    at least MODEL_EDGE_MIN_CENTS under 100c (fair can't exceed 100c)."""
    ya, _, na, _ = quote(m)
    if ya is None or na is None:
        return False
    mid, max_ask = listing_mid(ya, na), 10000 - MODEL_EDGE_MIN_CENTS * 100
    return (me.agrees_with_lean("YES", mid) and ya <= max_ask) or \
        (me.agrees_with_lean("NO", mid) and na <= max_ask)


def fetch_kraken_for_windows(markets, now_ts, kraken, errors):
    """Fill `kraken` for every pair with a live 15-min window that could pass
    the screen: Trades since the earliest such w_open - 300 plus 1-min OHLC,
    two calls per pair through the shared semaphore (http_get_json with a
    per-host Session: Kraken is its own host). Failures go to errors; that
    pair's windows are then skipped. Returns the pair count."""
    since = {}
    for m, pair, wo, _ in live_model_windows(markets, now_ts):
        if model_take_possible(m):
            since[pair] = min(since.get(pair, wo), wo)

    def http(url, params):
        return http_get_json(url, params, timeout=me.KRAKEN_TIMEOUT,
                             retries=me.KRAKEN_ATTEMPTS - 1)

    pairs = sorted(since)
    for err in _parallel_map(
            lambda p: kraken.fetch(p, since[p] - me.TRADES_LOOKBACK_S, http), pairs):
        if err:
            errors.append(err)
    return len(pairs)


def _refresh_legs(cand, books):
    """Replace listing prices with orderbook top-of-book; return depth_sets."""
    depth = math.inf
    for l in cand["legs"]:
        if l.get("venue") == "polymarket":
            ask_cc, size = books.get(("poly", l["token"]), (None, 0.0))
        else:
            ask_cc, size = books.get(l["ticker"], {}).get(l["side"], (None, 0.0))
        if ask_cc is None or size <= 0:
            return 0
        l["price_cents"] = cc_to_cents(ask_cc)
        depth = min(depth, size)
    return int(math.floor(depth)) if depth != math.inf else 0


def _finalize_model_edge(cand, books, mults, args, now_iso):
    """Tier 3: re-price the single leg at the orderbook ask, re-check the
    market's lean on the fresh book, re-shrink the raw fair toward the FRESH
    mid (the signal side must not flip), then size by expected value."""
    book = books.get(cand["ticker"]) or {}
    (ya, ys), (na, ns) = book.get("yes", (None, 0.0)), book.get("no", (None, 0.0))
    if ya is None or na is None or ys <= 0 or ns <= 0:
        return None
    fresh_mid = listing_mid(ya, na)
    if not me.agrees_with_lean(cand["side"], fresh_mid):
        return None
    fair_taken_cc = cand["fair_taken_cc"]
    fair_raw = (cand.get("model") or {}).get("fair_raw")
    if fair_raw is not None:
        fair_fresh = me.shrink_midrange(fair_raw, fresh_mid)
        side_fresh = "YES" if fair_fresh > fresh_mid else "NO"
        if side_fresh != cand["side"]:
            return None
        fair_taken_cc = int(round((fair_fresh if side_fresh == "YES" else 1 - fair_fresh)
                                  * 10000))
    cand["fair_taken_cc"] = fair_taken_cc
    depth = _refresh_legs(cand, books)
    leg = cand["legs"][0]
    leg["fee_mult"] = mults.get(series_of({"ticker": leg["ticker"]}), 1.0)
    sized = evaluate_model_edge(leg, depth, fair_taken_cc, args.max_outlay_cents,
                                args.min_outlay_cents)
    if not sized:
        return None
    return {
        "combo_type": "M",
        "tier": "model-edge",
        "side": cand["side"],
        "legs": [{k: leg[k] for k in ("ticker", "side", "price_cents")} | {"venue": "kalshi"}],
        **sized,
        "model": cand["model"],
        "guarantee": MODEL_EDGE_GUARANTEE,
        "title": cand["title"],
        "expiry": expiry_of(cand["markets"][0]),
        "detected_at": now_iso,
    }


def finalize(cand, books, mults, args, now_iso):
    """Depth-gated, fee-priced combo dict for output, or None."""
    if cand.get("tier") == "model-edge":
        return _finalize_model_edge(cand, books, mults, args, now_iso)
    depth = _refresh_legs(cand, books)
    for l in cand["legs"]:
        if l.get("venue", "kalshi") == "kalshi":
            l["fee_mult"] = mults.get(series_of({"ticker": l["ticker"]}), 1.0)
    tier = cand.get("tier", "locked")
    if tier == "locked":
        # Type D legs pay >= 100c per set (200c when settlement lands between
        # the strikes), so its locked_profit_cents is a conservative lower bound.
        sized = evaluate_combo(cand["legs"], depth, args.max_outlay_cents,
                               args.min_outlay_cents, args.min_roi)
    else:
        sized = evaluate_edge(cand["legs"], depth, args.max_outlay_cents,
                              args.min_outlay_cents, EDGE_EVAL_MIN_CENTS[cand["combo_type"]],
                              pre_fee=cand["combo_type"] == "A")
    if not sized:
        return None
    exps = [e for e in (expiry_of(m) for m in cand["markets"]) if e]
    out = {
        "combo_type": cand["combo_type"],
        "tier": tier,
        "side": cand["side"],
        "legs": [{k: l[k] for k in ("ticker", "side", "price_cents")} |
                 {"venue": l.get("venue", "kalshi")} for l in cand["legs"]],
        **sized,
        "expiry": max(exps, key=lambda s: _parse_ts(s) or 0) if exps else None,
        "detected_at": now_iso,
    }
    if cand.get("event_ticker"):
        out["event_ticker"] = cand["event_ticker"]
        out["title"] = cand.get("event_title")
        out["guarantee"] = "mutually exclusive event; outcome strikes tile the number line"
        if cand["combo_type"] == "D":
            out["guarantee"] = ("cumulative threshold ladder; the two legs always pay >= $1 "
                                "on the same settlement value")
    else:
        out["title"] = cand["markets"][0].get("title")
    if cand["combo_type"] == "C":
        out["poly_market"] = cand["poly_market"]
        out["match_score"] = cand["match_score"]
        out["guarantee"] = "title-matched across venues; resolution rules may differ"
    if tier != "locked":
        out["guarantee"] = A_EDGE_GUARANTEE if cand["combo_type"] == "A" else EDGE_GUARANTEE
    return out


# -------------------------------------------------------- tier 4 legs ----

def _picker_leg(entry, kleg, kalshi_leg_in_cand, **kw):
    """PickLeg from a confirmed entry's Kalshi leg, or None when any field is
    missing or out of range (fail closed)."""
    m = (entry["_cand"].get("markets") or [None])[0] or {}
    side = str(kleg.get("side") or "").upper()
    ask = num(kleg.get("price_cents"))
    expiry = _parse_ts(m.get("close_time"))
    event = m.get("event_ticker")
    depth = entry.get("depth_sets")
    if side not in ("YES", "NO") or not 0 < ask < 100 or not 0 < kw["fair_cents"] <= 100 \
            or not event or expiry is None or not isinstance(depth, int) or depth < 0 \
            or m.get("ticker") != kleg.get("ticker"):
        return None
    return cp.PickLeg(ticker=kleg["ticker"], side=side, ask_cents=float(ask),
                      event_ticker=event, game_key=cp.game_key(event), expiry_ts=int(expiry),
                      venue="kalshi", fee_mult=float(kalshi_leg_in_cand.get("fee_mult", 1.0)),
                      depth_contracts=depth, **kw)


def screen_picker_legs(confirmed_model_edge, confirmed_xvenue_edge, now_ts):
    """Tier 4 leg pool from CONFIRMED (orderbook-priced) entries that still
    carry "_cand": Tier 3 model-edge legs (fair = model fair of the taken
    side) and the Kalshi leg of confirmed Type C cross-venue pairs (tiers
    "edge" and "locked") (fair = 100 - Polymarket opposite-side ask, kept only
    when fair - ask >= cp.LEG_MIN_EDGE_CENTS).
    Anything that doesn't map cleanly, or expires within cp.PICK_MIN_TTL_S of
    now_ts, is skipped; cp.build_combo_picks re-applies its full pre-filter."""
    out = []
    for e in confirmed_model_edge:
        try:
            cand, kleg = e["_cand"], e["legs"][0]
            leg = _picker_leg(e, kleg, cand["legs"][0],
                              fair_cents=int(cand["fair_taken_cc"]) / 100,
                              fair_source="kraken_model", category="crypto15m",
                              direction=1 if str(e.get("side")).upper() == "YES" else -1,
                              model_version="kraken_brownian_v1")
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if leg is not None and leg.side == str(e.get("side")).upper():
            out.append(leg)
    for e in confirmed_xvenue_edge:
        try:
            cand = e["_cand"]
            kl = [l for l in e["legs"] if l.get("venue", "kalshi") == "kalshi"]
            pl = [l for l in e["legs"] if l.get("venue") == "polymarket"]
            ckl = [l for l in cand["legs"] if l.get("venue", "kalshi") == "kalshi"]
            if e.get("combo_type") != "C" or len(kl) != 1 or len(pl) != 1 or len(ckl) != 1:
                continue
            p_ask = num(pl[0].get("price_cents"))
            if not 0 < p_ask < 100:
                continue
            fair = 100 - p_ask
            if fair - num(kl[0].get("price_cents")) < cp.LEG_MIN_EDGE_CENTS:
                continue
            leg = _picker_leg(e, kl[0], ckl[0], fair_cents=float(fair),
                              fair_source="xvenue_poly", category=cp.category_of(kl[0]["ticker"]),
                              direction=0, model_version="xvenue_poly_v1")
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if leg is not None:
            out.append(leg)
    return [l for l in out if l.expiry_ts - now_ts >= cp.PICK_MIN_TTL_S]


def _picker_key(pick_json):
    return f"picker|{pick_json['combo_key']}"


# ----------------------------------------------------------------- main ----

def _model_edge_pass(kmarkets, kraken, kraken_box, kraken_thread, books, t0, errors, stats):
    """Tier 3 after the Tier 1/2 books: wait for Kraken until
    MODEL_KRAKEN_BUDGET_S, screen, then fetch up to MAX_MODEL_ORDERBOOK_FETCHES
    books until EDGE_DETAIL_BUDGET_S (added to `books`). Returns
    (candidates, tickers whose books were requested)."""
    kraken_thread.join(timeout=max(0.0, t0 + MODEL_KRAKEN_BUDGET_S - time.time()))
    if kraken_thread.is_alive():
        errors.append(f"model-edge: kraken fetch unfinished at {MODEL_KRAKEN_BUDGET_S}s; "
                      "pairs without data skipped")
    model_now = int(time.time())
    stats["M_live_windows"] = len(live_model_windows(kmarkets, model_now))
    stats["M_kraken_pairs"] = kraken_box.get("pairs", 0)
    cands = screen_model_edge(kmarkets, model_now, kraken, errors, stats)
    cands.sort(key=lambda c: -c["edge"])
    stats["M"] = len(cands)
    if len(cands) > MAX_CANDIDATES:
        errors.append(f"type M: {len(cands)} candidates, depth-checked top {MAX_CANDIDATES}")
    cands = cands[:MAX_CANDIDATES]
    mkt = sorted({c["ticker"] for c in cands} - set(books))
    if len(mkt) > MAX_MODEL_ORDERBOOK_FETCHES:
        errors.append(f"model-edge orderbook fetches capped at {MAX_MODEL_ORDERBOOK_FETCHES}")
        mkt = mkt[:MAX_MODEL_ORDERBOOK_FETCHES]
    deadline, skipped = t0 + EDGE_DETAIL_BUDGET_S, []

    def _book(t):
        if time.time() > deadline:
            skipped.append(t)
            return None
        try:
            return kalshi_orderbook_top(t)
        except Exception as e:
            errors.append(f"orderbook {t}: {e}")
            return None

    for t, res in zip(mkt, _parallel_map(_book, mkt)):
        if res is not None:
            books[t] = res
    if skipped:
        errors.append(f"model-edge: {len(skipped)} orderbook fetches skipped "
                      f"(past {EDGE_DETAIL_BUDGET_S}s budget)")
    return cands, mkt


def run_scan(args, errors):
    t0 = time.time()
    now = datetime.now(timezone.utc)
    now_iso, now_ts = now.isoformat(), int(now.timestamp())
    deadline = t0 + SWEEP_BUDGET_S

    poly_box = {}

    def _poly():
        try:
            poly_box["markets"] = poly_markets()
        except Exception as e:
            poly_box["error"] = f"polymarket fetch failed: {e}"

    poly_thread = threading.Thread(target=_poly, daemon=True)
    poly_thread.start()  # shares the HTTP semaphore (one reserved slot) with the Kalshi sweep
    kmarkets = kalshi_open_markets(now_ts, errors, deadline)
    poly_thread.join(timeout=max(1.0, deadline + 10 - time.time()))
    if "error" in poly_box:
        errors.append(poly_box["error"])
    elif "markets" not in poly_box:
        errors.append("polymarket fetch timed out; Type C skipped")

    # Tier 3 Kraken data downloads while Tier 1/2 screening runs on the CPU.
    model_on = getattr(args, "model_edge", True)
    kraken, kraken_box, kraken_thread = me.KrakenCache(), {}, None
    if model_on:
        def _kraken():
            try:
                kraken_box["pairs"] = fetch_kraken_for_windows(
                    kmarkets, int(time.time()), kraken, errors)
            except Exception as e:
                errors.append(f"model-edge: kraken fetch failed: {type(e).__name__}: {e}")
        kraken_thread = threading.Thread(target=_kraken, daemon=True)
        kraken_thread.start()

    edge_tier = getattr(args, "edge_tier", True)
    cands, edge_cands, stats = [], [], {}
    stats["polymarket_markets"] = len(poly_box.get("markets") or [])
    for kind, found in (
            ("A", screen_type_a(kmarkets, near_arb=edge_tier)),
            ("B", screen_type_b(kmarkets, stats)),
            ("C", screen_type_c(kmarkets, poly_box.get("markets") or [],
                                XVENUE_EDGE_MIN_CENTS if edge_tier else None)),
            ("D", screen_type_d(kmarkets, stats))):
        found.sort(key=lambda c: -c["edge"])
        locked = [c for c in found if c["tier"] == "locked"]
        stats[kind] = len(locked)
        if len(locked) > MAX_CANDIDATES:
            errors.append(f"type {kind}: {len(locked)} candidates, depth-checked top {MAX_CANDIDATES}")
        cands.extend(locked[:MAX_CANDIDATES])
        if kind not in ("B", "D"):
            edge = [c for c in found if c["tier"] == "edge"]
            stats[f"{kind}_edge"] = len(edge)
            edge_cands.extend(edge[:MAX_CANDIDATES])

    # Type B needs event confirmation (mutual exclusivity + exhaustive brackets);
    # Type D needs the ladder (not mutually exclusive, legs live, strikes unchanged).
    confirmers = {"B": confirm_event_book, "D": confirm_ladder}
    ev_cands = [c for c in cands if c["combo_type"] in confirmers]
    ok = dict(zip([id(c) for c in ev_cands],
                  _parallel_map(lambda c: confirmers[c["combo_type"]](c, errors), ev_cands)))
    cands = [c for c in cands if c["combo_type"] not in confirmers or ok.get(id(c))]
    for kind in confirmers:
        stats[f"{kind}_event_confirmed"] = sum(
            1 for c in ev_cands if c["combo_type"] == kind and ok.get(id(c)))

    kt = sorted({l["ticker"] for c in cands for l in c["legs"] if l.get("venue", "kalshi") == "kalshi"})
    pt = sorted({l["token"] for c in cands for l in c["legs"] if l.get("venue") == "polymarket"})
    if len(kt) + len(pt) > MAX_ORDERBOOK_FETCHES:
        errors.append(f"orderbook fetches capped at {MAX_ORDERBOOK_FETCHES}")
        kt = kt[:MAX_ORDERBOOK_FETCHES]
        pt = pt[:max(0, MAX_ORDERBOOK_FETCHES - len(kt))]

    # Tier 2 books ride on top of Tier 1's list so they can never displace it.
    ekt = sorted({l["ticker"] for c in edge_cands for l in c["legs"]
                  if l.get("venue", "kalshi") == "kalshi"} - set(kt))
    ept = sorted({l["token"] for c in edge_cands for l in c["legs"]
                  if l.get("venue") == "polymarket"} - set(pt))
    if len(ekt) + len(ept) > MAX_EDGE_ORDERBOOK_FETCHES:
        errors.append(f"tier 2 orderbook fetches capped at {MAX_EDGE_ORDERBOOK_FETCHES}")
        ekt = ekt[:MAX_EDGE_ORDERBOOK_FETCHES]
        ept = ept[:max(0, MAX_EDGE_ORDERBOOK_FETCHES - len(ekt))]
    edge_deadline, edge_skipped = t0 + EDGE_DETAIL_BUDGET_S, []

    def _book(job):
        venue, key, tier = job
        if tier == "edge" and time.time() > edge_deadline:
            edge_skipped.append(key)
            return None
        try:
            return kalshi_orderbook_top(key) if venue == "k" else poly_best_ask(key)
        except Exception as e:
            errors.append(f"orderbook {key}: {e}")
            return None

    jobs = ([("k", t, "locked") for t in kt] + [("p", t, "locked") for t in pt] +
            [("k", t, "edge") for t in ekt] + [("p", t, "edge") for t in ept])
    books = {}
    for (venue, key, _), res in zip(jobs, _parallel_map(_book, jobs)):
        if res is not None:
            books[key if venue == "k" else ("poly", key)] = res
    if edge_skipped:
        errors.append(f"tier 2: {len(edge_skipped)} orderbook fetches skipped "
                      f"(past {EDGE_DETAIL_BUDGET_S}s budget)")
    # Tier 3 runs only after every Tier 1/2 book is in, within its own budgets.
    model_cands, mkt = [], []
    if kraken_thread is not None:
        model_cands, mkt = _model_edge_pass(kmarkets, kraken, kraken_box, kraken_thread,
                                            books, t0, errors, stats)
    series = sorted({series_of({"ticker": t}) for t in
                     kt + [t for t in ekt if t in books] + [t for t in mkt if t in books]})
    run_stats = dict(getattr(kmarkets, "stats", None) or {})
    mults = fee_multipliers(series, now_ts, run_stats)
    # reverify_new reuses the fee_mult of legs that survived this filter, so
    # every multiplier it prices with is known.
    unknown = set(run_stats.get("fee_mult_unknown") or [])
    cands, n_c = _drop_unknown_fee_mult(cands, unknown)
    edge_cands, n_e = _drop_unknown_fee_mult(edge_cands, unknown)
    model_cands, n_m = _drop_unknown_fee_mult(model_cands, unknown)
    if n_c + n_e + n_m:
        run_stats["fee_mult_unknown_rejects"] = {"cands": n_c, "edge_cands": n_e,
                                                 "model_cands": n_m}

    def _final(cand):
        out = finalize(cand, books, mults, args, now_iso)
        if out:
            out["_cand"] = cand      # for the verify-before-alert re-check; never printed
        return out

    combos = [c for c in map(_final, cands) if c]
    edges = [c for c in map(_final, edge_cands) if c]
    models = [c for c in map(_final, model_cands) if c]
    models.sort(key=lambda c: -c["expected_roi_pct"])
    return (kmarkets, rank_combos(combos) + rank_combos(edges) + models,
            stats, now_iso, now_ts, t0, run_stats)

def _leg_book_key(leg):
    return leg["ticker"] if leg.get("venue", "kalshi") == "kalshi" else ("poly", leg["token"])


def reverify_new(new, args, errors, now_iso, deadline, stats=None):
    """Verify-before-alert: re-fetch every leg's book once for NEW Tier 1 and
    Type C edge combos and re-run the depth/edge evaluation at those prices.

    Returns (kept, dropped). Combos whose edge vanished or that no longer
    fill the minimum outlay are dropped (stats["reverify_dropped"]), and so
    are combos whose books weren't all fetched by `deadline`
    (stats["reverify_unverified"]): nothing is alerted on screen-time prices,
    and a combo that is still live is re-detected by the next sweep. Kept
    ones carry depth_reverified=True. Type A near-arb edge entries are
    informational and pass through untouched.
    """
    def wanted(c):
        return "_cand" in c and (c.get("tier", "locked") == "locked" or c["combo_type"] == "C")

    targets = [c for c in new if wanted(c)]
    jobs = sorted({_leg_book_key(l) for c in targets for l in c["_cand"]["legs"]}, key=str)

    def _book(key):
        if time.time() > deadline:
            return None
        try:
            return poly_best_ask(key[1]) if isinstance(key, tuple) else kalshi_orderbook_top(key)
        except Exception as e:
            errors.append(f"reverify orderbook {key}: {e}")
            return None

    books = {k: b for k, b in zip(jobs, _parallel_map(_book, jobs)) if b is not None}
    kept, dropped, unverified = [], [], 0
    for c in new:
        if not wanted(c):
            kept.append(c)
            continue
        cand = copy.deepcopy(c["_cand"])
        if not all(_leg_book_key(l) in books for l in cand["legs"]):
            unverified += 1
            dropped.append(c)
            continue
        mults = {series_of({"ticker": l["ticker"]}): l.get("fee_mult", 1.0)
                 for l in cand["legs"] if l.get("venue", "kalshi") == "kalshi"}
        out = finalize(cand, books, mults, args, c.get("detected_at") or now_iso)
        if out is None:
            dropped.append(c)
            continue
        out["depth_reverified"] = True
        out["_cand"] = cand
        kept.append(out)
    if unverified:
        errors.append(f"reverify: {unverified} new combo(s) dropped unverified "
                      f"(orderbook re-fetch failed or missed the {REVERIFY_BUDGET_S}s budget)")
    if stats is not None:
        stats.update(reverify_checked=len(targets), reverify_dropped=len(dropped) - unverified,
                     reverify_unverified=unverified)
    return kept, dropped


def coverage_line(st, complete):
    """One compact human-readable sweep line for the cron log (stderr)."""
    if complete is None or "markets_unique" not in st:
        return f"combo_scan sweep: coverage unknown (no sweep stats; complete={complete})"
    head = "complete" if complete else (
        f"INCOMPLETE: {len(st.get('ranges_budget_hit') or [])} range(s) unscanned, "
        f"~{st.get('markets_unscanned_est') if st.get('markets_unscanned_est') is not None else '?'}"
        " markets missing")
    return (f"combo_scan sweep {head} | unique {st.get('markets_unique', 0):,} "
            f"(rows {st.get('markets_scanned', 0):,}) | pages {st.get('pages_fetched', 0)} | "
            f"ranges ok {st.get('ranges_completed', 0)} retried {st.get('ranges_retried', 0)} | "
            f"{st.get('sweep_seconds', 0)}s | plan {st.get('sweep_plan', '?')}")


def _combo_pick_pass(combos, new, alerted, now_iso, t0, mark, errors, run_stats):
    """Tier 4, best-effort after Tiers 1-3: (all pick dicts, new pick dicts,
    stderr lines). Legs come from the finalized entries in `combos`, swapped
    for their re-verified copies in `new` where those exist. Skipped past
    RUN_HARD_BUDGET_S; a picker failure becomes an error entry and never
    touches Tier 1-3 output or state. New picks are logged only when marking."""
    if time.time() - t0 > RUN_HARD_BUDGET_S:
        run_stats["picker_skipped_budget"] = run_stats.get("picker_skipped_budget", 0) + 1
        return [], [], []
    try:
        def key(c):
            return combo_key(c["combo_type"], [l["ticker"] for l in c["legs"]], c["side"],
                             c.get("tier", "locked"))
        fresh = {key(c): c for c in new if "_cand" in c}
        confirmed = [fresh.get(key(c), c) for c in combos if "_cand" in c]
        now_ts = int(time.time())
        pool = screen_picker_legs(
            [c for c in confirmed if c.get("tier") == "model-edge"],
            [c for c in confirmed if c.get("combo_type") == "C"
             and c.get("tier") in ("edge", "locked")],
            now_ts)
        picks_json = [cp.pick_to_json(p) for p in cp.build_combo_picks(pool, now_ts)]
        new_json, _ = split_new(picks_json, alerted, now_iso, mark=False, key_fn=_picker_key)
        if mark:
            for d in new_json:
                cp.log_pick(d)
        lines = [f"combo_picker: {len(pool)} leg(s), {len(picks_json)} pick(s), "
                 f"{len(new_json)} new"] + [cp.pick_summary_line(d) for d in new_json]
        return picks_json, new_json, lines
    except Exception as e:
        errors.append(f"combo picker failed: {type(e).__name__}: {e}")
        return [], [], []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-outlay", type=float, default=DEFAULT_MAX_OUTLAY_DOLLARS,
                    help="max dollars per combo (default 20)")
    ap.add_argument("--min-outlay", type=float, default=DEFAULT_MIN_OUTLAY_DOLLARS,
                    help="min dollars the book must fill at quoted prices (default 10)")
    ap.add_argument("--min-roi", type=float, default=0.0,
                    help="min after-fee ROI percent (default 0: any positive profit)")
    ap.add_argument("--no-mark", action="store_true",
                    help="diagnostic read-only: never write dedupe state")
    ap.add_argument("--edge-tier", action=argparse.BooleanOptionalAction, default=True,
                    help="also flag Tier 2 edge (not locked) combos (default on)")
    ap.add_argument("--model-edge", action=argparse.BooleanOptionalAction, default=True,
                    help="also flag Tier 3 model-edge bets on 15-min crypto windows (default on)")
    ap.add_argument("--combo-picks", action=argparse.BooleanOptionalAction, default=True,
                    help="also build Tier 4 combo picks from confirmed edge legs (default on)")
    args = ap.parse_args(argv)
    args.max_outlay_cents = int(round(args.max_outlay * 100))
    args.min_outlay_cents = int(round(args.min_outlay * 100))
    mark = not args.no_mark

    errors, t0 = [], time.time()
    kmarkets, combos, stats, now_iso, run_stats = [], [], {}, iso_now(), {}
    new, repeats, new_model = [], 0, []
    picks_json, new_picks_json, pick_lines = [], [], []
    try:
        kmarkets, combos, stats, now_iso, now_ts, t0, run_stats = run_scan(args, errors)
        state = load_state()
        alerted = state.get("alerted", {})
        new, repeats = split_new([c for c in combos if c.get("tier") != "model-edge"],
                                 alerted, now_iso, mark=False)
        new_model, _ = split_new([c for c in combos if c.get("tier") == "model-edge"],
                                 alerted, now_iso, mark=False)
        rv_deadline = min(time.time() + REVERIFY_BUDGET_S, t0 + RUN_HARD_BUDGET_S)
        new, dropped = reverify_new(new, args, errors, now_iso, rv_deadline, run_stats)
        if dropped:
            gone = {id(c) for c in dropped}
            combos = [c for c in combos if id(c) not in gone]
        if args.combo_picks:
            picks_json, new_picks_json, pick_lines = _combo_pick_pass(
                combos, new, alerted, now_iso, t0, mark, errors, run_stats)
        if mark:
            split_new(new + new_model, alerted, now_iso, mark)  # stamp only what is output
            split_new(new_picks_json, alerted, now_iso, mark, key_fn=_picker_key)
            prune_alerted(alerted, now_ts)
            save_state({"alerted": alerted})
        # Cross-venue feed for PolySentry's live xvenue_poly leg pool: every
        # completed run exports the surviving Type C matches (post-reverify).
        # A data feed, not dedupe state: written even under --no-mark.
        try:
            write_xvenue_export(build_xvenue_export(combos), now_ts, now_iso, errors)
        except Exception as e:  # noqa: BLE001 - feed must never break the scan
            errors.append(f"xvenue export failed: {type(e).__name__}: {e}")
    except Exception as e:
        errors.append(f"scan failed: {type(e).__name__}: {e}")
    if "coverage_complete" in run_stats:
        complete = run_stats["coverage_complete"] and not any(
            e.startswith("scan failed") for e in errors)
    else:
        complete = False if any(e.startswith("scan failed") for e in errors) else None
    run_stats["coverage_complete"] = complete
    new = [{k: v for k, v in c.items() if k not in ("_cand", "_xvenue")} for c in new]
    new_model = [{k: v for k, v in c.items() if k not in ("_cand", "_xvenue")} for c in new_model]
    n_edge = sum(1 for c in combos if c.get("tier") == "edge")
    n_model = sum(1 for c in combos if c.get("tier") == "model-edge")
    out = {
        "as_of": now_iso,
        "markets_scanned": len(kmarkets),
        "combos_found": len(combos) - n_edge - n_model,
        "edge_combos_found": n_edge,
        "model_edge_combos_found": n_model,
        "new_combos": new,
        "new_edge_combos": [c for c in new if c.get("tier") == "edge"],
        "new_model_edge_combos": new_model,
        "repeat_combos_count": repeats,
        "sweep_seconds": round(time.time() - t0, 1),
        "fees_model": FEES_MODEL,
        "max_outlay_cents": args.max_outlay_cents,
        "min_outlay_cents": args.min_outlay_cents,
        "min_roi_pct": args.min_roi,
        "screen_candidates": stats,
        "coverage_complete": complete,
        "stats": run_stats,
        "errors": errors,
    }
    if args.combo_picks:
        out["combo_picks_found"] = picks_json
        out["new_combo_picks"] = new_picks_json
    print(json.dumps(out, indent=1))
    print(coverage_line(run_stats, complete), file=sys.stderr)
    for line in pick_lines:
        print(line, file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (0, None):
            print(json.dumps({"as_of": iso_now(), "markets_scanned": 0, "combos_found": 0,
                              "edge_combos_found": 0, "model_edge_combos_found": 0,
                              "new_combos": [], "new_edge_combos": [],
                              "new_model_edge_combos": [], "repeat_combos_count": 0, "sweep_seconds": 0,
                              "fees_model": FEES_MODEL, "coverage_complete": False,
                              "errors": [f"bad arguments: exit {e.code}"]}))
    sys.exit(0)
