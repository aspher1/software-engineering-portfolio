import base64
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fomo_trader import DexScreenerSource, Hunter, SOL_MINT, Trader, entry_evidence, lp_evidence


FIELDS = ("signal_price_usd", "commit_price_native", "commit_price_usd",
          "slip_from_signal_pct", "holder_top1_pct", "holder_top5_pct",
          "lp_burn_pct", "lp_locked", "liquidity_usd", "entry_latency_ms", "m15_buys",
          "m15_sells", "m15_volume_usd", "mcap_usd")


def trader(tmp_path, chain, account):
    t = Trader.__new__(Trader)  # no wallet file is loaded
    t.cfg = {"hunter": {"entry": {}, "rug_guard": {}},
             "risk": {"max_open_positions": 3,
                      "buy_sol_per_trade": .06, "buy_bnb_per_trade": .009},
             "exit": {"slippage_bps": 500}}
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
        t.rpc = SimpleNamespace(call=account)
    else:
        t.bnb_usd = lambda: 800.0
        t.bscswap = lambda: SimpleNamespace(
            honeypot_check=lambda *args: (True, "ok"),
            token_decimals=lambda mint: 18,
            quote_buy=lambda *args: 1_000_000 * 10**18)
    return t


def signal(chain, **extra):
    s = {"chain": chain, "mint": "MINT" if chain == "solana" else "0xMINT",
         "name": "TEST", "m15_gain_pct": 50, "buy_sell_ratio": 2,
         "liquidity_usd": 20000, "window_label": "15m", "pool_url": "pool",
         "source": "geckoterminal", "ts": time.time() - 1,
         "signal_price_usd": .00001, "m15_buys": 20, "m15_sells": 10,
         "m15_volume_usd": 5000, "mcap_usd": 100000,
         "lp_burn_pct": None, "lp_locked": None}
    s.update(extra)
    return s


def account_call(method, args, **kwargs):
    if method == "getAccountInfo":
        return {"result": {"value": {"data": [base64.b64encode(bytes(82)).decode(), "base64"]}}}
    if method == "getTokenSupply":
        return {"result": {"value": {"amount": "1000"}}}
    return {"result": {"value": [{"amount": "100"}, {"amount": "50"}]}}


@pytest.mark.parametrize("chain", ["solana", "bsc"])
def test_paper_entry_journals_and_positions(tmp_path, chain):
    t = trader(tmp_path, chain, account_call)
    with patch("fomo_trader.jup_quote", return_value={"outAmount": str(1_000_000 * 10**6)}), \
         patch.object(t, "honeypot_check", return_value=True):
        t.enter(signal(chain))
    row = json.loads((tmp_path / "trades.jsonl").read_text().strip())
    pos = t.state["positions"][row["mint"]]
    assert row["type"] == "entry" and t.dry_run is True
    assert all(field in row and row[field] == pos[field] for field in FIELDS)
    assert row["liquidity_usd"] == 20000
    assert row["entry_latency_ms"] >= 0
    assert row["signal_price_usd"] == .00001
    assert row["commit_price_native"] == row["entry"]
    assert row["commit_price_usd"] == pytest.approx(row["entry"] * row["sol_usd"])
    assert row["slip_from_signal_pct"] == pytest.approx(
        row["commit_price_usd"] / .00001 - 1)
    assert (row["holder_top1_pct"], row["holder_top5_pct"]) == (
        (10, 15) if chain == "solana" else (None, None))
    assert row["lp_burn_pct"] is None and row["lp_locked"] is None
    assert (row["m15_buys"], row["m15_sells"], row["m15_volume_usd"],
            row["mcap_usd"]) == (20, 10, 5000, 100000)


@pytest.mark.parametrize("chain", ["solana", "bsc"])
def test_missing_optional_signal_fields_do_not_block_entry(tmp_path, chain):
    t = trader(tmp_path, chain, account_call)
    s = signal(chain)
    for field in ("signal_price_usd", "m15_buys", "m15_sells",
                  "m15_volume_usd", "mcap_usd", "lp_burn_pct", "lp_locked"):
        s.pop(field)
    with patch("fomo_trader.jup_quote", return_value={"outAmount": str(1_000_000 * 10**6)}), \
         patch.object(t, "honeypot_check", return_value=True):
        t.enter(s)
    row = json.loads((tmp_path / "trades.jsonl").read_text().strip())
    assert row["signal_price_usd"] is None
    assert row["slip_from_signal_pct"] is None
    assert all(row[field] is None for field in ("m15_buys", "m15_sells",
                                            "m15_volume_usd", "mcap_usd",
                                            "lp_burn_pct", "lp_locked"))


