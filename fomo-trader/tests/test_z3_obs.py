"""Hermetic tests for Z3 observation mode: research tagging, kill-criteria
dashboard, field-usage audit. No network; all files under tmp_path."""

import base64
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "analysis" / "obs"))

import fomo_trader  # noqa: E402
import obs_dashboard  # noqa: E402
from fomo_trader import (  # noqa: E402
    RESEARCH_OFF,
    Trader,
    maybe_research_dashboard,
    maybe_tag_research,
    registered_question_ids,
    research_settings,
)
import field_usage_audit  # noqa: E402

T = 1790500000.0
TS = "2026-09-27 12:00:00"
ABSENT = object()

# Byte-for-byte what the code journals for these fixtures (taken from real
# runs of tests/test_entry_enrichment.py, clock pinned to T). Includes the
# 2026-09-28 instrumentation fields (strategy_version, pool_created_at).
GOLDEN_ENTRY = {
    "solana": '{"ts": "2026-09-27 12:00:00", "ts_epoch": 1790500000.0, "type": "entry", "mint": "MINT", '
              '"name": "TEST", "entry": 6e-08, "buy_sol": 0.06, '
              '"buy_sig": "dryrun-1790500000", "sol_usd": 100.0, '
              '"signal_gain_pct": 50, "signal_ratio": 2, "liquidity_usd": 20000, '
              '"source": "geckoterminal", "window": "15m", '
              '"signal_price_usd": 1e-05, "commit_price_native": 6e-08, '
              '"commit_price_usd": 5.999999999999999e-06, '
              '"slip_from_signal_pct": -0.40000000000000013, '
              '"holder_top1_pct": 10.0, "holder_top5_pct": 15.0, '
              '"lp_burn_pct": null, "lp_locked": null, "entry_latency_ms": 1000, '
              '"m15_buys": 20, "m15_sells": 10, "m15_volume_usd": 5000, '
              '"mcap_usd": 100000, "strategy_version": "1", "pool_created_at": null}',
    "bsc": '{"ts": "2026-09-27 12:00:00", "ts_epoch": 1790500000.0, "type": "entry", "mint": "0xMINT", '
           '"name": "TEST", "chain": "bsc", "entry": 9e-09, "buy_sol": 0.009, '
           '"buy_sig": "pool", "sol_usd": 800.0, "signal_gain_pct": 50, '
           '"signal_ratio": 2, "liquidity_usd": 20000, "source": "geckoterminal", '
           '"window": "15m", "signal_price_usd": 1e-05, '
           '"commit_price_native": 9e-09, "commit_price_usd": 7.2e-06, '
           '"slip_from_signal_pct": -0.28000000000000014, '
           '"holder_top1_pct": null, "holder_top5_pct": null, '
           '"lp_burn_pct": null, "lp_locked": null, "entry_latency_ms": 1000, '
           '"m15_buys": 20, "m15_sells": 10, "m15_volume_usd": 5000, '
           '"mcap_usd": 100000, "strategy_version": "1", "pool_created_at": null}',
}
GOLDEN_CLOSE = {
    "solana": '{"ts": "2026-09-27 12:00:00", "ts_epoch": 1790500000.0, "type": "close", "mint": "MINT", '
              '"name": "TEST", "chain": "solana", "entry": 1e-09, "peak": 2e-09, '
              '"exit": 1.5e-09, "buy_sol": 0.06, "rungs": [[0, 50.0]], '
              '"reason": "trailing stop -25.0% from peak", "realized_sol": 0.03, '
              '"realized_bnb": null, "realized_usd": 3.0, "bankroll_usd": 3.0, "drawdown_pct": 0.0, "sol_usd": 100.0}',
    "bsc": '{"ts": "2026-09-27 12:00:00", "ts_epoch": 1790500000.0, "type": "close", "mint": "0xMINT", '
           '"name": "TEST", "chain": "bsc", "entry": 9e-09, "peak": 9e-09, '
           '"exit": 8e-09, "buy_sol": 0.009, "rungs": [], "reason": "test close", '
           '"realized_sol": null, "realized_bnb": 0.001, "realized_usd": 0.8, '
           '"bankroll_usd": 0.8, "drawdown_pct": 0.0, "sol_usd": 800.0}',
}
ENRICH_KEYS = ("signal_price_usd", "commit_price_native", "commit_price_usd",
               "slip_from_signal_pct", "holder_top1_pct", "holder_top5_pct",
               "lp_burn_pct", "lp_locked", "liquidity_usd", "entry_latency_ms",
               "m15_buys", "m15_sells", "m15_volume_usd", "mcap_usd")

