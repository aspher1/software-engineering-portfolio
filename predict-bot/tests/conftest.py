"""Keep the suite offline: the scanner's alert-time orderbook fetch
(entry_ob_imbalance) would otherwise hit Kalshi from any test that runs
main() in mark mode with a firing signal. Orderbook URLs fail fast with
ConnectionError (-> ob_imb_at_entry None); every other call is delegated
untouched. Tests may still monkeypatch requests.get themselves."""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import same_day_loop as sdl


@pytest.fixture(autouse=True)
def _offline_orderbook(monkeypatch):
    real_get = requests.get

    def guarded_get(url, *args, **kwargs):
        if str(url).rstrip("/").endswith("/orderbook"):
            raise requests.ConnectionError("orderbook fetch disabled in tests")
        return real_get(url, *args, **kwargs)

    monkeypatch.setattr(requests, "get", guarded_get)


@pytest.fixture(autouse=True)
def _redirect_sdl_data_dir(tmp_path, monkeypatch):
    """Hermeticity: no test may write into the repo's real data/ dir.

    A mark-mode main() run writes data/latest_fair.json every scan, and the
    live flip analyst trusts that file for 180 s — a pytest mark-run would
    otherwise inject a fake model snapshot into the live analyst's context
    (iter-19 lane-cursor finding). Redirect sdl.DATA to a per-test tmp dir;
    tests that need their own DATA keep patching it themselves (later
    monkeypatch wins).
    """
    fake_data = tmp_path / "sdl_data"  # not "data": tests may create their
    fake_data.mkdir()                # own tmp_path/"data" subdir themselves
    monkeypatch.setattr(sdl, "DATA", str(fake_data))


@pytest.fixture(autouse=True)
def _offline_kalshi_pacing(monkeypatch):
    import kalshi_client
    monkeypatch.setattr(kalshi_client.limiter, "acquire", lambda: None)
