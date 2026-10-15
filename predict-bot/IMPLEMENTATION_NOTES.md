# Implementation notes — 6 prioritized improvements (2026-10-04)

Source: `IMPROVEMENT_ANALYSIS.md`. Implemented by Codex (`gpt-6-sol`) on
2026-10-04 ~18:10 EDT; coordinator fixed 2 test issues and verified the
full suite green (468 passed).

Standing rule: **paper/analysis only**. Nothing here places orders or moves
money. Improvements #1, #2, #4 are **shadow-only**: they log beside the
live path and never change signal/exit decisions.

## What changed

### #1 Shadow probability calibration (shadow only)
- `scripts/same_day_loop.py`: new `calibrated_taken_price(mid, edge, side)`
  computing `taken + 0.4 * abs(edge)` in taken-side 0–1 units.
- Logged on every state entry and copied to every grade row as
  `fair_calibrated_taken`. The live `fair`/edge decision path is untouched.
- `accuracy_stats` reports `mean_calibrated_probability` beside win rate.

### #2 Persistence-gate shadow test (shadow only)
- New `update_persistence_shadow()` / `record_persistence_shadow()`.
- First eligible scan (edge ≥5¢ taken, mid ≥50¢ taken, ≥2 min left) is
  recorded per ticker/side/window; a second scan ≥90s later decides
  `would_accept` / `would_reject`.
- Logs to `data/persistence_shadow_log.jsonl` (+ state file
  `data/persistence_shadow_state.json`). Real signals are emitted exactly
  as before — the gate never delays or suppresses anything.

### #3 Quote-side exit price logging
- `check_exits` now logs on every exit record: `signal_key` (stable
  signal↔exit link), `exit_yes_bid`, `exit_yes_ask`, `entry_ask_taken`,
  `exit_bid_taken` (side-correct: YES exit at bid; NO exit at `1 − YES ask`),
  `quoted_spread_taken`.
- Mid-based exit logic unchanged; these fields enable future quote-side
  policy comparison (hold vs current exits vs edge_reversed-only).

### #4 XRP suppression shadow test (shadow only)
- Every signal record carries `xrp_shadow_suppressed = (sym == "XRP")`.
- XRP signals are still emitted and graded normally. No filtering.

### #5 Hold-guard arithmetic + wording (direct fix)
- `scripts/hold_guard.py`: new `pullback_fields()` returns separately named
  `peak_to_now_drop` and `entry_to_now_pnl = now_price_taken − entry_price_taken`
  (taken-side terms, YES/NO correct).
- Alert wording: "protect paper profit" only when `entry_to_now_pnl > 0`;
  "cut the paper loss" when negative; "adverse move" otherwise.
- Covered by parametrized YES/NO profitable/losing pullback tests.

### #6 Price-aware monitoring
- `accuracy_stats` now also returns `mean_taken_price` (existed),
  `mean_calibrated_probability`, `mean_paper_mid_payoff`, `independent_windows`,
  `entry_policy_cohort` — all reporting only, never read by decisions.

## Test changes
- New `tests/test_prioritized_improvements.py`: calibration math, shadow
  accept/reject timing, hold-guard wording matrix, accuracy reporting.
- `tests/test_exits.py`: asserts new quote-side exit fields (fixed a
  use-before-assign of `key` left by Codex).
- `tests/test_iter11_ab.py`, `tests/test_iter12_d1a_shadow.py`: new additive
  keys added to the `NEW_KEYS`/`PRIORITIZED_KEYS` allowlists per the
  documented pattern (verified all diffs are new keys only, no decision changes).

## How to validate each prospectively
1. Calibration: fit/verify the 0.4 coefficient on chronologically later
   Kalshi-settled windows; compare Brier + calibration tables.
2. Persistence: compare `persistence_shadow_log.jsonl` accept/reject cohorts
   on payoff (quote-side), not just win rate, over several full days.
3. Quote-side exits: re-evaluate hold vs current-exit vs edge_reversed-only
   at `exit_bid_taken` with fees.
4. XRP: compare total paper payoff per available window with/without the
   shadow-suppressed cohort over future full-day cohorts.
5. Hold guard: already unit-tested; watch live alerts for wording sanity.
6. Monitoring: alert on price-adjusted deterioration over multiple
   nonoverlapping windows, not 3-win wiggles in 50.

## Not claimed
No profitability or live-readiness is inferred from paper-mid results.
Spread, fees, and quote-side execution remain unmeasured in live P&L.
