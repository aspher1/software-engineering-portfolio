# Allocator analyst report (2026-09-29 08:22:42 UTC)

**Verdict: `fail`**. Do not flip to live. Keep `allocator.mode = "shadow"`.

Paper journal only. Every number below is comparative (allocator vs 1.0x baseline on the same trades). Nothing here is a profitability claim.

## Data

- Journal: `/home/hatch/workspace/fomo-trader/runs/paper-1h/trades.jsonl` (read-only), 212 entries, 215 closes; 191 priced closes used (24 unpriced, 0 without timestamp, 3 closes with no matching entry -> features neutral).
- Entries already carrying allocator fields (shadow/live): 112.
- Feature coverage (trades with a non-missing value): signal_gain_pct 191, liquidity_usd 191, buy_count_15m 144, sell_count_15m 142, buy_sell_ratio 184, volume_15m_usd 145, mcap_usd 145, holder_top1_pct 0, holder_top5_pct 0, signal_to_fill_slippage_pct 145, entry_latency_sec 145, chain_bsc 163

## Recency discipline

Training uses the FULL journal with exponential recency weights (half-life 14 days). In this journal the oldest trade weighs 0.787 and the newest 1.000.

Recent window (last 58 closes): -0.692 $/trade, win rate 33%. Older journal (133 closes): -1.026 $/trade, win rate 29%. Full journal: -0.925 $/trade. Difference +0.334 $/trade (z = 0.93).

**RECENCY-CONCENTRATED.** Recent results are better than the older journal. The claim that the system "has been doing way better now" is consistent with the recent window, but this analyst does NOT treat it as a persistent edge: a short hot streak is indistinguishable from luck or a passing regime at this sample size. Weights are fit on the full journal with a 14-day half-life, and the recent window is not overweighted beyond that.

The recent window is still net negative (-0.692 $/trade): "better" here means losing less, not making money.

The improvement is 0.93 standard errors: within noise.

## P&L attribution (journal realized_usd, before replay costs)

### allocator score (current config weights)

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 0.449 .. 0.492 | 38 | -1.372 | 16% | -52.13 |
| Q2 | 0.492 .. 0.507 | 38 | -0.512 | 47% | -19.45 |
| Q3 | 0.507 .. 0.524 | 38 | -1.781 | 24% | -67.69 |
| Q4 | 0.524 .. 0.548 | 38 | -0.804 | 32% | -30.57 |
| Q5 | 0.548 .. 0.577 | 39 | -0.173 | 33% | -6.76 |

### signal_gain_pct

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 18.9 .. 62.8 | 38 | -0.408 | 50% | -15.49 |
| Q2 | 63.1 .. 123 | 38 | -0.689 | 42% | -26.17 |
| Q3 | 125 .. 180 | 38 | -1.045 | 24% | -39.69 |
| Q4 | 182 .. 254 | 38 | -1.170 | 24% | -44.47 |
| Q5 | 257 .. 12170 | 39 | -1.302 | 13% | -50.78 |

### liquidity_usd

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 8272 .. 34012 | 38 | -0.101 | 53% | -3.82 |
| Q2 | 35087 .. 46652 | 38 | -1.056 | 26% | -40.11 |
| Q3 | 47632 .. 68074 | 38 | -1.416 | 21% | -53.80 |
| Q4 | 69959 .. 1.05e+05 | 38 | -1.194 | 24% | -45.36 |
| Q5 | 1.05e+05 .. 1.59e+05 | 39 | -0.859 | 28% | -33.51 |

### buy_sell_ratio

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 1.5 .. 1.7 | 38 | -0.653 | 42% | -24.83 |
| Q2 | 1.7 .. 2 | 38 | -0.872 | 29% | -33.14 |
| Q3 | 2.1 .. 2.8 | 38 | -0.530 | 39% | -20.13 |
| Q4 | 3 .. 4.8 | 38 | -0.829 | 29% | -31.50 |
| Q5 | 4.8 .. 16.6 | 39 | -1.718 | 13% | -67.00 |

