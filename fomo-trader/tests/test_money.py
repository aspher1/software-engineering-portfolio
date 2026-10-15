"""Tests for money.py: paper bankroll, adaptive caps, drawdown brake."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import money


def cfg(**over):
    m = {"starting_bankroll_usd": 1000.0, "risk_per_trade_pct": 1.0,
         "daily_loss_pct": 6.0, "max_drawdown_pct": 15.0,
         "drawdown_resume_pct": 10.0}
    m.update(over)
    return {"money": m}


def test_ensure_state_defaults():
    st = {}
    assert money.ensure_state(st, cfg()) is True
    assert st["bankroll_usd"] == 1000.0
    assert st["equity_peak_usd"] == 1000.0
    assert st["day_start_bankroll_usd"] == 1000.0
    assert st["bankroll_seeded"] is False
    # second call adds nothing
    assert money.ensure_state(st, cfg()) is False


def _write_journal(rows):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def test_seed_from_journal_sums_priced_closes():
    jp = _write_journal([
        {"type": "close", "realized_usd": 5.0},
        {"type": "close", "realized_usd": -12.5},
        {"type": "close", "realized_sol": -0.1, "sol_usd": 150.0},  # -15 native-priced
        {"type": "close", "realized_sol": -0.2},  # no rate -> skipped
        {"type": "entry", "realized_usd": 999.0},  # entries ignored
    ])
    st = {}
    res = money.seed_from_journal(st, cfg(), jp)
    assert res["seeded"] is True
    # 1000 + 5 - 12.5 - 15 = 977.5
    assert st["bankroll_usd"] == 977.5
    assert res["priced_closes"] == 3
    assert res["skipped_closes"] == 1
    assert st["equity_peak_usd"] == 1000.0
    # never seeds twice
    res2 = money.seed_from_journal(st, cfg(), jp)
    assert res2["seeded"] is False
    assert st["bankroll_usd"] == 977.5


def test_apply_close_moves_bankroll_and_ratchets_peak():
    st = {"bankroll_usd": 900.0, "equity_peak_usd": 1000.0}
    money.apply_close(st, 150.0)
    assert st["bankroll_usd"] == 1050.0
    assert st["equity_peak_usd"] == 1050.0  # new high ratchets peak
    money.apply_close(st, -200.0)
    assert st["bankroll_usd"] == 850.0
    assert st["equity_peak_usd"] == 1050.0  # peak never falls
    assert money.apply_close(st, None) == 850.0  # unpriced: no move


def test_drawdown_pct():
    st = {"bankroll_usd": 900.0, "equity_peak_usd": 1000.0}
    assert money.drawdown_pct(st) == 10.0
    st["bankroll_usd"] = 1000.0
    assert money.drawdown_pct(st) == 0.0


def test_drawdown_brake_hysteresis():
    c = cfg()
    st = {"bankroll_usd": 840.0, "equity_peak_usd": 1000.0}  # 16% dd
    assert money.entries_blocked_by_drawdown(st, c) is True
    st["bankroll_usd"] = 880.0  # 12% dd: still blocked (hysteresis)
    assert money.entries_blocked_by_drawdown(st, c) is True
    st["bankroll_usd"] = 905.0  # 9.5% dd: released
    assert money.entries_blocked_by_drawdown(st, c) is False
    st["bankroll_usd"] = 860.0  # 14% dd: not re-engaged below max
    assert money.entries_blocked_by_drawdown(st, c) is False


def test_effective_daily_loss_cap_is_adaptive():
    c = cfg()
    st = {"bankroll_usd": 900.0, "day_start_bankroll_usd": 900.0}
    # min(60 fixed, 6% of 900 = 54)
    assert money.effective_daily_loss_cap_usd(st, c, 60.0) == 54.0
    # fixed cap below dynamic -> fixed wins
    assert money.effective_daily_loss_cap_usd(st, c, 20.0) == 20.0
    # no fixed cap -> dynamic only
    assert money.effective_daily_loss_cap_usd(st, c, None) == 54.0


def test_ticket_usd_ceiling():
    c = cfg()
    st = {"bankroll_usd": 900.0}
    # 1% of 900 / 35% hard stop = 25.71
    assert money.ticket_usd_ceiling(st, c, 35) == 25.71
    # bankroll falls -> ceiling falls
    st["bankroll_usd"] = 300.0
    assert money.ticket_usd_ceiling(st, c, 35) == 8.57


def test_roll_day_snapshots_bankroll():
    st = {"bankroll_usd": 950.0, "day_start_bankroll_usd": 1000.0,
          "equity_peak_usd": 1000.0, "bankroll_seeded": True}
    money.roll_day(st, cfg())
    assert st["day_start_bankroll_usd"] == 950.0


def _boost_trader(realized_usd):
    import threading, time
    from fomo_trader import Trader
    trader = object.__new__(Trader)
    trader.cfg = {"risk": {"max_trades_per_day": 30,
                           "profit_boost_threshold_usd": 10.0,
                           "profit_boost_trades_per_day": 40},
                  "money": {"starting_bankroll_usd": 1000.0}}
    trader.state = {"positions": {}, "realized_usd": realized_usd}
    trader.lock = threading.RLock()
    return trader


def test_profit_boost_lifts_daily_cap_at_threshold():
    assert _boost_trader(9.99)._effective_max_trades_per_day() == 30
    assert _boost_trader(10.0)._effective_max_trades_per_day() == 40
    assert _boost_trader(25.5)._effective_max_trades_per_day() == 40


def test_profit_boost_defaults_without_config():
    import threading
    from fomo_trader import Trader
    trader = object.__new__(Trader)
    trader.cfg = {"risk": {"max_trades_per_day": 30}}  # no boost keys
    trader.state = {"positions": {}, "realized_usd": 50.0}
    trader.lock = threading.RLock()
    # defaults: threshold 10.0, boosted cap 40
    assert trader._effective_max_trades_per_day() == 40
    trader.state["realized_usd"] = 0.0
    assert trader._effective_max_trades_per_day() == 30
