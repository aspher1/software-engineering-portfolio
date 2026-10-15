"""Iter-8 decision-neutral infra bundle.

B1 fetch-time spot age (`spot_age_fetch_s`), B2 full-page flag
(`trades_full_page`), B3 `exit_direction` on exit dicts, C1 explicit Kraken
`error`-array check, C2 log-only `p_ref`/`p_now`/`sigma`/`floor_strike`,
C3 `accuracy_stats` post-filter split.

Every field is additive metadata: the poison tests below feed absurd values
through the decision paths and assert identical outcomes. No network: all
I/O is monkeypatched.
"""
import json
import math
import os
import random
import sys
import time
import types
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import same_day_loop as sdl
from same_day_loop import (accuracy_stats, check_exits, exit_direction,
                           grade_pending, main)

TICKER = "KXBTC15M-26SEP301900-00"
NEW_ENTRY_FIELDS = ("spot_age_fetch_s", "trades_full_page", "p_ref", "p_now",
                    "sigma", "floor_strike")


# ---------------- fixtures: a real fair_15m path on synthetic Kraken data ----------------

def _candles(now, n=75, sigma=0.001, px=100.0):
    out, p = [], px
    for i in range(n):
        out.append((now - 60 * (n - i), p))
        p *= math.exp(sigma if i % 2 == 0 else -sigma)
    return out


def _trades(wo, n_rows, last_ts, ref_px=100.0, now_px=100.5):
    """n_rows trades: 10 reference prints in [wo-60, wo), then prints at
    now_px with the final one at last_ts."""
    rows = [[f"{ref_px}", "1.0", wo - 60 + 5 * i, "b", "m", ""] for i in range(10)]
    rest = n_rows - 10
    for i in range(rest):
        ts = wo + (last_ts - wo) * (i + 1) / rest
        rows.append([f"{now_px}", "1.0", ts, "s", "m", ""])
    return rows


def _market(now, strike=101.25, bid="0.60", ask="0.62"):
    m = {
        "ticker": TICKER, "title": "BTC up?",
        "open_time": datetime.fromtimestamp(now - 600, timezone.utc).isoformat(),
        "close_time": datetime.fromtimestamp(now + 300, timezone.utc).isoformat(),
        "yes_bid_dollars": bid, "yes_ask_dollars": ask, "volume_fp": "1000",
    }
    if strike is not None:
        m["floor_strike"] = strike
    return m


def _scan(tmp_path, monkeypatch, capsys, n_rows=999, fetch_delay=40,
          last_trade_lag=30, strike=101.25, argv=("same_day_loop.py",)):
    """Run main() through the REAL fair_15m. Run start = now; the trades page
    comes back `fetch_delay` s later; its last print is `last_trade_lag` s
    older than the fetch."""
    sp, gp = tmp_path / "state.json", tmp_path / "grades.jsonl"
    sp.write_text(json.dumps({"alerted": {}}))
    monkeypatch.setattr(sdl, "STATE_PATH", str(sp))
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    # Freeze the clock the script reads (datetime.now(timezone.utc)) and
    # derive `now` from the same instant: removes the second-boundary race
    # between the test's int(time.time()) and the script's datetime.now().
    # A subclass (not a namespace) so fromisoformat/strptime keep working.
    frozen = datetime(2026, 10, 2, 21, 10, 0, tzinfo=timezone.utc)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(sdl, "datetime", _FrozenDT)
    now = int(frozen.timestamp())
    wo = now - 600
    fetch_ts = now + fetch_delay
    trades = _trades(wo, n_rows, fetch_ts - last_trade_lag)
    monkeypatch.setattr(sdl, "kalshi_same_day", lambda today: [_market(now, strike)])
    monkeypatch.setattr(sdl, "poly_same_day", lambda today: [])
    monkeypatch.setattr(sdl, "kraken_trades", lambda pair, since: trades)
    monkeypatch.setattr(sdl, "kraken_candles", lambda pair: _candles(now))
    monkeypatch.setattr(sdl, "time", types.SimpleNamespace(time=lambda: fetch_ts))
    monkeypatch.setattr(sys, "argv", list(argv))
    main()
    out = json.loads(capsys.readouterr().out)
    return out, json.loads(sp.read_text())["alerted"], now, trades


