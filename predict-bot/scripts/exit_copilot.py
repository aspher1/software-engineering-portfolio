"""Exit copilot: a fast-LLM advisory layer on top of exit decisions.

Paper/analysis only. Advisory only — it NEVER changes entry, exit, or
grading logic. It can only add a second opinion ("EXIT"|"HOLD" + one line)
to an exit the deterministic model already produced.

Design contract (user-approved 2026-10-02):
- Runs at most ONCE per scan, on the most urgent exit only.
- Tight prompt (~200 tokens in, ~20 out); latency budget 4s; on timeout
  or any error it returns None silently and the scan continues.
- Disabled unless EXIT_COPILOT_ENABLED=1 and no kill-switch file exists.
  The Groq credential is the user-connected `custom.groq` Secure Vault
  connector; the raw key never appears here.
- Kill switch: touch data/exit_copilot.kill to disable instantly,
  no code change needed.
- Every call is logged to data/exit_copilot_log.jsonl for later review.

Backend: Groq free tier (llama-3.1-8b-instant), sub-second typical.
MockBackend exists for tests and for running the harness with no key.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_DIR, "data")
KILL_SWITCH_PATH = os.path.join(DATA_DIR, "exit_copilot.kill")
LOG_PATH = os.path.join(DATA_DIR, "exit_copilot_log.jsonl")

# ---------------------------------------------------------------- providers
# Groq's API is Cloudflare-walled against this VM's egress IPs (HTTP 403,
# Cloudflare error 1010 on 2026-10-02) even with a valid key, so the live
# default is Gemini's free tier over its OpenAI-compatible endpoint.
# Groq config is kept for environments where it is reachable.

PROVIDERS = {
    "gemini": {
        "url": ("https://generativelanguage.googleapis.com/v1beta"
                "/openai/chat/completions"),
        "credential": "custom.gemini",
        "hosts": ("generativelanguage.googleapis.com",),
        "model": "gemini-2.5-flash",
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "credential": "custom.groq",
        "hosts": ("api.groq.com",),
        "model": "llama-3.1-8b-instant",
    },
    "pollinations": {
        # Keyless free fallback (no signup, OpenAI-compatible). Used only
        # when the primary provider fails (e.g. Gemini 429). Anonymous tier
        # is rate-limited too, so the call-rate guards still apply.
        "url": "https://text.pollinations.ai/openai",
        "credential": None,
        "hosts": ("text.pollinations.ai",),
        "model": "openai",
    },
    "openrouter": {
        # Free-tier models via OpenRouter (user adds key to Secure Vault as
        # custom.openrouter). More reliable than pollinations; tried before
        # pollinations in the fallback chain.
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "credential": "custom.openrouter",
        "hosts": ("openrouter.ai",),
        "model": "poolside/laguna-s-2.1:free",
    },
}
PROVIDER_ENV = "EXIT_COPILOT_PROVIDER"  # "gemini" (default) or "groq"
# Failover chain tried in order when the primary provider raises.
FALLBACK_PROVIDERS = ("openrouter", "pollinations")

# Provider health: when a provider fails, it is skipped for a cooldown so
# the chain stops burning budget on a dead backend. 2026-10-06: Gemini and
# OpenRouter sat at hard quota 429s for hours while every flip batch
# retried them (and hammered Pollinations' anonymous tier in the process).
PROVIDER_HEALTH_PATH = os.path.join(DATA_DIR, "llm_provider_health.json")
PROVIDER_429_COOLDOWN_S = 3600  # quota 429s reset slowly; don't hammer
PROVIDER_FAIL_COOLDOWN_S = 300  # other failures: short cooldown


def _provider_name(cfg):
    for name, c in PROVIDERS.items():
        if c is cfg or c.get("url") == cfg.get("url"):
            return name
    return cfg.get("url", "unknown")


def _load_provider_health():
    try:
        with open(PROVIDER_HEALTH_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_provider_health(health):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(PROVIDER_HEALTH_PATH, "w") as f:
            json.dump(health, f)
    except Exception:
        pass  # health tracking is best-effort; never break a call


def _mark_provider_down(name, http_code):
    try:
        health = _load_provider_health()
        cooldown = (PROVIDER_429_COOLDOWN_S if http_code == 429
                    else PROVIDER_FAIL_COOLDOWN_S)
        health[name] = {
            "unhealthy_until": time.time() + cooldown,
            "reason": ("http_%s" % http_code) if http_code else "error",
        }
        _save_provider_health(health)
    except Exception:
        pass


def _provider_cfg():
    name = os.environ.get(PROVIDER_ENV, "gemini")
    return name, PROVIDERS.get(name, PROVIDERS["gemini"])


ENABLED_ENV = "EXIT_COPILOT_ENABLED"
LATENCY_BUDGET_S = 4.0
MAX_REASON_CHARS = 140


# ---------------------------------------------------------------- snapshot

def build_snapshot(exit_dict, recent_flips=None, news_flag=None):
    """Compress one exit dict into the minimal decision snapshot.

    exit_dict: one element of check_exits() output (taken-side fields).
    recent_flips: optional list of short strings, e.g. ["YES 20c->38c/22s"].
    news_flag: optional short string, e.g. "ETF headline 3m ago", else None.
    """
    return {
        "market": exit_dict.get("title"),
        "ticker": exit_dict.get("ticker"),
        "side": exit_dict.get("side"),
        "entry_price": exit_dict.get("entry_price_taken"),
        "entry_edge": exit_dict.get("entry_edge_taken"),
        "now_price": exit_dict.get("now_price_taken"),
        "now_fair": exit_dict.get("now_fair_taken"),
        "now_edge": exit_dict.get("now_edge"),
        "reason": exit_dict.get("reason"),  # edge_gone | edge_reversed
        "price_move": exit_dict.get("exit_direction"),  # favourable|adverse
        "mins_left": exit_dict.get("mins_left"),
        "recent_flips": recent_flips or [],
        "news": news_flag,
    }


# ---------------------------------------------------------------- prompt

_SYSTEM = (
    "You are an exit copilot for 15-minute Kalshi crypto prediction markets. "
    "The pricing model already decided the trade thesis is dead. Your job: "
    "give a second opinion on whether to exit IMMEDIATELY or whether holding "
    "to the close could still pay. Reply in exactly two lines:\n"
    "DECISION: EXIT or HOLD\n"
    "REASON: one short sentence, max 140 chars"
)


def render_prompt(snapshot):
    """Render the compact user prompt for one snapshot."""
    s = snapshot
    flips = "; ".join(s["recent_flips"]) if s["recent_flips"] else "none"
    lines = [
        f"Market: {s['market']} ({s['ticker']}), side taken: {s['side']}",
        f"Entry: {s['entry_price']}c @ edge {s['entry_edge']}; "
        f"now: {s['now_price']}c, fair {s['now_fair']}c, edge {s['now_edge']}",
        f"Model exit reason: {s['reason']}; price move since entry: {s['price_move']}",
        f"Minutes left in window: {s['mins_left']}",
        f"Recent rapid flips: {flips}",
        f"News flag: {s['news'] or 'none'}",
        "Second opinion?",
    ]
    return "\n".join(lines)


def render_messages(snapshot):
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": render_prompt(snapshot)},
    ]


# ---------------------------------------------------------------- parsing

def parse_decision(text):
    """Parse 'DECISION: EXIT|HOLD' + 'REASON: ...' from model output.

    Returns (decision, reason) or (None, None) when unparseable.
    Never raises.
    """
    try:
        decision, reason = None, None
        for line in (text or "").splitlines():
            up = line.strip().upper()
            if up.startswith("DECISION:"):
                val = up.split(":", 1)[1].strip().split()[0]
                if val in ("EXIT", "HOLD"):
                    decision = val
            elif up.startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()[:MAX_REASON_CHARS]
        if decision:
            return decision, reason or ""
        return None, None
    except Exception:
        return None, None


# ---------------------------------------------------------------- backends

class MockBackend:
    """Deterministic stand-in: no network, no key. For tests and dry runs."""

    def __init__(self, decision="HOLD", reason="mock backend", latency_ms=5):
        self._decision = decision
        self._reason = reason
        self._latency_ms = latency_ms
        self.calls = []

    def complete(self, messages, timeout_s=LATENCY_BUDGET_S):
        self.calls.append(messages)
        text = f"DECISION: {self._decision}\nREASON: {self._reason}"
        return text, self._latency_ms


class CopilotBackend:
    """Fast-LLM backend via the user's Secure Vault connector.

    The raw key never touches this code: add_surrogate_to_request attaches
    a surrogate that authd exchanges at request time. Raises on any failure.
    Provider is chosen by EXIT_COPILOT_PROVIDER ("gemini" default, "groq").
    """

    def __init__(self, model=None, provider=None):
        if provider is None:
            _, cfg = _provider_cfg()
        else:
            cfg = PROVIDERS.get(provider, PROVIDERS["gemini"])
        self.cfg = cfg
        self.model = model or cfg["model"]

    @property
    def backend_name(self):
        return type(self).__name__

    def complete(self, messages, timeout_s=LATENCY_BUDGET_S):
        """Complete via primary provider, failing over to FALLBACK_PROVIDERS.

        Providers still inside their failure cooldown are skipped, so a
        quota-dead primary stops eating the latency budget (and hammering
        the anonymous fallback tier) on every call. Each provider gets
        whatever is left of the latency budget; a 429 on the primary fails
        fast so the fallback usually gets a full window.
        Raises RuntimeError only if every provider fails.
        """
        t0 = time.monotonic()
        primary = self.cfg["url"]
        full_chain = [self.cfg] + [
            PROVIDERS[p] for p in FALLBACK_PROVIDERS
            if PROVIDERS[p]["url"] != primary]
        health = _load_provider_health()
        now = time.time()
        chain = [c for c in full_chain
                 if health.get(_provider_name(c), {}).get(
                     "unhealthy_until", 0) <= now]
        if not chain:
            chain = full_chain  # best effort: everything is marked down
        last_err = None
        flaky = []  # (name, cfg) with transient (non-429) failures: retried
        for i, cfg in enumerate(chain):
            remaining = timeout_s - (time.monotonic() - t0)
            if remaining < 1.0:
                break
            # Cap the primary at 2s: a throttled Gemini sometimes hangs
            # instead of fast-failing, which would starve the fallbacks.
            if i == 0 and len(chain) > 1:
                remaining = min(remaining, 2.0)
            try:
                return self._complete_with(cfg, messages, timeout_s=remaining)
            except Exception as e:
                last_err = e
                code = getattr(e, "_http_code", None)
                name = _provider_name(cfg)
                if code == 429:
                    _mark_provider_down(name, code)
                else:
                    flaky.append((name, cfg))
        # One retry pass for transient failures (the flaky anonymous tier):
        # 429s are never retried within a call — their cooldown is
        # authoritative. A provider that fails twice gets its cooldown.
        for name, cfg in flaky:
            remaining = timeout_s - (time.monotonic() - t0)
            if remaining < 1.0:
                break
            try:
                return self._complete_with(cfg, messages, timeout_s=remaining)
            except Exception as e:
                last_err = e
                _mark_provider_down(name, getattr(e, "_http_code", None))
        raise RuntimeError(
            f"copilot call failed: {type(last_err).__name__}"
            if last_err else "copilot call failed: budget exhausted")

    def _complete_with(self, cfg, messages, timeout_s):
        sys.path.insert(
            0, "/opt/hatch/skills/skill-creator/bin")
        from dynamic_credentials import (
            add_surrogate_to_request, read_json_response)
        body = {
            "model": self.model if cfg is self.cfg else cfg["model"],
            "messages": messages,
            "max_tokens": 150,
        }
        # Pollinations' OpenAI endpoint 500s on unknown/unsupported params;
        # keep its body minimal. Gemini/Groq accept temperature fine.
        if "pollinations" not in cfg["url"]:
            body["temperature"] = 0.2
        # Gemini 2.5+ "thinks" by default and thinking tokens eat the
        # output budget -> responses got cut mid-sentence. This provider
        # hint disables thinking on Gemini's OpenAI-compatible endpoint
        # (other providers ignore unknown extra_body fields).
        if "google" in cfg["url"] or "generativelanguage" in cfg["url"]:
            body["extra_body"] = {"google": {
                "thinking_config": {"thinking_budget": 0}}}
        req = urllib.request.Request(
            cfg["url"], data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        if cfg.get("credential"):
            add_surrogate_to_request(
                req, cfg["credential"], allowed_hosts=cfg["hosts"])
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = read_json_response(resp)
        except Exception as e:
            # Preserve the HTTP status so the failover chain can tell a
            # quota 429 (long cooldown) from a transient blip.
            err = RuntimeError(f"copilot call failed: {type(e).__name__}")
            err._http_code = getattr(e, "code", None)
            raise err
        latency_ms = int((time.monotonic() - t0) * 1000)
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("copilot response unparseable")
        return text, latency_ms


# Backwards-compatible alias (original name from the first build).
GroqBackend = CopilotBackend


# ---------------------------------------------------------------- gating

def copilot_active():
    """True only when explicitly enabled and not kill-switched.

