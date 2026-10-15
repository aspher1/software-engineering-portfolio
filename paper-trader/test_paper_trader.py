#!/usr/bin/env python3
"""Unit tests for paper_trader.py (Worker C: P1 instrumentation + tests).

PAPER ONLY - no brokerage, no real money, no network access.

Run:  cd ~/workspace/paper-trader && python3 test_paper_trader.py

All market-data entry points (paper_trader.current_price /
crypto_momentum / stock_quote) are monkeypatched, and the module path
globals (STATE_PATH, JOURNAL_PATH, LOG_PATH, PID_PATH) are redirected to a
per-test temp dir, so the real state.json / trades.jsonl / bot.log /
bot.pid are never touched.

Coverage (a)-(n) per the work order, plus (o) for log rotation:
  (a) enter() sets peak to the raw signal price, not the slipped fill
  (b) TP exit fires at +6% on crypto
  (c) SL exit fires at -4%
  (d) trailing stop fires on a pullback with the retuned (tight) trail
  (e) max-hold exits a position older than 48h
  (f) drawdown kill switch blocks entries at >= 12% drawdown
  (g) entries resume once drawdown recovers below the 10% resume band
  (h) 24h cooldown blocks re-entry of a symbol via the journal
  (i) analyze.stats() profit factor on synthetic closes
  (j) state round-trip preserves peak_equity / max_drawdown_pct / scan_stats
  (k) corrupt state.json resets to defaults with a loud WARNING in the log
  (l) check_single_instance(): dead pid proceeds, live paper_trader pid
      refuses, missing pidfile proceeds
  (m) equity() / unrealized_pnl() / drawdown_pct() math on synthetic positions
  (n) scan_stats date rollover resets counters and logs the old day
  (o) rotate_log() keeps 3 backups at ~5MB and drops the oldest
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta

import paper_trader as pt
import analyze

# setUp monkeypatches these module globals; keep the real ones for tests
# of the data layer itself (which still never touches the network: the
# sources in pt.CRYPTO_SOURCES are replaced with fakes).
REAL_CRYPTO_MOMENTUM = pt.crypto_momentum
REAL_CURRENT_PRICE = pt.current_price


def today_str():
    return datetime.now(pt.ET).strftime("%Y-%m-%d")


def make_cfg(**over):
    cfg = {
        "bankroll": 1000.0,
        "trade_size": 100.0,
        "max_positions": 5,
        "max_trades_day": 10,
        "cooldown_hours": 24,
        "max_hold_hours": 48,
        "drawdown_kill_pct": 12.0,
        "drawdown_resume_pct": 10.0,
        "scan_interval_sec": 300,
        "manage_interval_sec": 60,
        # trail_pct retuned tight (inside the TP/SL band) so the trailing
        # stop can actually engage - mirrors the worker-B retune note.
        "crypto": {"symbols": ["BTC", "ETH", "SOL", "DOGE"],
                   "entry_1h_pct": 2.5, "entry_4h_min_pct": 0.0,
                   "tp_pct": 6.0, "sl_pct": -4.0, "trail_pct": 3.0,
                   "slippage_pct": 0.6},
        "stocks": {"symbols": ["NVDA"],
                   "entry_day_pct": 4.0,
                   "tp_pct": 8.0, "sl_pct": -5.0, "trail_pct": 3.0,
                   "slippage_pct": 0.1},
    }
    cfg.update(over)
    return cfg


class PaperTraderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pt_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # redirect all on-disk paths away from the real run dir
        pt.STATE_PATH = os.path.join(self.tmp, "state.json")
        pt.JOURNAL_PATH = os.path.join(self.tmp, "trades.jsonl")
        pt.LOG_PATH = os.path.join(self.tmp, "bot.log")
        pt.PID_PATH = os.path.join(self.tmp, "bot.pid")
        # no network: script all price reads
        self.prices = {}
        pt.current_price = lambda sleeve, sym: self.prices.get(sym)
        pt.crypto_momentum = lambda sym: (self.prices.get(sym), 0.0, 0.0)
        pt.stock_quote = lambda sym: (self.prices.get(sym), 0.0)
        self.cfg = make_cfg()

    # -- helpers -------------------------------------------------------
    def fresh_state(self):
        return pt.default_state(self.cfg)

    def enter_btc(self, state, price=100.0):
        return pt.enter(self.cfg, state, "crypto", "BTC",
                        price, 3.0, "test")

    def last_journal(self):
        with open(pt.JOURNAL_PATH) as f:
            lines = [ln for ln in f if ln.strip()]
        return json.loads(lines[-1])

    def log_text(self):
        with open(pt.LOG_PATH) as f:
            return f.read()

    # -- (a) peak is the raw signal price --------------------------------
    def test_a_enter_peak_is_raw_price_not_slipped_fill(self):
        st = self.fresh_state()
        self.assertTrue(self.enter_btc(st, price=100.0))
        p = st["positions"]["BTC"]
        self.assertEqual(p["peak"], 100.0)            # raw signal price
        self.assertAlmostEqual(p["entry"], 100.6)     # fill pays 0.6% slip
        self.assertAlmostEqual(p["qty"], 100.0 / 100.6)

    # -- (b) take profit at +6% crypto ------------------------------------
    def test_b_take_profit_fires_at_plus6_crypto(self):
        st = self.fresh_state()
        self.assertTrue(self.enter_btc(st, price=100.0))
        self.prices["BTC"] = 107.0   # +6.4% over the slipped fill 100.6
        self.assertTrue(pt.manage(self.cfg, st))
        self.assertNotIn("BTC", st["positions"])
        rec = self.last_journal()
        self.assertEqual(rec["type"], "close")
        self.assertIn("take profit", rec["reason"])
        self.assertGreater(rec["pnl"], 0)

    # -- (c) stop loss at -4% --------------------------------------------
    def test_c_stop_loss_fires_at_minus4(self):
        st = self.fresh_state()
        self.assertTrue(self.enter_btc(st, price=100.0))
        self.prices["BTC"] = 96.0    # -4.6% vs fill -> SL
        pt.manage(self.cfg, st)
        self.assertNotIn("BTC", st["positions"])
        rec = self.last_journal()
        self.assertIn("stop loss", rec["reason"])
        self.assertLess(rec["pnl"], 0)

    # -- (d) trailing stop on pullback ------------------------------------
    def test_d_trailing_stop_fires_on_pullback(self):
        st = self.fresh_state()      # trail_pct = 3.0 (retuned)
        self.assertTrue(self.enter_btc(st, price=100.0))
        self.prices["BTC"] = 103.0
        pt.manage(self.cfg, st)      # peak -> 103, no exit (+2.4% < TP 6%)
        self.assertIn("BTC", st["positions"])
        self.assertEqual(st["positions"]["BTC"]["peak"], 103.0)
        self.prices["BTC"] = 99.5    # -3.4% from peak, only -1.1% vs entry
        pt.manage(self.cfg, st)
        self.assertNotIn("BTC", st["positions"])
        self.assertIn("trailing stop", self.last_journal()["reason"])

    # -- (e) max hold 48h --------------------------------------------------
    def test_e_max_hold_exits_after_48h(self):
        st = self.fresh_state()
        self.assertTrue(self.enter_btc(st, price=100.0))
        st["positions"]["BTC"]["opened_at"] = (
            datetime.now(pt.ET) - timedelta(hours=49)).isoformat()
        self.prices["BTC"] = 100.0   # flat: no TP/SL/trail would fire
        pt.manage(self.cfg, st)
        self.assertNotIn("BTC", st["positions"])
        self.assertIn("max hold", self.last_journal()["reason"])

    # -- (f) drawdown kill switch blocks entries ---------------------------
    def test_f_drawdown_kill_blocks_entries_at_12pct(self):
        st = self.fresh_state()
        st["drawdown_pct"] = 12.0     # at the kill threshold
        self.assertFalse(self.enter_btc(st, price=100.0))
        self.assertNotIn("BTC", st["positions"])
        self.assertTrue(st["entries_halted"])
        self.assertIn("DRAWDOWN KILL SWITCH", self.log_text())

    # -- (g) entries resume below the resume band ---------------------------
    def test_g_entries_resume_below_10pct_drawdown(self):
        st = self.fresh_state()
        st["entries_halted"] = True
        st["drawdown_pct"] = 12.0
        st["peak_equity"] = 1000.0
        st["bankroll"] = 950.0        # live dd = 5% < resume band 10%
        self.prices["BTC"] = 100.0
        pt.manage(self.cfg, st)       # bookkeeping clears the halt
        self.assertFalse(st["entries_halted"])
        self.assertIn("entries resumed", self.log_text())
        self.assertTrue(self.enter_btc(st, price=100.0))
        self.assertIn("BTC", st["positions"])

    # -- (h) 24h cooldown via journal ----------------------------------------
    def test_h_cooldown_blocks_reentry_within_24h(self):
        st = self.fresh_state()
        self.assertTrue(self.enter_btc(st, price=100.0))
        self.prices["BTC"] = 96.0
        pt.manage(self.cfg, st)       # stopped out; journal has entry+close
        self.assertNotIn("BTC", st["positions"])
        st2 = self.fresh_state()      # fresh state, same journal on disk
        self.assertFalse(pt.enter(self.cfg, st2, "crypto", "BTC",
                                  100.0, 3.0, "test"))
        self.assertNotIn("BTC", st2["positions"])
        self.assertIn("cooldown", self.log_text())

    # -- (i) analyze.stats profit factor --------------------------------------
    def test_i_analyze_stats_profit_factor(self):
        closes = [{"pnl": 10.0}, {"pnl": -5.0},
                  {"pnl": 20.0}, {"pnl": -5.0}]
        s = analyze.stats(closes)
        self.assertEqual(s["n"], 4)
        self.assertAlmostEqual(s["profit_factor"], 3.0)   # 30 / 10
        self.assertAlmostEqual(s["win_rate"], 50.0)
        self.assertIsNone(analyze.stats([]))

    # -- (j) state round-trip ---------------------------------------------------
    def test_j_state_roundtrip_preserves_risk_fields(self):
        st = self.fresh_state()
        for f in ("peak_equity", "max_drawdown_pct", "drawdown_pct",
                  "entries_halted", "scan_stats"):
            self.assertIn(f, st)      # contract: default_state carries these
        st["peak_equity"] = 1010.5
        st["max_drawdown_pct"] = 3.25
        st["drawdown_pct"] = 1.5
        st["scan_stats"] = {"date": "2026-09-26", "scans": 7,
                            "signals": 2, "entered": 1}
        pt.save_state(st)
        st2 = pt.load_state(self.cfg)
        self.assertEqual(st2["peak_equity"], 1010.5)
        self.assertEqual(st2["max_drawdown_pct"], 3.25)
        self.assertEqual(st2["drawdown_pct"], 1.5)
        self.assertEqual(st2["scan_stats"],
                         {"date": "2026-09-26", "scans": 7,
                          "signals": 2, "entered": 1})

    # -- (k) corrupt state.json ---------------------------------------------------
    def test_k_corrupt_state_resets_with_loud_warning(self):
        with open(pt.STATE_PATH, "w") as f:
            f.write("{corrupt json!!!")
        st = pt.load_state(self.cfg)
        self.assertEqual(st["bankroll"], self.cfg["bankroll"])
        self.assertEqual(st["positions"], {})
        self.assertIn("WARNING: state.json corrupt or unreadable",
                      self.log_text())

    # -- (l) single instance ------------------------------------------------------
    def test_l_single_instance_dead_pid_proceeds(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        with open(pt.PID_PATH, "w") as f:
            f.write(str(proc.pid))    # stale pid, process is gone
        self.assertFalse(pt.check_single_instance())
        self.assertFalse(os.path.exists(pt.PID_PATH))  # stale file cleaned

    def test_l_single_instance_live_pid_refuses(self):
        # argv[0] shim: a live process whose cmdline contains paper_trader
        proc = subprocess.Popen(["bash", "-c",
                                 "exec -a paper_trader sleep 120"])
        try:
            time.sleep(0.3)
            self.assertIsNone(proc.poll())   # still alive
            with open(pt.PID_PATH, "w") as f:
                f.write(str(proc.pid))
            self.assertTrue(pt.check_single_instance())
            self.assertIn("another paper_trader instance", self.log_text())
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_l_single_instance_missing_pidfile_proceeds(self):
        self.assertFalse(os.path.exists(pt.PID_PATH))
        self.assertFalse(pt.check_single_instance())

    # -- (m) equity / unrealized / drawdown math ------------------------------------
    def test_m_equity_and_unrealized_math(self):
        st = self.fresh_state()
        st["bankroll"] = 900.0
        st["positions"]["BTC"] = {"sleeve": "crypto", "entry": 100.0,
                                  "qty": 1.0, "peak": 100.0,
                                  "invested": 100.0,
                                  "opened_at":
                                      datetime.now(pt.ET).isoformat(),
                                  "momentum_at_entry": 3.0}
        prices = {"BTC": 110.0}
        self.assertAlmostEqual(pt.unrealized_pnl(st, prices), 10.0)
        self.assertAlmostEqual(pt.equity(st, prices), 910.0)
        st["peak_equity"] = 1000.0
        self.assertAlmostEqual(pt.drawdown_pct(st, prices), 9.0)

    # -- (n) scan_stats date rollover -----------------------------------------------
    def test_n_scan_stats_date_rollover_resets_counters(self):
        st = self.fresh_state()
        st["scan_stats"] = {"date": "2026-09-25", "scans": 5,
                            "signals": 3, "entered": 2}
        st["scan_stats"]["crypto_skipped"] = 4
        ss = pt.roll_scan_stats(st)
        self.assertEqual(ss["date"], today_str())
        self.assertEqual(ss["scans"], 0)
        self.assertEqual(ss["signals"], 0)
        self.assertEqual(ss["entered"], 0)
        self.assertEqual(ss["crypto_skipped"], 0)
        self.assertIn("4 crypto skipped (no data)", self.log_text())
        self.assertIn("throughput 2026-09-25: 5 scans, 3 signals, 2 entered",
                      self.log_text())
        # same-day call must not reset
        st["scan_stats"]["scans"] = 4
        ss2 = pt.roll_scan_stats(st)
        self.assertEqual(ss2["date"], today_str())
        self.assertEqual(ss2["scans"], 4)

    # -- (o) log rotation --------------------------------------------------------------
    def test_o_rotate_log_keeps_three_backups(self):
        real_max = pt.LOG_MAX_BYTES
        pt.LOG_MAX_BYTES = 100
        self.addCleanup(setattr, pt, "LOG_MAX_BYTES", real_max)

        def write_log(n, ch):
            with open(pt.LOG_PATH, "w") as f:
                f.write(ch * n)

        for ch in "abcd":            # 4 rotations at >100 bytes each
            write_log(200, ch)
            pt.rotate_log()
        for i in (1, 2, 3):
            self.assertTrue(os.path.exists(pt.LOG_PATH + ".%d" % i),
                            "bot.log.%d missing" % i)
        self.assertFalse(os.path.exists(pt.LOG_PATH + ".4"))
        with open(pt.LOG_PATH + ".1") as f:
            self.assertEqual(f.read(), "d" * 200)   # newest backup
        with open(pt.LOG_PATH + ".3") as f:
            self.assertEqual(f.read(), "b" * 200)   # oldest kept; "a" dropped
        # under-threshold log is left alone
        write_log(50, "z")
        pt.rotate_log()
        self.assertTrue(os.path.exists(pt.LOG_PATH))
        with open(pt.LOG_PATH) as f:
            self.assertEqual(f.read(), "z" * 50)


def candles_ending_now(now, closes_by_min_ago):
    """{start_ts: close} 5m candles; closes_by_min_ago maps "this close is
    the price N minutes before the last completed candle end" -> close."""
    last_end = int(now) // pt.CANDLE_SEC * pt.CANDLE_SEC
    return {last_end - m * 60 - pt.CANDLE_SEC: c
            for m, c in closes_by_min_ago.items()}