# ---------------- B1: fetch-time spot age ----------------

def test_fair_15m_meta_records_fetch_time_and_rows(monkeypatch):
    now = int(time.time())
    trades = _trades(now - 600, 500, now - 20)
    monkeypatch.setattr(sdl, "kraken_trades", lambda pair, since: trades)
    monkeypatch.setattr(sdl, "kraken_candles", lambda pair: _candles(now))
    monkeypatch.setattr(sdl, "time", types.SimpleNamespace(time=lambda: now + 7.5))
    meta = {}
    with_meta = sdl.fair_15m("XBTUSD", now - 600, now + 300, now, meta)
    assert meta["trades_fetch_ts"] == now + 7.5
    assert meta["trades_rows"] == 500
    assert meta["candles"] == _candles(now)  # iter-11: window-so-far 1m closes
    # the meta out-param never changes the returned tuple
    assert with_meta == sdl.fair_15m("XBTUSD", now - 600, now + 300, now)


def test_fair_15m_meta_untouched_when_fetch_fails(monkeypatch):
    def _boom(pair, since):
        raise RuntimeError("kraken down")
    monkeypatch.setattr(sdl, "kraken_trades", _boom)
    meta = {}
    assert sdl.fair_15m("XBTUSD", 0, 900, 100, meta) is None
    assert meta == {}


def test_spot_age_fetch_uses_fetch_time_not_run_start(tmp_path, monkeypatch, capsys):
    # slow scan: page returns 40 s after run start, last print 30 s before
    # that -> run-start age is -10 (the live artifact), fetch-time age is 30
    out, alerted, now, _ = _scan(tmp_path, monkeypatch, capsys,
                                 fetch_delay=40, last_trade_lag=30)
    e = alerted[f"{TICKER}:YES"]
    assert e["spot_age_s"] == -10
    assert e["spot_age_fetch_s"] == 30
    assert isinstance(e["spot_age_fetch_s"], int)
    sig = out["new_signals"][0]
    for f in NEW_ENTRY_FIELDS:
        assert f not in sig  # stdout delivery payload unchanged


def test_spot_age_fetch_none_when_fair_is_stubbed(tmp_path, monkeypatch, capsys):
    # stubs that ignore the meta out-param leave the fields None, not wrong
    sp = tmp_path / "state.json"
    sp.write_text(json.dumps({"alerted": {}}))
    monkeypatch.setattr(sdl, "STATE_PATH", str(sp))
    monkeypatch.setattr(sdl, "GRADE_PATH", str(tmp_path / "g.jsonl"))
    now = int(time.time())
    monkeypatch.setattr(sdl, "kalshi_same_day", lambda today: [_market(now)])
    monkeypatch.setattr(sdl, "poly_same_day", lambda today: [])
    monkeypatch.setattr(sdl, "fair_15m",
                        lambda *a: (0.75, 100.0, 100.0, 0.001, 5.0, a[3]))
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py"])
    main()
    capsys.readouterr()
    e = json.loads(sp.read_text())["alerted"][f"{TICKER}:YES"]
    assert e["spot_age_fetch_s"] is None and e["trades_full_page"] is None


# ---------------- B2: full-page flag ----------------

@pytest.mark.parametrize("n_rows,expected", [(11, False), (999, False), (1000, True)])
def test_trades_full_page_true_iff_1000_rows(tmp_path, monkeypatch, capsys,
                                             n_rows, expected):
    _, alerted, _, _ = _scan(tmp_path, monkeypatch, capsys, n_rows=n_rows)
    assert alerted[f"{TICKER}:YES"]["trades_full_page"] is expected


def test_page_cap_constant():
    assert sdl.KRAKEN_TRADES_PAGE == 1000


# ---------------- C2: log-only model inputs + strike ----------------

