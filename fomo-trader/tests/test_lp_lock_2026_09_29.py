"""LP-lock/burn screen for BSC entries (default OFF).

BscSwap.lp_lock_status against a mocked w3 (no network), and enter_bsc
wiring on the hermetic golden-bytes entry harness from test_z3_obs. Every
test runs with socket connects disabled.
"""
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import bsc_swap  # noqa: E402
import test_z3_obs as z3  # noqa: E402
from bsc_swap import BscSwap  # noqa: E402

TOKEN = "0x1111111111111111111111111111111111111111"
PAIR = "0x2222222222222222222222222222222222222222"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted in test")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


class _Call:
    def __init__(self, w3, name, args, value):
        self.w3, self.name, self.args, self.value = w3, name, args, value

    def call(self):
        self.w3.calls.append((self.name, self.args))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class _Functions:
    def __init__(self, w3, address, abi):
        self._w3, self._address, self._abi = w3, address, abi

    def getPair(self, a, b):
        assert self._abi is bsc_swap._FACTORY_ABI
        return _Call(self._w3, "getPair", (self._address, a, b),
                     self._w3.pair)

    def totalSupply(self):
        assert self._abi is bsc_swap._PAIR_ABI
        return _Call(self._w3, "totalSupply", (self._address,),
                     self._w3.total)

    def balanceOf(self, owner):
        assert self._abi is bsc_swap._PAIR_ABI
        return _Call(self._w3, "balanceOf", (self._address, owner),
                     self._w3.dead)


class FakeW3:
    """Just enough of web3's contract surface for lp_lock_status."""

    def __init__(self, pair=PAIR, total=1000, dead=600):
        self.pair, self.total, self.dead = pair, total, dead
        self.calls = []
        self.eth = SimpleNamespace(contract=self._contract)

    def _contract(self, address, abi):
        return SimpleNamespace(functions=_Functions(self, address, abi))


def make_swap(w3):
    s = BscSwap.__new__(BscSwap)
    s.w3 = w3
    s.key_file = "/nonexistent/should-never-be-read"
    s._key_bytes = None
    s._address = None
    s._decimals_cache = {}
    return s


# -- BscSwap.lp_lock_status ------------------------------------------------

def test_factory_constant_is_pancake_v2_mainnet():
    assert bsc_swap.PANCAKE_V2_FACTORY == \
        "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
    assert Web3.to_checksum_address(bsc_swap.PANCAKE_V2_FACTORY) == \
        bsc_swap.PANCAKE_V2_FACTORY


def test_burned_lp_at_or_above_threshold():
    w3 = FakeW3(total=1000, dead=600)
    st = make_swap(w3).lp_lock_status(TOKEN)
    assert st == {"pair": Web3.to_checksum_address(PAIR),
                  "burn_pct": 60.0, "verdict": "burned"}
    assert make_swap(FakeW3(total=1000, dead=500)).lp_lock_status(
        TOKEN)["verdict"] == "burned"


def test_low_burn_pct_is_unlocked():
    st = make_swap(FakeW3(total=1000, dead=10)).lp_lock_status(TOKEN)
    assert st["verdict"] == "unlocked"
    assert st["burn_pct"] == pytest.approx(1.0)
    assert st["pair"] == Web3.to_checksum_address(PAIR)


def test_threshold_argument_is_respected():
    s = make_swap(FakeW3(total=1000, dead=600))
    assert s.lp_lock_status(TOKEN, 80.0)["verdict"] == "unlocked"
    assert s.lp_lock_status(TOKEN, 60.0)["verdict"] == "burned"


def test_calls_are_sequential_reads_on_the_right_contracts():
    w3 = FakeW3()
    make_swap(w3).lp_lock_status(TOKEN)
    assert [c[0] for c in w3.calls] == ["getPair", "totalSupply", "balanceOf"]
    assert w3.calls[0][1] == (bsc_swap.PANCAKE_V2_FACTORY,
                              Web3.to_checksum_address(TOKEN),
                              Web3.to_checksum_address(bsc_swap.WBNB))
    assert w3.calls[1][1] == (Web3.to_checksum_address(PAIR),)
    assert w3.calls[2][1] == (Web3.to_checksum_address(PAIR),
                              bsc_swap.DEAD_ADDRESS)


