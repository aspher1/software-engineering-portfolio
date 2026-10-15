# Allocator analyst report (2026-09-29 02:22:43 UTC)

**Verdict: `fail`**. Do not flip to live. Keep `allocator.mode = "shadow"`.

Paper journal only. Every number below is comparative (allocator vs 1.0x baseline on the same trades). Nothing here is a profitability claim.

## Data

- Journal: `/home/hatch/workspace/fomo-trader/runs/paper-1h/trades.jsonl` (read-only), 186 entries, 187 closes; 163 priced closes used (24 unpriced, 0 without timestamp, 3 closes with no matching entry -> features neutral).
- Entries already carrying allocator fields (shadow/live): 84.
- Feature coverage (trades with a non-missing value): signal_gain_pct 163, liquidity_usd 163, buy_count_15m 116, sell_count_15m 115, buy_sell_ratio 157, volume_15m_usd 117, mcap_usd 117, holder_top1_pct 0, holder_top5_pct 0, signal_to_fill_slippage_pct 117, entry_latency_sec 117, chain_bsc 135

## Recency discipline

Training uses the FULL journal with exponential recency weights (half-life 14 days). In this journal the oldest trade weighs 0.795 and the newest 1.000.

Recent window (last 49 closes): -0.945 $/trade, win rate 31%. Older journal (114 closes): -0.999 $/trade, win rate 30%. Full journal: -0.983 $/trade. Difference +0.054 $/trade (z = 0.15).

**RECENCY-CONCENTRATED.** Recent results are better than the older journal. The claim that the system "has been doing way better now" is consistent with the recent window, but this analyst does NOT treat it as a persistent edge: a short hot streak is indistinguishable from luck or a passing regime at this sample size. Weights are fit on the full journal with a 14-day half-life, and the recent window is not overweighted beyond that.

The recent window is still net negative (-0.945 $/trade): "better" here means losing less, not making money.

The improvement is 0.15 standard errors: within noise.

## P&L attribution (journal realized_usd, before replay costs)

### allocator score (current config weights)

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 0.449 .. 0.488 | 32 | -1.583 | 9% | -50.65 |
| Q2 | 0.489 .. 0.502 | 33 | -0.344 | 52% | -11.37 |
| Q3 | 0.503 .. 0.516 | 32 | -1.354 | 22% | -43.34 |
| Q4 | 0.516 .. 0.547 | 33 | -1.562 | 30% | -51.54 |
| Q5 | 0.548 .. 0.577 | 33 | -0.099 | 36% | -3.27 |

### signal_gain_pct

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 18.9 .. 58.4 | 32 | -0.585 | 50% | -18.72 |
| Q2 | 58.5 .. 116 | 33 | -0.924 | 39% | -30.48 |
| Q3 | 117 .. 173 | 32 | -0.880 | 28% | -28.17 |
| Q4 | 177 .. 254 | 33 | -1.308 | 21% | -43.15 |
| Q5 | 257 .. 12170 | 33 | -1.201 | 12% | -39.64 |

### liquidity_usd

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 8272 .. 31819 | 32 | +0.093 | 50% | +2.98 |
| Q2 | 32164 .. 46385 | 33 | -1.228 | 27% | -40.53 |
| Q3 | 46414 .. 64655 | 32 | -1.452 | 19% | -46.46 |
| Q4 | 65155 .. 1.05e+05 | 33 | -1.448 | 24% | -47.77 |
| Q5 | 1.05e+05 .. 1.59e+05 | 33 | -0.860 | 30% | -28.39 |

### buy_sell_ratio

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| Q1 | 1.5 .. 1.7 | 32 | -0.706 | 44% | -22.60 |
| Q2 | 1.7 .. 2.1 | 33 | -0.723 | 30% | -23.84 |
| Q3 | 2.1 .. 2.8 | 32 | -0.629 | 38% | -20.13 |
| Q4 | 3 .. 4.8 | 33 | -1.113 | 24% | -36.73 |
| Q5 | 5.1 .. 16.6 | 33 | -1.723 | 15% | -56.86 |

