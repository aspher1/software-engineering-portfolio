#!/usr/bin/env python3
"""Observation-mode kill-criteria dashboard for the paper bot.

Reads the trade journal (runs/paper-1h/trades.jsonl, read-only) and scores
the consolidated kill criteria from
~/workspace/research_notes/copy-trading-track-records-20260927-2020/report.md,
section "Concrete validation experiment + kill criteria (paper bot,
consolidated)". The report lists exactly 8 criteria, the same count as the
Z1 brief. They are encoded in the report's order, and any one kills the
program:

  1. Net P&L <= 0 after all costs over >= 100 trades.
  2. Median round-trip cost > 15% of the $7 ticket.
  3. Failed-tx rate > 10% or median inclusion delay > 2 slots.
  4. Win rate < 30% AND profit factor < 0.5 sustained over 100 trades.
  5. Any "profitable" variant depends on < 5 outlier trades.
  6. Rug-gap exits account for > 50% of gross losses with no filter
     reducing them.
  7. BSC leg: median round-trip tax+fee+slippage > 15%.
  8. 60-day review: no variant passes the IS->OOS sign test; the program
     shuts down rather than re-tuning.

Each criterion reports status "ok" or "TRIPPED", plus `evaluable` and a
`note`. On a criterion that can't be evaluated, "ok" means "not tripped",
never "passed". What the paper journal can and cannot measure:

  1. Net is the journaled paper P&L (`realized_usd`, or native x `sol_usd`;
     Solana closes without a rate use the $115 fallback, counted). Bot fees,
     priority fees and failed attempts are not journaled, so paper net is an
     upper bound on real net: a trip is reliable, an "ok" is optimistic.
  2, 7. Only entry-side execution is journaled (`slip_from_signal_pct`,
     commit vs signal price, a fraction). That is a LOWER bound on
     round-trip cost (no exit slippage, taxes, bot or priority fees). Needs
     >= 20 observations (a dashboard choice; the report sets no minimum).
  3. Paper mode sends no transactions, so failed-tx rate and inclusion
     delay are unmeasurable; never evaluable. `entry_latency_ms`
     (signal -> commit) is shown for information only and is not compared
     with the 2-slot threshold.
  4. Evaluated on the last 100 closes in journal order.
  5. Variants = the whole program plus each `research_question` tag. For a
     profitable variant, count how many top winners must be removed to erase
     its net; fewer than 5 trips.
  6. Rug gap = exit/peak < 0.4 (analysis/x1_ruggap). The dashboard sees the
     loss share only; the "no filter reducing them" clause needs offline
     counterfactuals (X1). Needs >= 10 losing closes (dashboard choice).
  8. Evaluable once the journal spans >= 60 days. IS = first 60% of each
     variant's closes, OOS = last 40% (report topic 6). A variant passes if
     IS net > 0 and OOS net > 0.

Records are ordered by file position, not `ts`: the journal mixes local and
UTC naive stamps. Exact duplicate lines are dropped (counted); corrupt lines
are skipped (counted). update() never raises. On failure it returns
{"error": ...} and writes nothing.

    python obs_dashboard.py [--journal PATH] [--out PATH]
"""
import argparse
import json
import math
import os
import statistics
import sys
import tempfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_JOURNAL = os.path.join(ROOT, "runs", "paper-1h", "trades.jsonl")
DEFAULT_OUT = os.path.join(ROOT, "analysis", "obs", "kill_dashboard.json")
REPORT = ("~/workspace/research_notes/copy-trading-track-records-20260927-2020/"
          "report.md")

MIN_TRADES = 100
COST_MAX_FRACTION = 0.15
MIN_COST_OBS = 20
FAILED_TX_MAX = 0.10
MAX_DELAY_SLOTS = 2
SLOT_MS = 350
WR_MAX = 0.30
PF_MAX = 0.5
OUTLIER_TRADES = 5
RUG_GAP_EXIT_PEAK_RATIO = 0.4
RUG_GAP_LOSS_SHARE = 0.5
MIN_LOSSES_FOR_SHARE = 10
REVIEW_DAYS = 60
IS_FRACTION = 0.6
SOL_FALLBACK_USD = 115.0
TS_FMT = "%Y-%m-%d %H:%M:%S"
ALL = "ALL"


