# Track C signal quality — offline paper snapshot

## Scope and reconstruction

The read-only snapshot held **628 parsed FOMO/PRE-PUMP lines**, 70 journal entries, and 73 closes (3 closes without an entry). Its copies contain 16,136 log lines and 143 journal lines. This snapshot was taken while the paper bot was running; subsequent journal additions are outside these numbers. The earlier 67-close baseline was verified as historical context, not reused as the current baseline. The current all-close costed baseline is 73 / $-121.87 / 26.0% / 0.262 / $121.87 (n / net USD / win rate / PF / max drawdown). Costs and USD conversion use `replay.net_pnl`, including the documented approximate SOL conversion and 3× slippage stress model.

`replay.parse_signals()` supplies signals; `replay.load_journal()` pairs entries and closes. Each log `entered NAME` line is matched to a journal entry of normalized name and clock time within 120 seconds to anchor its calendar date. A signal takes the date of the nearest anchor by log line position, with a ±1-day correction for clocks crossing midnight. Each entry is paired once with the latest same-name, same-chain signal at or before entry, at most 30 minutes earlier. Name normalization is casefold plus whitespace collapse. The journal and log use naive local clock stamps. `deaths.log` records launcher switches between EDT and UTC. Its `bot started` clock and zone are matched to nearby `wallet ... DRY RUN` lines (within two seconds), assigning the latest known zone to each subsequent signal; 18 startup markers matched and no signal had unknown zone. The earliest signals inherit the first recorded EDT zone. Hour buckets convert each signal using that zone. Unrecorded switches or ambiguous startup matches would affect these hours and the time split. Restarts, missing entry lines, repeated names, and long anchor-free stretches can misdate or mispair signals; no independent date is present on a signal line. All 628 signals received inferred dates from 70 journal-backed entry anchors. The join cannot prove a never-entered signal would have traded if accepted.

| Signal status | Count |
|---|---:|
| entered-winner | 17 |
| entered-loser | 53 |
| entered-unmatched-close | 0 |
| never-entered | 476 |
| skipped-by-guard | 82 |

The guard label is best effort: a same-name `RUG-GUARD SKIP`, uppercase `SKIP`, or lowercase `skip ... (guardrails)` within two minutes and 12 following log lines marks a never-entered signal. It covers 82/558 signals without a matched entry (14.7%). It is an observed log association, not a complete reason code; repeated scans and generic guardrail skips may be capacity or route failures. Entered status uses positive native costed P&L for winner; all 70 matched entries here have closes. Three unmatched closes are omitted from feature attribution.

## Entered outcome and selection attribution

Each table uses the signal as logged for gain, ratio, liquidity, source, chain, early flag, and hour. The parsed `m15_gain_pct` key contains the displayed gain even when the log says `5m`: 63/628 signals and 4/70 matched entries have a 5m window, so gain buckets mix windows. The optional count, volume, market-cap, slippage, and latency values come from the matched entry record. `No entry` combines never-entered and skipped-by-guard. It is a count of signal observations, not distinct tokens. `Enter %` is entered / (entered + no entry) in that bucket and shows selection bias. Outcomes use entered closes only; net USD includes estimated costs. Buckets are descriptive and were not chosen on OOS labels.

### m15_gain_pct

| Bucket | Entered n | No entry n | Enter % | Win rate | PF | Net USD |
|---|---:|---:|---:|---:|---:|---:|
| 100-200 | 14 | 191 | 6.8% | 14.3% | 0.014 | $-42.97 |
| 200-400 | 15 | 146 | 9.3% | 6.7% | 0.029 | $-32.87 |
| <100 | 30 | 176 | 14.6% | 43.3% | 1.055 | $+1.88 |
| >=400 | 11 | 45 | 19.6% | 9.1% | 0.092 | $-45.85 |

### buy_sell_ratio

| Bucket | Entered n | No entry n | Enter % | Win rate | PF | Net USD |
|---|---:|---:|---:|---:|---:|---:|
| 2-4 | 28 | 242 | 10.4% | 28.6% | 0.332 | $-59.69 |
| <2 | 31 | 194 | 13.8% | 29.0% | 0.270 | $-33.67 |
| >=4 | 11 | 122 | 8.3% | 0.0% | 0.000 | $-26.46 |

### m15_buys

| Bucket | Entered n | No entry n | Enter % | Win rate | PF | Net USD |
|---|---:|---:|---:|---:|---:|---:|
| 25-74 | 1 | 0 | 100.0% | 0.0% | 0.000 | $-1.69 |
| >=75 | 2 | 0 | 100.0% | 0.0% | 0.000 | $-0.54 |
| missing | 67 | 558 | 10.7% | 25.4% | 0.264 | $-117.58 |

### m15_sells