### chain

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| bsc | - | 163 | -1.057 | 27% | -172.34 |
| solana | - | 28 | -0.152 | 50% | -4.27 |

## Proposed weights (full journal, recency-weighted)

Method: L2 (lambda=0.10) logistic regression of P(win) on normalized features, deterministic gradient descent; each weight = change in win log-odds per unit of normalized feature. Intercept re-centered so the recency-weighted average trade scores 0.5 (1.0x). take_threshold = 0.35.

| feature | current | proposed (full fit) | IS-only fit (gated) |
|---|---:|---:|---:|
| intercept | +0.0000 | +0.1846 | +0.2790 |
| signal_gain_pct | -0.1000 | -0.2994 | -0.3255 |
| liquidity_usd | +0.1500 | -0.1458 | -0.2109 |
| buy_count_15m | +0.0500 | -0.0985 | -0.1062 |
| sell_count_15m | +0.0000 | +0.0206 | -0.0111 |
| buy_sell_ratio | +0.1000 | -0.2252 | -0.2163 |
| volume_15m_usd | +0.0500 | -0.1249 | -0.1120 |
| mcap_usd | +0.0000 | -0.1079 | -0.1196 |
| holder_top1_pct | -0.1500 | +0.0000 | +0.0000 |
| holder_top5_pct | -0.1000 | +0.0000 | +0.0000 |
| signal_to_fill_slippage_pct | -0.1500 | -0.3369 | -0.4437 |
| entry_latency_sec | -0.0500 | -0.0807 | -0.0728 |
| chain_bsc | +0.0000 | -0.1918 | -0.2721 |

## Validation gate

Costs: entry 100 bps + exit 300 bps of notional, plus fixed fee per trade bsc $0.20, solana $0.60. Linear ticket scaling (m x realized). The money.py risk ceiling and balance-aware sizing are future replay extensions; upsizing here is an upper bound.

IS window 2026-09-24 10:56:20 .. 2026-09-28 09:27:13 (133 closes); OOS window 2026-09-28 10:56:55 .. 2026-09-29 07:01:19 (58 closes). Weights refit on IS only.

| window | n | taken | base $/trade | alloc $/trade | edge $/trade | base WR | alloc WR (taken) | base maxDD $ | alloc maxDD $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IS | 133 | 126 | -1.562 | -1.365 | +0.197 | 29% | 31% | 207.74 | 181.61 |
| OOS | 58 | 57 | -1.149 | -1.220 | -0.071 | 33% | 33% | 67.24 | 72.26 |

| check | value | threshold | result |
|---|---:|---|---|
| is_edge_positive | 0.1965 | > 0 | PASS |
| oos_edge_positive | -0.0713 | > 0 | FAIL |
| oos_retention | -0.3628 | >= 0.60 | FAIL |
| oos_decay | 1.3628 | <= 0.70 | FAIL |
| is_win_rate_not_overfit | 0.3095 | <= 0.90 | PASS |
| edge_not_single_chain | 1.0 | <= 0.80 (if evaluable) | FAIL |
| edge_not_single_time_regime | 0.6932 | <= 0.80 (if evaluable) | PASS |

Edge by chain (IS-fit replay, $): {'solana': -1.21, 'bsc': 23.21}. Edge by time tercile ($): {'0': 7.92, '1': 17.9, '2': -3.83}.
Chain concentration includes comparative edge from shrinking or skipping trades on a weak chain; that is not evidence of within-chain ranking skill.

In-sample only (informational, not gated): proposed full-fit weights on the whole journal: edge +0.086 $/trade, 187/191 taken.

## Apply (manual; only on a `pass` verdict)

1. Copy `weights` from `analysis/allocator_proposal.json` into the `allocator.weights` section of `runs/paper-1h/config.json`.
2. Set `allocator.mode` to `"live"`.
3. Restart the bot so it reloads config. `dry_run` stays `true`.

This script never edits the config.