def _f(x):
    """Finite float or None (bools rejected)."""
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def _r(x, nd=4):
    v = _f(x)
    return None if v is None else round(v, nd)


def load_journal(path):
    """(records, stats). Missing file -> no records, stats["missing"]=True."""
    stats = {"lines": 0, "corrupt_lines": 0, "non_object_lines": 0,
             "duplicates_dropped": 0, "missing": False}
    records, seen = [], set()
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except (OSError, TypeError, ValueError):
        stats["missing"] = True
        return records, stats
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            stats["lines"] += 1
            if line in seen:
                stats["duplicates_dropped"] += 1
                continue
            seen.add(line)
            try:
                rec = json.loads(line)
            except (ValueError, RecursionError):
                stats["corrupt_lines"] += 1
                continue
            if not isinstance(rec, dict):
                stats["non_object_lines"] += 1
                continue
            records.append(rec)
    return records, stats


def chain_of(rec):
    chain = rec.get("chain")
    if isinstance(chain, str) and chain:
        return chain
    return "bsc" if str(rec.get("mint", "")).startswith("0x") else "solana"


def close_net_usd(rec):
    """(net_usd, used_fallback). net_usd is None when unpriceable."""
    usd = _f(rec.get("realized_usd"))
    if usd is not None:
        return usd, False
    chain = chain_of(rec)
    native = _f(rec.get("realized_bnb" if chain == "bsc" else "realized_sol"))
    if native is None:
        return None, False
    rate = _f(rec.get("sol_usd"))  # holds the BNB rate on BSC closes
    if rate is not None and rate > 0:
        return native * rate, False
    if chain == "bsc":
        return None, False
    return native * SOL_FALLBACK_USD, True


def is_rug_gap(rec):
    peak, exit_ = _f(rec.get("peak")), _f(rec.get("exit"))
    if peak is None or exit_ is None or peak <= 0:
        return False
    return exit_ / peak < RUG_GAP_EXIT_PEAK_RATIO


def _variant(rec):
    q = rec.get("research_question")
    return q if isinstance(q, str) and q else None


def _parse_ts(value):
    try:
        return datetime.strptime(value, TS_FMT)
    except (TypeError, ValueError):
        return None


def _profit_factor(nets):
    gw = sum(x for x in nets if x > 0)
    gl = -sum(x for x in nets if x < 0)
    return gw / gl if gl > 0 else None


def _median(xs):
    return statistics.median(xs) if xs else None


def _crit(cid, key, rule, tripped, evaluable, value, threshold, note):
    return {"id": cid, "key": key, "rule": rule,
            "status": "TRIPPED" if tripped else "ok",
            "evaluable": bool(evaluable), "value": value,
            "threshold": threshold, "note": note}


def _cost_criterion(cid, key, rule, entries, note):
    costs = [c for c in (_f(e.get("slip_from_signal_pct")) for e in entries)
             if c is not None]
    med = _median(costs)
    evaluable = len(costs) >= MIN_COST_OBS
    tripped = evaluable and med > COST_MAX_FRACTION
    return _crit(cid, key, rule, tripped, evaluable,
                 {"n_observations": len(costs), "median_cost_fraction": _r(med)},
                 {"median_cost_fraction_gt": COST_MAX_FRACTION,
                  "min_observations": MIN_COST_OBS}, note)


