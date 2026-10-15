#!/usr/bin/env python3
"""Apply the proposed journal diff in a temporary directory and test it."""
import importlib.util
import math
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIFF = ROOT / 'analysis/iter2_pool_created_at_patch.diff'

with tempfile.TemporaryDirectory() as tmp:
    target = Path(tmp) / 'journal_proposed.py'
    subprocess.run(['patch', '--silent', '--output', str(target),
                    str(ROOT / 'journal.py'), str(DIFF)], check=True)
    spec = importlib.util.spec_from_file_location('journal_proposed', target)
    journal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(journal)

    cases = [
        ({'pool_created_at': '2026-09-28T16:25:16Z'}, 1790612716.0),
        ({'created_at': '2026-09-28T18:25:16+02:00'}, 1790612716.0),
        ({'pairCreatedAt': 1790612716000}, 1790612716.0),
        ({'poolCreatedAt': 1790612716}, 1790612716.0),
        ({'created_at': 'bad-date'}, None),
        ({'created_at': '2026-09-28T16:25:16'}, None),
    ]
    for payload, expected in cases:
        actual = journal.pool_created_at(payload)
        assert actual == expected, (payload, actual, expected)
    print(f'PASS: {len(cases)} proposed-patch pool_created_at cases')
