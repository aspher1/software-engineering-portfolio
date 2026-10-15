"""Flip analyst: a fast-LLM decision layer on rapid-flip batches.

Paper/analysis only. Makes hard calls — BUY_YES / BUY_NO / CLOSE / HOLD /
SKIP — from the flip tape plus the quantitative model's fair-value snapshot
(data/latest_fair.json, written by the 2-min scan).

Conviction discipline (the honest core):
- MODEL+TAPE: the tape direction agrees with a fresh model edge >= 5c.
  This is the validated leg (model edge is what the backtests support).
- TAPE-ONLY: the tape screams but the model shows no edge (or the snapshot
  is stale). The call is still made decisively, but labeled for what it is.
Every call is logged with its window context so the hit rate of MODEL+TAPE
vs TAPE-ONLY calls can be graded — if tape-only calls don't earn their keep,
they get retired.

Design contract (user-approved 2026-10-02):
- Reads the flip detector's own stashed batch (10c+ moves only since the
  2026-10-02 threshold raise): zero changes to the detector, its 5s poll, or
  the scanner's entry/exit/grading logic.
- Fires at most once per COOLDOWN_S (60s), unless a coin's flip direction
  reversed since its last recorded direction (regime change; per-coin memory
  with a 10-min TTL so one batch can't wipe other coins' context).
- Idempotent per batch_id: a re-seen batch returns the cached result, no
  second model call.
- Tight prompt; latency budget 4s (shared with exit_copilot); on timeout
  or any error it returns None silently — the flip alert relays plainly.
- Gated on FLIP_ANALYST_ENABLED=1 and the shared kill switch
  (data/exit_copilot.kill): touch the kill file to silence both copilots.
- Every call is logged to data/flip_analyst_log.jsonl with call, conviction,
  and model-snapshot age for later hit-rate grading.

Output contract: exactly three lines from the model —
  CALL: <BUY_YES | BUY_NO | CLOSE | HOLD | SKIP> <COIN> - <MODEL+TAPE | TAPE-ONLY>
  REASON: <why, with the key numbers>
  WATCH: <what would flip this call>
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from exit_copilot import (CopilotBackend, LATENCY_BUDGET_S,
                          KILL_SWITCH_PATH, MAX_REASON_CHARS)

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_DIR, "data")
STATE_PATH = os.path.join(DATA_DIR, "flip_analyst_state.json")
LOG_PATH = os.path.join(DATA_DIR, "flip_analyst_log.jsonl")
HOOK_STATE_PATH = os.path.expanduser("~/hooks/state/kalshi-flip-watch.json")
LATEST_FLIPS_PATH = os.path.expanduser(
    "~/hooks/state/kalshi-flip-watch-latest.json")
HOOK_BATCH_DIR = os.path.expanduser("~/hooks/state/flip-batches")
FAIR_SNAP_PATH = os.path.join(DATA_DIR, "latest_fair.json")
FAIR_FRESH_S = 180         # model snapshot usable if this fresh (seconds)
MODEL_EDGE_MIN = 0.05      # |edge| that counts as model backing (matches scan)
SCAN_STATE_PATH = os.path.join(DATA_DIR, "same_day_loop_state.json")

ENABLED_ENV = "FLIP_ANALYST_ENABLED"
COOLDOWN_S = 60            # min seconds between model calls
TAPE_LOOKBACK_S = 300     # how far back to read price histories (live data
                          # goes stale; handoffs routinely arrive 2-4 min
                          # after the poll, 90s was starving the analyst)
BATCH_LOOKBACK_S = 900    # exact-batch path: a stashed batch is a frozen
                          # record of the flip, its age doesn't degrade its
                          # accuracy (files prune at 15 min anyway)
TAPE_MIN_MOVE = 0.10      # fallback tape legs: 10c+ only (matches the
                          # 2026-10-02 alert threshold; smaller is noise)
RECENT_ALERT_S = 180      # "alerted" if last_alert_ts within this
DIR_TTL_S = 600           # per-coin direction memory for reversal detection
BACKEND_ERROR_BACKOFF_S = 300  # after a full-chain backend failure, stop
                          # hammering the backends for this long (the
                          # 2026-10-06 overnight whipsaw burned the Gemini
                          # and OpenRouter daily quotas with a backend
                          # attempt on nearly every batch)


def _now():
    return time.time()


def _load_json(path, default):
    try:
        with open(path) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else default
    except (OSError, ValueError):
        return default


def _save_json(path, obj):
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def get_open_positions(now_ts=None):
    """Model-issued positions still open: side, coin, taken entry price."""
    now_ts = now_ts if now_ts is not None else _now()
    st = _load_json(SCAN_STATE_PATH, {})
    alerted = st.get("alerted", {})
    out = []
    for key, v in alerted.items():
        if not isinstance(v, dict) or v.get("side") not in ("YES", "NO"):
            continue
        if v.get("exit_ts"):
            continue
        wc = v.get("window_close_utc") or 0
        if wc <= now_ts:
            continue
        ticker = v.get("ticker", "")
        coin = ticker.split("15M")[0].replace("KX", "") if "15M" in ticker \
            else key.split(":")[0]
        side = v["side"]
        # State records store the YES-oriented market_mid; real ones never
        # carry entry_price_taken (it only exists on exit dicts), so without
        # this the analyst prompt renders every open position as "entry ?"
        # (iter-20 fix). Derive exactly as hold_guard.entry_taken does.
        # Prefer a stored entry_price_taken when one exists (some records
        # carry it); derive from side + market_mid otherwise. Display only:
        # never read by any entry/exit decision.
        entry_price_taken = v.get("entry_price_taken")
        if entry_price_taken is None:
            mid = v.get("market_mid")
            if mid is not None:
                entry_price_taken = mid if side == "YES" else 1.0 - mid
        out.append({
            "coin": coin,
            "side": side,
            "entry_price_taken": entry_price_taken,
            "ticker": ticker,
        })
    return out


def get_model_snap(now_ts=None):
    """Fresh per-coin model fair/edge snapshot from the 2-min scan.

    Returns (coins_dict, age_s) or ({}, None) when missing/stale. Never raises.
    """
    now_ts = now_ts if now_ts is not None else _now()
    try:
        with open(FAIR_SNAP_PATH) as f:
            snap = json.load(f)
        coins = snap.get("coins", {}) if isinstance(snap, dict) else {}
        age = now_ts - snap.get("as_of_ts", 0)
        if not isinstance(coins, dict) or age > FAIR_FRESH_S:
            return {}, None
        return coins, age
    except (OSError, ValueError):
        return {}, None


def _load_batch(batch_id):
    """Load one stashed batch by id. Strict id format (digits-digits, as the
    wrapper generates) so a handoff can never drive path traversal."""
    if not batch_id or not re.fullmatch(r"[0-9]+-[0-9]+", batch_id):
        return None
    try:
        with open(os.path.join(HOOK_BATCH_DIR, batch_id + ".json")) as f:
            batch = json.load(f)
            return batch if isinstance(batch, dict) else None
    except (OSError, ValueError):
        return None


def build_tape(now_ts=None, batch_id=None):
    """Recent fast legs for one batch.

    When batch_id is given (the exact batch behind a relayed handoff), that
    batch is read — this is what keeps the AI's call paired with the alert
    the user is reading. Otherwise the latest stashed batch is used, with
    the detector's price histories as fallback.

    Returns (tape_lines, alerted_dirs, batch_id, flips).
    """
    now_ts = now_ts if now_ts is not None else _now()
    batch = _load_batch(batch_id) if batch_id else None
    if batch is None:
        try:
            with open(LATEST_FLIPS_PATH) as f:
                batch = json.load(f)
        except (OSError, ValueError):
            batch = None
    if isinstance(batch, dict):
        flips = batch.get("flips", [])
        # Exact-batch path uses BATCH_LOOKBACK_S: the stashed batch is a
        # frozen record, unlike live histories. Fallback path below keeps
        # the tighter TAPE_LOOKBACK_S.
        lookback = BATCH_LOOKBACK_S if batch_id else TAPE_LOOKBACK_S
        if flips and now_ts - batch.get("stashed_at", 0) <= lookback:
            lines, alerted_dirs = [], {}
            for fl in flips:
                coin = fl.get("coin", "?")
                d = fl.get("direction", "")
                line = "%s %s %.1fc->%.1fc in %ds%s%s" % (
                    coin, "UP" if "YES" in d else "DOWN",
                    fl.get("from_mid", 0) * 100, fl.get("to_mid", 0) * 100,
                    int(fl.get("span_s", 0)),
                    ", crossed 50c" if fl.get("crossed_50") else "",
                    (", %.1f min left" % fl.get("mins_left"))
                    if fl.get("mins_left") is not None else "")
                lines.append(line)
                alerted_dirs[coin] = "up" if "YES" in d else "down"
            return lines, alerted_dirs, batch.get("batch_id"), flips
    lines, alerted_dirs = _build_tape_from_histories(now_ts)
    return lines, alerted_dirs, None, []


def _build_tape_from_histories(now_ts):
    st = _load_json(HOOK_STATE_PATH, {})
    tickers = st.get("tickers", {})
    lines, alerted_dirs = [], {}
    for tk, ent in tickers.items():
        hist = [[ts, px] for ts, px in ent.get("hist", [])
                if now_ts - ts <= TAPE_LOOKBACK_S]
        if len(hist) < 2:
            continue
        old_ts, old_px = hist[0]
        new_px = hist[-1][1]
        move = new_px - old_px
        if abs(move) < TAPE_MIN_MOVE:
            continue
        if abs(move) > 0.25:
            continue  # bad-tick guard: a single 25c+ print is a stale
                      # last_price, not order flow
        coin = tk.split("15M")[0].replace("KX", "") if "15M" in tk else tk
        crossed = (old_px - 0.50) * (new_px - 0.50) < 0
        mins_left = None
        try:
            ct = ent.get("close_time") or ""
            if ct:
                mins_left = round(
                    (datetime.fromisoformat(ct.replace("Z", "+00:00"))
                     .timestamp() - now_ts) / 60, 1)
        except (ValueError, TypeError):
            pass
        line = ("%s %s %.1fc->%.1fc in %ds%s%s" % (
            coin, "UP" if move > 0 else "DOWN",
            old_px * 100, new_px * 100, int(hist[-1][0] - old_ts),
            ", crossed 50c" if crossed else "",
            (", %.1f min left" % mins_left)
            if mins_left is not None else ""))
        lines.append(line)
        if now_ts - ent.get("last_alert_ts", 0) <= RECENT_ALERT_S:
            d = ent.get("last_alert_dir")
            if d in ("up", "down"):
                alerted_dirs[coin] = d
    return lines, alerted_dirs


def render_messages(tape_lines, positions, model_snap, snap_age_s):
    pos_txt = ("none open"
               if not positions else "; ".join(
                   "%s %s (entry %s)" % (
                       p["coin"], p["side"],
                       ("%.1fc" % (p["entry_price_taken"] * 100))
                       if p.get("entry_price_taken") is not None
                       else "?")
                   for p in positions))
    tape_txt = "\n".join("- " + l for l in tape_lines)
    if model_snap:
        snap_txt = "; ".join(
            "%s fair %.0fc edge %s%.0fc%s" % (
                c, v.get("fair", 0) * 100,
                "+" if v.get("edge", 0) > 0 else "",
                v.get("edge", 0) * 100,
                " SIGNAL" if v.get("has_signal") else "")
            for c, v in sorted(model_snap.items()))
        snap_txt += (" (snapshot age %.0f seconds; under 180s = fresh and "
                     "usable, over 180s = stale)" % snap_age_s)
    else:
        snap_txt = "unavailable/stale — no model backing possible"
    system = (
        "You are a decisive tape-trading analyst for 15-minute crypto "
        "prediction markets on Kalshi (paper/analysis only). You make hard "
        "calls — BUY, CLOSE, HOLD, SKIP — from rapid price-move events "
        "('flips': a window's mid moved >=10c in ~30s) plus the "
        "quantitative model's fair value. Edge is in YES terms "
        "(fair - mid): positive edge favors YES, negative favors NO.\n"
        "Output EXACTLY three lines, nothing else:\n"
        "CALL: <BUY_YES | BUY_NO | CLOSE | HOLD | SKIP> <COIN> - "
        "<MODEL+TAPE | TAPE-ONLY>\n"
        "REASON: <one line: why, with the key numbers>\n"
        "WATCH: <one line: what would flip this call>\n"
        "Rules:\n"
        "- BUY_YES: tape surging toward YES on COIN and fresh model edge "
        ">= +5c on YES. BUY_NO: tape dumping toward NO and fresh model "
        "edge <= -5c (model favors NO).\n"
        "- Tape screaming but model shows no edge (or snapshot stale): "
        "still make the directional call, but label it TAPE-ONLY and say "
        "so in REASON. Never invent an edge number.\n"
        "- CLOSE: only for a coin with an OPEN position where the tape is "
        "violently adverse (>=10c against the position side). Otherwise "
        "HOLD.\n"
        "- SKIP: no open position, no model edge, tape mixed or "
        "unconvincing. SKIP is a decision — use it instead of hedging.\n"
        "Each line max %d chars. No invented numbers beyond the tape and "
        "the snapshot." % MAX_REASON_CHARS)
    user = ("Recent fast legs (mid-price, YES side; UP favors YES):\n"
            "%s\nOpen model positions: %s\n"
            "Model snapshot: %s" % (tape_txt, pos_txt, snap_txt))
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


CALL_ACTIONS = ("BUY_YES", "BUY_NO", "CLOSE", "HOLD", "SKIP")


def parse_call(text):
    """Strict parser for the CALL/REASON/WATCH contract.

    Returns (action, coin, conviction, reason, watch) or Nones.
    """
    if not text:
        return None, None, None, None, None
    vals = {}
    for line in text.strip().splitlines():
        for key in ("CALL", "REASON", "WATCH"):
            if line.strip().upper().startswith(key + ":"):
                vals[key] = line.split(":", 1)[1].strip()[
                    :MAX_REASON_CHARS]
    call, reason, watch = (vals.get("CALL"), vals.get("REASON"),
                           vals.get("WATCH"))
    if not (call and reason and watch):
        return None, None, None, None, None
    action = next((a for a in CALL_ACTIONS if a in call.upper()), None)
    conviction = ("MODEL+TAPE" if "MODEL+TAPE" in call.replace(" ", "")
                  else "TAPE-ONLY")
    coin = None
    for tok in call.replace("-", " ").split():
        t = tok.strip().upper()
        if t in ("BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "ZEC",
                 "NEAR", "BNB"):
            coin = t
            break
    if not action:
        return None, None, None, None, None
    return action, coin, conviction, reason, watch


def should_analyze(alerted_dirs, state, now_ts=None):
    """Cooldown gate with a reversal bypass.

    Direction memory is per-coin and merged across batches (with a TTL),
    so one single-coin batch can't wipe the regime context of other coins.
    Returns (ok, reason). Analyzes when the cooldown has elapsed, or
    immediately when any coin's alert direction reversed since its last
    recorded direction (regime change).
    """
    now_ts = now_ts if now_ts is not None else _now()
    if not alerted_dirs:
        return False, "no_alerts"
    # Backoff gate: after a full-chain backend failure, every batch would
    # otherwise hit all three providers again (the 2026-10-06 overnight
    # whipsaw did exactly that via the reversal bypass and burned both
    # keyed quotas). A dead backend stays dead for a few minutes; the
    # analyst self-recovers when the backoff expires.
    last_err = state.get("last_backend_error_ts", 0)
    if now_ts - last_err < BACKEND_ERROR_BACKOFF_S:
        return False, "backend_backoff"
    # No cooldown-style backoff beyond the above: the backend fails over
    # across providers (Gemini -> OpenRouter -> Pollinations), so a
    # throttled primary fails fast and the fallbacks still get their
    # chance. The 150s cooldown below is the rate limiter;
    # last_backend_error_ts is kept for diagnostics.
    dirs = state.get("dirs", {})
    reversed_coins = [
        c for c, d in alerted_dirs.items()
        if c in dirs and now_ts - dirs[c].get("ts", 0) <= DIR_TTL_S
        and dirs[c].get("dir") != d]
    last_ts = state.get("last_read_ts", 0)
    if reversed_coins:
        return True, "reversal:" + ",".join(sorted(reversed_coins))
    if now_ts - last_ts >= COOLDOWN_S:
        return True, "cooldown_elapsed"
    return False, "cooling_down"


def _merge_dirs(state, alerted_dirs, now_ts):
    dirs = state.get("dirs", {})
    for c, d in alerted_dirs.items():
        dirs[c] = {"dir": d, "ts": now_ts}
    # prune stale entries
    state["dirs"] = {c: v for c, v in dirs.items()
                     if now_ts - v.get("ts", 0) <= DIR_TTL_S}


def analyst_active():
    return (os.environ.get(ENABLED_ENV) == "1"
            and not os.path.exists(KILL_SWITCH_PATH))


def _log(entry):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def analyze_flips(backend=None, now_ts=None, batch_id=None):
    """One tape read. Returns dict or None. NEVER raises.

    When batch_id is given, analyzes that exact stashed batch (pairing the
    AI's call with the relayed handoff). Idempotent per batch: if this exact
    batch id was already processed, the cached result is returned without
    another model call — safe when two runners see the same handoff.
    """
    try:
        if not analyst_active():
            return None
        now_ts = now_ts if now_ts is not None else _now()
        tape_lines, alerted_dirs, used_batch_id, flips = build_tape(
            now_ts, batch_id)
        state = _load_json(STATE_PATH, {})
        if used_batch_id and used_batch_id == state.get("last_batch_id"):
            return state.get("last_result")
        ok, reason = should_analyze(alerted_dirs, state, now_ts)
        result = None
        snap_age_s = None
        if ok:
            positions = get_open_positions(now_ts)
            model_snap, snap_age_s = get_model_snap(now_ts)
            be = backend or CopilotBackend()
            text, latency_ms = be.complete(
                render_messages(tape_lines, positions, model_snap,
                                snap_age_s),
                timeout_s=LATENCY_BUDGET_S)
            action, coin, conviction, call_reason, watch = parse_call(text)
            if action:
                # Single-coin attribution: some fallback models (e.g.
                # Pollinations) return a valid CALL line but omit the coin
                # token. When the batch names exactly one coin the call is
                # unambiguous — attribute it. Multi-coin batches stay
                # unattributed and the hook's mismatch guard drops them.
                if coin is None:
                    batch_coins = sorted({
                        str(f.get("coin", "")).upper()
                        for f in flips if f.get("coin")})
                    if len(batch_coins) == 1:
                        coin = batch_coins[0]
                result = {"call": action, "coin": coin,
                          "conviction": conviction, "reason": call_reason,
                          "watch": watch, "latency_ms": latency_ms,
                          "backend": type(be).__name__}
                state["last_read_ts"] = now_ts
                _merge_dirs(state, alerted_dirs, now_ts)
                if used_batch_id:
                    state["last_batch_id"] = used_batch_id
                    state["last_result"] = result
                _save_json(STATE_PATH, state)
        # Every call is logged with its window/model context so hit rate
        # can be graded later (BUY_YES wins if YES settles, etc.).
        # call_ticker/call_window_close make each directional call gradeable
        # against Kalshi's published settlement.
        call_ticker, call_window_close = None, None
        if result and result.get("coin"):
            fl = next((f for f in flips
                       if f.get("coin") == result["coin"]), {})
            call_ticker = fl.get("ticker")
            call_window_close = fl.get("close_time")
        _log({
            "ts": datetime.now(timezone.utc).isoformat(),
            "gated": reason,
            "batch_id": batch_id or used_batch_id,
            "tape": tape_lines,
            "call": result["call"] if result else None,
            "call_coin": result["coin"] if result else None,
            "call_ticker": call_ticker,
            "call_window_close": call_window_close,
            "conviction": result["conviction"] if result else None,
            "model_snap_age_s": (round(snap_age_s, 1)
                                 if snap_age_s is not None else None),
            "latency_ms": result["latency_ms"] if result else None,
        })
        return result
    except Exception as e:
        # Never raise, but never swallow silently either: a backend 429 /
        # timeout looks identical to "gated" downstream if we don't log it.
        # Also stamp last_backend_error_ts so should_analyze backs off.
        try:
            st = _load_json(STATE_PATH, {})
            st["last_backend_error_ts"] = _now()
            _save_json(STATE_PATH, st)
        except Exception:
            pass
        try:
            _log({
                "ts": datetime.now(timezone.utc).isoformat(),
                "gated": "backend_error:%s" % type(e).__name__,
                "batch_id": batch_id,
                "tape": [],
                "call": None,
                "call_coin": None,
                "call_ticker": None,
                "call_window_close": None,
                "conviction": None,
                "model_snap_age_s": None,
                "latency_ms": None,
            })
        except Exception:
            pass
        return None


def main():
    # Optional --batch-id: analyze the exact stashed batch behind a relayed
    # handoff (keeps the AI's call paired with the alert being relayed).
    # No args: reads the latest stashed batch (or detector histories).
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", default=None)
    args = ap.parse_args()
    out = analyze_flips(batch_id=args.batch_id)
    print(json.dumps(out if out else {"read": None}))


if __name__ == "__main__":
    main()
