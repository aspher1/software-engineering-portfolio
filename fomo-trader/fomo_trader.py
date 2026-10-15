#!/usr/bin/env python3
"""FOMO Trader - end-to-end automated memecoin system (Solana).

ONLY trades FOMO setups: violent buy-pressure pumps (huge 15m gain with
buys crushing sells, like NPC/POOPFUN). No signal, no trade.

Loop:
  1. HUNTER scans GeckoTerminal trending/new pools, applies the FOMO filter.
  2. On a fresh signal (guardrails pass) it buys via Jupiter with your wallet.
  3. MANAGER watches each position on a tight loop: take-profit ladder +
     trailing stop + hard stop. First trigger wins, sells immediately.

SAFETY DEFAULTS: dry_run=true (paper-trades everything, sends nothing).
Your keypair stays on YOUR machine, read from a local file. Never shared.

    python fomo_trader.py --config config.json          # live loop
    python fomo_trader.py --config config.json --hunt   # hunter scan only (read-only)

Deps: pip install solders requests
"""
import argparse
import base64
import copy
import faulthandler
import http.client
import json
import math
import os
import queue
import random
import signal
import socket
import sys
import threading
import time
import traceback
import requests
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

import allocator
import journal
import money
from shadow_dump import evaluate as evaluate_shadow_dump

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

SOL_MINT = "So11111111111111111111111111111111111111112"
# BSC quote vehicles, using GeckoTerminal's own token ids (verified via
# /networks/bsc/tokens/): WBNB, USDT, and native BNB.
BSC_QUOTES = {
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # Wrapped BNB
    "0x55d398326f99059ff775485246999027b3197955",  # USDT
    "0x0000000000000000000000000000000000000000",  # native BNB
}
# per-chain hunter setup: pages scanned and which quote tokens count as the
# chain's native trading vehicle (memecoin standard pairs only)
CHAINS = {
    "solana": {"pages_trending": 3, "pages_new": 4,
               "quotes": {SOL_MINT.lower()}, "quote_label": "SOL"},
    "bsc": {"pages_trending": 2, "pages_new": 3,
            "quotes": BSC_QUOTES, "quote_label": "WBNB"},
}
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
GT_BASE = "https://api.geckoterminal.com/api/v2"
JUP_BASE = "https://lite-api.jup.ag/swap/v1"  # free tier
# The bot's calendar day (kill switch, daily trade cap, day-start bankroll
# snapshot) is pinned to one fixed tz. Host-local time is unsafe: the host TZ
# has flipped across restarts, which changes the day string and silently
# resets trades_today / realized_* mid-session, disarming the daily caps.
# A state.json written with the old host-local day string self-heals on the
# next roll check (at most one benign extra roll); no migration needed.
DAY_TZ = ZoneInfo("America/New_York")  # fixed bot-day tz; NEVER host-local


def _bot_today_str():
    return datetime.now(DAY_TZ).strftime("%Y-%m-%d")


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def http_get(url, timeout=20):
    # requests (urllib3) rather than urllib: DexScreener's edge drops
    # connections in a way urllib surfaces as IncompleteRead(0 bytes),
    # while curl/urllib3 handle it fine. Verified 2026-09-24.
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def http_post(url, payload, timeout=30):
    r = requests.post(url, json=payload,
                      headers={"Content-Type": "application/json",
                               "User-Agent": UA},
                      timeout=timeout)
    r.raise_for_status()
    return r.json()


def to_f(x):
    try:
        value = float(x)
        return value if math.isfinite(value) else 0.0
    except (TypeError, ValueError):
        return 0.0


def optional_positive_float(value):
    """Return a usable observed price, or None without inventing a zero."""
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def lp_evidence(payload):
    """Only copy explicit LP fields from an already fetched pool payload."""
    burn = payload.get("lp_burn_pct")
    locked = payload.get("lp_locked")
    try:
        burn = float(burn) if burn is not None else None
        if burn is not None and not (math.isfinite(burn) and 0 <= burn <= 100):
            burn = None
    except (TypeError, ValueError):
        burn = None
    return burn, locked if isinstance(locked, bool) else None


def entry_evidence(signal, entry, native_usd, holder=None, commit_ts=None):
    """Assemble already available entry observations; no I/O."""
    now = time.time() if commit_ts is None else commit_ts
    signal_usd = optional_positive_float(signal.get("signal_price_usd"))
    rate = optional_positive_float(native_usd)
    commit_usd = entry * rate if entry is not None and rate is not None else None
    if commit_usd is not None and not math.isfinite(commit_usd):
        commit_usd = None
    try:
        latency = max(0, round((now - float(signal["ts"])) * 1000))
    except (KeyError, TypeError, ValueError, OverflowError):
        latency = None
    return {
        "signal_price_usd": signal_usd,
        "commit_price_native": entry,
        "commit_price_usd": commit_usd,
        "slip_from_signal_pct": (commit_usd / signal_usd - 1
                                 if commit_usd is not None and signal_usd else None),
        "holder_top1_pct": (holder or {}).get("holder_top1_pct"),
        "holder_top5_pct": (holder or {}).get("holder_top5_pct"),
        "lp_burn_pct": signal.get("lp_burn_pct"),
        "lp_locked": signal.get("lp_locked"),
        "liquidity_usd": signal.get("liquidity_usd"),
        "entry_latency_ms": latency,
        "m15_buys": signal.get("m15_buys"),
        "m15_sells": signal.get("m15_sells"),
        "m15_volume_usd": signal.get("m15_volume_usd"),
        "mcap_usd": signal.get("mcap_usd"),
    }


def wipe_drift_cap_native(signal, entry_native, native_usd, cfg_buy, buy_native):
    """Shrink a positive-drift ticket to the allocator's 0.5x floor."""
    try:
        sig_usd = float(signal.get("signal_price_usd") or 0)
    except (TypeError, ValueError, OverflowError):
        sig_usd = 0.0
    slip = (entry_native * native_usd / sig_usd - 1
            if sig_usd > 0 and entry_native and native_usd else None)
    if slip is not None and slip > 0:
        floor_native = (cfg_buy or 0) * 0.5
        if floor_native > 0 and buy_native > floor_native:
            return floor_native, True, slip
    return buy_native, False, slip


def slip_from_signal(signal, entry_native, native_usd):
    """Signed fractional slip of the entry fill vs the scanner signal price.

    (entry_native * native_usd / signal_price_usd - 1); > 0 means we are
    buying above the signal print (chasing the top). Returns None when the
    slip cannot be measured (missing signal price or rate). Pure, no I/O.
    """
    try:
        sig_usd = float(signal.get("signal_price_usd") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        if sig_usd > 0 and entry_native and native_usd:
            slip = float(entry_native) * float(native_usd) / sig_usd - 1
            return slip if math.isfinite(slip) else None
    except (TypeError, ValueError, OverflowError):
        return None
    return None


RESEARCH_OFF = (False, None, False)
CAPTURE_OFF = (False, False)
QUOTE_PATH_MAX_TICKS = 5000
QUOTE_PATH_MAX_BYTES = 1024 * 1024
_failed_quotes_lock = threading.Lock()
_quote_paths_lock = threading.Lock()
_quote_path_counts = {}
RESEARCH_QUESTIONS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "research_questions.json")
_research_warned = set()
_research_registry = {"key": None, "ids": frozenset()}


def _research_warn(key, msg):
    """Log a research-mode fallback once per distinct cause."""
    if key not in _research_warned:
        _research_warned.add(key)
        log(msg)


def research_settings(cfg):
    """(tag_trades, question_id, dashboard) from the optional `research`
    config section. A missing section or enabled != true is all-off; any
    malformed value fails safe to off with a one-time log line. Never raises."""
    try:
        sec = cfg.get("research") if isinstance(cfg, dict) else None
        if sec is None:
            return RESEARCH_OFF
        if not isinstance(sec, dict):
            _research_warn(("research", type(sec).__name__),
                           "RESEARCH config ignored: `research` is %s, not an "
                           "object; observation mode off" % type(sec).__name__)
            return RESEARCH_OFF
        flags = {}
        for key in ("enabled", "tag_trades", "dashboard"):
            value = sec.get(key)
            if value is not None and not isinstance(value, bool):
                shown = repr(value)[:60]
                _research_warn((key, shown),
                               "RESEARCH config: %s=%s is not true/false; "
                               "treated as false" % (key, shown))
            flags[key] = value is True
            if key == "enabled" and not flags[key]:
                return RESEARCH_OFF
        qid = sec.get("question_id")
        if isinstance(qid, str) and qid.strip():
            qid = qid.strip()
        else:
            if qid is not None:
                shown = repr(qid)[:60]
                _research_warn(("question_id", shown),
                               "RESEARCH config: question_id=%s is not a "
                               "non-empty string; ignored" % shown)
            qid = None
        return (flags["tag_trades"], qid, flags["dashboard"])
    except Exception as e:
        _research_warn(("research", "error", type(e).__name__),
                       "RESEARCH config unreadable (%s); observation mode off"
                       % type(e).__name__)
        return RESEARCH_OFF


def capture_settings(cfg):
    """Return the two opt-in capture flags; malformed settings fail closed."""
    try:
        sec = cfg.get("research") if isinstance(cfg, dict) else None
        if sec is None:
            return CAPTURE_OFF
        if not isinstance(sec, dict):
            _research_warn(("capture", "research", type(sec).__name__),
                           "RESEARCH capture ignored: research is not an object")
            return CAPTURE_OFF
        enabled = sec.get("enabled")
        if enabled is not True:
            if enabled is not None and not isinstance(enabled, bool):
                _research_warn(("capture", "enabled", repr(enabled)[:60]),
                               "RESEARCH capture ignored: malformed enabled")
            return CAPTURE_OFF
        values = [sec.get(k) for k in ("capture_quote_path",
                                       "capture_failed_quotes")]
        for key, value in zip(("capture_quote_path", "capture_failed_quotes"),
                              values):
            if value is not None and not isinstance(value, bool):
                _research_warn(("capture", key, repr(value)[:60]),
                               "RESEARCH capture ignored: malformed %s" % key)
                return CAPTURE_OFF
        return tuple(value is True for value in values)
    except Exception as e:
        _research_warn(("capture", "error", type(e).__name__),
                       "RESEARCH capture settings unreadable (%s)" % type(e).__name__)
        return CAPTURE_OFF


def _capture_mint(mint):
    return "".join(c if c.isalnum() and c.isascii() else "_" for c in str(mint))[:128] or "unknown"


def maybe_capture_quote_tick(trader_or_cfg, state_path, mint, chain,
                             price_usd, liquidity_usd):
    """Append an observed mark. A callable price defers native/USD conversion."""
    try:
        cfg = trader_or_cfg if isinstance(trader_or_cfg, dict) else trader_or_cfg.cfg
        if not capture_settings(cfg)[0]:
            return
        path = os.path.join(os.path.dirname(os.path.abspath(state_path)),
                            "quote_paths", _capture_mint(mint) + ".jsonl")
        row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "mint": mint,
               "chain": chain,
               "price_usd": price_usd() if callable(price_usd) else price_usd,
               "liquidity_usd": liquidity_usd}
        with _quote_paths_lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            count = _quote_path_counts.get(path)
            if count is None:
                if os.path.exists(path):
                    with open(path) as f:
                        count = sum(1 for _ in f)
                else:
                    count = 0
            if count >= QUOTE_PATH_MAX_TICKS or size > QUOTE_PATH_MAX_BYTES:
                _research_warn(("quote_cap", path),
                               "RESEARCH quote path capped for %s" % mint)
                return
            line = json.dumps(row) + "\n"
            if size + len(line.encode("utf-8")) > QUOTE_PATH_MAX_BYTES:
                _research_warn(("quote_cap", path),
                               "RESEARCH quote path capped for %s" % mint)
                return
            with open(path, "a") as f:
                f.write(line)
            _quote_path_counts[path] = count + 1
    except Exception as e:
        _research_warn(("quote_path", type(e).__name__),
                       "RESEARCH quote path unavailable (%s)" % type(e).__name__)


def maybe_capture_failed_quote(cfg, state_path, mint, chain, reason, econ_dict):
    """Append a failed quote or guard decision without affecting trading."""
    try:
        if not capture_settings(cfg)[1]:
            return
        path = os.path.join(os.path.dirname(os.path.abspath(state_path)),
                            "failed_quotes.jsonl")
        row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "mint": mint,
               "chain": chain, "reason": reason}
        if isinstance(econ_dict, dict):
            row.update({k: v for k, v in econ_dict.items() if k not in row})
        line = json.dumps(row) + "\n"
        with _failed_quotes_lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a") as f:
                f.write(line)
    except Exception as e:
        _research_warn(("failed_quote", type(e).__name__),
                       "RESEARCH failed quote capture unavailable (%s)"
                       % type(e).__name__)


def _maybe_capture_failure(trader, signal, reason, **extra):
    """Collect only already-known economics when failed-quote capture is on."""
    try:
        if not capture_settings(trader.cfg)[1]:
            return
        econ = {k: signal.get(k) for k in
                ("name", "liquidity_usd", "signal_price_usd", "pool_url",
                 "m15_gain_pct", "buy_sell_ratio")}
        econ.update(extra)
        maybe_capture_failed_quote(trader.cfg, trader.state_path,
                                   signal.get("mint"),
                                   signal.get("chain", "solana"), reason, econ)
    except Exception as e:
        _research_warn(("failure_site", type(e).__name__),
                       "RESEARCH failed quote capture unavailable (%s)"
                       % type(e).__name__)


def registered_question_ids(path=None):
    """Ids of `open` questions in research_questions.json, re-read only when
    the file changes. Missing or corrupt registry -> empty (logged once)."""
    path = path or RESEARCH_QUESTIONS_PATH
    try:
        st = os.stat(path)
    except OSError as e:
        _research_warn(("registry", path, type(e).__name__),
                       "RESEARCH registry %s unreadable (%s); trades tagged "
                       "unregistered" % (path, type(e).__name__))
        return frozenset()
    key = (path, st.st_mtime_ns, st.st_size)
    if _research_registry["key"] == key:
        return _research_registry["ids"]
    ids = frozenset()
    try:
        with open(path) as f:
            data = json.load(f)
        ids = frozenset(q["id"] for q in data["questions"]
                        if isinstance(q, dict) and isinstance(q.get("id"), str)
                        and q.get("status") == "open")
    except Exception as e:
        _research_warn(("registry", key, type(e).__name__),
                       "RESEARCH registry %s corrupt (%s); trades tagged "
                       "unregistered" % (path, type(e).__name__))
    _research_registry.update(key=key, ids=ids)
    return ids