| Bucket | Entered n | No entry n | Enter % | Win rate | PF | Net USD |
|---|---:|---:|---:|---:|---:|---:|
| 10-29 | 2 | 0 | 100.0% | 0.0% | 0.000 | $-0.54 |
| <10 | 1 | 0 | 100.0% | 0.0% | 0.000 | $-1.69 |
| missing | 67 | 558 | 10.7% | 25.4% | 0.264 | $-117.58 |

### liquidity_usd

| Bucket | Entered n | No entry n | Enter % | Win rate | PF | Net USD |
|---|---:|---:|---:|---:|---:|---:|
| 10-25k | 17 | 42 | 28.8% | 29.4% | 0.762 | $-6.37 |
| 25-50k | 21 | 188 | 10.0% | 38.1% | 0.136 | $-42.68 |
| <10k | 9 | 10 | 47.4% | 33.3% | 0.551 | $-8.07 |
| >=50k | 23 | 318 | 6.7% | 4.3% | 0.076 | $-62.70 |

### m15_volume_usd

| Bucket | Entered n | No entry n | Enter % | Win rate | PF | Net USD |
|---|---:|---:|---:|---:|---:|---:|
| 10-50k | 3 | 0 | 100.0% | 0.0% | 0.000 | $-2.23 |
| missing | 67 | 558 | 10.7% | 25.4% | 0.264 | $-117.58 |

### mcap_usd

| Bucket | Entered n | No entry n | Enter % | Win rate | PF | Net USD |
|---|---:|---:|---:|---:|---:|---:|
| 100-500k | 3 | 0 | 100.0% | 0.0% | 0.000 | $-2.23 |
| missing | 67 | 558 | 10.7% | 25.4% | 0.264 | $-117.58 |

### slip_from_signal_pct

| Bucket | Entered n | No entry n | Enter % | Win rate | PF | Net USD |
|---|---:|---:|---:|---:|---:|---:|
| <0 | 2 | 0 | 100.0% | 0.0% | 0.000 | $-0.54 |
| >=10% | 1 | 0 | 100.0% | 0.0% | 0.000 | $-1.69 |
| missing | 67 | 558 | 10.7% | 25.4% | 0.264 | $-117.58 |

### entry_latency_ms

| Bucket | Entered n | No entry n | Enter % | Win rate | PF | Net USD |
|---|---:|---:|---:|---:|---:|---:|
| 10-30s | 1 | 0 | 100.0% | 0.0% | 0.000 | $-0.31 |
| <10s | 1 | 0 | 100.0% | 0.0% | 0.000 | $-0.23 |
| >=30s | 1 | 0 | 100.0% | 0.0% | 0.000 | $-1.69 |
| missing | 67 | 558 | 10.7% | 25.4% | 0.264 | $-117.58 |

### source

| Bucket | Entered n | No entry n | Enter % | Win rate | PF | Net USD |
|---|---:|---:|---:|---:|---:|---:|
| dexscreener | 0 | 4 | 0.0% | 0.0% | 0.000 | $+0.00 |
| geckoterminal | 70 | 554 | 11.2% | 24.3% | 0.260 | $-119.81 |

### hour_utc

| Bucket | Entered n | No entry n | Enter % | Win rate | PF | Net USD |
|---|---:|---:|---:|---:|---:|---:|
| 06-11 | 1 | 4 | 20.0% | 100.0% | inf | $+2.21 |
| 12-17 | 29 | 184 | 13.6% | 24.1% | 0.204 | $-71.91 |
| 18-23 | 40 | 370 | 9.8% | 22.5% | 0.300 | $-50.12 |

### chain

| Bucket | Entered n | No entry n | Enter % | Win rate | PF | Net USD |
|---|---:|---:|---:|---:|---:|---:|
| bsc | 21 | 472 | 4.3% | 4.8% | 0.105 | $-43.71 |
| solana | 49 | 86 | 36.3% | 32.7% | 0.327 | $-76.10 |

### early

| Bucket | Entered n | No entry n | Enter % | Win rate | PF | Net USD |
|---|---:|---:|---:|---:|---:|---:|
| FOMO | 70 | 506 | 12.2% | 24.3% | 0.260 | $-119.81 |
| pre-pump | 0 | 52 | 0.0% | 0.0% | 0.000 | $+0.00 |

Optional field coverage is **3/70 entered (4.3%)** for m15 buys, m15 sells, m15 volume, market cap, signal-to-commit slip, and entry latency; **67/70 (95.7%) are null/missing**. The 558 signals without an entry have no corresponding entry fields, so entered-vs-never selection cannot be estimated for those features. Slip is a fraction: `0.10` means 10%. Source and early type cannot be judged by entered outcomes here: all 70 matched entries are GeckoTerminal FOMO signals; four DexScreener and 52 pre-pump observations have no matched entry.

## Signals that stand out, and weak discriminators

