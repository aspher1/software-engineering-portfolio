#!/usr/bin/env python3
"""AI entry gate (v2 strategy) for the paper trader.

The rules engine finds candidate signals; this gate asks an LLM to approve
or veto each one before money (paper money) goes in.

FAIL-CLOSED: any error, timeout, unparseable output, missing backend binary,
exhausted daily cap, or quota outage -> VETO. The gate NEVER approves on
uncertainty. Exits, sizing, and all risk rails (SL/TP/trailing/drawdown
kill) stay 100% deterministic - the gate only votes on entries.

Every decision is journaled (ai_veto records, or an "ai" block on entries)
so the daily analysis can score whether the gate helps or hurts.
"""

import json
import os
import re
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RUN = os.path.dirname(os.path.abspath(__file__))
JOURNAL = os.path.join(RUN, "trades.jsonl")

def _run_cmd(argv, timeout, stdin_text=None, cwd=None):
    """Subprocess seam (monkeypatched in tests)."""
    return subprocess.run(argv, input=stdin_text, capture_output=True,
                          text=True, timeout=timeout,
                          cwd=cwd or RUN)


def _resolve_bin(candidates):
    """First candidate path that exists, else None."""
    if isinstance(candidates, str):
        candidates = [candidates]
    for c in candidates:
        c = os.path.expanduser(c)
        if c and os.path.exists(c):
            return c
    return None


PROMPT = """You are the entry screener for a paper-money momentum trading bot. A rules engine flagged a candidate entry. You vote APPROVE or VETO. Be skeptical: most breakouts fail, and doing nothing is a valid outcome.

CANDIDATE
- Symbol: {symbol} ({sleeve})
- Price: {price}
- Momentum: {momentum_line}
- Session: {timestamp_et} ({session_note})
- Portfolio: {open_count}/{max_pos} positions open, ${size}/trade paper money
- Risk rails (fixed, you cannot change them): stop-loss {sl}%, take-profit {tp}%, trailing stop {trail}%, entries halt at {kill}% drawdown

VETO unless the move shows genuine sustained pressure. Typical veto reasons: a vertical spike with no base (likely to snap back), a move already extended far past the entry bar, thin weekend/lunch-hour volume noise, or choppy back-and-forth rather than a real trend.

First write 1-2 sentences of analysis in your own words. Do not use any tools; judge only from the numbers above. Then end your reply with EXACTLY these three lines, filled in with your own judgment. Never copy the parenthesised hints verbatim - a copied hint is not a valid vote.
DECISION: (APPROVE or VETO - pick one)
CONFIDENCE: (a number 0-100)
REASON: (one sentence in your own words)
"""


def _today():
    return datetime.now(ET).strftime("%Y-%m-%d")


