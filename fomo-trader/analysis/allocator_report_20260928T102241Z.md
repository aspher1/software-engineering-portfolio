# Allocator analyst report (2026-09-28 10:22:41 UTC)

**Verdict: `fail`**. Do not flip to live. Keep `allocator.mode = "shadow"`.

Paper journal only. Every number below is comparative (allocator vs 1.0x baseline on the same trades). Nothing here is a profitability claim.

## Data

- Journal: `/home/hatch/workspace/fomo-trader/runs/paper-1h/trades.jsonl` (read-only), 154 entries, 157 closes; 133 priced closes used (24 unpriced, 0 without timestamp, 3 closes with no matching entry -> features neutral).
- Entries already carrying allocator fields (shadow/live): 54.
- Feature coverage (trades with a non-missing value): signal_gain_pct 133, liquidity_usd 133, buy_count_15m 86, sell_count_15m 85, buy_sell_ratio 127, volume_15m_usd 87, mcap_usd 87, holder_top1_pct 0, holder_top5_pct 0, signal_to_fill_slippage_pct 87, entry_latency_sec 87, chain_bsc 105

## Recency discipline

Training uses the FULL journal with exponential recency weights (half-life 14 days). In this journal the oldest trade weighs 0.823 and the newest 1.000.

Recent window (last 40 closes): -0.948 $/trade, win rate 32%. Older journal (93 closes): -1.059 $/trade, win rate 28%. Full journal: -1.026 $/trade. Difference +0.111 $/trade (z = 0.27).

**RECENCY-CONCENTRATED.** Recent results are better than the older journal. The claim that the system "has been doing way better now" is consistent with the recent window, but this analyst does NOT treat it as a persistent edge: a short hot streak is indistinguishable from luck or a passing regime at this sample size. Weights are fit on the full journal with a 14-day half-life, and the recent window is not overweighted beyond that.

The recent window is still net negative (-0.948 $/trade): "better" here means losing less, not making money.

The improvement is 0.27 standard errors: within noise.

## P&L attribution (journal realized_usd, before replay costs)

### allocator score (current config weights)

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 0.449 .. 0.486 | 26 | -1.712 | 8% | -44.52 |
| Q2 | 0.487 .. 0.498 | 27 | -0.205 | 52% | -5.53 |
| Q3 | 0.498 .. 0.511 | 26 | -1.137 | 23% | -29.57 |
| Q4 | 0.512 .. 0.536 | 27 | -2.067 | 26% | -55.80 |
| Q5 | 0.536 .. 0.577 | 27 | -0.038 | 37% | -1.02 |

### signal_gain_pct

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 18.9 .. 54.6 | 26 | -0.601 | 50% | -15.64 |
| Q2 | 55.6 .. 103 | 27 | -0.768 | 37% | -20.75 |
| Q3 | 104 .. 168 | 26 | -1.206 | 27% | -31.34 |
| Q4 | 171 .. 251 | 27 | -1.433 | 26% | -38.68 |
| Q5 | 254 .. 2335 | 27 | -1.113 | 7% | -30.04 |

### liquidity_usd

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 8272 .. 28041 | 26 | +0.232 | 54% | +6.02 |
| Q2 | 30225 .. 45357 | 27 | -1.325 | 26% | -35.77 |
| Q3 | 45367 .. 59962 | 26 | -1.160 | 23% | -30.17 |
| Q4 | 60092 .. 93550 | 27 | -1.407 | 26% | -37.99 |
| Q5 | 96588 .. 1.59e+05 | 27 | -1.427 | 19% | -38.54 |

### buy_sell_ratio

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 1.5 .. 1.7 | 26 | -0.923 | 38% | -24.01 |
| Q2 | 1.7 .. 2 | 27 | -0.652 | 33% | -17.59 |
| Q3 | 2 .. 2.6 | 26 | -0.831 | 35% | -21.61 |
| Q4 | 2.6 .. 4.2 | 27 | -0.890 | 26% | -24.03 |
| Q5 | 4.2 .. 13.2 | 27 | -1.822 | 15% | -49.20 |

