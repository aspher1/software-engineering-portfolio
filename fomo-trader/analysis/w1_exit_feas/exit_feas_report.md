# W1 — Pre-entry exit-feasibility simulation: REJECTED

Date: 2026-09-27. Implementer: Codex (`/usr/bin/codex exec --model gpt-6-sol`).
Coordinator verification: Codex's sandbox session was read-only, so it
produced no files; the coordinator re-implemented from Codex's method,
re-ran the full validation in a writable shell, and wrote the deliverables.
Verdict is independent of which agent typed the code: the numbers reject it.

## Hypothesis
From the 2026-09-27 deep-research report (high confidence): exit failure
kills more than entry failure. Simulate the exit before entering: estimate
the slippage the planned position would suffer on exit from entry-time
depth data, and skip the entry if it exceeds a calibrated threshold
(research suggested 20–30% as a starting point).

## Method
- Estimator: constant-product average execution impact =
  `100 * position_usd / (liquidity_usd / 2 + position_usd)`, maxed with a
  15m-volume absorption bound. Pure stdlib, local, <50ms, fail-open
  (missing data → approve).
- Position USD from journal: `buy_sol*sol_usd`, `buy_bnb*bnb_usd`, or
  `buy_native * commit_price_usd / commit_price_native`.
- Thresholds swept: 10, 15, 20, 25, 30, 40%.
- Validation: 71 matched closes (3 unmatched closes excluded), chronological
  47 IS / 24 OOS, `replay.net_pnl` realistic costs, 3× slippage stress.

## Results
- Scorable: 49 of 71 entries (69%). 22 entries lack liquidity/position data
  → fail open (approved).
- Estimated exit impacts: min 0.0095%, median 0.0412%, **max 0.1697%**.
- Every threshold 10–40% retained every trade. Filtered sets are byte-
  identical to baseline:

| Threshold | IS P&L / PF / WR / DD | OOS P&L / PF / WR / DD |
|---|---|---|
| 10–40% (all identical) | -$69.36 / 0.348 / 34.0% / $75.79 | -$52.11 / 0.090 / 4.2% / $52.11 |
| Baseline (no veto) | identical | identical |
| OOS, 3× slippage | — | -$58.50 / 0.077 |

- Per-trade edge over baseline: **$0 IS, $0 OOS**. No IS edge exists to
  retain; stressed OOS is unchanged.

## Verdict: REJECT (`ship_recommend: false`)
1. The veto cannot fire: max impact 0.1697% is ~60× below the lowest
   threshold. A filter that never fires is not a filter.
2. Structural cause: ~$7 positions vs a $15k minimum entry-liquidity floor.
   Exit impact is negligible by construction at this size.
3. No 60%-retention or stress gate can be passed when the rule changes
   nothing.

## What would make this hypothesis testable
Position sizes ≥$50–100, or a much lower liquidity floor, would move
estimated impacts into a range where thresholds discriminate. Neither is
on the table (position sizes are never increased to chase paper profit).
Revisit only if the operating range changes; the estimator in
`exit_feas.py` is kept for that day.

## Files
- `analysis/w1_exit_feas/exit_feas.py` — estimator, `Params`, fail-open
  `approve()` (fallbacks logged + counted, lock-guarded), `evaluate()`.
- `analysis/w1_exit_feas/exit_feas.json` — params, numbers, `ship_recommend: false`.
- `analysis/w1_exit_feas/STAFF_REVIEW.md` — staff-review doc: verdict, three
  strongest objections + answers, latency/concurrency/look-ahead audits.
- `tests/test_w1_exit_feas.py` — 21 hermetic tests incl. adversarial cases,
  thread-safety, <50ms timing.

## Audits (detail in STAFF_REVIEW.md)
- **Latency (measured, n=20k):** fast path p50 1.42µs / p99 2.03µs;
  fallback path p50 17.8µs / p99 49µs (logging-dominated); 0 KiB RSS growth
  over 200k calls. ~25,000× inside the 50ms budget on the fast path.
- **Adversarial:** NaN/Inf/string payloads, corrupt params, Solana-vs-BSC
  shapes, empty 15m windows, duplicate mints, hostile entry objects,
  8-thread counter hammering — all pass, every fallback explicit.
- **Concurrency:** cached frozen `Params` (race-free); only shared state is
  `fallback_counts` under one non-nested lock; no fast-path locks; entry
  dict never mutated.
- **Look-ahead:** all features verified pre-decision by reading the entry
  path (signal snapshot pre-entry; commit fields at fill; label post-decision
  as correct). Signal-snapshot staleness noted — biases optimistic,
  strengthens rejection.
