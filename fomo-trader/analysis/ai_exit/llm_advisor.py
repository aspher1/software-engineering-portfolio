"""Network-LLM exit advisor. DEFAULT OFF. ADVISORY CADENCE ONLY.

This backend asks a frontier model (Codex CLI on the user's ChatGPT login,
Cursor agent CLI as fallback) for a second opinion on an open position.
It is NOT a hot-path component, by architecture:

  Latency math (honest):
    - Codex CLI `codex exec` cold invocation: typically 15-60s+ wall time
      (sandbox startup + model round trip). Observed smoke tests in this
      repo land in the tens of seconds even for trivial prompts.
    - Cursor agent CLI: similar order, plus occasional transient
      "socket hang up" errors (retry once).
    - The bot's manage tick is sell_poll_sec = 2s (live config). A 30s LLM
      call cannot participate in a 2s decision loop.
  Therefore the LLM advisor may run at most on an ADVISORY CADENCE:
    default every 300s per open position, only when enabled, with a daily
    call cap (default 50). It produces a second opinion that the operator
    reviews; it never blocks or delays the deterministic exits.

  Cost math (honest):
    - Every call burns the user's Codex/Cursor usage quota. Codex usage was
      exhausted 2026-09-27 ~17:31 EDT (reset ~23:06 ET). A 3-position book
      at 5-min cadence is 36 calls/hour - enough to burn a quota alone.
    - Standing repo rule: no Codex/Cursor/Grok/network LLM in the trading
      hot path. This backend complies by never being in the hot path.

Fail-closed contract (tested):
    timeout, non-zero exit, missing binary, empty output, or unparseable
    output -> HOLD ("no opinion"), NEVER exit. An LLM that cannot speak
    clearly does not get to sell the user's position.

Output contract demanded of the model (strict):
    A single line:  DECISION: EXIT|HOLD | CONFIDENCE: 0.00-1.00 | REASON: <short>
    Anything else -> HOLD.

SHADOW-ONLY: even when enabled, advice feeds the shadow evaluator and the
gatekeeper in advisor.py. Nothing here touches the live manage loop.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from typing import Dict, List, Optional, Sequence, Tuple

from .advisor import (ExitAdvice, ExitAdvisor, PositionState, QuoteTick,
                      filter_quotes, hold, exit_now, valid_marks)

# Verified 2026-09-27. Codex usage exhausted ~17:31 EDT, reset ~23:06 ET.
CODEX_BIN = "/usr/bin/codex"
CODEX_MODEL = "gpt-6-sol"
CODEX_ARGS = ["exec", "--model", CODEX_MODEL, "-s", "workspace-write"]
# Cursor CLI: --force (alias --yolo) is MANDATORY or write/exec tool calls
# get rejected. Verified working 2026-09-27.
CURSOR_BIN = "/home/hatch/.local/bin/agent"
CURSOR_ARGS = ["-p", "--trust", "--force", "--output-format", "text"]

DECISION_RE = re.compile(
    r"DECISION:\s*(EXIT|HOLD)\s*\|\s*CONFIDENCE:\s*([0-9]*\.?[0-9]+)\s*\|\s*REASON:\s*(.+)",
    re.IGNORECASE | re.DOTALL)

PROMPT_TEMPLATE = """You are an exit-risk second opinion for a PAPER-TRADING memecoin bot (Solana/BSC).
You do NOT trade. You advise EXIT (sell the rest of the position now) or HOLD (no opinion; the bot's deterministic stops decide).

Position:
- mint: {mint} [{chain}]
- entry: {entry:.8f}  peak: {peak:.8f}  now: {now:.8f}
- gain now: {gain_now:+.1f}%   peak gain: {peak_gain:+.1f}%   drawdown from peak: {dd_peak:.1f}%
- age: {age_min:.1f} min   TP rungs already fired: {rungs}
- deterministic floor (ALWAYS active regardless of you): hard stop -{hard}%, trailing stop -{trail}% from peak, TP ladder {tps}, dump exit -{dump}%/{dump_win}s, stale exit {stale}m

Recent marks (USD, oldest -> newest, ~{cadence}s apart; these are MARKS not fills):
{marks}

Consider: round-trip-from-peak (pumped then faded back near entry), accelerating drawdown, momentum decay, rug signatures (vertical collapse). Recommend EXIT only if holding looks worse than exiting now at the mark. When in doubt, HOLD.

Reply with EXACTLY one line and nothing else:
DECISION: EXIT|HOLD | CONFIDENCE: 0.00-1.00 | REASON: <under 12 words>
"""


class LLMAdvisor(ExitAdvisor):
    """Network-LLM second opinion. Disabled by default."""

    name = "llm_advisor"

    def __init__(self, enabled: bool = False, backend: str = "codex",
                 timeout_s: float = 90.0, min_interval_s: float = 300.0,
                 max_calls_per_day: int = 50, marks_shown: int = 48):
        self.enabled = enabled
        self.backend = backend  # "codex" or "cursor"
        self.timeout_s = timeout_s
        self.min_interval_s = min_interval_s
        self.max_calls_per_day = max_calls_per_day
        self.marks_shown = marks_shown
        self._last_call_ts = 0.0
        self._calls_today = 0
        self._day = time.strftime("%Y-%m-%d")

    # -- plumbing ---------------------------------------------------------
    def _quota_ok(self) -> Tuple[bool, str]:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._calls_today = 0
        if self._calls_today >= self.max_calls_per_day:
            return False, "daily call cap reached (%d)" % self.max_calls_per_day
        if time.time() - self._last_call_ts < self.min_interval_s:
            return False, "advisory cadence: called %.0fs ago, min interval %.0fs" % (
                time.time() - self._last_call_ts, self.min_interval_s)
        return True, ""

    def _build_prompt(self, pos: PositionState,
                      marks: List[Tuple[float, float]]) -> str:
        shown = marks[-self.marks_shown:]
        entry = pos.entry_price or 1.0
        gain_now = (pos.current_price - entry) / entry * 100.0 if entry else 0.0
        peak_gain = (pos.peak_price - entry) / entry * 100.0 if entry else 0.0
        dd_peak = ((pos.peak_price - pos.current_price) / pos.peak_price * 100.0
                   if pos.peak_price else 0.0)
        cfg = pos.exit_cfg or {}
        marks_str = ", ".join("%.6g" % p for _, p in shown) or "(no marks)"
        cadence = ""
        if len(shown) >= 2:
            cadence = "%.0f" % ((shown[-1][0] - shown[0][0]) / max(1, len(shown) - 1))
        return PROMPT_TEMPLATE.format(
            mint=pos.mint[:12], chain=pos.chain, entry=pos.entry_price,
            peak=pos.peak_price, now=pos.current_price, gain_now=gain_now,
            peak_gain=peak_gain, dd_peak=dd_peak,
            age_min=(pos.now_ts - pos.opened_ts) / 60.0, rungs=pos.rungs_fired,
            hard=cfg.get("hard_stop_pct", "?"), trail=cfg.get("trailing_stop_pct", "?"),
            tps=cfg.get("take_profits", "?"), dump=cfg.get("dump_drop_pct", "?"),
            dump_win=cfg.get("dump_window_sec", "?"),
            stale=cfg.get("stale_exit_min", "?"),
            marks=marks_str, cadence=cadence or "?")

    def _run_cli(self, prompt: str) -> Tuple[bool, str]:
        """Run the model CLI. Returns (ok, stdout_or_error). Never raises."""
        if self.backend == "codex":
            exe = shutil.which(CODEX_BIN) or (CODEX_BIN if shutil.which("codex") else None)
            if not exe:
                return False, "codex binary not found"
            cmd = [CODEX_BIN] + CODEX_ARGS + [prompt]
            cwd = "/tmp"
        elif self.backend == "cursor":
            if not shutil.which(CURSOR_BIN):
                return False, "cursor agent binary not found"
            cmd = [CURSOR_BIN] + CURSOR_ARGS + [prompt]
            cwd = "/tmp"  # cursor stalls on confirmation prompts in some cwds
        else:
            return False, "unknown backend %r" % self.backend
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout_s, cwd=cwd)
        except subprocess.TimeoutExpired:
            return False, "timeout after %.0fs" % self.timeout_s
        except OSError as e:
            return False, "exec failed: %s" % e
        if proc.returncode != 0:
            err = (proc.stderr or "")[-200:]
            return False, "exit %d: %s" % (proc.returncode, err)
        return True, (proc.stdout or "").strip()

    @staticmethod
    def parse_output(text: str) -> Optional[Tuple[str, float, str]]:
        """Strict parse of the one-line contract. None = unparseable."""
        if not text:
            return None
        m = DECISION_RE.search(text.strip().splitlines()[0] if "\n" in text else text)
        # also try: find the pattern anywhere in the first 3 lines
        if not m:
            for line in text.strip().splitlines()[:3]:
                m = DECISION_RE.search(line)
                if m:
                    break
        if not m:
            return None
        decision = m.group(1).upper()
        try:
            conf = float(m.group(2))
        except ValueError:
            return None
        if not (0.0 <= conf <= 1.0):
            return None
        reason = m.group(3).strip()[:160]
        return decision, conf, reason

    # -- ExitAdvisor interface ---------------------------------------------
    def advise(self, pos: PositionState,
               quotes: Sequence[QuoteTick],
               as_of_ts: Optional[float] = None) -> ExitAdvice:
        t0 = time.perf_counter()
        ms = lambda: (time.perf_counter() - t0) * 1000.0  # noqa: E731
        if not self.enabled:
            return hold("llm backend disabled (default)", self.name, ms())
        ok, why = self._quota_ok()
        if not ok:
            return hold("llm throttled: " + why, self.name, ms())
        seen = filter_quotes(quotes, as_of_ts)
        marks = valid_marks(seen)
        if len(marks) < 6:
            return hold("llm: insufficient marks", self.name, ms(),
                        {"n_marks": len(marks)})
        prompt = self._build_prompt(pos, marks)
        self._last_call_ts = time.time()
        self._calls_today += 1
        ok, out = self._run_cli(prompt)
        if not ok:
            # fail-closed: a silent/broken model never sells
            return hold("llm call failed (fail-closed): " + out, self.name, ms())
        parsed = self.parse_output(out)
        if not parsed:
            return hold("llm unparseable output (fail-closed)",
                        self.name, ms(), {"raw": out[:200]})
        decision, conf, reason = parsed
        if decision == "EXIT":
            return exit_now("llm(%s): %s" % (self.backend, reason),
                            self.name, ms(), {"n_marks": len(marks)}, conf)
        return hold("llm(%s): %s" % (self.backend, reason),
                    self.name, ms(), {"n_marks": len(marks)}, conf)
