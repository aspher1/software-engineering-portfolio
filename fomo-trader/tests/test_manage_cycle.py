#!/usr/bin/env python3
"""Integrated manage-cycle smoke test for fomo_trader.py (paper only).

Exercises _manage_once() exit paths with synthetic positions and a
throwaway keypair - no real entries, no network, no wallet funds touched:
  1. TP at +50% sells half the bag and leaves the runner open
  2. Failed TP sell is NOT marked and retries on the next cycle
  3. After a TP, the trailing stop tightens (12%) so the rest rotates out
  4. Dump detector fires on a >=12% drop inside 60s
  5. Trailing stop fires on a 25% drawdown from peak (no TP yet)
  6. Hard stop fires on a 35% drop from entry
  7. Stale exit fires on a flat old position
  8. close_trade() handles 3-element rungs [idx, gain, proceeds]
  9. close_trade() still handles legacy 2-element / bare-int rungs
  10. Venue dump tripwire fires on DexScreener m5 <= -30%
  11. Venue tripwire stays quiet on m5 = -5% and on API failure (None)
  12. Venue check is throttled to venue_check_sec per position
  13. Kill-switch protect mode: tripped USD cap tightens the trail to 10%
      (an 11% drawdown closes; the untripped control stays open)
  14. Protect mode persists across a day rollover and never loosens the
      post-TP trail (uses 10% rather than 12%)
  15. SOL cap trips the helper and moonbag trail tightens to 10%;
      below both caps the helper and entry guard stay clear
  16. Protect trail never loosens a tighter normal trail, and invalid
      configured values fall back to 10%
  17. Commit-time entry guard refuses a trade whose in-flight minutes
      overlapped a kill-switch trip (kill switch rechecked at commit)
  18. Commit keeps the daily and hourly trade caps
  19. Commit allows when caps and kill switch are clear
"""
import json
import os
import sys
import tempfile
import time
import base64
from collections import deque
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solders.keypair import Keypair  # noqa: E402

from fomo_trader import DexScreenerSource, Hunter, SOL_MINT, Trader  # noqa: E402

ENTRY = 1.0e-9          # synthetic entry price (SOL per token)
BUY_SOL = 0.06
TOKENS = int(1e12)      # synthetic raw token balance (6 decimals worth)

PASSED = []
FAILED = []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print("%s %s %s" % ("PASS" if cond else "FAIL", name,
                        ("- " + detail) if detail and not cond else ""))


def make_trader():
    tmp = tempfile.mkdtemp(prefix="fomo-test-")
    kp = Keypair()
    keyfile = os.path.join(tmp, "key.json")
    with open(keyfile, "w") as f:
        f.write(json.dumps(list(bytes(kp))))
    os.chmod(keyfile, 0o600)
    cfg = {
        "dry_run": True,
        "rpc_urls": ["https://api.mainnet-beta.solana.com"],
        "wallets": {"solana_key_file": keyfile},
        "exit": {
            "take_profits": [[50, 50]],
            "trailing_stop_pct": 25,
            "trail_after_tp_pct": 12,
            "hard_stop_pct": 35,
            "dump_drop_pct": 12,
            "dump_window_sec": 60,
            "venue_dump_m5_pct": -30,
            "venue_check_sec": 30,
            "stale_exit_min": 30,
            "stale_exit_max_gain_pct": 10,
            "slippage_bps": 500,
            "sell_poll_sec": 5,
            "mode": "ladder",
        },
        "risk": {"max_open_positions": 3, "max_trades_per_day": 10,
                 "kill_switch_max_daily_loss_sol": 0.2,
                 "buy_sol_per_trade": 0.06, "cooldown_min_per_mint": 120},
    }
    t = Trader(cfg, os.path.join(tmp, "state.json"))
    return t, tmp


def add_position(t, mint, entry=ENTRY, peak=None, age_min=1,
                 tokens_raw=TOKENS, rungs=None, sold_sol=0.0):
    now = time.time()
    t.state["positions"][mint] = {
        "name": "TEST/" + mint[:6], "entry": entry,
        "peak": peak if peak is not None else entry,
        "buy_sol": BUY_SOL, "tokens_raw": tokens_raw,
        "sold_sol": sold_sol, "decimals": 9,
        "opened_at": now - age_min * 60, "rungs_fired": rungs or [],
        "quote_fails": 0, "last_price_ts": now,
    }


class PriceFeed:
    """Mutable fake price; optional one-shot swap failure."""

    def __init__(self, px):
        self.px = px
        self.fail_next_swap = False
        self.swaps = []

    def price(self, mint):
        return self.px

    def swap(self, input_mint, output_mint, amount_raw, label):
        if self.fail_next_swap:
            self.fail_next_swap = False
            raise RuntimeError("simulated swap failure")
        out_est = int(amount_raw * self.px * 1e9)
        self.swaps.append((label, amount_raw, out_est))
        return out_est, "fakesig-%d" % len(self.swaps)


