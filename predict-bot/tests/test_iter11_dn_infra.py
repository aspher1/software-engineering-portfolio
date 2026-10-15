"""Iter-11 decision-neutral infra bundle: unit tests.

ITEM 0 (priority): jump_flag log-first — Lee-Mykland-2008-style jump test on
the window-so-far 1-min Kraken returns (bipower-variation robust sigma),
k=4; computed at signal entry time from the candles fair_15m already
fetched; logged as additive `jump_flag`/`jump_stat` metadata on the state
entry AND the grade row; NEVER read by any decision; never raises.
ITEMS 1-7: state-lock sidecar + .bak fallback on file-not-found,
load_grades fail-closed (GradesUnavailable), grading under the grades
sidecar lock, DST end-of-day bound, $1-stake gross/net pnl keys, bid/ask on
grade rows, contrarian-drop shadow log.

Every item is additive-only and decision-neutral. Paper/analysis only; no
network (all I/O monkeypatched to temp paths / stubs).
"""
import json
import math
import multiprocessing as mp
import os
import sys
import threading
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import same_day_loop as sdl
from same_day_loop import (
    GradesUnavailable,
    accuracy_stats,
    grade_pending,
    jump_test_statistic,
    load_grades,
    load_state,
    main,
)


@pytest.fixture(autouse=True)
def _tmp_data(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(sdl, "DATA", str(d))
    monkeypatch.setattr(sdl, "STATE_PATH", str(d / "same_day_loop_state.json"))
    monkeypatch.setattr(sdl, "GRADE_PATH", str(d / "signal_grades.jsonl"))
    return d


# ---------------- ITEM 0: jump flag ----------------

WO = 1_000_000


def _jump_candles(t0=WO):
    """12 closes, 1-min apart: steady +0.0003 drift, one +0.025 jump."""
    closes, px = [], 100.0
    rets = [0.0003] * 8 + [0.025, 0.0003, -0.0002]
    for i, r in enumerate([0.0] + rets):
        if i:
            px *= math.exp(r)
        closes.append((t0 + 60 * i, px))
    return closes


def _flat_candles(n=12, drift=0.0003):
    closes, px = [], 100.0
    for i in range(n):
        if i:
            px *= math.exp(drift)
        closes.append((WO + 60 * i, px))
    return closes


def test_jump_flag_detects_jump():
    flag, stat = jump_test_statistic(_jump_candles(), WO, WO + 11 * 60)
    assert flag is True
    # hand-computed: BV=(pi/2)*1.569e-5, sigma=sqrt(BV/10), stat=0.025/sigma
    assert stat == pytest.approx(15.92, rel=0.01)
    assert stat > 4.0


def test_jump_flag_no_jump():
    flag, stat = jump_test_statistic(_flat_candles(), WO, WO + 11 * 60)
    assert flag is False
    assert stat == pytest.approx(0.798, rel=0.01)
    assert stat < 4.0


@pytest.mark.parametrize("candles", [
    None, [], [(WO, 100.0)], _flat_candles(n=3),
    [(WO + 60 * i, 100.0) for i in range(12)],          # all-zero returns
    [(WO + 60 * i, 0.0) for i in range(12)],            # zero prices
    [(WO + 60 * i, float("nan")) for i in range(12)],   # non-finite
    [(WO + 60 * i, -5.0) for i in range(12)],           # negative prices
])
def test_jump_flag_none_safe(candles):
    assert jump_test_statistic(candles, WO, WO + 11 * 60) == (None, None)


@pytest.mark.parametrize("k,expected", [(3.0, True), (4.0, True),
                                        (15.0, True), (16.0, False),
                                        (100.0, False)])
def test_jump_flag_k_sweep(k, expected):
    flag, stat = jump_test_statistic(_jump_candles(), WO, WO + 11 * 60, k=k)
    assert flag is expected
    assert stat == pytest.approx(15.92, rel=0.01)  # stat is k-independent


@pytest.mark.parametrize("k", [0, -1.0, float("nan")])
def test_jump_flag_nonpositive_k_is_none(k):
    assert jump_test_statistic(_jump_candles(), WO, WO + 11 * 60, k=k) == (None, None)


@pytest.mark.parametrize("garbage", [
    "nope", [1], [[WO]], [(WO,)], [("a", "b")], [{"ts": WO}],
    [None, None], 42,
])
def test_jump_flag_never_raises(garbage):
    assert jump_test_statistic(garbage, WO, WO + 11 * 60) == (None, None)


def test_jump_window_filtering():
    # closes outside [w_open, now] are ignored; too few inside -> None
    cs = [(WO - 3600 + 60 * i, 100.0 * math.exp(0.0003 * i)) for i in range(12)]
    assert jump_test_statistic(cs, WO, WO + 11 * 60) == (None, None)
    cs2 = _flat_candles()
    flag, _ = jump_test_statistic(cs2, WO, WO + 5 * 60)  # 6 closes in window
    assert flag is False


def _freeze_at(monkeypatch, now):
    """Pin the module's clock: main() reads datetime.now(timezone.utc)."""
    frozen = datetime.fromtimestamp(now, timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(sdl, "datetime", _Frozen)


def _scan_env(monkeypatch, tmp_path, markets, argv, now=None):
    if now is not None:
        _freeze_at(monkeypatch, now)
    monkeypatch.setattr(sdl, "kalshi_same_day", lambda today: markets)
    monkeypatch.setattr(sdl, "poly_same_day", lambda today: [])
    monkeypatch.setattr(sdl, "entry_ob_imbalance", lambda t: 0.12)
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py"] + argv)


def _firing_market(now, ticker="KXBTC15M-26OCT020115-00", bid="0.585",
                   ask="0.615"):
    return {
        "ticker": ticker, "title": "BTC up?",
        "open_time": datetime.fromtimestamp(now - 600, timezone.utc).isoformat(),
        "close_time": datetime.fromtimestamp(now + 300, timezone.utc).isoformat(),
        "yes_bid_dollars": bid, "yes_ask_dollars": ask, "volume_fp": "1000",
        "floor_strike": "90000",
    }


def test_no_mark_never_invokes_jump(monkeypatch, tmp_path, capsys):
    """--no-mark builds no pending entries, so the jump path must not run
    (and therefore never fetches)."""
    now = int(datetime(2026, 10, 2, 5, 10, tzinfo=timezone.utc).timestamp())
    _scan_env(monkeypatch, tmp_path, [_firing_market(now)], ["--no-mark"], now)
    monkeypatch.setattr(sdl, "fair_15m",
                        lambda *a, **k: (0.68, 100.0, 100.5, 0.0004, 5.0, a[3]))
    calls = []
    monkeypatch.setattr(sdl, "jump_test_statistic",
                        lambda *a, **k: calls.append(a) or (True, 9.9))
    main()
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["signals_total"] >= 1
    assert calls == []  # jump path never evaluated in --no-mark


def test_jump_and_book_logged_on_entry_and_graded(monkeypatch, tmp_path):
    """mark run: entry carries jump_flag/jump_stat/bid/ask; the grade row
    carries them through; legacy entries grade to None."""
    now = int(datetime(2026, 10, 2, 5, 10, tzinfo=timezone.utc).timestamp())
    mk = _firing_market(now)
    _scan_env(monkeypatch, tmp_path, [mk], [], now)
    cs = _jump_candles(t0=now - 660)  # 12 closes; 11 fall in [wo, now]

    def fake_fair(pair, w_open, w_close, now_ts, meta=None):
        if meta is not None:
            meta["candles"] = cs
        return (0.68, 100.0, 100.5, 0.0004, 5.0, now_ts - 5)

    monkeypatch.setattr(sdl, "fair_15m", fake_fair)
    main()
    state = load_state()
    key = "KXBTC15M-26OCT020115-00:YES"
    rec = state["alerted"][key]
    assert rec["jump_flag"] is True
    # 10 in-window rets: 8x0.0003, 0.025, 0.0003 -> stat = 0.025/sigma_robust
    assert rec["jump_stat"] == pytest.approx(15.136, rel=0.01)
    assert rec["bid"] == 0.585 and rec["ask"] == 0.615
    # grade it: force the window closed and settle YES
    rec["window_close_utc"] = now - 600
    sdl.save_state(state)
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda t: "YES")
    new_rows, _ = grade_pending(load_state()["alerted"], now, mark=True)
    assert len(new_rows) == 1
    row = new_rows[0]
    assert row["jump_flag"] is True
    assert row["jump_stat"] == rec["jump_stat"]
    assert row["bid"] == 0.585 and row["ask"] == 0.615
    assert row["correct"] is True


def test_legacy_grade_row_jump_and_book_are_none(monkeypatch, tmp_path):
    now = int(datetime(2026, 10, 2, 5, 10, tzinfo=timezone.utc).timestamp())
    alerted = {"KXBTC15M-26OCT020045-00:YES": "KXBTC15M-26OCT020045-00:YES"}
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda t: "YES")
    new_rows, _ = grade_pending(alerted, now, mark=True)
    assert len(new_rows) == 1
    row = new_rows[0]
    assert row["jump_flag"] is None and row["jump_stat"] is None
    assert row["bid"] is None and row["ask"] is None
    assert row["correct"] is True


