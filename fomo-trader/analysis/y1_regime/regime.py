"""Y1 regime gate: WHEN to trade, not WHICH token.

Per-entry binary gate (trade / stand down) built ONLY from data available at entry
time. Pure stdlib. Fail-open everywhere: missing features -> trade.

Conventions (shared with sibling tracks):
- P&L via analysis/replay.py net_pnl / metrics (realistic costs, SOL_FALLBACK_USD).
- Journal ts format: 'YYYY-MM-DD HH:MM:SS' (naive; treated as ET for AM/PM halves).
"""
import json
import math
import os
import sys
import threading
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))          # analysis/y1_regime
_ANALYSIS = os.path.dirname(_HERE)                           # analysis
ROOT = os.path.dirname(_ANALYSIS)                            # fomo-trader repo root
if _ANALYSIS not in sys.path:
    sys.path.insert(0, _ANALYSIS)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from analysis.replay import net_pnl, metrics  # noqa: E402

JOURNAL = os.path.join(ROOT, "runs", "paper-1h", "trades.jsonl")

_TS_FMT = "%Y-%m-%d %H:%M:%S"
_SPLIT_TS = "2026-09-26T00:00:00"  # pre-registered IS/OOS boundary


def _parse_ts(ts):
    try:
        return datetime.strptime(ts, _TS_FMT)
    except (TypeError, ValueError):
        return None


def load_rows(path=JOURNAL):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def match_entry(close, entries_by_mint):
    """Latest entry with same mint and ts <= close ts. None -> orphan (fail open)."""
    cts = _parse_ts(close.get("ts"))
    if cts is None:
        return None
    best = None
    for e in entries_by_mint.get(close.get("mint"), []):
        ets = _parse_ts(e.get("ts"))
        if ets is not None and ets <= cts and (best is None or ets > _parse_ts(best["ts"])):
            best = e
    return best


def build_features(entries, closes):
    """Per-close regime features from closes strictly before the matched entry's ts.

    Returns {close_index: features}. Features are native floats/ints or None.
    close_native_usd: net USD of each close (via net_pnl, standard costs), used only
    from strictly-prior closes -- never the close being gated.
    """
    entries_by_mint = {}
    for e in entries:
        entries_by_mint.setdefault(e.get("mint"), []).append(e)
    # native usd per close (standard costs); None -> treated as 0.0 in sums
    usd_of = {}
    for i, c in enumerate(closes):
        _, u = net_pnl(c)
        usd_of[i] = float(u) if u is not None else 0.0
    feats = {}
    for i, c in enumerate(closes):
        entry = match_entry(c, entries_by_mint)
        if entry is None:
            feats[i] = {"orphan": True}
            continue
        ets = _parse_ts(entry["ts"])
        prior = [(j, closes[j]) for j in range(len(closes))
                 if j != i and _parse_ts(closes[j].get("ts")) is not None
                 and _parse_ts(closes[j]["ts"]) < ets]
        prior.sort(key=lambda t: _parse_ts(t[1]["ts"]))
        f = {"orphan": False}
        if not prior:
            f.update({"trail_12h_usd": None, "trail_24h_usd": None,
                      "trail_12h_n": 0, "gap_hours": None,
                      "cum_usd_before": 0.0})
        else:
            w12 = [j for j, pc in prior if _parse_ts(pc["ts"]) >= ets - timedelta(hours=12)]
            w24 = [j for j, pc in prior if _parse_ts(pc["ts"]) >= ets - timedelta(hours=24)]
            last_ts = _parse_ts(prior[-1][1]["ts"])
            f.update({
                "trail_12h_usd": sum(usd_of[j] for j in w12),
                "trail_24h_usd": sum(usd_of[j] for j in w24),
                "trail_12h_n": len(w12),
                "gap_hours": (ets - last_ts).total_seconds() / 3600.0 if last_ts else None,
                "cum_usd_before": sum(usd_of[j] for j, _ in prior),
            })
        # entry calendar features (known at decision time, no look-ahead)
        f["entry_half"] = "AM" if ets.hour < 12 else "PM"
        f["entry_dow"] = ets.strftime("%a")
        feats[i] = f
    return feats


# ---------------------------------------------------------------- gates
# Each gate: (features) -> True means TRADE, False means STAND DOWN (veto).
# Missing/None features fail open (trade).

def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def gate_trail12_pnl(threshold):
    def g(f):
        if f.get("orphan"):
            return True
        v = _num(f.get("trail_12h_usd"))
        return True if v is None else not (v < threshold)
    g.__name__ = "R1_trail12_pnl<%g" % threshold
    return g


def gate_trail24_pnl(threshold):
    def g(f):
        if f.get("orphan"):
            return True
        v = _num(f.get("trail_24h_usd"))
        return True if v is None else not (v < threshold)
    g.__name__ = "R2_trail24_pnl<%g" % threshold
    return g


def gate_thin(n_min):
    def g(f):
        if f.get("orphan"):
            return True
        n = f.get("trail_12h_n")
        if n is None:
            return True
        # no prior window at all -> fail open; thin only when a window exists but is quiet
        if f.get("trail_12h_usd") is None:
            return True
        return not (n < n_min)
    g.__name__ = "R3_thin12h<%d" % n_min
    return g


def gate_cold_restart(max_gap_h=12.0):
    def g(f):
        if f.get("orphan"):
            return True
        v = _num(f.get("gap_hours"))
        return True if v is None else not (v > max_gap_h)
    g.__name__ = "R4_cold_restart>%gh" % max_gap_h
    return g


