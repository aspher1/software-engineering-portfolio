# W3 stale-position time-exit calibration

## Assumptions and scope

**Counterfactual convention:** For each closed trade whose recorded `(close_ts - entry_ts) > T`, assume it was still open at T and score its counterfactual net P&L as `min(0, actual_net_pnl)`. This is a stipulated pessimistic bound: a winning trade becomes breakeven at best and a losing trade keeps its full realized loss. It assumes no favorable timeout fill and uses no lifetime peak. It does **not** establish the price, liquidity, execution cost, partial take profits, or achievable P&L at T. A losing trade could have been worth less at T; a winning trade could have been worth more. A candidate is an estimated replay outcome, never a measured timeout fill. All values pass through `replay.net_pnl`, `replay.estimated_cost`, and `replay.metrics`.

The bound has an important mathematical limit: `min(0, actual_net_pnl)` can only match or reduce each recorded outcome, so **no timeout can improve P&L relative to the baseline under this convention**. The sweep measures how much historical gain is removed and where exposure is unchanged. Its rejection is not evidence that an executable time exit would fail.

Only paired rows with an entry timestamp enter the chronological split. The comparison is by recorded, timezone-naive `entry_ts`; the local/UTC regime mix can misorder entries within a day. Missing timestamps, negative durations, future timestamps, and same-day 2–5 hour durations are never treated as timeout-affected. The latter interval is deliberately censored because a four-hour EDT/UTC clock switch can mimic a long hold. This also censors genuine 2–5 hour holds. The known multi-day hold remains eligible. Metrics, including drawdown, use entry order within each cohort; they do not model changed close ordering, position capacity, capital recycling, or trades that would have been entered after an earlier exit.

Snapshot read: 2026-09-27T20:23:28+00:00. Journal has 77 closes, 75 entry rows, 3 unmatched closes, and 1 unmatched open entries. The 73-close / -$121.87 / PF 0.262 / 26% WR figure in the hypothesis was an earlier snapshot. The journal is append-only during this analysis, so these results describe this read.

## Baseline and split

All closes, `replay.metrics`: **$-123.10 / 0.275 / 26.0% / $124.65**, n=77. The paired analysis cohort has 49 IS and 25 OOS closes, with 3 unmatched closes excluded from the split. IS baseline: $-72.25 / 0.339 / 32.7% / $75.19. OOS baseline: $-48.80 / 0.147 / 8.0% / $48.80. The cut is the first two-thirds of paired rows sorted by nominal `entry_ts`; it was fixed before comparing timeouts.

## Candidate results

Each metric cell is P&L USD / PF / WR / max drawdown USD. `3×` means `replay.metrics(..., cost_multiplier=3)`, which multiplies the full estimated cost, a stronger charge than slippage alone. `Affected` is IS/OOS. Edge is P&L change per cohort trade versus the corresponding no-timeout baseline.

| T | IS metrics | OOS metrics | Affected IS/OOS | IS/OOS edge $/trade | IS 3× metrics | OOS 3× metrics | Gates |
|---|---|---|---:|---:|---|---|---|
| 3m | $-90.71 / 0.170 / 20.4% / $90.71 | $-48.80 / 0.147 / 8.0% / $48.80 | 22/4 | -0.377/+0.000 | $-134.91 / 0.088 / 6.1% / $134.91 | $-54.53 / 0.128 / 8.0% / $54.53 | FAIL: oos_edge_retention_60pct,oos_decay_le_70pct,both_chains_benefit,stress_same_verdict,broad_plateau |
| 5m | $-82.86 / 0.242 / 26.5% / $85.81 | $-48.80 / 0.147 / 8.0% / $48.80 | 16/4 | -0.217/+0.000 | $-130.22 / 0.119 / 10.2% / $131.33 | $-54.53 / 0.128 / 8.0% / $54.53 | FAIL: oos_edge_retention_60pct,oos_decay_le_70pct,both_chains_benefit,stress_same_verdict,broad_plateau |
| 10m | $-75.37 / 0.310 / 28.6% / $78.32 | $-48.80 / 0.147 / 8.0% / $48.80 | 13/3 | -0.064/+0.000 | $-123.86 / 0.162 / 12.2% / $124.97 | $-54.53 / 0.128 / 8.0% / $54.53 | FAIL: oos_edge_retention_60pct,oos_decay_le_70pct,both_chains_benefit,stress_same_verdict,broad_plateau |
| 15m | $-72.35 / 0.338 / 30.6% / $75.29 | $-48.80 / 0.147 / 8.0% / $48.80 | 9/3 | -0.002/+0.000 | $-121.92 / 0.175 / 14.3% / $123.03 | $-54.53 / 0.128 / 8.0% / $54.53 | FAIL: oos_edge_retention_60pct,oos_decay_le_70pct,both_chains_benefit,stress_same_verdict,broad_plateau |
| 30m | $-72.35 / 0.338 / 30.6% / $75.29 | $-48.80 / 0.147 / 8.0% / $48.80 | 7/2 | -0.002/+0.000 | $-121.92 / 0.175 / 14.3% / $123.03 | $-54.53 / 0.128 / 8.0% / $54.53 | FAIL: oos_edge_retention_60pct,oos_decay_le_70pct,both_chains_benefit,stress_same_verdict,broad_plateau |
| 60m | $-72.25 / 0.339 / 32.7% / $75.19 | $-48.80 / 0.147 / 8.0% / $48.80 | 1/1 | +0.000/+0.000 | $-121.92 / 0.175 / 14.3% / $123.03 | $-54.53 / 0.128 / 8.0% / $54.53 | FAIL: oos_edge_retention_60pct,oos_decay_le_70pct,both_chains_benefit,stress_same_verdict,broad_plateau |
| 120m | $-72.25 / 0.339 / 32.7% / $75.19 | $-48.80 / 0.147 / 8.0% / $48.80 | 0/1 | +0.000/+0.000 | $-121.92 / 0.175 / 14.3% / $123.03 | $-54.53 / 0.128 / 8.0% / $54.53 | FAIL: oos_edge_retention_60pct,oos_decay_le_70pct,both_chains_benefit,stress_same_verdict,broad_plateau |
| 4h | $-72.25 / 0.339 / 32.7% / $75.19 | $-48.80 / 0.147 / 8.0% / $48.80 | 0/1 | +0.000/+0.000 | $-121.92 / 0.175 / 14.3% / $123.03 | $-54.53 / 0.128 / 8.0% / $54.53 | FAIL: oos_edge_retention_60pct,oos_decay_le_70pct,both_chains_benefit,stress_same_verdict,broad_plateau |
| 8h | $-72.25 / 0.339 / 32.7% / $75.19 | $-48.80 / 0.147 / 8.0% / $48.80 | 0/1 | +0.000/+0.000 | $-121.92 / 0.175 / 14.3% / $123.03 | $-54.53 / 0.128 / 8.0% / $54.53 | FAIL: oos_edge_retention_60pct,oos_decay_le_70pct,both_chains_benefit,stress_same_verdict,broad_plateau |

