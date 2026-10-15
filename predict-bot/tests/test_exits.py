import sys, os, time, json
import threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest

import same_day_loop as sdl
from same_day_loop import decide_exit, check_exits, EXIT_EDGE


# ---------- decide_exit: pure logic ----------

@pytest.mark.parametrize("side,mid,fair,expected", [
    ("YES", 0.50, 0.60, None),        # healthy edge, hold
    ("YES", 0.50, 0.53, None),        # small edge (below entry gate) still holds: hysteresis
    ("YES", 0.55, 0.55, "edge_gone"), # edge exactly zero
    ("YES", 0.56, 0.55, "edge_gone"), # tiny reversal counts as gone
    ("YES", 0.60, 0.55, "edge_reversed"),
    ("NO", 0.50, 0.40, None),         # NO edge +0.10, hold
    ("NO", 0.53, 0.50, None),         # NO edge +0.03, hold (hysteresis)
    ("NO", 0.47, 0.50, "edge_reversed"),  # NO edge -0.03: reversed past the -0.02 line
    ("NO", 0.45, 0.45, "edge_gone"),
    ("NO", 0.44, 0.45, "edge_gone"),
    ("NO", 0.40, 0.45, "edge_reversed"),
])
def test_decide_exit_matrix(side, mid, fair, expected):
    assert decide_exit(side, mid, fair) == expected


def test_exit_edge_constant_is_zero():
    assert EXIT_EDGE == 0.0


# ---------- check_exits: integration with stubbed fair_15m ----------

def _alerted_entry(side, edge, mid, mins_left=10.0):
    now = int(time.time())
    return {
        f"KXBTC15M-26SEP301800-00:{side}": {
            "ts": "2026-09-30T21:50:00+00:00", "ticker": "KXBTC15M-26SEP301800-00",
            "side": side, "edge": edge, "market_mid": mid,
            "window_open_utc": now - 300, "window_close_utc": now + int(mins_left * 60),
        }
    }


def _live(ticker="KXBTC15M-26SEP301800-00", bid=0.50, ask=0.51, sym="BTC"):
    now = int(time.time())
    m = {"ticker": ticker, "title": "BTC price up?",
         "yes_bid_dollars": bid, "yes_ask_dollars": ask}
    return {ticker: (m, sym, now - 300, now + 600)}


def _stub_fair(fair):
    def _f(pair, w_open, w_close, now_ts):
        return (fair, 100.0, 100.0, 0.001, 10.0, now_ts)
    return _f


def test_exit_fires_and_stamps(monkeypatch):
    # entered YES at edge +0.10; now mid 0.60 vs fair 0.55 -> taken-side edge -0.05
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    alerted = _alerted_entry("YES", 0.10, 0.50)
    now = int(time.time())
    exits = check_exits(alerted, _live(bid=0.595, ask=0.605), now, mark=True)
    assert len(exits) == 1
    e = exits[0]
    assert e["side"] == "YES" and e["reason"] == "edge_reversed"
    # stub fair 0.55 is inside the shrink band: 0.55 + 0.25*(0.60-0.55) = 0.5625,
    # stored rounded to 3dp
    assert e["now_edge"] == pytest.approx(-0.0375, abs=1e-3)
    assert e["entry_edge"] == 0.10 and e["entry_mid"] == 0.50
    key = "KXBTC15M-26SEP301800-00:YES"
    assert e["signal_key"] == key
    assert e["exit_yes_bid"] == pytest.approx(.595)
    assert e["exit_yes_ask"] == pytest.approx(.605)
    assert e["exit_bid_taken"] == pytest.approx(.595)
    assert e["quoted_spread_taken"] == pytest.approx(.01)
    assert alerted[key]["exit_ts"] and alerted[key]["exit_reason"] == "edge_reversed"


def test_no_exit_when_edge_healthy(monkeypatch):
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.60))
    alerted = _alerted_entry("YES", 0.10, 0.50)
    exits = check_exits(alerted, _live(bid=0.495, ask=0.505), int(time.time()), mark=True)
    assert exits == []
    assert "exit_ts" not in alerted["KXBTC15M-26SEP301800-00:YES"]


