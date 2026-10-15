import json
import time

import pytest

from analysis.w4_wash_detect import wash_detect as wd


def _params(tmp_path, *, ship=True, sol=(2.0, 10000.0), bsc=(4.0, 20000.0)):
    path = tmp_path / "wash.json"
    path.write_text(json.dumps({"ship_recommend": ship, "thresholds": {
        "solana": {"wash_proxy": sol[0], "min_volume_usd": sol[1]},
        "bsc": {"wash_proxy": bsc[0], "min_volume_usd": bsc[1]},
    }}))
    wd._params_cache.clear()
    wd.reset_fallback_counts()
    return path


def test_score_math_and_reject_invalid_counts():
    assert wd.wash_proxy(10, 10) == 10
    assert wd.wash_proxy(9, 3) == .5
    assert wd.wash_proxy(0, 0) == 0
    for bad in (-1, 1.5, float("nan"), True, "5"):
        with pytest.raises(ValueError):
            wd.wash_proxy(bad, 5)


def test_chain_specific_thresholds_and_volume_floor(tmp_path):
    path = _params(tmp_path)
    signal = {"m15_buys": 12, "m15_sells": 8, "m15_volume_usd": 15000}
    assert not wd.approve({**signal, "chain": "solana"}, path=path)
    assert wd.approve({**signal, "chain": "bsc"}, path=path)
    assert wd.approve({**signal, "chain": "unknown"}, path=path)
    assert wd.approve({**signal, "chain": "solana", "m15_volume_usd": 9999}, path=path)
    assert not wd.approve({**signal, "chain": "bsc", "m15_buys": 20,
                           "m15_sells": 16, "m15_volume_usd": 20000}, path=path)


@pytest.mark.parametrize("change", [
    {"m15_buys": None}, {"m15_sells": None}, {"m15_volume_usd": None},
    {"m15_buys": -1}, {"m15_sells": "bad"},
    {"m15_volume_usd": float("nan")}, {"m15_volume_usd": True},
])
def test_missing_or_bad_evidence_fails_open(tmp_path, change):
    path = _params(tmp_path)
    signal = {"chain": "solana", "m15_buys": 12, "m15_sells": 8,
              "m15_volume_usd": 15000}
    assert wd.approve({**signal, **change}, path=path)


@pytest.mark.parametrize("content", [None, "{bad", "{}"])
def test_bad_file_fails_open(tmp_path, content):
    path = tmp_path / "wash.json"
    if content is not None:
        path.write_text(content)
    wd._params_cache.clear()
    wd.reset_fallback_counts()
    assert wd.approve({"chain": "solana", "m15_buys": 10,
                       "m15_sells": 10, "m15_volume_usd": 99999}, path=path)


@pytest.mark.parametrize("signal", [None, [], "bad", {"chain": []},
                                             {"chain": "solana", "m15_buys": object()}])
def test_malformed_signal_never_raises(tmp_path, signal):
    assert wd.approve(signal, path=_params(tmp_path))


@pytest.mark.parametrize("mutation", [
    lambda x: x.pop("bsc"),
    lambda x: x["solana"].update(wash_proxy=20),
    lambda x: x["solana"].update(min_volume_usd=-1.0),
    lambda x: x["bsc"].update(extra=1),
])
def test_schema_rejects_bad_thresholds(tmp_path, mutation):
    path = _params(tmp_path)
    data = json.loads(path.read_text())
    mutation(data["thresholds"])
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        wd.load_params(path)


def test_schema_rejects_bad_ship_flag(tmp_path):
    path = _params(tmp_path)
    data = json.loads(path.read_text())
    data["ship_recommend"] = 1
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        wd.load_params(path)


def test_disabled_params_pass_all(tmp_path):
    path = _params(tmp_path, ship=False)
    assert wd.approve({"chain": "solana", "m15_buys": 100,
                       "m15_sells": 100, "m15_volume_usd": 100000}, path=path)


def test_approve_under_50ms(tmp_path):
    path = _params(tmp_path)
    signal = {"chain": "solana", "m15_buys": 12, "m15_sells": 8,
              "m15_volume_usd": 15000}
    start = time.perf_counter()
    assert not wd.approve(signal, path=path)
    assert time.perf_counter() - start < .05
    start = time.perf_counter()
    for _ in range(1000):
        wd.approve(signal, path=path)
    assert (time.perf_counter() - start) / 1000 < .05


def test_fallbacks_are_counted_and_logged(tmp_path):
    path = _params(tmp_path)
    lines = []
    sig = {"chain": "solana", "m15_buys": 12, "m15_sells": 8,
           "m15_volume_usd": 15000}
    assert wd.approve({**sig, "m15_buys": None}, path=path, log=lines.append)
    assert wd.approve("bad", path=path, log=lines.append)
    assert wd.approve({**sig, "chain": "nope"}, path=path, log=lines.append)
    assert wd.approve({**sig, "m15_sells": -3}, path=path, log=lines.append)
    assert wd.fallback_counts() == {"missing_evidence": 1, "non_dict_signal": 1,
                                    "unknown_chain": 1, "bad_counts": 1}
    assert lines == ["WASH FALLBACK missing_evidence",
                     "WASH FALLBACK non_dict_signal",
                     "WASH FALLBACK unknown_chain",
                     "WASH FALLBACK bad_counts"]


def test_disabled_state_is_not_a_fallback(tmp_path):
    path = _params(tmp_path, ship=False)
    assert wd.approve({"chain": "solana", "m15_buys": 100,
                       "m15_sells": 100, "m15_volume_usd": 100000}, path=path)
    assert wd.fallback_counts() == {}


def test_bad_params_counted(tmp_path):
    path = tmp_path / "wash.json"
    path.write_text("{bad")
    wd._params_cache.clear()
    wd.reset_fallback_counts()
    assert wd.approve({"chain": "solana", "m15_buys": 10,
                       "m15_sells": 10, "m15_volume_usd": 99999}, path=path)
    assert wd.fallback_counts() == {"bad_params": 1}


def test_unhashable_chain_counted(tmp_path):
    path = _params(tmp_path)
    assert wd.approve({"chain": []}, path=path)
    assert wd.fallback_counts() == {"unhashable_chain": 1}


def test_fallback_counter_thread_safe(tmp_path):
    import threading
    path = _params(tmp_path)
    sig = {"chain": "solana", "m15_buys": None, "m15_sells": 8,
           "m15_volume_usd": 15000}
    n_threads, n_calls = 8, 500

    def hammer():
        for _ in range(n_calls):
            assert wd.approve(sig, path=path)

    threads = [threading.Thread(target=hammer) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wd.fallback_counts() == {"missing_evidence": n_threads * n_calls}
