# Allocator analyst report (2026-09-30 02:22:42 UTC)

**Verdict: `fail`**. Do not flip to live. Keep `allocator.mode = "shadow"`.

Paper journal only. Every number below is comparative (allocator vs 1.0x baseline on the same trades). Nothing here is a profitability claim.

## Data

- Journal: `/home/hatch/workspace/fomo-trader/runs/paper-1h/trades.jsonl` (read-only), 242 entries, 245 closes; 221 priced closes used (24 unpriced, 0 without timestamp, 3 closes with no matching entry -> features neutral).
- Entries already carrying allocator fields (shadow/live): 142.
- Feature coverage (trades with a non-missing value): signal_gain_pct 221, liquidity_usd 221, buy_count_15m 173, sell_count_15m 172, buy_sell_ratio 214, volume_15m_usd 175, mcap_usd 175, holder_top1_pct 0, holder_top5_pct 0, signal_to_fill_slippage_pct 175, entry_latency_sec 175, chain_bsc 193

## Recency discipline

Training uses the FULL journal with exponential recency weights (half-life 14 days). In this journal the oldest trade weighs 0.757 and the newest 1.000.

Recent window (last 67 closes): -0.575 $/trade, win rate 28%. Older journal (154 closes): -1.014 $/trade, win rate 31%. Full journal: -0.881 $/trade. Difference +0.440 $/trade (z = 1.38).

**RECENCY-CONCENTRATED.** Recent results are better than the older journal. The claim that the system "has been doing way better now" is consistent with the recent window, but this analyst does NOT treat it as a persistent edge: a short hot streak is indistinguishable from luck or a passing regime at this sample size. Weights are fit on the full journal with a 14-day half-life, and the recent window is not overweighted beyond that.

The recent window is still net negative (-0.575 $/trade): "better" here means losing less, not making money.

The improvement is 1.38 standard errors: within noise.

## P&L attribution (journal realized_usd, before replay costs)

### allocator score (current config weights)

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 0.449 .. 0.493 | 44 | -0.916 | 27% | -40.28 |
| Q2 | 0.493 .. 0.511 | 44 | -0.846 | 34% | -37.22 |
| Q3 | 0.511 .. 0.526 | 44 | -1.911 | 23% | -84.08 |
| Q4 | 0.526 .. 0.549 | 44 | -0.570 | 32% | -25.10 |
| Q5 | 0.55 .. 0.577 | 45 | -0.179 | 33% | -8.04 |

### signal_gain_pct

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 18.9 .. 63.1 | 44 | -0.719 | 48% | -31.64 |
| Q2 | 64 .. 126 | 44 | -0.445 | 41% | -19.58 |
| Q3 | 128 .. 187 | 44 | -1.057 | 23% | -46.51 |
| Q4 | 187 .. 257 | 44 | -0.945 | 25% | -41.56 |
| Q5 | 258 .. 12170 | 45 | -1.232 | 13% | -55.43 |

### liquidity_usd

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 8272 .. 36254 | 44 | -0.129 | 50% | -5.68 |
| Q2 | 37700 .. 48791 | 44 | -1.070 | 25% | -47.10 |
| Q3 | 49060 .. 72244 | 44 | -1.444 | 23% | -63.55 |
| Q4 | 72737 .. 1.08e+05 | 44 | -1.307 | 23% | -57.49 |
| Q5 | 1.08e+05 .. 1.59e+05 | 45 | -0.465 | 29% | -20.91 |

### buy_sell_ratio

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 1.5 .. 1.7 | 44 | -0.775 | 39% | -34.11 |
| Q2 | 1.7 .. 2 | 44 | -0.801 | 27% | -35.22 |
| Q3 | 2.1 .. 2.7 | 44 | -0.535 | 34% | -23.54 |
| Q4 | 2.8 .. 4.8 | 44 | -0.609 | 34% | -26.78 |
| Q5 | 4.8 .. 16.6 | 45 | -1.668 | 16% | -75.07 |

