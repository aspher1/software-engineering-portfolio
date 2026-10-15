#!/usr/bin/env python3
"""Offline allocator analyst (warm loop; cron, never inside the bot).

Reads the trade journal READ-ONLY, joins entry records (features and any
allocator fields) with their close records (realized_usd), attributes P&L
by feature buckets, proposes allocator weights, and runs a validation gate.
Writes analysis/allocator_report_<ts>.md and analysis/allocator_proposal.json.
It NEVER writes the bot config: applying weights is a manual step.

RECENCY DISCIPLINE
------------------
Weights are fit on the FULL journal, never on a recent window. Each close
gets an exponential recency weight 0.5 ** (age_days / 14): a 14-day
half-life, so a two-week-old trade still counts half as much as today's.
The report explicitly checks whether recent performance (the most recent
~30% of closes) is better than the older journal. If it is, the report says
so and states that the recent streak is NOT treated as a persistent edge
("the system has been doing way better now" is a claim to test, not an
input). The gate's out-of-sample window is the most recent 30%, so a
recency-only edge cannot pass without also having existed in-sample.

METHOD (auditable)
------------------
Features are normalized exactly as allocator.normalize() does ([-1, 1],
missing = 0). A recency-weighted, L2-penalized logistic regression
(P(realized_usd > 0), deterministic full-batch gradient descent from zero)
gives one coefficient per feature: the change in log-odds of a winning
trade per unit of normalized feature. The intercept is then re-centered so
the recency-weighted average trade scores 0.5 (1.0x): the model ranks
trades relative to the journal average instead of shifting every ticket.
A trade is skipped (at take_threshold 0.35) only when its modeled win odds
are below ~54% of the average trade's.

VALIDATION GATE (all must pass to recommend shadow -> live)
-----------------------------------------------------------
- Time split: OOS = most recent max(ceil(30% of closes), 30) priced closes.
  If that exceeds 40% of the journal (i.e. fewer than 30 OOS closes can be
  held out at ~30%), the verdict is insufficient_data, never pass.
- The weights are refit on IS only (recency reference = last IS close) and
  replayed on IS and OOS with realistic costs. Edge = allocator net minus
  the 1.0x baseline net, per trade in the window.
- IS edge > 0, OOS edge > 0, OOS retains >= 60% of IS edge per trade
  (equivalently OOS decay <= 40%; the >70% decay reject is implied).
- Overfit rejects: IS win rate of allocator-taken trades > 90%; > 80% of
  the positive edge in a single chain or a single time tercile (evaluated
  only where >= 2 groups have >= 10 trades).
Verdicts: pass | fail | insufficient_data. No profitability claim is ever
made; all numbers are comparative (allocator vs 1.0x baseline).

REPLAY COSTS AND SIZING ASSUMPTIONS
-----------------------------------
Journal realized_usd comes from quote-based paper fills. The replay charges
an extra haircut on every traded ticket: entry 100 bps + exit 300 bps of
notional (scales with ticket size) plus a fixed per-trade fee (SOL $0.60,
BSC $0.20; does not scale). Linear ticket scaling: a trade at multiplier m
realizes m * realized_usd (price impact beyond the bps haircut is ignored).
The money.py risk ceiling and balance-aware sizing are future replay
extensions (historical balances are not journaled per entry), so replayed
upsizing is an upper bound. Skipped
trades realize 0 and pay no costs.

MANUAL APPLY (only on verdict == "pass")
----------------------------------------
1. Copy "weights" from analysis/allocator_proposal.json into the
   "allocator": {"weights": ...} section of runs/paper-1h/config.json.
2. Set "allocator": {"mode": "live"}.
3. Restart the bot so it reloads config. dry_run stays true.

CRON: see analysis/allocator_cron.md. The script no-ops (exit 0, one log
line) unless >= 25 new priced closes exist since the close-count watermark.
"""
import argparse
import json
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import allocator  # noqa: E402  (pure module, no bot import)

ANALYSIS_DIR = os.path.join(ROOT, "analysis")
DEFAULT_JOURNAL = os.path.join(ROOT, "runs", "paper-1h", "trades.jsonl")
DEFAULT_CONFIG = os.path.join(ROOT, "runs", "paper-1h", "config.json")