def maybe_tag_research(rec, cfg, pos=None):
    """Observation mode: set rec["research_question"] when research.enabled
    and research.tag_trades are both true; otherwise return rec untouched.
    Entry sites tag the enrichment dict, so the journal record and the
    position both carry it; the close site passes the position so its record
    keeps the entry-time tag. Never raises."""
    try:
        tag, qid, _ = research_settings(cfg)
        if not tag or not isinstance(rec, dict):
            return rec
        if isinstance(pos, dict):
            q = pos.get("research_question")
            rec["research_question"] = (q if isinstance(q, str) and q
                                        else "unregistered")
            return rec
        if qid is None:
            _research_warn(("question_id", "unset"),
                           "RESEARCH tag_trades is on but no question_id is "
                           "set; trades tagged unregistered")
        elif qid not in registered_question_ids():
            _research_warn(("question_id", "unregistered", qid),
                           "RESEARCH question_id %r is not an open question in "
                           "%s; trades tagged unregistered"
                           % (qid, RESEARCH_QUESTIONS_PATH))
            qid = None
        rec["research_question"] = qid or "unregistered"
        return rec
    except Exception as e:
        _research_warn(("tag", type(e).__name__),
                       "RESEARCH tagging failed (%s); record left untagged"
                       % type(e).__name__)
        return rec


def maybe_research_dashboard(cfg, state_path):
    """After a close: refresh analysis/obs/kill_dashboard.json when
    research.enabled and research.dashboard are both true. Off -> returns
    None without importing anything. Failures are logged once, never raised."""
    try:
        if not research_settings(cfg)[2]:
            return None
        import obs_dashboard
        dash = obs_dashboard.update(os.path.join(
            os.path.dirname(os.path.abspath(state_path)), "trades.jsonl"))
        if isinstance(dash, dict) and "error" in dash:
            err = str(dash["error"])[:120]
            _research_warn(("dashboard", err[:60]),
                           "RESEARCH dashboard update failed (%s); trading "
                           "unaffected" % err)
        return dash
    except Exception as e:
        _research_warn(("dashboard", type(e).__name__),
                       "RESEARCH dashboard update failed (%s: %s); trading "
                       "unaffected" % (type(e).__name__, str(e)[:80]))
        return None


class ApiThrottled(Exception):
    """Raised when the circuit breaker is open: fail fast, don't hit the API."""


class RateLimiter:
    """Token bucket + circuit breaker for one API family.

    Why this exists: GeckoTerminal's free tier allows ~30 req/min and
    penalizes concurrent bursts with 429s that escalate to dropped
    connections. One bad polling pattern (pages x threads every 10s)
    poisoned the endpoint for hours overnight. This makes the bot
    physically incapable of repeating that pattern:
      - token bucket caps the sustained rate (default 20 req/min)
      - every request goes through the bucket: no concurrent bursts
      - HTTP 429s honor a backoff with jitter instead of retrying hot
      - after N consecutive failures the circuit opens and requests fail
        fast for `cooldown_sec`: the scan degrades instead of dying
    """
    def __init__(self, name, requests_per_min=20, burst=2,
                 fail_threshold=5, cooldown_sec=600):
        self.name = name
        self.rate = requests_per_min / 60.0  # tokens per second
        self.burst = max(burst, 1)
        self.tokens = float(self.burst)
        self.last = time.monotonic()
        self.lock = threading.Lock()
        self.fails = 0
        self.fail_threshold = fail_threshold
        self.cooldown = cooldown_sec
        self.circuit_open_until = 0.0
        self.retry_after = 0.0

    def _refill(self):
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now

    def acquire(self):
        """Block until one request is allowed. Raises ApiThrottled when the
        circuit is open - the caller's cue to degrade gracefully."""
        with self.lock:
            if time.monotonic() < self.circuit_open_until:
                left = self.circuit_open_until - time.monotonic()
                raise ApiThrottled("%s circuit open, %.0fs left"
                                   % (self.name, left))
            ra, self.retry_after = self.retry_after, 0.0
        if ra > 0:
            time.sleep(ra)
        while True:
            with self.lock:
                self._refill()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.rate
            time.sleep(min(wait, 1.0))

    def ok(self):
        with self.lock:
            self.fails = 0

    def fail(self, retry_after=0.0):
        with self.lock:
            self.fails += 1
            if retry_after > 0:
                self.retry_after = max(self.retry_after, retry_after)
            if self.fails >= self.fail_threshold:
                now = time.monotonic()
                if now >= self.circuit_open_until:
                    log("CIRCUIT OPEN %s: %d consecutive failures, "
                        "pausing requests for %ds"
                        % (self.name, self.fails, self.cooldown))
                self.circuit_open_until = now + self.cooldown


_gt_limiter = None


def gt_limiter(cfg):
    """Shared GeckoTerminal limiter, built once from the hunter config."""
    global _gt_limiter
    if _gt_limiter is None:
        rl = (cfg.get("hunter") or {}).get("rate_limit") or {}
        _gt_limiter = RateLimiter(
            "geckoterminal",
            requests_per_min=rl.get("requests_per_min", 20),
            burst=rl.get("burst", 2),
            fail_threshold=rl.get("fail_threshold", 5),
            cooldown_sec=rl.get("cooldown_sec", 600))
    return _gt_limiter


