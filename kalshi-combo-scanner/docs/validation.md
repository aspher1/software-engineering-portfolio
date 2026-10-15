# Validation

## Ship bar

A change ships only when all of these hold:

1. **Full suite green.** `pytest tests/ -q` passes in a fresh shell. The
   tests are offline and cover fee-table rows, exact-cent combo math, depth
   gating, dedup and state backup, sweep splitting and coverage, the
   cross-venue guards, tier boundaries, the Tier 3 model and entry rule, the
   Tier 4 picker, and public-release hygiene.
2. **Independent audit.** A second reviewer reproduces every claim in their
   own shell. They check the diff, re-run the full suite, run a live
   `--no-mark` sweep, and confirm that no trade-capable path, credential or
   guaranteed-profit language was added. Nothing ships on the author's word
   alone.
3. **Tier 1 spot-probe.** A single-market Yes+No at 48¢ + 48¢ must be locked.
   The per-set pre-fee cost is 96¢ and exact cent-rounded taker fees are
   2¢/leg (ceil(1.7472)), so a 1-set fill costs exactly 100¢ — zero edge,
   rejected as a combo at that size
   (`test_evaluate_edge_after_fee_rejects_exact_zero_edge`). The probe locks
   because per-order fee ceil amortizes at size: a 20-set fill costs
   1,920¢ + 70¢ = 1,990¢ against a 2,000¢ payout (10¢ locked profit). The
   same market at 49¢ + 49¢ must be rejected, because fees push the all-in
   cost past the payout at every fill size (screen edge −1.5¢). This pins
   the fee math and the locked threshold.
4. **Output-contract check.** With every optional tier switched off, the
   JSON keys must be identical to the previous release. New tiers only add
   keys.
5. **Live coverage.** A live `--no-mark` sweep must report
   `coverage_complete: true` with no errors.

## No-threshold-loosening rule

Fees, depth and outlay gates, and edge thresholds are never loosened to
produce more hits. Fees are always rounded up, and series multipliers are
never taken below 1. Many sweeps find zero combos and zero picks, and that
is the normal outcome. A change that adds hits by relaxing a guard is
rejected, unless it comes with settlement-graded evidence and is labeled
for what it is.

## What was tried and falsified

- **After-fee "near-arb" thresholds above 100¢.** A same-market Yes+No pair
  always pays exactly 100¢. An early rule flagged pairs costing up to 101.5¢
  after fees. Every hit was a guaranteed loss of about −0.21¢ per set (38 of
  38 in one live sweep). The rule was replaced with a strictly **pre-fee**
  test (combined ask < 100¢), which is clearly labeled as erased by current
  taker fees.
- **Pre-fee Yes+No under 100¢ as a common event.** On Kalshi's unified book
  this requires a crossed book. Live hits are essentially never there, so
  Tier 2 Type A is expected to be empty almost always.
- **Zero-or-better Type C edges.** Pairs whose cent-rounded fees exactly
  erased the gap were reported at an edge of 0.0¢. Tier 2 Type C now
  requires a strictly positive after-fee edge, the same as Tier 1.
- **"Polymarket taker fees are zero."** This is false. Fee-enabled markets
  charge `C × rate × p × (1 − p)`, with rates of 0.03–0.07. The scanner
  charges them.
- **Non-exhaustive event books.** Ladders ("8+ / 6+ / 4+") and incomplete
  named-outcome events often sum under 100¢, but they are not arbitrage.
  Type B requires a proven exhaustive, mutually exclusive book and skips the
  rest by design.
- **A fixed maker margin over the product of asks.** Public measurements of
  combo quotes show a non-constant spread, so no constant margin can be
  subtracted. The picker uses a conservative 1¢/leg guard and labels the
  RFQ margin as unknown.
- **Spot from the trades endpoint alone.** Kraken's 1000-trade cap made
  "latest" spot several minutes stale on busy pairs. Spot now uses the newer
  of the last trade and the latest candle close, and refuses anything older
  than 180s.
- **Sweeps that silently lose coverage.** One early run covered only about
  50k of 131k markets with no warning. Coverage is now proven on every run
  (`coverage_complete`, `stats.ranges_budget_hit`).
- **A duplicated scan call after a merge.** It doubled API load on every
  run. It was caught by post-merge verification and removed.
- **Strict vs inclusive bracket bounds.** Kalshi `less` caps and `greater`
  floors are STRICT (`rules_primary`: "is less than X°" / "is greater than
  X°") while `between` is inclusive both ends. A bracket set like
  `less 3.19 / between 3.20–3.29 / greater 3.29` is NOT exhaustive —
  settlement at exactly 3.19 pays nothing on any leg — even though the old
  gap check certified it. `brackets_exhaustive` now requires strict bounds
  to meet with no gap (two strict bounds must overlap); the fix only ever
  rejects.

## What is not validated

- **Combo picks (Tier 4):** there are no graded results yet. See
  [backtest-protocol.md](backtest-protocol.md).
- **Tier 3 model-edge:** it has a one-day replay with a very small sample.
  Treat it as unvalidated; forward paper logging continues.
