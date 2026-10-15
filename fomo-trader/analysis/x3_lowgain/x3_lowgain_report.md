# X3 low-gain signal re-test

**Verdict: reject.** This offline paper-journal snapshot has 71 eligible closed signal joins, split chronologically into 47 IS and 24 OOS. The IS-selected veto is `m15_gain_pct >= 100` (keep strictly below 100). It improves OOS USD per retained trade relative to the unfiltered baseline by only **$0.180**, versus **$2.066** IS: **8.7% retained**. Its seven retained OOS trades lose **$13.94 costed**, or **$14.21** with threefold modeled slippage. `ship_recommend=false`; the evaluator passes signals through. No executable return claim follows from these paper quotes.

## Snapshot, split, and cost convention

The read-only inputs currently contain 637 parsed signals, 72 entries, and 74 closes; three closes lack entry records. `join_signals` found 71 entry anchors. Of its 637 signal rows, 566 lacked a matched closed trade. Among matched closed rows, no gain or timestamp was invalid or post-entry. The specified `floor(2N/3)` split is 47/24: the last IS journal `entry_ts` is `2026-09-24 16:30:09`, and the first OOS value is `2026-09-24 17:05:01`. Their normalized UTC instants are 20:30:09 and 21:05:01. The raw journal clocks mix EDT and UTC, so ordering applies the launcher's logged UTC offset. No boundary was moved after seeing returns.

Returns use `replay.net_pnl` on recorded paper closes, which subtracts estimated swap, slippage, and fixed transaction costs. The stress column uses its `stress=True`, which triples **slippage**, not all costs. PF is positive net USD divided by absolute negative net USD; win rate counts positive net native P&L; drawdown is the maximum fall in cumulative costed USD starting at zero. An empty PF is serialized as `null`. The baseline is 47 / -$69.36 / PF 0.348 / 34.0% wins / $76.24 DD IS and 24 / -$52.11 / PF 0.090 / 4.2% wins / $52.11 DD OOS. Baseline stress P&L is -$71.53 IS and -$52.95 OOS.

## Fixed threshold sweep

`T` keeps only rows with logged gain strictly below T. Edge is retained USD/trade minus unfiltered USD/trade in the same split. The best IS edge selected T=100; OOS did not select or change the threshold.

| T | Split | Kept | Costed P&L | PF | Win rate | Max DD | USD/trade | Edge vs baseline | 3× slip P&L |
| ---: | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 75 | IS | 20 | +$5.43 | 1.332 | 45.0% | $10.22 | +$0.272 | +$1.747 | +$4.46 |
| 75 | OOS | 4 | -$8.76 | 0.369 | 25.0% | $13.88 | -$2.189 | -$0.018 | -$8.91 |
| **100** | **IS** | **24** | **+$14.16** | **1.853** | **50.0%** | **$7.26** | **+$0.590** | **+$2.066** | **+$12.95** |
| **100** | **OOS** | **7** | **-$13.94** | **0.269** | **14.3%** | **$17.41** | **-$1.991** | **+$0.180** | **-$14.21** |
| 125 | IS | 27 | +$7.81 | 1.338 | 48.1% | $7.57 | +$0.289 | +$1.765 | +$6.43 |
| 125 | OOS | 10 | -$16.20 | 0.240 | 10.0% | $19.36 | -$1.620 | +$0.551 | -$16.58 |
| 150 | IS | 29 | -$2.13 | 0.936 | 48.3% | $17.95 | -$0.073 | +$1.402 | -$3.59 |
| 150 | OOS | 11 | -$22.88 | 0.183 | 9.1% | $26.03 | -$2.080 | +$0.092 | -$23.27 |

The adjacent-threshold directional plateau test **passes narrowly**: T=125, adjacent to selected T=100, also has a positive OOS *improvement versus a losing baseline*. T=75 does not. No threshold has positive retained OOS P&L, and T=125's apparent better per-trade improvement still corresponds to a $16.20 loss. Thus the plateau is not a profitable region. The 100% point is also weaker than its own IS result by 91.3% on the preregistered edge metric, failing both the 60% retention and 70% maximum decay tests.

## Concentration