def test_rug_guard_still_fails_closed_on_mint_authority(tmp_path):
    bad = bytearray(82)
    bad[0:4] = (1).to_bytes(4, "little")
    calls = []

    def rpc(method, args, **kwargs):
        calls.append(method)
        return {"result": {"value": {"data": [base64.b64encode(bad).decode(), "base64"]}}}

    t = trader(tmp_path, "solana", rpc)
    with patch("fomo_trader.jup_quote") as quote:
        t.enter(signal("solana"))
    quote.assert_not_called()
    assert calls == ["getAccountInfo"]
    assert not t.state["positions"]
    assert not (tmp_path / "trades.jsonl").exists()


def test_lp_evidence_only_explicit_fields():
    assert lp_evidence({"reserve_in_usd": "20000"}) == (None, None)
    assert lp_evidence({"lp_burn_pct": "30", "lp_locked": True}) == (30, True)


def test_scanners_copy_price_and_lp_fields_from_payloads():
    config = {"hunter": {"solana_pages_trending": 1, "solana_pages_new": 0,
                         "bsc_pages_trending": 0, "bsc_pages_new": 0,
                         "min_m15_gain_pct": 20, "min_buy_sell_ratio": 1.5,
                         "min_liquidity_usd": 8000, "min_m15_volume_usd": 5000,
                         "min_m15_buys": 10, "min_m15_sells": 5,
                         "early": {"enabled": False}}}
    attrs = {"address": "POOL", "name": "TEST / SOL",
             "price_change_percentage": {"m15": 30},
             "reserve_in_usd": 20000, "volume_usd": {"m15": 6000},
             "transactions": {"m15": {"buys": 20, "sells": 10}},
             "base_token_price_usd": "0.001", "lp_burn_pct": 50,
             "lp_locked": True}
    item = {"attributes": attrs,
            "relationships": {"base_token": {"data": {"id": "solana_MINT"}},
                              "quote_token": {"data": {"id": "solana_" + SOL_MINT}}}}
    with patch("fomo_trader.gt_get", return_value={"data": [item]}):
        rows, _ = Hunter(config)._scan_gt()
    assert rows[0]["signal_price_usd"] == .001
    assert (rows[0]["lp_burn_pct"], rows[0]["lp_locked"]) == (50, True)

    pair = {"chainId": "solana", "pairAddress": "POOL", "priceUsd": "0.002",
            "baseToken": {"address": "MINT", "name": "TEST"},
            "quoteToken": {"address": SOL_MINT},
            "priceChange": {"m5": 10}, "volume": {"m5": 3000},
            "txns": {"m5": {"buys": 20, "sells": 10}},
            "liquidity": {"usd": 20000}}
    ds = DexScreenerSource(config)._signal(pair)
    assert ds["signal_price_usd"] == .002
    assert ds["lp_burn_pct"] is None and ds["lp_locked"] is None


def test_entry_evidence_latency_nonnegative():
    assert entry_evidence({"ts": time.time() + 5}, .1, 100)["entry_latency_ms"] == 0


def test_entry_evidence_assembly_timing():
    s = signal("solana")
    holder = {"holder_top1_pct": 10.0, "holder_top5_pct": 25.0}
    count = 100_000
    start = time.perf_counter()
    for _ in range(count):
        evidence = entry_evidence(s, .00001, 100.0, holder, s["ts"] + 1)
    ms_per_call = (time.perf_counter() - start) * 1000 / count
    assert evidence["holder_top1_pct"] == 10.0
    print(f"entry evidence assembly: {ms_per_call:.6f} ms/call over {count} calls")


@pytest.mark.parametrize("chain", ["solana", "bsc"])
def test_rejected_commit_does_not_fetch_usd_rate(tmp_path, chain):
    t = trader(tmp_path, chain, account_call)
    t._entry_commit_ok = lambda signal: False
    rate_name = "sol_usd" if chain == "solana" else "bnb_usd"
    with patch.object(t, rate_name) as rate, \
         patch("fomo_trader.jup_quote", return_value={"outAmount": str(1_000_000 * 10**6)}), \
         patch.object(t, "honeypot_check", return_value=True):
        t.enter(signal(chain))
    rate.assert_not_called()
    assert not t.state["positions"]