HALF_LIFE_DAYS = 14.0
OOS_FRAC = 0.30
MIN_OOS = 30
MAX_OOS_FRAC = 0.40
MIN_NEW_CLOSES = 25
RETENTION_MIN = 0.60
MAX_OOS_DECAY = 0.70
MAX_IS_WIN_RATE = 0.90
MAX_CONCENTRATION = 0.80
MIN_GROUP_TRADES = 10
L2 = 0.1
FIT_ITERS = 1000
FIT_LR = 0.5
WEIGHT_CAP = 3.0
DEFAULT_COSTS = {"entry_bps": 100.0, "exit_bps": 300.0,
                 "fee_usd": {"solana": 0.60, "bsc": 0.20}}
DEFAULT_NOTIONAL_USD = 10.0


def log(msg):
    print("[allocator_analyst %s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg),
          flush=True)


# -- journal ---------------------------------------------------------------

def parse_ts(s):
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        return None


def load_journal(path):
    rows, bad = [], 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(r, dict):
                rows.append(r)
            else:
                bad += 1
    return rows, bad


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def close_usd(close):
    u = _f(close.get("realized_usd"))
    if u is not None:
        return u
    chain = close.get("chain") or "solana"  # legacy closes are SOL
    nat = _f(close.get("realized_bnb" if chain == "bsc" else "realized_sol"))
    # BSC journal rows store the contemporaneous BNB rate as sol_usd.
    px = (_f(close.get("bnb_usd")) or _f(close.get("sol_usd"))
          if chain == "bsc" else _f(close.get("sol_usd")))
    if nat is not None and px is not None and px > 0:
        return nat * px
    return None


def join_trades(rows):
    """FIFO-join closes to the earliest open entry for the same mint.

    Returns (trades sorted by close time, stats). Unpriced closes and
    closes without a parseable timestamp are dropped and counted."""
    open_entries = {}
    trades = []
    stats = {"entries": 0, "closes": 0, "unpriced": 0, "no_ts": 0,
             "no_entry": 0, "with_allocator_fields": 0}
    for idx, r in enumerate(rows):
        t = r.get("type")
        if t == "entry":
            stats["entries"] += 1
            open_entries.setdefault(r.get("mint"), []).append(r)
        elif t == "close":
            stats["closes"] += 1
            q = open_entries.get(r.get("mint")) or []
            entry = q.pop(0) if q else None
            if entry is None:
                stats["no_entry"] += 1
            usd = close_usd(r)
            ts = parse_ts(r.get("ts"))
            if usd is None:
                stats["unpriced"] += 1
                continue
            if ts is None:
                stats["no_ts"] += 1
                continue
            chain = (r.get("chain") or (entry or {}).get("chain")
                     or "solana")
            rate = _f(r.get("sol_usd")) or _f((entry or {}).get("sol_usd"))
            buy = _f(r.get("buy_sol"))
            notional = buy * rate if buy and rate else None
            if entry and "allocator_score" in entry:
                stats["with_allocator_fields"] += 1
            feats = allocator.features_from_record(entry or {"chain": chain})
            feats["chain"] = chain
            trades.append({"idx": idx, "ts": ts, "ts_str": r.get("ts"),
                           "chain": chain, "realized_usd": usd,
                           "notional_usd": notional, "features": feats,
                           "entry": entry, "close": r})
    trades.sort(key=lambda x: (x["ts"], x["idx"]))
    return trades, stats


# -- recency weighting -----------------------------------------------------

def recency_weights(ts_list, half_life_days=HALF_LIFE_DAYS, ref_ts=None):
    """0.5 ** (age_days / half_life); age from ref_ts (default: newest)."""
    if not ts_list:
        return []
    ref = max(ts_list) if ref_ts is None else ref_ts
    out = []
    for ts in ts_list:
        age_days = max(0.0, (ref - ts) / 86400.0)
        out.append(0.5 ** (age_days / half_life_days))
    return out


# -- weight fitting --------------------------------------------------------