- Logged gain <100%: 30 entries, 43.3% wins, PF 1.055, +$1.88; 100–200%: 14, 14.3%, PF 0.014, -$42.97; ≥400%: 11, 9.1%, PF 0.092, -$45.85. This is descriptive. A <100% only rule had +$2.066/trade IS edge but only +$0.146/trade OOS edge (7.1% retained) and -$12.51 filtered OOS under stress; the prior 350% cap also reversed OOS. Neither is recommended.
- Ratio ≥4: 11 entries, zero winners, PF 0, -$26.46, with 11/133 (8.3%) signal observations entered. Ratio <2: 31 entries, 29.0% wins, PF 0.270, -$33.67, with 31/225 (13.8%) entered. The threshold 4 veto has only +$0.024/trade IS edge, so apparent separation is small in training.
- Liquidity ≥$50k: 23 entries, one winner (4.3%), PF 0.076, -$62.70, versus $10–25k: 17, 29.4%, PF 0.762, -$6.37. Entry selection is already lower at high liquidity (23/341, 6.7%) than at $10–25k (17/59, 28.8%). The round-2 $40k cap retained only eight OOS trades and stayed negative under 3× stress; it is not reused.
- UTC hour buckets 12–17 and 18–23 have similar win rates (24.1% and 22.5%) and both lose money (-$71.91 and -$50.12). The 06–11 bucket has only one entered trade. These do not support a time veto. BSC has 21 entries, one winner, -$43.71, versus Solana 49 entries, 16 winners, -$76.10; chain is strongly confounded with the newer OOS period and different stakes.
- **Do not retest from this snapshot alone:** ratio <2 vs 2–4 has nearly identical win rates (29.0% vs 28.6%); m15 buy/sell counts, volume, market cap, slip and latency have only three observed entries each; source and pre-pump have zero entered comparison group; 06–11 UTC has only one observed entry (one winner, +$2.21); 00–05 has no observed entries. These fields have no support for a new threshold. The $25–50k liquidity bucket has 38.1% wins but PF 0.136 and -$42.68, showing that win rate alone is misleading.

## Time ordered validation of the simplest testable veto

Only two ratio ceilings were considered for tuning: veto ≥3 or ≥4. The first 47 fully observed matched entries, sorted by entry timestamp converted to UTC using the launcher zone, are IS; the final 23 are untouched OOS. Naive timestamp strings cannot be sorted directly across the EDT/UTC switches. The IS per-trade edge for ≥3 was -$0.001; ≥4 was +$0.024, so ≥4 was selected on IS only. The rule uses one recorded ratio and no imputation. The normalized entry period runs from approximately 2026-09-24 11:56:37 UTC to 2026-09-27 19:18:53 UTC; the first entry precedes the earliest recorded launcher zone and inherits EDT. Because the logged ratio is available for all 70 matched entries, the validation has 47/23 trades. This is an offline exclusion estimate; skipped entries could have changed later capacity and the observed paper outcomes do not establish future returns or fills.

Metrics are **n / net USD / win rate / PF / max USD drawdown**:

| Split | Unfiltered | Ratio <4 retained | Edge per retained trade |
|---|---|---|---:|
| IS | 47 / $-69.36 / 34.0% / 0.348 / $76.24 | 41 / $-59.53 / 39.0% / 0.383 / $66.75 | $+0.024 |
| OOS | 23 / $-50.46 / 4.3% / 0.092 / $50.46 | 18 / $-33.83 / 5.6% / 0.132 / $33.83 | $+0.314 |
| IS 3× slippage | 47 / $-71.53 / 34.0% / 0.335 / $78.28 | 41 / $-61.45 / 39.0% / 0.370 / $68.29 | $+0.023 |
| OOS 3× slippage | 23 / $-51.26 / 4.3% / 0.090 / $51.26 | 18 / $-34.49 / 5.6% / 0.128 / $34.49 | $+0.313 |

OOS edge retention exceeds the 60% bar (+$0.314 versus +$0.024 IS), and OOS win rate is 5.6%, below the >90% overfit flag. Yet the retained OOS set has **one winner among 18**, so positive P&L is necessarily concentrated in one winner/regime, failing the concentration check used in `train_model.py` (which requires at least three winners across multiple chain/liquidity/gain/hour buckets). More decisively, filtered OOS remains **-$34.49** under 3× slippage (PF 0.128), failing the mandatory stress bar. **Reject shipping the veto.** The persisted candidate has `ship_recommend=false`; `approve()` therefore passes all signals in this offline module. No bot, entry rule, guard, size, or risk setting is changed.

## Reproduction

The implementation exposes `join_signals`, `attribution_rows`, and `validate_ratio_veto` in `analysis/signal_filter.py`; `analysis/signal_filter.json` records the split and metrics. Input is read-only; no HTTP or extra package is needed. Full hermetic test count is recorded in `CHANGELOG.md`.
