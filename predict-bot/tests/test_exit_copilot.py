"""Tests for the exit-copilot advisory layer.

Contract under test:
- advisory only: never raises, returns None when disabled/failing
- disabled by default (env flag off, no key, kill switch)
- parses DECISION/REASON strictly; unparseable -> None
- one call per review max; latency budget respected by backend timeout
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import exit_copilot as ec


def _exit_dict():
    return {
        "ticker": "KXETH15M-26OCT021445-45",
        "title": "ETH price up in next 15 mins?",
        "side": "NO",
        "reason": "edge_gone",
        "entry_price_taken": 0.655,
        "entry_edge_taken": 0.18,
        "now_price_taken": 0.585,
        "now_fair_taken": 0.571,
        "now_edge": -0.014,
        "exit_direction": "adverse",
        "mins_left": 8.0,
    }


# --- snapshot / prompt ----------------------------------------------------

def test_build_snapshot_keeps_only_decision_fields():
    s = ec.build_snapshot(_exit_dict(), recent_flips=["YES 20c->38c/22s"],
                          news_flag="ETF headline 3m ago")
    assert s["side"] == "NO"
    assert s["now_edge"] == -0.014
    assert s["recent_flips"] == ["YES 20c->38c/22s"]
    assert s["news"] == "ETF headline 3m ago"
    assert "entry_mid" not in s  # deprecated YES-oriented fields stay out


def test_render_prompt_is_compact():
    prompt = ec.render_prompt(ec.build_snapshot(_exit_dict()))
    assert len(prompt) < 800
    assert "ETH price up" in prompt
    assert "edge_gone" in prompt


# --- parsing --------------------------------------------------------------

def test_parse_decision_exit():
    d, r = ec.parse_decision("DECISION: EXIT\nREASON: edge flipped fast, get out")
    assert d == "EXIT" and r == "edge flipped fast, get out"


def test_parse_decision_hold_case_insensitive():
    d, _ = ec.parse_decision("decision: hold\nreason: noise, edge may return")
    assert d == "HOLD"


def test_parse_decision_rejects_garbage():
    assert ec.parse_decision("probably exit maybe") == (None, None)
    assert ec.parse_decision("") == (None, None)
    assert ec.parse_decision(None) == (None, None)
    assert ec.parse_decision("DECISION: MAYBE\nREASON: unsure") == (None, None)


def test_parse_decision_truncates_long_reason():
    d, r = ec.parse_decision("DECISION: EXIT\nREASON: " + "x" * 500)
    assert d == "EXIT" and len(r) <= ec.MAX_REASON_CHARS


# --- gating ---------------------------------------------------------------

def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv(ec.ENABLED_ENV, raising=False)
    assert ec.copilot_active() is False
    # and the review path returns None without touching the network
    assert ec.maybe_copilot_review(_exit_dict()) is None


def test_kill_switch_disables(monkeypatch, tmp_path):
    monkeypatch.setenv(ec.ENABLED_ENV, "1")
    monkeypatch.setattr(ec, "KILL_SWITCH_PATH", str(tmp_path / "kill"))
    assert ec.copilot_active() is True
    open(ec.KILL_SWITCH_PATH, "w").write("stop")
    assert ec.copilot_active() is False
    assert ec.maybe_copilot_review(_exit_dict()) is None


# --- mock backend path ----------------------------------------------------

def test_mock_backend_review_returns_decision(monkeypatch, tmp_path):
    monkeypatch.setattr(ec, "LOG_PATH", str(tmp_path / "log.jsonl"))
    be = ec.MockBackend(decision="EXIT", reason="test reason", latency_ms=7)
    out = ec.maybe_copilot_review(_exit_dict(), backend=be)
    assert out["decision"] == "EXIT"
    assert out["reason"] == "test reason"
    assert out["latency_ms"] == 7
    assert len(be.calls) == 1  # exactly one model call
    # logged
    rows = open(ec.LOG_PATH).read().strip().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["decision"] == "EXIT"


def test_backend_exception_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(ec, "LOG_PATH", str(tmp_path / "log.jsonl"))

    class Boom:
        def complete(self, messages, timeout_s=4.0):
            raise RuntimeError("boom")

    assert ec.maybe_copilot_review(_exit_dict(), backend=Boom()) is None


def test_unparseable_output_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(ec, "LOG_PATH", str(tmp_path / "log.jsonl"))

    class Gibberish:
        def complete(self, messages, timeout_s=4.0):
            return "hmm, tough call", 12

    assert ec.maybe_copilot_review(_exit_dict(), backend=Gibberish()) is None


def test_copilot_backend_builds_without_raw_key():
    # The key lives in the Secure Vault (custom.gemini / custom.groq); the
    # backend must not require one passed in. Default provider is gemini:
    # Groq is Cloudflare-blocked from this VM's egress (2026-10-02).
    be = ec.CopilotBackend()
    assert be.cfg["credential"] == "custom.gemini"
    assert be.model == "gemini-2.5-flash"
    be_groq = ec.CopilotBackend(provider="groq")
    assert be_groq.cfg["credential"] == "custom.groq"
    # backwards-compatible alias
    assert ec.GroqBackend is ec.CopilotBackend


def test_fallback_chain_on_primary_failure(monkeypatch, tmp_path):
    # Primary (gemini) 429s -> openrouter 429s -> pollinations serves it.
    monkeypatch.setattr(ec, "PROVIDER_HEALTH_PATH",
                        str(tmp_path / "health.json"))
    be = ec.CopilotBackend()
    calls = []

    def fake_complete_with(cfg, messages, timeout_s):
        calls.append(cfg["url"])
        if "pollinations" in cfg["url"]:
            return ("FALLBACK_OK", 900)
        raise RuntimeError("copilot call failed: HTTPError")

    monkeypatch.setattr(be, "_complete_with", fake_complete_with)
    out, lat = be.complete([{"role": "user", "content": "hi"}], timeout_s=10)
    assert out == "FALLBACK_OK"
    assert len(calls) == 3
    assert "generativelanguage" in calls[0]
    assert "openrouter" in calls[1]
    assert "pollinations" in calls[2]


def test_all_providers_down_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(ec, "PROVIDER_HEALTH_PATH",
                        str(tmp_path / "health.json"))
    be = ec.CopilotBackend()

    def always_fail(cfg, messages, timeout_s):
        raise RuntimeError("copilot call failed: HTTPError")

    be._complete_with = always_fail
    try:
        be.complete([{"role": "user", "content": "hi"}], timeout_s=5)
        assert False, "should have raised"
    except RuntimeError as e:
        assert "copilot call failed" in str(e)


def _fail_with_code(code):
    def fake(cfg, messages, timeout_s):
        err = RuntimeError("copilot call failed: HTTPError")
        err._http_code = code
        raise err
    return fake


def test_429_provider_skipped_until_cooldown(monkeypatch, tmp_path):
    # A 429'd provider is skipped on the next call (1h cooldown), so the
    # chain goes straight to the healthy fallback.
    monkeypatch.setattr(ec, "PROVIDER_HEALTH_PATH",
                        str(tmp_path / "health.json"))
    be = ec.CopilotBackend()
    calls = []

    def fake(cfg, messages, timeout_s):
        calls.append(cfg["url"])
        if "pollinations" in cfg["url"]:
            return ("OK", 100)
        raise _fail_with_code(429)(cfg, messages, timeout_s)

    monkeypatch.setattr(be, "_complete_with", fake)
    out, _ = be.complete([{"role": "user", "content": "hi"}], timeout_s=10)
    assert out == "OK"
    assert len(calls) == 3  # first call tries all three, marks 2 down
    calls.clear()
    out, _ = be.complete([{"role": "user", "content": "hi"}], timeout_s=10)
    assert out == "OK"
    assert len(calls) == 1 and "pollinations" in calls[0]


def test_transient_failure_retried_once(monkeypatch, tmp_path):
    # A flaky (non-429) fallback gets one retry within the call; a 429
    # never does.
    monkeypatch.setattr(ec, "PROVIDER_HEALTH_PATH",
                        str(tmp_path / "health.json"))
    be = ec.CopilotBackend()
    calls = []
    attempts = {"n": 0}

    def fake(cfg, messages, timeout_s):
        calls.append(cfg["url"])
        if "pollinations" in cfg["url"]:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _fail_with_code(None)(cfg, messages, timeout_s)
            return ("RETRY_OK", 100)
        raise _fail_with_code(429)(cfg, messages, timeout_s)

    monkeypatch.setattr(be, "_complete_with", fake)
    out, _ = be.complete([{"role": "user", "content": "hi"}], timeout_s=10)
    assert out == "RETRY_OK"
    # gemini 429, openrouter 429, pollinations flake, pollinations retry OK
    assert len(calls) == 4
    assert calls[2] == calls[3] and "pollinations" in calls[3]
    # 429s are cooled down; the transient single failure is not.
    health = json.loads((tmp_path / "health.json").read_text())
    assert "gemini" in health and "openrouter" in health
    assert "pollinations" not in health


def test_provider_retried_after_cooldown_expires(monkeypatch, tmp_path):
    monkeypatch.setattr(ec, "PROVIDER_HEALTH_PATH",
                        str(tmp_path / "health.json"))
    # Seed an expired 429 cooldown for gemini.
    (tmp_path / "health.json").write_text(json.dumps({
        "gemini": {"unhealthy_until": time.time() - 1,
                   "reason": "http_429"}}))
    be = ec.CopilotBackend()
    calls = []
    monkeypatch.setattr(be, "_complete_with",
                        lambda cfg, m, timeout_s:
                        (calls.append(cfg["url"]), ("OK", 50))[1])
    out, _ = be.complete([{"role": "user", "content": "hi"}], timeout_s=10)
    assert out == "OK"
    assert "generativelanguage" in calls[0]  # primary tried again


def test_all_marked_down_falls_back_to_full_chain(monkeypatch, tmp_path):
    # Everything marked down -> best effort tries the full chain anyway.
    monkeypatch.setattr(ec, "PROVIDER_HEALTH_PATH",
                        str(tmp_path / "health.json"))
    future = time.time() + 9999
    (tmp_path / "health.json").write_text(json.dumps({
        p: {"unhealthy_until": future, "reason": "http_429"}
        for p in ("gemini", "openrouter", "pollinations")}))
    be = ec.CopilotBackend()
    calls = []
    monkeypatch.setattr(be, "_complete_with",
                        lambda cfg, m, timeout_s:
                        (calls.append(cfg["url"]), ("OK", 50))[1])
    out, _ = be.complete([{"role": "user", "content": "hi"}], timeout_s=10)
    assert out == "OK"
    assert len(calls) == 1  # primary first, best effort


def test_pollinations_body_has_no_temperature():
    # Pollinations' endpoint 500s on the temperature param (2026-10-02).
    be = ec.CopilotBackend(provider="pollinations")
    assert be.cfg["credential"] is None
    assert be.cfg["model"] == "openai"