def test_entry_logs_model_inputs_and_strike(tmp_path, monkeypatch, capsys):
    _, alerted, now, trades = _scan(tmp_path, monkeypatch, capsys, strike=101.25)
    e = alerted[f"{TICKER}:YES"]
    res = sdl.fair_15m("XBTUSD", now - 600, now + 300, now)
    assert e["p_ref"] == res[1] == 100.0
    assert e["p_now"] == res[2] == 100.5
    assert e["sigma"] == pytest.approx(0.001)
    assert e["sigma"] == res[3]
    assert e["floor_strike"] == 101.25


def test_missing_strike_logs_none(tmp_path, monkeypatch, capsys):
    _, alerted, _, _ = _scan(tmp_path, monkeypatch, capsys, strike=None)
    assert alerted[f"{TICKER}:YES"]["floor_strike"] is None


def test_scan_makes_no_new_network_calls(tmp_path, monkeypatch, capsys):
    calls = []
    real = sdl.requests.get

    def spy(url, *a, **k):
        calls.append(str(url))
        return real(url, *a, **k)  # conftest guard fails orderbook fetches
    monkeypatch.setattr(sdl.requests, "get", spy)
    _scan(tmp_path, monkeypatch, capsys)
    # Kraken/Kalshi list fetches are stubbed: the only HTTP call left is the
    # pre-existing alert-time orderbook fetch, exactly as before the change
    assert calls == [f"{sdl.KALSHI_API}/markets/{TICKER}/orderbook"]


def test_no_mark_writes_nothing(tmp_path, monkeypatch, capsys):
    sp = tmp_path / "state.json"
    out, alerted, _, _ = _scan(tmp_path, monkeypatch, capsys,
                               argv=("same_day_loop.py", "--no-mark"))
    assert alerted == {}
    assert out["signals_total"] == 1


# ---------------- grade rows carry every new field ----------------

def _closed_entry(now, side="NO", **extra):
    e = {"ts": datetime.fromtimestamp(now - 1200, timezone.utc).isoformat(),
         "ticker": "KXBTC15M-26SEP301700-00", "side": side,
         "edge": -0.10, "market_mid": 0.60,
         "window_open_utc": now - 2100, "window_close_utc": now - 1200}
    e.update(extra)
    return e


def test_grade_rows_carry_new_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(sdl, "GRADE_PATH", str(tmp_path / "grades.jsonl"))
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda t: "NO")
    now = int(time.time())
    logged = dict(spot_age_fetch_s=12, trades_full_page=True, p_ref=100.0,
                  p_now=100.5, sigma=0.001, floor_strike=101.25)
    alerted = {
        "KXBTC15M-26SEP301700-00:NO": _closed_entry(now, **logged),
        "KXETH15M-26SEP301700-00:YES": dict(
            _closed_entry(now, side="YES"), ticker="KXETH15M-26SEP301700-00"),
        "KXSOL15M-26SEP301700-00:NO": "legacy-string",
    }
    new, _ = grade_pending(alerted, now, mark=True)
    by = {r["key"]: r for r in new}
    for f, v in logged.items():
        assert by["KXBTC15M-26SEP301700-00:NO"][f] == v
        assert by["KXETH15M-26SEP301700-00:YES"][f] is None
        assert by["KXSOL15M-26SEP301700-00:NO"][f] is None
    disk = [json.loads(l) for l in (tmp_path / "grades.jsonl").read_text().splitlines()]
    assert {r["key"]: r["trades_full_page"] for r in disk}["KXBTC15M-26SEP301700-00:NO"] is True


# ---------------- poison tests: new fields never move a decision ----------------

POISON = dict(spot_age_fetch_s=10 ** 9, trades_full_page=True, p_ref=-1.0,
              p_now=float("inf"), sigma=0.0, floor_strike="garbage",
              exit_direction="adverse")


def test_grading_ignores_new_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda t: "NO")
    now = int(time.time())
    outs = []
    for i, extra in enumerate(({}, POISON)):
        monkeypatch.setattr(sdl, "GRADE_PATH", str(tmp_path / f"g{i}.jsonl"))
        new, acc = grade_pending({"KXBTC15M-26SEP301700-00:NO": _closed_entry(now, **extra)},
                                 now, mark=True)
        outs.append(([(r["key"], r["actual"], r["correct"]) for r in new], acc))
    assert outs[0] == outs[1]