def test_no_mark_state_immutability_and_no_shadow_log(monkeypatch, tmp_path,
                                                     capsys):
    seed = json.dumps({"alerted": {"k": "v"}}).encode()
    sp = tmp_path / "data" / "same_day_loop_state.json"
    sp.write_bytes(seed)
    now = int(datetime(2026, 10, 2, 5, 10, tzinfo=timezone.utc).timestamp())
    # contrarian market: would shadow-log in mark mode
    mk = _firing_market(now, bid="0.385", ask="0.415")
    _scan_env(monkeypatch, tmp_path, [mk], ["--no-mark"], now)
    monkeypatch.setattr(sdl, "fair_15m",
                        lambda *a, **k: (0.48, 100.0, 100.5, 0.0004, 5.0, a[3]))
    main()
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert sp.read_bytes() == seed
    assert not (tmp_path / "data" / "contrarian_drops.jsonl").exists()
    assert not (tmp_path / "data" / "signal_grades.jsonl").exists()


# ---------------- ITEM 1: state-lock sidecar + .bak fallback ----------------

def test_load_state_missing_falls_back_to_bak(tmp_path, monkeypatch):
    bak = {"alerted": {"KXBTC15M-26OCT020045-00:YES": {"side": "YES"}}}
    (tmp_path / "data" / "same_day_loop_state.json.bak").write_text(json.dumps(bak))
    assert load_state() == bak