def flat_candles(now, ref_1h, ref_4h, base=100.0):
    """5m candles covering 5h with explicit 60/240-min reference closes."""
    c = {m: base for m in range(0, 305, 5)}
    last_end = int(now) // pt.CANDLE_SEC * pt.CANDLE_SEC
    c = candles_ending_now(now, c)
    c[int(now - 3600) // pt.CANDLE_SEC * pt.CANDLE_SEC - pt.CANDLE_SEC] = ref_1h
    c[int(now - 14400) // pt.CANDLE_SEC * pt.CANDLE_SEC
      - pt.CANDLE_SEC] = ref_4h
    assert max(c) <= last_end
    return c


class CryptoDataTest(unittest.TestCase):
    """(p)-(w): trailing-window momentum, source fallback, fail-closed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pt_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        pt.STATE_PATH = os.path.join(self.tmp, "state.json")
        pt.JOURNAL_PATH = os.path.join(self.tmp, "trades.jsonl")
        pt.LOG_PATH = os.path.join(self.tmp, "bot.log")
        pt.PID_PATH = os.path.join(self.tmp, "bot.pid")
        real_sources = pt.CRYPTO_SOURCES
        self.addCleanup(setattr, pt, "CRYPTO_SOURCES", real_sources)
        real_mh = pt.market_hours
        self.addCleanup(setattr, pt, "market_hours", real_mh)
        pt.market_hours = lambda: False
        real_mom = pt.crypto_momentum
        self.addCleanup(setattr, pt, "crypto_momentum", real_mom)
        self.cfg = make_cfg()

    def log_text(self):
        if not os.path.exists(pt.LOG_PATH):
            return ""
        with open(pt.LOG_PATH) as f:
            return f.read()

    @staticmethod
    def source(spot=None, candles=None, exc=None):
        def s(sym):
            if exc:
                raise exc
            return spot

        def c(sym):
            if exc:
                raise exc
            return candles
        return s, c

    # -- (p) momentum is measured over trailing 60 / 240 minutes -------------
    def test_p_momentum_uses_trailing_windows(self):
        now = time.time()
        c = flat_candles(now, ref_1h=100.0, ref_4h=98.0)
        p1h, p4h = pt.momentum_from_candles(103.0, c, now)
        self.assertAlmostEqual(p1h, 3.0)
        self.assertAlmostEqual(p4h, (103.0 - 98.0) / 98.0 * 100)

    def test_p_momentum_fails_closed_on_bad_data(self):
        now = time.time()
        c = flat_candles(now, ref_1h=100.0, ref_4h=100.0)
        self.assertIsNone(pt.momentum_from_candles(None, c, now))
        self.assertIsNone(pt.momentum_from_candles(0.0, c, now))
        self.assertIsNone(pt.momentum_from_candles(100.0, {}, now))
        # stale: newest candle is 20 min older than "now"
        self.assertIsNone(pt.momentum_from_candles(100.0, c, now + 1200))
        # missing 1h reference candle
        ref1 = int(now - 3600) // pt.CANDLE_SEC * pt.CANDLE_SEC - pt.CANDLE_SEC
        gap = dict(c)
        del gap[ref1]
        self.assertIsNone(pt.momentum_from_candles(100.0, gap, now))

    # -- (q) primary source down -> fallback source is used -------------------
    def test_q_falls_back_to_second_source(self):
        now = time.time()
        good = flat_candles(now, ref_1h=100.0, ref_4h=100.0)
        pt.CRYPTO_SOURCES = [
            ("primary",) + self.source(exc=ConnectionError("proxy refused")),
            ("backup",) + self.source(spot=103.0, candles=good)]
        price, p1h, p4h = REAL_CRYPTO_MOMENTUM("BTC")
        self.assertEqual(price, 103.0)
        self.assertAlmostEqual(p1h, 3.0)
        self.assertIn("crypto data failed BTC via primary", self.log_text())

    # -- (r) stale primary data is not used; fallback takes over --------------
    def test_r_stale_primary_is_rejected(self):
        now = time.time()
        stale = flat_candles(now - 3600, ref_1h=50.0, ref_4h=50.0)
        good = flat_candles(now, ref_1h=100.0, ref_4h=100.0)
        pt.CRYPTO_SOURCES = [
            ("primary",) + self.source(spot=103.0, candles=stale),
            ("backup",) + self.source(spot=101.0, candles=good)]
        price, p1h, _ = REAL_CRYPTO_MOMENTUM("BTC")
        self.assertEqual(price, 101.0)
        self.assertAlmostEqual(p1h, 1.0)
        self.assertIn("stale/incomplete BTC via primary", self.log_text())

    # -- (s) every source down -> no data, never a price ----------------------
    def test_s_all_sources_down_fails_closed(self):
        pt.CRYPTO_SOURCES = [
            ("primary",) + self.source(exc=ConnectionError("down")),
            ("backup",) + self.source(exc=ValueError("bad json"))]
        self.assertEqual(REAL_CRYPTO_MOMENTUM("BTC"), (None, None, None))
        self.assertIn("no price source for BTC", self.log_text())

    # -- (t) blind crypto scan is skipped, counted, and enters nothing --------
    def test_t_blind_scan_skips_and_counts(self):
        pt.crypto_momentum = lambda sym: (None, None, None)
        st = pt.default_state(self.cfg)
        self.assertEqual(pt.scan(self.cfg, st), [])
        self.assertEqual(st["scan_stats"]["crypto_skipped"], 1)
        self.assertIn("crypto scan skipped", self.log_text())
        self.assertEqual(st["positions"], {})

    def test_t_partial_outage_still_scans_healthy_symbols(self):
        pt.crypto_momentum = lambda sym: ((100.0, 3.0, 1.0) if sym == "SOL"
                                          else (None, None, None))
        st = pt.default_state(self.cfg)
        sigs = pt.scan(self.cfg, st)
        self.assertEqual([s[1] for s in sigs], ["SOL"])
        self.assertEqual(st["scan_stats"]["crypto_skipped"], 0)

    def test_t_threshold_unchanged_below_2_5_no_signal(self):
        pt.crypto_momentum = lambda sym: (100.0, 2.4, 1.0)
        st = pt.default_state(self.cfg)
        self.assertEqual(pt.scan(self.cfg, st), [])

    # -- (u) exits use spot with fallback, and None when all sources fail -----
    def test_u_current_price_crypto_uses_spot_fallback(self):
        pt.CRYPTO_SOURCES = [
            ("primary",) + self.source(exc=ConnectionError("down")),
            ("backup",) + self.source(spot=99.0, candles={})]
        self.assertEqual(REAL_CURRENT_PRICE("crypto", "BTC"), 99.0)
        pt.CRYPTO_SOURCES = [
            ("primary",) + self.source(exc=ConnectionError("down"))]
        self.assertIsNone(REAL_CURRENT_PRICE("crypto", "BTC"))

    # -- (v) kraken pair map covers the configured crypto symbols -------------
    def test_v_kraken_pairs_cover_config_symbols(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "config.json")) as f:
            cfg = json.load(f)
        for sym in cfg["crypto"]["symbols"]:
            self.assertIn(sym, pt.KRAKEN_PAIRS)

    # -- (w) analyze reads the keys paper_trader actually writes --------------
    def test_w_analyze_throughput_reads_bot_keys(self):
        lines = analyze.throughput_lines(
            {"date": "2026-09-27", "scans": 10, "signals": 3,
             "entered": 2, "crypto_skipped": 0})
        self.assertIn("signals found 3 | signals entered 2", lines[0])
        self.assertIn("throughput looks normal", lines[1])
        lines = analyze.throughput_lines(
            {"date": "2026-09-27", "scans": 30, "signals": 0,
             "entered": 0, "crypto_skipped": 20})
        self.assertIn("data feed failing on 20/30", lines[1])


# -- (x) AI entry gate: approve/veto parsing, fail-closed behavior --------
class AIGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gate_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        import ai_gate as ag
        self.ag = ag
        ag.JOURNAL = os.path.join(self.tmp, "trades.jsonl")
        self.real_run = ag._run_cmd
        ag._run_cmd = self.fake_run
        self.addCleanup(setattr, ag, "_run_cmd", self.real_run)
        self.fake_output = ("DECISION: APPROVE\nCONFIDENCE: 72\n"
                            "REASON: steady climb on rising volume\n")
        self.calls = 0
        import sys
        self.cfg = make_cfg(ai_gate={"enabled": True,
                                     "backends": ["cursor"],
                                     "cursor_bin": [sys.executable],
                                     "cursor_args": ["-p"],
                                     "timeout_sec": 120,
                                     "max_calls_per_day": 20})

    def fake_run(self, argv, timeout, stdin_text, cwd=None):
        self.calls += 1
        prompt = stdin_text if stdin_text is not None else argv[-1]
        assert prompt.startswith("You are the entry screener")
        if isinstance(self.fake_output, Exception):
            raise self.fake_output
        class R:
            pass
        r = R()
        r.stdout, r.stderr, r.returncode = self.fake_output, "", 0
        return r

    def gate(self, **over):
        g = dict(self.cfg["ai_gate"])
        g.update(over)
        return self.ag.AIGate(g)

    def fresh_state(self):
        return pt.default_state(self.cfg)

    def test_x_approve_parses(self):
        ok, info = self.gate().evaluate(
            self.cfg, self.fresh_state(), "crypto", "BTC",
            100.0, 3.1, "+3.1%/1h", 1.2)
        self.assertTrue(ok)
        self.assertEqual(info["decision"], "APPROVE")
        self.assertEqual(info["confidence"], 72)
        self.assertIn("rising volume", info["reason"])

    def test_x_veto_parses_and_journals(self):
        self.fake_output = ("DECISION: VETO\nCONFIDENCE: 81\n"
                            "REASON: vertical spike, no base\n")
        ok, info = self.gate().evaluate(
            self.cfg, self.fresh_state(), "crypto", "SOL",
            50.0, 4.0, "+4.0%/1h", 0.5)
        self.assertFalse(ok)
        self.assertIsNone(info)
        with open(self.ag.JOURNAL) as f:
            recs = [json.loads(l) for l in f]
        self.assertEqual(recs[-1]["type"], "ai_veto")
        self.assertEqual(recs[-1]["symbol"], "SOL")
        self.assertIn("no base", recs[-1]["ai_reason"])

    def test_x_unparseable_output_vetoes(self):
        self.fake_output = "looks good to me, send it"
        ok, info = self.gate().evaluate(
            self.cfg, self.fresh_state(), "crypto", "BTC",
            100.0, 3.1, "+3.1%/1h", 1.2)
        self.assertFalse(ok)

    def test_x_timeout_vetoes(self):
        self.fake_output = subprocess.TimeoutExpired("codex", 120)
        ok, info = self.gate().evaluate(
            self.cfg, self.fresh_state(), "crypto", "BTC",
            100.0, 3.1, "+3.1%/1h", 1.2)
        self.assertFalse(ok)

    def test_x_missing_backend_vetoes_without_call(self):
        ok, info = self.gate(backends=["nope"]).evaluate(
            self.cfg, self.fresh_state(), "crypto", "BTC",
            100.0, 3.1, "+3.1%/1h", 1.2)
        self.assertFalse(ok)
        self.assertEqual(self.calls, 0)

    def test_x_daily_cap_vetoes_without_call(self):
        g = self.gate(max_calls_per_day=1)
        st = self.fresh_state()
        ok, _ = g.evaluate(self.cfg, st, "crypto", "BTC",
                           100.0, 3.1, "+3.1%/1h", 1.2)
        self.assertTrue(ok)
        self.assertEqual(self.calls, 1)
        ok, _ = g.evaluate(self.cfg, st, "crypto", "ETH",
                           50.0, 3.0, "+3.0%/1h", 0.8)
        self.assertFalse(ok)
        self.assertEqual(self.calls, 1)  # cap hit: no second model call

    def test_x_disabled_gate_passes_through(self):
        ok, info = self.gate(enabled=False).evaluate(
            self.cfg, self.fresh_state(), "crypto", "BTC",
            100.0, 3.1, "+3.1%/1h", 1.2)
        self.assertTrue(ok)
        self.assertIsNone(info)
        self.assertEqual(self.calls, 0)

    def test_x_quota_exhaustion_vetoes_and_counts(self):
        self.fake_output = ("ERROR: You've hit your usage limit. "
                            "try again at 1:07 PM.\n")
        st = self.fresh_state()
        ok, info = self.gate().evaluate(
            self.cfg, st, "crypto", "BTC", 100.0, 3.1, "+3.1%/1h", 1.2)
        self.assertFalse(ok)
        self.assertEqual(st["ai_gate"]["quota_vetoes"], 1)
        self.assertEqual(st["ai_gate"].get("vetoes", 0), 0)
        with open(self.ag.JOURNAL) as f:
            recs = [json.loads(l) for l in f]
        self.assertIn("quota-exhausted", recs[-1]["ai_reason"])

    def test_x_chain_falls_back_to_codex(self):
        import sys
        g = self.gate(backends=["cursor", "codex"],
                      cursor_bin=["/nonexistent/agent"],
                      codex_bin=[sys.executable])
        ok, info = g.evaluate(self.cfg, self.fresh_state(), "crypto",
                              "BTC", 100.0, 3.1, "+3.1%/1h", 1.2)
        self.assertTrue(ok)
        self.assertEqual(info["backend"], "codex")
        self.assertEqual(self.calls, 1)  # cursor skipped, codex called

    def test_x_chain_records_deciding_backend(self):
        ok, info = self.gate().evaluate(
            self.cfg, self.fresh_state(), "crypto", "BTC",
            100.0, 3.1, "+3.1%/1h", 1.2)
        self.assertTrue(ok)
        self.assertEqual(info["backend"], "cursor")

    def test_x_quota_on_first_backend_tries_next(self):
        import sys
        outs = {"cursor": ("ERROR: quota exceeded for the month\n", "quota"),
                "codex": (self.fake_output, "")}

        def fake2(argv, timeout, stdin_text, cwd=None):
            self.calls += 1
            name = "cursor" if "cursor" in argv[0] or "-p" in argv else "codex"
            body, _ = outs[name]
            class R:
                pass
            r = R()
            r.stdout, r.stderr, r.returncode = body, "", 0
            return r

        self.ag._run_cmd = fake2
        g = self.gate(backends=["cursor", "codex"],
                      cursor_bin=[sys.executable],
                      codex_bin=[sys.executable])
        ok, info = g.evaluate(self.cfg, self.fresh_state(), "crypto",
                              "BTC", 100.0, 3.1, "+3.1%/1h", 1.2)
        self.assertTrue(ok)
        self.assertEqual(info["backend"], "codex")
        self.assertEqual(self.calls, 2)

    def test_x_analyze_reports_gate_stats(self):
        lines = analyze.throughput_lines(
            {"date": "2026-09-27", "scans": 10, "signals": 4,
             "entered": 1, "crypto_skipped": 0,
             "ai_approvals": 1, "ai_vetoes": 3})
        self.assertTrue(any("AI gate: 1 approved / 3 vetoed" in l
                            for l in lines))

    def test_x_entry_journal_tags_strategy(self):
        st = self.fresh_state()
        real_state, real_journal = pt.STATE_PATH, pt.JOURNAL_PATH
        pt.STATE_PATH = os.path.join(self.tmp, "state.json")
        pt.JOURNAL_PATH = os.path.join(self.tmp, "ptrades.jsonl")
        self.addCleanup(setattr, pt, "STATE_PATH", real_state)
        self.addCleanup(setattr, pt, "JOURNAL_PATH", real_journal)
        pt.enter(self.cfg, st, "crypto", "BTC", 100.0, 3.1, "+3.1%/1h",
                 ai={"decision": "APPROVE", "confidence": 72,
                     "reason": "ok", "latency_ms": 5, "model": "gpt-6-sol"})
        with open(pt.JOURNAL_PATH) as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["strategy"], "v2-ai-gate")
        self.assertEqual(rec["ai"]["confidence"], 72)
        st2 = self.fresh_state()
        pt.enter(self.cfg, st2, "crypto", "ETH", 50.0, 3.0, "+3.0%/1h")
        with open(pt.JOURNAL_PATH) as f:
            recs = [json.loads(l) for l in f]
        self.assertEqual(recs[-1]["strategy"], "v1-rules")
        self.assertNotIn("ai", recs[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
