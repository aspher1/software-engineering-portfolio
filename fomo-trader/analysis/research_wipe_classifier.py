#!/usr/bin/env python3
"""Research prototype: fast pre-entry wipe classifier (stdlib only, offline).

NOT wired into fomo_trader.py. Honest replay protocol mirrored from the
2026-09-28 fundamental-fix validation:
- FIFO (chain, mint) pairing, append-order split (first 2/3 closes IS).
- Features from the ENTRY record only (no look-ahead).
- Costed P&L via analysis/replay.py net_pnl at 1x and 3x stress.
- Veto: skipped entries contribute $0. Edge = (retained_net - full_net)/n.
- Gates: OOS edge > 0; OOS edge >= 60% of IS edge; retained OOS net > $0 at
  1x AND 3x; retained OOS n >= 30; retained win rate <= 90%; both chains.
"""
import json
import math
import sys
from pathlib import Path

ROOT = Path("/home/hatch/workspace/fomo-trader")
sys.path.insert(0, str(ROOT / "analysis"))
import replay  # noqa: E402

FEATS = ["signal_gain_pct", "signal_ratio", "liquidity_usd", "m15_buys",
         "m15_sells", "m15_volume_usd", "mcap_usd", "entry_latency_ms",
         "slip_from_signal_pct"]
# log-scale these (heavy right tail)
LOG_FEATS = {"signal_gain_pct", "liquidity_usd", "m15_buys", "m15_sells",
             "m15_volume_usd", "mcap_usd"}


def num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def entry_vec(e):
    out = []
    for f in FEATS:
        v = num(e.get(f))
        if v is not None and f in LOG_FEATS:
            v = math.log1p(max(v, 0.0))
        out.append(v)
    return out


def fit_stats(rows):
    meds, means, sds = [], [], []
    cols = list(zip(*[entry_vec(t["entry_record"] or {}) for t in rows]))
    for col in cols:
        vals = sorted(v for v in col if v is not None)
        med = vals[len(vals) // 2] if vals else 0.0
        meds.append(med)
        filled = [v if v is not None else med for v in col]
        mu = sum(filled) / len(filled)
        sd = math.sqrt(sum((v - mu) ** 2 for v in filled) / len(filled)) or 1.0
        means.append(mu)
        sds.append(sd)
    return meds, means, sds


def design(rows, meds, means, sds):
    X = []
    for t in rows:
        raw = entry_vec(t["entry_record"] or {})
        X.append([1.0] + [(v if v is not None else meds[i] - means[i]) / sds[i]
                          if False else
                          ((v if v is not None else meds[i]) - means[i]) / sds[i]
                          for i, v in enumerate(raw)])
    return X


def train_logreg(X, y, l2=1.0, iters=2000, lr=0.1):
    w = [0.0] * len(X[0])
    n = len(X)
    for _ in range(iters):
        grad = [0.0] * len(w)
        for xi, yi in zip(X, y):
            z = sum(a * b for a, b in zip(w, xi))
            p = 1.0 / (1.0 + math.exp(-max(-500.0, min(500.0, z))))
            err = p - yi
            for j in range(len(w)):
                grad[j] += err * xi[j]
        for j in range(len(w)):
            w[j] -= lr * (grad[j] / n + (l2 * w[j] / n if j else 0.0))
    return w


def pwipe(w, x):
    z = sum(a * b for a, b in zip(w, x))
    z = max(-500.0, min(500.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def usd_net(trades, stress):
    tot = 0.0
    for t in trades:
        _, u = replay.net_pnl(t, stress=stress)
        tot += u or 0.0
    return tot


def wins(trades):
    return sum(1 for t in trades if (replay.net_pnl(t)[1] or 0.0) > 0)


def main():
    trades, counts = replay.load_journal()
    paired = [t for t in trades if not t.get("unmatched_close")
              and t.get("entry_record")]
    n = len(paired)
    cut = (2 * n) // 3
    IS, OOS = paired[:cut], paired[cut:]
    print("paired=%d IS=%d OOS=%d" % (n, len(IS), len(OOS)))

    # label: one-tick wipe = realized native return <= -90%
    def label(t):
        en = num(t.get("entry"))
        ex = num(t.get("exit"))
        if not en or not ex:
            return 0
        return 1 if (ex - en) / en <= -0.9 else 0

    y_is = [label(t) for t in IS]
    print("IS wipe rate: %d/%d" % (sum(y_is), len(y_is)))

    meds, means, sds = fit_stats(IS)
    X_is = design(IS, meds, means, sds)
    w = train_logreg(X_is, y_is)
    print("weights:", [round(v, 3) for v in w])

    # threshold sweep on IS only: maximize IS edge, keep >=30 retained
    scored = sorted(((pwipe(w, x), t) for x, t in zip(X_is, IS)),
                    key=lambda r: r[0])
    full_is_1x = usd_net(IS, False)
    best = None
    for k in range(0, len(IS)):
        thr = scored[k][0] if k < len(IS) else 1.0
        kept = [t for p, t in scored if p < thr]
        if len(kept) < 30:
            continue
        edge = (usd_net(kept, False) - full_is_1x) / len(IS)
        if best is None or edge > best[0]:
            best = (edge, thr, kept)
    edge_is, thr, kept_is = best
    print("IS: full_net_1x=%.2f kept=%d thr=%.3f edge/trade=%.4f"
          % (full_is_1x, len(kept_is), thr, edge_is))

    # ---- single OOS evaluation ----
    X_oos = design(OOS, meds, means, sds)
    kept_oos = [t for p, t in
                sorted(((pwipe(w, x), t) for x, t in zip(X_oos, OOS)),
                       key=lambda r: r[0]) if p < thr]
    full_oos_1x, full_oos_3x = usd_net(OOS, False), usd_net(OOS, True)
    ret_1x, ret_3x = usd_net(kept_oos, False), usd_net(kept_oos, True)
    edge_oos_1x = (ret_1x - full_oos_1x) / len(OOS)
    edge_oos_3x = (ret_3x - full_oos_3x) / len(OOS)
    chains = sorted({replay.chain_of(t) for t in kept_oos})
    wr = wins(kept_oos) / len(kept_oos) if kept_oos else 0
    print("OOS: full 1x=%.2f 3x=%.2f | kept=%d ret_1x=%.2f ret_3x=%.2f "
          "edge_1x=%.4f edge_3x=%.4f win_rate=%.2f chains=%s"
          % (full_oos_1x, full_oos_3x, len(kept_oos), ret_1x, ret_3x,
             edge_oos_1x, edge_oos_3x, wr, chains))
    print("retention of IS edge (1x): %.1f%%"
          % (100 * edge_oos_1x / edge_is if edge_is else 0))

    # latency micro-benchmark: one veto evaluation
    import time
    x0 = X_oos[0]
    t0 = time.perf_counter()
    for _ in range(10000):
        pwipe(w, x0)
    dt = (time.perf_counter() - t0) / 10000 * 1e6
    print("veto eval latency: %.1f us per entry" % dt)


if __name__ == "__main__":
    main()