OFF_CONFIGS = [
    ("absent", ABSENT),
    ("null", None),
    ("shipped_defaults", {"enabled": False, "tag_trades": False,
                          "question_id": None, "dashboard": False,
                          "field_audit": False}),
    ("master_off", {"enabled": False, "tag_trades": True,
                    "question_id": "Q1", "dashboard": True}),
    ("enabled_only", {"enabled": True, "tag_trades": False,
                      "dashboard": False}),
    ("empty_object", {}),
    ("string", "yes"),
    ("list", ["enabled"]),
    ("int", 1),
    ("enabled_str", {"enabled": "true", "tag_trades": True, "dashboard": True}),
    ("enabled_int", {"enabled": 1, "tag_trades": 1, "dashboard": 1}),
    ("tag_str", {"enabled": True, "tag_trades": "yes", "dashboard": "yes"}),
    ("tag_null", {"enabled": True, "tag_trades": None, "dashboard": None,
                  "question_id": 7}),
]


class _FrozenTime:
    """Stand-in for fomo_trader's `time` module with a pinned clock."""

    def __getattr__(self, name):
        return getattr(time, name)

    @staticmethod
    def time():
        return T

    @staticmethod
    def strftime(fmt, *args):
        return TS


@pytest.fixture(autouse=True)
def _reset_research_state():
    fomo_trader._research_warned.clear()
    fomo_trader._research_registry.update(key=None, ids=frozenset())
    yield
    fomo_trader._research_warned.clear()
    fomo_trader._research_registry.update(key=None, ids=frozenset())


def account_call(method, args, **kwargs):
    if method == "getAccountInfo":
        return {"result": {"value": {"data": [base64.b64encode(bytes(82)).decode(), "base64"]}}}
    if method == "getTokenSupply":
        return {"result": {"value": {"amount": "1000"}}}
    return {"result": {"value": [{"amount": "100"}, {"amount": "50"}]}}


def entry_trader(tmp_path, chain, research=ABSENT):
    t = Trader.__new__(Trader)
    t.cfg = {"hunter": {"entry": {}, "rug_guard": {}},
             "risk": {"max_open_positions": 3,
                      "buy_sol_per_trade": .06, "buy_bnb_per_trade": .009},
             "exit": {"slippage_bps": 500}}
    if research is not ABSENT:
        t.cfg["research"] = research
    t.dry_run = True
    t.state_path = str(tmp_path / "state.json")
    t.state = {"positions": {}, "cooldown": {}, "trades_today": [],
               "trades_this_hour": []}
    t.lock = threading.RLock()
    t.pending_entries = set()
    t.manage = lambda mint: None
    t._entry_commit_ok = lambda signal: True
    if chain == "solana":
        t.decimals = lambda mint: 6
        t.sol_usd = lambda: 100.0
        t.rpc = SimpleNamespace(call=account_call)
    else:
        t.bnb_usd = lambda: 800.0
        t.bscswap = lambda: SimpleNamespace(
            honeypot_check=lambda *args: (True, "ok"),
            token_decimals=lambda mint: 18,
            quote_buy=lambda *args: 1_000_000 * 10**18)
    return t


