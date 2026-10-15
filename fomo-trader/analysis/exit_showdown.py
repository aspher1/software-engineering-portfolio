"""Empirical showdown: V0..V4 exit variants replayed on quote_paths.

Analysis-only. Never touches bot state, config, or journals.
Fixed historical entries; only exit logic varies across variants.

Variants (from on-disk baseline V0):
  V0: tps=[[50,50]], trail=25, hard=35            (on-disk config.json)
  V1: tps=[[100,50],[200,25]], trail=25, hard=35  (described TP ladder)
  V2: tps=[[50,50]], trail=30, hard=35           (described trailing stop)
  V3: tps=[[100,50],[200,25]], trail=30, hard=35  (described TP + trail)
  V4: tps=[[50,50]], trail=25, hard=20           (honest reachable hard stop)

Held constant: dump 12%/60s, stale 30min/<10%, venue m5 -30%/30s,
trail_after_tp 12%, veto_positive_slip, caps, sizing, kill switch, rug guard.

Exit state machine mirrors fomo_trader._manage_once (read 2026-09-28):
  per tick (no look-ahead): update peak -> TP rungs at market -> elif chain
  venue_dump -> dump_detector -> trailing_stop -> hard_stop -> stale_exit.
  Trail tightens to min(trail,12) once a rung fired (applies from next tick,
  as in the bot). Venue m5 is approximated from the path's own 5-min return
  (the bot uses DexScreener's independent venue tape) - documented approx.

Cost model vendored from analysis/replay.py as read 2026-09-28
(SWAP_FEE 0.0025/leg, SLIPPAGE 0.0015/leg, BNB gas 0.00002/leg,
SOL priority 2M lamports/leg). Primary P&L is gross of these costs so it
reconciles with Lane-Validation's realized_usd baseline; costed/3x-stress
numbers are reported as sensitivity.
"""

import json
import math
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES = os.path.join(ROOT, "runs", "paper-1h", "trades.jsonl")
QPATHS = os.path.join(ROOT, "runs", "paper-1h", "quote_paths")
OUT_JSON = os.path.join(ROOT, "analysis", "exit_showdown_results.json")

# ---- vendored cost model (analysis/replay.py, read 2026-09-28) ----
SWAP_FEE = {"solana": 0.0025, "bsc": 0.0025}
SLIPPAGE = 0.0015
BNB_GAS_NATIVE_PER_LEG = 0.00002
SOL_PRIORITY_LAMPORTS_PER_LEG = 2_000_000
SOL_LAMPORTS = 1_000_000_000
SOL_FALLBACK_USD = 115.0


def chain_of(row):
    return row.get("chain") or ("bsc" if str(row.get("mint", "")).startswith("0x") else "solana")


def estimated_cost_native(stake, price_ratio, chain, stress=False):
    variable = (stake + stake * max(0.0, price_ratio)) * (
        SWAP_FEE[chain] + SLIPPAGE * (3 if stress else 1))
    if chain == "bsc":
        fixed = 2 * BNB_GAS_NATIVE_PER_LEG
    else:
        fixed = 2 * SOL_PRIORITY_LAMPORTS_PER_LEG / SOL_LAMPORTS
    return variable + fixed


# ---- variants ----
DUMP_PCT, DUMP_WIN = 12.0, 60
STALE_MIN, STALE_GAIN = 30, 10.0
VENUE_M5, VENUE_SEC = -30.0, 30
TRAIL_AFTER_TP = 12.0