def test_no_mark_never_stamps(monkeypatch):
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    alerted = _alerted_entry("YES", 0.10, 0.50)
    exits = check_exits(alerted, _live(bid=0.595, ask=0.605), int(time.time()), mark=False)
    assert len(exits) == 1
    assert "exit_ts" not in alerted["KXBTC15M-26SEP301800-00:YES"]


@pytest.mark.parametrize("side,edge,entry_mid,bid,ask,fair,entry_price,exit_price,exit_bid,now_fair,now_edge", [
    ("YES", .10, .50, .595, .605, .55, .50, .60, .595, .5625, -.0375),
    ("NO", -.10, .60, .395, .405, .45, .40, .60, .595, .5625, -.0375),
])
def test_exit_event_record_taken_side_and_stamp_once(
        monkeypatch, tmp_path, side, edge, entry_mid, bid, ask, fair,
        entry_price, exit_price, exit_bid, now_fair, now_edge):
    path = tmp_path / "events.jsonl"
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(fair))
    alerted = _alerted_entry(side, edge, entry_mid)
    now = int(time.time())
    live = _live(bid=bid, ask=ask)
    exits = check_exits(alerted, live, now, mark=True, exit_event_path=str(path))
    assert len(exits) == 1
    assert check_exits(alerted, live, now, mark=True,
                       exit_event_path=str(path)) == []
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 1
    rec = records[0]
    assert set(rec) == {
        "signal_key", "ticker", "sym", "side", "entry_ts",
        "entry_price_taken", "entry_edge_taken", "exit_ts", "exit_reason",
        "exit_price_taken", "now_fair_taken", "now_edge", "exit_yes_bid",
        "exit_yes_ask", "exit_bid_taken", "quoted_spread_taken",
        "exit_direction", "mins_left", "window_open_utc", "window_close_utc"}
    assert rec["signal_key"] == f"KXBTC15M-26SEP301800-00:{side}"
    assert rec["ticker"] == "KXBTC15M-26SEP301800-00"
    assert rec["sym"] == "BTC" and rec["side"] == side
    assert rec["entry_ts"] == alerted[rec["signal_key"]]["ts"]
    assert rec["entry_price_taken"] == pytest.approx(entry_price)
    assert rec["entry_edge_taken"] == pytest.approx(.10)
    assert rec["exit_ts"] == alerted[rec["signal_key"]]["exit_ts"]
    assert rec["exit_reason"] == exits[0]["reason"]
    assert rec["exit_price_taken"] == pytest.approx(exit_price)
    assert rec["now_fair_taken"] == pytest.approx(now_fair, abs=.001)
    assert rec["now_edge"] == pytest.approx(now_edge, abs=.001)
    assert rec["exit_yes_bid"] == pytest.approx(bid)
    assert rec["exit_yes_ask"] == pytest.approx(ask)
    assert rec["exit_bid_taken"] == pytest.approx(exit_bid)
    assert rec["quoted_spread_taken"] == pytest.approx(ask - bid)
    assert rec["exit_direction"] == "favourable"
    assert rec["window_open_utc"] == alerted[rec["signal_key"]]["window_open_utc"]
    assert rec["window_close_utc"] == alerted[rec["signal_key"]]["window_close_utc"]
    assert rec["mins_left"] == pytest.approx(10, abs=.1)


def test_exit_event_no_mark_writes_nothing(monkeypatch, tmp_path):
    path = tmp_path / "events.jsonl"
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(.55))
    alerted = _alerted_entry("YES", .10, .50)
    assert len(check_exits(alerted, _live(bid=.595, ask=.605),
                           int(time.time()), mark=False,
                           exit_event_path=str(path))) == 1
    assert not path.exists()


def test_exit_event_write_failure_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(sdl.os, "makedirs", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read-only")))
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(.55))
    alerted = _alerted_entry("YES", .10, .50)
    exits = check_exits(alerted, _live(bid=.595, ask=.605), int(time.time()),
                        mark=True, exit_event_path=str(tmp_path / "missing" / "events.jsonl"))
    assert len(exits) == 1
    assert alerted[exits[0]["signal_key"]]["exit_ts"]