def wire(t, feed):
    t.price_sol = feed.price
    t.price_native_fast = lambda mint, chain="solana": feed.price(mint)
    t.execute_swap = feed.swap
    t.venue_m5_dump = lambda mint, chain="solana": None  # venue tape quiet unless overridden
    t.sol_usd = lambda: 115.0   # hermetic USD stamping
    t.bnb_usd = lambda: 780.0


def manage_params(t):
    ex = t.cfg["exit"]
    return (ex["take_profits"], ex["trailing_stop_pct"],
            ex["hard_stop_pct"])


def test_tp_rung_fire():
    t, _ = make_trader()
    feed = PriceFeed(ENTRY * 1.70)   # +70% -> TP (50%) must fire, sell 50%
    wire(t, feed)
    mint = "TP1111111111111111111111111111111111111111"
    add_position(t, mint)
    closed = t._manage_once(mint, *manage_params(t))
    pos = t.state["positions"][mint]
    check("tp at +50% fires and leaves the runner open", not closed,
          "closed=%s" % closed)
    check("tp sells half the bag",
          pos["tokens_raw"] == TOKENS // 2,
          "tokens_raw=%s" % pos["tokens_raw"])
    check("tp rung is 3-element [idx, gain, proceeds]",
          len(pos["rungs_fired"]) == 1 and len(pos["rungs_fired"][0]) == 3,
          "rungs=%s" % pos["rungs_fired"])


