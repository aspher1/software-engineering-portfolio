#!/usr/bin/env python3
"""Paper trader for stocks + crypto. PAPER ONLY - no brokerage, no real money.

Two sleeves, one loop:
  crypto: BTC/ETH/SOL/DOGE via Coinbase public API (Kraken fallback), 24/7,
          5m scans; skipped (never traded) if no source has fresh data
  stocks: momentum watchlist via Yahoo Finance, market hours only (ET)

Strategy (v1 baseline - the analysis loop tunes these):
  momentum entry, TP / SL / trailing-stop exits, fixed fractional size.

Everything is journaled to trades.jsonl immediately on entry AND close.
Improvement loop: analyze.py reads the journal daily; param changes go in
CHANGELOG.md with the evidence that motivated them.

Run:  python paper_trader.py --config config.json
Files (all under the run dir next to the config):
  state.json  bankroll, positions, realized P&L, daily counters
  trades.jsonl  every entry and close, one JSON object per line
  bot.log     timestamped log
  bot.pid     pidfile (refreshed on start)
  halt.flag   create to stop the bot gracefully (exit 42)
"""
import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from ai_gate import AIGate

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
ET = ZoneInfo("America/New_York")

RUN = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(RUN, "state.json")

JOURNAL_PATH = os.path.join(RUN, "trades.jsonl")
LOG_PATH = os.path.join(RUN, "bot.log")
PID_PATH = os.path.join(RUN, "bot.pid")
HALT_PATH = os.path.join(RUN, "halt.flag")

running = True


def log(msg):
    line = "[%s] %s" % (datetime.now(ET).strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


LOG_MAX_BYTES = 5 * 1024 * 1024  # rotate bot.log past ~5MB
LOG_BACKUPS = 3                  # keep bot.log.1/.2/.3, oldest dropped


def rotate_log():
    """Rotate LOG_PATH at ~5MB, keeping 3 backups (bot.log.1/2/3).

    Called once at startup (first statement in main). The oldest backup
    (.3) is dropped; .2 -> .3, .1 -> .2, bot.log -> .1. Fail-soft: a
    rotation error must never stop the bot.
    """
    try:
        if (not os.path.exists(LOG_PATH)
                or os.path.getsize(LOG_PATH) < LOG_MAX_BYTES):
            return
    except OSError:
        return
    try:
        oldest = "%s.%d" % (LOG_PATH, LOG_BACKUPS)
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(LOG_BACKUPS - 1, 0, -1):
            src = "%s.%d" % (LOG_PATH, i)
            if os.path.exists(src):
                os.replace(src, "%s.%d" % (LOG_PATH, i + 1))
        os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass


def roll_scan_stats(state):
    """Roll scan_stats throughput counters by ET date.

    If the stored date is not today, logs the old day's totals
    ("throughput <date>: N scans, M signals, K entered") and resets the
    counters with the new date. Returns the scan_stats dict.
    crypto_skipped counts scans where no crypto symbol had usable data.
    """
    today = datetime.now(ET).strftime("%Y-%m-%d")
    ss = state.setdefault("scan_stats",
                          {"date": today, "scans": 0, "signals": 0,
                           "entered": 0, "crypto_skipped": 0})
    if ss.get("date") != today:
        log("throughput %s: %d scans, %d signals, %d entered, "
            "%d crypto skipped (no data)"
            % (ss.get("date"), ss.get("scans", 0),
               ss.get("signals", 0), ss.get("entered", 0),
               ss.get("crypto_skipped", 0)))
        ss.update({"date": today, "scans": 0, "signals": 0, "entered": 0,
                   "crypto_skipped": 0})
    return ss


def http_get(url, timeout=20, params=None):
    r = requests.get(url, headers={"User-Agent": UA}, params=params,
                     timeout=timeout)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- market data

CANDLE_SEC = 300          # 5m candles
CANDLE_MAX_AGE_SEC = 900  # newest candle must start within 15 min of now
KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD",
                "DOGE": "XDGUSD"}


def coinbase_spot(symbol):
    d = http_get("https://api.coinbase.com/v2/prices/%s-USD/spot" % symbol)
    return float(d["data"]["amount"])