def test_already_exited_skipped(monkeypatch):
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    alerted = _alerted_entry("YES", 0.10, 0.50)
    key = "KXBTC15M-26SEP301800-00:YES"
    alerted[key]["exit_ts"] = "2026-09-30T21:55:00+00:00"
    exits = check_exits(alerted, _live(bid=0.595, ask=0.605), int(time.time()), mark=True)
    assert exits == []


def test_closed_window_skipped(monkeypatch):
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    alerted = _alerted_entry("YES", 0.10, 0.50)
    key = "KXBTC15M-26SEP301800-00:YES"
    alerted[key]["window_close_utc"] = int(time.time()) - 60  # already closed
    exits = check_exits(alerted, _live(bid=0.595, ask=0.605), int(time.time()), mark=True)
    assert exits == []


def test_last_minute_skipped(monkeypatch):
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    alerted = _alerted_entry("YES", 0.10, 0.50, mins_left=0.5)
    exits = check_exits(alerted, _live(bid=0.595, ask=0.605), int(time.time()), mark=True)
    assert exits == []


def test_cross_venue_key_skipped(monkeypatch):
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    alerted = {"xvenue:KXBTC15M-26SEP301800-00":
               {"ts": "2026-09-30T21:50:00+00:00", "ticker": "KXBTC15M-26SEP301800-00"}}
    exits = check_exits(alerted, _live(bid=0.595, ask=0.605), int(time.time()), mark=True)
    assert exits == []


def test_missing_live_ticker_skipped(monkeypatch):
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    alerted = _alerted_entry("YES", 0.10, 0.50)
    exits = check_exits(alerted, {}, int(time.time()), mark=True)
    assert exits == []


def test_no_side_exits_sorted_most_urgent_first(monkeypatch):
    now = int(time.time())
    alerted = {
        "KXBTC15M-26SEP301800-00:NO": {
            "ts": "t", "ticker": "KXBTC15M-26SEP301800-00", "side": "NO",
            "edge": -0.10, "market_mid": 0.60,
            "window_open_utc": now - 300, "window_close_utc": now + 600},
        "KXETH15M-26SEP301800-00:NO": {
            "ts": "t", "ticker": "KXETH15M-26SEP301800-00", "side": "NO",
            "edge": -0.08, "market_mid": 0.55,
            "window_open_utc": now - 300, "window_close_utc": now + 600},
    }
    live = dict(_live("KXBTC15M-26SEP301800-00", bid=0.495, ask=0.505))
    live.update(_live("KXETH15M-26SEP301800-00", bid=0.595, ask=0.605, sym="ETH"))
    # BTC: fair 0.55 -> NO edge = -(0.55-0.50) = -0.05 (reversed)
    # ETH: fair 0.615 -> NO edge = -(0.615-0.60) = -0.015 (gone)
    def _f(pair, w_open, w_close, now_ts):
        return ((0.55 if pair == "XBTUSD" else 0.615), 100.0, 100.0, 0.001, 10.0)
    monkeypatch.setattr(sdl, "fair_15m", _f)
    # shrink_midrange only touches 0.40-0.60 fairs; patch it for determinism.
    monkeypatch.setattr(sdl, "shrink_midrange", lambda f, m, **k: f)
    exits = check_exits(alerted, live, now, mark=True)
    assert [e["ticker"] for e in exits] == [
        "KXBTC15M-26SEP301800-00", "KXETH15M-26SEP301800-00"]
    assert exits[0]["reason"] == "edge_reversed"
    assert exits[1]["reason"] == "edge_gone"


# ---------- open_signals: pure filtering ----------

