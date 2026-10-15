# Allocator analyst report (2026-09-30 08:22:42 UTC)

**Verdict: `fail`**. Do not flip to live. Keep `allocator.mode = "shadow"`.

Paper journal only. Every number below is comparative (allocator vs 1.0x baseline on the same trades). Nothing here is a profitability claim.

## Data

- Journal: `/home/hatch/workspace/fomo-trader/runs/paper-1h/trades.jsonl` (read-only), 267 entries, 270 closes; 246 priced closes used (24 unpriced, 0 without timestamp, 3 closes with no matching entry -> features neutral).
- Entries already carrying allocator fields (shadow/live): 167.
- Feature coverage (trades with a non-missing value): signal_gain_pct 246, liquidity_usd 246, buy_count_15m 198, sell_count_15m 197, buy_sell_ratio 236, volume_15m_usd 200, mcap_usd 200, holder_top1_pct 0, holder_top5_pct 0, signal_to_fill_slippage_pct 200, entry_latency_sec 200, chain_bsc 218

## Recency discipline

Training uses the FULL journal with exponential recency weights (half-life 14 days). In this journal the oldest trade weighs 0.747 and the newest 1.000.

Recent window (last 74 closes): -0.320 $/trade, win rate 35%. Older journal (172 closes): -0.958 $/trade, win rate 30%. Full journal: -0.766 $/trade. Difference +0.638 $/trade (z = 2.0).

**RECENCY-CONCENTRATED.** Recent results are better than the older journal. The claim that the system "has been doing way better now" is consistent with the recent window, but this analyst does NOT treat it as a persistent edge: a short hot streak is indistinguishable from luck or a passing regime at this sample size. Weights are fit on the full journal with a 14-day half-life, and the recent window is not overweighted beyond that.

The recent window is still net negative (-0.320 $/trade): "better" here means losing less, not making money.

## P&L attribution (journal realized_usd, before replay costs)

### allocator score (current config weights)

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 0.449 .. 0.495 | 49 | -1.042 | 29% | -51.04 |
| Q2 | 0.495 .. 0.514 | 49 | -1.152 | 29% | -56.44 |
| Q3 | 0.514 .. 0.53 | 49 | -1.593 | 27% | -78.06 |
| Q4 | 0.53 .. 0.55 | 49 | -0.033 | 41% | -1.63 |
| Q5 | 0.55 .. 0.577 | 50 | -0.023 | 34% | -1.17 |

### signal_gain_pct

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 18.9 .. 63.1 | 49 | -0.638 | 47% | -31.26 |
| Q2 | 64 .. 132 | 49 | -0.400 | 41% | -19.61 |
| Q3 | 136 .. 187 | 49 | -0.789 | 27% | -38.65 |
| Q4 | 189 .. 261 | 49 | -1.040 | 29% | -50.94 |
| Q5 | 264 .. 12170 | 50 | -0.957 | 16% | -47.87 |

### liquidity_usd

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 8272 .. 38685 | 49 | -0.245 | 49% | -12.01 |
| Q2 | 38951 .. 49060 | 49 | -1.112 | 24% | -54.49 |
| Q3 | 49286 .. 73352 | 49 | -1.207 | 29% | -59.12 |
| Q4 | 73789 .. 1.09e+05 | 49 | -1.150 | 20% | -56.37 |
| Q5 | 1.09e+05 .. 1.59e+05 | 50 | -0.127 | 36% | -6.35 |

### buy_sell_ratio

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 1.5 .. 1.7 | 49 | -0.894 | 35% | -43.81 |
| Q2 | 1.7 .. 2.1 | 49 | -0.783 | 29% | -38.35 |
| Q3 | 2.1 .. 2.7 | 49 | -0.127 | 43% | -6.22 |
| Q4 | 2.8 .. 4.6 | 49 | -0.628 | 35% | -30.77 |
| Q5 | 4.7 .. 16.6 | 50 | -1.384 | 18% | -69.18 |