def signal(chain):
    return {"chain": chain, "mint": "MINT" if chain == "solana" else "0xMINT",
            "name": "TEST", "m15_gain_pct": 50, "buy_sell_ratio": 2,
            "liquidity_usd": 20000, "window_label": "15m", "pool_url": "pool",
            "source": "geckoterminal", "ts": T - 1,
            "signal_price_usd": .00001, "m15_buys": 20, "m15_sells": 10,
            "m15_volume_usd": 5000, "mcap_usd": 100000,
            "lp_burn_pct": None, "lp_locked": None}


def run_entry(tmp_path, chain, research=ABSENT):
    tmp_path.mkdir(parents=True, exist_ok=True)
    t = entry_trader(tmp_path, chain, research)
    with patch("fomo_trader.time", _FrozenTime()), \
         patch("fomo_trader.jup_quote",
               return_value={"outAmount": str(1_000_000 * 10**6)}), \
         patch.object(t, "honeypot_check", return_value=True):
        t.enter(signal(chain))
    lines = (tmp_path / "trades.jsonl").read_text().splitlines()
    assert len(lines) == 1
    mint = "MINT" if chain == "solana" else "0xMINT"
    return lines[0], t.state["positions"][mint]


def golden_position(chain):
    rec = json.loads(GOLDEN_ENTRY[chain])
    pos = {"name": "TEST"}
    if chain == "bsc":
        pos["chain"] = "bsc"
    pos.update({"entry": rec["entry"], "peak": rec["entry"],
                "buy_sol": rec["buy_sol"], "buy_sig": rec["buy_sig"],
                "rungs_fired": [], "opened_at": T, "last_price_ts": T,
                "tokens_raw": 10**12 if chain == "solana" else 999999999999999983222784,
                "sold_sol": 0.0, "decimals": 6 if chain == "solana" else 18})
    pos.update({k: rec[k] for k in ENRICH_KEYS})
    return pos


def close_position(chain):
    if chain == "solana":
        return {"name": "TEST", "entry": 1e-09, "peak": 2e-09, "buy_sol": 0.06,
                "rungs_fired": [[0, 50.0, 0.045]], "tokens_raw": 0,
                "sold_sol": 0.09, "decimals": 6}
    return {"name": "TEST", "chain": "bsc", "entry": 9e-09, "peak": 9e-09,
            "buy_sol": 0.009, "rungs_fired": [], "tokens_raw": 0,
            "sold_sol": 0.01, "decimals": 18}