def _open_entry(side, edge, mid, **extra):
    now = int(time.time())
    e = {"ts": "t", "ticker": "KXBTC15M-26SEP301800-00", "side": side,
         "edge": edge, "market_mid": mid,
         "window_open_utc": now - 300, "window_close_utc": now + 600}
    e.update(extra)
    return {f"KXBTC15M-26SEP301800-00:{side}": e}


def _live(bid, ask):
    now = int(time.time())
    m = {"ticker": "KXBTC15M-26SEP301800-00", "title": "BTC up?",
         "yes_bid_dollars": bid, "yes_ask_dollars": ask}
    return {"KXBTC15M-26SEP301800-00": (m, "BTC", now - 300, now + 600)}


def _stub_fair(fair):
    return lambda *a: (fair, 100.0, 100.0, 0.001, 10.0, a[3])


@pytest.mark.parametrize("side,edge,mid,bid,ask,fair", [
    ("YES", 0.10, 0.50, 0.595, 0.605, 0.55),
    ("NO", -0.077, 0.415, 0.545, 0.555, 0.55),
    ("YES", 0.10, 0.50, 0.495, 0.505, 0.70),   # hold
])
def test_exits_ignore_new_entry_fields(monkeypatch, side, edge, mid, bid, ask, fair):
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(fair))
    now = int(time.time())
    clean = check_exits(_open_entry(side, edge, mid), _live(bid, ask), now, mark=False)
    poisoned = check_exits(_open_entry(side, edge, mid, **POISON), _live(bid, ask),
                           now, mark=False)
    assert clean == poisoned


# ---------------- B3: exit_direction ----------------

@pytest.mark.parametrize("entry,now_px,expected", [
    (0.50, 0.60, "favourable"),
    (0.60, 0.50, "adverse"),
    (0.585, 0.585, "adverse"),            # no move is not favourable
    (0.585, 0.5850000000000001, "adverse"),  # float noise below display precision
    (0.585, 0.586, "favourable"),
    (None, 0.60, None),                   # legacy entry without a logged mid
])
def test_exit_direction_pure(entry, now_px, expected):
    assert exit_direction(entry, now_px) == expected


def test_exit_direction_favourable_yes_edge_reversed(monkeypatch):
    # YES entry at 0.50, market now 0.60 with fair below it: edge_reversed,
    # yet the taken side gained 10c -> favourable
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    ex = check_exits(_open_entry("YES", 0.10, 0.50), _live(0.595, 0.605),
                     int(time.time()), mark=False)
    assert ex[0]["reason"] == "edge_reversed"
    assert ex[0]["exit_direction"] == "favourable"


def test_exit_direction_adverse_yes(monkeypatch):
    # YES entry at 0.60, market now 0.50 and fair 0.45: thesis dead AND the
    # taken side lost 10c -> adverse
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.45))
    monkeypatch.setattr(sdl, "shrink_midrange", lambda f, m, **k: f)
    ex = check_exits(_open_entry("YES", 0.08, 0.60), _live(0.495, 0.505),
                     int(time.time()), mark=False)
    assert ex[0]["reason"] == "edge_reversed"
    assert ex[0]["exit_direction"] == "adverse"


def test_exit_direction_no_side_uses_taken_price(monkeypatch):
    # NO entry at YES-mid 0.415 (taken 0.585); YES-mid now 0.30 (taken 0.70)
    # with fair 0.33: NO edge -0.03 -> edge_reversed, taken side +11.5c
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.33))
    ex = check_exits(_open_entry("NO", -0.077, 0.415), _live(0.295, 0.305),
                     int(time.time()), mark=False)
    assert ex[0]["reason"] == "edge_reversed"
    assert ex[0]["now_price_taken"] == pytest.approx(0.70)
    assert ex[0]["exit_direction"] == "favourable"
    # NO entry, YES-mid rises 0.415 -> 0.55 (taken 0.45): adverse
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    ex = check_exits(_open_entry("NO", -0.077, 0.415), _live(0.545, 0.555),
                     int(time.time()), mark=False)
    assert ex[0]["exit_direction"] == "adverse"