def journal(rec):
    rec = dict(rec)
    rec["ts"] = datetime.now(ET).isoformat()
    try:
        with open(JOURNAL, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


class AIGate:
    def __init__(self, cfg):
        g = cfg or {}
        self.enabled = bool(g.get("enabled", False))
        # Backend chain, tried in order. "cursor" = Cursor agent CLI on the
        # operator's login; "codex" = Codex CLI on the ChatGPT login.
        if g.get("backends"):
            self.backends = list(g["backends"])
        else:
            self.backends = [g.get("backend", "codex")]
        # codex 0.149.0 reads the prompt from stdin and needs the trust
        # flag outside trusted dirs; it rejects gpt-6-sol on ChatGPT
        # accounts, so model=None means the account default.
        self.model = g.get("model")
        self.codex_bins = g.get("codex_bin",
                                ["/usr/bin/codex",
                                 "/opt/hatch-image/bin/codex"])
        self.codex_args = g.get("codex_args",
                                ["exec", "--skip-git-repo-check"])
        self.prompt_via_stdin = bool(g.get("prompt_via_stdin", True))
        # cursor agent CLI takes the prompt as a positional arg. It stalls
        # on a confirmation prompt when run inside the bot dir, so it runs
        # from a neutral cwd (the prompt is self-contained).
        self.cursor_bins = g.get("cursor_bin",
                                 ["~/.local/bin/agent", "/usr/bin/agent"])
        self.cursor_args = g.get("cursor_args",
                                 ["-p", "--trust",
                                  "--output-format", "text"])
        self.cursor_cwd = g.get("cursor_cwd", "/tmp")
        self.codex_cwd = g.get("codex_cwd", RUN)
        self.timeout = int(g.get("timeout_sec", 120))
        self.max_calls = int(g.get("max_calls_per_day", 20))

    # -- public API ----------------------------------------------------
    def evaluate(self, cfg, state, sleeve, symbol, price, momentum_pct,
                 reason, momentum_4h=None):
        """Returns (proceed, ai_info).

        proceed=True  -> entry may go ahead; ai_info is None (gate off) or a
                         dict with the model's confidence/reason (approved).
        proceed=False -> vetoed; the veto is journaled and counted here.
        """
        if not self.enabled:
            return True, None
        gate = self._roll(state)
        if gate["calls"] >= self.max_calls:
            self._veto(state, sleeve, symbol, price, momentum_pct,
                       "daily call cap reached (%d)" % self.max_calls, 0)
            return False, None
        gate["calls"] += 1
        approved, conf, why, ms, backend = self._ask(
            cfg, state, sleeve, symbol, price, momentum_pct,
            momentum_4h, reason)
        if approved:
            gate["approvals"] = gate.get("approvals", 0) + 1
            return True, {"decision": "APPROVE", "confidence": conf,
                          "reason": why, "latency_ms": ms,
                          "backend": backend,
                          "model": self.model or "default"}
        if why.startswith("QUOTA_EXHAUSTED"):
            gate["quota_vetoes"] = gate.get("quota_vetoes", 0) + 1
            why = "all backends quota-exhausted - fail-closed veto"
        else:
            gate["vetoes"] = gate.get("vetoes", 0) + 1
        self._veto(state, sleeve, symbol, price, momentum_pct, why, conf,
                   backend)
        return False, None

    # -- internals ------------------------------------------------------
    def _roll(self, state):
        gate = state.setdefault("ai_gate", {})
        if gate.get("date") != _today():
            gate.update({"date": _today(), "calls": 0,
                         "approvals": 0, "vetoes": 0})
        return gate

    def _veto(self, state, sleeve, symbol, price, momentum_pct, why, conf,
              backend=None):
        journal({"type": "ai_veto", "strategy": "v2-ai-gate",
                 "sleeve": sleeve, "symbol": symbol, "price": price,
                 "momentum_pct": momentum_pct, "ai_reason": why,
                 "ai_confidence": conf, "ai_backend": backend})
        try:
            with open(os.path.join(RUN, "bot.log"), "a") as f:
                f.write("[%s] AI VETO %s [%s] (%s)\n"
                        % (datetime.now(ET).strftime("%H:%M:%S"),
                           symbol, sleeve, why))
        except OSError:
            pass

    def _context(self, cfg, state, sleeve, symbol, price, momentum_pct,
                 momentum_4h, reason):
        if sleeve == "crypto":
            mom_line = ("+%.1f%% over 60 min" % momentum_pct)
            if momentum_4h is not None:
                mom_line += (", %+.1f%% over 240 min" % momentum_4h)
            session_note = "crypto trades 24/7"
        else:
            mom_line = "+%.1f%% today" % momentum_pct
            session_note = "US equity regular session"
        sleeve_cfg = cfg.get(sleeve, {})
        return {
            "symbol": symbol,
            "sleeve": sleeve,
            "price": "%.4f" % price,
            "momentum_line": mom_line,
            "timestamp_et": datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"),
            "session_note": session_note,
            "open_count": len(state.get("positions", {})),
            "max_pos": cfg.get("max_positions", 5),
            "size": "%.2f" % cfg.get("trade_size", 100),
            "sl": abs(sleeve_cfg.get("sl_pct", 4)),
            "tp": sleeve_cfg.get("tp_pct", 6),
            "trail": sleeve_cfg.get("trail_pct", 3),
            "kill": cfg.get("drawdown_kill_pct", 12),
        }

    def _ask(self, cfg, state, sleeve, symbol, price, momentum_pct,
             momentum_4h, reason):
        """Try each backend in order; first parseable vote wins.

        Returns (approved, confidence, why, latency_ms, backend_name).
        Fail closed: every backend exhausted -> VETO.
        """
        prompt = PROMPT.format(**self._context(
            cfg, state, sleeve, symbol, price, momentum_pct,
            momentum_4h, reason))
        quota_hits = []
        for name in self.backends:
            ok, out, ms, err = self._ask_one(name, prompt)
            if not ok:
                if err == "quota":
                    quota_hits.append(name)
                continue  # try the next backend
            approved, conf, why, _ = self._parse(out, ms)
            if why == "unparseable model output":
                continue  # next backend may answer cleanly
            return approved, conf, why, ms, name
        if quota_hits and len(quota_hits) == len(self.backends):
            return False, 0, "QUOTA_EXHAUSTED: %s quota hit" \
                % "+".join(quota_hits), 0, None
        return False, 0, "all backends unavailable", 0, None

    def _ask_one(self, name, prompt):
        """One backend call. Returns (ok, output, ms, err_kind).

        err_kind in {"quota", "unavailable", "timeout", "error"}.
        """
        if name == "codex":
            b = _resolve_bin(self.codex_bins)
            if not b:
                return False, "", 0, "unavailable"
            argv = [b] + list(self.codex_args)
            if self.model:
                argv += ["--model", self.model]
            if self.prompt_via_stdin:
                argv, stdin_text = argv, prompt
            else:
                argv, stdin_text = argv + [prompt], None
            cwd = self.codex_cwd
        elif name == "cursor":
            b = _resolve_bin(self.cursor_bins)
            if not b:
                return False, "", 0, "unavailable"
            argv = [b] + list(self.cursor_args) + [prompt]
            stdin_text = None
            cwd = self.cursor_cwd
        else:
            return False, "", 0, "unavailable"
        t0 = datetime.now(ET)
        try:
            p = _run_cmd(argv, self.timeout, stdin_text, cwd)
            out = (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            return False, "", self.timeout * 1000, "timeout"
        except Exception:
            return False, "", 0, "error"
        ms = int((datetime.now(ET) - t0).total_seconds() * 1000)
        if re.search(r"usage limit|try again at|rate.?limit[^a-z]|"
                     r"too many requests|\bquota\b", out, re.I):
            return False, "", ms, "quota"
        return True, out, ms, ""

    @staticmethod
    def _parse(out, ms):
        m_dec = re.search(r"^DECISION:\s*(APPROVE|VETO)\s*$", out,
                          re.M | re.I)
        m_conf = re.search(r"^CONFIDENCE:\s*(\d{1,3})\s*$", out, re.M)
        m_rea = re.search(r"^REASON:\s*(.+?)\s*$", out, re.M)
        if not (m_dec and m_conf and m_rea):
            return False, 0, "unparseable model output", ms
        conf = max(0, min(100, int(m_conf.group(1))))
        why = m_rea.group(1).strip()[:200]
        if m_dec.group(1).upper() == "APPROVE":
            return True, conf, why, ms
        return False, conf, why, ms
