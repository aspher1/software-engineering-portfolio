import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import combo_scan as cs  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Keep every test off the real disk caches (fee multipliers, sweep plan)."""
    monkeypatch.setattr(cs, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(cs, "_FEE_MULT_LIVE", {})
    monkeypatch.setattr(cs, "_FEE_MULT_FAILED", set())
    monkeypatch.setattr(cs.cp, "PICK_LOG_PATH", str(tmp_path / "data" / "picker_picks.jsonl"))
