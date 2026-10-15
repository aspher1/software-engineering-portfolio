"""Tier 4 combo picker (paper/analysis only): Kalshi-app-style parlay picks.

Turns confirmed directional edge legs (Tier 3 Kraken model, Tier 2 Type C
cross-venue gaps) into "Pick 2+ markets -> $10 pays $X" combos, priced with a
correlation-aware joint fair (product of marginals, hard same-event/dup
rejects, a JOINT_HAIRCUT_PTS haircut per flagged pair), built by beam search
and sized by quarter-Kelly on a paper bankroll. Design: docs/combo-picker.md.

Every pick is a MODEL estimate, never a guaranteed profit. Fees reuse the
scanner's helpers verbatim (rounded UP, never loosened). The module is pure:
no HTTP, no threads, no API clients; only log_pick touches the disk. No
orders, no credentials, no market or RFQ creation, ever.

Money is in cents (float where needed), probabilities are unit floats.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from fractions import Fraction

# Fee helpers are used from combo_scan verbatim (taker_fee_cents, leg_fee_cc ->
# poly_fee_cc), looked up at call time: combo_scan imports this module at load.
import combo_scan as _cs
import scanner_env as _env

LEG_MIN_EDGE_CENTS = 5.0       # per-leg bar: fair_cents - ask_cents >= 5
PICK_MIN_TTL_S = 300           # leg must expire >= 5 min out
JOINT_HAIRCUT_PTS = 4.0        # pct-points subtracted per flagged pair
MAX_FLAGGED_PAIRS = 2          # more -> reject combo
COMBO_MIN_JOINT_FAIR = 0.02    # below -> reject (lottery tickets)
MAKER_MARGIN_GUARD_CENTS = 1.0   # added to EACH leg ask before the product
COMBO_MIN_EDGE_CENTS = 5.0     # per $1 of reference stake
PICK_REF_STAKE_DOLLARS = 10
MAX_COMBO_LEGS = 4
MAX_LEG_POOL = 60
BEAM_WIDTH = 200
PAPER_BANKROLL_DOLLARS = 1000
KELLY_FRACTION = 0.25
PAPER_MAX_STAKE_DOLLARS = 10
PAPER_MIN_STAKE_DOLLARS = 2
PAPER_DAY_LOSS_CAP_DOLLARS = 50   # phase 2 (paper ledger); not enforced here

PICK_GUARANTEE = "MODEL estimate — NOT guaranteed; fair-value model vs market"
FAIR_SOURCES = ("kraken_model", "xvenue_poly")

REPO_ROOT = _env.REPO_ROOT
PICK_LOG_PATH = _env.env_path("COMBO_SCANNER_PICK_LOG",
                              os.path.join(REPO_ROOT, "data", "picker_picks.jsonl"))

SPORTS_PREFIXES = ("KXNFL", "KXNBA", "KXNHL", "KXMLB", "KXEPL", "KXUCL", "KXUEL",
                   "KXLALIGA", "KXSERIEA", "KXMLS", "KXBUNDESLIGA")
CRYPTO_PREFIXES = ("KXBTC", "KXETH", "KXXRP", "KXSOL", "KXDOGE", "KXBNB", "KXHYPE", "KXZEC")

# Path substrings of outward-facing writes (market/RFQ/quote creation). The
# picker makes no HTTP calls; this guard exists so any future call fails closed.
_WRITE_BLACKLIST = (
    "multivariate_event_collections",   # POST = create combo market (5000/week cap)
    "/communications/rfqs",
    "/communications/quotes",
    "/v1/maker/",
)
_READ_OK = ("multivariate_event_collections",)   # documented public GET reads
_READ_METHODS = ("GET", "HEAD")


class WriteBlocked(RuntimeError):
    pass


def guard_no_write(url, method="GET"):
    """Return url when it is a plain read; raise WriteBlocked on any write
    method, and on any method against the RFQ/quote/maker families."""
    m = (method or "").upper()
    hit = next((s for s in _WRITE_BLACKLIST if s in (url or "")), None)
    if m not in _READ_METHODS:
        raise WriteBlocked(f"refusing {m} {url}: combo picker is read-only"
                           + (f" (blacklisted: {hit})" if hit else ""))
    if hit and hit not in _READ_OK:
        raise WriteBlocked(f"refusing {m} {url}: blacklisted trading surface ({hit})")
    return url


# ------------------------------------------------------------- legs ----

@dataclass
class PickLeg:
    ticker: str            # Kalshi market ticker
    side: str              # "YES" | "NO" — side we would take
    ask_cents: float       # taker ask for taken side, post orderbook re-verify, in (0,100)
    fair_cents: float      # model fair value of taken side, in (0,100]
    fair_source: str       # "kraken_model" | "xvenue_poly"  ("none" never enters the pool)
    event_ticker: str      # Kalshi event ticker (same-event rejection key)
    game_key: str | None   # "LEAGUE|YYYYMMDD|TEAMA|TEAMB" (teams sorted) or None
    category: str          # "crypto15m" | "sports" | "politics" | "econ" | "other"
    expiry_ts: int         # market close, unix seconds
    venue: str = "kalshi"
    fee_mult: float = 1.0
    fee_rate: float = 0.0
    depth_contracts: int = 0
    direction: int = 0     # crypto15m: YES=+1 / NO=-1; else 0
    model_version: str = ""


def leg_edge_cents(leg):
    return leg.fair_cents - leg.ask_cents


def leading_alpha(event_ticker):
    m = re.match(r"[A-Z]+", (event_ticker or "").upper())
    return m.group(0) if m else ""


def league_of(event_ticker):
    """Known league code the ticker's leading alpha run starts with (longest
    match: KXNFLGAME and KXNFLSPREAD are both KXNFL, so two markets on one
    game share a league), else the leading alpha run itself."""
    run = leading_alpha(event_ticker)
    known = [p for p in SPORTS_PREFIXES if run.startswith(p)]
    return max(known, key=len) if known else run


def category_of(ticker):
    t = (ticker or "").upper()
    if t.startswith(SPORTS_PREFIXES):
        return "sports"
    if t.startswith(CRYPTO_PREFIXES):
        return "crypto15m"
    return "other"


_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_GAME_RE = re.compile(r"^([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})([A-Z]{3})([A-Z]{3})$")


def game_key(event_ticker):
    """"LEAGUE|YYYYMMDD|TEAMA|TEAMB" (teams sorted) for sports event tickers
    like KXNFLGAME-26OCT04INDWAS, else None. Only the exact shape
    <alpha>-<YY><MON><DD><3 letters><3 letters> parses; anything else (times
    in the ticker, 2- or 4-letter codes, bad dates) is None."""
    m = _GAME_RE.match((event_ticker or "").upper())
    if not m:
        return None
    _, yy, mon, dd, a, b = m.groups()
    if mon not in _MONTHS or a == b:
        return None
    try:
        d = datetime(2000 + int(yy), _MONTHS[mon], int(dd))
    except ValueError:
        return None
    ta, tb = sorted((a, b))
    return f"{league_of(event_ticker)}|{d:%Y%m%d}|{ta}|{tb}"


# --------------------------------------------------------- pricing ----

class PickRejected(Exception):
    pass


def game_tail(key):
    """"YYYYMMDD|TEAMA|TEAMB" of a game_key, or None. League-free: the league
    code is the ticker's alpha run, which differs between the market-type
    series of one game (KXCFLGAME vs KXCFLSPREAD) outside SPORTS_PREFIXES."""
    if key is None:
        return None
    return key.split("|", 1)[1] if "|" in key else key


def _flag(a, b):
    """Correlation flag for one leg pair, or None."""
    if a.game_key is not None and game_tail(a.game_key) == game_tail(b.game_key):
        return "same_game"
    if (a.category == "sports" and b.category == "sports"
            and league_of(a.event_ticker) == league_of(b.event_ticker)
            and (a.game_key is None or b.game_key is None)):
        return "same_game_unknown"
    if (a.category == "crypto15m" and b.category == "crypto15m"
            and a.direction and b.direction and a.direction == -b.direction):
        return "crypto_opp"
    return None


def joint_fair(legs):
    """(joint fair win prob, flagged pairs [(i, j, reason)]). Raises
    PickRejected on same-event / dup-ticker legs, too many flagged pairs, or
    a haircut joint below COMBO_MIN_JOINT_FAIR."""
    events, tickers = set(), set()
    for leg in legs:
        if leg.ticker in tickers:
            raise PickRejected("dup_ticker")
        if leg.event_ticker in events:
            raise PickRejected("same_event")
        tickers.add(leg.ticker)
        events.add(leg.event_ticker)
    flagged = []
    for i in range(len(legs)):
        for j in range(i + 1, len(legs)):
            reason = _flag(legs[i], legs[j])
            if reason:
                flagged.append((i, j, reason))
    if len(flagged) > MAX_FLAGGED_PAIRS:
        raise PickRejected("too_correlated")
    joint_raw = math.prod(leg.fair_cents / 100 for leg in legs)
    joint = max(0.0, joint_raw - 0.01 * JOINT_HAIRCUT_PTS * len(flagged))
    if joint < COMBO_MIN_JOINT_FAIR:
        raise PickRejected("joint_too_small")
    return joint, flagged


@dataclass
class ComboPick:
    legs: list[PickLeg]
    joint_fair: float
    flagged_pairs: list[tuple[int, int, str]]
    p_combo_cents: float
    contracts_10: int
    stake_cents_10: float
    fee_cents_10: float
    edge_cents_10: float
    expected_roi_pct: float
    payout_ask_10: float      # contracts_10 as dollars: "$10 pays $X" at guarded asks
    payout_fair_10: float     # 10.0 / joint_fair: MODEL ESTIMATE of fair payout
    guarantee: str            # PICK_GUARANTEE
    paper_stake_dollars: float  # quarter-Kelly sizing, clamped [2, 10]
    priced_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _leg_dict(leg):
    return {"venue": leg.venue, "price_cents": leg.ask_cents,
            "fee_mult": leg.fee_mult, "fee_rate": leg.fee_rate}


def paper_stake(joint, p_combo_cents):
    """Quarter-Kelly paper stake in dollars for decimal odds o = 100/p_combo,
    f* = (p*o - 1)/(o - 1), clamped to [PAPER_MIN, PAPER_MAX]."""
    o = 100 / p_combo_cents
    if o <= 1:
        return float(PAPER_MIN_STAKE_DOLLARS)
    kelly_f = KELLY_FRACTION * (joint * o - 1) / (o - 1)
    if kelly_f <= 0:
        return float(PAPER_MIN_STAKE_DOLLARS)
    return float(min(PAPER_MAX_STAKE_DOLLARS,
                     max(PAPER_MIN_STAKE_DOLLARS, kelly_f * PAPER_BANKROLL_DOLLARS)))


def price_combo(legs):
    """True-parlay price of one combo at guarded asks and the $10 reference
    stake. Raises PickRejected on hard rejects (see joint_fair) or when $10
    buys no whole contract."""
    joint, flagged = joint_fair(legs)
    # Exact: a float product like 100*.05*.80 = 4.000000000000001 floors $10
    # to 249 contracts instead of 250.
    p_exact = 100 * math.prod(Fraction(str(leg.ask_cents + MAKER_MARGIN_GUARD_CENTS)) / 100
                              for leg in legs)
    if not 0 < p_exact < 100:
        raise PickRejected("bad_price")
    p_combo_cents = float(p_exact)
    contracts_10 = math.floor(PICK_REF_STAKE_DOLLARS * 100 / p_exact)
    if contracts_10 < 1:
        raise PickRejected("no_contract")
    stake_cents_10 = float(contracts_10 * p_exact)
    fee_one_order = _cs.taker_fee_cents(p_combo_cents, contracts_10,
                                        max(leg.fee_mult for leg in legs))
    fee_per_leg_sum = sum(_cs.leg_fee_cc(_leg_dict(leg), contracts_10) for leg in legs) / 100
    fee_cents_10 = max(fee_one_order, fee_per_leg_sum)
    edge_cents_10 = joint * contracts_10 * 100 - stake_cents_10 - fee_cents_10
    return ComboPick(
        legs=list(legs), joint_fair=joint, flagged_pairs=flagged,
        p_combo_cents=p_combo_cents, contracts_10=contracts_10,
        stake_cents_10=stake_cents_10, fee_cents_10=float(fee_cents_10),
        edge_cents_10=edge_cents_10,
        expected_roi_pct=100 * edge_cents_10 / stake_cents_10,
        payout_ask_10=float(contracts_10), payout_fair_10=PICK_REF_STAKE_DOLLARS / joint,
        guarantee=PICK_GUARANTEE,
        paper_stake_dollars=paper_stake(joint, p_combo_cents))


def price_passes(price):
    return (2 <= len(price.legs) <= MAX_COMBO_LEGS
            and price.joint_fair >= COMBO_MIN_JOINT_FAIR
            and price.edge_cents_10 >= COMBO_MIN_EDGE_CENTS * PICK_REF_STAKE_DOLLARS
            and price.contracts_10 >= 1
            and all(leg.depth_contracts >= price.contracts_10 for leg in price.legs))


def leg_passes(leg, now_ts):
    """Per-leg pre-filter: a known fair source, fair - ask >= LEG_MIN_EDGE_CENTS,
    some depth, and at least PICK_MIN_TTL_S to expiry."""
    return (leg.fair_source in FAIR_SOURCES
            and 0 < leg.ask_cents < 100 and 0 < leg.fair_cents <= 100
            and leg_edge_cents(leg) >= LEG_MIN_EDGE_CENTS
            and leg.depth_contracts >= 1
            and leg.expiry_ts - now_ts >= PICK_MIN_TTL_S)


def combo_key(pick):
    return "P|" + ",".join(sorted(f"{leg.ticker}:{leg.side}" for leg in pick.legs))


def build_combo_picks(legs, now_ts=None):
    """Beam search over 2..MAX_COMBO_LEGS-leg subsets of the pre-filtered pool
    (top MAX_LEG_POOL legs by edge). Only passing combos are extended; each
    size keeps its top BEAM_WIDTH by edge_cents_10. Deterministic: ties break
    on pool indices. Returns deduped passing picks, edge desc then joint desc."""
    now_ts = int(time.time()) if now_ts is None else now_ts
    pool = [leg for leg in legs if leg_passes(leg, now_ts)]
    pool.sort(key=lambda leg: (-leg_edge_cents(leg), leg.ticker, leg.side))
    pool = pool[:MAX_LEG_POOL]
    beam = [(i,) for i in range(len(pool))]
    picks = []
    for _ in range(2, MAX_COMBO_LEGS + 1):
        cand = []
        for s in beam:
            for j in range(s[-1] + 1, len(pool)):
                t = s + (j,)
                try:
                    price = price_combo([pool[i] for i in t])
                except PickRejected:
                    continue
                if price_passes(price):
                    cand.append((price.edge_cents_10, t, price))
        cand.sort(key=lambda c: (-c[0], c[1]))
        beam = [t for _, t, _ in cand[:BEAM_WIDTH]]
        picks += [p for _, _, p in cand]
        if not beam:
            break
    seen, out = set(), []
    for p in picks:
        k = combo_key(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    out.sort(key=lambda p: (-p.edge_cents_10, -p.joint_fair, combo_key(p)))
    return out


# ---------------------------------------------------------- output ----

def _round(x):
    if isinstance(x, float):
        return round(x, 4)
    if isinstance(x, dict):
        return {k: _round(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_round(v) for v in x]
    return x


_COMMIT = None


def git_commit():
    """HEAD sha of the repo read from .git (no subprocess), or "unknown"."""
    global _COMMIT
    if _COMMIT is None:
        _COMMIT = "unknown"
        try:
            git = os.path.join(REPO_ROOT, ".git")
            with open(os.path.join(git, "HEAD")) as f:
                head = f.read().strip()
            if head.startswith("ref: "):
                ref = head[5:]
                try:
                    with open(os.path.join(git, ref)) as f:
                        head = f.read().strip()
                except FileNotFoundError:
                    with open(os.path.join(git, "packed-refs")) as f:
                        head = next(line.split()[0] for line in f
                                    if line.strip().endswith(" " + ref))
            if re.fullmatch(r"[0-9a-f]{40}", head):
                _COMMIT = head
        except Exception:
            pass
    return _COMMIT


def pick_to_json(pick):
    exp = max(leg.expiry_ts for leg in pick.legs)
    d = asdict(pick)
    d["flagged_pairs"] = [list(f) for f in pick.flagged_pairs]
    d.update(combo_key=combo_key(pick),
             model_versions=sorted({leg.model_version for leg in pick.legs}),
             expiry=datetime.fromtimestamp(exp, timezone.utc).isoformat(),
             commit=git_commit())
    return _round(d)


def log_pick(pick, path=None):
    """Append one JSON line for `pick` (append-only backtest log)."""
    path = path or PICK_LOG_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    row = pick if isinstance(pick, dict) else pick_to_json(pick)
    with open(path, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def pick_summary_line(d):
    """App-language stderr line for one pick_to_json dict."""
    legs = " + ".join(f"{l['ticker']} {l['side']} @ {l['ask_cents']:g}c" for l in d["legs"])
    return (f"PICK: {legs} -> ${PICK_REF_STAKE_DOLLARS} pays ${d['payout_ask_10']:g} "
            f"(fair ${d['payout_fair_10']:.2f}, edge {d['edge_cents_10']:+.1f}c; "
            f"MODEL estimate, not guaranteed)")