def test_exit_direction_none_without_entry_mid(monkeypatch):
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    alerted = _open_entry("YES", 0.10, 0.50)
    del alerted["KXBTC15M-26SEP301800-00:YES"]["market_mid"]
    ex = check_exits(alerted, _live(0.595, 0.605), int(time.time()), mark=False)
    assert ex[0]["exit_direction"] is None


def test_exit_direction_not_stamped_into_state(monkeypatch):
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    alerted = _open_entry("YES", 0.10, 0.50)
    check_exits(alerted, _live(0.595, 0.605), int(time.time()), mark=True)
    v = alerted["KXBTC15M-26SEP301800-00:YES"]
    assert v["exit_reason"] == "edge_reversed" and "exit_direction" not in v


# ---------------- C1: explicit Kraken error-array check ----------------

class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def _legacy_trades(body):
    res = body.get("result", {})
    key = next((k for k in res if k != "last"), None)
    return res.get(key, []) if key else []


def _legacy_candles(body):
    res = body.get("result", {})
    key = next((k for k in res if k != "last"), None)
    return [(int(c[0]), float(c[4])) for c in res.get(key, [])] if key else []


TRADE_ROWS = [["100.1", "0.5", 1790000000.1, "b", "m", "", 1],
              ["100.2", "0.1", 1790000001.2, "s", "l", "", 2]]
OHLC_ROWS = [[1790000000, "1", "2", "0.5", "100.5", "100", "3", 4],
             [1790000060, "1", "2", "0.5", "100.7", "100", "3", 4]]

CONFORMANT = [
    lambda rows: {"error": [], "result": {"XXBTZUSD": rows, "last": "1790000001"}},
    lambda rows: {"error": [], "result": {"last": 1790000001, "XBTUSD": rows}},
    lambda rows: {"error": [], "result": {"XXBTZUSD": [], "last": "0"}},
    lambda rows: {"error": [], "result": {}},
    lambda rows: {"error": ["WGeneral:Deprecated"], "result": {"XXBTZUSD": rows, "last": "1"}},
]


@pytest.mark.parametrize("make", CONFORMANT)
def test_kraken_conformant_bodies_identical_to_legacy(monkeypatch, make):
    monkeypatch.setattr(sdl.requests, "get", lambda *a, **k: _Resp(make(TRADE_ROWS)))
    assert sdl.kraken_trades("XBTUSD", 0) == _legacy_trades(make(TRADE_ROWS))
    monkeypatch.setattr(sdl.requests, "get", lambda *a, **k: _Resp(make(OHLC_ROWS)))
    assert sdl.kraken_candles("XBTUSD") == _legacy_candles(make(OHLC_ROWS))


@pytest.mark.parametrize("body", [
    {"error": ["EGeneral:Too many requests"]},
    {"error": ["EService:Unavailable"], "result": {}},
    # non-conformant: error AND a result -> the error wins
    {"error": ["EQuery:Unknown asset pair"], "result": {"XXBTZUSD": TRADE_ROWS, "last": "1"}},
    {"error": ["WGeneral:x", "EAPI:Rate limit exceeded"], "result": {"XXBTZUSD": TRADE_ROWS}},
])
def test_kraken_error_bodies_return_empty(monkeypatch, body):
    monkeypatch.setattr(sdl.requests, "get", lambda *a, **k: _Resp(body))
    assert sdl.kraken_trades("XBTUSD", 0) == []
    assert sdl.kraken_candles("XBTUSD") == []


def test_kraken_error_makes_fair_none(monkeypatch):
    monkeypatch.setattr(sdl.requests, "get",
                        lambda *a, **k: _Resp({"error": ["EAPI:Rate limit exceeded"]}))
    assert sdl.fair_15m("XBTUSD", 0, 900, 100) is None


# ---------------- C3: accuracy_stats post-filter split ----------------