def _mk_alerted(now):
    return {
        "KXBTC15M-26SEP301800-00:YES": {
            "ts": "t", "ticker": "KXBTC15M-26SEP301800-00", "side": "YES",
            "edge": 0.10, "market_mid": 0.50,
            "window_open_utc": now - 300, "window_close_utc": now + 600},
        "KXETH15M-26SEP301800-00:NO": {
            "ts": "t", "ticker": "KXETH15M-26SEP301800-00", "side": "NO",
            "edge": -0.08, "market_mid": 0.55,
            "window_open_utc": now - 300, "window_close_utc": now + 600,
            "exit_ts": "2026-09-30T21:55:00+00:00"},
        "KXSOL15M-26SEP301745-45:YES": {
            "ts": "t", "ticker": "KXSOL15M-26SEP301745-45", "side": "YES",
            "edge": 0.06, "market_mid": 0.40,
            "window_open_utc": now - 1200, "window_close_utc": now - 300},
        "KXDOGE15M-26SEP301800-00:YES": {
            "ts": "t", "ticker": "KXDOGE15M-26SEP301800-00", "side": "YES",
            "edge": 0.07, "market_mid": 0.30,
            "window_open_utc": now - 840, "window_close_utc": now + 30},
        "xvenue:KXBTC15M-26SEP301800-00": {
            "ts": "t", "ticker": "KXBTC15M-26SEP301800-00"},
    }


def test_open_signals_filters():
    from same_day_loop import open_signals
    now = int(time.time())
    opens = open_signals(_mk_alerted(now), now)
    # only the first entry qualifies: second exited, third closed,
    # fourth has <1 min left, fifth has no side
    assert list(opens) == ["KXBTC15M-26SEP301800-00"]


# ---------- exits_only_run ----------

def test_exits_only_all_quotes_fail_is_failed_run(monkeypatch, capsys, tmp_path):
    from same_day_loop import exits_only_run
    def _boom(ticker):
        raise RuntimeError("429 rate limit")
    monkeypatch.setattr(sdl, "kalshi_market", _boom)
    # mark=True re-reads state from disk inside the lock: seed a tmp state.
    monkeypatch.setattr(sdl, "STATE_PATH", str(tmp_path / "state.json"))
    now = int(time.time())
    sdl.save_state({"alerted": _mk_alerted(now)})
    exits_only_run({}, {}, now, mark=True)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["mode"] == "exits_only"


def test_exits_only_detects_and_marks(monkeypatch, capsys, tmp_path):
    from same_day_loop import exits_only_run
    monkeypatch.setattr(sdl, "kalshi_market",
                        lambda tk: {"ticker": tk, "title": "BTC price up?",
                                    "yes_bid_dollars": 0.595, "yes_ask_dollars": 0.605})
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    # mark=True re-reads state from disk inside the lock: seed a tmp state.
    monkeypatch.setattr(sdl, "STATE_PATH", str(tmp_path / "state.json"))
    now = int(time.time())
    sdl.save_state({"alerted": _mk_alerted(now)})
    exits_only_run({}, {}, now, mark=True)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["exits_new"] == 1
    assert out["exits"][0]["ticker"] == "KXBTC15M-26SEP301800-00"
    stamped = sdl.load_state()["alerted"]["KXBTC15M-26SEP301800-00:YES"]
    assert stamped["exit_ts"]
    assert stamped["exit_reason"] == "edge_reversed"


def test_exits_only_no_opens_is_ok(monkeypatch, capsys, tmp_path):
    from same_day_loop import exits_only_run
    monkeypatch.setattr(sdl, "STATE_PATH", str(tmp_path / "state.json"))
    sdl.save_state({"alerted": {}})
    now = int(time.time())
    exits_only_run({}, {}, now, mark=True)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["exits_new"] == 0


# ---------- FIX 2a: taken-side fields in the exit payload ----------