### chain

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| bsc | - | 193 | -0.987 | 27% | -190.46 |
| solana | - | 28 | -0.152 | 50% | -4.27 |

## Proposed weights (full journal, recency-weighted)

Method: L2 (lambda=0.10) logistic regression of P(win) on normalized features, deterministic gradient descent; each weight = change in win log-odds per unit of normalized feature. Intercept re-centered so the recency-weighted average trade scores 0.5 (1.0x). take_threshold = 0.35.

| feature | current | proposed (full fit) | IS-only fit (gated) |
|---|---:|---:|---:|
| intercept | +0.0000 | +0.1732 | +0.2339 |
| signal_gain_pct | -0.1000 | -0.2861 | -0.3307 |
| liquidity_usd | +0.1500 | -0.1206 | -0.1374 |
| buy_count_15m | +0.0500 | -0.1363 | -0.1169 |
| sell_count_15m | +0.0000 | -0.0379 | -0.0167 |
| buy_sell_ratio | +0.1000 | -0.1890 | -0.2103 |
| volume_15m_usd | +0.0500 | -0.1280 | -0.0775 |
| mcap_usd | +0.0000 | -0.1097 | -0.0900 |
| holder_top1_pct | -0.1500 | +0.0000 | +0.0000 |
| holder_top5_pct | -0.1000 | +0.0000 | +0.0000 |
| signal_to_fill_slippage_pct | -0.1500 | -0.2977 | -0.4256 |
| entry_latency_sec | -0.0500 | -0.0583 | -0.0630 |
| chain_bsc | +0.0000 | -0.1729 | -0.2265 |

## Validation gate

Costs: entry 100 bps + exit 300 bps of notional, plus fixed fee per trade bsc $0.20, solana $0.60. Linear ticket scaling (m x realized). The money.py risk ceiling and balance-aware sizing are future replay extensions; upsizing here is an upper bound.

IS window 2026-09-24 10:56:20 .. 2026-09-28 20:26:30 (154 closes); OOS window 2026-09-28 20:53:48 .. 2026-09-30 01:39:41 (67 closes). Weights refit on IS only.

| window | n | taken | base $/trade | alloc $/trade | edge $/trade | base WR | alloc WR (taken) | base maxDD $ | alloc maxDD $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IS | 154 | 148 | -1.536 | -1.404 | +0.132 | 31% | 32% | 236.50 | 216.25 |
| OOS | 67 | 67 | -1.048 | -1.144 | -0.096 | 28% | 28% | 70.18 | 76.64 |

| check | value | threshold | result |
|---|---:|---|---|
| is_edge_positive | 0.1315 | > 0 | PASS |
| oos_edge_positive | -0.0964 | > 0 | FAIL |
| oos_retention | -0.7331 | >= 0.60 | FAIL |
| oos_decay | 1.7331 | <= 0.70 | FAIL |
| is_win_rate_not_overfit | 0.3176 | <= 0.90 | PASS |
| edge_not_single_chain | 1.0 | <= 0.80 (if evaluable) | FAIL |
| edge_not_single_time_regime | 0.568 | <= 0.80 (if evaluable) | PASS |

Edge by chain (IS-fit replay, $): {'solana': -1.69, 'bsc': 15.48}. Edge by time tercile ($): {'0': 10.83, '1': 14.24, '2': -11.26}.
Chain concentration includes comparative edge from shrinking or skipping trades on a weak chain; that is not evidence of within-chain ranking skill.

In-sample only (informational, not gated): proposed full-fit weights on the whole journal: edge +0.041 $/trade, 219/221 taken.

## Apply (manual; only on a `pass` verdict)

1. Copy `weights` from `analysis/allocator_proposal.json` into the `allocator.weights` section of `runs/paper-1h/config.json`.
2. Set `allocator.mode` to `"live"`.
3. Restart the bot so it reloads config. `dry_run` stays `true`.

This script never edits the config.