The JSON records the top three contributors and P&L after removing the largest one and two **positive** winners for every threshold and split. Top two IS trades were Binancians / SOL (+$8.94) and musebook / SOL (+$7.49), only 91 seconds apart. For selected T=100, removing them changes IS +$14.16 to **-$2.27**. One of them alone leaves +$5.22. This is a local cluster, not independent confirmation. OOS has exactly one positive retained trade at every T: Bukangi / WBNB (+$5.12). The selected T=100 OOS result is already -$13.94; without that winner it is **-$19.06**.

| T | IS top two positive contributors | IS P&L without top two | OOS positive contributor | OOS P&L without it |
| ---: | :--- | ---: | :--- | ---: |
| 75 | Binancians +$8.94; musebook +$7.49 | -$11.00 | Bukangi +$5.12 | -$13.88 |
| 100 | Binancians +$8.94; musebook +$7.49 | -$2.27 | Bukangi +$5.12 | -$19.06 |
| 125 | Binancians +$8.94; musebook +$7.49 | -$8.62 | Bukangi +$5.12 | -$21.33 |
| 150 | Binancians +$8.94; musebook +$7.49 | -$18.56 | Bukangi +$5.12 | -$28.00 |

The IS positive P&L is therefore dependent on two nearby entries at the selected threshold. OOS does not have a positive net edge to concentrate, but its only positive trade is in one chain and the late half.

## OOS regimes for T=100

| Regime | Kept | Costed P&L | PF | Win rate | Edge vs regime baseline | 3× slip P&L |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| Solana | 2 | -$6.81 | 0.000 | 0.0% | -$0.560/trade | -$6.88 |
| BSC | 5 | -$7.13 | 0.418 | 20.0% | +$0.650/trade | -$7.33 |
| Early OOS half | 5 | -$17.41 | 0.000 | 0.0% | -$0.217/trade | -$17.57 |
| Late OOS half | 2 | +$3.47 | 3.098 | 50.0% | +$2.812/trade | +$3.37 |

The relative improvement comes from BSC and the late half; Solana and the early half worsen relative to their own baselines. Both chains lose dollars. The late half's gain is just two retained trades, including Bukangi. This fails the preregistered no-single-regime requirement.

## One corroborating feature

Because selected gain-only T=100 had a small positive OOS *relative* edge, the preregistered single combination was evaluated: keep `m15_gain_pct < 100` **and** `buy_sell_ratio < 4`. It kept 21 IS entries for +$18.10 (PF 2.428, 57.1% wins, $6.72 DD, +$2.337 edge/trade, +$17.00 stress) and six OOS entries for **-$6.87** (PF 0.427, 16.7% wins, $10.34 DD, +$1.026 edge/trade, **-$7.12 stress**). It improves OOS edge/trade over gain-only but still loses costed dollars and fails stress. It does not rescue the rule. No other band was tried.

## Look-ahead audit and limits

All 71 matched signal timestamps are at or before their entry timestamps; observed signal-to-entry lag is 1–46 seconds. The join takes the latest same-name, same-chain signal within 30 minutes before entry. The gain and ratio in the log exactly match the values copied into all 71 journal entry records as `signal_gain_pct` and `signal_ratio` at commit. The entry paths in `fomo_trader.py` write those fields from the signal before logging the entered trade. No close, peak, P&L, or future price is used to decide a candidate's eligibility. Outcome labels enter only the retrospective scoring and IS threshold selection. The code's population gate also excludes post-entry or undated signals.

The parsed `m15_gain_pct` name is not always a true 15-minute measurement: four eligible signals show a `5m` window, and two of the 31 T=100 retained rows have it. The log value is still known before entry, but this mixed measurement weakens interpretation. Matching depends on log date anchoring, name normalization, and launcher timezone markers; the exact timestamp audit does not prove that a similarly named repeated token was always the intended source. The paper dataset contains entries actually taken under the old policy, so vetoed trades may have altered later capital, scheduling, or risk limits. The test cannot verify live fills or future profitability.

## Decision

`ship_recommend=false` because edge retention is 8.7% rather than at least 60%; selected retained OOS costed and stress P&L are negative; both chains lose; early OOS loses; and IS profit vanishes when two nearby winners are removed. The plateau and win-rate checks pass, but they do not overcome those failures. The exact deployed rule is **none**. The JSON's `rule` field records the rejected IS-selected candidate solely for reproducibility; `approve()` returns true while `ship_recommend=false`.
