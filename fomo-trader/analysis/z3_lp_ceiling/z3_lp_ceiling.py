#!/usr/bin/env python3
"""Z3: DLMM LP fee-ceiling test for the paper bot's $7 ticket.

A synthetic UPPER BOUND, not a simulation. Deterministic, hermetic (no
network, no journal reads, no file writes), stdlib only. Every input is a
stated assumption (see z3_lp_ceiling_report.md) and every knob that could
favor the LP is set at its favorable end:

  - perfect bin placement (the position is always in the active bin),
  - zero inventory loss / zero impermanent loss,
  - fee yield at the TOP of the cited 0.1-0.7% range, applied per pool-day
    and compounded for free,
  - no failed transactions, no priority fees, only base signature fees,
  - every bin array already initialized (no non-refundable array rent).

Mechanical requirements are NOT relaxed: the ticket is the episode's total
capital budget (Z1 section 3B), so the refundable rent deposits needed to
hold a position come out of it before any liquidity is deployed.

    python analysis/z3_lp_ceiling/z3_lp_ceiling.py          # table + verdict
    python analysis/z3_lp_ceiling/z3_lp_ceiling.py --json   # full result
"""
import json
import math
import sys

LAMPORTS_PER_SOL = 1_000_000_000
# Solana rent exemption: (data bytes + 128 overhead) * 3480 lamports per
# byte-year * 2 years.
RENT_LAMPORTS_PER_BYTE_YEAR = 3480
RENT_EXEMPT_YEARS = 2
ACCOUNT_OVERHEAD_BYTES = 128
BINS_PER_ARRAY = 70
MAX_TICKET_USD = 1e9

DEFAULTS = {
    "sol_usd": 115.0,
    "pool_days": 30,
    "fee_yield_per_pool_day": 0.007,
    "protocol_cut": 0.20,
    "position_account_bytes": 8120,
    "token_account_bytes": 165,
    "token_accounts": 2,
    "bin_array_bytes": 10136,
    "min_bins": 1,
    "uninitialized_bin_arrays": 0,
    "signatures_per_episode": 3,
    "base_fee_lamports_per_signature": 5000,
    "priority_fee_lamports_per_episode": 0,
    "episodes": 1,
}

# (low, high, integer) -- anything outside falls back to the default.
BOUNDS = {
    "sol_usd": (0.01, 1e6, False),
    "pool_days": (1, 366, True),
    "fee_yield_per_pool_day": (0.0, 1.0, False),
    "protocol_cut": (0.0, 0.99, False),
    "position_account_bytes": (0, 10_000_000, True),
    "token_account_bytes": (0, 10_000_000, True),
    "token_accounts": (0, 10, True),
    "bin_array_bytes": (0, 10_000_000, True),
    "min_bins": (1, 1400, True),
    "uninitialized_bin_arrays": (0, 100, True),
    "signatures_per_episode": (0, 100, True),
    "base_fee_lamports_per_signature": (0, 1_000_000_000, True),
    "priority_fee_lamports_per_episode": (0, 10_000_000_000, True),
    "episodes": (1, 366, True),
}

TICKETS = (7.0, 25.0, 100.0)
YIELDS = (0.001, 0.004, 0.007)
CUTS = (0.0, 0.20)
HEADLINE_TICKET = 7.0

SENSITIVITIES = (
    ("base: favorable assumptions, fixed 70-bin position rent", {}),
    ("wSOL account treated as not needed (1 token account)",
     {"token_accounts": 1}),
    ("SOL $100 (below every journal SOL price)", {"sol_usd": 100.0}),
    ("SOL $100 and zero protocol cut",
     {"sol_usd": 100.0, "protocol_cut": 0.0}),
    ("SOL $100 plus one standard 0.01 SOL priority fee on each of 2 txs",
     {"sol_usd": 100.0, "priority_fee_lamports_per_episode": 20_000_000}),
    ("one uninitialized bin array (non-refundable rent)",
     {"uninitialized_bin_arrays": 1}),
    ("rotation: 30 one-day episodes", {"episodes": 30}),
    ("launch-pool stress: 5%/pool-day (beyond the cited range)",
     {"fee_yield_per_pool_day": 0.05}),
    ("HYPOTHETICAL 1-bin position account (392 bytes, unverified)",
     {"position_account_bytes": 392}),
)

# Measured, n=1 (report topic 2): cryptognome/dlmmbot, 10 SOL -> 9.98 SOL
# over 4 days, 112 positions. Context only; not an input to the ceiling.
REALISM_ANCHOR = {
    "source": "cryptognome/dlmmbot (report topic 2, LOW confidence, n=1)",
    "start_sol": 10.0, "end_sol": 9.98, "days": 4, "positions": 112,
    "realized_daily_return": round((9.98 / 10.0) ** (1 / 4) - 1, 6),
}


def _num(value):
    """Finite float, or None. Booleans are not numbers here."""
    if isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None


