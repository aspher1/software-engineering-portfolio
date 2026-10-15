# Allocator analyst report (2026-09-28 00:12:12 UTC)

**Verdict: `fail`**. Do not flip to live. Keep `allocator.mode = "shadow"`.

Paper journal only. Every number below is comparative (allocator vs 1.0x baseline on the same trades). Nothing here is a profitability claim.

## Data

- Journal: `/home/hatch/workspace/fomo-trader/runs/paper-1h/trades.jsonl` (read-only), 100 entries, 102 closes; 78 priced closes used (24 unpriced, 0 without timestamp, 3 closes with no matching entry -> features neutral).
- Entries already carrying allocator fields (shadow/live): 0.
- Journal growth since prior watermark: 0 new close(s) (0 priced); rerun via `--force` after analyst fixes. Since the prior published 99-close report, the running bot added 3 closes; this snapshot has 102.
- Feature coverage (trades with a non-missing value): signal_gain_pct 78, liquidity_usd 78, buy_count_15m 32, sell_count_15m 31, buy_sell_ratio 75, volume_15m_usd 32, mcap_usd 32, holder_top1_pct 0, holder_top5_pct 0, signal_to_fill_slippage_pct 32, entry_latency_sec 32, chain_bsc 50

## Recency discipline

Training uses the FULL journal with exponential recency weights (half-life 14 days). In this journal the oldest trade weighs 0.846 and the newest 1.000.

Recent window (last 24 closes): -0.495 $/trade, win rate 21%. Older journal (54 closes): -1.085 $/trade, win rate 30%. Full journal: -0.903 $/trade. Difference +0.590 $/trade (z = 0.9).

**RECENCY-CONCENTRATED.** Recent results are better than the older journal. The claim that the system "has been doing way better now" is consistent with the recent window, but this analyst does NOT treat it as a persistent edge: a short hot streak is indistinguishable from luck or a passing regime at this sample size. Weights are fit on the full journal with a 14-day half-life, and the recent window is not overweighted beyond that.

The recent window is still net negative (-0.495 $/trade): "better" here means losing less, not making money.

The improvement is 0.9 standard errors: within noise.

## P&L attribution (journal realized_usd, before replay costs)

### allocator score (current config weights)

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 0.455 .. 0.482 | 15 | -1.032 | 13% | -15.48 |
| Q2 | 0.484 .. 0.495 | 16 | +0.377 | 62% | +6.02 |
| Q3 | 0.495 .. 0.507 | 15 | -1.171 | 27% | -17.56 |
| Q4 | 0.508 .. 0.518 | 16 | -1.744 | 12% | -27.90 |
| Q5 | 0.518 .. 0.577 | 16 | -0.971 | 19% | -15.54 |

### signal_gain_pct

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 18.9 .. 45.1 | 15 | -0.509 | 53% | -7.64 |
| Q2 | 51.3 .. 102 | 16 | +0.399 | 50% | +6.38 |
| Q3 | 103 .. 177 | 15 | -1.804 | 20% | -27.06 |
| Q4 | 182 .. 258 | 16 | -1.510 | 6% | -24.16 |
| Q5 | 264 .. 1103 | 16 | -1.123 | 6% | -17.97 |

### liquidity_usd

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 8272 .. 25036 | 15 | +0.482 | 53% | +7.23 |
| Q2 | 25309 .. 40199 | 16 | -0.849 | 38% | -13.59 |
| Q3 | 40308 .. 53128 | 15 | -1.129 | 20% | -16.94 |
| Q4 | 53265 .. 77215 | 16 | -1.265 | 12% | -20.25 |
| Q5 | 78160 .. 1.59e+05 | 16 | -1.682 | 12% | -26.91 |

### buy_sell_ratio

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 1.5 .. 1.6 | 15 | -0.871 | 27% | -13.06 |
| Q2 | 1.7 .. 2 | 16 | -0.961 | 31% | -15.37 |
| Q3 | 2 .. 2.6 | 15 | -0.068 | 47% | -1.02 |
| Q4 | 2.6 .. 4 | 16 | -1.062 | 19% | -17.00 |
| Q5 | 4.1 .. 13 | 16 | -1.500 | 12% | -24.01 |

