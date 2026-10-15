"""Offline wash-like flow proxy for paper signals; disabled unless validated.

Aggregate counts cannot identify identical-amount trades or individual wallets.
"""

import json
import math
import threading
from collections import Counter
from pathlib import Path


PARAMS_PATH = Path(__file__).with_name("w4_wash_detect.json")
_params_cache = {}

# Fallback accounting: every silent-approve path below increments one of these
# under _fallback_lock, so a data regression or corrupt params file can never
# silently disable an enabled veto without leaving a metric. The disabled state
# (ship_recommend=false) is a normal configuration, not a fallback, and is not
# counted. Lock is only taken on fallback paths, never on the happy path.
_fallback_lock = threading.Lock()
_fallback_counts = Counter()


def fallback_counts():
    """Snapshot of fallback-approve counts by reason (thread-safe)."""
    with _fallback_lock:
        return dict(_fallback_counts)


def reset_fallback_counts():
    """Clear fallback counters (tests only)."""
    with _fallback_lock:
        _fallback_counts.clear()


def _fallback(reason, log):
    with _fallback_lock:
        _fallback_counts[reason] += 1
    if log is not None:
        try:
            log("WASH FALLBACK %s" % reason)
        except Exception:
            pass
    return True


def _count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid trade count")
    if not math.isfinite(value) or value < 0 or int(value) != value:
        raise ValueError("invalid trade count")
    return int(value)


def wash_proxy(buys, sells):
    """Return matched aggregate counts / absolute count imbalance."""
    buys, sells = _count(buys), _count(sells)
    return min(buys, sells) / max(1, abs(buys - sells))


def load_params(path=PARAMS_PATH):
    """Validate the complete parameter schema; invalid files raise ValueError.

    Note: the check-then-set on _params_cache is intentionally lock-free. The
    bot calls approve() from per-signal entry threads; under CPython's GIL the
    dict assignment is atomic and writes are idempotent, so the worst case is a
    redundant file parse. No torn state is possible.
    """
    path = Path(path)
    if path in _params_cache:
        return _params_cache[path]
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or set(data) != {"ship_recommend", "thresholds"}
            or type(data["ship_recommend"]) is not bool
            or not isinstance(data["thresholds"], dict)
            or set(data["thresholds"]) != {"solana", "bsc"}):
        raise ValueError("invalid wash parameters")
    for chain in ("solana", "bsc"):
        limits = data["thresholds"][chain]
        if not isinstance(limits, dict) or set(limits) != {"wash_proxy", "min_volume_usd"}:
            raise ValueError("invalid wash thresholds")
        for key in ("wash_proxy", "min_volume_usd"):
            value = limits[key]
            if type(value) is not float or not math.isfinite(value) or value < 0:
                raise ValueError("invalid wash threshold")
        if data["ship_recommend"] and limits["wash_proxy"] <= 0:
            raise ValueError("active wash threshold must be positive")
    _params_cache[path] = data
    return data


def approve(signal, *, path=PARAMS_PATH, log=None):
    """Fail open for missing evidence, malformed signals, or bad parameters.

    Every fail-open path is counted in fallback_counts() and, when a log
    callable is supplied, emitted as a "WASH FALLBACK <reason>" line. Never
    raises. Lock-free on the happy path; the lock is taken only on fallbacks.
    """
    try:
        try:
            params = load_params(path)
        except Exception:
            return _fallback("bad_params", log)
        if not isinstance(signal, dict):
            return _fallback("non_dict_signal", log)
        if not params["ship_recommend"]:
            return True
        try:
            limits = params["thresholds"].get(signal.get("chain"))
        except Exception:
            return _fallback("unhashable_chain", log)
        if limits is None:
            return _fallback("unknown_chain", log)
        buys = signal.get("m15_buys")
        sells = signal.get("m15_sells")
        volume = signal.get("m15_volume_usd")
        if buys is None or sells is None or volume is None:
            return _fallback("missing_evidence", log)
        if isinstance(volume, bool) or not isinstance(volume, (int, float)):
            return _fallback("bad_volume", log)
        if not math.isfinite(volume) or volume < 0:
            return _fallback("bad_volume", log)
        try:
            score = wash_proxy(buys, sells)
        except ValueError:
            return _fallback("bad_counts", log)
        return not (volume >= limits["min_volume_usd"] and score >= limits["wash_proxy"])
    except Exception:
        return _fallback("exception", log)