### chain

| bucket | range | n | avg realized $/trade | win rate | total $ |
|---|---|---:|---:|---:|---:|
| bsc | - | 135 | -1.155 | 26% | -155.90 |
| solana | - | 28 | -0.152 | 50% | -4.27 |

## Proposed weights (full journal, recency-weighted)

Method: L2 (lambda=0.10) logistic regression of P(win) on normalized features, deterministic gradient descent; each weight = change in win log-odds per unit of normalized feature. Intercept re-centered so the recency-weighted average trade scores 0.5 (1.0x). take_threshold = 0.35.

| feature | current | proposed (full fit) | IS-only fit (gated) |
|---|---:|---:|---:|
| intercept | +0.0000 | +0.2229 | +0.3094 |
| signal_gain_pct | -0.1000 | -0.3043 | -0.3632 |
| liquidity_usd | +0.1500 | -0.1448 | -0.2336 |
| buy_count_15m | +0.0500 | -0.1010 | -0.1539 |
| sell_count_15m | +0.0000 | +0.0015 | -0.0778 |
| buy_sell_ratio | +0.1000 | -0.2104 | -0.2016 |
| volume_15m_usd | +0.0500 | -0.0668 | -0.1687 |
| mcap_usd | +0.0000 | -0.0822 | -0.1575 |
| holder_top1_pct | -0.1500 | +0.0000 | +0.0000 |
| holder_top5_pct | -0.1000 | +0.0000 | +0.0000 |
| signal_to_fill_slippage_pct | -0.1500 | -0.3751 | -0.3215 |
| entry_latency_sec | -0.0500 | -0.0619 | -0.0655 |
| chain_bsc | +0.0000 | -0.2227 | -0.2944 |

## Validation gate

Costs: entry 100 bps + exit 300 bps of notional, plus fixed fee per trade bsc $0.20, solana $0.60. Linear ticket scaling (m x realized). The money.py risk ceiling and balance-aware sizing are future replay extensions; upsizing here is an upper bound.

IS window 2026-09-24 10:56:20 .. 2026-09-28 05:37:50 (114 closes); OOS window 2026-09-28 06:08:21 .. 2026-09-29 02:13:39 (49 closes). Weights refit on IS only.

| window | n | taken | base $/trade | alloc $/trade | edge $/trade | base WR | alloc WR (taken) | base maxDD $ | alloc maxDD $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IS | 114 | 110 | -1.561 | -1.403 | +0.158 | 30% | 31% | 177.92 | 159.97 |
| OOS | 49 | 48 | -1.363 | -1.408 | -0.045 | 31% | 31% | 66.77 | 69.00 |

| check | value | threshold | result |
|---|---:|---|---|
| is_edge_positive | 0.1575 | > 0 | PASS |
| oos_edge_positive | -0.0454 | > 0 | FAIL |
| oos_retention | -0.2883 | >= 0.60 | FAIL |
| oos_decay | 1.2883 | <= 0.70 | FAIL |
| is_win_rate_not_overfit | 0.3091 | <= 0.90 | PASS |
| edge_not_single_chain | 1.0 | <= 0.80 (if evaluable) | FAIL |
| edge_not_single_time_regime | 0.5919 | <= 0.80 (if evaluable) | PASS |

Edge by chain (IS-fit replay, $): {'solana': -1.29, 'bsc': 17.03}. Edge by time tercile ($): {'0': 4.15, '1': 9.31, '2': 2.27}.
Chain concentration includes comparative edge from shrinking or skipping trades on a weak chain; that is not evidence of within-chain ranking skill.

In-sample only (informational, not gated): proposed full-fit weights on the whole journal: edge +0.085 $/trade, 159/163 taken.

## Apply (manual; only on a `pass` verdict)

1. Copy `weights` from `analysis/allocator_proposal.json` into the `allocator.weights` section of `runs/paper-1h/config.json`.
2. Set `allocator.mode` to `"live"`.
3. Restart the bot so it reloads config. `dry_run` stays `true`.

This script never edits the config.
