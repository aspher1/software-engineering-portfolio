"""Allocator (allocator.py), its entry-commit wiring, and the offline analyst."""
import base64
import json
import math
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "analysis"))

import allocator  # noqa: E402
import allocator_analyst as analyst  # noqa: E402
from fomo_trader import Trader  # noqa: E402

TYPICAL = {"signal_gain_pct": 120, "liquidity_usd": 30000,
           "buy_count_15m": 50, "sell_count_15m": 20, "buy_sell_ratio": 2.5,
           "volume_15m_usd": 20000, "mcap_usd": 400000,
           "holder_top1_pct": 18, "holder_top5_pct": 40,
           "signal_to_fill_slippage_pct": 1.0, "entry_latency_sec": 1.5,
           "chain": "solana"}


def _logit(p):
    return math.log(p / (1 - p))


# -- pure allocator ----------------------------------------------------------

@pytest.mark.parametrize("feats", [
    {k: 1e18 for k in TYPICAL if k != "chain"},
    {k: -1e18 for k in TYPICAL if k != "chain"},
    {k: 0 for k in TYPICAL if k != "chain"},
    {k: float("nan") for k in TYPICAL},
    {k: float("inf") for k in TYPICAL},
    {k: "junk" for k in TYPICAL},
    {"holder_top1_pct": 100, "holder_top5_pct": 100,
     "signal_to_fill_slippage_pct": 500, "entry_latency_sec": 1e6,
     "chain": "bsc"},
])
@pytest.mark.parametrize("scale", [1.0, 100.0, -100.0])
def test_multiplier_bounds_on_extremes(feats, scale):
    cfg = {"weights": {k: v * scale for k, v in allocator.DEFAULT_WEIGHTS.items()}}
    take, score, mult = allocator.score_signal(feats, cfg)
    assert isinstance(take, bool)
    assert 0.0 <= score <= 1.0
    assert allocator.MULT_MIN <= mult <= allocator.MULT_MAX


def test_multiplier_map_is_monotone_and_anchored():
    assert (allocator.MULT_MIN, allocator.MULT_MAX) == (0.5, 3.0)
    for score, expected in [(0.0, 0.5), (0.5, 1.0), (0.6, 1.75),
                            (0.7, 2.5), (0.71, 2.5 + 0.5 / 30),
                            (1.0, 3.0), (-3, 0.5), (7, 3.0)]:
        assert allocator.multiplier_from_score(score) == pytest.approx(expected)
    grid = [allocator.multiplier_from_score(i / 1000) for i in range(1001)]
    assert all(b > a for a, b in zip(grid, grid[1:]))


def test_determinism():
    cfg = {"take_threshold": 0.35, "weights": {"liquidity_usd": 0.7}}
    first = allocator.score_signal(dict(TYPICAL), cfg)
    second = allocator.score_signal(dict(TYPICAL), cfg)
    assert first == second
    assert allocator.normalize(TYPICAL) == allocator.normalize(dict(TYPICAL))


