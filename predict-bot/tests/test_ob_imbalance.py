"""Order-book imbalance logging (idea #1, log-first phase).

book_imbalance() turns a Kalshi orderbook (bids only; NO bids are the YES
asks at 1 - no_price) into a signed top-depth YES imbalance in [-1, 1].
entry_ob_imbalance() fetches it best-effort at alert time and never raises.
The value is logged into the alerted state entry and copied onto grade rows
only — no gate, no exit trigger, no threshold reads it, and the printed
signal payload is unchanged. Paper/analysis only; no network (all I/O
monkeypatched)."""
import json
import os
import sys
import time
from datetime import datetime, timezone

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import same_day_loop as sdl
from same_day_loop import book_imbalance, entry_ob_imbalance, main

TICKER = "KXBTC15M-26SEP301900-00"


def _book(yes, no):
    return {"orderbook_fp": {"yes_dollars": yes, "no_dollars": no}}


# ---- book_imbalance ----

def test_bid_heavy_is_positive():
    imb = book_imbalance(_book([["0.50", "300.00"], ["0.49", "100.00"]],
                               [["0.48", "100.00"]]))
    assert imb == pytest.approx((400 - 100) / 500)
    assert imb > 0


def test_ask_heavy_is_negative():
    imb = book_imbalance(_book([["0.50", "50.00"]],
                               [["0.48", "200.00"], ["0.47", "250.00"]]))
    assert imb == pytest.approx((50 - 450) / 500)
    assert imb < 0


def test_symmetric_is_zero():
    imb = book_imbalance(_book([["0.50", "100.00"], ["0.49", "100.00"]],
                               [["0.49", "100.00"], ["0.48", "100.00"]]))
    assert imb == pytest.approx(0.0)


@pytest.mark.parametrize("ob", [
    {},                                   # empty dict
    {"foo": 1},                           # no orderbook_fp, no sides
    {"orderbook_fp": None},
    {"orderbook_fp": {}},
    _book([], []),                        # both sides empty
    None, "not a book", 42, [],
])
def test_empty_or_missing_is_none(ob):
    assert book_imbalance(ob) is None


def test_single_sided_yes_only_is_plus_one():
    assert book_imbalance({"orderbook_fp": {"yes_dollars": [["0.5", "10"]]}}) == 1.0
    assert book_imbalance(_book([["0.5", "10"]], None)) == 1.0
    assert book_imbalance(_book([["0.5", "10"]], [])) == 1.0


def test_single_sided_no_only_is_minus_one():
    assert book_imbalance(_book([], [["0.5", "10"]])) == -1.0


@pytest.mark.parametrize("yes", [
    [["abc", "10"]],                      # non-numeric price
    [["0.5", "lots"]],                    # non-numeric size
    [None],                               # None level
    [["0.5", None]],                      # None size
    ["0.5"],                              # level not a list
    [["0.5"]],                            # level too short
    "0.5,10",                             # side not a list
    [["0.5", "nan"]],                     # non-finite size
    [["0.5", "-5"]],                      # negative size
])
def test_malformed_levels_are_none(yes):
    assert book_imbalance(_book(yes, [["0.4", "10"]])) is None


def test_raw_book_and_legacy_keys_accepted():
    raw = {"yes_dollars": [["0.50", "30"]], "no_dollars": [["0.40", "10"]]}
    assert book_imbalance(raw) == pytest.approx(0.5)
    legacy = {"orderbook": {"yes": [[50, 30]], "no": [[40, 10]]}}
    assert book_imbalance(legacy) == pytest.approx(0.5)


def test_depth_respected_sixth_level_ignored():
    yes5 = [[f"0.{50 - i}", "10"] for i in range(5)]
    no5 = [[f"0.{45 - i}", "10"] for i in range(5)]
    base = book_imbalance(_book(yes5, no5), depth=5)
    # a huge 6th-best YES bid must not move the depth=5 result
    yes6 = yes5 + [["0.10", "1000000"]]
    assert book_imbalance(_book(yes6, no5), depth=5) == pytest.approx(base)
    # but it counts once depth covers it
    assert book_imbalance(_book(yes6, no5), depth=6) > base