def _legacy_accuracy_stats(grades):
    authoritative = [g for g in grades if g.get("proxy") == "kalshi"
                     and not g.get("excluded_from_accuracy")]
    scored = [g for g in authoritative if g.get("correct") is not None]
    recent = scored[-50:]
    wins = sum(1 for g in recent if g["correct"])
    note = ("settlement per Kalshi's published result only "
            "(legacy Kraken-proxy rows excluded); gross of fees")
    pnl, staked = 0.0, 0.0
    for g in recent:
        m = g.get("market_mid")
        if m is None:
            continue
        staked += m if g.get("side") == "YES" else (1 - m)
        if g.get("side") == "YES":
            pnl += (1 - m) if g["correct"] else -m
        else:
            pnl += m if g["correct"] else -(1 - m)
    return {"graded": len(recent), "wins": wins,
            "win_rate": round(wins / len(recent), 3) if recent else None,
            "pnl_per_dollar_staked": round(pnl / staked, 4) if staked else None,
            "note": note}


def _rand_rows(rng, n):
    rows = []
    for i in range(n):
        rows.append({
            "key": f"k{i}", "side": rng.choice(["YES", "NO"]),
            "proxy": rng.choice(["kalshi", "kalshi", "kraken"]),
            "excluded_from_accuracy": rng.random() < 0.1 or None,
            "correct": rng.choice([True, False, None]) if rng.random() < 0.1
            else rng.random() < 0.4,
            "market_mid": None if rng.random() < 0.15 else round(rng.uniform(0.01, 0.99), 3),
        })
    return rows


@pytest.mark.parametrize("seed", range(25))
def test_accuracy_legacy_keys_byte_identical(seed):
    rows = _rand_rows(random.Random(seed), random.Random(seed).randint(0, 300))
    new, old = accuracy_stats(rows), _legacy_accuracy_stats(rows)
    assert json.dumps({k: new[k] for k in old}) == json.dumps(old)
    assert list(new)[:len(old)] == list(old)  # legacy key order preserved


def test_accuracy_post_filter_block():
    rows = [
        {"key": "a", "proxy": "kalshi", "side": "YES", "market_mid": 0.70, "correct": True},
        {"key": "b", "proxy": "kalshi", "side": "NO", "market_mid": 0.40, "correct": False},
        {"key": "c", "proxy": "kalshi", "side": "YES", "market_mid": 0.20, "correct": False},
        {"key": "d", "proxy": "kalshi", "side": "NO", "market_mid": 0.90, "correct": True},
        {"key": "e", "proxy": "kalshi", "side": "YES", "market_mid": None, "correct": True},
        {"key": "f", "proxy": "kalshi", "side": "YES", "market_mid": 0.50, "correct": True},
        {"key": "g", "proxy": "kraken", "excluded_from_accuracy": True,
         "side": "YES", "market_mid": 0.90, "correct": False},
    ]
    acc = accuracy_stats(rows)
    pf = acc["post_filter"]
    # a (0.70), b (taken 0.60), f (0.50) agree; c, d are contrarian; e unpriced
    assert pf["graded"] == 3 and pf["wins"] == 2
    assert pf["win_rate"] == pytest.approx(0.667)
    assert pf["mean_taken_price"] == pytest.approx(0.6)
    # pnl: a +0.30, b -0.60, f +0.50 = +0.20 over 1.80 staked
    assert pf["pnl_per_dollar_staked"] == pytest.approx(round(0.2 / 1.8, 4))
    # top-level baseline over the legacy rolling window (a,b,c,d,f priced)
    assert acc["mean_taken_price"] == pytest.approx(round((0.7 + 0.6 + 0.2 + 0.1 + 0.5) / 5, 3))


def test_accuracy_post_filter_rolls_last_50():
    rows = [{"key": f"k{i}", "proxy": "kalshi", "side": "YES",
             "market_mid": 0.6, "correct": i >= 10} for i in range(60)]
    pf = accuracy_stats(rows)["post_filter"]
    assert pf["graded"] == 50 and pf["wins"] == 50


def test_accuracy_post_filter_empty():
    acc = accuracy_stats([])
    assert acc["mean_taken_price"] is None
    assert acc["post_filter"] == {"graded": 0, "wins": 0, "win_rate": None,
                                  "mean_taken_price": None,
                                  "pnl_per_dollar_staked": None,
                                  # iter-11 additive $1-stake keys
                                  "pnl_1usd_stake_gross": None,
                                  "pnl_1usd_stake_net": None}