def test_allocator_module_has_no_randomness_or_io():
    import ast
    with open(os.path.join(ROOT, "allocator.py")) as f:
        tree = ast.parse(f.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in ("open", "exec", "eval", "__import__")
    assert imported == {"math"}


def test_missing_features_are_neutral():
    assert allocator.normalize({}) == {f: 0.0 for f in allocator.FEATURES}
    assert allocator.score_signal({}, {}) == (True, 0.5, 1.0)
    nulls = {k: None for k in TYPICAL}
    assert allocator.score_signal(nulls, None) == (True, 0.5, 1.0)
    # BSC: holder concentration null, must not move the score
    bsc = dict(TYPICAL, chain="bsc", holder_top1_pct=None, holder_top5_pct=None)
    n = allocator.normalize(bsc)
    assert n["holder_top1_pct"] == 0.0 and n["holder_top5_pct"] == 0.0


def test_defaults_take_typical_signal_near_1x():
    take, score, mult = allocator.score_signal(TYPICAL, {"mode": "shadow"})
    assert take is True
    assert 0.9 <= mult <= 1.1
    assert abs(score - 0.5) < 0.05


def test_default_weights_cover_every_feature():
    assert set(allocator.DEFAULT_WEIGHTS) == {"intercept", *allocator.FEATURES}


@pytest.mark.parametrize("target", [0.35, 0.5, 0.2])
def test_take_threshold_boundary(target):
    cfg = {"weights": {"intercept": _logit(target)}}
    _, score, _ = allocator.score_signal({}, cfg)
    assert score == pytest.approx(target)
    assert allocator.score_signal({}, dict(cfg, take_threshold=score))[0] is True
    assert allocator.score_signal(
        {}, dict(cfg, take_threshold=score + 1e-9))[0] is False
    assert allocator.score_signal(
        {}, dict(cfg, take_threshold=score - 1e-9))[0] is True


def test_default_threshold_is_035():
    just_below = {"weights": {"intercept": _logit(0.35) - 1e-6}}
    just_above = {"weights": {"intercept": _logit(0.35) + 1e-6}}
    assert allocator.score_signal({}, just_below)[0] is False
    assert allocator.score_signal({}, just_above)[0] is True


def test_internal_exception_falls_back():
    with patch.object(allocator, "normalize", side_effect=RuntimeError("boom")):
        assert allocator.score_signal(TYPICAL, {}) == allocator.FALLBACK
    assert allocator.score_signal(TYPICAL, {"weights": {"liquidity_usd": "x"}}) \
        == allocator.FALLBACK
    assert allocator.score_signal(TYPICAL, {"take_threshold": "nan"}) \
        == allocator.FALLBACK
    assert allocator.FALLBACK == (True, 0.5, 1.0)


def test_modes():
    assert allocator.mode_of(None) == "off"
    assert allocator.mode_of("live") == "off"
    assert allocator.mode_of({}) == "shadow"
    assert allocator.mode_of({"mode": "bogus"}) == "shadow"
    assert allocator.mode_of({"mode": "live"}) == "live"
    assert allocator.mode_of({"mode": "off"}) == "off"


def test_size_native_ceiling_always_wins():
    assert allocator.size_native(0.1, 3.0, 0.15) == pytest.approx(0.15)
    assert allocator.size_native(0.1, 3.0, 0.5) == pytest.approx(0.3)
    assert allocator.size_native(0.1, 0.5, 0.5) == pytest.approx(0.05)
    assert allocator.size_native(0.1, 1.0, 0.03) == pytest.approx(0.03)
    # no ceiling available: may shrink, never grow
    assert allocator.size_native(0.1, 3.0, None) == pytest.approx(0.1)
    assert allocator.size_native(0.1, 0.5, None) == pytest.approx(0.05)
    # multiplier is clamped even if a caller passes garbage
    assert allocator.size_native(0.1, 50.0, 10.0) == pytest.approx(0.3)
    assert allocator.size_native(0.1, -50.0, 10.0) == pytest.approx(0.05)


def test_features_from_signal_commit_time():
    sig = {"m15_gain_pct": 80, "liquidity_usd": 20000, "m15_buys": 30,
           "m15_sells": 10, "buy_sell_ratio": 3.0, "m15_volume_usd": 9000,
           "mcap_usd": 0, "signal_price_usd": 1e-5, "ts": 1000.0}
    f = allocator.features_from_signal(sig, "solana", 1.1e-7, 100.0,
                                       {"holder_top1_pct": 12.0,
                                        "holder_top5_pct": 30.0}, now=1002.5)
    assert f["signal_to_fill_slippage_pct"] == pytest.approx(10.0)
    assert f["entry_latency_sec"] == pytest.approx(2.5)
    assert f["holder_top1_pct"] == 12.0 and f["chain"] == "solana"
    assert allocator.normalize(f)["mcap_usd"] == 0.0  # 0 = scanner "unknown"


def test_features_from_record_units():
    rec = {"signal_gain_pct": 80, "signal_ratio": 3.0, "liquidity_usd": 1,
           "slip_from_signal_pct": 0.14, "entry_latency_ms": 1200}
    f = allocator.features_from_record(rec)
    assert f["signal_to_fill_slippage_pct"] == pytest.approx(14.0)
    assert f["entry_latency_sec"] == pytest.approx(1.2)
    assert f["buy_sell_ratio"] == 3.0 and f["chain"] == "solana"


def test_scoring_is_microseconds():
    n = 20000
    t0 = time.perf_counter()
    for _ in range(n):
        allocator.score_signal(TYPICAL, {"mode": "live"})
    per_call_ms = (time.perf_counter() - t0) * 1000 / n
    assert per_call_ms < 1.0


# -- entry-commit wiring -----------------------------------------------------

def _account_call(method, args, **kwargs):
    if method == "getAccountInfo":
        return {"result": {"value": {"data": [base64.b64encode(bytes(82)).decode(), "base64"]}}}
    if method == "getTokenSupply":
        return {"result": {"value": {"amount": "1000"}}}
    return {"result": {"value": [{"amount": "100"}, {"amount": "50"}]}}


def _trader(tmp_path, chain, alloc_cfg=None):
    t = Trader.__new__(Trader)
    t.cfg = {"hunter": {"entry": {}, "rug_guard": {}},
             "risk": {"max_open_positions": 3,
                      "buy_sol_per_trade": .06, "buy_bnb_per_trade": .009},
             "exit": {"slippage_bps": 500, "hard_stop_pct": 35}}
    if alloc_cfg is not None:
        t.cfg["allocator"] = alloc_cfg
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
        t.rpc = SimpleNamespace(call=_account_call)
    else:
        t.bnb_usd = lambda: 800.0
        t.bscswap = lambda: SimpleNamespace(
            honeypot_check=lambda *args: (True, "ok"),
            token_decimals=lambda mint: 18,
            quote_buy=lambda *args: 1_000_000 * 10**18)
    return t


def _signal(chain):
    return {"chain": chain, "mint": "MINT" if chain == "solana" else "0xMINT",
            "name": "TEST", "m15_gain_pct": 50, "buy_sell_ratio": 2,
            "liquidity_usd": 20000, "window_label": "15m", "pool_url": "pool",
            "source": "geckoterminal", "ts": time.time() - 1,
            "signal_price_usd": .00001, "m15_buys": 20, "m15_sells": 10,
            "m15_volume_usd": 5000, "mcap_usd": 100000,
            "lp_burn_pct": None, "lp_locked": None}


BASE = {"solana": .06, "bsc": .009}
RATE = {"solana": 100.0, "bsc": 800.0}
HIGH = {"weights": {"intercept": 20.0}}   # score ~1 -> 3.0x
LOW = {"weights": {"intercept": -20.0}}   # score ~0 -> skip


def _enter(t, chain, ceiling_usd=1e9):
    with patch("fomo_trader.money.ticket_usd_ceiling", return_value=ceiling_usd), \
         patch("fomo_trader.jup_quote",
               return_value={"outAmount": str(1_000_000 * 10**6)}), \
         patch.object(t, "honeypot_check", return_value=True):
        t.enter(_signal(chain))


def _rows(tmp_path):
    p = tmp_path / "trades.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line]


CHAINS = ["solana", "bsc"]


@pytest.mark.parametrize("chain", CHAINS)
def test_live_ceiling_below_3x_wins(tmp_path, chain):
    t = _trader(tmp_path, chain, dict(HIGH, mode="live"))
    ceiling_native = 1.5 * BASE[chain]
    _enter(t, chain, ceiling_usd=ceiling_native * RATE[chain])
    pos = next(iter(t.state["positions"].values()))
    assert pos["buy_sol"] == pytest.approx(ceiling_native)
    assert pos["buy_sol"] < 3.0 * BASE[chain]
    row = _rows(tmp_path)[0]
    assert row["allocator_mode"] == "live"
    assert row["allocator_multiplier"] == pytest.approx(3.0)
    assert row["allocator_applied_native"] == pytest.approx(ceiling_native)
    assert row["buy_sol"] == pytest.approx(ceiling_native)


@pytest.mark.parametrize("chain", CHAINS)
def test_live_ceiling_below_base_still_wins(tmp_path, chain):
    t = _trader(tmp_path, chain, dict(HIGH, mode="live"))
    _enter(t, chain, ceiling_usd=0.4 * BASE[chain] * RATE[chain])
    pos = next(iter(t.state["positions"].values()))
    assert pos["buy_sol"] == pytest.approx(0.4 * BASE[chain])


@pytest.mark.parametrize("chain", CHAINS)
def test_live_upsizes_to_3x_under_a_high_ceiling(tmp_path, chain):
    t = _trader(tmp_path, chain, dict(HIGH, mode="live"))
    _enter(t, chain)
    pos = next(iter(t.state["positions"].values()))
    assert pos["buy_sol"] == pytest.approx(3.0 * BASE[chain])
    # the virtual ledger scales the quoted tokens linearly with the ticket
    assert pos["tokens_raw"] == pytest.approx(
        3.0 * (1_000_000 * 10**(6 if chain == "solana" else 18)), rel=1e-6)


@pytest.mark.parametrize("chain", CHAINS)
def test_live_skip_opens_nothing_and_counts_nothing(tmp_path, chain, capsys):
    t = _trader(tmp_path, chain, dict(LOW, mode="live"))
    _enter(t, chain)
    assert t.state["positions"] == {}
    assert t.state["trades_today"] == [] and t.state["trades_this_hour"] == []
    assert t.state["cooldown"] == {}
    assert t.pending_entries == set()
    assert _rows(tmp_path) == []
    assert "ALLOCATOR SKIP TEST score=0.000" in capsys.readouterr().out


@pytest.mark.parametrize("chain", CHAINS)
@pytest.mark.parametrize("weights", [HIGH, LOW])
def test_shadow_never_skips_and_trades_1x(tmp_path, chain, weights):
    t = _trader(tmp_path, chain, dict(weights, mode="shadow"))
    _enter(t, chain)
    pos = next(iter(t.state["positions"].values()))
    assert pos["buy_sol"] == pytest.approx(BASE[chain])
    assert len(t.state["trades_today"]) == 1
    row = _rows(tmp_path)[0]
    assert row["allocator_mode"] == "shadow"
    assert row["buy_sol"] == pytest.approx(BASE[chain])
    assert row["allocator_base_native"] == pytest.approx(BASE[chain])
    assert "allocator_applied_native" not in row
    if weights is HIGH:
        assert row["allocator_take"] is True
        assert row["allocator_multiplier"] == pytest.approx(3.0)
        assert row["allocator_would_be_native"] == pytest.approx(3.0 * BASE[chain])
    else:
        assert row["allocator_take"] is False
        assert row["allocator_would_be_native"] == 0.0
    assert 0.0 <= row["allocator_score"] <= 1.0


@pytest.mark.parametrize("chain", CHAINS)
def test_shadow_keeps_the_existing_bankroll_ceiling(tmp_path, chain):
    t = _trader(tmp_path, chain, dict(HIGH, mode="shadow"))
    _enter(t, chain, ceiling_usd=0.5 * BASE[chain] * RATE[chain])
    pos = next(iter(t.state["positions"].values()))
    assert pos["buy_sol"] == pytest.approx(0.5 * BASE[chain])


@pytest.mark.parametrize("chain", CHAINS)
@pytest.mark.parametrize("mode", ["live", "shadow"])
def test_allocator_error_commits_at_1x(tmp_path, chain, mode):
    t = _trader(tmp_path, chain, dict(LOW, mode=mode))
    with patch("fomo_trader.allocator.score_signal_with_balance",
               side_effect=RuntimeError("injected")):
        _enter(t, chain)
    pos = next(iter(t.state["positions"].values()))
    assert pos["buy_sol"] == pytest.approx(BASE[chain])
    row = _rows(tmp_path)[0]
    assert row["allocator_error"] is True
    assert row["allocator_take"] is True
    assert row["allocator_multiplier"] == 1.0
    assert row["allocator_balance_factor"] == 1.0


@pytest.mark.parametrize("chain", CHAINS)
def test_no_allocator_section_is_legacy(tmp_path, chain):
    t = _trader(tmp_path, chain, None)
    with patch("fomo_trader.allocator.score_signal_with_balance") as score:
        _enter(t, chain)
    score.assert_not_called()
    row = _rows(tmp_path)[0]
    assert not any(k.startswith("allocator_") for k in row)
    assert row["buy_sol"] == pytest.approx(BASE[chain])


@pytest.mark.parametrize("chain", CHAINS)
def test_mode_off_is_legacy(tmp_path, chain):
    t = _trader(tmp_path, chain, dict(LOW, mode="off"))
    _enter(t, chain)
    row = _rows(tmp_path)[0]
    assert not any(k.startswith("allocator_") for k in row)


@pytest.mark.parametrize("chain", CHAINS)
def test_commit_guardrails_run_before_and_independently(tmp_path, chain):
    t = _trader(tmp_path, chain, dict(HIGH, mode="live"))
    t._entry_commit_ok = lambda signal: False
    with patch("fomo_trader.allocator.score_signal_with_balance") as score:
        _enter(t, chain)
    score.assert_not_called()
    assert t.state["positions"] == {} and _rows(tmp_path) == []


def test_live_solana_features_include_holder_concentration(tmp_path):
    t = _trader(tmp_path, "solana", {"mode": "live"})
    seen = {}

    def spy(features, balance, cfg):
        seen.update(features)
        return (*allocator.FALLBACK, 1.0)
    with patch("fomo_trader.allocator.score_signal_with_balance", side_effect=spy):
        _enter(t, "solana")
    assert (seen["holder_top1_pct"], seen["holder_top5_pct"]) == (10.0, 15.0)
    assert seen["chain"] == "solana"
    assert seen["signal_to_fill_slippage_pct"] is not None
    assert seen["entry_latency_sec"] >= 0


@pytest.mark.parametrize("rel,mode", [
    ("config.example.json", "shadow"),
    # allocator reverted to shadow 2026-09-28 (follow-up fix): it went live
    # with uncalibrated priors against the standing shadow verdict; it only
    # goes live again on a pass verdict in analysis/allocator_proposal.json.
    (os.path.join("runs", "paper-1h", "config.json"), "shadow"),
])
def test_shipped_configs_are_paper_with_expected_allocator_mode(rel, mode):
    with open(os.path.join(ROOT, rel)) as f:
        cfg = json.load(f)
    assert cfg["dry_run"] is True
    assert allocator.mode_of(cfg["allocator"]) == mode
    assert allocator.weights_of(cfg["allocator"]) == allocator.DEFAULT_WEIGHTS
    assert cfg["allocator"]["take_threshold"] == 0.35


# -- offline analyst ---------------------------------------------------------

def _synthetic_journal(n, overfit=False, start=(2026, 9, 1)):
    """Deterministic: chains alternate; liquidity high/low alternates in
    pairs. Stable edge: high-liq wins 4/5, low-liq wins 1/5. Overfit:
    high-liq always wins, low-liq always loses."""
    rows = []
    t0 = time.mktime((*start, 0, 0, 0, 0, 0, -1))
    for i in range(n):
        chain = "bsc" if i % 2 else "solana"
        high = (i % 4) < 2
        k = i // 4
        win = high if overfit else ((k % 5 != 0) if high else (k % 5 == 0))
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0 + i * 3600))
        mint = "M%d" % i
        buy = .06 if chain == "solana" else .009
        rate = 150.0 if chain == "solana" else 780.0
        rows.append({"ts": ts, "type": "entry", "mint": mint, "chain": chain,
                     "buy_sol": buy, "sol_usd": rate,
                     "liquidity_usd": 300000 if high else 3000,
                     "signal_gain_pct": 100, "signal_ratio": 3})
        rows.append({"ts": ts, "type": "close", "mint": mint, "chain": chain,
                     "buy_sol": buy, "sol_usd": rate,
                     "realized_usd": 2.0 if win else -1.5})
    return rows