@pytest.mark.parametrize("pair", [bsc_swap.ZERO_ADDRESS, None, ""])
def test_no_pair(pair):
    w3 = FakeW3(pair=pair)
    assert make_swap(w3).lp_lock_status(TOKEN) == {"pair": None,
                                                   "verdict": "no_pair"}
    assert [c[0] for c in w3.calls] == ["getPair"]


@pytest.mark.parametrize("field", ["pair", "total", "dead"])
def test_rpc_exception_is_unknown_never_raises(field):
    w3 = FakeW3()
    setattr(w3, field, ConnectionError("rpc down"))
    st = make_swap(w3).lp_lock_status(TOKEN)
    assert st["verdict"] == "unknown"
    assert "ConnectionError" in st["error"] and "rpc down" in st["error"]
    assert "burn_pct" not in st


@pytest.mark.parametrize("total,dead", [(0, 0), (100, 200), (100, -1),
                                        (None, 5), (100, "5")])
def test_implausible_supply_is_unknown(total, dead):
    st = make_swap(FakeW3(total=total, dead=dead)).lp_lock_status(TOKEN)
    assert st["verdict"] == "unknown" and st["error"]


def test_bad_token_or_missing_w3_is_unknown():
    assert make_swap(FakeW3()).lp_lock_status("not-an-address")[
        "verdict"] == "unknown"
    s = BscSwap.__new__(BscSwap)
    assert s.lp_lock_status(TOKEN)["verdict"] == "unknown"


def test_budget_exceeded_is_unknown(monkeypatch):
    monkeypatch.setattr(bsc_swap, "LP_LOCK_BUDGET_SEC", -1.0)
    w3 = FakeW3()
    st = make_swap(w3).lp_lock_status(TOKEN)
    assert st["verdict"] == "unknown" and "TimeoutError" in st["error"]
    assert [c[0] for c in w3.calls] == ["getPair"]


def test_never_loads_a_key():
    s = make_swap(FakeW3())
    with patch.object(BscSwap, "_ensure_key") as ek, \
         patch("keystore.load_evm_key") as lk:
        for w3 in (FakeW3(), FakeW3(dead=1), FakeW3(pair=None),
                   FakeW3(total=RuntimeError("x"))):
            s.w3 = w3
            s.lp_lock_status(TOKEN)
    ek.assert_not_called()
    lk.assert_not_called()
    assert s._key_bytes is None and s._address is None


# -- enter_bsc wiring ------------------------------------------------------

class RecordingBsc:
    """Stub BSC client for enter_bsc that records lp_lock_status calls."""

    def __init__(self, status=None, honeypot_ok=True, raises=None):
        self.status, self.honeypot_ok, self.raises = status, honeypot_ok, raises
        self.lp_calls = []

    def honeypot_check(self, *args):
        return (True, "ok") if self.honeypot_ok else (False, "trap")

    def token_decimals(self, mint):
        return 18

    def quote_buy(self, *args):
        return 1_000_000 * 10**18

    def lp_lock_status(self, token, threshold_pct=50.0):
        self.lp_calls.append((token, threshold_pct))
        if self.raises:
            raise self.raises
        return self.status


def run_bsc_entry(tmp_path, bsc, entry_cfg):
    t = z3.entry_trader(tmp_path, "bsc")
    if entry_cfg is not z3.ABSENT:
        t.cfg["hunter"]["entry"] = entry_cfg
    t.bscswap = lambda: bsc
    with patch("fomo_trader.time", z3._FrozenTime()):
        t.enter_bsc(z3.signal("bsc"))
    trades = tmp_path / "trades.jsonl"
    lines = trades.read_text().splitlines() if trades.exists() else []
    cands = tmp_path / "candidates.jsonl"
    events = ([json.loads(x) for x in cands.read_text().splitlines()]
              if cands.exists() else [])
    return t, lines, events


def golden_bsc():
    return json.loads(z3.GOLDEN_ENTRY["bsc"])


@pytest.mark.parametrize("entry_cfg", [z3.ABSENT, {}, {"verify_lp_lock": False},
                                       {"verify_lp_lock": None},
                                       {"verify_lp_lock": 0}])