### chain

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| bsc | - | 105 | -1.259 | 24% | -132.18 |
| solana | - | 28 | -0.152 | 50% | -4.27 |

## Proposed weights (full journal, recency-weighted)

Method: L2 (lambda=0.10) logistic regression of P(win) on normalized features, deterministic gradient descent; each weight = change in win log-odds per unit of normalized feature. Intercept re-centered so the recency-weighted average trade scores 0.5 (1.0x). take_threshold = 0.35.

| feature | current | proposed (full fit) | IS-only fit (gated) |
|---|---:|---:|---:|
| intercept | +0.0000 | +0.2790 | +0.3813 |
| signal_gain_pct | -0.1000 | -0.3255 | -0.4104 |
| liquidity_usd | +0.1500 | -0.2109 | -0.2144 |
| buy_count_15m | +0.0500 | -0.1062 | -0.1531 |
| sell_count_15m | +0.0000 | -0.0111 | -0.0706 |
| buy_sell_ratio | +0.1000 | -0.2163 | -0.2288 |
| volume_15m_usd | +0.0500 | -0.1120 | -0.1341 |
| mcap_usd | +0.0000 | -0.1196 | -0.1179 |
| holder_top1_pct | -0.1500 | +0.0000 | +0.0000 |
| holder_top5_pct | -0.1000 | +0.0000 | +0.0000 |
| signal_to_fill_slippage_pct | -0.1500 | -0.4437 | -0.2741 |
| entry_latency_sec | -0.0500 | -0.0728 | -0.0381 |
| chain_bsc | +0.0000 | -0.2721 | -0.3868 |

## Validation gate

Costs: entry 100 bps + exit 300 bps of notional, plus fixed fee per trade bsc $0.20, solana $0.60. Linear ticket scaling (m x realized). The money.py risk ceiling and balance-aware sizing are future replay extensions; upsizing here is an upper bound.

IS window 2026-09-24 10:56:20 .. 2026-09-27 22:40:02 (93 closes); OOS window 2026-09-27 22:45:38 .. 2026-09-28 09:27:13 (40 closes). Weights refit on IS only.

| window | n | taken | base $/trade | alloc $/trade | edge $/trade | base WR | alloc WR (taken) | base maxDD $ | alloc maxDD $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IS | 93 | 91 | -1.654 | -1.544 | +0.110 | 28% | 29% | 153.83 | 143.64 |
| OOS | 40 | 39 | -1.348 | -1.208 | +0.140 | 32% | 33% | 56.74 | 53.33 |

| check | value | threshold | result |
|---|---:|---|---|
| is_edge_positive | 0.1096 | > 0 | PASS |
| oos_edge_positive | 0.1396 | > 0 | PASS |
| oos_retention | 1.2737 | >= 0.60 | PASS |
| oos_decay | -0.2737 | <= 0.70 | PASS |
| is_win_rate_not_overfit | 0.2857 | <= 0.90 | PASS |
| edge_not_single_chain | 1.0 | <= 0.80 (if evaluable) | FAIL |
| edge_not_single_time_regime | 0.5484 | <= 0.80 (if evaluable) | PASS |

Edge by chain (IS-fit replay, $): {'solana': -2.8, 'bsc': 18.58}. Edge by time tercile ($): {'0': -0.09, '1': 8.7, '2': 7.17}.
Chain concentration includes comparative edge from shrinking or skipping trades on a weak chain; that is not evidence of within-chain ranking skill.

In-sample only (informational, not gated): proposed full-fit weights on the whole journal: edge +0.197 $/trade, 126/133 taken.

## Apply (manual; only on a `pass` verdict)

1. Copy `weights` from `analysis/allocator_proposal.json` into the `allocator.weights` section of `runs/paper-1h/config.json`.
2. Set `allocator.mode` to `"live"`.
3. Restart the bot so it reloads config. `dry_run` stays `true`.

This script never edits the config.
