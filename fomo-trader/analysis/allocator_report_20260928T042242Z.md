# Allocator analyst report (2026-09-28 04:22:42 UTC)

**Verdict: `fail`**. Do not flip to live. Keep `allocator.mode = "shadow"`.

Paper journal only. Every number below is comparative (allocator vs 1.0x baseline on the same trades). Nothing here is a profitability claim.

## Data

- Journal: `/home/hatch/workspace/fomo-trader/runs/paper-1h/trades.jsonl` (read-only), 128 entries, 130 closes; 106 priced closes used (24 unpriced, 0 without timestamp, 3 closes with no matching entry -> features neutral).
- Entries already carrying allocator fields (shadow/live): 27.
- Feature coverage (trades with a non-missing value): signal_gain_pct 106, liquidity_usd 106, buy_count_15m 60, sell_count_15m 59, buy_sell_ratio 102, volume_15m_usd 60, mcap_usd 60, holder_top1_pct 0, holder_top5_pct 0, signal_to_fill_slippage_pct 60, entry_latency_sec 60, chain_bsc 78

## Recency discipline

Training uses the FULL journal with exponential recency weights (half-life 14 days). In this journal the oldest trade weighs 0.839 and the newest 1.000.

Recent window (last 32 closes): -1.321 $/trade, win rate 38%. Older journal (74 closes): -0.791 $/trade, win rate 28%. Full journal: -0.951 $/trade. Difference -0.530 $/trade (z = -0.9).

Recent results are not better than the older journal; there is no recency-concentrated improvement to discount.

## P&L attribution (journal realized_usd, before replay costs)

### allocator score (current config weights)

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 0.452 .. 0.489 | 21 | -1.308 | 10% | -27.47 |
| Q2 | 0.489 .. 0.498 | 21 | -0.208 | 62% | -4.36 |
| Q3 | 0.498 .. 0.511 | 21 | -0.802 | 29% | -16.85 |
| Q4 | 0.511 .. 0.525 | 21 | -2.476 | 14% | -52.00 |
| Q5 | 0.526 .. 0.577 | 22 | -0.006 | 41% | -0.13 |

### signal_gain_pct

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 18.9 .. 54.6 | 21 | -0.308 | 57% | -6.47 |
| Q2 | 55.6 .. 103 | 21 | -0.887 | 38% | -18.64 |
| Q3 | 103 .. 171 | 21 | -1.349 | 29% | -28.32 |
| Q4 | 173 .. 254 | 21 | -1.143 | 24% | -24.00 |
| Q5 | 257 .. 2335 | 22 | -1.063 | 9% | -23.38 |

### liquidity_usd

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 8272 .. 27109 | 21 | +0.525 | 57% | +11.02 |
| Q2 | 27848 .. 43535 | 21 | -1.122 | 24% | -23.57 |
| Q3 | 44951 .. 57433 | 21 | -0.769 | 33% | -16.14 |
| Q4 | 59161 .. 80421 | 21 | -1.847 | 19% | -38.79 |
| Q5 | 84169 .. 1.59e+05 | 22 | -1.515 | 23% | -33.32 |

### buy_sell_ratio

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 1.5 .. 1.6 | 21 | -1.154 | 29% | -24.23 |
| Q2 | 1.7 .. 1.9 | 21 | -0.713 | 43% | -14.96 |
| Q3 | 2 .. 2.6 | 21 | -0.351 | 43% | -7.36 |
| Q4 | 2.6 .. 4.2 | 21 | -0.914 | 29% | -19.20 |
| Q5 | 4.2 .. 13.2 | 22 | -1.593 | 14% | -35.04 |

### chain

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| bsc | - | 78 | -1.238 | 24% | -96.54 |
| solana | - | 28 | -0.152 | 50% | -4.27 |

## Proposed weights (full journal, recency-weighted)

Method: L2 (lambda=0.10) logistic regression of P(win) on normalized features, deterministic gradient descent; each weight = change in win log-odds per unit of normalized feature. Intercept re-centered so the recency-weighted average trade scores 0.5 (1.0x). take_threshold = 0.35.

| feature | current | proposed (full fit) | IS-only fit (gated) |
|---|---:|---:|---:|
| intercept | +0.0000 | +0.2924 | +0.4072 |
| signal_gain_pct | -0.1000 | -0.3810 | -0.4758 |
| liquidity_usd | +0.1500 | -0.2118 | -0.2638 |
| buy_count_15m | +0.0500 | -0.1447 | -0.1595 |
| sell_count_15m | +0.0000 | -0.0484 | -0.0875 |
| buy_sell_ratio | +0.1000 | -0.2329 | -0.2430 |
| volume_15m_usd | +0.0500 | -0.1443 | -0.0843 |
| mcap_usd | +0.0000 | -0.1691 | -0.0621 |
| holder_top1_pct | -0.1500 | +0.0000 | +0.0000 |
| holder_top5_pct | -0.1000 | +0.0000 | +0.0000 |
| signal_to_fill_slippage_pct | -0.1500 | -0.3564 | -0.2115 |
| entry_latency_sec | -0.0500 | -0.0799 | -0.0372 |
| chain_bsc | +0.0000 | -0.2897 | -0.4647 |

## Validation gate

Costs: entry 100 bps + exit 300 bps of notional, plus fixed fee per trade bsc $0.20, solana $0.60. Linear ticket scaling (m x realized). The money.py risk ceiling and balance-aware sizing are future replay extensions; upsizing here is an upper bound.

IS window 2026-09-24 10:56:20 .. 2026-09-27 20:05:09 (74 closes); OOS window 2026-09-27 20:10:10 .. 2026-09-28 00:17:03 (32 closes). Weights refit on IS only.

| window | n | taken | base $/trade | alloc $/trade | edge $/trade | base WR | alloc WR (taken) | base maxDD $ | alloc maxDD $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IS | 74 | 73 | -1.423 | -1.289 | +0.134 | 28% | 29% | 105.27 | 95.39 |
| OOS | 32 | 30 | -1.757 | -1.510 | +0.247 | 38% | 40% | 56.23 | 48.31 |

| check | value | threshold | result |
|---|---:|---|---|
| is_edge_positive | 0.1335 | > 0 | PASS |
| oos_edge_positive | 0.2475 | > 0 | PASS |
| oos_retention | 1.8539 | >= 0.60 | PASS |
| oos_decay | -0.8539 | <= 0.70 | PASS |
| is_win_rate_not_overfit | 0.2877 | <= 0.90 | PASS |
| edge_not_single_chain | 1.0 | <= 0.80 (if evaluable) | FAIL |
| edge_not_single_time_regime | 0.4815 | <= 0.80 (if evaluable) | PASS |

Edge by chain (IS-fit replay, $): {'solana': -2.3, 'bsc': 20.1}. Edge by time tercile ($): {'0': 5.43, '1': 3.8, '2': 8.57}.
Chain concentration includes comparative edge from shrinking or skipping trades on a weak chain; that is not evidence of within-chain ranking skill.

In-sample only (informational, not gated): proposed full-fit weights on the whole journal: edge +0.198 $/trade, 101/106 taken.

## Apply (manual; only on a `pass` verdict)

1. Copy `weights` from `analysis/allocator_proposal.json` into the `allocator.weights` section of `runs/paper-1h/config.json`.
2. Set `allocator.mode` to `"live"`.
3. Restart the bot so it reloads config. `dry_run` stays `true`.

This script never edits the config.
