# X3 staff review

## What was built

An offline, read-only paper-journal re-test using the existing replay and signal-join functions. `x3_lowgain.py` implements a deterministic 47/24 chronological split for the current 71-row snapshot, the fixed 75/100/125/150 sweep, costed and threefold-slippage metrics, concentration and OOS-regime checks, one preregistered ratio combination, and a fail-open evaluator with counted fallbacks. The JSON contains all measured results and `ship_recommend=false`; the evaluator is not wired into the bot. Hermetic tests cover the join, UTC ordering, strict veto boundary, malformed inputs and parameters, and empty populations.

## Three strongest objections to shipping

1. **The OOS policy loses money despite a positive relative edge.** At IS-selected T=100, seven OOS trades lose $13.94 after estimated costs and $14.21 under threefold slippage. The edge improvement retains just 8.7% of IS. The ratio combination still loses $6.87 costed and $7.12 stressed. **Answer:** This objection stands. Comparing per-trade averages with a worse baseline does not make a losing subset shippable.

2. **The IS win is a small local cluster.** Binancians and musebook entered 91 seconds apart and contribute $16.43, more than the selected bucket's $14.16 net. Removing both makes IS -$2.27. In OOS only Bukangi is positive; Solana and the early OOS half are negative, while the late half has just two retained trades. **Answer:** This objection stands. A passing directional threshold plateau is not independent temporal or chain replication.

3. **The evidence is observational paper data with mixed signal windows and estimated execution costs.** Four eligible signals are labeled `5m` despite the `m15_gain_pct` key. Matching is reconstructed from names, log times, and launcher timezone markers; vetoes can change future available capital. Estimated costs and paper closes do not prove live fills. **Answer:** The timestamp and commit-field audit rules out an obvious look-ahead leak for these matched rows, but it cannot remove selection, matching, or execution uncertainty. This objection also stands.

**Recommendation: do not ship a low-gain veto.** Leave bot behavior unchanged; no live-readiness or profitability claim is supported.
