# Round 5 enriched-entry descriptive report

Run: 2026-09-28 00:07 UTC. Frozen pre-registration: `ROUND5_PREREGISTRATION.md`. **Verdict: awaiting data; ship_recommend=false.**

Journal has 99 entries and 99 closes. 29 paired enriched closes from 32 enriched entries; all are BSC. The data trigger is 30 paired enriched closes. No candidate veto was evaluated.

Costed figures use `analysis/replay.py` fee, slippage, fixed transaction cost, and USD conversion, applied to observed paper quotes. These are estimates, not executable fills or a profitability claim.

## Chronological split

| Split | n | Costed net USD | Win rate | Profit factor | Max drawdown USD |
|---|---:|---:|---:|---:|---:|
| IS | 19 | $-13.84 | 21.1% | 0.47 | $18.26 |
| OOS | 10 | $-3.81 | 20.0% | 0.58 | $6.40 |

## Descriptive attribution

Buckets are fixed reporting bins. They are not candidate cutoffs and were not used to tune a veto. Hour is the journal timestamp hour; launcher time-zone changes make it unsuitable for a causal time-of-day conclusion.

| Attribute | Bucket | n | Costed net USD | Win rate |
|---|---|---:|---:|---:|
| chain | bsc | 29 | $-17.66 | 20.7% |
| commit_liquidity | 20-40k | 3 | $-6.18 | 0.0% |
| commit_liquidity | 40-80k | 20 | $-12.26 | 20.0% |
| commit_liquidity | >=80k | 6 | $0.78 | 33.3% |
| signal_gain | 100-200% | 10 | $-12.77 | 10.0% |
| signal_gain | 50-100% | 5 | $-2.49 | 40.0% |
| signal_gain | <50% | 3 | $6.31 | 66.7% |
| signal_gain | >=200% | 11 | $-8.70 | 9.1% |
| slip_from_signal | -10-0% | 4 | $0.57 | 50.0% |
| slip_from_signal | 0-10% | 5 | $-2.14 | 20.0% |
| slip_from_signal | <-10% | 12 | $1.32 | 25.0% |
| slip_from_signal | >=10% | 8 | $-17.41 | 0.0% |
| buys_sells_ratio | 1-2 | 8 | $-5.61 | 12.5% |
| buys_sells_ratio | 2-4 | 11 | $-3.73 | 36.4% |
| buys_sells_ratio | >=4 | 10 | $-8.32 | 10.0% |
| mcap | 100-500k | 22 | $-16.49 | 13.6% |
| mcap | 500k-1m | 2 | $0.59 | 50.0% |
| mcap | <100k | 4 | $-0.76 | 50.0% |
| mcap | >=1m | 1 | $-1.00 | 0.0% |
| hour_of_day | 16 | 3 | $-2.03 | 33.3% |
| hour_of_day | 17 | 6 | $-13.13 | 0.0% |
| hour_of_day | 18 | 6 | $0.75 | 33.3% |
| hour_of_day | 19 | 11 | $-4.92 | 18.2% |
| hour_of_day | 20 | 3 | $1.67 | 33.3% |

The ≥10% slip bucket has 8 closes and −$17.41 costed net; the other 21 sum to about −$0.25. This is a descriptive concentration, not evidence that the frozen chase veto works OOS. The 20–40k liquidity bucket has only 3 closes; holder and LP fields have no observed values. All candidate decisions remain pending the data trigger and later verdict-capable sample gates.

No bot code, config, live run file, or running process was changed by this analysis.
