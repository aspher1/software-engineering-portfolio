# Allocator analyst report (2026-09-28 00:05:57 UTC)

**Verdict: `fail`**. Do not flip to live. Keep `allocator.mode = "shadow"`.

Paper journal only. Every number below is comparative (allocator vs 1.0x baseline on the same trades). Nothing here is a profitability claim.

## Data

- Journal: `/home/hatch/workspace/fomo-trader/runs/paper-1h/trades.jsonl` (read-only), 99 entries, 99 closes; 75 priced closes used (24 unpriced, 0 without timestamp, 3 closes with no matching entry -> features neutral).
- Entries already carrying allocator fields (shadow/live): 0.
- Feature coverage (trades with a non-missing value): signal_gain_pct 75, liquidity_usd 75, buy_count_15m 29, sell_count_15m 28, buy_sell_ratio 72, volume_15m_usd 29, mcap_usd 29, holder_top1_pct 0, holder_top5_pct 0, signal_to_fill_slippage_pct 29, entry_latency_sec 29, chain_bsc 47

## Recency discipline

Training uses the FULL journal with exponential recency weights (half-life 14 days). In this journal the oldest trade weighs 0.846 and the newest 1.000.

Recent window (last 23 closes): -0.444 $/trade, win rate 22%. Older journal (52 closes): -0.949 $/trade, win rate 31%. Full journal: -0.794 $/trade. Difference +0.505 $/trade (z = 0.76).

**RECENCY-CONCENTRATED.** Recent results are better than the older journal. The claim that the system "has been doing way better now" is consistent with the recent window, but this analyst does NOT treat it as a persistent edge: a short hot streak is indistinguishable from luck or a passing regime at this sample size. Weights are fit on the full journal with a 14-day half-life, and the recent window is not overweighted beyond that.

The recent window is still net negative (-0.444 $/trade): "better" here means losing less, not making money.

The improvement is 0.76 standard errors: within noise.

## P&L attribution (journal realized_usd, before replay costs)

### allocator score (current config weights)

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 0.455 .. 0.484 | 15 | -1.036 | 13% | -15.54 |
| Q2 | 0.487 .. 0.495 | 15 | +0.489 | 67% | +7.33 |
| Q3 | 0.495 .. 0.507 | 15 | -1.171 | 27% | -17.56 |
| Q4 | 0.508 .. 0.517 | 15 | -1.393 | 13% | -20.90 |
| Q5 | 0.518 .. 0.577 | 15 | -0.861 | 20% | -12.91 |

### signal_gain_pct

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 18.9 .. 45.1 | 15 | -0.509 | 53% | -7.64 |
| Q2 | 51.3 .. 96.9 | 15 | +0.540 | 53% | +8.10 |
| Q3 | 102 .. 177 | 15 | -1.452 | 20% | -21.78 |
| Q4 | 182 .. 257 | 15 | -1.609 | 7% | -24.13 |
| Q5 | 258 .. 1103 | 15 | -0.941 | 7% | -14.12 |

### liquidity_usd

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 8272 .. 25036 | 15 | +0.482 | 53% | +7.23 |
| Q2 | 25309 .. 39800 | 15 | -0.896 | 40% | -13.45 |
| Q3 | 40199 .. 53128 | 15 | -1.055 | 20% | -15.83 |
| Q4 | 53265 .. 76452 | 15 | -1.347 | 13% | -20.21 |
| Q5 | 77215 .. 1.59e+05 | 15 | -1.155 | 13% | -17.32 |

### buy_sell_ratio

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 1.5 .. 1.7 | 15 | -0.350 | 33% | -5.25 |
| Q2 | 1.7 .. 2 | 15 | -1.079 | 27% | -16.18 |
| Q3 | 2 .. 2.6 | 15 | +0.020 | 47% | +0.30 |
| Q4 | 2.6 .. 4 | 15 | -1.046 | 20% | -15.69 |
| Q5 | 4.1 .. 13 | 15 | -1.517 | 13% | -22.76 |