VARIANTS = {
    "V0": {"tps": [[50, 50]], "trail": 25, "hard": 35},
    "V1": {"tps": [[100, 50], [200, 25]], "trail": 25, "hard": 35},
    "V2": {"tps": [[50, 50]], "trail": 30, "hard": 35},
    "V3": {"tps": [[100, 50], [200, 25]], "trail": 30, "hard": 35},
    "V4": {"tps": [[50, 50]], "trail": 25, "hard": 20},
}


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def load_stamped():
    """Close lines with realized_usd != null (Lane-Validation stamped def),
    FIFO-paired to entries by mint for the entry timestamp."""
    pending = defaultdict(deque)
    stamped = []
    for line in open(TRADES, encoding="utf-8"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("type") == "entry":
            pending[r["mint"].lower()].append(r)
        elif r.get("type") == "close" and r.get("realized_usd") is not None:
            e = pending[r["mint"].lower()].popleft() if pending[r["mint"].lower()] else None
            stamped.append({"entry": e, "close": r})
    return stamped


def load_path(mint):
    p = os.path.join(QPATHS, "".join(
        c for c in str(mint).lower() if c.isalnum() and c.isascii()) + ".jsonl")
    if not os.path.exists(p):
        return None
    ticks = []
    for line in open(p, encoding="utf-8"):
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            continue
        px = t.get("price_usd")
        if px is None or not math.isfinite(px) or px < 0:
            continue
        ticks.append((parse_ts(t["ts"]), float(px)))
    ticks.sort(key=lambda x: x[0])
    return ticks


def bucket_reason(reason):
    r = str(reason or "")
    if "venue dump" in r:
        return "venue-dump"
    if "dump detector" in r:
        return "dump-detector"
    if "trailing stop" in r:
        return "trailing-stop"
    if "stale exit" in r:
        return "stale"
    if "take profit" in r.lower():
        return "take-profit"
    if "hard stop" in r:
        return "hard-stop"
    return "other:" + r[:40]


def replay_trade(entry_px, entry_dt, ticks, spec):
    """Replay one trade's tape under one variant spec.

    Returns dict with fills, exit reason, proceeds ratio R (proceeds/stake),
    max gain seen, and whether we force-closed at the recorded close.
    """
    tps, trail, hard = spec["tps"], spec["trail"], spec["hard"]
    peak = entry_px
    remaining = 1.0
    fills = []          # (fraction_of_original, fill_price)
    fired = set()
    max_gain = -1e9
    hist = deque()      # (dt, price) for dump detector
    last_venue = None
    exit_reason = None
    exit_dt = None

    for dt, price in ticks:
        if dt < entry_dt:
            continue
        if price > peak:
            peak = price
        gain = (price - entry_px) / entry_px * 100 if entry_px > 0 else 0.0
        max_gain = max(max_gain, gain)
        dd_peak = (peak - price) / peak * 100 if peak > 0 else 0.0
        dd_entry = -gain

        # trail tightens after a rung fired (bot: from rungs fired on PRIOR ticks)
        trail_eff = min(trail, TRAIL_AFTER_TP) if fired else trail

        # TP rungs fire first, at market (tick price), on remaining balance
        for i, (tp_pct, sell_pct) in enumerate(tps):
            if i not in fired and gain >= tp_pct and remaining > 0:
                frac = remaining * sell_pct / 100.0
                fills.append((frac, price))
                remaining -= frac
                fired.add(i)
                if remaining <= 1e-12:
                    exit_reason = "take-profit-full"
                    exit_dt = dt
                    break
        if exit_reason:
            break

        # dump detector: 12% off the 60s rolling max
        hist.append((dt, price))
        while hist and (dt - hist[0][0]).total_seconds() > DUMP_WIN:
            hist.popleft()
        wmax = max(p for _, p in hist) if hist else 0.0
        dumped = wmax > 0 and (wmax - price) / wmax * 100 >= DUMP_PCT

        # venue dump: path's own m5 (approx of DexScreener venue tape), every 30s
        venue_dumped = False
        if last_venue is None or (dt - last_venue).total_seconds() >= VENUE_SEC:
            last_venue = dt
            ref = None
            for rdt, rpx in reversed(ticks):
                if rdt >= dt:
                    continue
                age = (dt - rdt).total_seconds()
                if 270 <= age <= 330:
                    ref = rpx
                    break
                if age > 330:
                    break
            if ref:
                m5 = (price - ref) / ref * 100
                venue_dumped = m5 <= VENUE_M5

        if venue_dumped and remaining > 0:
            fills.append((remaining, price)); remaining = 0
            exit_reason = "venue-dump"; exit_dt = dt; break
        elif dumped and remaining > 0:
            fills.append((remaining, price)); remaining = 0
            exit_reason = "dump-detector"; exit_dt = dt; break
        elif dd_peak >= trail_eff and remaining > 0:
            fills.append((remaining, price)); remaining = 0
            exit_reason = "trailing-stop"; exit_dt = dt; break
        elif dd_entry >= hard and remaining > 0:
            fills.append((remaining, price)); remaining = 0
            exit_reason = "hard-stop"; exit_dt = dt; break
        elif ((dt - entry_dt).total_seconds() >= STALE_MIN * 60
              and gain < STALE_GAIN and remaining > 0):
            fills.append((remaining, price)); remaining = 0
            exit_reason = "stale"; exit_dt = dt; break

    forced = False
    if remaining > 1e-12:
        # exits never fired on the tape: force-close at last tick <= recorded close
        last_px = ticks[-1][1] if ticks else entry_px
        fills.append((remaining, last_px))
        remaining = 0
        exit_reason = (exit_reason or "force-close@tape-end")
        forced = True

    R = sum(frac * (px / entry_px) for frac, px in fills) if entry_px > 0 else 0.0
    return {"fills": [(f, p) for f, p in fills], "exit_reason": exit_reason,
            "R": R, "max_gain_pct": max_gain, "forced": forced,
            "exit_dt": exit_dt.strftime("%Y-%m-%d %H:%M:%S") if exit_dt else None,
            "rungs_fired": sorted(fired)}


def metrics(rows):
    """rows: list of dicts with usd (gross), usd_base, usd_stress3, close_dt."""
    n = len(rows)
    vals = [r["usd"] for r in rows]
    wins = sum(v > 0 for v in vals)
    gains = sum(v for v in vals if v > 0)
    losses = -sum(v for v in vals if v < 0)
    cum = high = dd = 0.0
    for v in vals:
        cum += v
        high = max(high, cum)
        dd = max(dd, high - cum)
    return {
        "n": n, "net": round(sum(vals), 2),
        "pf": round(gains / losses, 4) if losses > 0 else (float("inf") if gains > 0 else 0.0),
        "wr": round(wins / n, 4) if n else 0.0,
        "max_dd": round(dd, 2),
        "avg": round(sum(vals) / n, 4) if n else 0.0,
        "net_base_cost": round(sum(r["usd_base"] for r in rows), 2),
        "net_stress3": round(sum(r["usd_stress3"] for r in rows), 2),
        "pf_base_cost": None, "pf_stress3": None,
    }


def pf_of(vals):
    gains = sum(v for v in vals if v > 0)
    losses = -sum(v for v in vals if v < 0)
    return gains / losses if losses > 0 else (float("inf") if gains > 0 else 0.0)


def main():
    stamped = load_stamped()
    print("stamped closes (realized_usd != null):", len(stamped))

    # ---------- 1. coverage audit ----------
    audit = []
    for s in stamped:
        e, c = s["entry"], s["close"]
        rec = {"mint": c["mint"], "close_ts": c["ts"],
               "entry_ts": e["ts"] if e else None,
               "reason": c.get("reason"), "bucket": bucket_reason(c.get("reason")),
               "realized_usd": c.get("realized_usd"),
               "path": False, "full_cover": False, "ticks": 0}
        if e:
            ticks = load_path(c["mint"])
            if ticks:
                rec["path"] = True
                rec["ticks"] = len(ticks)
                edt, cdt = parse_ts(e["ts"]), parse_ts(c["ts"])
                # tolerance: first tick ~one poll after entry, last tick ~one
                # poll before the recorded close (both journaled separately)
                if (ticks[0][0] <= edt + timedelta(seconds=120)
                        and ticks[-1][0] >= cdt - timedelta(seconds=120)):
                    rec["full_cover"] = True
                    gaps = [(ticks[i+1][0] - ticks[i][0]).total_seconds()
                            for i in range(len(ticks) - 1)]
                    gaps.sort()
                    rec["median_gap_s"] = gaps[len(gaps)//2] if gaps else None
        audit.append(rec)

    by_bucket = defaultdict(list)
    for a in audit:
        by_bucket[a["bucket"]].append(a)
    print("\n== COVERAGE AUDIT ==")
    print(f"{'bucket':<22}{'n':>4}{'path':>6}{'full':>6}{'full%':>7}")
    for b, rows in sorted(by_bucket.items(), key=lambda x: -len(x[1])):
        n = len(rows)
        np_ = sum(r["path"] for r in rows)
        nf = sum(r["full_cover"] for r in rows)
        print(f"{b:<22}{n:>4}{np_:>6}{nf:>6}{100*nf/n:>6.1f}%")
    n_full = sum(a["full_cover"] for a in audit)
    print(f"TOTAL full-window coverage: {n_full}/{len(audit)} = {100*n_full/len(audit):.1f}%")

    # ---------- 2. replay covered trades ----------
    covered = [s for s, a in zip(stamped, audit) if a["full_cover"]]
    print("\nfully-covered trades entering replay:", len(covered))

    results = {}   # variant -> list of per-trade rows
    fidelity = []  # V0 replay usd vs recorded realized_usd
    moot = {"reach_50": 0, "reach_100": 0, "reach_200": 0, "n": 0}
    exit_reasons = defaultdict(lambda: defaultdict(int))

    for s in covered:
        e, c = s["entry"], s["close"]
        ticks = load_path(c["mint"])
        edt = parse_ts(e["ts"])
        # restrict tape to [entry, close+120s]; force-close uses last tick <= close
        cdt = parse_ts(c["ts"])
        tape = [(dt, px) for dt, px in ticks if dt <= cdt]
        if not tape:
            continue
        entry_px = float(c["entry"])
        stake = float(c.get("buy_bnb") or c.get("buy_sol") or 0)
        chain = chain_of(c)
        rate = c.get("sol_usd") or (SOL_FALLBACK_USD if chain == "solana" else None)
        if not rate or stake <= 0 or entry_px <= 0:
            continue
        # UNIT FIX: journal entry/exit/peak are NATIVE prices (BNB/SOL);
        # quote-path price_usd is native * fx. Work in native units like the
        # bot (gains/ratios are unit-free; a constant fx bias cancels).
        tape_native = [(dt, px / rate) for dt, px in tape]
        base = {"mint": c["mint"][:12], "close_ts": c["ts"],
                "recorded_usd": c["realized_usd"],
                "recorded_reason": bucket_reason(c.get("reason")),
                "stake": stake, "chain": chain}
        for vname, spec in VARIANTS.items():
            rp = replay_trade(entry_px, edt, tape_native, spec)
            usd = stake * (rp["R"] - 1) * rate
            cost1 = estimated_cost_native(stake, rp["R"], chain, stress=False)
            cost3 = estimated_cost_native(stake, rp["R"], chain, stress=True)
            row = dict(base, variant=vname, usd=round(usd, 4),
                       usd_base=round(usd - cost1 * rate, 4),
                       usd_stress3=round(usd - cost3 * rate, 4),
                       exit_reason=rp["exit_reason"], forced=rp["forced"],
                       max_gain_pct=round(rp["max_gain_pct"], 2),
                       rungs_fired=rp["rungs_fired"])
            results.setdefault(vname, []).append(row)
            exit_reasons[vname][rp["exit_reason"]] += 1
            if vname == "V0":
                fidelity.append((c["realized_usd"], usd))
                moot["n"] += 1
                if rp["max_gain_pct"] >= 50: moot["reach_50"] += 1
                if rp["max_gain_pct"] >= 100: moot["reach_100"] += 1
                if rp["max_gain_pct"] >= 200: moot["reach_200"] += 1

    # ---------- 3. walk-forward metrics ----------
    print("\n== VARIANT VERDICTS (walk-forward: IS < 2026-09-28, OOS = 2026-09-28) ==")
    verdict = {}
    for vname in VARIANTS:
        rows = results.get(vname, [])
        rows_sorted = sorted(rows, key=lambda r: r["close_ts"])
        is_rows = [r for r in rows_sorted if r["close_ts"] < "2026-09-28"]
        oos_rows = [r for r in rows_sorted if r["close_ts"] >= "2026-09-28"]
        m_all, m_is, m_oos = metrics(rows_sorted), metrics(is_rows), metrics(oos_rows)
        m_all["pf_base_cost"] = round(pf_of([r["usd_base"] for r in rows_sorted]), 4)
        m_all["pf_stress3"] = round(pf_of([r["usd_stress3"] for r in rows_sorted]), 4)
        retention = (m_oos["pf"] / m_is["pf"]
                     if m_is["pf"] not in (0, float("inf")) and m_oos["pf"] != float("inf") else None)
        decay = (1 - retention) if retention is not None else None
        red = []
        if m_is["wr"] > 0.90: red.append("WR_IS>90%")
        if decay is not None and decay > 0.70: red.append("OOS decay>70%")
        if retention is not None and retention < 0.60: red.append("retention<60%")
        # single-regime: all OOS closes on one date already; check day concentration
        verdict[vname] = {"all": m_all, "is": m_is, "oos": m_oos,
                          "retention": round(retention, 4) if retention is not None else None,
                          "red_flags": red,
                          "exit_reasons": dict(exit_reasons[vname])}
        print(f"\n{vname}: n={m_all['n']} (IS {m_is['n']}/OOS {m_oos['n']}) "
              f"net={m_all['net']:+.2f} PF={m_all['pf']} WR={m_all['wr']:.1%} DD={m_all['max_dd']:.2f}")
        print(f"  IS: net={m_is['net']:+.2f} PF={m_is['pf']} | OOS: net={m_oos['net']:+.2f} PF={m_oos['pf']} "
              f"| retention={verdict[vname]['retention']} | red={red or 'none'}")
        print(f"  costed: base PF={m_all['pf_base_cost']} net={m_all['net_base_cost']:+.2f} | "
              f"3x stress PF={m_all['pf_stress3']} net={m_all['net_stress3']:+.2f}")
        print(f"  replay exit reasons: {dict(exit_reasons[vname])}")

    # ---------- 4. fidelity: V0 replay vs recorded ----------
    if fidelity:
        rec = [a for a, _ in fidelity]; rep = [b for _, b in fidelity]
        diffs = [b - a for a, b in fidelity]
        mad = sum(abs(d) for d in diffs) / len(diffs)
        bias = sum(diffs) / len(diffs)
        # pearson
        ma, mb = sum(rec)/len(rec), sum(rep)/len(rep)
        num = sum((a-ma)*(b-mb) for a, b in fidelity)
        den = math.sqrt(sum((a-ma)**2 for a in rec) * sum((b-mb)**2 for b in rep))
        print(f"\n== FIDELITY (V0 replay vs recorded realized_usd, n={len(fidelity)}) ==")
        print(f"mean abs diff/trade: ${mad:.3f} | bias (replay-recorded): ${bias:+.3f} | pearson r={num/den:.4f}")
        verdict["_fidelity"] = {"n": len(fidelity), "mad": round(mad, 4),
                                "bias": round(bias, 4), "pearson": round(num/den, 4)}

    print(f"\n== MOOTNESS (covered paths, n={moot['n']}) ==")
    print(f"paths ever reaching +50%: {moot['reach_50']} | +100%: {moot['reach_100']} | +200%: {moot['reach_200']}")
    verdict["_mootness"] = moot
    verdict["_coverage"] = {
        "stamped": len(audit), "full_cover": n_full,
        "by_bucket": {b: {"n": len(r), "path": sum(x["path"] for x in r),
                          "full": sum(x["full_cover"] for x in r)}
                      for b, r in by_bucket.items()}}

    json.dump({"variants": verdict,
               "per_trade": {v: results.get(v, []) for v in VARIANTS}},
              open(OUT_JSON, "w"), indent=1)
    print("\nwrote", OUT_JSON)


if __name__ == "__main__":
    main()
