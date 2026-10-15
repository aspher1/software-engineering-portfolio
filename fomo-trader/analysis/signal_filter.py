"""Offline paper signal attribution and a fail-open, optional veto evaluator."""

from collections import defaultdict
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import re

try:
    from . import replay
except ImportError:
    import replay

PARAMS_PATH = Path(__file__).with_name("signal_filter.json")
_params_cache = {}
_LINE_TIME = re.compile(r"\[(\d\d:\d\d:\d\d)\]")
_ENTERED = re.compile(r"\[\d\d:\d\d:\d\d\] entered (.*?) @")
_SKIP = re.compile(r"\[\d\d:\d\d:\d\d\] (?:RUG-GUARD SKIP|SKIP|skip) (.*?)(?::| \()")
_START = re.compile(r"\[\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d) (EDT|UTC)\] bot started")


def normalize_name(name):
    return " ".join(str(name).casefold().split())


def _clock(line):
    match = _LINE_TIME.search(line)
    return match.group(1) if match else None


def _date_near(clock, anchor):
    result = datetime.combine(anchor.date(), datetime.strptime(clock, "%H:%M:%S").time())
    if result - anchor > timedelta(hours=12):
        result -= timedelta(days=1)
    elif anchor - result > timedelta(hours=12):
        result += timedelta(days=1)
    return result


def timezone_regimes(log_lines, deaths_lines):
    """Locate launcher timezone changes at matching bot wallet startup lines."""
    wallets = [(i, _clock(line)) for i, line in enumerate(log_lines)
               if "| DRY RUN:" in line and "wallet" in line]
    starts = []
    last_index = -1
    for line in deaths_lines:
        match = _START.search(line)
        if not match:
            continue
        clock, zone = match.groups()
        target = datetime.strptime(clock, "%H:%M:%S")
        candidates = [(i, c) for i, c in wallets if i > last_index and c is not None
                      and abs((datetime.strptime(c, "%H:%M:%S") - target).total_seconds()) <= 2]
        if not candidates:
            continue
        index, _ = candidates[0]
        starts.append((index, 4 if zone == "EDT" else 0))
        last_index = index
    return starts


def join_signals(signals, trades, log_lines, *, window_minutes=30, deaths_lines=None):
    """Assign dates from journal-backed entered lines; match one latest signal per entry.

    Input signals must preserve parse_signals order. Entries without a close may be
    supplied as trades with entry_record and no close_record.
    """
    lines = list(log_lines)
    regimes = timezone_regimes(lines, deaths_lines or [])
    indexed = []
    for index, line in enumerate(lines):
        if replay.SIGNAL.search(line) or replay.PREPUMP.search(line):
            indexed.append(index)
    if len(indexed) != len(signals):
        raise ValueError("signal/log count mismatch")
    entries = [t for t in trades if t.get("entry_record") and t.get("entry_ts")]
    entries.sort(key=lambda t: t["entry_ts"])
    anchors = []
    used = set()
    for index, line in enumerate(lines):
        match = _ENTERED.search(line)
        if not match:
            continue
        name, clock = normalize_name(match.group(1)), _clock(line)
        choices = [(abs((datetime.fromisoformat(t["entry_ts"]) -
                         datetime.combine(datetime.fromisoformat(t["entry_ts"]).date(),
                                          datetime.strptime(clock, "%H:%M:%S").time())).total_seconds()), j)
                   for j, t in enumerate(entries)
                   if j not in used and normalize_name(t.get("name")) == name]
        if choices:
            _, j = min(choices)
            if choices and min(choices)[0] <= 120:
                used.add(j)
                anchors.append((index, datetime.fromisoformat(entries[j]["entry_ts"])))
    result = []
    for signal, index in zip(signals, indexed):
        anchor = min(anchors, key=lambda a: abs(a[0] - index)) if anchors else None
        stamp = _date_near(signal["time"], anchor[1]) if anchor else None
        result.append({"signal": signal, "line_index": index, "signal_ts": stamp,
                       "utc_offset_hours": next((offset for start, offset in reversed(regimes)
                                                 if start <= index), regimes[0][1] if regimes else None),
                       "status": "never-entered", "trade": None})
    for trade in entries:
        stamp = datetime.fromisoformat(trade["entry_ts"])
        candidates = [r for r in result if r["trade"] is None and r["signal_ts"] is not None
                      and normalize_name(r["signal"]["name"]) == normalize_name(trade.get("name"))
                      and r["signal"].get("chain") == replay.chain_of(trade)
                      and timedelta(0) <= stamp - r["signal_ts"] <= timedelta(minutes=window_minutes)]
        if candidates:
            chosen = max(candidates, key=lambda r: (r["signal_ts"], r["line_index"]))
            chosen["trade"] = trade
            chosen["status"] = ("entered-unmatched-close" if not trade.get("close_record") else
                                "entered-winner" if replay.net_pnl(trade)[0] > 0 else "entered-loser")
    skips = []
    for index, line in enumerate(lines):
        match = _SKIP.search(line)
        if match:
            skips.append((index, normalize_name(match.group(1)), _clock(line)))
    for row in result:
        if row["status"] != "never-entered":
            continue
        for index, name, clock in skips:
            if index < row["line_index"] or index - row["line_index"] > 12:
                continue
            if name == normalize_name(row["signal"]["name"]):
                skip_ts = _date_near(clock, row["signal_ts"]) if row["signal_ts"] else None
                if skip_ts is None or timedelta(0) <= skip_ts - row["signal_ts"] <= timedelta(minutes=2):
                    row["status"] = "skipped-by-guard"
                    break
    return result, {"entry_anchors": len(anchors), "timezone_starts": len(regimes),
                    "unanchored_signals": sum(r["signal_ts"] is None for r in result),
                    "unknown_timezone_signals": sum(r["utc_offset_hours"] is None for r in result)}


