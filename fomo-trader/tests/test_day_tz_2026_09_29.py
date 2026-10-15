"""Bot calendar day is pinned to America/New_York, immune to host TZ flips.

Hermetic: no bot process, no network, no real runs/ state.
"""
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fomo_trader as ft

NY = ZoneInfo("America/New_York")
# 2026-09-30 02:00 UTC == 2026-09-29 22:00 New York == 2026-09-30 16:00
# Kiritimati: host-local day differs from the bot day under both flips.
LATE_EVENING_NY = datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc).timestamp()
# 2026-09-29 20:00 UTC == 16:00 New York == 2026-09-30 10:00 Kiritimati.
AFTERNOON_NY = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc).timestamp()


def _frozen_datetime(epoch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(epoch, tz)
    return Frozen


@pytest.fixture
def host_tz():
    """Flip the process TZ like a host restart would; always restore."""
    had = "TZ" in os.environ
    old = os.environ.get("TZ")

    def flip(name):
        os.environ["TZ"] = name
        time.tzset()

    yield flip
    if had:
        os.environ["TZ"] = old
    else:
        os.environ.pop("TZ", None)
    time.tzset()


def trader(tmp_path, day):
    t = ft.Trader.__new__(ft.Trader)
    t.cfg = {"risk": {"max_trades_per_day": 10, "max_trades_per_hour": 3}}
    t.lock = threading.RLock()
    t.state_path = str(tmp_path / "state.json")
    t.state = {"positions": {}, "cooldown": {}, "day": day,
               "trades_today": [1.0, 2.0], "trades_this_hour": [],
               "realized_sol": 0.25, "realized_bnb": 0.1,
               "realized_usd": -12.5, "bankroll_usd": 90.0,
               "day_start_bankroll_usd": 100.0}
    return t


def test_day_tz_is_fixed_new_york():
    assert ft.DAY_TZ.key == "America/New_York"


@pytest.mark.parametrize("tz_name", ["UTC", "Pacific/Kiritimati"])
@pytest.mark.parametrize("epoch,expected", [
    (LATE_EVENING_NY, "2026-09-29"), (AFTERNOON_NY, "2026-09-29")])
def test_bot_today_ignores_host_tz_frozen(monkeypatch, host_tz, tz_name,
                                          epoch, expected):
    monkeypatch.setattr(ft, "datetime", _frozen_datetime(epoch))
    host_tz(tz_name)
    assert ft._bot_today_str() == expected


def test_host_local_day_would_have_flipped(host_tz):
    """Documents the defect: host-local strftime disagrees across flips."""
    host_tz("America/New_York")
    ny = time.strftime("%Y-%m-%d", time.localtime(LATE_EVENING_NY))
    host_tz("UTC")
    utc = time.strftime("%Y-%m-%d", time.localtime(LATE_EVENING_NY))
    assert (ny, utc) == ("2026-09-29", "2026-09-30")


@pytest.mark.parametrize("tz_name", ["UTC", "Pacific/Kiritimati",
                                     "America/Los_Angeles"])
def test_bot_today_matches_new_york_on_real_clock(host_tz, tz_name):
    host_tz(tz_name)
    before = datetime.now(NY).strftime("%Y-%m-%d")
    got = ft._bot_today_str()
    after = datetime.now(NY).strftime("%Y-%m-%d")
    assert got in (before, after)


def test_roll_day_ignores_pure_tz_flip(tmp_path, monkeypatch, host_tz):
    monkeypatch.setattr(ft, "datetime", _frozen_datetime(LATE_EVENING_NY))
    host_tz("America/New_York")
    t = trader(tmp_path, ft._bot_today_str())
    for tz_name in ("UTC", "Pacific/Kiritimati", "America/Los_Angeles"):
        host_tz(tz_name)
        t._roll_day()
        assert t.state["day"] == "2026-09-29"
        assert t.state["trades_today"] == [1.0, 2.0]
        assert t.state["realized_usd"] == -12.5
        assert t.state["realized_sol"] == 0.25
        assert t.state["realized_bnb"] == 0.1
        assert t.state["day_start_bankroll_usd"] == 100.0
    assert not (tmp_path / "state.json").exists()


def test_roll_day_resets_when_fixed_tz_date_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(ft, "datetime", _frozen_datetime(AFTERNOON_NY))
    t = trader(tmp_path, ft._bot_today_str())
    t._roll_day()
    assert t.state["trades_today"] == [1.0, 2.0]
    # 2026-09-30 04:30 UTC == 00:30 New York on 2026-09-30.
    next_day = datetime(2026, 9, 30, 4, 30, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(ft, "datetime", _frozen_datetime(next_day))
    t._roll_day()
    assert t.state["day"] == "2026-09-30"
    assert t.state["trades_today"] == []
    assert t.state["realized_usd"] == 0.0
    assert t.state["realized_sol"] == 0.0
    assert t.state["realized_bnb"] == 0.0
    assert t.state["day_start_bankroll_usd"] == 90.0


def test_legacy_host_local_day_self_heals_with_one_roll(tmp_path, monkeypatch):
    """Old state.json day written in another tz rolls once, then is stable."""
    monkeypatch.setattr(ft, "datetime", _frozen_datetime(LATE_EVENING_NY))
    t = trader(tmp_path, "2026-09-30")  # host-local UTC day from old code
    t._roll_day()
    assert t.state["day"] == "2026-09-29"
    assert t.state["trades_today"] == []
    t.state["trades_today"] = [3.0]
    t._roll_day()
    assert t.state["trades_today"] == [3.0]