def _gate(rows):
    trades, _ = analyst.join_trades(rows)
    return analyst.run_gate(trades, 0.35)


def test_gate_passes_stable_edge():
    g = _gate(_synthetic_journal(150))
    assert g["verdict"] == "pass", g["checks"]
    assert g["n_oos"] >= 30
    assert g["retention"] >= 0.6
    assert g["is"]["alloc_win_rate"] <= 0.9
    assert g["is_fit_weights"]["liquidity_usd"] > 0


def test_gate_fails_overfit_win_rate():
    g = _gate(_synthetic_journal(150, overfit=True))
    assert g["verdict"] == "fail"
    failed = {c["name"] for c in g["checks"] if not c["passed"]}
    assert "is_win_rate_not_overfit" in failed
    assert g["is"]["alloc_win_rate"] > 0.9


@pytest.mark.parametrize("n", [0, 10, 60, 74])
def test_gate_insufficient_data_below_30_oos(n):
    g = _gate(_synthetic_journal(n))
    assert g["verdict"] == "insufficient_data"
    assert g["verdict"] != "pass"


def test_split_sizes():
    assert analyst.split_sizes(74) is None
    assert analyst.split_sizes(75) == (45, 30)
    assert analyst.split_sizes(100) == (70, 30)
    assert analyst.split_sizes(200) == (140, 60)


