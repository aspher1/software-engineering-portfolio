"""Train and time-validate an offline paper signal model; never wires entries."""

import json
import math
from pathlib import Path

try:
    from . import replay, signal_model
except ImportError:
    import replay
    import signal_model

MODEL_PATH = Path(__file__).with_name("signal_model.json")
FEATURE_NAMES = ["log_liquidity", "buy_sell_ratio", "m15_gain_pct",
                 "hour_sin", "hour_cos", "chain_bsc", "source_gecko"]


def features(trade):
    """Use recorded entry features only; absent volume is not imputed."""
    entry = trade.get("entry_record")
    if not entry or not entry.get("liquidity_usd") or entry.get("signal_ratio") is None or entry.get("signal_gain_pct") is None:
        return None
    hour = int(entry["ts"][11:13])
    return {"log_liquidity": math.log10(float(entry["liquidity_usd"])),
            "buy_sell_ratio": float(entry["signal_ratio"]),
            "m15_gain_pct": float(entry["signal_gain_pct"]),
            "hour_sin": math.sin(2*math.pi*hour/24),
            "hour_cos": math.cos(2*math.pi*hour/24),
            "chain_bsc": int(replay.chain_of(trade) == "bsc"),
            "source_gecko": int(entry.get("source") == "geckoterminal")}


def evaluate(rows, scores, threshold, config, stress=False):
    kept = [trade for trade, score in zip(rows, scores) if score >= threshold]
    baseline = replay.metrics(rows, config, stress)
    filtered = replay.metrics(kept, config, stress)
    baseline_mean = baseline["usd"] / baseline["n"] if baseline["n"] else 0
    filtered_mean = filtered["usd"] / filtered["n"] if filtered["n"] else -math.inf
    return {"baseline": baseline, "filtered": filtered,
            "edge_usd_per_trade": filtered_mean - baseline_mean,
            "coverage": len(kept) / len(rows) if rows else 0,
            "kept": kept}


def concentration_fail(kept, config):
    """Reject apparent gains whose positive P&L comes entirely from one regime."""
    winners = [t for t in kept if replay.net_pnl(t, config)[1] is not None
               and replay.net_pnl(t, config)[1] > 0]
    if len(winners) < 3:
        return True
    for field in ("chain", "liquidity", "gain", "hour_utc"):
        buckets = {replay.attributes(t)[field] for t in winners}
        if len(buckets) < 2:
            return True
    return False


def train(trades, config):
    labeled = [(t, features(t)) for t in trades if features(t) is not None]
    labeled.sort(key=lambda pair: pair[0]["entry_ts"])
    rows = [t for t, _ in labeled]
    vectors = [[f[name] for name in FEATURE_NAMES] for _, f in labeled]
    if len(rows) < 12:
        raise ValueError("too few labeled trades")
    split = round(len(rows) * 2 / 3)
    x_is, means, stds = signal_model.standardize(vectors[:split])
    labels = [int(replay.net_pnl(t, config)[0] > 0) for t in rows[:split]]
    weights, bias = signal_model.fit(x_is, labels, l2=10.0)
    model = {"feature_names": FEATURE_NAMES, "means": means, "stds": stds,
             "weights": weights, "bias": bias, "threshold": .5}
    scorer = signal_model.SignalModel(model)
    scores = [scorer.score(f) for _, f in labeled]
    candidates = [i/100 for i in range(5, 96, 2)]
    choices = [(evaluate(rows[:split], scores[:split], th, config), th) for th in candidates]
    viable = [(result, th) for result, th in choices if result["filtered"]["n"] >= max(3, round(split * .2))]
    if not viable:
        raise ValueError("no viable in-sample threshold")
    is_result, threshold = max(viable, key=lambda x: (x[0]["edge_usd_per_trade"], x[0]["coverage"]))
    model["threshold"] = threshold
    oos = evaluate(rows[split:], scores[split:], threshold, config)
    stress = evaluate(rows[split:], scores[split:], threshold, config, stress=True)
    flags = {
        "no_positive_is_edge": is_result["edge_usd_per_trade"] <= 0,
        "oos_edge_below_60pct_is": oos["edge_usd_per_trade"] <= 0 or oos["edge_usd_per_trade"] < .6 * is_result["edge_usd_per_trade"],
        "oos_win_rate_above_90pct": oos["filtered"]["win_rate"] > .9,
        "single_bucket_regime": concentration_fail(oos["kept"], config),
        "fails_3x_fee_stress": (stress["edge_usd_per_trade"] <= 0 or
                                stress["filtered"]["usd"] <= 0),
    }
    def compact(result):
        return {key: value for key, value in result.items() if key != "kept"}
    model["training"] = {
        "n": len(rows), "is_n": split, "oos_n": len(rows)-split,
        "date_range": [rows[0]["entry_ts"], rows[-1]["entry_ts"]],
        "split": "time-ordered 2/3 IS, 1/3 OOS", "l2": 10.0,
        "label": "positive native P&L after estimated round-trip costs",
        "is": compact(is_result), "oos": compact(oos),
        "oos_3x_slippage": compact(stress), "validation_flags": flags,
        "ship_filter": not any(flags.values())}
    return model


def main():
    trades, counts = replay.load_journal()
    config = json.loads(replay.CONFIG.read_text())
    model = train(trades, config)
    MODEL_PATH.write_text(json.dumps(model, indent=2, allow_nan=False) + "\n")
    print("journal:", counts)
    print(json.dumps(model["training"], indent=2))
    print("threshold:", model["threshold"], "ship_filter:", model["training"]["ship_filter"])


if __name__ == "__main__":
    main()