def gt_get(path, cfg, timeout=20):
    """GeckoTerminal GET through the rate limiter + circuit breaker."""
    lim = gt_limiter(cfg)
    lim.acquire()
    try:
        data = http_get(GT_BASE + path, timeout=timeout)
    except requests.exceptions.HTTPError as e:
        retry_after = 0.0
        code = e.response.status_code if e.response is not None else 0
        if code == 429:
            try:
                retry_after = float(e.response.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                retry_after = 0.0
            if retry_after <= 0:
                retry_after = 60.0  # GT rarely sends Retry-After; back off anyway
            retry_after += random.uniform(0, 5)  # jitter: no thundering herd
            log("GT 429 on %s, backing off %.0fs" % (path, retry_after))
        lim.fail(retry_after=retry_after if code == 429 else 0.0)
        raise
    except Exception:
        lim.fail()
        raise
    lim.ok()
    return data


DS_BASE = "https://api.dexscreener.com"
_ds_limiter = None


def ds_limiter(cfg):
    """Shared DexScreener limiter (generous free tier, still capped)."""
    global _ds_limiter
    if _ds_limiter is None:
        rl = (cfg.get("hunter") or {}).get("dexscreener") or {}
        _ds_limiter = RateLimiter(
            "dexscreener",
            requests_per_min=rl.get("requests_per_min", 60),
            burst=rl.get("burst", 5),
            fail_threshold=rl.get("fail_threshold", 5),
            cooldown_sec=rl.get("cooldown_sec", 300))
    return _ds_limiter


def ds_get(path, cfg, timeout=20, _retried=False):
    """DexScreener GET through its own rate limiter + circuit breaker."""
    lim = ds_limiter(cfg)
    lim.acquire()
    try:
        data = http_get(DS_BASE + path, timeout=timeout)
    except requests.exceptions.HTTPError as e:
        retry_after = 0.0
        code = e.response.status_code if e.response is not None else 0
        if code == 429:
            retry_after = 60.0 + random.uniform(0, 5)
            log("DS 429 on %s, backing off %.0fs" % (path, retry_after))
        lim.fail(retry_after=retry_after if code == 429 else 0.0)
        raise
    except Exception as e:
        # One immediate retry on transient transport errors (the same
        # IncompleteRead signature that killed GeckoTerminal). The retry
        # belongs to the same logical call, so a blip-then-recovery
        # counts once, not twice - otherwise the breaker opens faster
        # than the pre-retry behavior.
        if _transient(e) and not _retried:
            time.sleep(2.0)
            return ds_get(path, cfg, timeout=timeout, _retried=True)
        lim.fail()
        log(f"dexscreener GET {path} failed: {type(e).__name__}: {e}")
        raise
    lim.ok()
    return data


class DexScreenerSource:
    """Fallback discovery when GeckoTerminal is throttled.

    Boosted-token discovery + batched pair stats. Momentum is measured on
    the 5-minute window (DexScreener has no 15m bucket), keeping the same
    buy-pressure shape as the GT filter. Caveat: boosts are paid
    promotions, so this discovery list skews toward promoted tokens - it
    is a failover feed, not the primary one.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.d = (cfg.get("hunter") or {}).get("dexscreener") or {}

    def scan(self):
        if not self.d.get("enabled", True):
            return []
        try:
            boosts = ds_get("/token-boosts/top/v1", self.cfg, timeout=15)
        except ApiThrottled:
            return []
        except Exception:
            return []
        addrs = [b.get("tokenAddress") for b in (boosts or [])
                 if b.get("chainId") == "solana" and b.get("tokenAddress")]
        addrs = addrs[:self.d.get("max_tokens", 30)]
        if not addrs:
            return []
        signals = []
        for i in range(0, len(addrs), 30):  # max 30 addresses per call
            batch = addrs[i:i + 30]
            try:
                resp = ds_get("/latest/dex/tokens/" + ",".join(batch),
                              self.cfg, timeout=20)
            except ApiThrottled:
                break
            except Exception:
                continue  # single attempt, no retry
            for p in (resp or {}).get("pairs") or []:
                s = self._signal(p)
                if s:
                    signals.append(s)
        signals.sort(key=lambda s: s["m15_gain_pct"], reverse=True)
        return signals

    def _signal(self, p):
        try:
            if p.get("chainId") != "solana":
                return None
            bt = p.get("baseToken") or {}
            mint = bt.get("address")
            if not mint or mint == SOL_MINT:
                return None
            qt = p.get("quoteToken") or {}
            if qt.get("address") and qt.get("address") != SOL_MINT:
                return None  # strict SOL-pair enforcement
            pc = (p.get("priceChange") or {}).get("m5") or 0
            vol = (p.get("volume") or {}).get("m5") or 0
            tx = (p.get("txns") or {}).get("m5") or {}
            buys = int(tx.get("buys") or 0)
            sells = int(tx.get("sells") or 0)
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if to_f(pc) < self.d.get("min_m5_gain_pct", 5):
                return None
            if to_f(vol) < self.d.get("min_m5_volume_usd", 2000):
                return None
            if buys < self.d.get("min_m5_buys", 10):
                return None
            if sells < self.d.get("min_m5_sells", 5):
                return None  # nobody able to sell = possible honeypot
            ratio = buys / max(sells, 1)
            if ratio < self.d.get("min_buy_sell_ratio", 1.5):
                return None
            if not math.isfinite(float(liq)) or to_f(liq) < self.d.get("min_liquidity_usd", 0):
                return None
            if not math.isfinite(float(pc)) or not math.isfinite(float(vol)):
                return None
            lp_burn, lp_lock = lp_evidence(p)
            return {
                "name": bt.get("name") or bt.get("symbol") or mint[:8],
                "mint": mint, "pool": p.get("pairAddress"),
                "chain": "solana",
                "m15_gain_pct": round(to_f(pc), 1),  # 5m window; key kept
                "buy_sell_ratio": round(ratio, 1),
                "m15_buys": buys, "m15_sells": sells,
                "liquidity_usd": round(to_f(liq)),
                "m15_volume_usd": round(to_f(vol)),
                "mcap_usd": round(to_f(p.get("marketCap")
                                       or p.get("fdv") or 0)),
                "signal_price_usd": optional_positive_float(p.get("priceUsd")),
                "lp_burn_pct": lp_burn,
                "lp_locked": lp_lock,
                "pool_created_at": journal.pool_created_at(p),
                "pool_url": p.get("url") or "",
                "source": "dexscreener", "window_label": "5m",
                "ts": time.time(),
            }
        except Exception:
            return None


# ---------------- minimal Solana JSON-RPC client (with endpoint fallback) ----------------
DEFAULT_RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
]


class Rpc:
    def __init__(self, urls):
        if isinstance(urls, str):
            urls = [urls]
        self.urls = urls or list(DEFAULT_RPC_URLS)
        self._id = 0
        self._idx = 0

    def call(self, method, params=None, timeout=30, retries=3):
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method,
                   "params": params or []}
        last = {"error": "no attempts"}
        for i in range(max(retries, 1)):
            url = self.urls[(self._idx + i) % len(self.urls)]
            try:
                r = http_post(url, payload, timeout=timeout)
                if isinstance(r, dict) and "error" not in r and "result" in r:
                    self._idx = (self._idx + i) % len(self.urls)
                    return r
                last = r
            except Exception as e:
                last = {"error": str(e)}
            time.sleep(1)
        return last if isinstance(last, dict) else {"error": str(last)}

    def get_token_supply(self, mint):
        r = self.call("getTokenSupply", [mint])
        try:
            return r["result"]["value"]["decimals"]
        except Exception:
            raise RuntimeError("getTokenSupply failed for %s: %s"
                               % (mint, r.get("error")))

    def get_token_balance_raw(self, ata):
        r = self.call("getTokenAccountBalance", [str(ata)], retries=1)
        try:
            return int(r["result"]["value"]["amount"])
        except Exception:
            return 0

    def get_sol_balance(self, owner):
        r = self.call("getBalance", [str(owner)])
        try:
            return r["result"]["value"] / 1e9
        except Exception:
            return 0.0

    def send_transaction(self, signed_tx_bytes):
        b64 = base64.b64encode(signed_tx_bytes).decode()
        r = self.call("sendTransaction", [b64, {
            "encoding": "base64", "skipPreflight": False,
            "preflightCommitment": "confirmed", "maxRetries": 3}])
        if "error" in r:
            raise RuntimeError("sendTransaction failed: %s" % r["error"])
        return r["result"]

    def confirm(self, sig, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.call("getSignatureStatuses", [[sig],
                                                   {"searchTransactionHistory": True}])
            try:
                st = r["result"]["value"][0]
                if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                    if st.get("err"):
                        raise RuntimeError("tx failed on-chain: %s" % st["err"])
                    return True
            except (KeyError, TypeError, IndexError):
                pass
            time.sleep(2)
        raise RuntimeError("tx not confirmed in %ds: %s" % (timeout, sig))


def ata_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
    (addr, _b) = Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID)
    return addr


# ---------------- Jupiter swap ----------------
def jup_quote(input_mint, output_mint, amount_raw, slippage_bps, timeout=20):
    url = ("%s/quote?inputMint=%s&outputMint=%s&amount=%d&slippageBps=%d"
           % (JUP_BASE, input_mint, output_mint, amount_raw, slippage_bps))
    return http_get(url, timeout=timeout)


def jup_swap_tx(quote_resp, owner_str, max_priority_fee_lamports):
    body = {
        "quoteResponse": quote_resp,
        "userPublicKey": owner_str,
        "wrapAndUnwrapSol": True,
        "asLegacyTransaction": False,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "maxLamports": max_priority_fee_lamports,
                "priorityLevel": "high",
            }
        },
    }
    r = http_post("%s/swap" % JUP_BASE, body)
    return base64.b64decode(r["swapTransaction"])


def _transient(err):
    """Transport-level failures worth a quick retry. Never 4xx (except 429)
    and never our own circuit-breaker signal - those must fail fast."""
    if isinstance(err, requests.exceptions.HTTPError):
        code = err.response.status_code if err.response is not None else 0
        return code == 429 or 500 <= code < 600
    return isinstance(err, (
        http.client.IncompleteRead,
        http.client.RemoteDisconnected,
        ConnectionError,
        TimeoutError,
        socket.timeout,
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
    ))


def quote_with_retry(label, fn, attempts=3, base_delay=2.0):
    """Run fn() (a Jupiter quote call) up to `attempts` times, backing off
    on transient transport errors. Returns fn()'s result, or raises the
    last error. A flaky API must not flip a safety verdict on one bad call."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if not _transient(e) or i == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** i) + random.uniform(0, 0.5))
    raise last


def _race_price(jup_fn, ds_fn, budget=4.0, headstart=0.8):
    """First valid price wins between two racing callables.

    jup_fn gets `headstart` seconds alone (the common case costs exactly
    one request). If it hasn't answered, ds_fn fires too and the first
    finite positive price wins. Returns None when neither yields a valid
    price within `budget`. A raising leg is treated as failed; a leg that
    returns an invalid price (None/0/NaN/inf) is ignored so the other leg
    can still win.
    """
    results = queue.Queue()

    def run(fn):
        try:
            px = fn()
        except Exception:
            return
        if isinstance(px, (int, float)) and math.isfinite(px) and px > 0:
            results.put(float(px))

    tj = threading.Thread(target=run, args=(jup_fn,), daemon=True)
    tj.start()
    try:
        return results.get(timeout=headstart)
    except queue.Empty:
        pass
    td = threading.Thread(target=run, args=(ds_fn,), daemon=True)
    td.start()
    try:
        return results.get(timeout=max(0.05, budget - headstart))
    except queue.Empty:
        return None


# ---------------- FOMO hunter ----------------
class Hunter:
    """Finds violent buy-pressure pumps. Nothing else qualifies."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.h = cfg["hunter"]
        self._last_circuit_log = 0.0
        self._gt_was_down = False
        self.last_early = []  # set fresh on every scan()
        self.last_early_accel_rejects = []  # R10: set fresh on every scan()

    def scan(self):
        """Primary: GeckoTerminal. Failover: DexScreener, only when GT is
        unhealthy (0 pages loaded) - never as an augmentation layer."""
        self.last_early = []  # pre-peak signals from this scan, if any
        # R10 instrumentation: pools rejected by the early path's accel gate,
        # journaled so the loop can prospectively test the corrected gate
        # orientation. Read-only list of dicts; never affects entry decisions.
        self.last_early_accel_rejects = []
        signals, healthy = self._scan_gt()
        if healthy:
            if self._gt_was_down:
                log("scan: geckoterminal recovered")
            self._gt_was_down = False
            return signals
        self._gt_was_down = True
        ds = DexScreenerSource(self.cfg).scan()
        if ds:
            log("scan: GT down, dexscreener fallback -> %d signal(s)"
                % len(ds))
        return ds

    def _scan_gt(self):
        """Returns (signals, healthy). healthy=False means the feed itself
        failed - the caller decides whether to fail over."""
        pools = {}
        paths = []
        for net, ncfg in CHAINS.items():
            ptr = self.h.get("%s_pages_trending" % net,
                             ncfg["pages_trending"])
            pnew = self.h.get("%s_pages_new" % net, ncfg["pages_new"])
            paths += [("/networks/%s/trending_pools?page=%d" % (net, p), net)
                      for p in range(1, ptr + 1)]
            paths += [("/networks/%s/new_pools?page=%d" % (net, p), net)
                      for p in range(1, pnew + 1)]

        pages_ok = 0
        # Sequential through the token bucket, on purpose: GeckoTerminal
        # penalizes concurrent bursts, so there is no threading here.
        # Partial scans are fine.
        for path, net in paths:
            try:
                items = gt_get(path, self.cfg, timeout=15).get("data", []) or []
                pages_ok += 1
            except ApiThrottled:
                # circuit open: fail fast and degrade this scan, never hammer
                if time.time() - self._last_circuit_log > 60:
                    log("scan: rate-limit circuit open, skipping (degraded)")
                    self._last_circuit_log = time.time()
                break
            except Exception:
                # single attempt, no retry: retries deepen the penalty
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                attrs = item.get("attributes") or {}
                if not isinstance(attrs, dict):
                    continue
                addr = attrs.get("address")
                if not addr:
                    continue
                mint = None
                qt_mint = None
                try:
                    rel = item["relationships"]
                    mint = rel["base_token"]["data"]["id"]
                    for prefix in ("solana_", "bsc_"):
                        if mint.startswith(prefix):
                            mint = mint[len(prefix):]
                            break
                    qt_mint = rel["quote_token"]["data"]["id"]
                    for prefix in ("solana_", "bsc_"):
                        if qt_mint.startswith(prefix):
                            qt_mint = qt_mint[len(prefix):]
                            break
                except Exception:
                    pass
                pools[(net, addr)] = (attrs, mint, qt_mint)

        def native_pair_ok(net, a, mint, qt_mint):
            """Only the chain's native-vehicle pairs (SOL pairs on Solana,
            WBNB pairs on BSC). Rejects wrapped-quote leaks and dust."""
            ncfg = CHAINS[net]
            if not mint:
                return False
            if mint.lower() == SOL_MINT.lower():
                return False
            if net == "bsc" and mint.lower() in BSC_QUOTES:
                return False  # base is the quote vehicle itself, not a token
            if qt_mint:
                return qt_mint.lower() in ncfg["quotes"]
            nm = (a.get("name") or "").strip()
            return bool(nm) and nm.endswith("/ %s" % ncfg["quote_label"])

        signals = []
        min_gain = self.h.get("min_m15_gain_pct", 100)
        min_ratio = self.h.get("min_buy_sell_ratio", 10.0)
        min_liq = self.h.get("min_liquidity_usd", 15000)
        max_liq = self.h.get("max_liquidity_usd", 0)  # 0 = no cap
        min_vol = self.h.get("min_m15_volume_usd", 5000)
        min_buys = self.h.get("min_m15_buys", 50)
        min_sells = self.h.get("min_m15_sells", 5)

        for (net, addr), (a, mint, qt_mint) in pools.items():
            # strict native-pair enforcement per chain (SOL pairs on Solana,
            # WBNB pairs on BSC): the non-native entries in the journal
            # (Claude/ANTHRP, USDC/USDC) all came from quote leaks.
            try:
                if not native_pair_ok(net, a, mint, qt_mint):
                    continue
                pc = a.get("price_change_percentage") or {}
                vol = a.get("volume_usd") or {}
                tx = a.get("transactions") or {}
                liq = to_f(a.get("reserve_in_usd"))
                p15 = to_f(pc.get("m15"))
                if p15 < min_gain or liq < min_liq:
                    continue
                if max_liq and liq > max_liq:
                    continue  # too big = slow large-cap, not a frenzy
                if to_f(vol.get("m15")) < min_vol:
                    continue
                t = tx.get("m15", {}) or {}
                buys, sells = int(t.get("buys", 0)), int(t.get("sells", 0))
                if buys < min_buys:
                    continue
                if sells < min_sells:
                    continue  # nobody is able to sell = possible honeypot
                ratio = buys / max(sells, 1)
                if ratio < min_ratio:
                    continue
                lp_burn, lp_lock = lp_evidence(a)
                signals.append({
                    "name": a.get("name"), "mint": mint, "pool": addr,
                    "chain": net,
                    "m15_gain_pct": round(p15, 1),
                    "buy_sell_ratio": round(ratio, 1),
                    "m15_buys": buys, "m15_sells": sells,
                    "liquidity_usd": round(liq),
                    "m15_volume_usd": round(to_f(vol.get("m15"))),
                    "mcap_usd": round(to_f(a.get("market_cap_usd"))
                                       or to_f(a.get("fdv_usd"))),
                    "signal_price_usd": optional_positive_float(a.get("base_token_price_usd")),
                    "lp_burn_pct": lp_burn,
                    "lp_locked": lp_lock,
                    "pool_created_at": journal.pool_created_at(a),
                    "pool_url": "https://www.geckoterminal.com/%s/pools/%s" % (net, addr),
                    "source": "geckoterminal", "window_label": "15m",
                    "ts": time.time(),
                })
            except (TypeError, ValueError, OverflowError, AttributeError):
                continue
        signals.sort(key=lambda s: s["m15_gain_pct"], reverse=True)
        if pages_ok < len(paths):
            log("scan degraded: %d/%d pages ok" % (pages_ok, len(paths)))
        # pre-peak ("early") movers: 5m momentum that is still accelerating,
        # before the full 15m entry bar fires. Marked early=True for the
        # trader; pools already in `signals` are excluded to avoid double
        # entries on the same mint.
        early = []
        early_accel_rejects = []  # R10: accel-gate rejects, journaled only
        ecfg = self.h.get("early", {})
        if ecfg.get("enabled", True):
            seen_mints = {(s["chain"], s["mint"]) for s in signals}
            e_min_m5 = ecfg.get("min_m5_gain_pct", 10)
            e_max_m5 = ecfg.get("max_m5_gain_pct", 150)
            e_accel = ecfg.get("accel_ratio", 1.2)
            e_min_m15 = ecfg.get("min_m15_gain_pct", 15)
            e_min_liq = ecfg.get("min_liquidity_usd", 8000)
            e_min_vol = ecfg.get("min_m15_volume_usd", 5000)
            e_min_buys = ecfg.get("min_m15_buys", 10)
            e_min_sells = ecfg.get("min_m15_sells", 3)
            e_min_ratio = ecfg.get("min_buy_sell_ratio", 1.5)
            for (net, addr), (a, mint, qt_mint) in pools.items():
                try:
                    if not mint or (net, mint) in seen_mints:
                        continue
                    if not native_pair_ok(net, a, mint, qt_mint):
                        continue
                    pc = a.get("price_change_percentage") or {}
                    vol = a.get("volume_usd") or {}
                    tx = a.get("transactions") or {}
                    liq = to_f(a.get("reserve_in_usd"))
                    p5 = to_f(pc.get("m5"))
                    p15 = to_f(pc.get("m15"))
                    if not (e_min_m5 <= p5 <= e_max_m5):
                        continue
                    if p15 < e_min_m15 or p15 <= p5 * e_accel:
                        # R10 instrumentation: journal the accel-gate reject
                        # so the improvement loop can prospectively test the
                        # corrected gate orientation. Additive only: the gate
                        # behavior itself is unchanged.
                        try:
                            early_accel_rejects.append({
                                "name": a.get("name"), "mint": mint,
                                "pool": addr, "chain": net,
                                "signal_price_usd": optional_positive_float(
                                    a.get("base_token_price_usd")),
                                "liquidity_usd": round(liq),
                                "m15_volume_usd": round(to_f(vol.get("m15"))),
                                "m15_gain_pct": round(p15, 1),
                                "m5_gain_pct": round(p5, 1),
                                "buy_sell_ratio": None,
                                "mcap_usd": round(to_f(a.get("market_cap_usd"))
                                                 or to_f(a.get("fdv_usd"))),
                                "pool_created_at": journal.pool_created_at(a),
                                "source": "geckoterminal",
                                "accel_ratio": e_accel,
                            })
                        except Exception:
                            pass
                        continue  # not accelerating: move is stalling
                    if liq < e_min_liq:
                        continue
                    if to_f(vol.get("m15")) < e_min_vol:
                        continue
                    t = tx.get("m15", {}) or {}
                    buys, sells = int(t.get("buys", 0)), int(t.get("sells", 0))
                    if buys < e_min_buys or sells < e_min_sells:
                        continue
                    ratio = buys / max(sells, 1)
                    if ratio < e_min_ratio:
                        continue
                    seen_mints.add((net, mint))
                    lp_burn, lp_lock = lp_evidence(a)
                    early.append({
                        "name": a.get("name"), "mint": mint, "pool": addr,
                        "chain": net,
                        "m5_gain_pct": round(p5, 1),
                        "m15_gain_pct": round(p15, 1),
                        "buy_sell_ratio": round(ratio, 1),
                        "m15_buys": buys, "m15_sells": sells,
                        "liquidity_usd": round(liq),
                        "m15_volume_usd": round(to_f(vol.get("m15"))),
                        "mcap_usd": round(to_f(a.get("market_cap_usd"))
                                           or to_f(a.get("fdv_usd"))),
                        "signal_price_usd": optional_positive_float(a.get("base_token_price_usd")),
                        "lp_burn_pct": lp_burn,
                        "lp_locked": lp_lock,
                        "pool_created_at": journal.pool_created_at(a),
                        "pool_url": "https://www.geckoterminal.com/%s/pools/%s" % (net, addr),
                        "source": "geckoterminal", "window_label": "5m",
                        "early": True,
                        "ts": time.time(),
                    })
                except (TypeError, ValueError, OverflowError, AttributeError):
                    continue
            early.sort(key=lambda s: s["m5_gain_pct"] / max(s["m15_gain_pct"], 1),
                       reverse=True)  # earliest-first: most of the move still ahead
        self.last_early = early
        self.last_early_accel_rejects = early_accel_rejects
        return signals, pages_ok > 0


# ---------------- trader ----------------
class Trader:
    def __init__(self, cfg, state_path):
        self.cfg = cfg
        self.state_path = state_path
        rpc_urls = cfg.get("rpc_urls") or [cfg.get("rpc_url",
                                                  DEFAULT_RPC_URLS[0])]
        self.rpc = Rpc(rpc_urls)
        from keystore import load_solana_keypair, short as _short
        wallets = cfg.get("wallets", {}) or {}
        sol_path = wallets.get("solana_key_file") or cfg.get("keypair_path")
        if not sol_path:
            raise SystemExit("ERROR: set wallets.solana_key_file in config.json")
        self.keypair = load_solana_keypair(sol_path)
        self.owner = self.keypair.pubkey()
        self.owner_str = str(self.owner)
        self.bsc = None
        bsc_path = (wallets.get("bsc_key_file") or "").strip()
        if bsc_path:
            from bsc_wallet import BscWallet
            self.bsc = BscWallet(bsc_path, cfg.get("bsc_rpc_urls"))
            log("BSC wallet armed: %s" % self.bsc.status_line())
        else:
            # No key needed for paper BSC: quotes are keyless eth_call and
            # dry-run fills settle on the virtual ledger. A key file is
            # only required for live (non-dry-run) BSC swaps.
            log("BSC key not configured - live BSC swaps need "
                "wallets.bsc_key_file; paper BSC trading enabled")
        self._bscswap = None
        # BSC reconnect backoff: a failed BscSwap construction no longer
        # latches BSC off forever. _bsc_retry_at is the next epoch time a
        # (re)connect may be attempted; _bsc_backoff_s doubles per
        # consecutive failure (60s -> 30min cap) and resets on success.
        # _bsc_down_logged rate-limits the "unavailable" log to one line
        # per retry window so frequent entry attempts don't spam the log.
        self._bsc_retry_at = 0.0
        self._bsc_backoff_s = 60.0
        self._bsc_down_logged = False
        self.dry_run = cfg.get("dry_run", True)
        self.decimals_cache = {}
        self.lock = threading.RLock()
        self.pending_entries = set()
        # per-mint recent (ts, price) for the dump detector; in-memory only,
        # rebuilt after restarts (arms within ~a minute of managing).
        self._px_hist = {}
        # Research instrumentation (journal.py): candidate-observation
        # tracker. Additive only; never influences entry decisions.
        self._obs_tracker = journal.ObservationTracker()
        try:
            with open(state_path) as f:
                self.state = json.load(f)
            if (not isinstance(self.state, dict)
                    or not isinstance(self.state.get("positions"), dict)
                    or not isinstance(self.state.get("cooldown"), dict)
                    or not isinstance(self.state.get("trades_today"), list)):
                raise ValueError("state schema missing positions/cooldown/trades_today")
        except Exception as e:
            log("STATE LOAD FAILED (%s: %s); using empty paper state"
                % (type(e).__name__, e))
            self.state = {"positions": {}, "cooldown": {}, "trades_today": [],
                          "trades_this_hour": [],
                          "day": _bot_today_str(), "realized_sol": 0.0,
                          "realized_bnb": 0.0, "realized_usd": 0.0}
        self._roll_day()
        # Paper bankroll: seeded once from the journal's all-time P&L.
        money.ensure_state(self.state, self.cfg)
        _jp = os.path.join(os.path.dirname(os.path.abspath(state_path)),
                           "trades.jsonl")
        _seed = money.seed_from_journal(self.state, self.cfg, _jp)
        if _seed.get("seeded"):
            self.save()
            log("BANKROLL seeded at $%.2f from journal all-time P&L "
                "(%d priced closes, %d skipped without USD)" %
                (_seed["bankroll_usd"], _seed["priced_closes"],
                 _seed["skipped_closes"]))
        log("BANKROLL $%.2f | peak $%.2f | drawdown %.1f%%" %
            (self.state.get("bankroll_usd", 0.0),
             self.state.get("equity_peak_usd", 0.0),
             money.drawdown_pct(self.state)))
        log("wallet sol=%s bsc=%s | DRY RUN: %s"
            % (_short(self.owner_str),
               _short(self.bsc.address) if self.bsc else "none",
               self.dry_run))

    def _roll_day(self):
        with self.lock:
            today = _bot_today_str()
            if self.state.get("day") != today:
                self.state.update({"day": today, "trades_today": [],
                                   "realized_sol": 0.0, "realized_bnb": 0.0,
                                   "realized_usd": 0.0})
                # Snapshot today's starting bankroll for the dynamic cap.
                money.roll_day(self.state, self.cfg)
            self.state.setdefault("trades_this_hour", [])
            self.state.setdefault("realized_bnb", 0.0)
            self.state.setdefault("realized_usd", 0.0)
            self.state.setdefault("dump_cooldown", {})

    def _prune_hour_trades(self):
        """Rolling 60-minute window: drop entries older than an hour. This
        is the automated hourly reset - no manual reset, no midnight
        boundary, no action needed."""
        with self.lock:
            now = time.time()
            hour = self.state.setdefault("trades_this_hour", [])
            self.state["trades_this_hour"] = [t for t in hour if now - t < 3600]
            return self.state["trades_this_hour"]

    def save(self):
        with self.lock:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f, indent=1)
            os.replace(tmp, self.state_path)

    # -- helpers --
    def decimals(self, mint):
        if mint not in self.decimals_cache:
            self.decimals_cache[mint] = self.rpc.get_token_supply(mint)
        return self.decimals_cache[mint]

    def token_balance_raw(self, mint):
        if self.dry_run:
            # paper mode: virtual ledger - the real wallet holds no tokens
            pos = self.state["positions"].get(mint)
            return pos.get("tokens_raw", 0) if pos else 0
        return self.rpc.get_token_balance_raw(
            ata_address(self.owner, Pubkey.from_string(mint)))

    def price_sol(self, mint):
        """SOL per token, via Jupiter quote (retried on transient errors).
        On Jupiter failure, falls back to a FRESH DexScreener quote converted
        via sol_usd() - never a stale price. None when both fail - callers
        must never exit on a stale price."""
        try:
            d = self.decimals(mint)
            q = quote_with_retry(
                "price %s" % mint[:8],
                lambda: jup_quote(mint, SOL_MINT, 10 ** d,
                                  self.cfg["exit"].get("slippage_bps", 500)))
            return int(q["outAmount"]) / 1e9
        except Exception as e:
            log("price quote failed %s: %s" % (mint[:8], e))
        # FALLBACK: DexScreener fresh quote -> SOL. Logged as FALLBACK so a
        # bad fallback price can never silently drive an exit decision.
        px = self.ds_price_sol(mint)
        if px:
            log("FALLBACK price %s: %.9f SOL/token via DexScreener"
                % (mint[:8], px))
            return px
        log("FALLBACK price %s: no usable DexScreener quote" % mint[:8])
        return None

    def ds_price_sol(self, mint):
        """SOL per token via DexScreener's best-liquidity pair. Single shot,
        never raises: this is the racing leg for exits and the fallback for
        entries. None when unusable - callers must never exit on a stale
        price."""
        try:
            resp = requests.get(
                "https://api.dexscreener.com/latest/dex/tokens/" + mint,
                timeout=10).json()
        except Exception as e:
            log("DS price failed %s: %s" % (mint[:8], e))
            return None
        pairs = resp.get("pairs") or []
        if not pairs:
            return None
        best = max(pairs, key=lambda p: (
            p.get("liquidity") or {}).get("usd") or 0)
        try:
            usd = float(best.get("priceUsd") or 0)
        except (TypeError, ValueError):
            return None
        solusd = self.sol_usd()
        if usd > 0 and solusd and solusd > 0:
            return usd / solusd
        return None

    def price_sol_fast(self, mint, budget=4.0, headstart=0.8):
        """Exit-path SOL/token price: Jupiter races DexScreener.

        Jupiter gets `headstart` seconds alone, so the common case costs
        exactly one request, as today. If it hasn't answered, the
        DexScreener leg fires and the first valid price wins. Hard `budget`
        cap (default 4s) instead of the ~74s the patient retry chain could
        burn (3x20s timeouts + 2/4/8s backoff) - a stall that hit hardest
        during dumps, exactly when exits matter most. None when neither
        answers: callers must never exit on a stale price."""
        d = self.decimals(mint)  # cached; no network

        def jup_leg():
            q = jup_quote(mint, SOL_MINT, 10 ** d,
                          self.cfg["exit"].get("slippage_bps", 500),
                          timeout=5)
            return int(q["outAmount"]) / 1e9

        return _race_price(jup_leg, lambda: self.ds_price_sol(mint),
                           budget=budget, headstart=headstart)

    def price_native_fast(self, mint, chain):
        """Exit-path price with a hard latency budget (see price_sol_fast).
        BSC already single-shots DexScreener; only SOL gets the race."""
        if (chain or "solana") == "bsc":
            return self.price_bnb(mint)
        return self.price_sol_fast(mint)

    def venue_m5_dump(self, mint, chain="solana"):
        """DexScreener's own 5-minute price change for the best (highest
        liquidity) pair on the position's chain, or None when unavailable.
        This is the venue's tape - independent of our quote path. Used
        only as an exit tripwire; a missing/failed read never triggers
        an exit."""
        want = "bsc" if (chain or "solana") == "bsc" else "solana"
        try:
            resp = ds_get("/latest/dex/tokens/" + mint, self.cfg, timeout=10)
        except Exception:
            return None
        pairs = [p for p in (resp or {}).get("pairs") or []
                 if p.get("chainId") == want]
        if not pairs:
            return None
        best = max(pairs,
                   key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        try:
            return float((best.get("priceChange") or {}).get("m5"))
        except (TypeError, ValueError):
            return None

    def sol_usd(self):
        """SOL/USD for journal USD stamping. Cached 120s, fail-soft: never
        raises, never blocks trading - returns None when unreachable."""
        now = time.time()
        if now - getattr(self, "_sol_usd_ts", 0) < 120:
            return getattr(self, "_sol_usd", None)
        try:
            resp = requests.get("https://api.jup.ag/price/v3",
                                params={"ids": SOL_MINT}, timeout=10).json()
            px = float((resp.get(SOL_MINT) or {}).get("usdPrice") or 0)
            if px > 0:
                self._sol_usd, self._sol_usd_ts = px, now
                return px
        except Exception:
            pass
        return getattr(self, "_sol_usd", None)

    def bscswap(self):
        """Lazy PancakeSwap client. Keyless quotes work without a key file;
        the key is only loaded for live (non-dry-run) swaps. Returns None
        when BSC RPCs are unreachable - SOL trading is never affected.
        A failed construction retries with backoff (60s, doubling, 30min
        cap) instead of latching BSC off until the next restart."""
        if self._bscswap is None:
            now = time.time()
            if now < self._bsc_retry_at:
                return None
            self._bsc_down_logged = False  # new window: one log line allowed
            try:
                from bsc_swap import BscSwap
                key_file = ((self.cfg.get("wallets", {}) or {})
                            .get("bsc_key_file") or "").strip() or None
                self._bscswap = BscSwap(self.cfg.get("bsc_rpc_urls"),
                                        key_file=key_file)
                self._bsc_backoff_s = 60.0
            except Exception as e:
                self._bsc_retry_at = now + self._bsc_backoff_s
                self._bsc_backoff_s = min(self._bsc_backoff_s * 2, 1800.0)
                if not self._bsc_down_logged:
                    self._bsc_down_logged = True
                    log("BSC unavailable, BSC entries disabled "
                        "(retry in %ds): %s"
                        % (int(self._bsc_retry_at - now), e))
        return self._bscswap

    def bnb_usd(self):
        """BNB/USD via DexScreener WBNB. Cached 120s, fail-soft."""
        now = time.time()
        if now - getattr(self, "_bnb_usd_ts", 0) < 120:
            return getattr(self, "_bnb_usd", None)
        try:
            from bsc_swap import WBNB
            resp = ds_get("/latest/dex/tokens/" + WBNB, self.cfg, timeout=10)
            pairs = [p for p in (resp or {}).get("pairs") or []
                     if p.get("chainId") == "bsc"]
            if pairs:
                best = max(pairs,
                           key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
                px = float(best.get("priceUsd") or 0)
                if px > 0:
                    self._bnb_usd, self._bnb_usd_ts = px, now
                    return px
        except Exception:
            pass
        return getattr(self, "_bnb_usd", None)

    def price_bnb(self, mint):
        """BNB per token for a BSC token via DexScreener (best-liquidity
        pair). None when unavailable - callers must never exit on a
        stale price."""
        bnb = self.bnb_usd()
        if not bnb:
            return None
        try:
            resp = ds_get("/latest/dex/tokens/" + mint, self.cfg, timeout=10)
        except Exception:
            return None
        pairs = [p for p in (resp or {}).get("pairs") or []
                 if p.get("chainId") == "bsc"]
        if not pairs:
            return None
        best = max(pairs,
                   key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        try:
            usd = float(best.get("priceUsd") or 0)
        except (TypeError, ValueError):
            return None
        return usd / bnb if usd > 0 else None

    def price_native(self, mint, chain):
        """Native-currency price per token: SOL/token or BNB/token."""
        if (chain or "solana") == "bsc":
            return self.price_bnb(mint)
        return self.price_sol(mint)

    def native_usd(self, chain):
        """USD per native currency unit (SOL or BNB)."""
        if (chain or "solana") == "bsc":
            return self.bnb_usd()
        return self.sol_usd()

    def execute_swap(self, input_mint, output_mint, amount_raw, label):
        """Build, sign, send, confirm. Returns (out_amount_est, signature)."""
        slippage = self.cfg["exit"].get("slippage_bps", 500)
        quote = jup_quote(input_mint, output_mint, amount_raw, slippage)
        return self.send_quote(quote, label)

    def send_quote(self, quote, label):
        """Send an already-fetched + validated quote. Returns (out_est, sig)."""
        out = quote.get("outAmount")
        if self.dry_run:
            log("DRY %s: would swap (est out %s)" % (label, out))
            return out, "dryrun-%d" % int(time.time())
        max_fee = self.cfg["exit"].get("max_priority_fee_lamports", 2_000_000)
        raw = jup_swap_tx(quote, self.owner_str, max_fee)
        signed = VersionedTransaction(VersionedTransaction.from_bytes(raw).message,
                                      [self.keypair])
        sig = self.rpc.send_transaction(bytes(signed))
        log("%s tx sent: %s" % (label, sig))
        self.rpc.confirm(sig)
        log("%s confirmed: %s" % (label, sig))
        return out, sig

    def _kill_switch_tripped(self):
        """Read the current day's loss caps without changing state or logging."""
        r = self.cfg["risk"]
        st = self.state
        # kill switch: USD-denominated so it covers SOL and BSC together.
        # Enforce both configured limits; a USD limit must not mask the SOL one.
        # The USD cap is adaptive: min(fixed cap, daily_loss_pct * day-start
        # bankroll), so it tightens automatically as the bankroll shrinks.
        usd_cap = r.get("kill_switch_max_daily_loss_usd")
        eff_cap = money.effective_daily_loss_cap_usd(st, self.cfg, usd_cap)
        sol_cap = r.get("kill_switch_max_daily_loss_sol",
                        0.5 if usd_cap is None else None)
        return ((eff_cap is not None and
                 st.get("realized_usd", 0.0) <= -eff_cap) or
                (sol_cap is not None and st["realized_sol"] <= -sol_cap))

    def _drawdown_brake(self):
        """True while the bankroll drawdown brake is engaged (entries blocked,
        open positions tightened). Hysteresis: engages at max_drawdown_pct,
        releases below drawdown_resume_pct."""
        return money.entries_blocked_by_drawdown(self.state, self.cfg)

    def _risk_halted(self):
        """Either the daily kill switch or the drawdown brake is tripped."""
        return self._kill_switch_tripped() or self._drawdown_brake()

    def _bankroll_ticket(self, configured_native, native_usd):
        """Cap a trade's native ticket at the bankroll-derived USD ceiling.

        ticket_usd = risk_per_trade_pct * bankroll / hard_stop_pct, so a
        full hard-stop loss costs exactly risk_per_trade_pct of bankroll.
        Never raises size above the configured fixed ticket; falls back to
        the configured size when no USD rate is available.
        """
        try:
            hard = (self.cfg.get("exit") or {}).get("hard_stop_pct", 35)
            ceil_usd = money.ticket_usd_ceiling(self.state, self.cfg, hard)
            if ceil_usd and native_usd and native_usd > 0:
                return min(configured_native, ceil_usd / native_usd)
        except Exception:
            pass
        return configured_native

    def _risk_ceiling_native(self, native_usd):
        """money.py risk ceiling in native units, or None if unavailable."""
        try:
            hard = (self.cfg.get("exit") or {}).get("hard_stop_pct", 35)
            ceil_usd = money.ticket_usd_ceiling(self.state, self.cfg, hard)
            if ceil_usd and native_usd and native_usd > 0:
                return ceil_usd / native_usd
        except Exception:
            pass
        return None

    def _allocate(self, signal, chain, configured_native, native_usd,
                  entry, holder=None):
        """Allocator decision at entry commit, after _entry_commit_ok().

        Returns (buy_native, journal_fields, skip). The 1.0x ticket is the
        existing _bankroll_ticket(). Shadow mode always trades that ticket
        and only journals what the allocator would have done; live mode
        applies take/skip and min(configured * multiplier, risk ceiling).
        No `allocator` config section -> off (no fields, legacy behavior).
        Pure local arithmetic: native_usd is the rate already fetched for
        bankroll sizing, never a new fetch."""
        base = self._bankroll_ticket(configured_native, native_usd)
        acfg = self.cfg.get("allocator")
        mode = allocator.mode_of(acfg)
        if mode == "off":
            return base, None, False
        try:
            feats = allocator.features_from_signal(
                signal, chain, entry, native_usd, holder, time.time())
            balance = {"bankroll_usd": self.state.get("bankroll_usd"),
                       "equity_peak_usd": self.state.get("equity_peak_usd"),
                       "max_drawdown_pct": (self.cfg.get("money") or {}).get(
                           "max_drawdown_pct", 15.0)}
            take, score, mult, bfactor = allocator.score_signal_with_balance(
                feats, balance, acfg)
            error = False
        except Exception:
            (take, score, mult), error = allocator.FALLBACK, True
            bfactor = 1.0
        try:
            sized = allocator.size_native(
                configured_native, mult,
                self._risk_ceiling_native(native_usd))
        except Exception:
            (take, score, mult), error = allocator.FALLBACK, True
            bfactor = 1.0
            sized = base
        fields = {"allocator_mode": mode,
                  "allocator_score": round(score, 6),
                  "allocator_take": bool(take),
                  "allocator_multiplier": round(mult, 6),
                  "allocator_balance_factor": round(bfactor, 6),
                  "allocator_base_native": base,
                  "allocator_would_be_native": sized if take else 0.0}
        if error:
            fields["allocator_error"] = True
        if mode != "live":
            return base, fields, False
        if not take:
            return None, fields, True
        fields["allocator_applied_native"] = sized
        return sized, fields, False

    # -- entries --
    def _dump_cooldown_active(self, signal):
        closed_at = self.state.get("dump_cooldown", {}).get(signal.get("mint"))
        if closed_at is None:
            return False
        if 0 <= time.time() - closed_at < 24 * 60 * 60:
            log("DUMP-COOLDOWN SKIP %s" % signal["name"])
            return True
        return False

    def _effective_max_trades_per_day(self):
        # Profit-boost rule (user-set 2026-09-27): once the day's locked
        # profit reaches the threshold, the daily trade cap lifts from the
        # base to the boosted value. Hourly cap and all other guardrails
        # still apply.
        r = self.cfg["risk"]
        base = r.get("max_trades_per_day", 10)
        thr = r.get("profit_boost_threshold_usd", 10.0)
        boost = r.get("profit_boost_trades_per_day", 40)
        if (self.state.get("realized_usd") or 0.0) >= thr:
            return boost
        return base

    def guardrails_ok(self, signal):
        r = self.cfg["risk"]
        self._roll_day()
        st = self.state
        if self._kill_switch_tripped():
            log("KILL SWITCH: daily loss limit hit, no new entries")
            return False
        if self._drawdown_brake():
            log("DRAWDOWN BRAKE: %.1f%% from equity peak, no new entries"
                % money.drawdown_pct(st))
            return False
        if self._dump_cooldown_active(signal):
            return False
        if len(st["positions"]) >= r.get("max_open_positions", 3):
            return False
        max_day = self._effective_max_trades_per_day()
        if len(st["trades_today"]) >= max_day:
            log("max trades/day reached (%d)" % max_day)
            return False
        # hourly pacing: rolling 60-min window, resets itself automatically
        if len(self._prune_hour_trades()) >= r.get("max_trades_per_hour", 3):
            log("max trades/hour reached")
            return False
        cd = st["cooldown"].get(signal["mint"], 0)
        if time.time() - cd < r.get("cooldown_min_per_mint", 120) * 60:
            return False
        if signal["mint"] in st["positions"]:
            return False
        need = r.get("buy_sol_per_trade", 0.1) + 0.02
        if not self.dry_run and self.rpc.get_sol_balance(self.owner) < need:
            log("insufficient SOL for trade")
            return False
        return True

    # -- rug guard: fail-closed on-chain safety screen before any buy --
    def rug_check(self, mint, name, evidence=None):
        g = self.cfg["hunter"].get("rug_guard", {})
        if not g.get("enabled", True):
            return True
        tag = name or mint[:8]
        # 1. mint & freeze authorities must be disabled (else dev can
        #    print infinite tokens or freeze your bag)
        r = self.rpc.call("getAccountInfo", [mint, {"encoding": "base64"}],
                          timeout=20, retries=2)
        try:
            data = base64.b64decode(r["result"]["value"]["data"][0])
            if len(data) < 82:
                raise ValueError("mint account too short")
            mint_opt = int.from_bytes(data[0:4], "little")
            freeze_opt = int.from_bytes(data[46:50], "little")
        except Exception as e:
            log("RUG-GUARD SKIP %s: mint account unreadable (%s)" % (tag, e))
            return False  # fail closed: can't verify = no trade
        if g.get("check_mint_authority", True) and mint_opt != 0:
            log("RUG-GUARD SKIP %s: mint authority still enabled" % tag)
            return False
        if g.get("check_freeze_authority", True) and freeze_opt != 0:
            log("RUG-GUARD SKIP %s: freeze authority still enabled" % tag)
            return False
        # 2. holder concentration (dev wallets ready to dump)
        try:
            lh = self.rpc.call("getTokenLargestAccounts", [mint],
                               timeout=20, retries=2)
            accts = (lh.get("result") or {}).get("value") or []
            sup = self.rpc.call("getTokenSupply", [mint],
                                timeout=20, retries=2)
            supply = int((sup.get("result") or {}).get("value", {})
                         .get("amount") or 0)
            if supply <= 0 or not accts:
                log("RUG-GUARD SKIP %s: holder concentration unverifiable"
                    % tag)
                return False
            amts = sorted([int(a.get("amount", 0)) for a in accts],
                          reverse=True)
            top1 = amts[0] / supply * 100
            top5 = sum(amts[:5]) / supply * 100
            if top1 > g.get("max_top_holder_pct", 40):
                log("RUG-GUARD SKIP %s: top holder owns %.1f%%"
                    % (tag, top1))
                return False
            if top5 > g.get("max_top5_holder_pct", 75):
                log("RUG-GUARD SKIP %s: top 5 holders own %.1f%%"
                    % (tag, top5))
                return False
            if evidence is not None:
                evidence.update(holder_top1_pct=top1, holder_top5_pct=top5)
        except Exception as e:
            log("RUG-GUARD SKIP %s: holder check failed (%s)" % (tag, e))
            return False  # fail closed
        return True

    def honeypot_check(self, mint, name, tokens_raw):
        """Can holders actually sell? No Jupiter token->SOL route = trap.

        A single flaky quote must not flip the verdict: retry transient
        transport errors first, then fail closed either way - but log
        whether the API was unreachable or the route genuinely doesn't
        exist, so the two cases never look identical again."""
        g = self.cfg["hunter"].get("rug_guard", {})
        if not g.get("honeypot_quote_check", True):
            return True
        net_fail, rout = False, 0
        try:
            rq = quote_with_retry(
                "honeypot %s" % name,
                lambda: jup_quote(mint, SOL_MINT, tokens_raw,
                                  self.cfg["exit"].get("slippage_bps", 500)))
            rout = int(rq.get("outAmount") or 0)
        except Exception as e:
            net_fail = True
            log("RUG-GUARD %s: sell-route quote unreachable after retries "
                "(%s) - failing closed" % (name, e))
        if rout <= 0:
            if not net_fail:
                log("RUG-GUARD SKIP %s: no sell route on Jupiter (honeypot)"
                    % name)
            maybe_capture_failed_quote(
                self.cfg, self.state_path, mint, "solana",
                "honeypot_quote_unreachable" if net_fail else "honeypot_no_sell_route",
                {"name": name, "sell_in_amount_raw": tokens_raw,
                 "sell_out_amount_raw": rout})
            return False
        return True

    def log_signal(self, signal):
        """Alert-only logging for chains whose swaps aren't wired yet
        (BSC): prints the same FOMO SIGNAL / PRE-PUMP lines the entry path
        would, so the phone alert watcher picks them up."""
        name = signal["name"]
        chain = signal.get("chain", "solana")
        if signal.get("early"):
            log("PRE-PUMP [%s]: %s %s +%s%% m5 / +%s%% m15, buys/sells %s "
                "(liq $%s) %s"
                % (chain, name, signal.get("window_label", "5m"),
                   signal.get("m5_gain_pct"), signal["m15_gain_pct"],
                   signal["buy_sell_ratio"], signal["liquidity_usd"],
                   signal["pool_url"]))
        else:
            log("FOMO SIGNAL [%s]: %s +%s%% %s, buys/sells %s (liq $%s) [%s] %s"
                % (chain, name, signal["m15_gain_pct"],
                   signal.get("window_label", "15m"),
                   signal["buy_sell_ratio"], signal["liquidity_usd"],
                   signal.get("source", "geckoterminal"),
                   signal["pool_url"]))

    def _entry_commit_ok(self, signal):
        """Commit-time guardrails, checked under the lock right before a
        position is written to state. The scan loop's guardrails_ok() runs
        minutes earlier - rug screens, pullback waits, decimals lookups and
        entry quotes all happen in between, and another manage thread can
        close a losing position or the signal can fade in that time."""
        name = signal["name"]
        if self._dump_cooldown_active(signal):
            return False
        r = self.cfg["risk"]
        hour_trades = self._prune_hour_trades()
        if (len(self.state["trades_today"])
                >= self._effective_max_trades_per_day()
                or len(hour_trades)
                >= r.get("max_trades_per_hour", 3)):
            log("cap reached at entry commit, skipping %s" % name)
            _maybe_capture_failure(self, signal, "commit_cap",
                                   open_positions=len(self.state["positions"]),
                                   trades_today=len(self.state["trades_today"]),
                                   trades_this_hour=len(hour_trades))
            return False
        if self._risk_halted():
            log("RISK HALT tripped at entry commit, skipping %s" % name)
            _maybe_capture_failure(self, signal, "commit_kill_switch")
            return False
        entry_cfg = (self.cfg.get("hunter") or {}).get("entry") or {}
        try:
            configured_age = entry_cfg.get("signal_max_age_sec", 180)
            if isinstance(configured_age, bool):
                raise ValueError("boolean signal age")
            max_age = float(configured_age)
            if not math.isfinite(max_age) or max_age <= 0:
                max_age = 180
        except (TypeError, ValueError):
            max_age = 180
        try:
            age = time.time() - float(signal["ts"])
            if not math.isfinite(age) or age < 0:
                raise ValueError("invalid signal timestamp")
        except (KeyError, TypeError, ValueError):
            log("STALE SIGNAL SKIP %s: missing or invalid scan timestamp" % name)
            return False
        if age > max_age:
            return self._stale_signal_still_strong(signal, age)
        return True

    def _stale_signal_still_strong(self, signal, age):
        """One rate-limited pool fetch for an aged entry, under commit lock."""
        name = signal["name"]
        pool = signal.get("pool")
        chain = signal.get("chain", "solana")
        source = signal.get("source", "geckoterminal")
        if not pool or chain not in CHAINS:
            log("STALE SIGNAL SKIP %s: pool or chain missing" % name)
            return False
        try:
            if source == "dexscreener" and chain == "solana":
                # One attempt: ds_get's normal transport retry would make
                # this commit-time check fetch the pool twice.
                data = ds_get("/latest/dex/pairs/solana/%s" % pool,
                              self.cfg, timeout=10, _retried=True)
                pairs = (data or {}).get("pairs") or []
                p = next(p for p in pairs
                         if p.get("chainId") == "solana"
                         and p.get("pairAddress") == pool)
                gain = float((p.get("priceChange") or {})["m5"])
                liq = float((p.get("liquidity") or {})["usd"])
                tx = (p.get("txns") or {})["m5"]
                bars = (self.cfg["hunter"].get("dexscreener") or {})
                min_gain = bars.get("min_m5_gain_pct", 5)
                min_liq = bars.get("min_liquidity_usd", 0)
                min_ratio = bars.get("min_buy_sell_ratio", 1.5)
                window = "5m"
            elif source == "geckoterminal":
                data = gt_get("/networks/%s/pools/%s" % (chain, pool),
                              self.cfg, timeout=10)
                item = (data or {})["data"]
                a = item["attributes"]
                if str(a.get("address", "")).lower() != pool.lower():
                    raise ValueError("pool mismatch")
                gain = float((a.get("price_change_percentage") or {})["m15"])
                liq = float(a["reserve_in_usd"])
                tx = (a.get("transactions") or {})["m15"]
                bars = ((self.cfg["hunter"].get("early") or {})
                        if signal.get("early") else self.cfg["hunter"])
                min_gain = bars.get("min_m15_gain_pct", 15 if signal.get("early") else 100)
                min_liq = bars.get("min_liquidity_usd", 8000 if signal.get("early") else 15000)
                min_ratio = bars.get("min_buy_sell_ratio", 1.5 if signal.get("early") else 10.0)
                window = "15m"
            else:
                raise ValueError("unsupported signal source")
            buys = int(tx["buys"])
            sells = int(tx["sells"])
            if (not math.isfinite(gain) or not math.isfinite(liq)
                    or buys < 0 or sells <= 0):
                raise ValueError("invalid pool stats")
            ratio = buys / sells
        except Exception as e:
            log("STALE SIGNAL SKIP %s: re-fetch failed (%s)"
                % (name, type(e).__name__))
            return False
        if gain < min_gain or liq < min_liq or ratio < min_ratio:
            log("STALE SIGNAL SKIP %s: age %.0fs, %s gain %.1f%% (min %s%%), "
                "liquidity $%.0f (min $%s), buy/sell %.2f (min %s)"
                % (name, age, window, gain, min_gain, liq, min_liq,
                   ratio, min_ratio))
            return False
        return True

    def enter(self, signal):
        """Threaded entry: rug screen, then wait for a pullback so we never
        market-buy the top of the spike. Skips instead of chasing."""
        if signal.get("chain", "solana") == "bsc":
            return self.enter_bsc(signal)
        r = self.cfg["risk"]
        # The configured ticket sizes the entry quote. The bankroll cap is
        # applied at commit time (after _entry_commit_ok) so a rejected
        # commit never pays for a USD rate fetch.
        cfg_buy = r.get("buy_sol_per_trade", 0.1)
        mint = signal["mint"]
        name = signal["name"]
        max_open = r.get("max_open_positions", 3)
        with self.lock:
            if (len(self.state["positions"]) + len(self.pending_entries)
                    >= max_open):
                return
            if mint in self.state["positions"] or mint in self.pending_entries:
                return
            self.pending_entries.add(mint)
        try:
            log("FOMO SIGNAL [%s]: %s +%s%% %s, buys/sells %s (liq $%s) [%s]"
                % (signal.get("chain", "solana"), name, signal["m15_gain_pct"],
                   signal.get("window_label", "15m"),
                   signal["buy_sell_ratio"], signal["liquidity_usd"],
                   signal.get("source", "geckoterminal")))
            if signal.get("early"):
                log("PRE-PUMP: %s m5 +%s%%, m15 +%s%% (move still "
                    "accelerating) %s"
                    % (name, signal.get("m5_gain_pct"),
                       signal["m15_gain_pct"], signal["pool_url"]))
            holder_evidence = {}
            if not self.rug_check(mint, name, holder_evidence):
                return
            # anti-top: wait for a dip off the signal price (disabled when
            # entry.pullback_pct is 0, i.e. immediate market entry)
            ecfg = self.cfg["hunter"].get("entry", {})
            pb_pct = ecfg.get("pullback_pct", 0) or 0
            wait = ecfg.get("max_wait_sec", 0) or 0
            ref = None  # only set when pullback waiting is enabled
            if pb_pct > 0 and wait > 0:
                ref = self.price_sol(mint)
                if not ref:
                    log("SKIP %s: no reference price" % name)
                    return
                target = ref * (1 - pb_pct / 100.0)
                log("%s: signal @ %.9f, waiting %.1f%% pullback (up to %ds)"
                    % (name, ref, pb_pct, wait))
                t0, dipped = time.time(), False
                while time.time() - t0 < wait:
                    time.sleep(3)
                    px = self.price_sol(mint)
                    if px and px <= target:
                        dipped = True
                        break
                    if px and px > ref * 1.15:
                        log("SKIP %s: ripped +15%% past signal, not chasing"
                            % name)
                        return
                if not dipped:
                    log("SKIP %s: no pullback in %ds, not chasing top"
                        % (name, wait))
                    return
            amount_lamports = int(cfg_buy * 1e9)
            d = self.decimals(mint)  # fail fast before touching the swap
            try:
                quote = jup_quote(SOL_MINT, mint, amount_lamports,
                                  self.cfg["exit"].get("slippage_bps", 500))
            except Exception as e:
                # Jupiter rejects some mints outright (400/404/no route).
                # One clean line instead of a traceback; entry simply
                # doesn't happen (fail closed).
                log("SKIP %s: entry quote failed (%s: %s)"
                    % (name, type(e).__name__, str(e)[:90]))
                _maybe_capture_failure(self, signal, "entry_quote_failed",
                                       in_amount_raw=amount_lamports,
                                       error_type=type(e).__name__)
                return
            tokens_raw = int(quote.get("outAmount") or 0)
            if tokens_raw < 10_000 * (10 ** d):
                # dust quote = Jupiter found no liquid route; buying would
                # execute at an absurd price. Skip instead of "buying" air.
                log("SKIP %s: no liquid route (quote out=%s raw units)"
                    % (name, quote.get("outAmount")))
                _maybe_capture_failure(self, signal, "dust_quote",
                                       in_amount_raw=amount_lamports,
                                       out_amount_raw=tokens_raw)
                return
            if not self.honeypot_check(mint, name, tokens_raw):
                return
            out_est, sig = self.send_quote(quote, "BUY %s" % name)
            # entry price: SOL spent per token received (use quote estimate)
            tokens_est = int(out_est) / (10 ** d) if out_est else 0
            entry = cfg_buy / tokens_est if tokens_est else self.price_sol(mint)
            with self.lock:
                # Commit-time guardrails (trade caps + kill switch), rechecked
                # under the lock minutes after guardrails_ok() ran.
                if not self._entry_commit_ok(signal):
                    self.pending_entries.discard(mint)
                    return
                # Bankroll ticket sizing lands here, on the commit path only:
                # the USD rate is fetched only when the entry actually
                # commits. A shrunken ticket scales the quoted token amount
                # linearly; the per-token entry price is unchanged. (Live
                # mode is disabled; the pre-lock quote still executes at the
                # configured size there.) The allocator is an additional
                # filter after the commit guardrails; the risk ceiling wins.
                buy_sol, alloc_rec, alloc_skip = self._allocate(
                    signal, "solana", cfg_buy, self.sol_usd(), entry,
                    holder_evidence)
                if alloc_skip:
                    log("ALLOCATOR SKIP %s score=%.3f"
                        % (name, alloc_rec["allocator_score"]))
                    self.pending_entries.discard(mint)
                    return
                # Positive-slip (chase) veto: user-ordered 2026-09-28. Vetoes
                # entries filling above the signal print; fail-open when slip
                # cannot be measured. Vetoed entries are journaled as
                # slip_veto events for retrospective validation.
                if self._slip_veto(signal, entry, self.sol_usd(), "solana"):
                    self.pending_entries.discard(mint)
                    return
                scale = (buy_sol / cfg_buy) if cfg_buy else 1.0
                self.state["positions"][mint] = {
                    "name": name, "entry": entry, "peak": entry,
                    "buy_sol": buy_sol, "buy_sig": sig,
                    "rungs_fired": [], "opened_at": time.time(),
                    "last_price_ts": time.time(),
                    # virtual ledger (dry_run): exact paper accounting
                    "tokens_raw": int(tokens_raw * scale), "sold_sol": 0.0,
                    "decimals": d,
                }
                self.state["cooldown"][mint] = time.time()
                self.state["trades_today"].append(time.time())
                self.state["trades_this_hour"].append(time.time())
                self.pending_entries.discard(mint)
                self.save()
            native_usd = self.sol_usd()
            enrichment = entry_evidence(signal, entry, native_usd,
                                        holder_evidence)
            maybe_tag_research(enrichment, self.cfg)
            with self.lock:
                self.state["positions"][mint].update(enrichment)
                self.save()
            self._journal(journal.finalize_entry_record({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "ts_epoch": time.time(),
                "type": "entry",
                "mint": mint, "name": name, "entry": entry,
                "buy_sol": buy_sol, "buy_sig": sig,
                "sol_usd": native_usd,
                "signal_gain_pct": signal.get("m15_gain_pct"),
                "signal_ratio": signal.get("buy_sell_ratio"),
                "liquidity_usd": signal.get("liquidity_usd"),
                "source": signal.get("source"),
                "window": signal.get("window_label"),
                **enrichment, **(alloc_rec or {})}, signal))
            log("entered %s @ %.9f SOL/token (%.1f%% under signal)"
                % (name, entry or 0,
                   (1 - (entry or ref) / ref) * 100 if ref else 0))
            t = threading.Thread(target=self.manage, args=(mint,), daemon=True)
            t.start()
        except Exception:
            log("BUY FAILED for %s:\n%s" % (name, traceback.format_exc()))
        finally:
            with self.lock:
                self.pending_entries.discard(mint)

    def enter_bsc(self, signal):
        """BSC entry via PancakeSwap V2. Mirrors enter(): honeypot screen
        (buy+sell round-trip simulation instead of the Solana mint/freeze
        authority check), optional pullback wait, dust-quote rejection,
        then a paper buy on the virtual ledger (live swaps stay dormant
        while dry_run=true)."""
        r = self.cfg["risk"]
        # Configured ticket sizes the entry quote; the bankroll cap is
        # applied at commit time so a rejected commit never pays for a USD
        # rate fetch.
        cfg_buy = r.get("buy_bnb_per_trade", 0.01)
        mint = signal["mint"]
        name = signal["name"]
        max_open = r.get("max_open_positions", 3)
        with self.lock:
            if (len(self.state["positions"]) + len(self.pending_entries)
                    >= max_open):
                return
            if mint in self.state["positions"] or mint in self.pending_entries:
                return
            self.pending_entries.add(mint)
        try:
            log("FOMO SIGNAL [bsc]: %s +%s%% %s, buys/sells %s (liq $%s) [%s]"
                % (name, signal["m15_gain_pct"],
                   signal.get("window_label", "15m"),
                   signal["buy_sell_ratio"], signal["liquidity_usd"],
                   signal.get("source", "geckoterminal")))
            bsc = self.bscswap()
            if bsc is None:
                log("SKIP %s: BSC unavailable" % name)
                return
            amount_wei = int(cfg_buy * 1e18)
            ok, why = bsc.honeypot_check(mint, name, amount_wei)
            if not ok:
                log("HONEYPOT SKIP %s: %s" % (name, why))
                _maybe_capture_failure(self, signal, "bsc_honeypot",
                                       in_amount_raw=amount_wei,
                                       screen_reason=why)
                return
            ecfg = self.cfg["hunter"].get("entry", {})
            # LP burn screen (hunter.entry.verify_lp_lock): False/"off"
            # (default) = screen disabled, no RPC traffic; "measure" =
            # run the check and journal the verdict but never skip (builds
            # the prospective dataset needed to validate the gate);
            # True/"enforce" = skip the entry on a measured "unlocked".
            # Only a measured "unlocked" skips; "no_pair"/"unknown" fail open.
            lp_fields = None
            lp_mode = ecfg.get("verify_lp_lock", False)
            if lp_mode and lp_mode != "off":
                try:
                    st = bsc.lp_lock_status(
                        mint, ecfg.get("lp_burn_threshold_pct", 50.0))
                except Exception as e:
                    st = {"verdict": "unknown", "error": type(e).__name__}
                if not isinstance(st, dict):
                    st = {"verdict": "unknown", "error": "bad lp status"}
                lp_fields = journal.lp_lock_fields(st)
                if st.get("verdict") == "unlocked" and lp_mode in (True, "enforce"):
                    log("LP-LOCK SKIP %s: LP burn %.1f%% (pair %s)"
                        % (name, st.get("burn_pct") or 0.0, st.get("pair")))
                    try:
                        journal.journal_event(
                            os.path.dirname(os.path.abspath(self.state_path)),
                            journal.sanitize_lp_lock_skip(signal, st))
                    except Exception:
                        pass
                    return
                if st.get("verdict") != "burned":
                    if lp_mode == "measure":
                        log("LP-LOCK MEASURE %s: verdict %s (burn %.1f%%), "
                            "journaled, entry proceeds"
                            % (name, st.get("verdict"),
                               st.get("burn_pct") or 0.0))
                    else:
                        log("LP-LOCK GAP %s: verdict %s%s, proceeding (fail open)"
                            % (name, st.get("verdict"),
                               (" (%s)" % st["error"]) if st.get("error") else ""))
            # anti-top: wait for a dip off the signal price (disabled when
            # entry.pullback_pct is 0, i.e. immediate market entry)
            pb_pct = ecfg.get("pullback_pct", 0) or 0
            wait = ecfg.get("max_wait_sec", 0) or 0
            ref = None
            if pb_pct > 0 and wait > 0:
                ref = self.price_bnb(mint)
                if not ref:
                    log("SKIP %s: no reference price" % name)
                    return
                target = ref * (1 - pb_pct / 100.0)
                log("%s: signal @ %.9f, waiting %.1f%% pullback (up to %ds)"
                    % (name, ref, pb_pct, wait))
                t0, dipped = time.time(), False
                while time.time() - t0 < wait:
                    time.sleep(3)
                    px = self.price_bnb(mint)
                    if px and px <= target:
                        dipped = True
                        break
                    if px and px > ref * 1.15:
                        log("SKIP %s: ripped +15%% past signal, not chasing"
                            % name)
                        return
                if not dipped:
                    log("SKIP %s: no pullback in %ds, not chasing top"
                        % (name, wait))
                    return
            try:
                d = bsc.token_decimals(mint)
            except Exception as e:
                log("SKIP %s: token unreadable (%s: %s)"
                    % (name, type(e).__name__, str(e)[:80]))
                return
            try:
                tokens_raw = int(bsc.quote_buy(mint, amount_wei))
            except Exception as e:
                log("SKIP %s: entry quote failed (%s: %s)"
                    % (name, type(e).__name__, str(e)[:90]))
                _maybe_capture_failure(self, signal, "entry_quote_failed",
                                       in_amount_raw=amount_wei,
                                       error_type=type(e).__name__)
                return
            if tokens_raw < 10_000 * (10 ** d):
                log("SKIP %s: no liquid route (quote out=%d raw units)"
                    % (name, tokens_raw))
                _maybe_capture_failure(self, signal, "dust_quote",
                                       in_amount_raw=amount_wei,
                                       out_amount_raw=tokens_raw)
                return
            tokens_est = tokens_raw / (10 ** d)
            entry = cfg_buy / tokens_est if tokens_est else self.price_bnb(mint)
            sig = signal.get("pool_url", "")
            with self.lock:
                # Commit-time guardrails (trade caps + kill switch), rechecked
                # under the lock minutes after guardrails_ok() ran.
                if not self._entry_commit_ok(signal):
                    self.pending_entries.discard(mint)
                    return
                # Bankroll ticket sizing lands here, on the commit path only:
                # the USD rate is fetched only when the entry actually
                # commits. A shrunken ticket scales the quoted token amount
                # linearly; the per-token entry price is unchanged. (Live
                # mode is disabled; the pre-lock quote still executes at the
                # configured size there.) The allocator is an additional
                # filter after the commit guardrails; the risk ceiling wins.
                buy_bnb, alloc_rec, alloc_skip = self._allocate(
                    signal, "bsc", cfg_buy, self.bnb_usd(), entry)
                if alloc_skip:
                    log("ALLOCATOR SKIP %s score=%.3f"
                        % (name, alloc_rec["allocator_score"]))
                    self.pending_entries.discard(mint)
                    return
                buy_bnb, capped, slip = wipe_drift_cap_native(
                    signal, entry, self.bnb_usd(), cfg_buy, buy_bnb)
                # Positive-slip (chase) veto: user-ordered 2026-09-28. Runs
                # before the drift cap: a vetoed entry never reaches it, so
                # with the veto enabled the cap only fires when the veto is
                # toggled off. Fail-open when slip cannot be measured.
                if self._slip_veto(signal, entry, self.bnb_usd(), "bsc",
                                   slip=slip):
                    self.pending_entries.discard(mint)
                    return
                if capped:
                    if alloc_rec is None:
                        alloc_rec = {}
                    alloc_rec["wipe_drift_cap"] = True
                    alloc_rec["wipe_drift_slip"] = round(slip, 6)
                    log("WIPE-DRIFT CAP %s: slip %+.2f%% -> ticket capped at 0.5x floor"
                        % (name, slip * 100))
                scale = (buy_bnb / cfg_buy) if cfg_buy else 1.0
                self.state["positions"][mint] = {
                    "name": name, "chain": "bsc",
                    "entry": entry, "peak": entry,
                    "buy_sol": buy_bnb, "buy_sig": sig,
                    "rungs_fired": [], "opened_at": time.time(),
                    "last_price_ts": time.time(),
                    # virtual ledger (dry_run): exact paper accounting
                    "tokens_raw": int(tokens_raw * scale), "sold_sol": 0.0,
                    "decimals": d,
                }
                self.state["cooldown"][mint] = time.time()
                self.state["trades_today"].append(time.time())
                self.state["trades_this_hour"].append(time.time())
                self.pending_entries.discard(mint)
                self.save()
            if self.dry_run:
                log("DRY bsc buy %s: %d wei BNB -> %d token raw"
                    % (name, int(buy_bnb * 1e18), int(tokens_raw * scale)))
            else:
                # live: the entry quote above was indicative; this sends it.
                # In paper mode the quote IS the fill (virtual ledger).
                bsc.execute_buy(mint, amount_wei,
                                self.cfg["exit"].get("slippage_bps", 500),
                                dry_run=False)
            native_usd = self.bnb_usd()
            enrichment = entry_evidence(signal, entry, native_usd)
            maybe_tag_research(enrichment, self.cfg)
            with self.lock:
                self.state["positions"][mint].update(enrichment)
                self.save()
            self._journal(journal.finalize_entry_record({
                **(lp_fields or {}),
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "ts_epoch": time.time(),
                "type": "entry",
                "mint": mint, "name": name, "chain": "bsc",
                "entry": entry, "buy_sol": buy_bnb, "buy_sig": sig,
                "sol_usd": native_usd,
                "signal_gain_pct": signal.get("m15_gain_pct"),
                "signal_ratio": signal.get("buy_sell_ratio"),
                "liquidity_usd": signal.get("liquidity_usd"),
                "source": signal.get("source"),
                "window": signal.get("window_label"),
                **enrichment, **(alloc_rec or {})}, signal))
            log("entered %s @ %.9f BNB/token (%.1f%% under signal)"
                % (name, entry or 0,
                   (1 - (entry or ref) / ref) * 100 if ref else 0))
            t = threading.Thread(target=self.manage, args=(mint,), daemon=True)
            t.start()
        except Exception:
            log("BUY FAILED for %s:\n%s" % (name, traceback.format_exc()))
        finally:
            with self.lock:
                self.pending_entries.discard(mint)

    # -- exits: tight sell loop per position --
    def sell_pct_of_balance(self, mint, pct, reason):
        """Sell pct% of the position's token balance.
        Returns (sig, proceeds_native) on success, (None, 0.0) on failure.
        proceeds is quoted/simulated native currency received (SOL or BNB)."""
        bal = self.token_balance_raw(mint)
        if bal <= 0:
            return None, 0.0
        amt = bal * pct // 100  # integer math: float division loses
        # precision on huge raw amounts (BSC 18-decimal tokens) and a
        # "sell 100%" would leave dust behind, blocking close_trade.
        if amt <= 0:
            return None, 0.0
        pos = self.state["positions"][mint]
        name = pos["name"]
        chain = pos.get("chain", "solana")
        label = "SELL %s (%s)" % (name, reason)
        try:
            if chain == "bsc":
                bsc = self.bscswap()
                if bsc is None:
                    raise RuntimeError("BSC unavailable")
                out_est, sig = bsc.execute_sell(
                    mint, amt, self.cfg["exit"].get("slippage_bps", 500),
                    dry_run=self.dry_run)
                proceeds = int(out_est) / 1e18 if out_est else 0.0
            else:
                out_est, sig = self.execute_swap(mint, SOL_MINT, amt, label)
                proceeds = int(out_est) / 1e9 if out_est else 0.0
            if self.dry_run and out_est:
                # paper mode: settle the virtual ledger from the quote -
                # rung sells really shrink the bag now.
                with self.lock:
                    p = self.state["positions"].get(mint)
                    if p is not None:
                        p["tokens_raw"] = max(
                            0, p.get("tokens_raw", 0) - amt)
                        p["sold_sol"] = round(
                            p.get("sold_sol", 0.0) + proceeds, 9)
                        self.save()
            return sig, proceeds
        except Exception:
            log("SELL FAILED %s (%s):\n%s" % (name, reason, traceback.format_exc()))
            return None, 0.0

    def manage(self, mint):
        ex = self.cfg["exit"]
        poll = ex.get("sell_poll_sec", 5)
        mode = ex.get("mode", "ladder")
        if mode == "moonbag":
            # moonshot mode: bank half the stake at +100%, then let the
            # rest ride with a wide trailing stop and no take-profit ceiling.
            tps = [[100, 50]]
            trail = ex.get("moonbag_trailing_stop_pct", 50)
        else:
            tps = ex.get("take_profits", [[100, 50], [200, 25]])
            trail = ex.get("trailing_stop_pct", 30)
        hard = ex.get("hard_stop_pct", 40)
        stale_min = ex.get("stale_exit_min", 45)
        stale_gain = ex.get("stale_exit_max_gain_pct", 10)
        log("managing %s [%s]: TP %s | trail %s%% | hard stop %s%% | "
            "dump -%s%%/%ss | venue m5 %s%%/%ss | stale %sm/<%s%% | poll %ss"
            % (mint[:8], mode, tps, trail, hard,
               ex.get("dump_drop_pct", 12), ex.get("dump_window_sec", 60),
               ex.get("venue_dump_m5_pct", -30),
               ex.get("venue_check_sec", 30),
               stale_min, stale_gain, poll))
        while True:
            time.sleep(poll)
            try:
                if self._manage_once(mint, tps, trail, hard):
                    return
            except Exception:
                # a manage thread must NEVER die silently - log it and keep
                # watching the position.
                log("MANAGE ERROR %s (thread kept alive):\n%s"
                    % (mint[:8], traceback.format_exc()))

    def _manage_once(self, mint, tps, trail, hard):
        """One exit-check cycle. Returns True when the position is gone."""
        ex = self.cfg["exit"]
        with self.lock:
            self._roll_day()
            pos = self.state["positions"].get(mint)
            if pos and self._risk_halted() and not pos.get("protect_mode"):
                # Keep protection through a day rollover: an open position's
                # exit must never loosen just because the daily cap resets.
                pos["protect_mode"] = True
                self.save()
                log("%s: PROTECT MODE - daily loss kill switch tripped"
                    % pos["name"])
        if not pos:
            return True
        price = self.price_native_fast(mint, pos.get("chain", "solana"))
        if capture_settings(self.cfg)[0]:
            chain = pos.get("chain", "solana")
            # native_usd() can refresh over the network. Read its normal
            # cached rate directly; capture must never add a request.
            fx_key = "_bnb_usd" if chain == "bsc" else "_sol_usd"
            fx_ts = getattr(self, fx_key + "_ts", 0)
            fresh_fx = 0 <= time.time() - fx_ts < 120
            fx = getattr(self, fx_key, None) if fresh_fx else None
            maybe_capture_quote_tick(
                self.cfg, self.state_path, mint, chain,
                (lambda: price * fx) if price and fx else None,
                None)
        if not price:
            # No price = no exits can fire. Track how long we've been
            # blind and scream about it instead of silently missing
            # stops. Never exit on a stale price.
            with self.lock:
                pos = self.state["positions"].get(mint)
                if pos:
                    pos["quote_fails"] = pos.get("quote_fails", 0) + 1
                    blind_sec = (time.time() - pos.get(
                        "last_price_ts",
                        pos.get("opened_at", time.time())))
                    if blind_sec >= 300 and not pos.get("blind_warned"):
                        pos["blind_warned"] = True
                        log("BLIND %s: no price for %.0fs - "
                            "TP/stops CANNOT fire!" % (pos["name"],
                                                       blind_sec))
                    self.save()
            return False
        with self.lock:
            pos = self.state["positions"].get(mint)
            if pos:
                pos["quote_fails"] = 0
                pos["blind_warned"] = False
                pos["last_price_ts"] = time.time()
        entry, peak = pos["entry"], pos["peak"]
        if price > peak:
            peak = price
            with self.lock:
                self.state["positions"][mint]["peak"] = peak
                self.save()
        gain = (price - entry) / entry * 100 if entry else 0
        dd_peak = (peak - price) / peak * 100 if peak else 0
        dd_entry = -gain

        # profit rotation: once a take-profit has banked gains, tighten the
        # trailing stop on the remainder. The runner keeps its uncapped
        # upside, but the bot exits quickly when momentum turns instead of
        # babysitting the rest of the move.
        if pos.get("rungs_fired"):
            trail = min(trail, ex.get("trail_after_tp_pct", 12))
        if pos.get("protect_mode"):
            try:
                protect_trail = float(self.cfg["risk"].get(
                    "kill_switch_protect_trail_pct", 10))
                if not math.isfinite(protect_trail) or not 0 < protect_trail < 100:
                    protect_trail = 10
            except (TypeError, ValueError):
                protect_trail = 10
            trail = min(trail, protect_trail)

        # dump detector: a violent vertical drop needs an instant exit,
        # not a trailing-stop wait. Compare the live price against the max
        # seen in the recent window (in-memory per-mint history).
        hist = self._px_hist.get(mint)
        if hist is None:
            hist = self._px_hist[mint] = deque(maxlen=300)
        now_ts = time.time()
        hist.append((now_ts, price))
        dump_win = ex.get("dump_window_sec", 60)
        dump_pct = ex.get("dump_drop_pct", 12)
        cutoff = now_ts - dump_win
        wmax = 0.0
        for t, p in hist:
            if t >= cutoff and p > wmax:
                wmax = p
        dump_drop = (wmax - price) / wmax * 100 if wmax > 0 else 0.0
        dumped = dump_drop >= dump_pct
        shadow_enabled = (getattr(self, "dry_run", False)
                          and ex.get("shadow_dump_detector", True))
        if shadow_enabled and "shadow_dump_first" not in pos:
            shadow = evaluate_shadow_dump(
                hist, now_ts, price, None,
                ex.get("shadow_dump_drop_pct", 8),
                ex.get("shadow_dump_window_sec", 30),
                ex.get("shadow_venue_dump_m5_pct", -20))
            if shadow:
                shadow.update({"ts": now_ts, "price": price,
                               "venue_check_sec": ex.get("venue_check_sec", 30)})
                pos["shadow_dump_first"] = shadow
                log("SHADOW DUMP %s: %s" % (pos["name"], shadow))

        # take-profit ladder (rungs_fired stores [index, actual_gain_pct])
        fired = {r[0] if isinstance(r, (list, tuple)) else r
                 for r in pos["rungs_fired"]}
        for i, (tp_pct, sell_pct) in enumerate(tps):
            if i not in fired and gain >= tp_pct:
                log("%s: TAKE PROFIT +%s%% hit (gain %.1f%%), selling %s%%"
                    % (pos["name"], tp_pct, gain, sell_pct))
                sig, proceeds = self.sell_pct_of_balance(
                    mint, sell_pct, "TP+%s%%" % tp_pct)
                if not sig:
                    # sell failed: do NOT mark the rung - the bot retries
                    # next cycle instead of silently skipping the take-profit.
                    log("%s: TP+%s%% sell FAILED - rung not marked, will "
                        "retry" % (pos["name"], tp_pct))
                    continue
                # honest accounting: record what the fill actually realized,
                # not the phantom feed gain (thin pools quote dust on size).
                slice_cost = pos.get("buy_sol", 0) * sell_pct / 100.0
                real_gain = ((proceeds / slice_cost - 1) * 100
                             if slice_cost > 0 else 0.0)
                unit = "BNB" if (pos.get("chain") or "solana") == "bsc" \
                    else "SOL"
                log("%s: TP+%s%% filled: %.6f %s on %.4f %s slice "
                    "(%+.1f%% realized)"
                    % (pos["name"], tp_pct, proceeds, unit,
                       slice_cost, unit, real_gain))
                expected = slice_cost * (1 + gain / 100.0)
                if expected > 0 and proceeds < 0.25 * expected:
                    log("PRICE-IMPACT WARNING %s: feed said %+.1f%% but the "
                        "fill was %.6f SOL vs %.4f expected - phantom depth!"
                        % (pos["name"], gain, proceeds, expected))
                with self.lock:
                    self.state["positions"][mint]["rungs_fired"].append(
                        [i, round(real_gain, 2), round(proceeds, 9)])
                    self.save()
                if sell_pct >= 100:
                    # profit rotation: the whole bag is sold, so close the
                    # trade now and free the slot for the next hunt instead
                    # of holding an empty position.
                    self.close_trade(
                        mint, pos,
                        price, "take profit +%.0f%%, rotating" % tp_pct)
                    return True
        # venue dump tripwire: DexScreener's own 5-minute tape is a second,
        # independent rug signal. Our quote path can go blind or serve
        # cached prices during a fast dump (exits then fire far too late);
        # the venue's m5 doesn't depend on our quotes at all. Checked at
        # most every venue_check_sec per position. Never fires on a
        # missing/failed read - no exit on unavailable data, ever.
        venue_dumped = False
        venue_m5 = None
        vth = ex.get("venue_dump_m5_pct", -30)
        vsec = ex.get("venue_check_sec", 30)
        if vth and now_ts - pos.get("last_venue_check", 0) >= vsec:
            try:
                venue_m5 = self.venue_m5_dump(mint, pos.get("chain", "solana"))
            except Exception:
                venue_m5 = None
            with self.lock:
                p2 = self.state["positions"].get(mint)
                if p2:
                    p2["last_venue_check"] = now_ts
                    self.save()
            if venue_m5 is not None and venue_m5 <= vth:
                venue_dumped = True
        # Observation only: reuse this tick's quote ring and the venue value
        # fetched by the live path. Never enter the sell decision branches.
        if (shadow_enabled
                and "shadow_dump_first" not in pos):
            shadow = evaluate_shadow_dump(
                hist, now_ts, price, venue_m5,
                ex.get("shadow_dump_drop_pct", 8),
                ex.get("shadow_dump_window_sec", 30),
                ex.get("shadow_venue_dump_m5_pct", -20))
            if shadow:
                shadow.update({"ts": now_ts, "price": price,
                               "venue_check_sec": vsec})
                pos["shadow_dump_first"] = shadow
                log("SHADOW DUMP %s: %s" % (pos["name"], shadow))
        # dump detector first: violent vertical drops exit NOW, before the
        # wider trailing stop even matters. This is the anti-rug reflex.
        dump_exit = False
        if venue_dumped:
            dump_exit = True
            reason = ("venue dump: DexScreener m5 %.1f%%, selling all"
                      % venue_m5)
            log("%s: VENUE DUMP DexScreener m5 %.1f%% <= %.0f%%, "
                "selling all" % (pos["name"], venue_m5, vth))
            self.sell_pct_of_balance(mint, 100, "venue-dump")
        elif dumped:
            dump_exit = True
            reason = ("dump detector -%.1f%% in %ss, selling all"
                      % (dump_drop, dump_win))
            log("%s: DUMP DETECTOR -%.1f%% in %ss, selling all"
                % (pos["name"], dump_drop, dump_win))
            self.sell_pct_of_balance(mint, 100, "dump")
        # trailing stop
        elif dd_peak >= trail:
            reason = "trailing stop -%.1f%% from peak" % dd_peak
            log("%s: TRAILING STOP -%.1f%% from peak, selling all"
                % (pos["name"], dd_peak))
            self.sell_pct_of_balance(mint, 100, "trail-%.0f%%" % trail)
        # hard stop
        elif dd_entry >= hard:
            reason = "hard stop -%.1f%% from entry, selling all" % dd_entry
            log("%s: HARD STOP -%.1f%% from entry, selling all"
                % (pos["name"], dd_entry))
            self.sell_pct_of_balance(mint, 100, "hard-%.0f%%" % hard)
        # stale exit: the FOMO thesis is dead if the coin hasn't done
        # anything after N minutes. Free the slot for fresh signals
        # instead of babysitting flat positions.
        elif self._is_stale(pos, gain):
            stale_min = self.cfg["exit"].get("stale_exit_min", 45)
            age_min = (time.time() - pos.get("opened_at", time.time())) / 60
            reason = ("stale exit: %+.1f%% after %.0fm, thesis dead"
                      % (gain, age_min))
            log("%s: STALE EXIT %+.1f%% after %.0fm, selling all to free "
                "the slot" % (pos["name"], gain, age_min))
            self.sell_pct_of_balance(mint, 100, "stale-%.0fm" % stale_min)
        else:
            return False
        # after a full exit, check balance; if dust/zero, close position
        time.sleep(2)
        if self.token_balance_raw(mint) == 0:
            self.close_trade(mint, pos, price, reason, dump_exit=dump_exit)
            return True
        return False

    def _is_stale(self, pos, gain_pct):
        """True when a position has sat too long without real movement."""
        stale_min = self.cfg["exit"].get("stale_exit_min", 45)
        if not stale_min or stale_min <= 0:
            return False
        max_gain = self.cfg["exit"].get("stale_exit_max_gain_pct", 10)
        age_min = (time.time() - pos.get("opened_at", time.time())) / 60
        return age_min >= stale_min and gain_pct < max_gain

    def _journal(self, rec):
        """Append one record to the trade journal (entries AND closes)."""
        jp = os.path.join(os.path.dirname(os.path.abspath(self.state_path)),
                          "trades.jsonl")
        with open(jp, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def _journal_candidate(self, signal, verdict):
        """Research instrumentation: journal one scanner observation.

        Called for EVERY signal the hunter surfaces, including ones that
        fail guardrails and never become entries (verdict "guardrail_skip"
        vs "enter"). Additive only: never affects entry decisions, never
        raises. Uses a lazily-created tracker so test-constructed Traders
        (no __init__) work too.
        """
        try:
            tracker = getattr(self, "_obs_tracker", None)
            if tracker is None:
                tracker = journal.ObservationTracker()
                self._obs_tracker = tracker
            repeat = tracker.observe(signal)
            basedir = os.path.dirname(os.path.abspath(self.state_path))
            journal.journal_event(
                basedir,
                journal.sanitize_observation(signal, verdict=verdict,
                                            repeat_info=repeat))
        except Exception:
            pass

    def _slip_veto(self, signal, entry, native_usd, chain, slip=None):
        """Positive-slip (chase) entry veto. Returns True when vetoed.

        User-ordered 2026-09-28 (deep-research pass: vetoing slip>0 entries
        raised walk-forward PF 0.254 -> 0.796 at 91% OOS retention on the
        small validated sample). Config flag hunter.entry.veto_positive_slip
        (default true); the user can toggle it later.

        Fail-OPEN on missing data: when slip cannot be measured, log the
        gap and proceed — never block an entry on instrumentation. Strict
        > 0 threshold, no tolerance band. Vetoed entries are journaled as
        slip_veto events with full would-be details so the improvement loop
        can retrospectively study them. Never raises.
        """
        try:
            entry_cfg = (self.cfg.get("hunter") or {}).get("entry") or {}
            if not entry_cfg.get("veto_positive_slip", True):
                return False
            if slip is None:
                slip = slip_from_signal(signal, entry, native_usd)
            if slip is None:
                log("SLIP-VETO GAP %s [%s]: slip unknown, proceeding "
                    "(fail open)" % (signal.get("name"), chain))
                return False
            if slip > 0:
                basedir = os.path.dirname(os.path.abspath(self.state_path))
                journal.journal_event(
                    basedir,
                    journal.sanitize_veto(signal, entry, native_usd, slip,
                                          chain))
                log("SLIP-VETO %s [%s]: slip %+.2f%% > 0, skipping chase "
                    "entry" % (signal.get("name"), chain, slip * 100))
                return True
            return False
        except Exception:
            return False

    def close_trade(self, mint, pos, exit_price, reason, dump_exit=False):
        """Pop a fully-exited position, compute exact realized P&L, journal it.

        dump_exit=True (dump detector / venue dump) arms the 24h per-mint
        dump cooldown; `reason` is human-readable only and never parsed.
        """
        buy = pos.get("buy_sol", 0)
        entry = pos.get("entry") or 0
        exit_px = exit_price or pos.get("peak") or entry
        rungs = []
        for r in pos.get("rungs_fired", []):
            # rungs_fired entries may be [idx, gain] (legacy) or
            # [idx, gain, proceeds] (new): never unpack positionally.
            idx, g = ((r[0], r[1] if len(r) > 1 else None)
                      if isinstance(r, (list, tuple)) else (r, None))
            rungs.append([idx, round(g, 2) if g is not None else 0])
        if pos.get("sold_sol") is not None and pos.get("tokens_raw") is not None:
            # exact path (virtual ledger): simulated swap proceeds, plus any
            # dust left valued at the exit price, minus the stake.
            d = pos.get("decimals") or 9
            dust = pos["tokens_raw"] / (10 ** d) * (exit_px or 0)
            realized = round(pos["sold_sol"] + dust - buy, 6)
        else:
            # legacy path (positions opened before the virtual ledger):
            # price-math approximation.
            tps = self.cfg["exit"].get("take_profits", [])
            realized, remaining = 0.0, 1.0
            for r in pos.get("rungs_fired", []):
                # rungs_fired entries may be [idx, gain] (legacy) or
                # [idx, gain, proceeds] (new): never unpack positionally.
                idx, g = ((r[0], r[1] if len(r) > 1 else None)
                          if isinstance(r, (list, tuple)) else (r, None))
                if idx < len(tps):
                    frac = tps[idx][1] / 100.0
                    gpct = g if g is not None else tps[idx][0]
                else:
                    frac, gpct = 0.0, 0.0
                realized += buy * frac * gpct / 100.0
                remaining -= frac
            if entry and remaining > 0:
                realized += buy * remaining * (exit_px - entry) / entry
            realized = round(realized, 6)
        with self.lock:
            self.state["positions"].pop(mint, None)
            if dump_exit:
                self.state.setdefault("dump_cooldown", {})[mint] = time.time()
            # buy_sol/sold_sol hold NATIVE amounts (SOL or BNB per chain).
            if (pos.get("chain") or "solana") == "bsc":
                self.state["realized_bnb"] = round(
                    self.state.get("realized_bnb", 0.0) + realized, 9)
            else:
                self.state["realized_sol"] = round(
                    self.state.get("realized_sol", 0.0) + realized, 9)
            self.save()
        self._px_hist.pop(mint, None)
        chain = pos.get("chain", "solana")
        native_usd = self.native_usd(chain)
        realized_usd = (round(realized * native_usd, 2)
                        if native_usd else None)
        if realized_usd is not None:
            with self.lock:
                self.state["realized_usd"] = round(
                    self.state.get("realized_usd", 0.0) + realized_usd, 2)
                money.apply_close(self.state, realized_usd)
                self.save()
        unit = "BNB" if chain == "bsc" else "SOL"
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "ts_epoch": time.time(),
               "type": "close",
               "mint": mint, "name": pos.get("name"), "chain": chain,
               "entry": entry, "peak": pos.get("peak"), "exit": exit_px,
               "buy_sol": buy, "rungs": rungs, "reason": reason,
               "realized_sol": realized if chain != "bsc" else None,
               "realized_bnb": realized if chain == "bsc" else None,
               "realized_usd": realized_usd,
               "bankroll_usd": self.state.get("bankroll_usd"),
               "drawdown_pct": money.drawdown_pct(self.state),
               "sol_usd": self.sol_usd() if chain != "bsc"
               else self.bnb_usd()}
        if pos.get("shadow_dump_first"):
            rec["shadow_dump_first"] = pos["shadow_dump_first"]
        maybe_tag_research(rec, getattr(self, "cfg", None), pos)
        self._journal(rec)
        usd_note = ""
        if realized_usd is not None:
            usd_note = " ($%+.2f)" % realized_usd
        log("%s CLOSED: %s | realized %+.6f %s%s" % (pos.get("name"), reason, realized, unit, usd_note))
        maybe_research_dashboard(getattr(self, "cfg", None), self.state_path)
        return realized

    # -- main loop --
    def run(self):
        basedir = os.path.dirname(os.path.abspath(self.state_path))
        pidfile = os.path.join(basedir, "bot.pid")
        haltfile = os.path.join(basedir, "halt.flag")
        if os.path.exists(haltfile):
            log("halt.flag present - kill switch engaged, not starting. "
                "Remove it (or Start with force in the dashboard) to resume.")
            sys.exit(42)  # 42 = halted; systemd is configured not to restart this
        with open(pidfile, "w") as f:
            f.write(str(os.getpid()))
        hunter = Hunter(self.cfg)
        poll = self.cfg["hunter"].get("poll_sec", 45)
        log("hunter live: FOMO filter %s%%/15m, buy/sell >= %s, poll %ss" % (
            self.cfg["hunter"].get("min_m15_gain_pct"),
            self.cfg["hunter"].get("min_buy_sell_ratio"), poll))
        # resume managing positions from a previous run
        for mint in list(self.state["positions"]):
            threading.Thread(target=self.manage, args=(mint,), daemon=True).start()
        stop_flag = {"stop": False}

        def _on_term(signum, frame):
            stop_flag["stop"] = True

        prev_term = signal.signal(signal.SIGTERM, _on_term)
        exit_code = 0
        last_hb, t_start = 0.0, time.time()
        try:
            while True:
                if stop_flag["stop"]:
                    log("received SIGTERM, shutting down")
                    break
                if os.path.exists(haltfile):
                    log("halt.flag detected - kill switch engaged, shutting down")
                    exit_code = 42  # halted; systemd must not restart this
                    break
                if time.time() - last_hb >= 1800:
                    # heartbeat every 30m: silence in the log now means
                    # death, not quiet markets.
                    last_hb = time.time()
                    up = int(time.time() - t_start)
                    with self.lock:
                        npos = len(self.state["positions"])
                        rz = self.state.get("realized_sol", 0.0)
                    log("heartbeat: %d open | realized %+.6f SOL | "
                        "%+.2f USD | uptime %dh%02dm"
                        % (npos, rz, self.state.get("realized_usd", 0.0),
                           up // 3600, (up % 3600) // 60))
                try:
                    signals = hunter.scan()
                    log("scan: %d FOMO signal(s)" % len(signals))
                    for s in signals:
                        if self.guardrails_ok(s):
                            self._journal_candidate(s, "enter")
                            # threaded: pullback waits must not stall scanning
                            threading.Thread(target=self.enter, args=(s,),
                                             daemon=True).start()
                        else:
                            self._journal_candidate(s, "guardrail_skip")
                            log("skip %s (guardrails)" % s["name"])
                    early = hunter.last_early or []
                    if early:
                        log("scan: %d pre-pump signal(s)" % len(early))
                        for s in early:
                            if self.guardrails_ok(s):
                                self._journal_candidate(s, "enter")
                                threading.Thread(
                                    target=self.enter, args=(s,),
                                    daemon=True).start()
                            else:
                                self._journal_candidate(s, "guardrail_skip")
                                log("skip %s (guardrails)" % s["name"])
                    # R10 instrumentation: journal early-path accel-gate
                    # rejects as their own event type. Direct journal_event
                    # (not _journal_candidate) so reject rows never pollute
                    # the ObservationTracker repeat counts. Additive only.
                    try:
                        basedir = os.path.dirname(
                            os.path.abspath(self.state_path))
                        for r in hunter.last_early_accel_rejects or []:
                            journal.journal_event(
                                basedir,
                                journal.sanitize_early_accel_reject(r))
                    except Exception:
                        pass
                except Exception:
                    log("hunter error:\n%s" % traceback.format_exc())
                for _ in range(poll * 2):  # interruptible wait
                    if stop_flag["stop"]:
                        break
                    if os.path.exists(haltfile):
                        log("halt.flag detected - kill switch engaged, shutting down")
                        exit_code = 42
                        break
                    time.sleep(0.5)
                if exit_code == 42:
                    break
        finally:
            signal.signal(signal.SIGTERM, prev_term)
            self._release_pidfile(pidfile)
        sys.exit(exit_code)

    @staticmethod
    def _release_pidfile(pidfile):
        """Remove the pidfile only if it still holds our own pid. A newer
        bot may have started during our shutdown (restart overlap) - we
        must never delete its pidfile out from under the watchdog, or the
        watchdog will conclude the bot is dead and start a duplicate."""
        try:
            with open(pidfile) as f:
                owner = f.read().strip()
            if owner == str(os.getpid()):
                os.remove(pidfile)
        except OSError:
            pass


def main():
    # Crash forensics: if the interpreter ever segfaults/aborts, dump the
    # Python stack to stderr (bot.log) instead of vanishing silently.
    faulthandler.enable()
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--hunt", action="store_true",
                    help="single FOMO scan, print signals, exit (read-only)")
    args = ap.parse_args()
    with open(os.path.expanduser(args.config)) as f:
        cfg = json.load(f)
    if args.hunt:
        for s in Hunter(cfg).scan():
            print(json.dumps(s, indent=1))
        return
    wallets = cfg.get("wallets", {}) or {}
    if not (wallets.get("solana_key_file") or cfg.get("keypair_path")):
        log("ERROR: set wallets.solana_key_file in config.json")
        sys.exit(1)
    state_path = os.path.join(os.path.dirname(os.path.expanduser(args.config)),
                              "state.json")
    Trader(cfg, state_path).run()


if __name__ == "__main__":
    main()