def test_load_state_missing_no_bak_is_fresh(tmp_path):
    assert load_state() == {"alerted": {}}


def test_load_state_corrupt_falls_back_to_bak(tmp_path):
    bak = {"alerted": {"k": "v"}}
    (tmp_path / "data" / "same_day_loop_state.json").write_text("{corrupt")
    (tmp_path / "data" / "same_day_loop_state.json.bak").write_text(json.dumps(bak))
    assert load_state() == bak


def test_sidecar_lock_serializes_across_inode_replace(tmp_path, monkeypatch):
    """N processes x M increments under the sidecar lock, with save_state
    replacing the state inode inside the critical section every time: the
    final counter must be exact (the old STATE_PATH-flock lost exclusion
    here)."""
    sp = tmp_path / "data" / "same_day_loop_state.json"
    monkeypatch.setattr(sdl, "STATE_PATH", str(sp))
    assert sdl._state_lock_path() == str(sp) + ".lock"  # sidecar derivation
    sp.write_text(json.dumps({"alerted": {}, "counter": 0}))
    lock_path = str(sp) + ".lock"

    def worker(n, state_p, lock_p):
        # NOTE: the child must not touch module globals (fork inherits the
        # live repo's STATE_PATH); all paths are passed explicitly.
        import fcntl, json as j, os as o
        for _ in range(n):
            fd = o.open(lock_p, o.O_RDWR | o.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                with open(state_p) as f:
                    s = j.load(f)
                s["counter"] += 1
                tmp = state_p + ".tmp"
                with open(tmp, "w") as f:
                    j.dump(s, f)
                try:
                    o.replace(state_p, state_p + ".bak")
                except FileNotFoundError:
                    pass
                o.replace(tmp, state_p)  # inode replacement inside the lock
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                o.close(fd)

    procs = [mp.get_context("fork").Process(
                 target=worker, args=(10, str(sp), lock_path))
             for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    assert all(p.exitcode == 0 for p in procs)
    assert json.loads(sp.read_text())["counter"] == 40


# ---------------- ITEM 2: load_grades fail-closed ----------------

def test_load_grades_missing_file_is_empty():
    assert load_grades() == []


def test_load_grades_unreadable_raises(tmp_path):
    # a directory at GRADE_PATH: open() raises IsADirectoryError (OSError)
    (tmp_path / "data" / "signal_grades.jsonl").mkdir()
    with pytest.raises(GradesUnavailable):
        load_grades()


def test_load_grades_skips_non_dict_lines(tmp_path):
    gp = tmp_path / "data" / "signal_grades.jsonl"
    gp.write_text('{"key": "a"}\n[1, 2]\n"str"\n{"key": "b"}\n{bad json\n')
    rows = load_grades()
    assert [r["key"] for r in rows] == ["a", "b"]


def test_load_grades_unhashable_key_does_not_nuke_read(tmp_path):
    gp = tmp_path / "data" / "signal_grades.jsonl"
    gp.write_text('{"key": "a"}\n{"key": ["x"]}\n{"key": "c"}\n')
    rows = load_grades()
    assert [r["key"] for r in rows] == ["a", ["x"], "c"]


def test_grade_pending_fail_closed_writes_nothing(monkeypatch, tmp_path):
    (tmp_path / "data" / "signal_grades.jsonl").mkdir()  # unreadable
    now = int(datetime(2026, 10, 2, 5, 10, tzinfo=timezone.utc).timestamp())
    alerted = {"KXBTC15M-26OCT020045-00:YES": "KXBTC15M-26OCT020045-00:YES"}
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda t: "YES")
    with pytest.raises(GradesUnavailable):
        grade_pending(alerted, now, mark=True)


def test_grade_only_grades_unavailable(monkeypatch, tmp_path, capsys):
    (tmp_path / "data" / "signal_grades.jsonl").mkdir()
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py", "--grade-only"])
    main()
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "grades_unavailable" in out["error"]


def test_main_grading_fallback_guarded(monkeypatch, tmp_path, capsys):
    """grade_pending raises GradesUnavailable AND load_grades raises: the
    :1137 fallback must not crash the payload print."""
    (tmp_path / "data" / "signal_grades.jsonl").mkdir()
    now = int(datetime(2026, 10, 2, 5, 10, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(sdl, "kalshi_same_day", lambda today: [])
    monkeypatch.setattr(sdl, "poly_same_day", lambda today: [])

    def boom(alerted, now_ts, mark):
        raise GradesUnavailable("boom")

    monkeypatch.setattr(sdl, "grade_pending", boom)
    monkeypatch.setattr(sys, "argv", ["same_day_loop.py"])
    main()
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert any("grades_unavailable" in n for n in out["notes"])
    assert out["accuracy"] == {"ok": False, "error": "grades_unavailable"}


# ---------------- ITEM 3: grading under grades-lock ----------------

def test_concurrent_appends_are_serialized(tmp_path):
    from same_day_loop import _grades_append_lock, append_grades
    gp = tmp_path / "data" / "signal_grades.jsonl"

    def worker(n, wid):
        for i in range(n):
            with _grades_append_lock():
                append_grades([{"key": f"{wid}-{i}", "v": i}])

    threads = [threading.Thread(target=worker, args=(25, w)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    lines = gp.read_text().splitlines()
    assert len(lines) == 100
    assert len({json.loads(l)["key"] for l in lines}) == 100  # none lost/duped


def test_grade_pending_reread_filters_duplicates(monkeypatch, tmp_path):
    """A row graded between our read and our append is filtered inside the
    lock instead of appended twice."""
    now = int(datetime(2026, 10, 2, 5, 10, tzinfo=timezone.utc).timestamp())
    gp = tmp_path / "data" / "signal_grades.jsonl"
    gp.write_text(json.dumps({"key": "KXBTC15M-26OCT020045-00:YES",
                              "correct": True}) + "\n")
    alerted = {"KXBTC15M-26OCT020045-00:YES": "KXBTC15M-26OCT020045-00:YES"}
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda t: "YES")
    new_rows, acc = grade_pending(alerted, now, mark=True)
    assert new_rows == []
    assert len(gp.read_text().splitlines()) == 1  # no duplicate appended
    assert acc["graded"] == 0  # proxy-less row has correct but no proxy key


# ---------------- ITEM 4: DST end-of-day bound ----------------

def _kalshi_pages(monkeypatch, markets):
    calls = []

    class Resp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._p

    def fake_get(url, params=None, timeout=None):
        calls.append(dict(params or {}))
        if calls and len(calls) == 1:
            return Resp({"markets": markets, "cursor": None})
        return Resp({"markets": []})

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    return calls


def _mk(close_iso, ticker="T"):
    return {"ticker": ticker, "close_time": close_iso}


def test_dst_end_day_bound_includes_midnight_window(monkeypatch):
    # 2026-11-01: DST ends (25-hour day, ends 00:00 EST)
    markets = [_mk("2026-11-02T00:00:00-05:00", "M1"),   # 23:45-00:00 ET window
               _mk("2026-11-01T22:30:00-05:00", "M2")]   # late-evening window
    calls = _kalshi_pages(monkeypatch, markets)
    out = sdl.kalshi_same_day("2026-11-01")
    assert len(out) == 2
    from zoneinfo import ZoneInfo
    expect = int(datetime(2026, 11, 2, tzinfo=ZoneInfo("America/New_York")).timestamp())
    assert calls[0]["max_close_ts"] == expect  # 90000 s after day start, not 86399


def test_normal_day_midnight_window_included(monkeypatch):
    markets = [_mk("2026-10-03T00:00:00-04:00", "M1"),   # 23:45-00:00 ET window
               _mk("2026-10-02T12:00:00-04:00", "M2"),
               _mk("2026-10-03T00:15:00-04:00", "M3")]   # next day: excluded
    calls = _kalshi_pages(monkeypatch, markets)
    out = sdl.kalshi_same_day("2026-10-02")
    assert len(out) == 2
    from zoneinfo import ZoneInfo
    expect = int(datetime(2026, 10, 3, tzinfo=ZoneInfo("America/New_York")).timestamp())
    assert calls[0]["max_close_ts"] == expect


def test_spring_forward_day_is_23_hours(monkeypatch):
    calls = _kalshi_pages(monkeypatch, [_mk("2026-03-08T12:00:00-04:00")])
    out = sdl.kalshi_same_day("2026-03-08")
    assert len(out) == 1
    from zoneinfo import ZoneInfo
    expect = int(datetime(2026, 3, 9, tzinfo=ZoneInfo("America/New_York")).timestamp())
    assert calls[0]["max_close_ts"] == expect  # 82800 s after day start


# ---------------- ITEM 5: $1-stake pnl keys ----------------

def _grade_rows():
    return [
        {"key": "a", "side": "YES", "market_mid": 0.6, "correct": True,
         "proxy": "kalshi"},
        {"key": "b", "side": "NO", "market_mid": 0.7, "correct": False,
         "proxy": "kalshi"},
        {"key": "c", "side": "YES", "market_mid": None, "correct": True,
         "proxy": "kalshi"},
    ]


def test_pnl_1usd_stake_math():
    acc = accuracy_stats(_grade_rows())
    # gross: YES win @0.6 -> (0.4)/0.6 = 0.6667; NO loss @0.3 taken -> -1
    assert acc["pnl_1usd_stake_gross"] == pytest.approx(-0.1667, abs=1e-4)
    # net: fees ceil(0.07*(1/0.6)*0.6*0.4*100)/100 = 0.03 -> 0.6367;
    #      ceil(0.07*(1/0.3)*0.3*0.7*100)/100 = 0.05 -> -1.05
    assert acc["pnl_1usd_stake_net"] == pytest.approx(-0.2067, abs=1e-4)
    # post_filter keeps only the agreeing row (a: YES @ 0.6; row b's taken
    # price 0.3 < 0.5 disagrees and is excluded)
    assert acc["post_filter"]["pnl_1usd_stake_gross"] == pytest.approx(0.6667, abs=1e-4)
    assert acc["post_filter"]["pnl_1usd_stake_net"] == pytest.approx(0.6367, abs=1e-4)


def test_pnl_1usd_stake_none_without_mids():
    acc = accuracy_stats([{"key": "a", "side": "YES", "correct": True}])
    assert acc["pnl_1usd_stake_gross"] is None
    assert acc["pnl_1usd_stake_net"] is None


def test_legacy_pnl_key_byte_identical():
    acc = accuracy_stats(_grade_rows())
    # legacy formula untouched: staked 0.6+0.3=0.9, pnl 0.4-0.3=0.1
    assert acc["pnl_per_dollar_staked"] == round(0.1 / 0.9, 4) == 0.1111
    # post_filter keeps only row a: staked 0.6, pnl 0.4
    assert acc["post_filter"]["pnl_per_dollar_staked"] == round(0.4 / 0.6, 4)


# ---------------- ITEM 7: contrarian shadow log ----------------

def test_contrarian_drop_shadow_logged(monkeypatch, tmp_path, capsys):
    now = int(datetime(2026, 10, 2, 5, 10, tzinfo=timezone.utc).timestamp())
    mk = _firing_market(now, bid="0.385", ask="0.415")  # edge +0.08, disagree
    _scan_env(monkeypatch, tmp_path, [mk], [], now)
    monkeypatch.setattr(sdl, "fair_15m",
                        lambda *a, **k: (0.48, 100.0, 100.5, 0.0004, 5.0, a[3]))
    main()
    out = json.loads(capsys.readouterr().out)
    assert out["signals_new"] == 0
    assert "KXBTC15M-26OCT020115-00:YES" not in load_state()["alerted"]
    log = tmp_path / "data" / "contrarian_drops.jsonl"
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["ticker"] == "KXBTC15M-26OCT020115-00"
    assert rec["side"] == "YES" and rec["edge"] == pytest.approx(0.06, abs=1e-9)
    assert rec["market_mid"] == 0.4


def test_contrarian_log_rotation_is_bounded(monkeypatch, tmp_path):
    log = tmp_path / "data" / "contrarian_drops.jsonl"
    line = '{"t": 0, "pad": "' + "y" * 90 + '"}\n'
    log.write_text(line * (sdl.CONTRARIAN_LOG_MAX_BYTES // len(line) + 10))
    assert log.stat().st_size > sdl.CONTRARIAN_LOG_MAX_BYTES
    sdl._shadow_log_contrarian({"t": 1})
    assert log.stat().st_size <= sdl.CONTRARIAN_LOG_KEEP_BYTES + 500
    lines = log.read_text().splitlines()
    assert lines[-1] == '{"t": 1}'
    assert all(l.startswith('{"t":') for l in lines)  # line-aligned


def test_shadow_log_never_raises(monkeypatch, tmp_path):
    # DATA under a regular file: makedirs raises -> swallowed, no raise,
    # and nothing is written anywhere
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(sdl, "DATA", str(blocker))
    sdl._shadow_log_contrarian({"t": 1})