def compute(records, stats=None, generated_at=None, journal=None):
    """Score all 8 criteria over already-loaded journal records."""
    records = [r for r in (records or []) if isinstance(r, dict)]
    closes, unpriced, fallback = [], 0, 0
    for rec in records:
        if rec.get("type") != "close":
            continue
        net, used_fallback = close_net_usd(rec)
        if net is None:
            unpriced += 1
            continue
        fallback += used_fallback
        closes.append((rec, net))
    entries = [r for r in records if r.get("type") == "entry"]
    nets = [n for _, n in closes]
    variants = {ALL: nets}
    for rec, net in closes:
        v = _variant(rec)
        if v is not None and v != ALL:
            variants.setdefault(v, []).append(net)

    crits = []

    n, net = len(nets), sum(nets)
    ev = n >= MIN_TRADES
    crits.append(_crit(
        1, "net_pnl", "Net P&L <= 0 after all costs over >= 100 trades",
        ev and net <= 0, ev, {"n_closes": n, "net_usd": _r(net, 2)},
        {"net_usd_le": 0, "min_trades": MIN_TRADES},
        "Paper net excludes bot fees, priority fees and failed attempts "
        "(not journaled), so it overstates real net."))

    crits.append(_cost_criterion(
        2, "round_trip_cost", "Median round-trip cost > 15% of the $7 ticket",
        entries, "Proxy: entry-side slip vs signal only (a lower bound on "
        "round-trip cost); exit slippage and fees are not journaled."))

    lat = [x for x in (_f(e.get("entry_latency_ms")) for e in entries)
           if x is not None and x >= 0]
    crits.append(_crit(
        3, "failed_tx_or_delay",
        "Failed-tx rate > 10% or median inclusion delay > 2 slots",
        False, False,
        {"failed_tx_rate": None, "median_inclusion_delay_ms": None,
         "median_entry_latency_ms_info": _r(_median(lat), 1)},
        {"failed_tx_rate_gt": FAILED_TX_MAX,
         "median_inclusion_delay_ms_gt": MAX_DELAY_SLOTS * SLOT_MS},
        "Not measurable in paper mode (no transactions are sent). "
        "entry_latency_ms is signal->commit, not inclusion delay; shown for "
        "information only."))

    last = nets[-MIN_TRADES:]
    wr = (sum(1 for x in last if x > 0) / len(last)) if last else None
    pf = _profit_factor(last)
    ev = n >= MIN_TRADES
    crits.append(_crit(
        4, "wr_pf", "Win rate < 30% AND profit factor < 0.5 sustained over "
        "100 trades", ev and wr < WR_MAX and pf is not None and pf < PF_MAX,
        ev, {"window": len(last), "win_rate": _r(wr), "profit_factor": _r(pf)},
        {"win_rate_lt": WR_MAX, "profit_factor_lt": PF_MAX,
         "window_trades": MIN_TRADES},
        "Last 100 closes in journal order; profit factor is null when there "
        "are no losses."))

    outliers, any_profitable, any_tripped = {}, False, False
    for name, vnets in variants.items():
        vnet = sum(vnets)
        k = None
        if vnet > 0:
            any_profitable = True
            remaining = vnet
            for i, w in enumerate(sorted((x for x in vnets if x > 0),
                                         reverse=True), 1):
                remaining -= w
                if remaining <= 0:
                    k = i
                    break
            any_tripped = any_tripped or (k is not None and k < OUTLIER_TRADES)
        outliers[name] = {"n": len(vnets), "net_usd": _r(vnet, 2),
                          "winners_to_erase_net": k}
    crits.append(_crit(
        5, "outlier_dependence", 'Any "profitable" variant depends on < 5 '
        "outlier trades", any_tripped, any_profitable, outliers,
        {"winners_to_erase_net_lt": OUTLIER_TRADES},
        "Variants = ALL plus each research_question tag. Not evaluable while "
        "no variant is profitable (criterion 1 governs)."))

    losses = [(rec, x) for rec, x in closes if x < 0]
    gross_loss = -sum(x for _, x in losses)
    gap_loss = -sum(x for rec, x in losses if is_rug_gap(rec))
    share = gap_loss / gross_loss if gross_loss > 0 else None
    ev = len(losses) >= MIN_LOSSES_FOR_SHARE and share is not None
    crits.append(_crit(
        6, "rug_gap_loss_share", "Rug-gap exits account for > 50% of gross "
        "losses with no filter reducing them",
        ev and share > RUG_GAP_LOSS_SHARE, ev,
        {"n_losses": len(losses),
         "n_rug_gap_losses": sum(1 for rec, _ in losses if is_rug_gap(rec)),
         "gross_loss_usd": _r(gross_loss, 2),
         "rug_gap_loss_usd": _r(gap_loss, 2), "share": _r(share)},
        {"share_gt": RUG_GAP_LOSS_SHARE,
         "rug_gap_exit_peak_ratio_lt": RUG_GAP_EXIT_PEAK_RATIO,
         "min_losses": MIN_LOSSES_FOR_SHARE},
        "Loss share only. The 'no filter reducing them' clause needs offline "
        "counterfactuals (analysis/x1_ruggap)."))

    crits.append(_cost_criterion(
        7, "bsc_round_trip_cost", "BSC leg: median round-trip "
        "tax+fee+slippage > 15%", [e for e in entries if chain_of(e) == "bsc"],
        "Proxy: BSC entry-side slip vs signal only; transfer taxes, exit "
        "slippage and fees are not journaled (lower bound)."))

    stamps = [t for t in (_parse_ts(r.get("ts")) for r in records)
              if t is not None]
    span = ((max(stamps) - min(stamps)).total_seconds() / 86400
            if stamps else 0.0)
    ev = span >= REVIEW_DAYS
    sign = {}
    for name, vnets in variants.items():
        cut = int(len(vnets) * IS_FRACTION)
        is_net, oos_net = sum(vnets[:cut]), sum(vnets[cut:])
        sign[name] = {"is_n": cut, "oos_n": len(vnets) - cut,
                      "is_net_usd": _r(is_net, 2), "oos_net_usd": _r(oos_net, 2),
                      "passes": (cut > 0 and len(vnets) > cut
                                 and is_net > 0 and oos_net > 0)}
    crits.append(_crit(
        8, "is_oos_60d", "60-day review: no variant passes IS->OOS sign test",
        ev and not any(v["passes"] for v in sign.values()), ev,
        {"span_days": _r(span, 2), "variants": sign},
        {"min_span_days": REVIEW_DAYS, "is_fraction": IS_FRACTION},
        "IS = first 60% of each variant's closes in journal order. Shut down "
        "rather than re-tune when tripped."))

    tripped = [c["id"] for c in crits if c["status"] == "TRIPPED"]
    return {
        "schema": "obs_kill_dashboard/v1",
        "generated_at": generated_at or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "source_report": REPORT,
        "journal": journal,
        "journal_stats": dict(stats or {}, records=len(records),
                              closes_priced=len(closes),
                              closes_unpriced=unpriced,
                              closes_priced_with_sol_fallback=fallback,
                              entries=len(entries)),
        "criteria": crits,
        "summary": {
            "tripped": tripped,
            "not_evaluable": [c["id"] for c in crits if not c["evaluable"]],
            "program_killed": bool(tripped),
        },
    }


def _atomic_write(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".kill_dashboard.", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, allow_nan=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update(journal_path=None, out_path=None, generated_at=None):
    """Recompute from the journal and atomically write the dashboard.
    Returns the dashboard, or {"error": ...} (nothing written). Never raises."""
    jp = journal_path or DEFAULT_JOURNAL
    out = out_path or DEFAULT_OUT
    try:
        records, stats = load_journal(jp)
        dash = compute(records, stats, generated_at=generated_at, journal=jp)
        _atomic_write(out, dash)
        return dash
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e), "journal": jp}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    dash = update(args.journal, args.out)
    if "error" in dash:
        print("dashboard failed: %s" % dash["error"], file=sys.stderr)
        return 1
    for c in dash["criteria"]:
        print("%d %-22s %-7s evaluable=%-5s %s" % (
            c["id"], c["key"], c["status"], c["evaluable"],
            json.dumps(c["value"])[:100]))
    print("program_killed=%s tripped=%s not_evaluable=%s" % (
        dash["summary"]["program_killed"], dash["summary"]["tripped"],
        dash["summary"]["not_evaluable"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
