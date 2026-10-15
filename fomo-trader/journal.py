"""Research instrumentation journaling for FOMO Trader.

Additive only: records scanner candidate observations (including signals that
never become entries) and enriches entry journal records with the fields the
deep-research pass needs (pool age, strategy version, slip/liquidity/gain).

HARD RULES for this module:
  - Never raises. Every public function catches its own exceptions and
    degrades to None / no-op. A journaling failure must never block or alter
    a trade decision.
  - Never mutates its inputs. Signal dicts are read, never written.
  - No network, no subprocess, no randomness. Pure local bookkeeping.

STRATEGY_VERSION: bump ONLY when strategy behavior changes (entry logic,
exit logic, guardrails, sizing, vetoes). Instrumentation-only changes do
NOT bump it. Current version "1" covers the bot as of 2026-09-28, including
the positive-slip chase veto (user-ordered 2026-09-28) which IS a behavior
change from the pre-veto strategy.
"""
import json
import math
import os
import threading
import time
from datetime import datetime

# Bump only on strategy-behavior changes (see module docstring).
STRATEGY_VERSION = "1"

CANDIDATES_FILE = "candidates.jsonl"
# Single-file rotation guard: candidates.jsonl is append-only and could grow
# unbounded over months. Rotate once past this size; keep one backup.
CANDIDATES_MAX_BYTES = 50 * 1024 * 1024

_lock = threading.Lock()


def _num(x):
    """Finite float or None. Never raises."""
    try:
        if x is None or isinstance(x, bool):
            return None
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError, OverflowError):
        return None


def pool_created_at(payload):
    """Pool creation timestamp (seconds since epoch) from a raw pool payload.

    DexScreener reports ``pairCreatedAt`` in *milliseconds*; some payloads
    use seconds; GeckoTerminal uses ISO-8601 strings. Values > 1e12 are
    treated as ms and divided by 1000. Unparseable values return None.
    """
    try:
        p = payload or {}
        for key in ("pairCreatedAt", "pool_created_at", "poolCreatedAt",
                    "created_at", "createdAt"):
            raw = p.get(key)
            v = _num(raw)
            if v is None and isinstance(raw, str):
                try:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if dt.tzinfo is not None:
                        v = dt.timestamp()
                except (ValueError, OverflowError):
                    pass
            if v is None:
                continue
            if v > 1e12:  # milliseconds -> seconds
                v = v / 1000.0
            return v if v > 0 else None
        return None
    except Exception:
        return None


def _pick(signal, *keys):
    for k in keys:
        v = _num(signal.get(k))
        if v is not None:
            return v
    return None


class ObservationTracker:
    """In-memory first/second observation tracking per (chain, mint).

    The deep pass needs "second-observation prices" to test persistence
    confirmation (did the signal survive to the next scan?). This keeps the
    first observation per pool in memory and reports the repeat. Bounded:
    oldest entries are evicted past max_entries. Never raises.
    """

    def __init__(self, max_entries=5000):
        self._max = max(100, int(max_entries or 5000))
        self._seen = {}
        self._lock = threading.Lock()

    def _key(self, signal):
        try:
            return (str(signal.get("chain") or "?"),
                    str(signal.get("mint") or signal.get("pool") or "?"))
        except Exception:
            return ("?", "?")

    def observe(self, signal):
        """Record an observation. Returns repeat info or None.

        First sighting -> None. Repeat sighting -> {"first_ts": float,
        "second_price_usd": float|None, "sightings": int}. Never raises,
        never mutates the signal.
        """
        try:
            key = self._key(signal)
            now = time.time()
            price = _num((signal or {}).get("signal_price_usd"))
            with self._lock:
                rec = self._seen.get(key)
                if rec is None:
                    if len(self._seen) >= self._max:
                        # evict oldest first-seen entry
                        oldest = min(self._seen,
                                     key=lambda k: self._seen[k]["first_ts"])
                        del self._seen[oldest]
                    self._seen[key] = {"first_ts": now, "sightings": 1}
                    return None
                rec["sightings"] += 1
                return {"first_ts": rec["first_ts"],
                        "second_price_usd": price,
                        "sightings": rec["sightings"]}
        except Exception:
            return None