def test_gate_fails_when_edge_is_single_chain():
    rows = _synthetic_journal(150)
    # erase the edge on BSC: every BSC trade is a coin flip unrelated to liq
    bsc_i = 0
    for r in rows:
        if r["type"] == "close" and r["chain"] == "bsc":
            r["realized_usd"] = 2.0 if bsc_i % 2 else -1.5
            bsc_i += 1
    g = _gate(rows)
    assert g["verdict"] == "fail"


def test_recency_weights_decay_with_age():
    day = 86400.0
    now = 1_800_000_000.0
    ts = [now - 28 * day, now - 14 * day, now - 1 * day, now]
    w = analyst.recency_weights(ts, half_life_days=14)
    assert w[-1] == pytest.approx(1.0)
    assert w[1] == pytest.approx(0.5)
    assert w[0] == pytest.approx(0.25)
    assert w[0] < w[1] < w[2] < w[3]
    # future timestamps never exceed weight 1
    assert analyst.recency_weights([now + day], ref_ts=now) == [1.0]


def test_recency_check_flags_recent_hot_streak():
    rows = _synthetic_journal(100)
    closes = [r for r in rows if r["type"] == "close"]
    for i, r in enumerate(closes):
        r["realized_usd"] = -1.0 if i < 70 else 3.0
    trades, _ = analyst.join_trades(rows)
    rc = analyst.recency_check(trades)
    assert rc["recent_better"] and rc["recency_concentrated"]
    assert "RECENCY-CONCENTRATED" in analyst.recency_text(rc, 14)