### chain

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| bsc | - | 218 | -0.844 | 29% | -184.07 |
| solana | - | 28 | -0.152 | 50% | -4.27 |

## Proposed weights (full journal, recency-weighted)

Method: L2 (lambda=0.10) logistic regression of P(win) on normalized features, deterministic gradient descent; each weight = change in win log-odds per unit of normalized feature. Intercept re-centered so the recency-weighted average trade scores 0.5 (1.0x). take_threshold = 0.35.

| feature | current | proposed (full fit) | IS-only fit (gated) |
|---|---:|---:|---:|
| intercept | +0.0000 | +0.1152 | +0.2099 |
| signal_gain_pct | -0.1000 | -0.2631 | -0.3188 |
| liquidity_usd | +0.1500 | -0.0907 | -0.1435 |
| buy_count_15m | +0.0500 | -0.1362 | -0.1001 |
| sell_count_15m | +0.0000 | -0.0657 | -0.0093 |
| buy_sell_ratio | +0.1000 | -0.1445 | -0.1915 |
| volume_15m_usd | +0.0500 | -0.1299 | -0.1047 |
| mcap_usd | +0.0000 | -0.0819 | -0.0949 |
| holder_top1_pct | -0.1500 | +0.0000 | +0.0000 |
| holder_top5_pct | -0.1000 | +0.0000 | +0.0000 |
| signal_to_fill_slippage_pct | -0.1500 | -0.3558 | -0.3613 |
| entry_latency_sec | -0.0500 | +0.0045 | -0.0812 |
| chain_bsc | +0.0000 | -0.1469 | -0.2121 |

## Validation gate

Costs: entry 100 bps + exit 300 bps of notional, plus fixed fee per trade bsc $0.20, solana $0.60. Linear ticket scaling (m x realized). The money.py risk ceiling and balance-aware sizing are future replay extensions; upsizing here is an upper bound.

IS window 2026-09-24 10:56:20 .. 2026-09-29 03:38:05 (172 closes); OOS window 2026-09-29 03:48:28 .. 2026-09-30 08:21:20 (74 closes). Weights refit on IS only.

| window | n | taken | base $/trade | alloc $/trade | edge $/trade | base WR | alloc WR (taken) | base maxDD $ | alloc maxDD $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IS | 172 | 168 | -1.474 | -1.394 | +0.080 | 30% | 31% | 253.48 | 239.74 |
| OOS | 74 | 74 | -0.793 | -0.746 | +0.047 | 35% | 35% | 65.87 | 67.29 |

| check | value | threshold | result |
|---|---:|---|---|
| is_edge_positive | 0.0799 | > 0 | PASS |
| oos_edge_positive | 0.0469 | > 0 | PASS |
| oos_retention | 0.587 | >= 0.60 | FAIL |
| oos_decay | 0.413 | <= 0.70 | PASS |
| is_win_rate_not_overfit | 0.3095 | <= 0.90 | PASS |
| edge_not_single_chain | 1.0 | <= 0.80 (if evaluable) | FAIL |
| edge_not_single_time_regime | 0.7196 | <= 0.80 (if evaluable) | PASS |

Edge by chain (IS-fit replay, $): {'solana': -1.35, 'bsc': 18.56}. Edge by time tercile ($): {'0': 12.38, '1': 1.63, '2': 3.2}.
Chain concentration includes comparative edge from shrinking or skipping trades on a weak chain; that is not evidence of within-chain ranking skill.

In-sample only (informational, not gated): proposed full-fit weights on the whole journal: edge +0.081 $/trade, 242/246 taken.

## Apply (manual; only on a `pass` verdict)

1. Copy `weights` from `analysis/allocator_proposal.json` into the `allocator.weights` section of `runs/paper-1h/config.json`.
2. Set `allocator.mode` to `"live"`.
3. Restart the bot so it reloads config. `dry_run` stays `true`.

This script never edits the config.