The Groq credential lives in the Secure Vault (custom.groq connector);
a missing/bad credential surfaces as a failed call, which
maybe_copilot_review already turns into a silent None.
"""
    if os.environ.get(ENABLED_ENV) != "1":
        return False
    if os.path.exists(KILL_SWITCH_PATH):
        return False
    return True


def _log(entry):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # logging must never break the scan


# ---------------------------------------------------------------- entry point

def maybe_copilot_review(exit_dict, recent_flips=None, news_flag=None,
                         backend=None):
    """Advisory second opinion on one exit. Returns dict or None.

    Returns None when disabled, on timeout, on any backend error, or when
    the model output is unparseable. NEVER raises: safe to call inline in
    the scan loop.
    """
    try:
        if backend is None and not copilot_active():
            return None
        snapshot = build_snapshot(exit_dict, recent_flips, news_flag)
        messages = render_messages(snapshot)
        be = backend or CopilotBackend()
        text, latency_ms = be.complete(messages, timeout_s=LATENCY_BUDGET_S)
        decision, reason = parse_decision(text)
        result = None
        if decision:
            result = {"decision": decision, "reason": reason,
                      "latency_ms": latency_ms,
                      "backend": type(be).__name__}
        _log({
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticker": snapshot.get("ticker"),
            "side": snapshot.get("side"),
            "model_reason": snapshot.get("reason"),
            "decision": decision,
            "latency_ms": latency_ms,
        })
        return result
    except Exception:
        return None
