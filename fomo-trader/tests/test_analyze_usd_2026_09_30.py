"""Tests for the 2026-09-30 auto-tuner P&L integrity fix (Track A).

Regression: analyze.py used to compute every close's P&L from
`realized_sol` only. All 223 BSC closes have realized_sol=None, so they
became $0 phantoms -- counted as zero-value LOSSES (realized <= 0),
diluting win rate / PF and letting the tuner tighten both-chains filters
on a SOL-only view of ~19% of the book (min_buy_sell_ratio 2.0 -> 2.5 on
2026-09-30 11:14 UTC).

The fix: per-close P&L is USD and chain-aware (journaled realized_usd
first; legacy SOL closes via realized_sol x stamped/fallback SOL price);
unpriced closes are EXCLUDED from stats (never $0); the tuner refuses to
act when >20% of closes are unpriced.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import analyze


def close(**kw):
    base = {"type": "close", "ts": "2026-09-30 10:00:00", "mint": "MINT1",
            "name": "T / WBNB", "reason": "dump detector"}
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# close_pnl_usd
# --------------------------------------------------------------------------

def test_bsc_row_uses_journaled_realized_usd():
    c = close(chain="bsc", realized_usd=-1.71, realized_sol=None,
              sol_usd=776.78)  # sol_usd is BNB/USD on BSC rows; must be ignored
    assert analyze.close_pnl_usd(c) == pytest.approx(-1.71)


def test_bsc_row_missing_realized_usd_is_unpriced_not_zero():
    # The old code turned this into a $0 phantom loss. It must be None.
    c = close(chain="bsc", realized_usd=None, realized_sol=None,
              realized_bnb=-0.0022)
    assert analyze.close_pnl_usd(c) is None


def test_legacy_sol_row_priced_via_stamped_sol_usd():
    c = close(chain="?", realized_sol=-0.01, sol_usd=150.0)
    assert analyze.close_pnl_usd(c) == pytest.approx(-1.50)


def test_legacy_sol_row_uses_cli_fallback_price():
    c = close(chain="?", realized_sol=-0.02)
    assert analyze.close_pnl_usd(c, fallback_usd=200.0) == pytest.approx(-4.0)


def test_unpriced_row_returns_none():
    c = close(chain="?", realized_sol=-0.02)  # no sol_usd, no fallback
    assert analyze.close_pnl_usd(c) is None


def test_junk_values_return_none():
    assert analyze.close_pnl_usd(close(realized_usd="abc")) is None
    assert analyze.close_pnl_usd(close()) is None


# --------------------------------------------------------------------------
# enrich_closes
# --------------------------------------------------------------------------

def test_enrich_closes_prices_full_book_in_usd():
    closes = [
        close(mint="A", chain="bsc", realized_usd=5.0, realized_sol=None,
              ts="2026-09-30 10:00:00"),
        close(mint="B", chain="bsc", realized_usd=-2.0, realized_sol=None,
              ts="2026-09-30 10:05:00"),
        close(mint="C", chain="?", realized_sol=-0.01, sol_usd=100.0,
              ts="2026-09-30 10:10:00"),
        close(mint="D", chain="?", realized_sol=-0.5,  # unpriced
              ts="2026-09-30 10:15:00"),
    ]
    entries = [
        {"type": "entry", "mint": "A", "ts": "2026-09-30 09:55:00",
         "source": "geckoterminal", "signal_gain_pct": 120.0},
    ]
    enriched, n_unpriced = analyze.enrich_closes(closes, entries,
                                                 fallback_usd=None)
    assert n_unpriced == 1
    assert len(enriched) == 3
    by_mint = {e["close"]["mint"]: e for e in enriched}
    assert by_mint["A"]["realized"] == pytest.approx(5.0)
    assert by_mint["B"]["realized"] == pytest.approx(-2.0)
    assert by_mint["C"]["realized"] == pytest.approx(-1.0)
    # no $0 phantom for any BSC row
    assert by_mint["A"]["realized"] != 0.0
    assert by_mint["B"]["realized"] != 0.0
    # entry context still joins
    assert by_mint["A"]["source"] == "geckoterminal"
    assert by_mint["A"]["sig_gain"] == 120.0
    assert by_mint["A"]["hold_min"] == pytest.approx(5.0)
    assert by_mint["B"]["source"] == "unknown"


def test_enrich_closes_never_invents_zero_pnl():
    closes = [close(chain="bsc", realized_usd=None, realized_sol=None)
              for _ in range(5)]
    enriched, n_unpriced = analyze.enrich_closes(closes, [], None)
    assert enriched == []
    assert n_unpriced == 5


# --------------------------------------------------------------------------
# priced_data_ok (fail-closed coverage guard)
# --------------------------------------------------------------------------

def test_priced_data_ok_boundaries():
    assert analyze.priced_data_ok(100, 0)
    assert analyze.priced_data_ok(80, 20)      # exactly 20% is fine
    assert not analyze.priced_data_ok(79, 21)  # just over 20% blocks
    assert analyze.priced_data_ok(0, 0)         # empty: MIN_TRADES gate owns it


# --------------------------------------------------------------------------
# propose_tuning on USD stats
# --------------------------------------------------------------------------

def _row(pnl, source="unknown", reason="other", gain=200.0):
    return {"close": {"ts": "2026-09-30 10:00:00"}, "realized": pnl,
            "reason": reason, "source": source, "sig_gain": gain,
            "hold_min": 5.0}


def test_rule4_worst_source_ranked_on_usd():
    # BSC-heavy book: "unknown" (BSC flow) is the real loser in USD.
    enriched = ([_row(-2.0, "unknown") for _ in range(10)] +
                [_row(-0.1, "geckoterminal") for _ in range(10)] +
                [_row(0.5, "geckoterminal") for _ in range(5)])
    wins = [e for e in enriched if e["realized"] > 0]
    losses = [e for e in enriched if e["realized"] <= 0]
    reasons = {"other": {"n": 25, "total": sum(e["realized"] for e in enriched)}}
    buckets = {"<150%": [], "150-300%": [], ">300%": enriched, "unknown": []}
    sources = {"unknown": {"n": 10, "wins": 0, "total": -20.0},
               "geckoterminal": {"n": 15, "wins": 5, "total": 1.5}}
    cfg = {"hunter": {"min_buy_sell_ratio": 2.0}}
    prop = analyze.propose_tuning(enriched, wins, losses, 0.5, -1.5,
                                  reasons, buckets, sources, 0.3, cfg)
    assert prop is not None
    assert prop["path"] == "hunter.min_buy_sell_ratio"
    assert prop["new"] == 2.5
    assert "$" in prop["text"] and "SOL" not in prop["text"]
    assert "-$20.00" not in prop["text"]  # no SOL-era fragments
    assert "$-20.00" in prop["text"]  # real USD evidence


def test_no_tuning_when_stats_do_not_support_it():
    enriched = [_row(1.0, "geckoterminal") for _ in range(12)]
    wins = enriched
    reasons = {"other": {"n": 12, "total": 12.0}}
    buckets = {"<150%": [], "150-300%": [], ">300%": enriched, "unknown": []}
    sources = {"geckoterminal": {"n": 12, "wins": 12, "total": 12.0}}
    prop = analyze.propose_tuning(enriched, wins, [], 1.0, 0.0,
                                  reasons, buckets, sources, 99.0,
                                  {"hunter": {"min_buy_sell_ratio": 2.0}})
    assert prop is None


# --------------------------------------------------------------------------
# maybe_revert speaks USD
# --------------------------------------------------------------------------

def test_maybe_revert_rationale_in_usd(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"dry_run": True, "hunter": {}}))
    monkeypatch.setattr(analyze, "CONFIG", str(cfg_path))
    monkeypatch.setattr(analyze, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(analyze, "TWEAKS_LOG", str(tmp_path / "tweaks.jsonl"))
    monkeypatch.setattr(analyze, "CHANGELOG", str(tmp_path / "CHANGELOG.md"))
    monkeypatch.setattr(analyze, "restart_bot", lambda: "stubbed restart")
    before = [_row(1.0) for _ in range(3)]
    for e in before:
        e["close"] = {"ts": "2026-09-19 10:00:00"}
    after = [_row(-1.0) for _ in range(5)]
    for e in after:
        e["close"] = {"ts": "2026-09-21 10:00:00"}
    tweaks = [{"action": "apply", "ts": "2026-09-20 00:00:00",
               "path": "hunter.min_buy_sell_ratio", "old": 2.0, "new": 2.5}]
    note = analyze.maybe_revert({"dry_run": True, "hunter": {}},
                                before + after, tweaks)
    assert note is not None and "REVERTED" in note
    logged = [json.loads(l) for l in
              (tmp_path / "tweaks.jsonl").read_text().splitlines()]
    assert logged and logged[-1]["action"] == "revert"
    assert "$" in logged[-1]["rationale"] and "SOL" not in logged[-1]["rationale"]


def test_maybe_revert_no_action_when_not_worse(tmp_path, monkeypatch):
    rows = [_row(1.0) for _ in range(4)] + [_row(0.8) for _ in range(5)]
    for e in rows[:4]:
        e["close"] = {"ts": "2026-09-19 10:00:00"}
    for e in rows[4:]:
        e["close"] = {"ts": "2026-09-21 10:00:00"}
    tweaks = [{"action": "apply", "ts": "2026-09-20 00:00:00",
               "path": "hunter.min_buy_sell_ratio", "old": 2.0, "new": 2.5}]
    assert analyze.maybe_revert({}, rows, tweaks) is None


# --------------------------------------------------------------------------
# end-to-end main() on a synthetic journal
# --------------------------------------------------------------------------

def _write_journal(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_main_reports_usd_and_excludes_unpriced(tmp_path, monkeypatch, capsys):
    journal = tmp_path / "trades.jsonl"
    _write_journal(journal, [
        {"type": "close", "ts": "2026-09-30 10:00:00", "mint": "A",
         "name": "WIN / WBNB", "chain": "bsc", "realized_usd": 3.0,
         "realized_sol": None, "reason": "take profit 50% @+100%"},
        {"type": "close", "ts": "2026-09-30 10:05:00", "mint": "B",
         "name": "LOSS / WBNB", "chain": "bsc", "realized_usd": -2.0,
         "realized_sol": None, "reason": "dump detector"},
        {"type": "close", "ts": "2026-09-30 10:10:00", "mint": "C",
         "name": "NOPRICE", "chain": "?", "reason": "other"},  # unpriced
    ])
    monkeypatch.setattr(analyze, "JOURNAL", str(journal))
    monkeypatch.setattr(analyze, "STATE", str(tmp_path / "no-state.json"))
    monkeypatch.setattr(analyze, "CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setattr(analyze, "ANALYSIS_DIR", str(tmp_path / "analysis"))
    monkeypatch.setattr(sys, "argv", ["analyze.py"])
    analyze.main()
    out = capsys.readouterr().out
    assert "closed trades: 2" in out
    assert "$+1.00" in out                       # 3.0 + -2.0
    assert "1 unpriced close(s) excluded" in out
    assert "P&L in USD" in out
    for line in out.splitlines():
        if "realized" in line or "avg win" in line or "avg loss" in line:
            assert "SOL" not in line
    # report is dated today; find whatever was written
    written = list((tmp_path / "analysis").glob("*.md"))
    assert len(written) == 1
    text = written[0].read_text()
    assert "$+1.00" in text and "SOL" not in text.split("## Open positions")[0]