def resolve_params(params=None):
    """Merge overrides onto DEFAULTS. Invalid values keep the default and
    are listed in the returned errors; nothing here raises."""
    p = dict(DEFAULTS)
    errors = []
    if params is None:
        return p, errors
    if not isinstance(params, dict):
        errors.append("params is %s, not a dict; defaults used"
                      % type(params).__name__)
        return p, errors
    for key, raw in params.items():
        if key not in DEFAULTS:
            errors.append("unknown param %r ignored" % (key,))
            continue
        low, high, integer = BOUNDS[key]
        x = _num(raw)
        if (x is None or x < low or x > high
                or (integer and x != int(x))):
            errors.append("param %s=%r invalid; default %r used"
                          % (key, raw, DEFAULTS[key]))
            continue
        p[key] = int(x) if integer else x
    return p, errors


def rent_exempt_lamports(data_bytes):
    """Refundable rent-exempt minimum for an account of `data_bytes`."""
    return ((int(data_bytes) + ACCOUNT_OVERHEAD_BYTES)
            * RENT_LAMPORTS_PER_BYTE_YEAR * RENT_EXEMPT_YEARS)


def fixed_costs_lower(p):
    """Lower bound on what holding one position costs, in lamports and USD.

    deposit: refundable, but must be funded from the ticket while the
      position is open (position account + token accounts + the wallet's
      own rent-exempt minimum).
    nonrefundable: base signature fees for open/close (plus any priority
      fee) per episode, plus rent for bin arrays the position must
      initialize. Minimum viable bins sets how many arrays are spanned.
    protocol_cut: share of gross fees the protocol keeps (applied to fees).
    """
    deposit = (rent_exempt_lamports(p["position_account_bytes"])
               + p["token_accounts"]
               * rent_exempt_lamports(p["token_account_bytes"])
               + rent_exempt_lamports(0))
    spanned = math.ceil(p["min_bins"] / BINS_PER_ARRAY)
    new_arrays = min(p["uninitialized_bin_arrays"], spanned)
    tx = p["episodes"] * (p["signatures_per_episode"]
                          * p["base_fee_lamports_per_signature"]
                          + p["priority_fee_lamports_per_episode"])
    nonrefundable = tx + new_arrays * rent_exempt_lamports(p["bin_array_bytes"])
    usd = p["sol_usd"] / LAMPORTS_PER_SOL
    return {
        "deposit_lamports": deposit,
        "nonrefundable_lamports": nonrefundable,
        "deposit_usd": deposit * usd,
        "nonrefundable_usd": nonrefundable * usd,
        "reserve_usd": (deposit + nonrefundable) * usd,
        "protocol_cut": p["protocol_cut"],
        "min_bins": p["min_bins"],
        "bin_arrays_spanned": spanned,
        "new_bin_arrays": new_arrays,
    }


def fee_growth(p):
    """Compounded fee growth factor over the pool-days, net of protocol cut."""
    gross = (1.0 + p["fee_yield_per_pool_day"]) ** p["pool_days"] - 1.0
    return gross * (1.0 - p["protocol_cut"])


def fee_income_upper(deployable_usd, p):
    """Most fee income `deployable_usd` of liquidity could earn: perfect
    placement, zero IL, top-of-range yield compounded free, minus the
    protocol cut. Non-positive or malformed capital earns nothing."""
    d = _num(deployable_usd)
    if d is None or d <= 0:
        return 0.0
    return d * fee_growth(p)


def _r(x, nd=6):
    x = _num(x)
    return None if x is None else round(x, nd)


def ceiling(ticket_usd, params=None):
    """Upper-bound net over the pool-days for one ticket. Never raises:
    malformed tickets come back valid=False with net 0 and an error."""
    p, errors = resolve_params(params)
    row = {"ticket_usd": None, "valid": True, "feasible": False,
           "deployable_usd": 0.0, "fee_income_upper_usd": 0.0,
           "net_upper_usd": 0.0, "param_errors": errors}
    t = _num(ticket_usd)
    if t is None or t <= 0 or t > MAX_TICKET_USD:
        row.update(valid=False, error="ticket %r is not a finite amount in "
                   "(0, %g] USD" % (ticket_usd, MAX_TICKET_USD))
        return row
    c = fixed_costs_lower(p)
    deployable = t - c["reserve_usd"]
    feasible = deployable > 0
    fees = fee_income_upper(deployable, p) if feasible else 0.0
    # Infeasible: the position cannot be opened, so nothing is spent.
    net = fees - c["nonrefundable_usd"] if feasible else 0.0
    row.update(
        ticket_usd=_r(t), feasible=feasible,
        deployable_usd=_r(deployable),
        fee_income_upper_usd=_r(fees), net_upper_usd=_r(net),
        deposit_usd=_r(c["deposit_usd"]),
        nonrefundable_usd=_r(c["nonrefundable_usd"]),
        reserve_usd=_r(c["reserve_usd"]),
        deposit_share_of_ticket=_r(c["deposit_usd"] / t),
        fee_yield_per_pool_day=p["fee_yield_per_pool_day"],
        protocol_cut=p["protocol_cut"], sol_usd=p["sol_usd"],
        pool_days=p["pool_days"], bin_arrays_spanned=c["bin_arrays_spanned"],
        new_bin_arrays=c["new_bin_arrays"])
    return row