### chain

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| bsc | - | 47 | -1.177 | 15% | -55.31 |
| solana | - | 28 | -0.152 | 50% | -4.27 |

## Proposed weights (full journal, recency-weighted)

Method: L2 (lambda=0.10) logistic regression of P(win) on normalized features, deterministic gradient descent; each weight = change in win log-odds per unit of normalized feature. Intercept re-centered so the recency-weighted average trade scores 0.5 (1.0x). take_threshold = 0.35.

| feature | current | proposed (full fit) | IS-only fit (gated) |
|---|---:|---:|---:|
| intercept | +0.0000 | +0.4143 | +0.2974 |
| signal_gain_pct | -0.1000 | -0.4702 | -0.4688 |
| liquidity_usd | +0.1500 | -0.2726 | -0.4670 |
| buy_count_15m | +0.0500 | -0.1610 | -0.0620 |
| sell_count_15m | +0.0000 | -0.0983 | -0.0408 |
| buy_sell_ratio | +0.1000 | -0.2292 | -0.2375 |
| volume_15m_usd | +0.0500 | -0.0905 | -0.0366 |
| mcap_usd | +0.0000 | -0.0628 | -0.0447 |
| holder_top1_pct | -0.1500 | +0.0000 | +0.0000 |
| holder_top5_pct | -0.1000 | +0.0000 | +0.0000 |
| signal_to_fill_slippage_pct | -0.1500 | -0.1840 | -0.0264 |
| entry_latency_sec | -0.0500 | -0.0474 | +0.0021 |
| chain_bsc | +0.0000 | -0.4694 | -0.6123 |

## Validation gate

Costs: entry 100 bps + exit 300 bps of notional, plus fixed fee per trade bsc $0.20, solana $0.60. Linear ticket scaling (m x realized). The money.py risk ceiling is not replayed, so upsizing is an upper bound.

IS window 2026-09-24 10:56:20 .. 2026-09-27 16:19:59 (45 closes); OOS window 2026-09-27 16:22:41 .. 2026-09-27 20:16:33 (30 closes). Weights refit on IS only.

| window | n | taken | base $/trade | alloc $/trade | edge $/trade | base WR | alloc WR (taken) | base maxDD $ | alloc maxDD $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IS | 45 | 38 | -1.736 | -1.206 | +0.530 | 33% | 39% | 81.04 | 57.00 |
| OOS | 30 | 17 | -0.956 | -0.368 | +0.588 | 20% | 24% | 28.67 | 13.45 |

| check | value | threshold | result |
|---|---:|---|---|
| is_edge_positive | 0.5299 | > 0 | PASS |
| oos_edge_positive | 0.5877 | > 0 | PASS |
| oos_retention | 1.1091 | >= 0.60 | PASS |
| oos_decay | -0.1091 | <= 0.70 | PASS |
| is_win_rate_not_overfit | 0.3947 | <= 0.90 | PASS |
| edge_not_single_chain | 0.9855 | <= 0.80 (if evaluable) | FAIL |
| edge_not_single_time_regime | 0.5878 | <= 0.80 (if evaluable) | PASS |

Edge by chain (IS-fit replay, $): {'solana': 0.6, 'bsc': 40.88}. Edge by time tercile ($): {'0': 4.27, '1': 24.38, '2': 12.83}.

In-sample only (informational, not gated): proposed full-fit weights on the whole journal: edge +0.077 $/trade, 75/75 taken.

## Apply (manual; only on a `pass` verdict)

1. Copy `weights` from `analysis/allocator_proposal.json` into the `allocator.weights` section of `runs/paper-1h/config.json`.
2. Set `allocator.mode` to `"live"`.
3. Restart the bot so it reloads config. `dry_run` stays `true`.

This script never edits the config.