def run_close(tmp_path, chain, research=ABSENT, pos=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    t = Trader.__new__(Trader)
    t.cfg = {"exit": {"take_profits": [[50, 50]]}, "risk": {}}
    if research is not ABSENT:
        t.cfg["research"] = research
    t.state_path = str(tmp_path / "state.json")
    mint = "MINT" if chain == "solana" else "0xMINT"
    pos = close_position(chain) if pos is None else pos
    t.state = {"positions": {mint: pos}, "realized_sol": 0.0,
               "realized_bnb": 0.0, "realized_usd": 0.0}
    t.lock = threading.RLock()
    t.save = lambda: None
    t._px_hist = {}
    t.sol_usd = lambda: 100.0
    t.bnb_usd = lambda: 800.0
    exit_px, reason = ((1.5e-09, "trailing stop -25.0% from peak")
                       if chain == "solana" else (8e-09, "test close"))
    with patch("fomo_trader.time", _FrozenTime()):
        realized = t.close_trade(mint, pos, exit_px, reason)
    lines = (tmp_path / "trades.jsonl").read_text().splitlines()
    assert len(lines) == 1
    return lines[0], realized


def write_registry(path, questions):
    path.write_text(json.dumps({"questions": questions}))
    return str(path)


# -- behavior-neutral proof (the most important test in this file) --------

@pytest.mark.parametrize("chain", ["solana", "bsc"])
@pytest.mark.parametrize("name,research", OFF_CONFIGS,
                         ids=[n for n, _ in OFF_CONFIGS])
def test_flags_off_records_are_byte_identical_to_pre_change(
        tmp_path, chain, name, research):
    with patch.object(obs_dashboard, "update") as dashboard_update:
        row, pos = run_entry(tmp_path / "entry", chain, research)
        assert row == GOLDEN_ENTRY[chain]
        assert pos == golden_position(chain)
        assert list(pos) == list(golden_position(chain))
        assert "research_question" not in pos
        close_row, realized = run_close(tmp_path / "close", chain, research)
        assert close_row == GOLDEN_CLOSE[chain]
        assert realized == json.loads(GOLDEN_CLOSE[chain])[
            "realized_sol" if chain == "solana" else "realized_bnb"]
        # a tag left on a position from an earlier "on" period is ignored
        tagged = dict(close_position(chain), research_question="Q1")
        close_row2, _ = run_close(tmp_path / "close2", chain, research, tagged)
        assert close_row2 == GOLDEN_CLOSE[chain]
    assert not dashboard_update.called


@pytest.mark.parametrize("name,research", OFF_CONFIGS,
                         ids=[n for n, _ in OFF_CONFIGS])
def test_off_helper_returns_same_object_untouched(name, research):
    cfg = {} if research is ABSENT else {"research": research}
    rec = {"a": 1, "b": None}
    before = json.dumps(rec)
    assert maybe_tag_research(rec, cfg) is rec
    assert json.dumps(rec) == before
    assert maybe_tag_research(rec, cfg, {"research_question": "Q1"}) is rec
    assert json.dumps(rec) == before
    assert research_settings(cfg)[0] is False


def test_absent_and_shipped_defaults_log_nothing(capsys):
    for cfg in ({}, {"research": dict(OFF_CONFIGS[2][1])}, {"research": None}):
        research_settings(cfg)
        maybe_tag_research({}, cfg)
        maybe_research_dashboard(cfg, "/nonexistent/state.json")
    assert "RESEARCH" not in capsys.readouterr().out


# -- config parsing: malformed values fail safe, explicitly ----------------

@pytest.mark.parametrize("cfg", [None, "cfg", 5, [], {"research": "on"},
                                 {"research": 3.5}, {"research": [1]},
                                 {"research": {"enabled": "yes"}},
                                 {"research": {"enabled": 1}},
                                 {"research": {"enabled": float("nan")}}])
def test_malformed_config_is_off_and_never_raises(cfg):
    assert research_settings(cfg) == RESEARCH_OFF


def test_malformed_values_log_once_each(capsys):
    cfg = {"research": {"enabled": "true"}}
    for _ in range(5):
        research_settings(cfg)
    out = capsys.readouterr().out
    assert out.count("RESEARCH config: enabled=") == 1
    research_settings({"research": "yes"})
    assert "not an object" in capsys.readouterr().out


def test_question_id_validation():
    on = {"enabled": True, "tag_trades": True, "dashboard": False}
    assert research_settings({"research": dict(on, question_id="  Q2 ")}) == (
        True, "Q2", False)
    for bad in (7, "", "   ", [], {"id": "Q1"}):
        assert research_settings({"research": dict(on, question_id=bad)}) == (
            True, None, False)


# -- tagging on ------------------------------------------------------------

@pytest.mark.parametrize("chain", ["solana", "bsc"])
def test_tagging_on_adds_one_key_and_propagates_to_close(tmp_path, chain,
                                                         monkeypatch):
    monkeypatch.setattr(fomo_trader, "RESEARCH_QUESTIONS_PATH", write_registry(
        tmp_path / "rq.json", [{"id": "Q1", "status": "open"}]))
    research = {"enabled": True, "tag_trades": True, "question_id": "Q1",
                "dashboard": False, "field_audit": False}
    row, pos = run_entry(tmp_path / "entry", chain, research)
    rec = json.loads(row)
    # tagging adds exactly one key; instrumentation fields may follow it
    assert rec["research_question"] == "Q1"
    del rec["research_question"]
    assert json.dumps(rec) == GOLDEN_ENTRY[chain]
    assert pos["research_question"] == "Q1"
    assert {k: v for k, v in pos.items() if k != "research_question"} == \
        golden_position(chain)
    close_pos = dict(close_position(chain), research_question=pos["research_question"])
    close_row, _ = run_close(tmp_path / "close", chain, research, close_pos)
    crec = json.loads(close_row)
    assert crec.pop("research_question") == "Q1"
    assert json.dumps(crec) == GOLDEN_CLOSE[chain]


@pytest.mark.parametrize("qid,registry,expect_log", [
    (None, [{"id": "Q1", "status": "open"}], "no question_id is set"),
    ("Q404", [{"id": "Q1", "status": "open"}], "not an open question"),
    ("Q9", [{"id": "Q9", "status": "closed"}], "not an open question"),
    ("Q1", "MISSING", "unreadable"),
    ("Q1", "CORRUPT", "corrupt"),
    ("Q1", {"questions": 5}, "corrupt"),
    ("Q1", [None, 3, {"id": 1}, {"status": "open"}], "not an open question"),
])
def test_unregistered_fallbacks_are_explicit(tmp_path, monkeypatch, capsys,
                                             qid, registry, expect_log):
    path = tmp_path / "rq.json"
    if registry == "CORRUPT":
        path.write_text("{not json")
    elif isinstance(registry, dict):
        path.write_text(json.dumps(registry))
    elif registry != "MISSING":
        write_registry(path, registry)
    monkeypatch.setattr(fomo_trader, "RESEARCH_QUESTIONS_PATH", str(path))
    cfg = {"research": {"enabled": True, "tag_trades": True,
                        "question_id": qid}}
    for _ in range(3):
        rec = maybe_tag_research({"type": "entry"}, cfg)
        assert rec["research_question"] == "unregistered"
    out = capsys.readouterr().out
    assert expect_log in out
    assert out.count("RESEARCH") <= 2  # once per cause, not per trade


def test_close_without_entry_tag_is_unregistered():
    cfg = {"research": {"enabled": True, "tag_trades": True, "question_id": "Q1"}}
    for pos in ({}, {"research_question": None}, {"research_question": 5}):
        assert maybe_tag_research({}, cfg, pos)["research_question"] == "unregistered"


def test_registry_reloads_on_change_and_real_registry_is_valid(tmp_path):
    path = tmp_path / "rq.json"
    write_registry(path, [{"id": "Q1", "status": "open"}])
    assert registered_question_ids(str(path)) == {"Q1"}
    write_registry(path, [{"id": "Q1", "status": "open"},
                          {"id": "Q2", "status": "open"}])
    os.utime(path, ns=(1, 2))
    assert registered_question_ids(str(path)) == {"Q1", "Q2"}
    data = json.loads((ROOT / "research_questions.json").read_text())
    ids = [q["id"] for q in data["questions"]]
    assert ids == ["Q%d" % i for i in range(1, 9)]
    for q in data["questions"]:
        assert set(q) == {"id", "question", "registered", "status"}
        assert q["registered"] == "2026-09-27" and q["status"] == "open"
        assert q["question"].strip()


def test_tag_helper_never_raises_on_hostile_inputs():
    on = {"research": {"enabled": True, "tag_trades": True, "question_id": "Q1"}}
    for rec in (None, 5, "x", [], ()):
        assert maybe_tag_research(rec, on) is rec
    for pos in (None, 5, "x", []):
        out = maybe_tag_research({}, on, pos)
        assert out["research_question"] in ("Q1", "unregistered")


def _pct(samples, q):
    s = sorted(samples)
    return s[min(len(s) - 1, int(q * len(s)))] / 1e6


def test_tag_helper_latency_budget():
    base = json.loads(GOLDEN_ENTRY["solana"])
    enrichment = {k: base[k] for k in ENRICH_KEYS}
    cfgs = {"absent": {}, "off": {"research": dict(OFF_CONFIGS[2][1])},
            "on": {"research": {"enabled": True, "tag_trades": True,
                                "question_id": "Q1"}}}
    report = {}
    for label, cfg in cfgs.items():
        samples = []
        for _ in range(10_000):
            rec = dict(enrichment)
            t0 = time.perf_counter_ns()
            maybe_tag_research(rec, cfg)
            samples.append(time.perf_counter_ns() - t0)
        report[label] = (_pct(samples, .5), _pct(samples, .99))
        assert report[label][1] < 50.0
    print("maybe_tag_research p50/p99 ms over 10,000 calls: " + ", ".join(
        "%s %.4f/%.4f" % (k, *v) for k, v in report.items()))


# -- dashboard hook on the close path --------------------------------------

def test_dashboard_on_writes_after_close(tmp_path, monkeypatch):
    out = tmp_path / "obs" / "kill_dashboard.json"
    monkeypatch.setattr(obs_dashboard, "DEFAULT_OUT", str(out))
    research = {"enabled": True, "tag_trades": False, "dashboard": True}
    _, realized = run_close(tmp_path / "c", "solana", research)
    assert realized == 0.03
    dash = json.loads(out.read_text())
    assert [c["id"] for c in dash["criteria"]] == list(range(1, 9))
    assert dash["journal"] == str(tmp_path / "c" / "trades.jsonl")
    assert dash["journal_stats"]["closes_priced"] == 1


def test_dashboard_failure_never_breaks_close(tmp_path, capsys):
    research = {"enabled": True, "dashboard": True}
    with patch.object(obs_dashboard, "update", side_effect=RuntimeError("boom")):
        row, realized = run_close(tmp_path / "c", "solana", research)
    assert row == GOLDEN_CLOSE["solana"] and realized == 0.03
    assert "RESEARCH dashboard update failed" in capsys.readouterr().out
    with patch.object(obs_dashboard, "update", return_value={"error": "x"}):
        assert maybe_research_dashboard({"research": research},
                                        str(tmp_path / "s.json")) == {"error": "x"}


# -- dashboard criteria -----------------------------------------------------

def _close(i, net, peak=1.0, exit_=0.9, **kw):
    d = {"ts": "2026-09-27 12:00:00", "ts_epoch": 1790500000.0, "type": "close", "mint": "M%d" % i,
         "entry": 1.0, "peak": peak, "exit": exit_, "buy_sol": 0.06,
         "realized_usd": net}
    d.update(kw)
    return d


def _entry(i, slip, **kw):
    d = {"ts": "2026-09-27 11:59:00", "type": "entry", "mint": "M%d" % i,
         "entry": 1.0, "buy_sol": 0.06, "slip_from_signal_pct": slip,
         "entry_latency_ms": 1500}
    d.update(kw)
    return d


def _crit(dash, cid):
    return next(c for c in dash["criteria"] if c["id"] == cid)


def _write(path, rows):
    path.write_text("".join(
        (r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows))
    return str(path)


def test_empty_and_missing_journal(tmp_path):
    for jp in (_write(tmp_path / "empty.jsonl", []), str(tmp_path / "nope.jsonl")):
        dash = obs_dashboard.update(jp, str(tmp_path / "out.json"),
                                    generated_at="X")
        assert "error" not in dash
        assert all(c["status"] == "ok" for c in dash["criteria"])
        assert dash["summary"] == {"tripped": [], "not_evaluable": list(range(1, 9)),
                                   "program_killed": False}
    assert dash["journal_stats"]["missing"] is True


def test_corrupt_nan_none_duplicates_and_repeat_mints(tmp_path):
    rows = [_close(1, -1.0), _close(1, -1.0), "{bad json", "[1, 2]",
            '{"type": "close", "realized_usd": NaN, "mint": "N"}',
            _close(2, None), _close(3, None, realized_sol=0.01, sol_usd=None),
            _close(4, None, chain="bsc", realized_bnb=0.01, sol_usd=None),
            _entry(5, float("nan")), _entry(6, None),
            _close(7, -2.0, ts="2026-09-27 13:00:00"),
            _close(7, 1.0, ts="2026-09-27 14:00:00")]
    dash = obs_dashboard.update(_write(tmp_path / "j.jsonl", rows),
                                str(tmp_path / "o.json"), generated_at="X")
    st = dash["journal_stats"]
    assert st["duplicates_dropped"] == 1 and st["corrupt_lines"] == 1
    assert st["non_object_lines"] == 1
    assert st["closes_priced"] == 4          # M1, M3 (fallback), M7 twice
    assert st["closes_priced_with_sol_fallback"] == 1
    assert st["closes_unpriced"] == 3        # NaN, None, BSC without rate
    json.loads((tmp_path / "o.json").read_text())  # strict JSON on disk


def test_net_and_wr_pf_trip_at_100_losing_trades(tmp_path):
    rows = [_close(i, -1.0) for i in range(99)]
    dash = obs_dashboard.compute(rows)
    assert not _crit(dash, 1)["evaluable"] and _crit(dash, 1)["status"] == "ok"
    rows.append(_close(99, -1.0))
    dash = obs_dashboard.compute(rows)
    assert _crit(dash, 1)["status"] == "TRIPPED"
    assert _crit(dash, 4)["status"] == "TRIPPED"
    assert dash["summary"]["program_killed"] is True


def test_outlier_dependence(tmp_path):
    rows = [_close(i, -1.0) for i in range(20)] + [_close(99, 50.0)]
    c = _crit(obs_dashboard.compute(rows), 5)
    assert c["status"] == "TRIPPED" and c["value"]["ALL"]["winners_to_erase_net"] == 1
    rows = [_close(i, 2.0) for i in range(20)] + [_close(99, -1.0)]
    assert _crit(obs_dashboard.compute(rows), 5)["status"] == "ok"
    tagged = [_close(i, -1.0, research_question="Q1") for i in range(5)] + [
        _close(9, 30.0, research_question="Q1")]
    c = _crit(obs_dashboard.compute(tagged), 5)
    assert c["value"]["Q1"]["winners_to_erase_net"] == 1


def test_rug_gap_share(tmp_path):
    rows = ([_close(i, -5.0, peak=1.0, exit_=0.1) for i in range(6)]
            + [_close(10 + i, -1.0) for i in range(6)])
    c = _crit(obs_dashboard.compute(rows), 6)
    assert c["evaluable"] and c["status"] == "TRIPPED"
    assert c["value"]["share"] == pytest.approx(30 / 36, abs=1e-4)


def test_cost_criteria_and_bsc_leg():
    sol = [_entry(i, 0.20) for i in range(20)]
    dash = obs_dashboard.compute(sol)
    assert _crit(dash, 2)["status"] == "TRIPPED"
    assert not _crit(dash, 7)["evaluable"]
    bsc = [_entry(i, 0.25, chain="bsc") for i in range(20)]
    assert _crit(obs_dashboard.compute(bsc), 7)["status"] == "TRIPPED"
    assert _crit(obs_dashboard.compute([_entry(i, 0.05) for i in range(30)]),
                 2)["status"] == "ok"
    assert _crit(obs_dashboard.compute(sol), 3)["evaluable"] is False


def test_sixty_day_sign_test():
    early = [_close(i, -1.0, ts="2026-07-01 00:00:00") for i in range(6)]
    late = [_close(10 + i, -1.0, ts="2026-09-01 00:00:00") for i in range(4)]
    c = _crit(obs_dashboard.compute(early + late), 8)
    assert c["evaluable"] and c["status"] == "TRIPPED"
    good = ([_close(i, 1.0, ts="2026-07-01 00:00:00") for i in range(6)]
            + [_close(10 + i, 1.0, ts="2026-09-01 00:00:00") for i in range(4)])
    assert _crit(obs_dashboard.compute(good), 8)["status"] == "ok"


def test_update_never_raises_on_unwritable_output(tmp_path):
    jp = _write(tmp_path / "j.jsonl", [_close(1, 1.0)])
    res = obs_dashboard.update(jp, str(tmp_path))  # a directory
    assert "error" in res
    assert obs_dashboard.compute(None)["summary"]["program_killed"] is False
    assert obs_dashboard.compute([None, 5, "x"])["journal_stats"]["records"] == 0


def test_dashboard_update_latency(tmp_path):
    rows = []
    for i in range(300):
        rows += [_entry(i, 0.05), _close(i, 1.0 if i % 4 == 0 else -0.9)]
    jp = _write(tmp_path / "j.jsonl", rows)
    out = str(tmp_path / "o.json")
    samples = []
    for _ in range(30):
        t0 = time.perf_counter_ns()
        obs_dashboard.update(jp, out)
        samples.append(time.perf_counter_ns() - t0)
    p50, p99 = _pct(samples, .5), _pct(samples, .99)
    assert p99 < 50.0
    print("obs_dashboard.update on 600-line journal: p50 %.2f ms, p99 %.2f ms"
          % (p50, p99))


# -- field-usage audit ------------------------------------------------------

def test_field_usage_audit_flags_and_never_deletes(tmp_path):
    journal = _write(tmp_path / "j.jsonl", [
        {"ts": "2026-09-24 10:00:00", "type": "entry", "mint": "A",
         "entry": 1.0, "old_field": 3, "never": None},
        {"ts": "2026-10-19 10:00:00", "type": "close", "mint": "A",
         "entry": 1.0, "realized_usd": 1.5, "old_field": None,
         "nan_field": float("nan")},
        "{corrupt",
    ])
    adir = tmp_path / "analysis"
    (adir / "x").mkdir(parents=True)
    (adir / "obs").mkdir()
    (adir / "x" / "a.py").write_text('t.get("entry"); r["realized_usd"]\n'
                                     "'old_field'\n")
    (adir / "obs" / "self.py").write_text('"mint" "type" "ts" "never"\n')
    before = {p: p.read_bytes() for p in (Path(journal), adir / "x" / "a.py")}
    now = field_usage_audit._parse_ts("2026-10-20 10:00:00")
    res = field_usage_audit.audit(journal, str(adir), now)
    f = res["fields"]
    assert f["entry"]["days_since_used"] == 1.0 and not f["entry"]["sunset_review"]
    assert f["entry"]["analysis_refs"] == ["analysis/x/a.py"]
    assert f["old_field"]["days_since_used"] == 26.0 and f["old_field"]["sunset_review"]
    assert f["never"]["days_since_used"] is None and f["never"]["sunset_review"]
    assert f["mint"]["analysis_refs"] == []  # analysis/obs is excluded
    assert f["mint"]["sunset_review"]
    assert f["realized_usd"]["days_tracked"] == 1.0
    assert not f["realized_usd"]["sunset_review"]
    assert f["nan_field"]["non_null_records"] == 0
    assert res["journal_stats"]["corrupt_lines"] == 1
    assert "old_field" in res["sunset_candidates"]
    assert all(p.read_bytes() == b for p, b in before.items())
    out = tmp_path / "audit.json"
    assert field_usage_audit.main(["--journal", journal, "--analysis-dir",
                                   str(adir), "--out", str(out),
                                   "--now", "2026-10-20 10:00:00"]) == 0
    assert json.loads(out.read_text())["fields"]["entry"]["days_since_used"] == 1.0


def test_field_usage_audit_missing_inputs(tmp_path):
    res = field_usage_audit.audit(str(tmp_path / "none.jsonl"),
                                  str(tmp_path / "nodir"))
    assert res["fields"] == {} and res["journal_stats"]["missing"] is True
    assert field_usage_audit.main(["--now", "garbage", "--out",
                                   str(tmp_path / "o.json")]) == 2