def test_flag_off_never_calls_screen_and_is_byte_identical(tmp_path,
                                                           entry_cfg):
    bsc = RecordingBsc(status={"pair": PAIR, "burn_pct": 1.0,
                               "verdict": "unlocked"})
    t, lines, events = run_bsc_entry(tmp_path, bsc, entry_cfg)
    assert bsc.lp_calls == []
    assert lines == [z3.GOLDEN_ENTRY["bsc"]]
    assert events == []
    assert json.dumps(t.state["positions"]["0xMINT"]) == \
        json.dumps(z3.golden_position("bsc"))


def test_flag_off_real_client_makes_no_rpc_traffic(tmp_path):
    w3 = FakeW3(dead=1)
    swap = make_swap(w3)
    swap.honeypot_check = lambda *a: (True, "ok")
    swap.token_decimals = lambda mint: 18
    swap.quote_buy = lambda *a: 1_000_000 * 10**18
    with patch.object(BscSwap, "lp_lock_status",
                      wraps=swap.lp_lock_status) as spy:
        _, lines, _ = run_bsc_entry(tmp_path, swap, z3.ABSENT)
    spy.assert_not_called()
    assert w3.calls == []
    assert lines == [z3.GOLDEN_ENTRY["bsc"]]


def test_flag_on_unlocked_skips_entry(tmp_path):
    bsc = RecordingBsc(status={"pair": PAIR, "burn_pct": 3.5,
                               "verdict": "unlocked"})
    t, lines, events = run_bsc_entry(tmp_path, bsc, {"verify_lp_lock": True})
    assert bsc.lp_calls == [("0xMINT", 50.0)]
    assert lines == []
    assert t.state["positions"] == {}
    assert t.state["trades_today"] == [] and t.state["trades_this_hour"] == []
    assert t.pending_entries == set()
    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == "lp_lock_skip" and ev["mint"] == "0xMINT"
    assert ev["lp_lock_verdict"] == "unlocked"
    assert ev["lp_lock_burn_pct"] == 3.5 and ev["lp_pair"] == PAIR


def test_flag_on_burned_enters_and_journals_additively(tmp_path):
    bsc = RecordingBsc(status={"pair": PAIR, "burn_pct": 99.0,
                               "verdict": "burned"})
    t, lines, events = run_bsc_entry(tmp_path, bsc, {"verify_lp_lock": True})
    assert len(lines) == 1 and events == []
    rec = json.loads(lines[0])
    assert rec.pop("lp_lock_verdict") == "burned"
    assert rec.pop("lp_lock_burn_pct") == 99.0
    assert rec == golden_bsc()
    assert rec["lp_burn_pct"] is None


@pytest.mark.parametrize("status", [
    {"pair": None, "verdict": "no_pair"},
    {"verdict": "unknown", "error": "ConnectionError: rpc down"},
    None,
    "garbage",
    {},
])
def test_flag_on_inconclusive_fails_open(tmp_path, status):
    bsc = RecordingBsc(status=status)
    t, lines, events = run_bsc_entry(tmp_path, bsc, {"verify_lp_lock": True})
    assert len(bsc.lp_calls) == 1
    assert "0xMINT" in t.state["positions"]
    assert len(lines) == 1 and events == []
    rec = json.loads(lines[0])
    expected = status.get("verdict", "unknown") if isinstance(status, dict) \
        else "unknown"
    assert rec.pop("lp_lock_verdict") == (expected or "unknown")
    assert rec.pop("lp_lock_burn_pct") is None
    assert rec == golden_bsc()


def test_flag_on_screen_exception_fails_open(tmp_path):
    bsc = RecordingBsc(raises=RuntimeError("boom"))
    t, lines, _ = run_bsc_entry(tmp_path, bsc, {"verify_lp_lock": True})
    assert "0xMINT" in t.state["positions"]
    assert json.loads(lines[0])["lp_lock_verdict"] == "unknown"


def test_screen_runs_after_honeypot_check(tmp_path):
    bsc = RecordingBsc(status={"pair": PAIR, "burn_pct": 99.0,
                               "verdict": "burned"}, honeypot_ok=False)
    t, lines, _ = run_bsc_entry(tmp_path, bsc, {"verify_lp_lock": True})
    assert bsc.lp_calls == []
    assert lines == [] and t.state["positions"] == {}


def test_threshold_read_from_config(tmp_path):
    bsc = RecordingBsc(status={"pair": PAIR, "burn_pct": 99.0,
                               "verdict": "burned"})
    run_bsc_entry(tmp_path, bsc, {"verify_lp_lock": True,
                                  "lp_burn_threshold_pct": 90.0})
    assert bsc.lp_calls == [("0xMINT", 90.0)]


