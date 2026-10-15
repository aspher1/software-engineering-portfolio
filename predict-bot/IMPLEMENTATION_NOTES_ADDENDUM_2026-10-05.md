# Implementation notes addendum — 2026-10-05

## Item #1: edge-gate ladder challenger (shadow only)

`scripts/same_day_loop.py` now records `gate_edge5_pass`, `gate_edge7_pass`, and `gate_edge10_pass` on each newly emitted paper signal's state entry, using the unrounded absolute edge in fractional 0–1 units. Settled grade rows copy the same flags. These fields are never read by entry, exit, ranking, or alert decisions.

The motivating out-of-sample cohort was the chronological last 60% of 647 Kalshi-settled rows, graded 2026-10-02 through 2026-10-05: the live ≥5¢ rung had +7.38¢ paper-mid payoff per signal (n=647, 76.0% wins), a hypothetical ≥7¢ rung had +10.22¢ (n=427, 77.0%), and ≥10¢ had +13.83¢ (n=257, 77.8%). This is a prospective experiment, not a filter or evidence of executable profitability. Validate on future full-day settled cohorts by comparing **total paper payoff per available window** across all three rungs, including windows with no qualifying signal. Keep the current decision path until multi-day prospective validation.

## Item #2: durable exit event log (additive live infrastructure)

`scripts/same_day_loop.py` now appends one JSON record per newly stamped paper exit to `data/exit_events.jsonl` when `check_exits(..., mark=True)` returns exits. It writes under a `.lock` file with `fcntl.flock`; any logging failure is swallowed. `mark=False` writes nothing. The records include stable signal linkage, the stamp timestamp, side-correct taken prices and edges, YES bid/ask, taken-side exit bid, spread, direction, and window context. The log is observational and cannot alter signal, exit, grade, or alert decisions.

Validate prospectively by joining `data/exit_events.jsonl` on `signal_key` to settled grades and re-running hold versus current-exit versus reversed-only policies at `exit_bid_taken` with fees. Treat the result as quote-side paper analysis, not a claim of fills or live readiness.

## Files and verification

- `scripts/same_day_loop.py`: both independent additions.
- `tests/test_prioritized_improvements.py`, `tests/test_iter12_d1a_shadow.py`: gate boundary, side, signal-state, and grade-copy checks.
- `tests/test_exits.py`: YES/NO exit records, no-mark, failure isolation, and stamp-once checks.
- `tests/test_iter11_ab.py`, `tests/test_iter12_d1a_shadow.py`: allowlist extensions for new additive record keys only. The comparison still pins all existing values and decision behavior.
- `IMPLEMENTATION_NOTES_ADDENDUM_2026-10-05.md`: this record.

Full suite: `.venv/bin/python -m pytest tests/ -q` — **479 passed**.