Gate keys: `chronological_split` = nonempty chronological IS/OOS; `oos_edge_retention_60pct` = OOS edge at least 60% of positive IS edge; `oos_not_worse` = nonnegative OOS edge; `wr_le_90pct`; `oos_decay_le_70pct` = OOS retains at least 30% of IS edge; `both_chains_benefit` = positive OOS edge and affected trades on each chain; `stress_same_verdict` = the 60%/non-worse verdict persists at 3× estimated cost; `broad_plateau` = an adjacent timeout passes every other gate. Zero edge is not a demonstrated benefit. A single passing point would fail the plateau gate.

### OOS chain checks

| T | Solana n/affected/edge $ per trade | BSC n/affected/edge $ per trade |
|---|---:|---:|
| 3m | 2/2/+0.000 | 23/2/+0.000 |
| 5m | 2/2/+0.000 | 23/2/+0.000 |
| 10m | 2/2/+0.000 | 23/1/+0.000 |
| 15m | 2/2/+0.000 | 23/1/+0.000 |
| 30m | 2/1/+0.000 | 23/1/+0.000 |
| 60m | 2/1/+0.000 | 23/0/+0.000 |
| 120m | 2/1/+0.000 | 23/0/+0.000 |
| 4h | 2/1/+0.000 | 23/0/+0.000 |
| 8h | 2/1/+0.000 | 23/0/+0.000 |

## Look-ahead and clock audit

| Feature | Decision point | Used and knowable then? |
|---|---|---|
| `entry_ts` | At entry | Entry time is known; naive clock timezone is not encoded, so ordering within a day is uncertain. |
| `T`, elapsed time | At hypothetical T | Fixed before sweep; elapsed wall-clock time is available if one consistent clock is used. Historical naive clock switches make some durations unknowable. |
| `close_ts` | After actual close, for retrospective eligibility | Not available at T. It only proves that a paired trade was still open under a consistent clock; uncertain pairs are censored. |
| `entry`, `exit`, realized P&L, cost inputs | After close for replay scoring | Entry price/stake are known at entry; exit and realized P&L are not known at T. They set the stipulated `min(0, actual)` outcome bound, not a decision feature or fill. |
| `peak` | After close | Validated for input shape but never used by the timeout decision or counterfactual calculation. |
| Enriched entry fields | At entry where present | Not used; most historical values are null. |

Clock outcomes in this snapshot: {'missing_or_mixed_timestamp': 3, 'ambiguous_four_hour_clock': 6, 'clock_skew': 1}. All journal timestamp strings inspected lacked a UTC offset. At least one close precedes its entry by about four hours, and log event order supports an EDT/UTC switch. Thus naive durations **are affected** by timezone mixing. Six same-day 2–5 hour pairs were left unchanged; this is an explicit conservative censorship, not a timezone repair. The split itself remains vulnerable to same-day order errors, so it is insufficient evidence for shipping.

## Adversarial outcomes and operations

The hermetic tests cover 11 malformed/timing fallback cases (non-dict; None entry; NaN peak; malformed exit; missing realized; missing entry time; missing close time; entry after close; future close; ambiguous four-hour duration; mixed aware/naive timestamps), plus missing and two corrupt params files, BSC `0x` mints with `buy_sol` or `buy_bnb` stake and `realized_bnb`, Solana mints, and duplicate mints. Invalid economics are excluded; invalid timing remains in the all-closes baseline with no timeout treatment, though a missing entry time cannot join the chronological split; missing/corrupt params return a disabled rule. Every fallback occurrence increments a reason counter and writes one warning line. Duplicate mints remain independent paired rows; there is no mint-keyed aggregation. Production uses `replay.load_journal` FIFO pairing, so pairing ambiguity is inherited and cannot be resolved from these fields alone.

**Latency:** analysis only; no hot-path decision function was added. A future implementation must benchmark p50/p99 over at least 10,000 realistic Solana/BSC iterations, prove the 50 ms budget and bounded memory, and avoid per-decision file I/O before deployment.

## Verdict

**ship_recommend: false.** No timeout has a positive IS edge under the required convention, no OOS chain shows a positive improvement, and no broad passing plateau exists. The best passing candidate is **none**. The 15-minute and longer apparent OOS ties at zero edge are ties, not improvements. This is a historical-replay rejection; it says nothing about profitability or live readiness.