def sanitize_observation(signal, verdict=None, repeat_info=None):
    """Build a candidate-observation record from a scanner signal.

    Covers signals that never become entries (verdict "guardrail_skip") as
    well as ones that do ("enter"). Never raises; unknown fields -> None;
    never mutates the input signal.
    """
    try:
        s = signal or {}
        return {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ts_epoch": time.time(),
            "event": "candidate",
            "strategy_version": STRATEGY_VERSION,
            "name": s.get("name"),
            "mint": s.get("mint"),
            "pool": s.get("pool"),
            "chain": s.get("chain"),
            "price_usd": _num(s.get("signal_price_usd")),
            "liquidity_usd": _num(s.get("liquidity_usd")),
            "m15_volume_usd": _num(s.get("m15_volume_usd")),
            "m15_gain_pct": _num(s.get("m15_gain_pct")),
            "m5_gain_pct": _num(s.get("m5_gain_pct")),
            "m15_buys": s.get("m15_buys"),
            "m15_sells": s.get("m15_sells"),
            "buy_sell_ratio": _num(s.get("buy_sell_ratio")),
            "mcap_usd": _num(s.get("mcap_usd")),
            "pool_created_at": pool_created_at(s),
            "source": s.get("source"),
            "window": s.get("window_label"),
            "early": bool(s.get("early")),
            "guardrail_verdict": verdict,
            "repeat": repeat_info,
        }
    except Exception:
        return {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "event": "candidate",
                "strategy_version": STRATEGY_VERSION,
                "journal_error": True}


def sanitize_early_accel_reject(obs):
    """Build an early_accel_reject event record.

    The Hunter's early (pre-peak, 5m-window) path rejects pools whose m15
    gain is not larger than accel_ratio * m5 gain (fomo_trader.py, the
    "not accelerating: move is stalling" gate). The deep-improvement loop
    (iter-7) found that gate inverted vs its documented accelerating
    intent: it admits stalling moves and rejects fresh accelerations, the
    exact cohort the early path exists to catch.

    These rejected pools never surface as candidates, so the corrected
    orientation cannot be backtested from existing rows. Journaling them
    here (additive, zero strategy-behavior change) makes the
    corrected-admission set observable for a prospective Track B gate.
    Never raises; never mutates its input.
    """
    try:
        o = obs or {}
        p5 = _num(o.get("m5_gain_pct"))
        p15 = _num(o.get("m15_gain_pct"))
        accel_ratio = _num(o.get("accel_ratio"))
        decel_ratio = None
        try:
            if p5 and accel_ratio and p5 * accel_ratio:
                decel_ratio = p15 / (p5 * accel_ratio) if p15 else None
        except (TypeError, ValueError, ZeroDivisionError):
            decel_ratio = None
        return {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ts_epoch": time.time(),
            "event": "early_accel_reject",
            "strategy_version": STRATEGY_VERSION,
            "name": o.get("name"),
            "mint": o.get("mint"),
            "pool": o.get("pool"),
            "chain": o.get("chain"),
            "price_usd": _num(o.get("signal_price_usd")),
            "liquidity_usd": _num(o.get("liquidity_usd")),
            "m15_volume_usd": _num(o.get("m15_volume_usd")),
            "m15_gain_pct": p15,
            "m5_gain_pct": p5,
            "m15_buys": o.get("m15_buys"),
            "m15_sells": o.get("m15_sells"),
            "buy_sell_ratio": _num(o.get("buy_sell_ratio")),
            "mcap_usd": _num(o.get("mcap_usd")),
            "pool_created_at": pool_created_at(o),
            "source": o.get("source"),
            "window": "5m",
            "early": True,
            "gate": "accel",
            "accel_ratio": accel_ratio,
            "decel_ratio": decel_ratio,
        }
    except Exception:
        return {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "event": "early_accel_reject",
                "strategy_version": STRATEGY_VERSION,
                "journal_error": True}


def sanitize_veto(signal, entry_native, native_usd, slip, chain):
    """Build a slip_veto event record for a vetoed chase entry.

    Captures the full would-be entry details so the improvement loop can
    retrospectively study what vetoed entries would have done. Never raises.
    """
    try:
        s = signal or {}
        entry_usd = None
        try:
            if entry_native and native_usd:
                entry_usd = float(entry_native) * float(native_usd)
                if not math.isfinite(entry_usd):
                    entry_usd = None
        except (TypeError, ValueError, OverflowError):
            entry_usd = None
        return {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ts_epoch": time.time(),
            "event": "slip_veto",
            "strategy_version": STRATEGY_VERSION,
            "name": s.get("name"),
            "mint": s.get("mint"),
            "pool": s.get("pool"),
            "chain": chain or s.get("chain"),
            "signal_price_usd": _num(s.get("signal_price_usd")),
            "would_be_entry_native": _num(entry_native),
            "would_be_entry_usd": entry_usd,
            "slip_from_signal": _num(slip),
            "liquidity_usd": _num(s.get("liquidity_usd")),
            "signal_gain_pct": _num(s.get("m15_gain_pct")),
            "m15_buys": s.get("m15_buys"),
            "m15_sells": s.get("m15_sells"),
            "buy_sell_ratio": _num(s.get("buy_sell_ratio")),
            "pool_created_at": pool_created_at(s),
            "source": s.get("source"),
            "window": s.get("window_label"),
        }
    except Exception:
        return {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "event": "slip_veto",
                "strategy_version": STRATEGY_VERSION,
                "journal_error": True}