def test_levels_sorted_best_first_regardless_of_input_order():
    # best YES bid (0.50, size 90) listed last; depth=1 must still pick it
    ob = _book([["0.10", "1"], ["0.20", "1"], ["0.50", "90"]],
               [["0.05", "1"], ["0.45", "10"]])
    assert book_imbalance(ob, depth=1) == pytest.approx((90 - 10) / 100)


def test_live_shape_parses():
    ob = {"orderbook_fp": {
        "yes_dollars": [["0.9900", "100.00"], ["0.9800", "50.00"]],
        "no_dollars": [["0.0010", "354265.01"], ["0.0020", "10.00"]],
    }}
    imb = book_imbalance(ob)
    assert imb == pytest.approx((150 - 354275.01) / (150 + 354275.01))
    assert -1.0 <= imb <= 1.0


# ---- entry_ob_imbalance ----

def test_entry_ob_imbalance_connection_error_is_none(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(sdl.requests, "get", boom)
    assert entry_ob_imbalance(TICKER) is None


class _Resp:
    def __init__(self, body, status=200, bad_json=False):
        self._body, self.status_code, self._bad = body, status, bad_json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        if self._bad:
            raise ValueError("bad json")
        return self._body


def test_entry_ob_imbalance_success_and_url(monkeypatch):
    seen = {}

    def fake_get(url, *a, **k):
        seen["url"], seen["timeout"] = url, k.get("timeout")
        return _Resp(_book([["0.5", "30"]], [["0.4", "10"]]))
    monkeypatch.setattr(sdl.requests, "get", fake_get)
    assert entry_ob_imbalance(TICKER) == pytest.approx(0.5)
    assert seen["url"] == f"{sdl.KALSHI_API}/markets/{TICKER}/orderbook"
    assert seen["timeout"] == 10


@pytest.mark.parametrize("resp", [
    _Resp({}, status=404), _Resp(None, bad_json=True), _Resp({"orderbook_fp": "x"}),
])
def test_entry_ob_imbalance_http_json_parse_failures_are_none(monkeypatch, resp):
    monkeypatch.setattr(sdl.requests, "get", lambda *a, **k: resp)
    assert entry_ob_imbalance(TICKER) is None


# ---- entry path through main() ----

def _live_market(now):
    return {
        "ticker": TICKER, "title": "BTC up?",
        "open_time": datetime.fromtimestamp(now - 600, timezone.utc).isoformat(),
        "close_time": datetime.fromtimestamp(now + 300, timezone.utc).isoformat(),
        "yes_bid_dollars": "0.51", "yes_ask_dollars": "0.53",
        "volume_fp": "1000",
    }


def _patch(tmp_path, monkeypatch):
    sp = tmp_path / "state.json"
    gp = tmp_path / "grades.jsonl"
    sp.write_text(json.dumps({"alerted": {}}))
    monkeypatch.setattr(sdl, "STATE_PATH", str(sp))
    monkeypatch.setattr(sdl, "GRADE_PATH", str(gp))
    now = int(time.time())
    monkeypatch.setattr(sdl, "kalshi_same_day", lambda today: [_live_market(now)])
    monkeypatch.setattr(sdl, "poly_same_day", lambda today: [])
    # raw fair 0.60, mid 0.52 -> shrunk 0.58 -> edge 0.06 -> YES fires
    monkeypatch.setattr(sdl, "fair_15m",
                        lambda *a: (0.60, 100.0, 100.0, 0.001, 5.0, a[3]))
    return sp


def _run_main(monkeypatch, capsys, argv=("same_day_loop.py",)):
    monkeypatch.setattr(sys, "argv", list(argv))
    main()
    return json.loads(capsys.readouterr().out)


def test_orderbook_failure_never_blocks_signal(tmp_path, monkeypatch, capsys):
    sp = _patch(tmp_path, monkeypatch)
    calls = []

    def get(url, *a, **k):
        calls.append(url)
        if url.endswith("/orderbook"):
            raise requests.ConnectionError("orderbook down")
        raise AssertionError(f"unexpected network call: {url}")
    monkeypatch.setattr(sdl.requests, "get", get)
    out = _run_main(monkeypatch, capsys)
    assert out["ok"] is True
    assert out["signals_new"] == 1 and out["new_signals"]
    assert calls == [f"{sdl.KALSHI_API}/markets/{TICKER}/orderbook"]
    e = json.loads(sp.read_text())["alerted"][f"{TICKER}:YES"]
    assert "ob_imb_at_entry" in e and e["ob_imb_at_entry"] is None
    assert e["fair_raw"] == 0.6 and e["edge"] == 0.06


def test_entry_records_imbalance_but_payload_unchanged(tmp_path, monkeypatch, capsys):
    sp = _patch(tmp_path, monkeypatch)
    monkeypatch.setattr(sdl, "entry_ob_imbalance", lambda t: 0.123456)
    out = _run_main(monkeypatch, capsys)
    e = json.loads(sp.read_text())["alerted"][f"{TICKER}:YES"]
    assert e["ob_imb_at_entry"] == 0.123          # rounded to 3dp
    sig = out["new_signals"][0]
    assert "ob_imb_at_entry" not in sig           # stdout contract unchanged
    assert set(sig) == {"type", "ticker", "title", "side", "market_mid", "fair",
                        "edge", "spread", "volume", "mins_left",
                        "window_close_utc", "new"}


def test_raising_fetch_is_contained(tmp_path, monkeypatch, capsys):
    sp = _patch(tmp_path, monkeypatch)

    def explode(t):
        raise RuntimeError("should be contained")
    monkeypatch.setattr(sdl, "entry_ob_imbalance", explode)
    out = _run_main(monkeypatch, capsys)
    assert out["signals_new"] == 1
    e = json.loads(sp.read_text())["alerted"][f"{TICKER}:YES"]
    assert e["ob_imb_at_entry"] is None


def test_no_fetch_when_no_signal(tmp_path, monkeypatch, capsys):
    _patch(tmp_path, monkeypatch)
    # fair == mid -> no edge -> no signal -> orderbook never fetched
    monkeypatch.setattr(sdl, "fair_15m", lambda *a: (0.52, 100.0, 100.0, 0.001, 5.0, a[3]))
    called = []
    monkeypatch.setattr(sdl, "entry_ob_imbalance", lambda t: called.append(t))
    out = _run_main(monkeypatch, capsys)
    assert out["signals_new"] == 0 and called == []


def test_no_mark_never_fetches_or_writes(tmp_path, monkeypatch, capsys):
    sp = _patch(tmp_path, monkeypatch)
    before = sp.read_bytes()
    called = []
    monkeypatch.setattr(sdl, "entry_ob_imbalance", lambda t: called.append(t))
    out = _run_main(monkeypatch, capsys, ("same_day_loop.py", "--no-mark"))
    assert out["ok"] is True and out["signals_new"] == 1
    assert called == [] and sp.read_bytes() == before


# ---- grade flow ----

def test_grade_row_carries_ob_imb_through(tmp_path, monkeypatch):
    monkeypatch.setattr(sdl, "GRADE_PATH", str(tmp_path / "grades.jsonl"))
    monkeypatch.setattr(sdl, "kalshi_settlement", lambda ticker: "NO")
    now = int(time.time())
    ts = datetime.fromtimestamp(now - 1200, timezone.utc).isoformat()
    alerted = {
        "KXBTC15M-26SEP301700-00:NO": {
            "ts": ts, "ticker": "KXBTC15M-26SEP301700-00", "side": "NO",
            "edge": -0.10, "market_mid": 0.60, "fair_raw": 0.50,
            "ob_imb_at_entry": -0.42,
            "window_open_utc": now - 2100, "window_close_utc": now - 1200,
        },
        # legacy entry without the new field -> None
        "KXETH15M-26SEP301700-00:YES": {
            "ts": ts, "ticker": "KXETH15M-26SEP301700-00", "side": "YES",
            "edge": 0.08, "market_mid": 0.30,
            "window_open_utc": now - 2100, "window_close_utc": now - 1200,
        },
    }
    new, _ = sdl.grade_pending(alerted, now, mark=True)
    by_key = {r["key"]: r for r in new}
    assert by_key["KXBTC15M-26SEP301700-00:NO"]["ob_imb_at_entry"] == -0.42
    assert by_key["KXETH15M-26SEP301700-00:YES"]["ob_imb_at_entry"] is None
    # grading math untouched by the new field
    assert by_key["KXBTC15M-26SEP301700-00:NO"]["correct"] is True
    assert by_key["KXETH15M-26SEP301700-00:YES"]["correct"] is False