def coinbase_candles(symbol):
    """5m candles as {start_ts: close}."""
    d = http_get("https://api.exchange.coinbase.com/products/%s-USD/candles"
                 % symbol, params={"granularity": CANDLE_SEC})
    return {int(c[0]): float(c[4]) for c in d}


def kraken_result(method, symbol, params=None):
    q = dict(params or {}, pair=KRAKEN_PAIRS[symbol])
    d = http_get("https://api.kraken.com/0/public/%s" % method, params=q)
    if d.get("error"):
        raise ValueError("kraken error: %s" % d["error"])
    return next(v for k, v in d["result"].items() if k != "last")


def kraken_spot(symbol):
    return float(kraken_result("Ticker", symbol)["c"][0])


def kraken_candles(symbol):
    """5m candles as {start_ts: close}."""
    rows = kraken_result("OHLC", symbol, {"interval": CANDLE_SEC // 60})
    return {int(r[0]): float(r[4]) for r in rows}


# Tried in order. Spot and candles always come from the same source so a
# cross-exchange basis can never masquerade as momentum.
CRYPTO_SOURCES = [("coinbase", coinbase_spot, coinbase_candles),
                  ("kraken", kraken_spot, kraken_candles)]


def momentum_from_candles(price, candles, now):
    """Pure: (pct_1h, pct_4h) vs the price 60 / 240 minutes ago.

    `candles` is {start_ts: close}. The reference for "N sec ago" is the
    close of the newest candle that ended at or before now - N. Returns None
    (fail closed) if price is unusable, the newest candle is older than
    CANDLE_MAX_AGE_SEC, or a reference candle is missing.
    """
    if not candles or price is None or price <= 0:
        return None
    if now - max(candles) > CANDLE_MAX_AGE_SEC:
        return None

    def ref(ago):
        start = int(now - ago) // CANDLE_SEC * CANDLE_SEC - CANDLE_SEC
        c = candles.get(start)
        return c if c and c > 0 else None

    r1, r4 = ref(3600), ref(4 * 3600)
    if r1 is None or r4 is None:
        return None
    return (price - r1) / r1 * 100, (price - r4) / r4 * 100


def crypto_momentum(symbol):
    """Returns (price, pct_1h, pct_4h) from the first source with a fresh,
    complete read, or (None, None, None) if every source fails."""
    for name, spot, candles in CRYPTO_SOURCES:
        try:
            price = spot(symbol)
            mom = momentum_from_candles(price, candles(symbol), time.time())
        except Exception as e:
            log("crypto data failed %s via %s: %s: %s"
                % (symbol, name, type(e).__name__, e))
            continue
        if mom is None:
            log("crypto data stale/incomplete %s via %s" % (symbol, name))
            continue
        return (price,) + mom
    log("crypto: no price source for %s - skipped" % symbol)
    return None, None, None


def crypto_spot(symbol):
    """Spot price from the first working source, or None if all fail."""
    for name, spot, _ in CRYPTO_SOURCES:
        try:
            price = spot(symbol)
        except Exception as e:
            log("crypto spot failed %s via %s: %s: %s"
                % (symbol, name, type(e).__name__, e))
            continue
        if price and price > 0:
            return price
    return None


def stock_quote(symbol):
    """Returns (price, day_change_pct) or (None, None) on failure."""
    try:
        d = http_get("https://query1.finance.yahoo.com/v8/finance/chart/%s"
                     % symbol, params={"interval": "5m", "range": "1d"})
        res = d["chart"]["result"][0]
        meta = res["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if not price or not prev:
            return None, None
        return float(price), (float(price) - float(prev)) / float(prev) * 100
    except Exception as e:
        log("stock data failed %s: %s: %s" % (symbol, type(e).__name__, e))
        return None, None


def market_hours():
    """True during US equity regular session (Mon-Fri 9:30-16:00 ET)."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins < 16 * 60


# ---------------------------------------------------------------- state

def unrealized_pnl(state, prices):
    """Pure: sum over positions of qty*current_price - invested.

    Symbols missing from `prices` (or with a None price) are skipped.
    """
    total = 0.0
    for symbol, p in state.get("positions", {}).items():
        price = prices.get(symbol)
        if price is None:
            continue
        total += p["qty"] * price - p["invested"]
    return total


def equity(state, prices):
    """Pure: bankroll + unrealized P&L of open positions."""
    return state.get("bankroll", 0.0) + unrealized_pnl(state, prices)


def drawdown_pct(state, prices):
    """Pure: (peak_equity - equity)/peak_equity*100. 0.0 if peak <= 0."""
    peak = state.get("peak_equity", 0) or 0
    if peak <= 0:
        return 0.0
    return (peak - equity(state, prices)) / peak * 100


def default_state(cfg):
    today = datetime.now(ET).strftime("%Y-%m-%d")
    return {"bankroll": cfg["bankroll"],
            "positions": {},
            "realized": 0.0,
            "day": {"date": today,
                    "trades": 0},
            "last_scan": 0,
            "peak_equity": cfg["bankroll"],
            "max_drawdown_pct": 0.0,
            "drawdown_pct": 0.0,
            "entries_halted": False,
            "scan_stats": {"date": today, "scans": 0, "signals": 0,
                           "entered": 0, "crypto_skipped": 0}}


def load_state(cfg):
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except (OSError, ValueError):
            log("WARNING: state.json corrupt or unreadable - resetting to "
                "$%.2f bankroll defaults" % cfg["bankroll"])
    return default_state(cfg)


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_PATH)


def journal(rec):
    rec = dict(rec)
    rec["ts"] = datetime.now(ET).isoformat()
    with open(JOURNAL_PATH, "a") as f:
        f.write(json.dumps(rec) + "\n")


def roll_day(state):
    today = datetime.now(ET).strftime("%Y-%m-%d")
    if state["day"]["date"] != today:
        state["day"] = {"date": today, "trades": 0}


# ---------------------------------------------------------------- trading

def current_price(sleeve, symbol):
    if sleeve == "crypto":
        return crypto_spot(symbol)
    price, _ = stock_quote(symbol)
    return price


def scan(cfg, state):
    """Return list of (sleeve, symbol, price, momentum_pct, reason, ctx).

    ctx carries extra context for the AI gate (e.g. 4h momentum for
    crypto). Entries 0-4 keep their v1 meaning so existing consumers
    (log lines, tests) are unaffected."""
    signals = []
    cc = cfg["crypto"]
    checked = blind = 0
    for sym in cc["symbols"]:
        if sym in state["positions"]:
            continue
        checked += 1
        price, p1h, p4h = crypto_momentum(sym)
        if price is None:
            blind += 1
            continue
        if p1h >= cc["entry_1h_pct"] and p4h >= cc.get("entry_4h_min_pct", 0):
            signals.append(("crypto", sym, price, p1h,
                            "+%.1f%%/1h" % p1h, {"momentum_4h": p4h}))
    if checked and blind == checked:
        ss = state.setdefault("scan_stats", {})
        ss["crypto_skipped"] = ss.get("crypto_skipped", 0) + 1
        log("crypto scan skipped: no price source returned fresh data "
            "for any symbol")
    if market_hours():
        sc = cfg["stocks"]
        for sym in sc["symbols"]:
            if sym in state["positions"]:
                continue
            price, day_pct = stock_quote(sym)
            if price is None:
                continue
            if day_pct >= sc["entry_day_pct"]:
                signals.append(("stocks", sym, price, day_pct,
                                "+%.1f%% today" % day_pct, {}))
    return signals


def enter(cfg, state, sleeve, symbol, price, momentum_pct, reason, ai=None):
    """ai: None (gate off -> v1 rules entry) or the gate's approval dict."""
    size = cfg["trade_size"]
    if state["bankroll"] < size:
        log("skip %s: bankroll %.2f < trade size %.2f"
            % (symbol, state["bankroll"], size))
        return False
    if len(state["positions"]) >= cfg["max_positions"]:
        log("skip %s: max positions reached" % symbol)
        return False
    roll_day(state)
    if state.get("drawdown_pct", 0) >= cfg["drawdown_kill_pct"]:
        if not state.get("entries_halted"):
            state["entries_halted"] = True
            save_state(state)
            log("DRAWDOWN KILL SWITCH: entries halted at %.1f%% drawdown "
                "(trip %.0f%%)" % (state["drawdown_pct"],
                                   cfg["drawdown_kill_pct"]))
        return False
    if state["day"]["trades"] >= cfg["max_trades_day"]:
        log("skip %s: daily trade cap" % symbol)
        return False
    cd = cfg.get("cooldown_hours", 24)
    # (cooldown tracked via journal scan would be nicer; v1: skip if traded
    # this symbol in the last `cd` hours according to journal)
    if recently_traded(symbol, cd):
        log("skip %s: cooldown" % symbol)
        return False

    slip = cfg[sleeve]["slippage_pct"] / 100.0
    fill = price * (1 + slip)  # buys pay the spread
    qty = size / fill
    state["bankroll"] -= size
    state["positions"][symbol] = {
        "sleeve": sleeve, "entry": fill, "qty": qty,
        "peak": price, "invested": size,
        "opened_at": datetime.now(ET).isoformat(),
        "momentum_at_entry": momentum_pct,
    }
    state["day"]["trades"] += 1
    journal({"type": "entry", "sleeve": sleeve, "symbol": symbol,
             "price": fill, "qty": qty, "invested": size,
             "momentum_pct": momentum_pct, "reason": reason,
             "strategy": "v2-ai-gate" if ai else "v1-rules",
             **({"ai": ai} if ai else {})})
    save_state(state)
    log("ENTER %s [%s] @ %.4f (%s) size $%.2f" % (symbol, sleeve, fill,
                                                 reason, size))
    return True


def recently_traded(symbol, hours):
    """True if symbol appears in the journal within the last `hours`."""
    if not os.path.exists(JOURNAL_PATH):
        return False
    cutoff = time.time() - hours * 3600
    try:
        with open(JOURNAL_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("symbol") != symbol or r.get("type") != "entry":
                    continue
                ts = r.get("ts", "")
                try:
                    t = datetime.fromisoformat(ts).timestamp()
                except ValueError:
                    continue
                if t >= cutoff:
                    return True
    except OSError:
        pass
    return False


def exit_position(cfg, state, symbol, price, reason):
    p = state["positions"].pop(symbol)
    slip = cfg[p["sleeve"]]["slippage_pct"] / 100.0
    fill = price * (1 - slip)  # sells give up the spread
    proceeds = p["qty"] * fill
    pnl = proceeds - p["invested"]
    state["bankroll"] += proceeds
    state["realized"] += pnl
    journal({"type": "close", "sleeve": p["sleeve"], "symbol": symbol,
             "price": fill, "qty": p["qty"], "proceeds": proceeds,
             "pnl": pnl, "reason": reason,
             "entry": p["entry"],
             "return_pct": pnl / p["invested"] * 100})
    save_state(state)
    log("CLOSE %s [%s]: %s | pnl %+.2f (%.1f%%)" % (symbol, p["sleeve"],
                                                   reason, pnl,
                                                   pnl / p["invested"] * 100))
    return pnl


def manage(cfg, state):
    """Check exits on every open position. Returns True if state changed."""
    changed = False
    prices = {}
    max_hold_sec = cfg.get("max_hold_hours", 48) * 3600
    for symbol in list(state["positions"]):
        p = state["positions"][symbol]
        sc = cfg[p["sleeve"]]
        price = current_price(p["sleeve"], symbol)
        if price is None:
            continue
        prices[symbol] = price
        if price > p["peak"]:
            p["peak"] = price
            changed = True
        reason = None
        try:
            opened = datetime.fromisoformat(p["opened_at"]).timestamp()
            age_sec = time.time() - opened
        except (ValueError, KeyError):
            age_sec = 0
        if age_sec > max_hold_sec:
            reason = "max hold %dh" % cfg.get("max_hold_hours", 48)
        if reason is None:
            chg = (price - p["entry"]) / p["entry"] * 100
            from_peak = (price - p["peak"]) / p["peak"] * 100
            if chg >= sc["tp_pct"]:
                reason = "take profit +%.1f%%" % chg
            elif chg <= sc["sl_pct"]:
                reason = "stop loss %.1f%%" % chg
            # NOTE: trail_pct must sit INSIDE the TP/SL band to function:
            # a 25% trail can never fire when TP (+6%/+8%) or SL (-4%/-5%)
            # exits first, so it was dead config. trail_pct < tp_pct lets the
            # trail engage after a partial run-up before TP is reached.
            elif from_peak <= -sc["trail_pct"]:
                reason = "trailing stop %.1f%% from peak" % from_peak
        if reason:
            exit_position(cfg, state, symbol, price, reason)
            changed = True
    # --- drawdown bookkeeping (always runs so peak_equity stays current) ---
    eq = equity(state, prices)
    dd = drawdown_pct(state, prices)
    if eq > state.get("peak_equity", 0):
        state["peak_equity"] = eq
        changed = True
    if dd > state.get("max_drawdown_pct", 0.0):
        state["max_drawdown_pct"] = dd
        changed = True
    if abs(dd - state.get("drawdown_pct", 0.0)) > 1e-9:
        state["drawdown_pct"] = dd
        changed = True
    # hysteresis: resume entries only after drawdown recovers below the
    # resume band (prevents flapping at the kill threshold)
    if state.get("entries_halted") and dd < cfg["drawdown_resume_pct"]:
        state["entries_halted"] = False
        changed = True
        log("drawdown recovered to %.1f%% - entries resumed" % dd)
    if changed:
        save_state(state)
    return changed


# ---------------------------------------------------------------- main

def on_term(signum, frame):
    global running
    log("received SIGTERM, shutting down")
    running = False


def check_single_instance():
    """Return True if another live paper_trader instance already holds PID_PATH.

    Missing, unparseable, or stale pidfiles are removed and reported as
    no-instance (return False)."""
    try:
        with open(PID_PATH) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        if os.path.exists(PID_PATH):
            os.remove(PID_PATH)
        return False
    if pid == os.getpid():
        return False
    live = True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        live = False
    except PermissionError:
        live = True
    except OSError:
        live = False
    if live:
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                cmdline = f.read().decode("utf-8", "replace")
        except OSError:
            live = False
        else:
            live = "paper_trader" in cmdline
    if not live:
        try:
            os.remove(PID_PATH)
        except OSError:
            pass
        return False
    log("another paper_trader instance running (pid %d), exiting" % pid)
    return True


def main():
    global running
    rotate_log()
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    with open(args.config) as f:
        cfg = json.load(f)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    if check_single_instance():
        sys.exit(1)
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))

    state = load_state(cfg)
    roll_day(state)
    save_state(state)
    log("paper trader live: bankroll $%.2f, size $%.2f, max pos %d | DRY: paper only"
        % (state["bankroll"], cfg["trade_size"], cfg["max_positions"]))
    log("crypto: %s | stocks: %s (market hours)"
        % (cfg["crypto"]["symbols"], cfg["stocks"]["symbols"]))

    scan_every = cfg.get("scan_interval_sec", 300)
    manage_every = cfg.get("manage_interval_sec", 60)
    last_manage = 0.0
    gate = AIGate(cfg.get("ai_gate", {}))
    log("AI entry gate: %s (backends %s)"
        % ("ENABLED" if gate.enabled else "disabled",
           "+".join(gate.backends)))

    while running:
        if os.path.exists(HALT_PATH):
            log("halt.flag present, stopping (exit 42)")
            save_state(state)
            sys.exit(42)
        try:
            roll_day(state)
            now = time.time()
            if now - last_manage >= manage_every:
                manage(cfg, state)
                last_manage = now
            if now - state.get("last_scan", 0) >= scan_every:
                state["last_scan"] = now
                ss = roll_scan_stats(state)
                ss["scans"] += 1
                sigs = scan(cfg, state)
                ss["signals"] += len(sigs)
                if sigs:
                    log("scan: %d signal(s): %s"
                        % (len(sigs), ", ".join("%s %s" % (s[1], s[4])
                                                for s in sigs)))
                    for sleeve, sym, price, mom, reason, ctx in sigs:
                        if not running:
                            break
                        proceed, ai_info = gate.evaluate(
                            cfg, state, sleeve, sym, price, mom, reason,
                            ctx.get("momentum_4h"))
                        if not proceed:
                            ss["ai_vetoes"] = ss.get("ai_vetoes", 0) + 1
                            continue
                        if ai_info:
                            ss["ai_approvals"] = ss.get("ai_approvals", 0) + 1
                        if enter(cfg, state, sleeve, sym, price, mom, reason,
                                 ai=ai_info):
                            ss["entered"] += 1
                save_state(state)
        except Exception as e:
            log("loop error: %s: %s" % (type(e).__name__, e))
        for _ in range(50):
            if not running:
                break
            time.sleep(0.1)

    save_state(state)
    log("shutdown complete")


if __name__ == "__main__":
    main()