def _logistic(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def fit_logistic(X, y, w, l2=L2, iters=FIT_ITERS, lr=FIT_LR):
    """Weighted L2 logistic regression, deterministic GD from zero.
    Intercept unpenalized. Returns (b0, [beta])."""
    d = len(X[0]) if X else 0
    tot = sum(w) or 1.0
    s = [wi / tot for wi in w]
    b0, beta = 0.0, [0.0] * d
    for _ in range(iters):
        g0, g = 0.0, [0.0] * d
        for xi, yi, si in zip(X, y, s):
            z = b0
            for j in range(d):
                z += beta[j] * xi[j]
            e = (_logistic(z) - yi) * si
            g0 += e
            for j in range(d):
                g[j] += e * xi[j]
        b0 -= lr * g0
        for j in range(d):
            beta[j] -= lr * (g[j] + l2 * beta[j])
    return b0, beta


def propose_weights(trades, ref_ts=None, half_life_days=HALF_LIFE_DAYS):
    """Recency-weighted logistic fit, intercept centered on the weighted
    mean trade (score 0.5 = 1.0x). Returns (weights, fit_info)."""
    feats = allocator.FEATURES
    X = [[allocator.normalize(t["features"])[f] for f in feats] for t in trades]
    y = [1.0 if t["realized_usd"] > 0 else 0.0 for t in trades]
    w = recency_weights([t["ts"] for t in trades], half_life_days, ref_ts)
    b0, beta = fit_logistic(X, y, w)
    beta = [max(-WEIGHT_CAP, min(WEIGHT_CAP, b)) for b in beta]
    tot = sum(w) or 1.0
    means = [sum(wi * xi[j] for wi, xi in zip(w, X)) / tot
             for j in range(len(feats))]
    weights = {"intercept": round(-sum(b * m for b, m in zip(beta, means)), 4)}
    for f, b in zip(feats, beta):
        weights[f] = round(b, 4)
    info = {"n": len(trades), "raw_intercept": round(b0, 4),
            "weighted_win_rate": round(sum(wi * yi for wi, yi in zip(w, y)) / tot, 4),
            "recency_weight_min": round(min(w), 4) if w else None,
            "recency_weight_max": round(max(w), 4) if w else None,
            "feature_coverage": {f: sum(1 for xi in X if xi[j] != 0.0)
                                 for j, f in enumerate(feats)}}
    return weights, info


# -- replay ----------------------------------------------------------------

def trade_cost(notional, chain, costs):
    bps = costs["entry_bps"] + costs["exit_bps"]
    return notional * bps / 1e4 + costs["fee_usd"].get(chain, 0.0)


def replay(trades, weights, take_threshold, costs=None):
    costs = costs or DEFAULT_COSTS
    acfg = {"weights": weights, "take_threshold": take_threshold}
    out = []
    for t in trades:
        take, score, mult = allocator.score_signal(t["features"], acfg)
        notional = t["notional_usd"] or DEFAULT_NOTIONAL_USD
        base = t["realized_usd"] - trade_cost(notional, t["chain"], costs)
        alloc = (mult * t["realized_usd"]
                 - trade_cost(mult * notional, t["chain"], costs)) if take else 0.0
        out.append({"take": take, "score": score, "mult": mult,
                    "base_net": base, "alloc_net": alloc,
                    "realized_usd": t["realized_usd"], "chain": t["chain"],
                    "ts": t["ts"]})
    return out


def max_drawdown(values):
    peak = cum = dd = 0.0
    for v in values:
        cum += v
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def summarize(rep):
    n = len(rep)
    taken = [r for r in rep if r["take"]]
    base = sum(r["base_net"] for r in rep)
    alloc = sum(r["alloc_net"] for r in rep)
    return {
        "n": n, "n_taken": len(taken),
        "base_net_usd": round(base, 2), "alloc_net_usd": round(alloc, 2),
        "base_per_trade": round(base / n, 4) if n else None,
        "alloc_per_trade": round(alloc / n, 4) if n else None,
        "edge_per_trade": round((alloc - base) / n, 4) if n else None,
        "base_win_rate": round(sum(1 for r in rep if r["realized_usd"] > 0) / n, 4) if n else None,
        "alloc_win_rate": (round(sum(1 for r in taken if r["realized_usd"] > 0)
                                 / len(taken), 4) if taken else None),
        "base_max_dd_usd": round(max_drawdown([r["base_net"] for r in rep]), 2),
        "alloc_max_dd_usd": round(max_drawdown([r["alloc_net"] for r in rep]), 2),
        "avg_multiplier_taken": (round(sum(r["mult"] for r in taken) / len(taken), 4)
                                 if taken else None),
    }


def concentration(rep, key_fn):
    """Share of positive edge held by the single best group."""
    groups = {}
    for i, r in enumerate(rep):
        groups.setdefault(key_fn(i, r), []).append(r["alloc_net"] - r["base_net"])
    qual = {k: v for k, v in groups.items() if len(v) >= MIN_GROUP_TRADES}
    edges = {str(k): round(sum(v), 2) for k, v in groups.items()}
    counts = {str(k): len(v) for k, v in groups.items()}
    if len(qual) < 2:
        return {"evaluable": False, "share": None, "edges": edges, "counts": counts}
    pos = {k: sum(v) for k, v in qual.items() if sum(v) > 0}
    tot = sum(pos.values())
    share = (max(pos.values()) / tot) if tot > 0 else None
    return {"evaluable": True, "share": round(share, 4) if share is not None else None,
            "edges": edges, "counts": counts}


# -- gate ------------------------------------------------------------------

def split_sizes(n):
    n_oos = max(int(math.ceil(OOS_FRAC * n)), MIN_OOS)
    if n == 0 or 10 * n_oos > 4 * n:
        return None
    return n - n_oos, n_oos


def run_gate(trades, take_threshold, costs=None, proposed_weights=None):
    costs = costs or DEFAULT_COSTS
    n = len(trades)
    sizes = split_sizes(n)
    if sizes is None:
        return {"verdict": "insufficient_data", "n": n,
                "reason": ("need >= %d OOS closes at <= %d%% of the journal "
                           "(journal has %d priced closes; >= %d required)"
                           % (MIN_OOS, int(MAX_OOS_FRAC * 100), n,
                              int(math.ceil(MIN_OOS / MAX_OOS_FRAC)))),
                "checks": []}
    n_is, n_oos = sizes
    is_t, oos_t = trades[:n_is], trades[n_is:]
    if proposed_weights is None:
        w_is, fit_info = propose_weights(is_t, ref_ts=is_t[-1]["ts"])
    else:
        w_is, fit_info = proposed_weights, None
    rep_is = replay(is_t, w_is, take_threshold, costs)
    rep_oos = replay(oos_t, w_is, take_threshold, costs)
    s_is, s_oos = summarize(rep_is), summarize(rep_oos)
    rep_all = rep_is + rep_oos
    is_edge, oos_edge = s_is["edge_per_trade"], s_oos["edge_per_trade"]
    retention = (oos_edge / is_edge) if is_edge and is_edge > 0 else None
    decay = (1.0 - retention) if retention is not None else None
    chain_c = concentration(rep_all, lambda i, r: r["chain"])
    terc_c = concentration(rep_all, lambda i, r: min(2, i * 3 // len(rep_all)))
    checks = [
        ("is_edge_positive", is_edge is not None and is_edge > 0, is_edge, "> 0"),
        ("oos_edge_positive", oos_edge is not None and oos_edge > 0, oos_edge, "> 0"),
        ("oos_retention", retention is not None and retention >= RETENTION_MIN,
         None if retention is None else round(retention, 4), ">= %.2f" % RETENTION_MIN),
        ("oos_decay", decay is not None and decay <= MAX_OOS_DECAY,
         None if decay is None else round(decay, 4), "<= %.2f" % MAX_OOS_DECAY),
        ("is_win_rate_not_overfit",
         s_is["alloc_win_rate"] is None or s_is["alloc_win_rate"] <= MAX_IS_WIN_RATE,
         s_is["alloc_win_rate"], "<= %.2f" % MAX_IS_WIN_RATE),
        ("edge_not_single_chain",
         not chain_c["evaluable"] or (chain_c["share"] is not None
                                      and chain_c["share"] <= MAX_CONCENTRATION),
         chain_c["share"], "<= %.2f (if evaluable)" % MAX_CONCENTRATION),
        ("edge_not_single_time_regime",
         not terc_c["evaluable"] or (terc_c["share"] is not None
                                     and terc_c["share"] <= MAX_CONCENTRATION),
         terc_c["share"], "<= %.2f (if evaluable)" % MAX_CONCENTRATION),
    ]
    verdict = "pass" if all(c[1] for c in checks) else "fail"
    return {"verdict": verdict, "n": n, "n_is": n_is, "n_oos": n_oos,
            "is_window": [is_t[0]["ts_str"], is_t[-1]["ts_str"]],
            "oos_window": [oos_t[0]["ts_str"], oos_t[-1]["ts_str"]],
            "is_fit_weights": w_is, "is_fit_info": fit_info,
            "is": s_is, "oos": s_oos,
            "retention": None if retention is None else round(retention, 4),
            "decay": None if decay is None else round(decay, 4),
            "chain_concentration": chain_c, "time_concentration": terc_c,
            "checks": [{"name": c[0], "passed": bool(c[1]), "value": c[2],
                        "threshold": c[3]} for c in checks]}


# -- recency check ---------------------------------------------------------

def _mean_se(xs):
    n = len(xs)
    if n == 0:
        return None, None
    m = sum(xs) / n
    if n < 2:
        return m, None
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, math.sqrt(var / n)


def recency_check(trades):
    """Is recent (last ~30%) baseline performance better than the rest?"""
    n = len(trades)
    if n < 10:
        return {"evaluable": False}
    k = max(1, int(math.ceil(OOS_FRAC * n)))
    old = [t["realized_usd"] for t in trades[:-k]]
    new = [t["realized_usd"] for t in trades[-k:]]
    mo, so = _mean_se(old)
    mn, sn = _mean_se(new)
    diff = mn - mo
    se = math.sqrt((so or 0) ** 2 + (sn or 0) ** 2)
    z = diff / se if se > 0 else None
    full = sum(t["realized_usd"] for t in trades) / n
    better = diff > 0
    concentrated = better and (mo <= 0 or full <= 0 or (z is not None and z >= 1.0))
    return {"evaluable": True, "recent_n": len(new), "older_n": len(old),
            "recent_avg_usd": round(mn, 4), "older_avg_usd": round(mo, 4),
            "full_avg_usd": round(full, 4), "diff_usd": round(diff, 4),
            "z": None if z is None else round(z, 2),
            "recent_better": better, "recency_concentrated": concentrated,
            "recent_win_rate": round(sum(1 for x in new if x > 0) / len(new), 4),
            "older_win_rate": round(sum(1 for x in old if x > 0) / len(old), 4)
            if old else None}


# -- attribution -----------------------------------------------------------

def quintile_table(trades, value_fn):
    have = [(value_fn(t), t) for t in trades]
    missing = [t for v, t in have if v is None]
    have = sorted([(v, t) for v, t in have if v is not None], key=lambda x: x[0])
    rows = []
    n = len(have)
    if n:
        buckets = min(5, n)
        for b in range(buckets):
            chunk = have[b * n // buckets:(b + 1) * n // buckets]
            if not chunk:
                continue
            usd = [t["realized_usd"] for _, t in chunk]
            rows.append({"bucket": "Q%d" % (b + 1),
                         "range": "%s .. %s" % (_fmt(chunk[0][0]), _fmt(chunk[-1][0])),
                         "n": len(chunk), "avg_usd": sum(usd) / len(usd),
                         "win_rate": sum(1 for u in usd if u > 0) / len(usd),
                         "total_usd": sum(usd)})
    if missing:
        usd = [t["realized_usd"] for t in missing]
        rows.append({"bucket": "missing", "range": "-", "n": len(missing),
                     "avg_usd": sum(usd) / len(usd),
                     "win_rate": sum(1 for u in usd if u > 0) / len(usd),
                     "total_usd": sum(usd)})
    return rows


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, str):
        return v
    a = abs(v)
    if a >= 1e5:
        return "%.3g" % v
    if a >= 100:
        return "%.0f" % v
    return "%.3g" % v


def _num_feature(name):
    def fn(t):
        v = allocator._num(t["features"].get(name))
        return v
    return fn


def attribution(trades, current_cfg):
    def score_fn(t):
        return allocator.score_signal(t["features"], current_cfg)[1]
    tables = {"allocator score (current config weights)": quintile_table(trades, score_fn),
              "signal_gain_pct": quintile_table(trades, _num_feature("signal_gain_pct")),
              "liquidity_usd": quintile_table(trades, _num_feature("liquidity_usd")),
              "buy_sell_ratio": quintile_table(trades, _num_feature("buy_sell_ratio"))}
    by_chain = {}
    for t in trades:
        by_chain.setdefault(t["chain"], []).append(t["realized_usd"])
    tables["chain"] = [{"bucket": c, "range": "-", "n": len(u),
                        "avg_usd": sum(u) / len(u),
                        "win_rate": sum(1 for x in u if x > 0) / len(u),
                        "total_usd": sum(u)} for c, u in sorted(by_chain.items())]
    return tables


# -- report ----------------------------------------------------------------

def _md_table(rows):
    out = ["| bucket | range | n | avg realized $/trade | win rate | total $ |",
           "|---|---|---:|---:|---:|---:|"]
    for r in rows:
        out.append("| %s | %s | %d | %+.3f | %.0f%% | %+.2f |"
                   % (r["bucket"], r["range"], r["n"], r["avg_usd"],
                      r["win_rate"] * 100, r["total_usd"]))
    return "\n".join(out)


def _summary_rows(label, s):
    return ("| %s | %d | %d | %+.3f | %+.3f | %+.3f | %s | %s | %.2f | %.2f |"
            % (label, s["n"], s["n_taken"], s["base_per_trade"],
               s["alloc_per_trade"], s["edge_per_trade"],
               "-" if s["base_win_rate"] is None else "%.0f%%" % (s["base_win_rate"] * 100),
               "-" if s["alloc_win_rate"] is None else "%.0f%%" % (s["alloc_win_rate"] * 100),
               s["base_max_dd_usd"], s["alloc_max_dd_usd"]))


def recency_text(rc, half_life):
    if not rc.get("evaluable"):
        return "Too few closes to compare recent vs older performance."
    lines = ["Recent window (last %d closes): %+.3f $/trade, win rate %.0f%%. "
             "Older journal (%d closes): %+.3f $/trade, win rate %.0f%%. "
             "Full journal: %+.3f $/trade. Difference %+.3f $/trade (z = %s)."
             % (rc["recent_n"], rc["recent_avg_usd"], rc["recent_win_rate"] * 100,
                rc["older_n"], rc["older_avg_usd"], (rc["older_win_rate"] or 0) * 100,
                rc["full_avg_usd"], rc["diff_usd"],
                "n/a" if rc["z"] is None else rc["z"])]
    if rc["recency_concentrated"]:
        lines.append(
            "**RECENCY-CONCENTRATED.** Recent results are better than the "
            "older journal. The claim that the system \"has been doing way "
            "better now\" is consistent with the recent window, but this "
            "analyst does NOT treat it as a persistent edge: a short hot "
            "streak is indistinguishable from luck or a passing regime at "
            "this sample size. Weights are fit on the full journal with a "
            "%.0f-day half-life, and the recent window is not overweighted "
            "beyond that." % half_life)
        if rc["recent_avg_usd"] <= 0:
            lines.append("The recent window is still net negative (%+.3f "
                         "$/trade): \"better\" here means losing less, not "
                         "making money." % rc["recent_avg_usd"])
        if rc["z"] is None or rc["z"] < 2.0:
            lines.append("The improvement is %s standard errors: within noise."
                         % ("n/a" if rc["z"] is None else rc["z"]))
    elif rc["recent_better"]:
        lines.append("Recent results are modestly better than the older "
                     "journal, within noise. No extra weight is given to them.")
    else:
        lines.append("Recent results are not better than the older journal; "
                     "there is no recency-concentrated improvement to discount.")
    return "\n\n".join(lines)


def build_report(ctx):
    g = ctx["gate"]
    L = []
    L.append("# Allocator analyst report (%s)" % ctx["generated_at"])
    L.append("")
    L.append("**Verdict: `%s`**. %s" % (g["verdict"], {
        "pass": "The proposed weights may be applied manually (see Apply).",
        "fail": "Do not flip to live. Keep `allocator.mode = \"shadow\"`.",
        "insufficient_data": "Not decision-capable. Keep `allocator.mode = \"shadow\"`.",
    }[g["verdict"]]))
    L.append("")
    L.append("Paper journal only. Every number below is comparative "
             "(allocator vs 1.0x baseline on the same trades). Nothing here "
             "is a profitability claim.")
    L.append("")
    js = ctx["journal_stats"]
    L.append("## Data")
    L.append("")
    L.append("- Journal: `%s` (read-only), %d entries, %d closes; %d priced "
             "closes used (%d unpriced, %d without timestamp, %d closes with "
             "no matching entry -> features neutral)."
             % (ctx["journal"], js["entries"], js["closes"], ctx["n_trades"],
                js["unpriced"], js["no_ts"], js["no_entry"]))
    L.append("- Entries already carrying allocator fields (shadow/live): %d."
             % js["with_allocator_fields"])
    if ctx.get("forced"):
        L.append("- Journal growth since prior watermark: %d new close(s) "
                 "(%d priced); rerun via `--force` after analyst fixes."
                 % (ctx["new_closes"], ctx["new_priced_closes"]))
    fi = ctx["full_fit_info"]
    L.append("- Feature coverage (trades with a non-missing value): "
             + ", ".join("%s %d" % (k, v) for k, v in fi["feature_coverage"].items()))
    L.append("")
    L.append("## Recency discipline")
    L.append("")
    L.append("Training uses the FULL journal with exponential recency weights "
             "(half-life %.0f days). In this journal the oldest trade weighs "
             "%.3f and the newest 1.000."
             % (ctx["half_life_days"], fi["recency_weight_min"] or 0))
    L.append("")
    L.append(recency_text(ctx["recency"], ctx["half_life_days"]))
    L.append("")
    L.append("## P&L attribution (journal realized_usd, before replay costs)")
    for name, rows in ctx["attribution"].items():
        L.append("")
        L.append("### %s" % name)
        L.append("")
        L.append(_md_table(rows))
    L.append("")
    L.append("## Proposed weights (full journal, recency-weighted)")
    L.append("")
    L.append("Method: L2 (lambda=%.2f) logistic regression of P(win) on "
             "normalized features, deterministic gradient descent; each "
             "weight = change in win log-odds per unit of normalized feature. "
             "Intercept re-centered so the recency-weighted average trade "
             "scores 0.5 (1.0x). take_threshold = %.2f."
             % (L2, ctx["take_threshold"]))
    L.append("")
    L.append("| feature | current | proposed (full fit) | IS-only fit (gated) |")
    L.append("|---|---:|---:|---:|")
    isw = g.get("is_fit_weights") or {}
    for k in ["intercept"] + list(allocator.FEATURES):
        L.append("| %s | %+.4f | %+.4f | %s |"
                 % (k, ctx["current_weights"][k], ctx["proposed_weights"][k],
                    ("%+.4f" % isw[k]) if k in isw else "-"))
    L.append("")
    L.append("## Validation gate")
    L.append("")
    L.append("Costs: entry %.0f bps + exit %.0f bps of notional, plus fixed "
             "fee per trade %s. Linear ticket scaling (m x realized). The "
             "money.py risk ceiling and balance-aware sizing are future "
             "replay extensions; upsizing here is an upper bound."
             % (ctx["costs"]["entry_bps"], ctx["costs"]["exit_bps"],
                ", ".join("%s $%.2f" % kv for kv in sorted(ctx["costs"]["fee_usd"].items()))))
    L.append("")
    if g["verdict"] == "insufficient_data" and "is" not in g:
        L.append("Insufficient data: %s." % g["reason"])
    else:
        L.append("IS window %s .. %s (%d closes); OOS window %s .. %s (%d closes). "
                 "Weights refit on IS only."
                 % (g["is_window"][0], g["is_window"][1], g["n_is"],
                    g["oos_window"][0], g["oos_window"][1], g["n_oos"]))
        L.append("")
        L.append("| window | n | taken | base $/trade | alloc $/trade | edge $/trade "
                 "| base WR | alloc WR (taken) | base maxDD $ | alloc maxDD $ |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        L.append(_summary_rows("IS", g["is"]))
        L.append(_summary_rows("OOS", g["oos"]))
        L.append("")
        L.append("| check | value | threshold | result |")
        L.append("|---|---:|---|---|")
        for c in g["checks"]:
            L.append("| %s | %s | %s | %s |" % (c["name"], c["value"], c["threshold"],
                                                "PASS" if c["passed"] else "FAIL"))
        L.append("")
        L.append("Edge by chain (IS-fit replay, $): %s. Edge by time tercile ($): %s."
                 % (g["chain_concentration"]["edges"], g["time_concentration"]["edges"]))
        L.append("Chain concentration includes comparative edge from shrinking "
                 "or skipping trades on a weak chain; that is not evidence "
                 "of within-chain ranking skill.")
    fr = ctx.get("full_fit_replay")
    if fr:
        L.append("")
        L.append("In-sample only (informational, not gated): proposed full-fit "
                 "weights on the whole journal: edge %+.3f $/trade, %d/%d taken."
                 % (fr["edge_per_trade"], fr["n_taken"], fr["n"]))
    L.append("")
    L.append("## Apply (manual; only on a `pass` verdict)")
    L.append("")
    L.append("1. Copy `weights` from `analysis/allocator_proposal.json` into the "
             "`allocator.weights` section of `runs/paper-1h/config.json`.")
    L.append("2. Set `allocator.mode` to `\"live\"`.")
    L.append("3. Restart the bot so it reloads config. `dry_run` stays `true`.")
    L.append("")
    L.append("This script never edits the config.")
    L.append("")
    return "\n".join(L)


# -- main ------------------------------------------------------------------

def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def analyze(trades, journal_stats, acfg, journal_path, costs=None,
            half_life_days=HALF_LIFE_DAYS, generated_at=None):
    costs = costs or DEFAULT_COSTS
    thr = allocator.threshold_of(acfg or {})
    current = allocator.weights_of(acfg or {})
    gate = run_gate(trades, thr, costs)
    if trades:
        proposed, full_info = propose_weights(trades, half_life_days=half_life_days)
        full_rep = summarize(replay(trades, proposed, thr, costs))
    else:
        proposed, full_info, full_rep = dict(current), {
            "feature_coverage": {}, "recency_weight_min": None}, None
    return {"generated_at": generated_at or time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "journal": journal_path, "journal_stats": journal_stats,
            "n_trades": len(trades), "half_life_days": half_life_days,
            "take_threshold": thr, "costs": costs,
            "current_weights": current, "proposed_weights": proposed,
            "full_fit_info": full_info, "full_fit_replay": full_rep,
            "gate": gate, "recency": recency_check(trades),
            "attribution": attribution(trades, acfg or {})}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--journal", default=DEFAULT_JOURNAL)
    ap.add_argument("--config", default=DEFAULT_CONFIG,
                    help="bot config, read-only (current allocator weights)")
    ap.add_argument("--out-dir", default=ANALYSIS_DIR)
    ap.add_argument("--min-new", type=int, default=MIN_NEW_CLOSES)
    ap.add_argument("--force", action="store_true",
                    help="ignore the watermark (still updates it)")
    args = ap.parse_args(argv)

    watermark_path = os.path.join(args.out_dir, "allocator_watermark.json")
    rows, bad = load_journal(args.journal)
    n_closes = sum(1 for r in rows if r.get("type") == "close")
    wm = _read_json(watermark_path, {})
    seen = wm.get("closes_seen", 0) if isinstance(wm, dict) else 0
    if not isinstance(seen, int) or seen > n_closes:
        seen = 0
    new = n_closes - seen
    close_idx, new_priced = 0, 0
    for r in rows:
        if r.get("type") == "close":
            close_idx += 1
            if close_idx > seen and close_usd(r) is not None:
                new_priced += 1
    if not args.force and new_priced < args.min_new:
        log("no-op: %d new priced closes since watermark (need %d)"
            % (new_priced, args.min_new))
        return 0

    cfg = _read_json(args.config, {})
    acfg = cfg.get("allocator") if isinstance(cfg, dict) else None
    trades, stats = join_trades(rows)
    stats["malformed_lines"] = bad
    ctx = analyze(trades, stats, acfg if isinstance(acfg, dict) else {},
                  args.journal)
    ctx.update(forced=args.force, new_closes=new, new_priced_closes=new_priced)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    report_path = os.path.join(args.out_dir, "allocator_report_%s.md" % stamp)
    _write_atomic(report_path, build_report(ctx))
    g = ctx["gate"]
    proposal = {
        "generated_at": ctx["generated_at"], "verdict": g["verdict"],
        "weights": ctx["proposed_weights"],
        "take_threshold": ctx["take_threshold"],
        "weights_note": ("Full-journal recency-weighted fit. The gate validates "
                         "the same procedure refit on IS only (is_fit_weights)."),
        "gate": g, "recency": ctx["recency"],
        "costs": ctx["costs"], "half_life_days": ctx["half_life_days"],
        "journal_stats": stats, "report": os.path.basename(report_path),
        "apply": ("Manual, only if verdict == 'pass': copy 'weights' into the "
                  "'allocator' section of runs/paper-1h/config.json, set "
                  "allocator.mode to 'live', restart the bot. This script never "
                  "edits the config."),
    }
    _write_atomic(os.path.join(args.out_dir, "allocator_proposal.json"),
                  json.dumps(proposal, indent=1, sort_keys=True) + "\n")
    _write_atomic(watermark_path, json.dumps(
        {"closes_seen": n_closes, "updated_at": ctx["generated_at"],
         "report": os.path.basename(report_path)}, indent=1) + "\n")
    log("verdict=%s trades=%d report=%s" % (g["verdict"], len(trades), report_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