### chain

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| bsc | - | 50 | -1.324 | 14% | -66.19 |
| solana | - | 28 | -0.152 | 50% | -4.27 |

## Proposed weights (full journal, recency-weighted)

Method: L2 (lambda=0.10) logistic regression of P(win) on normalized features, deterministic gradient descent; each weight = change in win log-odds per unit of normalized feature. Intercept re-centered so the recency-weighted average trade scores 0.5 (1.0x). take_threshold = 0.35.

| feature | current | proposed (full fit) | IS-only fit (gated) |
|---|---:|---:|---:|
| intercept | +0.0000 | +0.4404 | +0.3102 |
| signal_gain_pct | -0.1000 | -0.4665 | -0.5066 |
| liquidity_usd | +0.1500 | -0.2810 | -0.4507 |
| buy_count_15m | +0.0500 | -0.1664 | -0.1257 |
| sell_count_15m | +0.0000 | -0.1060 | -0.1038 |
| buy_sell_ratio | +0.1000 | -0.2220 | -0.2250 |
| volume_15m_usd | +0.0500 | -0.1119 | -0.1375 |
| mcap_usd | +0.0000 | -0.0878 | -0.1262 |
| holder_top1_pct | -0.1500 | +0.0000 | +0.0000 |
| holder_top5_pct | -0.1000 | +0.0000 | +0.0000 |
| signal_to_fill_slippage_pct | -0.1500 | -0.1896 | -0.1121 |
| entry_latency_sec | -0.0500 | -0.0345 | +0.0365 |
| chain_bsc | +0.0000 | -0.4766 | -0.5529 |

## Validation gate

Costs: entry 100 bps + exit 300 bps of notional, plus fixed fee per trade bsc $0.20, solana $0.60. Linear ticket scaling (m x realized). The money.py risk ceiling and balance-aware sizing are future replay extensions; upsizing here is an upper bound.

IS window 2026-09-24 10:56:20 .. 2026-09-27 16:46:23 (48 closes); OOS window 2026-09-27 17:03:47 .. 2026-09-27 20:16:33 (30 closes). Weights refit on IS only.

| window | n | taken | base $/trade | alloc $/trade | edge $/trade | base WR | alloc WR (taken) | base maxDD $ | alloc maxDD $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IS | 48 | 42 | -1.671 | -1.244 | +0.427 | 33% | 38% | 84.27 | 66.81 |
| OOS | 30 | 17 | -1.297 | -0.438 | +0.860 | 17% | 24% | 38.92 | 13.13 |

| check | value | threshold | result |
|---|---:|---|---|
| is_edge_positive | 0.4272 | > 0 | PASS |
| oos_edge_positive | 0.8595 | > 0 | PASS |
| oos_retention | 2.0119 | >= 0.60 | PASS |
| oos_decay | -1.0119 | <= 0.70 | PASS |
| is_win_rate_not_overfit | 0.381 | <= 0.90 | PASS |
| edge_not_single_chain | 0.9894 | <= 0.80 (if evaluable) | FAIL |
| edge_not_single_time_regime | 0.4909 | <= 0.80 (if evaluable) | PASS |

Edge by chain (IS-fit replay, $): {'solana': 0.49, 'bsc': 45.8}. Edge by time tercile ($): {'0': 4.33, '1': 19.23, '2': 22.73}.
Chain concentration includes comparative edge from shrinking or skipping trades on a weak chain; that is not evidence of within-chain ranking skill.

In-sample only (informational, not gated): proposed full-fit weights on the whole journal: edge +0.087 $/trade, 78/78 taken.

## Apply (manual; only on a `pass` verdict)

1. Copy `weights` from `analysis/allocator_proposal.json` into the `allocator.weights` section of `runs/paper-1h/config.json`.
2. Set `allocator.mode` to `"live"`.
3. Restart the bot so it reloads config. `dry_run` stays `true`.

This script never edits the config.