def test_exit_taken_side_fields_yes(monkeypatch):
    # YES take: taken-side fields equal the YES-oriented ones.
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    alerted = _alerted_entry("YES", 0.10, 0.50)
    exits = check_exits(alerted, _live(bid=0.595, ask=0.605),
                        int(time.time()), mark=False)
    assert len(exits) == 1
    e = exits[0]
    assert e["side"] == "YES" and e["reason"] == "edge_reversed"
    assert e["entry_price_taken"] == pytest.approx(0.50)
    assert e["entry_edge_taken"] == pytest.approx(0.10)
    assert e["now_price_taken"] == pytest.approx(0.60)
    # stub fair 0.55 shrinks to 0.5625 against mid 0.60 (see
    # test_exit_fires_and_stamps); now_edge is taken-side as before
    assert e["now_fair_taken"] == pytest.approx(0.5625, abs=1e-3)
    assert e["now_edge"] == pytest.approx(-0.0375, abs=1e-3)
    # deprecated YES-oriented fields still present for backward compat
    assert e["entry_edge"] == 0.10 and e["entry_mid"] == 0.50
    assert e["now_mid"] == pytest.approx(0.60)
    assert e["now_fair"] == pytest.approx(0.5625, abs=1e-3)


def test_exit_taken_side_fields_no_eth_example(monkeypatch):
    # Real-world shape that motivated the fix: a NO take entered at YES-mid
    # 0.415 with YES-oriented edge -0.077 must report the taken-side entry
    # 58.5c @ +7.7, not "41.5c @ -7.7".
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    alerted = _alerted_entry("NO", -0.077, 0.415)
    exits = check_exits(alerted, _live(bid=0.545, ask=0.555),
                        int(time.time()), mark=False)
    assert len(exits) == 1
    e = exits[0]
    assert e["side"] == "NO" and e["reason"] == "edge_gone"
    assert e["entry_price_taken"] == pytest.approx(0.585)
    assert e["entry_edge_taken"] == pytest.approx(0.077)
    assert e["now_price_taken"] == pytest.approx(0.45)
    assert e["now_fair_taken"] == pytest.approx(0.45)
    assert e["now_edge"] == pytest.approx(0.0, abs=1e-3)
    # deprecated YES-oriented fields still present for backward compat
    assert e["entry_edge"] == pytest.approx(-0.077)
    assert e["entry_mid"] == pytest.approx(0.415)
    assert e["now_mid"] == pytest.approx(0.55)
    assert e["now_fair"] == pytest.approx(0.55)
    assert e["exit_bid_taken"] == pytest.approx(.445)
    assert e["quoted_spread_taken"] == pytest.approx(.01)


# ---------- FIX 2b: duplicate-exit race ----------

def test_concurrent_exit_watch_emits_exactly_once(tmp_path, monkeypatch):
    """Two stale readers racing through the locked mark path: exactly one
    exit is emitted in total and the on-disk state carries exactly one
    exit stamp (regression test for the double-fire race)."""
    monkeypatch.setattr(sdl, "STATE_PATH", str(tmp_path / "state.json"))
    # NO take whose taken-side edge has collapsed: mid 0.55 vs shrunk fair
    # 0.55 -> edge_gone.
    monkeypatch.setattr(sdl, "kalshi_market",
                        lambda tk: {"ticker": tk, "title": "SOL price up?",
                                    "yes_bid_dollars": 0.545,
                                    "yes_ask_dollars": 0.555})
    monkeypatch.setattr(sdl, "fair_15m", _stub_fair(0.55))
    now = int(time.time())
    key = "KXSOL15M-26SEP301800-00:NO"
    sdl.save_state({"alerted": {key: {
        "ts": "2026-09-30T21:50:00+00:00",
        "ticker": "KXSOL15M-26SEP301800-00",
        "side": "NO", "edge": -0.08, "market_mid": 0.55,
        "window_open_utc": now - 300, "window_close_utc": now + 600}}})
    barrier = threading.Barrier(2)
    counts = []

    def worker():
        _ = sdl.load_state()  # stale pre-lock read, like the racing processes
        barrier.wait()        # both readers loaded before either decides
        payload = sdl.exits_only_locked(int(time.time()))
        counts.append(payload["exits_new"])

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(counts) == 1, f"duplicate exit fired: {counts}"
    final = sdl.load_state()["alerted"][key]
    assert final.get("exit_ts")
    assert final.get("exit_reason") == "edge_gone"
    # the losing writer emitted nothing and stamped nothing new
    assert counts.count(1) == 1 and counts.count(0) == 1