def signal_values(row):
    signal = row["signal"]
    entry = row["trade"].get("entry_record", {}) if row.get("trade") else {}
    entry = entry or {}
    stamp = row.get("signal_ts")
    return {
        "m15_gain_pct": signal.get("m15_gain_pct"),
        "buy_sell_ratio": signal.get("buy_sell_ratio"),
        "m15_buys": entry.get("m15_buys"), "m15_sells": entry.get("m15_sells"),
        "liquidity_usd": signal.get("liquidity_usd"),
        "m15_volume_usd": entry.get("m15_volume_usd"), "mcap_usd": entry.get("mcap_usd"),
        "source": signal.get("source") or "unknown", "chain": signal.get("chain"),
        "hour_utc": ((stamp.hour + row["utc_offset_hours"]) % 24
                     if stamp and row.get("utc_offset_hours") is not None else None),
        "early": bool(signal.get("early")),
        "slip_from_signal_pct": entry.get("slip_from_signal_pct"),
        "entry_latency_ms": entry.get("entry_latency_ms"),
    }


BUCKETS = {
    "m15_gain_pct": ([100, 200, 400, math.inf], ["<100", "100-200", "200-400", ">=400"]),
    "buy_sell_ratio": ([2, 4, math.inf], ["<2", "2-4", ">=4"]),
    "m15_buys": ([25, 75, math.inf], ["<25", "25-74", ">=75"]),
    "m15_sells": ([10, 30, math.inf], ["<10", "10-29", ">=30"]),
    "liquidity_usd": ([10000, 25000, 50000, math.inf], ["<10k", "10-25k", "25-50k", ">=50k"]),
    "m15_volume_usd": ([10000, 50000, math.inf], ["<10k", "10-50k", ">=50k"]),
    "mcap_usd": ([100000, 500000, math.inf], ["<100k", "100-500k", ">=500k"]),
    "slip_from_signal_pct": ([0, .1, math.inf], ["<0", "0-10%", ">=10%"]),
    "entry_latency_ms": ([10000, 30000, math.inf], ["<10s", "10-30s", ">=30s"]),
}


def feature_bucket(field, value):
    if value is None:
        return "missing"
    if field == "hour_utc":
        return ("00-05" if value < 6 else "06-11" if value < 12 else
                "12-17" if value < 18 else "18-23")
    if field == "early":
        return "pre-pump" if value else "FOMO"
    if field in BUCKETS:
        edges, labels = BUCKETS[field]
        return replay.bucket(value, edges, labels)
    return str(value)


def attribution_rows(rows, config):
    fields = list(BUCKETS) + ["source", "hour_utc", "chain", "early"]
    output = {}
    for field in fields:
        groups = defaultdict(list)
        for row in rows:
            groups[feature_bucket(field, signal_values(row)[field])].append(row)
        output[field] = {}
        for label, members in sorted(groups.items()):
            entered = [r["trade"] for r in members if r["status"] in ("entered-winner", "entered-loser")]
            output[field][label] = {"entered": len(entered), "never": sum(r["status"] in ("never-entered", "skipped-by-guard") for r in members),
                                    "metrics": replay.metrics(entered, config)}
    return output


def validate_ratio_veto(rows, config, ceiling=4.0):
    """Evaluate an IS-selected ratio veto on entry-ordered closed paper trades."""
    # Journal entry strings mix EDT and UTC; normalize each before sorting.
    closed = sorted((r for r in rows if r.get("trade") and r["trade"].get("close_record")),
                    key=lambda r: (datetime.fromisoformat(r["trade"]["entry_ts"]) +
                                   timedelta(hours=r.get("utc_offset_hours") or 0),
                                   r["line_index"]))
    split = round(len(closed) * 2 / 3)

    def evaluate(part, stress):
        baseline = replay.metrics([r["trade"] for r in part], config, stress)
        kept = [r["trade"] for r in part if r["signal"].get("buy_sell_ratio") is None
                or r["signal"]["buy_sell_ratio"] < ceiling]
        filtered = replay.metrics(kept, config, stress)
        edge = (filtered["usd"] / filtered["n"] - baseline["usd"] / baseline["n"]
                if filtered["n"] and baseline["n"] else 0.0)
        return {"baseline": baseline, "filtered": filtered, "edge_usd_per_trade": edge}

    return {"is": evaluate(closed[:split], False),
            "oos": evaluate(closed[split:], False),
            "is_3x_slippage": evaluate(closed[:split], True),
            "oos_3x_slippage": evaluate(closed[split:], True)}


def load_params(path=PARAMS_PATH):
    path = Path(path)
    if path not in _params_cache:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("thresholds"), dict) or not isinstance(data.get("ship_recommend"), bool):
            raise ValueError("invalid filter parameters")
        for field, limit in data["thresholds"].items():
            if field not in BUCKETS or not isinstance(limit, (int, float)) or not math.isfinite(limit):
                raise ValueError("invalid filter threshold")
        _params_cache[path] = data
    return _params_cache[path]


def approve(signal, *, path=PARAMS_PATH, log=print):
    """Fail open on absent/corrupt parameters or malformed input; never raise."""
    try:
        params = load_params(path)
        if not isinstance(signal, dict):
            raise TypeError("signal must be a dict")
        if not params["ship_recommend"]:
            return True
        for field, ceiling in params["thresholds"].items():
            value = signal.get(field)
            if value is not None and float(value) >= ceiling:
                return False
        return True
    except Exception:
        try:
            log("SIGNAL FILTER FALLBACK")
        except Exception:
            pass
        return True