def lp_lock_fields(status):
    """{"lp_lock_verdict", "lp_lock_burn_pct"} from a
    BscSwap.lp_lock_status() result; burn pct is None unless measured.
    Distinct from the scanner-sourced "lp_burn_pct", which it never touches.
    Never raises."""
    try:
        st = status if isinstance(status, dict) else {}
        verdict = st.get("verdict")
        return {"lp_lock_verdict": verdict if isinstance(verdict, str)
                else "unknown",
                "lp_lock_burn_pct": _num(st.get("burn_pct"))}
    except Exception:
        return {"lp_lock_verdict": "unknown", "lp_lock_burn_pct": None}


def sanitize_lp_lock_skip(signal, status):
    """Build an lp_lock_skip event record for an entry skipped because the
    pair's LP is not burned. Never raises."""
    try:
        s = signal or {}
        st = status if isinstance(status, dict) else {}
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ts_epoch": time.time(),
            "event": "lp_lock_skip",
            "strategy_version": STRATEGY_VERSION,
            "name": s.get("name"),
            "mint": s.get("mint"),
            "pool": s.get("pool"),
            "chain": "bsc",
            "lp_pair": st.get("pair"),
            "signal_price_usd": _num(s.get("signal_price_usd")),
            "liquidity_usd": _num(s.get("liquidity_usd")),
            "signal_gain_pct": _num(s.get("m15_gain_pct")),
            "pool_created_at": pool_created_at(s),
            "source": s.get("source"),
            "window": s.get("window_label"),
        }
        rec.update(lp_lock_fields(st))
        return rec
    except Exception:
        return {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "event": "lp_lock_skip",
                "strategy_version": STRATEGY_VERSION,
                "journal_error": True}


def finalize_entry_record(rec, signal):
    """Add instrumentation fields to an entry journal record.

    Adds strategy_version + pool_created_at, and gap-fills the three fields
    the research pass needs on every entry (slip signed, liquidity,
    signal gain) from the signal when the record lacks them. Returns the
    same dict (mutated in place is fine for journal records under
    construction); never raises.
    """
    try:
        rec = rec if isinstance(rec, dict) else {}
        s = signal or {}
        rec.setdefault("strategy_version", STRATEGY_VERSION)
        if rec.get("pool_created_at") is None:
            rec["pool_created_at"] = pool_created_at(s)
        if rec.get("signal_gain_pct") is None:
            rec["signal_gain_pct"] = _num(s.get("m15_gain_pct"))
        if rec.get("liquidity_usd") is None:
            rec["liquidity_usd"] = _num(s.get("liquidity_usd"))
        # slip may live under either key; fill whichever is missing
        if rec.get("slip_from_signal_pct") is None and rec.get("slip_from_signal") is None:
            rec["slip_from_signal_pct"] = _num(s.get("slip_from_signal_pct"))
        return rec
    except Exception:
        try:
            return rec if isinstance(rec, dict) else {}
        except Exception:
            return {}


def journal_event(basedir, event):
    """Append one event dict to candidates.jsonl under basedir.

    Rotates the file (one backup) past CANDIDATES_MAX_BYTES. Never raises.
    """
    try:
        if not isinstance(event, dict):
            return
        path = os.path.join(basedir, CANDIDATES_FILE)
        with _lock:
            try:
                if (os.path.exists(path)
                        and os.path.getsize(path) > CANDIDATES_MAX_BYTES):
                    bak = path + ".1"
                    try:
                        if os.path.exists(bak):
                            os.remove(bak)
                    except OSError:
                        pass
                    try:
                        os.rename(path, bak)
                    except OSError:
                        pass
            except OSError:
                pass
            try:
                with open(path, "a") as f:
                    f.write(json.dumps(event) + "\n")
            except OSError:
                pass
    except Exception:
        pass