def test_tp_retry_after_failure():
    t, _ = make_trader()
    feed = PriceFeed(ENTRY * 1.70)
    wire(t, feed)
    mint = "RT1111111111111111111111111111111111111111"
    add_position(t, mint, rungs=[])
    feed.fail_next_swap = True
    closed = t._manage_once(mint, *manage_params(t))   # sell fails
    pos = t.state["positions"][mint]
    check("failed sell does not mark rung",
          len(pos["rungs_fired"]) == 0, "rungs=%s" % pos["rungs_fired"])
    check("failed sell keeps full bag", pos["tokens_raw"] == TOKENS)
    check("failed sell leaves position open", not closed)
    closed = t._manage_once(mint, *manage_params(t))   # retry succeeds
    pos = t.state["positions"][mint]
    check("retry marks rung exactly once", len(pos["rungs_fired"]) == 1,
          "rungs=%s" % pos["rungs_fired"])
    check("retry sells half, runner still open",
          not closed and pos["tokens_raw"] == TOKENS // 2)


def test_pidfile_release_keeps_newer_bot():
    # restart overlap: our shutdown must not delete a newer bot's pidfile
    t, tmp = make_trader()
    pf = os.path.join(tmp, "bot.pid")
    with open(pf, "w") as f:
        f.write("999999999")  # some other (newer) bot's pid
    t._release_pidfile(pf)
    check("pidfile of a newer bot is not deleted", os.path.exists(pf))
    with open(pf, "w") as f:
        f.write(str(os.getpid()))
    t._release_pidfile(pf)
    check("own pidfile is removed on shutdown", not os.path.exists(pf))


def _guard_trader():
    t, _ = make_trader()
    t.cfg["risk"]["max_trades_per_hour"] = 3
    sig = {"mint": "HH1111111111111111111111111111111111111111",
           "name": "HOUR", "ts": time.time()}
    return t, sig


def test_hourly_cap_blocks_fourth_trade():
    t, sig = _guard_trader()
    now = time.time()
    t.state["trades_this_hour"] = [now - 100, now - 200, now - 300]
    check("hourly cap blocks the 4th trade in an hour",
          t.guardrails_ok(sig) is False)


def test_hourly_window_resets_itself():
    # 3 trades, but all >1h old: the rolling window prunes them, so a new
    # trade is allowed with no manual reset
    t, sig = _guard_trader()
    now = time.time()
    t.state["trades_this_hour"] = [now - 3700, now - 3800, now - 3900]
    ok = t.guardrails_ok(sig)
    check("hourly window auto-resets after 60 minutes", ok is True)
    check("old hour entries are pruned",
          t.state["trades_this_hour"] == [])


def test_hourly_cap_allows_under_limit():
    t, sig = _guard_trader()
    now = time.time()
    t.state["trades_this_hour"] = [now - 100, now - 200]
    check("2 trades in the last hour allows a 3rd",
          t.guardrails_ok(sig) is True)


def test_commit_kill_switch_blocks_midflight_entry():
    # An entry spends minutes in flight (rug screen, pullback wait, quotes,
    # honeypot check) AFTER guardrails_ok() ran. Another position can close
    # at a loss and trip the daily-loss kill switch in between; the trade
    # must not commit then. Test cfg caps SOL loss at 0.2/day.
    t, _ = _guard_trader()
    t.state["realized_sol"] = -0.25
    t.pending_entries.add("MIDFLIGHT")
    check("commit refuses when kill switch tripped mid-flight",
          t._entry_commit_ok({"name": "MIDFLIGHT", "ts": time.time()}) is False)


def test_commit_caps_still_block():
    t, _ = _guard_trader()
    now = time.time()
    t.state["trades_today"] = [now - 60] * 10  # daily cap reached
    check("commit keeps the daily-trade cap",
          t._entry_commit_ok({"name": "D", "ts": time.time()}) is False)
    t.state["trades_today"] = []
    t.state["trades_this_hour"] = [now - 100, now - 200, now - 300]
    check("commit keeps the hourly-trade cap",
          t._entry_commit_ok({"name": "H", "ts": time.time()}) is False)


def test_commit_clear_allows():
    t, _ = _guard_trader()
    t.pending_entries.add("CLEAR")
    check("commit allows when caps and kill switch are clear",
          t._entry_commit_ok({"name": "CLEAR", "ts": time.time()}) is True)


def _aged_signal(source="geckoterminal", early=False):
    return {"name": "AGED", "mint": "AGEDMINT", "pool": "POOL123",
            "chain": "solana", "source": source, "early": early,
            "ts": time.time() - 181}


def _gt_pool(gain=25, liq=9000, buys=20, sells=10):
    return {"data": {"attributes": {
        "address": "POOL123", "price_change_percentage": {"m15": gain},
        "reserve_in_usd": liq,
        "transactions": {"m15": {"buys": buys, "sells": sells}}}}}


def test_signal_fresh_zero_http():
    t, _ = _guard_trader()
    t.cfg["hunter"] = {"entry": {"signal_max_age_sec": 180}}
    sig = _aged_signal()
    sig["ts"] = time.time() - 179
    with patch("fomo_trader.gt_get") as gt, patch("fomo_trader.ds_get") as ds:
        ok = t._entry_commit_ok(sig)
    check("fresh signal passes with zero feed calls",
          ok and gt.call_count == 0 and ds.call_count == 0)


def test_signal_aged_strong():
    t, _ = _guard_trader()
    t.cfg["hunter"] = {"entry": {"signal_max_age_sec": 180},
                       "min_m15_gain_pct": 20, "min_liquidity_usd": 8000,
                       "min_buy_sell_ratio": 1.5}
    with patch("fomo_trader.gt_get", return_value=_gt_pool()) as gt:
        ok = t._entry_commit_ok(_aged_signal())
    check("aged strong signal passes after one pool fetch",
          ok and gt.call_count == 1)


def test_signal_aged_faded_gain():
    t, _ = _guard_trader()
    t.cfg["hunter"] = {"min_m15_gain_pct": 20,
                       "min_liquidity_usd": 8000,
                       "min_buy_sell_ratio": 1.5}
    with patch("fomo_trader.gt_get", return_value=_gt_pool(gain=19)):
        check("aged faded gain skips", not t._entry_commit_ok(_aged_signal()))


def test_signal_aged_drained_liquidity():
    t, _ = _guard_trader()
    t.cfg["hunter"] = {"min_m15_gain_pct": 20,
                       "min_liquidity_usd": 8000,
                       "min_buy_sell_ratio": 1.5}
    with patch("fomo_trader.gt_get", return_value=_gt_pool(liq=7999)):
        check("aged drained liquidity skips",
              not t._entry_commit_ok(_aged_signal()))


def test_signal_aged_refetch_failure():
    t, _ = _guard_trader()
    t.cfg["hunter"] = {}
    with patch("fomo_trader.gt_get", side_effect=RuntimeError("feed down")) as gt:
        ok = t._entry_commit_ok(_aged_signal())
    check("aged re-fetch failure skips after one call",
          not ok and gt.call_count == 1)


def test_signal_max_age_fallback():
    t, _ = _guard_trader()
    t.cfg["hunter"] = {}
    sig = _aged_signal()
    sig["ts"] = time.time() - 179
    with patch("fomo_trader.gt_get") as gt:
        missing_ok = t._entry_commit_ok(sig)
        invalid_ok = []
        for value in (None, "bad", 0, -1, True, float("nan")):
            t.cfg["hunter"]["entry"] = {"signal_max_age_sec": value}
            invalid_ok.append(t._entry_commit_ok(sig))
    check("missing or invalid max age uses 180 seconds",
          missing_ok and all(invalid_ok) and gt.call_count == 0)


def test_signal_aged_ratio_and_early_bar():
    t, _ = _guard_trader()
    t.cfg["hunter"] = {"min_m15_gain_pct": 100,
                       "early": {"min_m15_gain_pct": 15,
                                 "min_liquidity_usd": 8000,
                                 "min_buy_sell_ratio": 1.5}}
    with patch("fomo_trader.gt_get", return_value=_gt_pool()) as gt:
        early_ok = t._entry_commit_ok(_aged_signal(early=True))
    with patch("fomo_trader.gt_get",
               return_value=_gt_pool(buys=10, sells=10)):
        ratio_ok = t._entry_commit_ok(_aged_signal(early=True))
    check("aged early signal uses early bars and checks ratio",
          early_ok and not ratio_ok and gt.call_count == 1)


def test_signal_aged_dexscreener():
    t, _ = _guard_trader()
    t.cfg["hunter"] = {"dexscreener": {"min_m5_gain_pct": 5,
                                        "min_liquidity_usd": 8000,
                                        "min_buy_sell_ratio": 1.5}}
    pair = {"chainId": "solana", "pairAddress": "POOL123",
            "priceChange": {"m5": 8}, "liquidity": {"usd": 9000},
            "txns": {"m5": {"buys": 20, "sells": 10}}}
    with patch("fomo_trader.ds_get", return_value={"pairs": [pair]}) as ds:
        ok = t._entry_commit_ok(_aged_signal(source="dexscreener"))
    check("aged DexScreener signal checks its 5m pool",
          ok and ds.call_count == 1)


def test_scanners_stamp_all_signal_paths():
    def gt_item(chain, pool, mint, quote, gain, m5):
        return {"attributes": {
            "address": pool, "name": "TEST / SOL" if chain == "solana"
            else "TEST / WBNB", "price_change_percentage": {"m15": gain,
                                                                "m5": m5},
            "reserve_in_usd": 9000, "volume_usd": {"m15": 6000},
            "transactions": {"m15": {"buys": 20, "sells": 10}}},
            "relationships": {
                "base_token": {"data": {"id": chain + "_" + mint}},
                "quote_token": {"data": {"id": chain + "_" + quote}}}}

    cfg = {"hunter": {"solana_pages_trending": 1, "solana_pages_new": 0,
                      "bsc_pages_trending": 1, "bsc_pages_new": 0,
                      "min_m15_gain_pct": 20, "min_liquidity_usd": 8000,
                      "min_buy_sell_ratio": 1.5, "min_m15_buys": 10,
                      "min_m15_sells": 5, "min_m15_volume_usd": 5000,
                      "early": {"enabled": True, "min_m15_gain_pct": 15,
                                "min_liquidity_usd": 8000,
                                "min_buy_sell_ratio": 1.5}}}
    sol = [gt_item("solana", "SOL_FOMO", "SOL_FOMO_MINT", SOL_MINT, 25, 12),
           gt_item("solana", "SOL_EARLY", "SOL_EARLY_MINT", SOL_MINT, 18, 10)]
    bsc = [gt_item("bsc", "BSC_FOMO", "BSC_FOMO_MINT",
                   "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", 25, 12)]

    def fake_gt(path, _cfg, timeout=15):
        return {"data": bsc if "/bsc/" in path else sol}

    hunter = Hunter(cfg)
    before = time.time()
    with patch("fomo_trader.gt_get", side_effect=fake_gt):
        fomo, healthy = hunter._scan_gt()
    found = fomo + hunter.last_early
    check("SOL, BSC, and early GT signals carry scan timestamps",
          healthy and len(fomo) == 2 and len(hunter.last_early) == 1
          and all(before <= s["ts"] <= time.time() for s in found))

    pair = {"chainId": "solana", "pairAddress": "DS_POOL",
            "baseToken": {"address": "DS_MINT", "name": "DS"},
            "quoteToken": {"address": SOL_MINT},
            "priceChange": {"m5": 8}, "volume": {"m5": 3000},
            "txns": {"m5": {"buys": 20, "sells": 10}},
            "liquidity": {"usd": 9000}}
    ds = DexScreenerSource(cfg)._signal(pair)
    check("DexScreener fallback signal carries scan timestamp",
          ds is not None and before <= ds["ts"] <= time.time())


def test_signal_aged_bsc_pool():
    t, _ = _guard_trader()
    t.cfg["hunter"] = {"min_m15_gain_pct": 20,
                       "min_liquidity_usd": 8000,
                       "min_buy_sell_ratio": 1.5}
    sig = _aged_signal()
    sig["chain"] = "bsc"
    with patch("fomo_trader.gt_get", return_value=_gt_pool()) as gt:
        ok = t._entry_commit_ok(sig)
    check("aged BSC signal rechecks its BSC pool",
          ok and gt.call_args.args[0] == "/networks/bsc/pools/POOL123")


class FakeBsc:
    """Stub PancakeSwap client: no network, deterministic quotes."""
    def __init__(self, honeypot_ok=True):
        self.honeypot_ok = honeypot_ok
        self.sells = []

    def honeypot_check(self, token, name, bnb_wei):
        return (True, "round trip 99.5%") if self.honeypot_ok else \
               (False, "round trip only 1.2% (honeypot/tax)")

    def token_decimals(self, token):
        return 18

    def quote_buy(self, token, bnb_wei):
        return int(1_000_000 * 10 ** 18)  # 1M tokens for the stake

    def execute_sell(self, token, tokens_raw, slippage_bps=500, dry_run=True):
        self.sells.append((token, tokens_raw))
        return int(0.0095 * 1e18), "dryrun-bsc-test"


def _bsc_trader(honeypot_ok=True):
    t, tmp = make_trader()
    t.cfg["hunter"] = {"entry": {}}
    t.cfg["risk"]["buy_bnb_per_trade"] = 0.009
    t.bscswap = lambda: FakeBsc(honeypot_ok=honeypot_ok)
    t.manage = lambda mint: None  # don't spawn the real manage loop
    t.price_bnb = lambda mint: 9e-9
    t.bnb_usd = lambda: 780.0  # entry journal must stay offline
    return t, tmp


def _bsc_signal():
    return {"mint": "0xB5C0000000000000000000000000000000000001",
            "name": "BSCTEST", "chain": "bsc", "m15_gain_pct": 42.0,
            "buy_sell_ratio": 2.0, "liquidity_usd": 50000,
            "source": "geckoterminal", "pool_url": "http://x",
            "window_label": "15m", "ts": time.time()}


def test_enter_bsc_paper():
    t, _ = _bsc_trader()
    t.enter_bsc(_bsc_signal())
    mint = "0xB5C0000000000000000000000000000000000001"
    pos = t.state["positions"].get(mint)
    check("BSC paper entry opens a position", pos is not None)
    if pos:
        check("BSC position is tagged chain=bsc",
              pos.get("chain") == "bsc")
        check("BSC entry price in BNB/token",
              abs(pos["entry"] - 9e-9) < 1e-12, "entry=%s" % pos["entry"])
    check("BSC entry counts toward hourly cap",
          len(t.state["trades_this_hour"]) == 1)
    check("BSC entry counts toward daily cap",
          len(t.state["trades_today"]) == 1)


def test_enter_bsc_honeypot_blocked():
    t, _ = _bsc_trader(honeypot_ok=False)
    t.enter_bsc(_bsc_signal())
    check("honeypot BSC token is not entered",
          len(t.state["positions"]) == 0)
    check("blocked honeypot does not consume cap",
          t.state["trades_today"] == [])


def test_bsc_close_usd_accounting():
    t, _ = _bsc_trader()
    feed = PriceFeed(9e-9)
    wire(t, feed)
    mint = "0xB5C0000000000000000000000000000000000002"
    t.state["positions"][mint] = {
        "name": "TEST/BSC2", "chain": "bsc", "entry": 9e-9,
        "peak": 9e-9, "buy_sol": 0.009, "tokens_raw": 0,
        "sold_sol": 0.010, "decimals": 18,
        "opened_at": time.time() - 60, "rungs_fired": [],
    }
    realized = t.close_trade(mint, t.state["positions"][mint],
                             9e-9, "test-bsc-close")
    check("BSC realized in BNB", abs(realized - 0.001) < 1e-9,
          "realized=%s" % realized)
    check("realized_bnb updated",
          abs(t.state.get("realized_bnb", 0) - 0.001) < 1e-9)
    check("realized_usd updated",
          abs(t.state.get("realized_usd", 0) - 0.78) < 1e-9,
          "usd=%s" % t.state.get("realized_usd"))
    check("SOL totals untouched by BSC close",
          t.state.get("realized_sol", 0) == 0.0)


def test_usd_kill_switch_blocks_entries():
    t, sig = _guard_trader()
    t.cfg["risk"]["kill_switch_max_daily_loss_usd"] = 23.0
    t.state["realized_usd"] = -25.0
    check("USD kill switch blocks entries at -$25",
          t.guardrails_ok(sig) is False)
    t.state["realized_usd"] = -5.0
    check("entries allowed at -$5 USD",
          t.guardrails_ok(sig) is True)


def test_sol_limit_still_applies_with_usd_limit():
    t, sig = _guard_trader()
    t.cfg["risk"]["kill_switch_max_daily_loss_usd"] = 23.0
    t.state["realized_usd"] = -5.0
    t.state["realized_sol"] = -0.2
    check("SOL loss cap blocks entry even below USD loss cap",
          t.guardrails_ok(sig) is False)
    t.state["realized_sol"] = -0.1
    check("both loss caps below limit permit entry",
          t.guardrails_ok(sig) is True)


def test_protect_mode_usd_exit_and_control():
    for realized, expect_close in ((-23.0, True), (-22.99, False)):
        t, _ = make_trader()
        t.cfg["risk"]["kill_switch_max_daily_loss_usd"] = 23.0
        t.state["realized_usd"] = realized
        mint = "PM1111111111111111111111111111111111111111"
        peak = ENTRY * 1.20
        feed = PriceFeed(peak * 0.89)  # -11% from peak, normal 25% holds
        wire(t, feed)
        add_position(t, mint, peak=peak)
        closed = t._manage_once(mint, *manage_params(t))
        check("USD cap %s: 11%% drawdown %s" %
              ("tripped" if expect_close else "clear",
               "closes" if expect_close else "stays open"),
              closed is expect_close and
              (mint not in t.state["positions"]) is expect_close)
        if expect_close:
            check("protect exit records a trailing stop",
                  len(feed.swaps) == 1 and "trail-10%" in feed.swaps[0][0])
            check("protect exit keeps paper trading", t.dry_run is True)
        else:
            check("no-trip position never enters protect mode",
                  not t.state["positions"][mint].get("protect_mode"))


def test_protect_mode_tp_and_rollover():
    t, tmp = make_trader()
    t.cfg["risk"]["kill_switch_max_daily_loss_usd"] = 23.0
    t.state["realized_usd"] = -23.0
    mint = "PT1111111111111111111111111111111111111111"
    peak = ENTRY * 1.40
    feed = PriceFeed(peak * 0.94)  # below protected and post-TP trails
    wire(t, feed)
    add_position(t, mint, peak=peak, rungs=[[0, 50.0, 0.03]],
                 tokens_raw=TOKENS // 2, sold_sol=0.03)
    check("kill-switch helper is a pure read",
          t._kill_switch_tripped() is True and
          not t.state["positions"][mint].get("protect_mode"))
    t._manage_once(mint, *manage_params(t))
    with open(os.path.join(tmp, "state.json")) as f:
        saved = json.load(f)
    check("protect mode persists while position remains open",
          t.state["positions"][mint]["protect_mode"] is True and
          saved["positions"][mint]["protect_mode"] is True)
    t.state["day"] = "2000-01-01"
    t._manage_once(mint, *manage_params(t))
    check("day rollover leaves protect mode active",
          t.state["realized_usd"] == 0.0 and
          t.state["positions"][mint]["protect_mode"] is True)
    feed.px = peak * 0.89  # -11%: closes at 10%, but not at post-TP 12%
    closed = t._manage_once(mint, *manage_params(t))
    check("protected post-TP runner uses 10% rather than 12% trail",
          closed and "trail-10%" in feed.swaps[-1][0])


def test_protect_mode_sol_and_moonbag():
    t, _ = make_trader()
    t.cfg["risk"]["kill_switch_max_daily_loss_usd"] = 23.0
    t.state["realized_usd"] = -5.0
    t.state["realized_sol"] = -0.2
    mint = "PS1111111111111111111111111111111111111111"
    peak = ENTRY * 1.40
    feed = PriceFeed(peak * 0.89)
    wire(t, feed)
    add_position(t, mint, peak=peak)
    check("SOL cap trips helper and entry guard with USD cap configured",
          t._kill_switch_tripped() is True and
          t.guardrails_ok({"mint": "other"}) is False)
    closed = t._manage_once(mint, [[100, 50]], 50, 35)
    check("moonbag trail tightens to 10% on SOL cap",
          closed and "trail-10%" in feed.swaps[-1][0])
    t.state["realized_sol"] = -0.1
    check("below both caps, helper and entry guard stay clear",
          t._kill_switch_tripped() is False and
          t.guardrails_ok({"mint": "other"}) is True)


def test_protect_trail_never_loosens_and_invalid_falls_back():
    for configured, normal, expected in ((20, 8, "trail-8%"),
                                         (0, 25, "trail-10%"),
                                         (100, 25, "trail-10%"),
                                         ("bad", 25, "trail-10%")):
        t, _ = make_trader()
        t.cfg["risk"]["kill_switch_protect_trail_pct"] = configured
        t.state["realized_sol"] = -0.2
        mint = "PC1111111111111111111111111111111111111111"
        peak = ENTRY * 1.20
        feed = PriceFeed(peak * 0.89)
        wire(t, feed)
        add_position(t, mint, peak=peak)
        closed = t._manage_once(mint, [], normal, 35)
        check("protect trail %r keeps effective %s" %
              (configured, expected),
              closed and expected in feed.swaps[-1][0])


def test_rug_guard_requires_verifiable_holder_data():
    t, _ = make_trader()
    t.cfg["hunter"] = {"rug_guard": {}}
    mint = "RG1111111111111111111111111111111111111111"
    account = base64.b64encode(bytes(82)).decode("ascii")
    supply = 1000
    holders = [{"amount": "100"}, {"amount": "100"}]

    def rpc_call(method, args, **kwargs):
        if method == "getAccountInfo":
            return {"result": {"value": {"data": [account, "base64"]}}}
        if method == "getTokenSupply":
            return {"result": {"value": {"amount": str(supply)}}}
        return {"result": {"value": holders}}

    t.rpc.call = rpc_call
    check("valid mint and holder data passes rug guard",
          t.rug_check(mint, "TEST") is True)
    supply = 0
    check("zero supply fails rug guard", t.rug_check(mint, "TEST") is False)
    supply = 1000
    holders = []
    check("empty holder list fails rug guard",
          t.rug_check(mint, "TEST") is False)
    account = base64.b64encode(bytes(20)).decode("ascii")
    check("truncated mint account fails rug guard",
          t.rug_check(mint, "TEST") is False)


def test_halt_flag_interrupts_wait():
    t, tmp = make_trader()
    t.cfg["hunter"] = {"poll_sec": 5}
    haltfile = os.path.join(tmp, "halt.flag")
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        with open(haltfile, "w"):
            pass

    with patch("fomo_trader.Hunter") as hunter, \
         patch("fomo_trader.time.sleep", side_effect=fake_sleep):
        hunter.return_value.scan.return_value = []
        hunter.return_value.last_early = []
        try:
            t.run()
        except SystemExit as e:
            code = e.code
        else:
            code = None
    check("mid-wait halt exits 42", code == 42, "exit=%s" % code)
    check("mid-wait halt stops after one half-second sleep",
          sleeps == [0.5], "sleeps=%s" % sleeps)
    check("halt exit releases test pidfile",
          not os.path.exists(os.path.join(tmp, "bot.pid")))


def test_startup_halt_still_exits_42():
    t, tmp = make_trader()
    t.cfg["hunter"] = {"poll_sec": 5}
    with open(os.path.join(tmp, "halt.flag"), "w"):
        pass
    with patch("fomo_trader.Hunter") as hunter:
        try:
            t.run()
        except SystemExit as e:
            code = e.code
        else:
            code = None
        check("startup halt exits 42", code == 42)
        check("startup halt never constructs hunter", not hunter.called)
        check("startup halt does not create pidfile",
              not os.path.exists(os.path.join(tmp, "bot.pid")))


def test_sell_100pct_huge_raw_leaves_no_dust():
    # float division lost precision on 18-decimal raw amounts, so a
    # "sell 100%" left dust behind and close_trade never fired.
    t, _ = make_trader()
    feed = PriceFeed(ENTRY)
    wire(t, feed)
    mint = "D11111111111111111111111111111111111111111"
    add_position(t, mint, tokens_raw=10 ** 24, sold_sol=0.0)  # 1M tokens, 18dp
    sig, proceeds = t.sell_pct_of_balance(mint, 100, "test-dust")
    check("sell 100% succeeds", sig is not None)
    check("no dust left after selling 100% of a huge raw balance",
          t.token_balance_raw(mint) == 0,
          "balance=%s" % t.token_balance_raw(mint))


def test_venue_dump_fires():
    # venue tape says -45% in 5m while our quote path still shows +10%:
    # the tripwire must exit even though no local exit would fire.
    t, _ = make_trader()
    feed = PriceFeed(ENTRY * 1.10)
    wire(t, feed)
    t.venue_m5_dump = lambda mint, chain="solana": -45.0
    mint = "VN1111111111111111111111111111111111111111"
    add_position(t, mint, peak=ENTRY * 1.10)
    closed = t._manage_once(mint, *manage_params(t))
    check("venue dump tripwire closes on m5 -45%", closed and
          mint not in t.state["positions"])


def test_venue_dump_quiet_and_failsafe():
    for m5, label in ((-5.0, "mild m5 -5%"), (None, "API failure")):
        t, _ = make_trader()
        feed = PriceFeed(ENTRY * 1.10)
        wire(t, feed)
        t.venue_m5_dump = lambda mint, chain="solana", v=m5: v
        mint = "VQ1111111111111111111111111111111111111111"
        add_position(t, mint, peak=ENTRY * 1.10)
        closed = t._manage_once(mint, *manage_params(t))
        check("venue tripwire quiet on %s" % label, not closed and
              mint in t.state["positions"])


def test_venue_dump_throttled():
    t, _ = make_trader()
    feed = PriceFeed(ENTRY * 1.10)
    wire(t, feed)
    calls = []

    def fake_venue(mint, chain="solana"):
        calls.append(mint)
        return -5.0
    t.venue_m5_dump = fake_venue
    mint = "VT1111111111111111111111111111111111111111"
    add_position(t, mint, peak=ENTRY * 1.10)
    t._manage_once(mint, *manage_params(t))
    t._manage_once(mint, *manage_params(t))
    check("venue check throttled to one API hit per 30s", len(calls) == 1,
          "calls=%d" % len(calls))


def test_trail_tightens_after_tp():
    # runner is up huge; a 13% pullback from peak must exit it now that a
    # TP has fired (base 25% trail would have held on).
    t, _ = make_trader()
    peak = ENTRY * 3.0
    feed = PriceFeed(peak * 0.87)    # -13% from peak, still +161% on entry
    wire(t, feed)
    mint = "TT1111111111111111111111111111111111111111"
    add_position(t, mint, entry=ENTRY, peak=peak, rungs=[[0, 50.0, 0.03]],
                 tokens_raw=TOKENS // 2, sold_sol=0.03)
    closed = t._manage_once(mint, *manage_params(t))
    check("tightened trail exits the runner after TP", closed and
          mint not in t.state["positions"])


def test_dump_detector():
    t, _ = make_trader()
    high = ENTRY * 2.0
    feed = PriceFeed(high * 0.78)     # -22% from the 60s window max
    wire(t, feed)
    mint = "DU1111111111111111111111111111111111111111"
    add_position(t, mint, entry=ENTRY, peak=high)
    now = time.time()
    t._px_hist[mint] = deque([(now - 30, high), (now - 5, high)], maxlen=300)
    t._manage_once(mint, *manage_params(t))
    check("dump detector closes position", mint not in t.state["positions"])


def test_trailing_stop():
    t, _ = make_trader()
    feed = PriceFeed(ENTRY * 1.02)    # +2% now, peak was +40%
    wire(t, feed)
    mint = "TR1111111111111111111111111111111111111111"
    add_position(t, mint, entry=ENTRY, peak=ENTRY * 1.40)
    t._manage_once(mint, *manage_params(t))
    check("trailing stop closes position", mint not in t.state["positions"])


def test_hard_stop():
    t, _ = make_trader()
    feed = PriceFeed(ENTRY * 0.64)    # -36% from entry
    wire(t, feed)
    mint = "HA1111111111111111111111111111111111111111"
    add_position(t, mint, entry=ENTRY, peak=ENTRY)
    # wide trail so only the hard stop can fire
    tps, _, _ = manage_params(t)
    t._manage_once(mint, tps, 50, 35)
    check("hard stop closes position", mint not in t.state["positions"])


def test_stale_exit():
    t, _ = make_trader()
    feed = PriceFeed(ENTRY * 1.02)    # +2% after 40 min -> thesis dead
    wire(t, feed)
    mint = "ST1111111111111111111111111111111111111111"
    add_position(t, mint, entry=ENTRY, peak=ENTRY * 1.02, age_min=40)
    t._manage_once(mint, *manage_params(t))
    check("stale exit closes position", mint not in t.state["positions"])


def test_close_trade_3element_rungs():
    t, _ = make_trader()
    feed = PriceFeed(ENTRY * 0.50)
    wire(t, feed)
    mint = "C31111111111111111111111111111111111111111"
    # exact (virtual-ledger) path: 3-element rungs + dust-free balance
    add_position(t, mint, tokens_raw=0, sold_sol=0.025,
                 rungs=[[0, 58.33, 0.0174], [1, 140.2, 0.0087]])
    try:
        realized = t.close_trade(mint, t.state["positions"][mint],
                                 ENTRY * 0.50, "test-3elem")
        ok = True
    except Exception as e:  # noqa: BLE001
        realized, ok = 0.0, False
        print("close_trade raised: %r" % e)
    check("close_trade handles 3-element rungs (no ValueError)", ok)
    check("close_trade exact realized = sold - buy",
          abs(realized - (0.025 - BUY_SOL)) < 1e-9,
          "realized=%s" % realized)
    # legacy path: no virtual ledger, 2-element + bare-int rungs
    mint2 = "C21111111111111111111111111111111111111111"
    t.state["positions"][mint2] = {
        "name": "TEST/legacy", "entry": ENTRY, "peak": ENTRY * 1.6,
        "buy_sol": BUY_SOL, "rungs_fired": [[0, 60.0], 1],
        "opened_at": time.time(),
    }
    try:
        t.close_trade(mint2, t.state["positions"][mint2],
                      ENTRY * 0.80, "test-legacy")
        ok2 = True
    except Exception as e:  # noqa: BLE001
        ok2 = False
        print("close_trade legacy raised: %r" % e)
    check("close_trade handles legacy 2-element/bare rungs", ok2)


def main():
    with patch("requests.sessions.Session.request",
               side_effect=AssertionError("network forbidden in tests")) as net:
        for fn in (test_tp_rung_fire, test_tp_retry_after_failure,
                   test_trail_tightens_after_tp, test_venue_dump_fires,
                   test_venue_dump_quiet_and_failsafe, test_venue_dump_throttled,
                   test_pidfile_release_keeps_newer_bot,
                   test_hourly_cap_blocks_fourth_trade,
                   test_hourly_window_resets_itself,
                   test_hourly_cap_allows_under_limit,
                   test_commit_kill_switch_blocks_midflight_entry,
                   test_commit_caps_still_block, test_commit_clear_allows,
                   test_signal_fresh_zero_http, test_signal_aged_strong,
                   test_signal_aged_faded_gain,
                   test_signal_aged_drained_liquidity,
                   test_signal_aged_refetch_failure,
                   test_signal_max_age_fallback,
                   test_signal_aged_ratio_and_early_bar,
                   test_signal_aged_dexscreener,
                   test_scanners_stamp_all_signal_paths,
                   test_signal_aged_bsc_pool,
                   test_enter_bsc_paper, test_enter_bsc_honeypot_blocked,
                   test_bsc_close_usd_accounting,
                   test_usd_kill_switch_blocks_entries,
                   test_sol_limit_still_applies_with_usd_limit,
                   test_protect_mode_usd_exit_and_control,
                   test_protect_mode_tp_and_rollover,
                   test_protect_mode_sol_and_moonbag,
                   test_protect_trail_never_loosens_and_invalid_falls_back,
                   test_rug_guard_requires_verifiable_holder_data,
                   test_halt_flag_interrupts_wait,
                   test_startup_halt_still_exits_42,
                   test_sell_100pct_huge_raw_leaves_no_dust,
                   test_dump_detector, test_trailing_stop, test_hard_stop,
                   test_stale_exit, test_close_trade_3element_rungs):
            try:
                fn()
            except Exception:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                FAILED.append(fn.__name__ + " (exception)")
        check("test suite made no HTTP requests", net.call_count == 0,
              "HTTP attempts=%d" % net.call_count)
    print("\n%d passed, %d failed" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("FAILED:", FAILED)
        sys.exit(1)


if __name__ == "__main__":
    main()