@pytest.mark.parametrize("w3,entered,verdict", [
    (FakeW3(total=1000, dead=10), False, "unlocked"),
    (FakeW3(total=1000, dead=900), True, "burned"),
    (FakeW3(pair=bsc_swap.ZERO_ADDRESS), True, "no_pair"),
    (FakeW3(total=ConnectionError("rpc down")), True, "unknown"),
])
def test_end_to_end_with_real_lp_lock_status(tmp_path, w3, entered, verdict):
    swap = make_swap(w3)
    swap.honeypot_check = lambda *a: (True, "ok")
    swap.token_decimals = lambda mint: 18
    swap.quote_buy = lambda *a: 1_000_000 * 10**18
    sig_mint = "0x" + "ab" * 20
    t = z3.entry_trader(tmp_path, "bsc")
    t.cfg["hunter"]["entry"] = {"verify_lp_lock": True}
    t.bscswap = lambda: swap
    sig = dict(z3.signal("bsc"), mint=sig_mint)
    with patch.object(BscSwap, "_ensure_key") as ek, \
         patch("fomo_trader.time", z3._FrozenTime()):
        t.enter_bsc(sig)
    ek.assert_not_called()
    assert (sig_mint in t.state["positions"]) is entered
    if entered:
        rec = json.loads((tmp_path / "trades.jsonl").read_text())
        assert rec["lp_lock_verdict"] == verdict
    else:
        assert not (tmp_path / "trades.jsonl").exists()
        ev = json.loads((tmp_path / "candidates.jsonl").read_text())
        assert ev["lp_lock_verdict"] == verdict


# -- journal helpers -------------------------------------------------------

@pytest.mark.parametrize("bad", [None, "x", 5, [], {"verdict": 3},
                                 {"verdict": "burned", "burn_pct": "nan"}])
def test_journal_helpers_never_raise(bad):
    import journal
    f = journal.lp_lock_fields(bad)
    assert set(f) == {"lp_lock_verdict", "lp_lock_burn_pct"}
    assert f["lp_lock_burn_pct"] is None
    ev = journal.sanitize_lp_lock_skip({"name": "X"}, bad)
    assert ev["event"] == "lp_lock_skip"


# -- "measure" mode: journal the verdict, never skip ------------------------

def test_measure_mode_unlocked_journals_and_enters(tmp_path):
    bsc = RecordingBsc(status={"pair": PAIR, "burn_pct": 3.5,
                               "verdict": "unlocked"})
    with patch("fomo_trader.log") as lg:
        t, lines, events = run_bsc_entry(tmp_path, bsc,
                                         {"verify_lp_lock": "measure"})
    assert bsc.lp_calls == [("0xMINT", 50.0)]
    assert "0xMINT" in t.state["positions"]
    assert len(lines) == 1 and events == []  # no skip event
    rec = json.loads(lines[0])
    assert rec.pop("lp_lock_verdict") == "unlocked"
    assert rec.pop("lp_lock_burn_pct") == 3.5
    assert rec == golden_bsc()
    logged = " ".join(str(c.args[0]) for c in lg.call_args_list)
    assert "LP-LOCK MEASURE" in logged and "LP-LOCK SKIP" not in logged


def test_measure_mode_burned_journals_burned(tmp_path):
    bsc = RecordingBsc(status={"pair": PAIR, "burn_pct": 99.0,
                               "verdict": "burned"})
    t, lines, events = run_bsc_entry(tmp_path, bsc,
                                     {"verify_lp_lock": "measure"})
    assert "0xMINT" in t.state["positions"] and events == []
    rec = json.loads(lines[0])
    assert rec["lp_lock_verdict"] == "burned"
    assert rec["lp_lock_burn_pct"] == 99.0


@pytest.mark.parametrize("entry_cfg", [{"verify_lp_lock": "off"},
                                       {"verify_lp_lock": ""}])
def test_measure_off_strings_never_call_screen(tmp_path, entry_cfg):
    bsc = RecordingBsc(status={"pair": PAIR, "burn_pct": 1.0,
                               "verdict": "unlocked"})
    t, lines, events = run_bsc_entry(tmp_path, bsc, entry_cfg)
    assert bsc.lp_calls == []
    assert lines == [z3.GOLDEN_ENTRY["bsc"]]
    assert events == []