def test_join_is_fifo_per_mint_and_prices_native_closes():
    rows = [
        {"ts": "2026-09-01 00:00:00", "type": "entry", "mint": "A", "liquidity_usd": 1},
        {"ts": "2026-09-01 00:10:00", "type": "close", "mint": "A", "realized_usd": 1.0},
        {"ts": "2026-09-01 01:00:00", "type": "entry", "mint": "A", "liquidity_usd": 2},
        {"ts": "2026-09-01 01:10:00", "type": "close", "mint": "A",
         "realized_sol": -0.01, "sol_usd": 150.0},
        {"ts": "2026-09-01 02:10:00", "type": "close", "mint": "B",
         "realized_sol": -0.01},
    ]
    trades, stats = analyst.join_trades(rows)
    assert [t["features"]["liquidity_usd"] for t in trades] == [1, 2]
    assert trades[1]["realized_usd"] == pytest.approx(-1.5)
    assert stats["unpriced"] == 1 and stats["no_entry"] == 1


def _write_rows(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_main_noops_without_25_new_closes(tmp_path, capsys):
    journal = tmp_path / "trades.jsonl"
    _write_rows(journal, _synthetic_journal(150))
    (tmp_path / "allocator_watermark.json").write_text(
        json.dumps({"closes_seen": 130}))
    before = sorted(os.listdir(tmp_path))
    assert analyst.main(["--journal", str(journal), "--out-dir", str(tmp_path),
                         "--config", str(tmp_path / "none.json")]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and "no-op" in out[0]
    assert sorted(os.listdir(tmp_path)) == before


def test_main_writes_report_proposal_watermark_never_config(tmp_path):
    journal = tmp_path / "trades.jsonl"
    rows = _synthetic_journal(150)
    _write_rows(journal, rows)
    config = tmp_path / "config.json"
    cfg_text = json.dumps({"dry_run": True, "allocator": {"mode": "shadow"}})
    config.write_text(cfg_text)
    journal_before = journal.read_bytes()
    assert analyst.main(["--journal", str(journal), "--out-dir", str(tmp_path),
                         "--config", str(config)]) == 0
    assert config.read_text() == cfg_text
    assert journal.read_bytes() == journal_before
    reports = [p for p in os.listdir(tmp_path) if p.startswith("allocator_report_")]
    assert len(reports) == 1
    report = (tmp_path / reports[0]).read_text()
    assert "Verdict: `pass`" in report and "Recency discipline" in report
    prop = json.loads((tmp_path / "allocator_proposal.json").read_text())
    assert prop["verdict"] == "pass"
    assert set(prop["weights"]) == set(allocator.DEFAULT_WEIGHTS)
    assert json.loads((tmp_path / "allocator_watermark.json").read_text())[
        "closes_seen"] == 150
    # the next cron tick is a no-op until 25 more closes land
    assert analyst.main(["--journal", str(journal), "--out-dir", str(tmp_path),
                         "--config", str(config)]) == 0
    assert len([p for p in os.listdir(tmp_path)
                if p.startswith("allocator_report_")]) == 1


def test_balance_factor_drawdown_curve():
    assert allocator.balance_factor(1000, 1000, 15) == 1.0
    assert allocator.balance_factor(1100, 1000, 15) == 1.0
    assert allocator.balance_factor(925, 1000, 15) == pytest.approx(0.75)
    assert allocator.balance_factor(850, 1000, 15) == 0.5
    assert allocator.balance_factor(700, 1000, 15) == 0.5
    assert allocator.balance_factor(925, 1000, 5e-324) == 0.5


@pytest.mark.parametrize("bankroll,peak,cap", [
    (None, 1000, 15), (1000, None, 15), (1000, 0, 15),
    (float("inf"), 1000, 15), (1000, float("nan"), 15),
    (925, 1000, float("inf")), (925, 1000, float("nan")),
    (925, 1000, None), (925, 1000, 0), (925, 1000, -15),
])
def test_balance_factor_bad_data_is_neutral(bankroll, peak, cap):
    assert allocator.balance_factor(bankroll, peak, cap) == 1.0


def test_score_signal_with_balance_reduces_ticket_and_preserves_score():
    cfg = {"weights": {"intercept": 20.0}}
    full = {"bankroll_usd": 1000, "equity_peak_usd": 1000,
            "max_drawdown_pct": 15}
    drawn = dict(full, bankroll_usd=925)
    normal = allocator.score_signal_with_balance(TYPICAL, full, cfg)
    reduced = allocator.score_signal_with_balance(TYPICAL, drawn, cfg)
    assert normal[:2] == reduced[:2] == allocator.score_signal(TYPICAL, cfg)[:2]
    assert normal[3] == 1.0
    assert reduced[3] == pytest.approx(0.75)
    assert reduced[2] < normal[2]
    assert allocator.size_native(0.1, reduced[2], 1.0) < \
        allocator.size_native(0.1, normal[2], 1.0)
    assert allocator.score_signal_with_balance(TYPICAL, drawn, cfg) == reduced
    assert allocator.size_native(0.1, normal[2], 0.01) == 0.01
    assert allocator.size_native(0.1, reduced[2], 0.01) == 0.01


def test_score_signal_with_balance_missing_data_and_internal_error():
    assert allocator.FALLBACK == (True, 0.5, 1.0)
    assert allocator.score_signal_with_balance({}, {}, {}) == \
        (True, 0.5, 1.0, 1.0)
    with patch.object(allocator, "score_signal", side_effect=RuntimeError("boom")):
        assert allocator.score_signal_with_balance({}, {}, {}) == \
            (True, 0.5, 1.0, 1.0)


@pytest.mark.parametrize("chain", CHAINS)
@pytest.mark.parametrize("mode", ["shadow", "live"])
def test_balance_factor_commit_wiring_and_journal(tmp_path, chain, mode):
    t = _trader(tmp_path, chain, dict(HIGH, mode=mode))
    t.state.update(bankroll_usd=925, equity_peak_usd=1000)
    t.cfg["money"] = {"max_drawdown_pct": 15}
    _enter(t, chain)
    row = _rows(tmp_path)[0]
    assert row["allocator_balance_factor"] == 0.75
    assert row["allocator_multiplier"] == 2.25
    expected = BASE[chain] if mode == "shadow" else 2.25 * BASE[chain]
    assert row["buy_sol"] == pytest.approx(expected)
    assert row["allocator_would_be_native"] == pytest.approx(
        2.25 * BASE[chain])
