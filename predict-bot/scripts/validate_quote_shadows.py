"""Read-only chronological replay of quote gate and model feature shadows."""

import json
import math
import random
import statistics

from quote_side_analysis import load_jsonl, paper_fill
from same_day_loop import executable_edge_shadow


def chronological_rows():
    grades = [g for g in load_jsonl("data/signal_grades.jsonl") if g.get("proxy") == "kalshi"]
    exits = {e["signal_key"]: e for e in load_jsonl("data/exit_events.jsonl")}
    grades.sort(key=lambda g: (g.get("window_close_utc") or 0, g.get("key") or ""))
    for g in grades:
        g["_fill"] = paper_fill(g, exits.get(g.get("key")))
    return grades


def gate_report(grades):
    rows = [g for g in grades if g["_fill"]["executable_pnl_net"] is not None
            and isinstance(g.get("edge"), (int, float))]
    for g in rows:
        g["_gate"] = executable_edge_shadow(g["edge"], g["bid"], g["ask"])["gate_executable_edge_pass"]
    # Four chronological folds: earliest 40% is an untouched calibration
    # period, next three 20% blocks are forward tests of a fixed rule.
    n = len(rows)
    folds = [(int(n * a), int(n * b)) for a, b in ((.4, .6), (.6, .8), (.8, 1))]
    rng = random.Random(104729)
    results = []
    pooled_placebos = [0.0] * 2000
    pooled_observed = 0.0
    pooled_n = 0
    for lo, hi in folds:
        block = rows[lo:hi]
        kept = [g for g in block if g["_gate"]]
        if not kept:
            continue
        def pnl(g):
            return g["_fill"]["executable_pnl_net"]
        # Probability matched placebo, using taken-side market probability
        # deciles. Random samples are without replacement within each bin.
        bins = lambda g: min(9, int((g["market_mid"] if g["side"] == "YES" else 1-g["market_mid"]) * 10))
        counts = {b: sum(bins(g) == b for g in kept) for b in range(10)}
        pools = {b: [g for g in block if bins(g) == b] for b in range(10)}
        observed = statistics.mean(map(pnl, kept))
        placebos = [statistics.mean(pnl(g) for b, count in counts.items()
                    for g in rng.sample(pools[b], count)) for _ in range(2000)]
        pooled_observed += observed * len(kept)
        pooled_n += len(kept)
        pooled_placebos = [total + value * len(kept)
                           for total, value in zip(pooled_placebos, placebos)]
        results.append({"n_all": len(block), "n_kept": len(kept),
            "mean_all": round(statistics.mean(map(pnl, block)), 6),
            "mean_kept": round(observed, 6),
            "mean_probability_matched_placebo": round(statistics.mean(placebos), 6),
            "delta_vs_placebo": round(observed-statistics.mean(placebos), 6),
            "placebo_p025": round(sorted(placebos)[49], 6),
            "placebo_p975": round(sorted(placebos)[1949], 6),
            "p_one_sided": round(sum(p >= observed for p in placebos)/len(placebos), 4),
            "close_utc_first": block[0]["window_close_utc"],
            "close_utc_last": block[-1]["window_close_utc"]})
    if pooled_n:
        observed = pooled_observed / pooled_n
        placebo = sorted(value / pooled_n for value in pooled_placebos)
        combined = {"n_kept": pooled_n, "mean_kept": round(observed, 6),
                    "mean_probability_matched_placebo": round(statistics.mean(placebo), 6),
                    "delta_vs_placebo": round(observed-statistics.mean(placebo), 6),
                    "placebo_p025": round(placebo[49], 6),
                    "placebo_p975": round(placebo[1949], 6),
                    "p_one_sided": round(sum(p >= observed for p in placebo)/len(placebo), 4)}
    else:
        combined = None
    return {"n_eligible": n, "folds": results, "combined": combined}


def model_report(grades):
    """Fixed, shrunk train-bin residual corrections, evaluated forward."""
    features = {
        "book_sign": lambda g, train: (g["ob_imb_at_entry"] > 0) if g.get("ob_imb_at_entry") is not None else None,
        "book_alignment": lambda g, train: ((g["ob_imb_at_entry"] > 0) == (g["side"] == "YES")) if g.get("ob_imb_at_entry") is not None else None,
        "sigma_regime": lambda g, train: (g["sigma"] >= train) if g.get("sigma") is not None else None,
        "jump_flag": lambda g, train: g.get("jump_flag"),
        "jump_shrink_half": lambda g, train: g.get("jump_flag"),
    }
    out = {}
    for name, feature in features.items():
        rows = [g for g in grades if g.get("market_mid") is not None
                and g.get("edge") is not None and feature(g, 0) is not None]
        n = len(rows)
        folds = []
        for a, b in ((.4, .6), (.6, .8), (.8, 1)):
            train, test = rows[:int(n*a)], rows[int(n*a):int(n*b)]
            if not train or not test:
                continue
            threshold = statistics.median(g["sigma"] for g in train) if name == "sigma_regime" else 0
            base = lambda g: max(.001, min(.999, g["market_mid"] + g["edge"]))
            groups = {False: [], True: []}
            for g in train:
                groups[bool(feature(g, threshold))].append((g["actual"] == "YES") - base(g))
            # No tunable feature coefficient: half-strength group mean,
            # with 50 pseudo-observations at zero residual per group.
            correction = {v: .5 * sum(vals)/(len(vals)+50) for v, vals in groups.items()}
            old, new, bins = [], [], {i: [[], []] for i in range(5)}
            for g in test:
                y = float(g["actual"] == "YES")
                p = base(g)
                if name == "jump_shrink_half":
                    q = max(.001, min(.999, g["market_mid"] +
                                             (.5 if g["jump_flag"] else 1) * g["edge"]))
                else:
                    q = max(.001, min(.999, p + correction[bool(feature(g, threshold))]))
                old.append((p-y)**2); new.append((q-y)**2)
                idx = min(4, int(p*5))
                bins[idx][0].append((p-y)**2); bins[idx][1].append((q-y)**2)
            folds.append({"n": len(test), "brier_base": round(statistics.mean(old), 6),
                "brier_candidate": round(statistics.mean(new), 6),
                "improvement": round(statistics.mean(old)-statistics.mean(new), 6),
                "bins_degraded": [i for i, (o, c) in bins.items() if o and statistics.mean(c)>statistics.mean(o)],
                "close_utc_first": test[0]["window_close_utc"],
                "close_utc_last": test[-1]["window_close_utc"]})
        out[name] = {"n_eligible": n, "folds": folds,
                     "oos_improvement_weighted": round(sum(f["improvement"]*f["n"] for f in folds)/sum(f["n"] for f in folds), 6) if folds else None}
    return out


if __name__ == "__main__":
    grades = chronological_rows()
    print(json.dumps({"gate": gate_report(grades), "model": model_report(grades)}, indent=2))