def breakevens(params=None, ticket_usd=HEADLINE_TICKET):
    """SOL price at or above which `ticket_usd` cannot fund the position,
    and the smallest ticket whose ceiling clears $0 at the configured SOL."""
    p, _ = resolve_params(params)
    c = fixed_costs_lower(p)
    reserve_sol = ((c["deposit_lamports"] + c["nonrefundable_lamports"])
                   / LAMPORTS_PER_SOL)
    growth = fee_growth(p)
    return {
        "sol_usd_ceiling_infeasible_at_or_above": (
            _r(ticket_usd / reserve_sol, 4) if reserve_sol > 0 else None),
        "min_ticket_usd_to_clear_zero": (
            _r(c["reserve_usd"] + c["nonrefundable_usd"] / growth, 4)
            if growth > 0 else None),
    }


def run(params=None):
    """Full deterministic result: headline, grid, sensitivities, verdict."""
    base = params if isinstance(params, dict) else {}
    resolved, errors = resolve_params(params)
    headline = ceiling(HEADLINE_TICKET, params)
    grid = [ceiling(t, dict(base, fee_yield_per_pool_day=y, protocol_cut=cut))
            for t in TICKETS for y in YIELDS for cut in CUTS]
    sens = [dict(ceiling(HEADLINE_TICKET, dict(base, **over)), label=label)
            for label, over in SENSITIVITIES]
    killed = not (headline["net_upper_usd"] or 0.0) > 0
    be = breakevens(params)
    min_ticket = be["min_ticket_usd_to_clear_zero"]
    if killed:
        why = ("infeasible: required refundable deposits $%.2f exceed the "
               "$%.0f budget" % (headline.get("deposit_usd") or 0.0,
                                 HEADLINE_TICKET)
               if not headline["feasible"] else "fees do not cover costs")
        line = ("VERDICT: KILLED - $%.0f ticket, %d pool-days: upper-bound net "
                "$%.2f (%s); the ceiling clears $0 only at tickets >= %s"
                % (HEADLINE_TICKET, resolved["pool_days"],
                   headline["net_upper_usd"] or 0.0, why,
                   "$%.2f" % min_ticket if min_ticket is not None else "n/a"))
    else:
        line = ("VERDICT: CEILING_CLEARS_ZERO - $%.0f ticket, %d pool-days: "
                "upper-bound net $%.2f; this permits the realistic IS/OOS "
                "test, it is not a profitability result"
                % (HEADLINE_TICKET, resolved["pool_days"],
                   headline["net_upper_usd"]))
    return {
        "schema": "z3_lp_ceiling/v1",
        "assumptions": resolved,
        "param_errors": errors,
        "headline": headline,
        "grid": grid,
        "sensitivities_7usd": sens,
        "breakeven": be,
        "realism_anchor": REALISM_ANCHOR,
        "verdict": "KILLED" if killed else "CEILING_CLEARS_ZERO",
        "verdict_line": line,
    }


def _fmt(x):
    return "    n/a" if x is None else "%7.2f" % x


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    res = run()
    if "--json" in argv:
        print(json.dumps(res, indent=2))
        return 0
    h = res["headline"]
    print("Z3 DLMM fee ceiling (synthetic upper bound, SOL $%.2f, %d pool-days)"
          % (res["assumptions"]["sol_usd"], res["assumptions"]["pool_days"]))
    print("$7 required deposits $%.4f (%.1f%% of ticket), non-refundable "
          "$%.4f" % (h["deposit_usd"], 100 * h["deposit_share_of_ticket"],
                     h["nonrefundable_usd"]))
    print("\nticket  yield/day  cut  feasible  deployable  fees_upper  net_upper")
    for g in res["grid"]:
        print("%6.0f  %8.3f%%  %3.0f%%  %-8s  %s     %s     %s"
              % (g["ticket_usd"], 100 * g["fee_yield_per_pool_day"],
                 100 * g["protocol_cut"], g["feasible"],
                 _fmt(g["deployable_usd"]), _fmt(g["fee_income_upper_usd"]),
                 _fmt(g["net_upper_usd"])))
    print("\n$7 sensitivities:")
    for s in res["sensitivities_7usd"]:
        print("  %-66s net %s  feasible=%s"
              % (s["label"], _fmt(s["net_upper_usd"]), s["feasible"]))
    print("\nbreakeven:", json.dumps(res["breakeven"]))
    print(res["verdict_line"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