def gate_half(stand_down_half):
    def g(f):
        if f.get("orphan"):
            return True
        h = f.get("entry_half")
        return True if h is None else h != stand_down_half
    g.__name__ = "R5_standdown_%s" % stand_down_half
    return g


def gate_drawdown(floor=-50.0):
    def g(f):
        if f.get("orphan"):
            return True
        v = _num(f.get("cum_usd_before"))
        return True if v is None else not (v < floor)
    g.__name__ = "R6_drawdown<%g" % floor
    return g


CANDIDATE_GATES = (
    [gate_trail12_pnl(t) for t in (0, -5, -10, -20)]
    + [gate_trail24_pnl(t) for t in (0, -10, -20, -40)]
    + [gate_thin(n) for n in (3, 5, 10)]
    + [gate_cold_restart()]
    + [gate_half("PM"), gate_half("AM")]
    + [gate_drawdown()]
)


# ---------------------------------------------------------------- evaluation
def evaluate_gate(gate, indexed_closes, feats, stress=False):
    """Apply gate; return metrics for retained closes vs baseline.

    indexed_closes: list of (original_index, close) so feats stay aligned.
    """
    kept = [c for i, c in indexed_closes if gate(feats[i])]
    all_c = [c for _, c in indexed_closes]
    m_kept = metrics(kept, stress=stress)
    m_base = metrics(all_c, stress=stress)
    n_ret = m_kept["n"]
    edge = ((m_kept["usd"] - m_base["usd"]) / n_ret) if n_ret else 0.0
    return {"gate": gate.__name__, "n_retained": n_ret, "n_total": len(all_c),
            "kept": m_kept, "baseline": m_base, "edge_per_trade": edge}


def split_is_oos(closes):
    cut = datetime.fromisoformat(_SPLIT_TS)
    is_c = [c for c in closes if _parse_ts(c.get("ts")) and _parse_ts(c["ts"]) < cut]
    oos_c = [c for c in closes if _parse_ts(c.get("ts")) and _parse_ts(c["ts"]) >= cut]
    return is_c, oos_c


def full_report(path=JOURNAL, stress=False):
    rows = load_rows(path)
    entries = [r for r in rows if r.get("type") == "entry"]
    closes = sorted([r for r in rows if r.get("type") == "close"],
                    key=lambda r: r.get("ts") or "")
    feats = build_features(entries, closes)
    is_c, oos_c = split_is_oos(closes)
    idx = {id(c): i for i, c in enumerate(closes)}
    indexed_is = [(idx[id(c)], c) for c in is_c]
    indexed_oos = [(idx[id(c)], c) for c in oos_c]
    out = []
    for gate in CANDIDATE_GATES:
        r_is = evaluate_gate(gate, indexed_is, feats, stress=False)
        r_oos = evaluate_gate(gate, indexed_oos, feats, stress=False)
        r_oos_stress = evaluate_gate(gate, indexed_oos, feats, stress=True)
        b_oos_stress = metrics(oos_c, stress=True)
        out.append({
            "gate": gate.__name__,
            "IS": {"n": r_is["n_total"], "retained": r_is["n_retained"],
                   "usd": r_is["kept"]["usd"], "pf": r_is["kept"]["pf"],
                   "wr": r_is["kept"]["win_rate"], "dd": r_is["kept"]["max_dd_usd"],
                   "edge": r_is["edge_per_trade"],
                   "base_usd": r_is["baseline"]["usd"]},
            "OOS": {"n": r_oos["n_total"], "retained": r_oos["n_retained"],
                    "usd": r_oos["kept"]["usd"], "pf": r_oos["kept"]["pf"],
                    "wr": r_oos["kept"]["win_rate"], "dd": r_oos["kept"]["max_dd_usd"],
                    "edge": r_oos["edge_per_trade"],
                    "base_usd": r_oos["baseline"]["usd"]},
            "OOS_stress3x": {"kept_usd": r_oos_stress["kept"]["usd"],
                             "base_usd": b_oos_stress["usd"],
                             "kept_pf": r_oos_stress["kept"]["pf"]},
        })
    return out


# Thread-safety: gates are pure functions of the feature dict; no shared state.
# A lock-free fallback counter is not needed (no fallbacks beyond fail-open booleans).
_GATE_LOCK = threading.Lock()  # reserved; gates intentionally take no locks.


def benchmark_gate(n=20000):
    """p50/p99 latency of a representative gate decision over synthetic features."""
    import time
    import random
    gate = gate_trail12_pnl(-10)
    feats = [{"orphan": False, "trail_12h_usd": random.uniform(-50, 20),
              "trail_24h_usd": random.uniform(-80, 30), "trail_12h_n": random.randint(0, 20),
              "gap_hours": random.uniform(0, 72), "cum_usd_before": random.uniform(-120, 10),
              "entry_half": "AM", "entry_dow": "Thu"} for _ in range(n)]
    # warmup
    for f in feats[:1000]:
        gate(f)
    ts = []
    for f in feats:
        t0 = time.perf_counter_ns()
        gate(f)
        ts.append((time.perf_counter_ns() - t0) / 1000.0)
    ts.sort()
    return {"n": n, "p50_us": ts[n // 2], "p99_us": ts[int(n * 0.99)],
            "max_us": ts[-1]}
