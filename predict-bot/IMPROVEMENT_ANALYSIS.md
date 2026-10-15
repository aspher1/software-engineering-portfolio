# Kalshi 15-minute crypto scanner: paper-data improvement analysis

Analysis snapshot: `data/signal_grades.jsonl` through the Oct 4, 2026 20:49 UTC grading record, joined by `key` to `data/same_day_loop_state.json`; `data/contrarian_drops.jsonl` used only for the agreement-filter shadow check. This is a paper-signal analysis, not an order or a claim of achievable trading returns.

## Executive summary

- The 892 Kalshi-settled signals won **565/892 = 63.3%** if held to settlement. Entry mid and edge exist on 853; their mean taken-side market price was 60.8%, while the model implied 70.7% and outcomes were 64.0%. The model overstated win probability by **6.7 percentage points (pp)** on this priced cohort.
- Larger stated edge did **not** yield a higher win rate: 64.9% at 5–10¢ edge (541 signals), 62.5% at 10–15¢ (192), and 62.5% at 15¢+ (120). Price-adjusted paper outcome improved with larger edge, but only about **0.23–0.48 pp of realized lift per 1 pp of claimed edge** across these buckets.
- The 50¢ agreement gate is supported for paper selection. The historical priced population below 50¢ won **22/128 = 17.2%**, versus **524/725 = 72.3%** at or above 50¢. Those groups also had very different market-implied base rates (19.8% versus 68.0%); the improvement is not a 55 pp model-skill gain. The current gate was introduced after the low-price cohort, so this is a historical comparison.
- **492/853 = 57.7%** of priced signals exited within 2.5 minutes and **613/853 = 71.9%** within 4.5 minutes, using alert and exit timestamps as approximations for one and two scans. Fast edge exits are common, but the latter group still won **67.4%** at settlement and had a **+6.2¢** mean hold-to-settlement paper payoff per contract. A persistence gate could discard useful signals.
- At logged mids, the observed exit-or-settle policy averaged **+6.8¢** per priced paper signal versus **+3.2¢** from always holding, a **+3.5¢** difference. This does **not** establish executable profitability: exit bid/ask and fees are absent, and entry price is also a mid.
- A hard 5-minute minimum is contradicted by the available entry-time data: `<5` minutes won **82/108 = 75.9%** with **+8.1¢** mean paper payoff; `5–10` won **222/312 = 71.2%** with **+3.6¢**. Keep these signals while testing persistence separately.
- XRP is the clearest symbol concern. Among 84 post-agreement XRP signals, actual win rate was **66.7%** against a **72.0%** mean market price, or **−5.4¢** per paper contract; Oct 4 alone was **−8.7¢** on 36. SOL was **+13.7¢** on 85 post-agreement signals. Treat either symbol rule as a prospective experiment because symbol comparisons were selected after seeing the data.
- Oct 4 does **not** show a demonstrated new regime: by entry date it won **218/299 = 72.9%**, versus **86/114 = 75.4%** on Oct 3 and **247/356 = 69.4%** on Oct 2–3 combined. The latest 50 graded signals were **34/50 = 68%**; a change from 74% to 68% is only three wins in a 50-signal window. Older Oct 1 signals, before the agreement gate, explain much of the low long-run average.

## Methods and limits

`correct` means the alerted side matched Kalshi's published `actual`. All accuracy figures below use only `proxy == "kalshi"`; 411 Kraken-proxy rows are excluded. One paper contract bought at the **entry taken-side mid** has settlement payoff `correct − entry_price` dollars. This gross paper payoff is useful for comparing different base prices; it excludes spread, execution and fees. Model-predicted win probability is `entry taken-side mid + abs(edge)`. Exit payoff uses the logged exit **mid** minus entry mid, or settlement payoff where no exit was logged. An exit is an advisory scanner event, not evidence of a filled sale.

Fields arrived in stages: 892 rows have outcomes, 853 have entry mid/edge and state entry time, 684 have minutes-left/spread, 485 have volume. Missing groups are historical cohorts, **not random missing observations**. The 853 priced signals span 816 unique tickers; opposite sides can occur in the same window, so row-level comparisons are correlated. UTC hour is taken from state `ts`; entry date and close date can differ near midnight. The source logs are changing while the bot runs; all counts are a snapshot.

## Accuracy decomposition and edge calibration

Mean paper payoff below is dollars per one contract bought at the mid, shown as cents. A cell with fewer than 20 observations is marked `low N`.

| Side | N | Held win rate | Mean entry price | Mean paper payoff |
|---|---:|---:|---:|---:|
| YES | 480 | 62.7% | 62.3¢ | +0.6¢ |
| NO | 412 | 64.1% | 59.0¢ | +6.3¢ |

The 39 old rows without a mid contribute to win rates but not prices or payoffs. Side differences may reflect the changing entry policy and price mix.

| Taken-side edge | N | Held wins | Mean market price | Mean model probability | Mean paper payoff |
|---|---:|---:|---:|---:|---:|
| 0–5¢ | 0 | — | — | — | — |
| 5–10¢ | 541 | 351/541 = 64.9% | 63.3% | 70.3% | +1.6¢ |
| 10–15¢ | 192 | 120/192 = 62.5% | 58.5% | 70.3% | +4.0¢ |
| 15¢+ | 120 | 75/120 = 62.5% | 52.9% | 72.8% | +9.6¢ |
| Missing edge | 39 | 19/39 = 48.7% | — | — | — |

The 5¢ entry threshold explains the empty 0–5¢ bucket. On the 853 priced signals, mean claimed edge was **9.9¢**, but actual wins exceeded market mid by only **3.2 pp**. In the three edge buckets the realized lift over mean market price was **1.6, 4.0, and 9.6 pp**, compared with claimed edges of **7.0, 11.8, and 19.9 pp**. A 10¢ edge thus does **not** produce roughly 10 pp of observed lift. On the 725 historical signals that meet the present agreement gate, mean realized lift was **4.3 pp** against roughly **9.9 pp** claimed. Calibration varied by day: realized lift/claimed edge was 0.7/10.1 pp on Oct 2, 4.8/9.5 on Oct 3, and 4.1/10.2 on Oct 4. A single global correction needs prospective validation.

| Minutes left at entry | N | Held win rate | Mean market price | Mean paper payoff |
|---|---:|---:|---:|---:|
| `<5` | 108 | 75.9% | 67.8¢ | +8.1¢ |
| `5–10` | 312 | 71.2% | 67.5¢ | +3.6¢ |
| `10–15` | 264 | 70.1% | 68.5¢ | +1.6¢ |
| Missing | 208 | 36.5% | 31.8¢ on priced subset | +2.0¢ on priced subset |

The missing group is dominated by older, lower-priced signals. The observed `<5` group outperformed `5–10` by **4.7 pp** on 108 versus 312 signals; a hard minimum would remove the best observed time bucket. These results do not establish that late entry causes better outcomes.

| Symbol | All N | All held win rate | Post-agreement N | Post-agreement win rate | Post-agreement paper payoff |
|---|---:|---:|---:|---:|---:|
| BTC | 127 | 66.1% | 107 | 73.8% | +4.7¢ |
| ETH | 110 | 62.7% | 91 | 70.3% | +3.3¢ |
| SOL | 105 | 73.3% | 85 | 84.7% | +13.7¢ |
| XRP | 104 | 59.6% | 84 | 66.7% | −5.4¢ |
| NEAR | 101 | 61.4% | 82 | 74.4% | +7.6¢ |
| BNB | 101 | 57.4% | 87 | 64.4% | −0.2¢ |
| HYPE | 85 | 65.9% | 67 | 71.6% | +5.7¢ |
| DOGE | 81 | 66.7% | 64 | 76.6% | +7.2¢ |
| ZEC | 78 | 55.1% | 58 | 67.2% | +1.9¢ |

XRP's post-agreement outcome is **5.3 pp below its 72.0% mean market price**, while SOL is **13.7 pp above its 71.0% mean price**. The 9-symbol comparison is exploratory; do not translate it into automatic real allocation.

| Entry YES spread | N | Held win rate | Mean market price | Mean paper payoff |
|---|---:|---:|---:|---:|
| `<2¢` | 434 | 73.5% | 69.5¢ | +4.0¢ |
| `2–<4¢` | 193 | 66.3% | 66.1¢ | +0.3¢ |
| `4–<6¢` | 44 | 75.0% | 62.1¢ | +12.9¢ |
| `6¢+` | 13, low N | 69.2% | 65.1¢ | +4.1¢ |
| Missing | 208 | 36.5% | 31.8¢ on priced subset | +2.0¢ on priced subset |

Narrow spreads correlate with higher raw accuracy, but the `<2¢` versus `2¢+` comparison is **73.5% versus 68.0%** with different entry prices (**69.5¢ versus 65.3¢**). The `4¢+` group has **57** observations and **+10.9¢** mean paper payoff. No monotone accuracy or mid-payoff case exists for a hard spread cutoff. Wider spreads do raise unmeasured execution cost.

| Entry volume | N | Held win rate | Mean market price | Mean paper payoff |
|---|---:|---:|---:|---:|
| `<1k` | 7, low N | 71.4% | 56.3¢ | +15.1¢ |
| `1k–<10k` | 180 | 70.6% | 66.0¢ | +4.6¢ |
| `10k–<50k` | 190 | 75.3% | 71.8¢ | +3.5¢ |
| `50k+` | 108 | 75.0% | 69.6¢ | +5.4¢ |
| Missing | 407 | 51.4% | 50.1¢ on priced subset | +1.5¢ on priced subset |

The available `<10k` group (187) won **70.6%** versus **75.2%** at `10k+` (298), but had **+5.0¢** versus **+4.2¢** mean mid-payoff. Volume is not a justified hard accuracy gate in this sample. A `<1k` execution-quality review is sensible, but seven outcomes cannot establish a threshold.

| UTC entry hour | N | Held win rate | Mean market price | Mean paper payoff |
|---|---:|---:|---:|---:|
| 00–04 | 102 | 66.7% | 68.5¢ | −1.8¢ |
| 04–08 | 168 | 60.7% | 55.1¢ | +5.6¢ |
| 08–12 | 183 | 52.5% | 49.4¢ | +3.1¢ |
| 12–16 | 130 | 57.7% | 62.4¢ | −4.7¢ |
| 16–20 | 147 | 73.5% | 67.5¢ | +6.0¢ |
| 20–24 | 123 | 78.9% | 69.4¢ | +9.4¢ |
| Missing entry time | 39 | 48.7% | — | — |

Time of day is heavily confounded by policy date: Oct 3 has **no** 00–12 UTC entry cohort, and Oct 4's 08–12 UTC cohort won **77.1% on 48**, unlike the pooled 52.5%. Even the apparently poor 12–16 period varies by day: Oct 2 was **54.8% on 42** and Oct 4 **70.5% on 44**. Do not impose hour blocks from this pooled table.

## Early exits, paper exit value, and hold counterfactual

Of 853 priced and timestamped settled signals, **681 (79.8%)** have a logged `edge_gone` or `edge_reversed` exit; **172** have no exit. Among the 681 exits, **492 (72.2%)** occurred within 2.5 minutes and **613 (90.0%)** within 4.5 minutes of entry. Exit lag is measured from state timestamps; scan intervals are approximate, and 256 exits happened within 1 minute. The 4.5-minute group would have won **413/613 = 67.4%** if held and had **+6.2¢** mean settlement payoff. Early collapse of modeled edge does not itself imply a bad settlement direction.

Fast exits are especially common at lower taken-side prices: **188/224 = 83.9%** for 50–<60¢, **141/175 = 80.6%** for 60–<70¢, **116/154 = 75.3%** for 70–<80¢, **61/105 = 58.1%** for 80–<90¢, and **7/26 = 26.9%** for 90¢+; all these have minutes-left metadata. On the 485 volume-tagged signals, **154/180 = 85.6%** at 1k–<10k exited within 4.5 minutes, versus **65/108 = 60.2%** at 50k+. At 2–<4¢ spread it was **160/193 = 82.9%**, versus **308/434 = 71.0%** below 2¢. Conversely, high edge was *less* quick-exit prone: **76/120 = 63.3%** at 15¢+ versus **399/541 = 73.8%** at 5–<10¢. Among model probabilities at least 90%, **67/141 = 47.5%** still exited within 4.5 minutes. Thus high model confidence does not ensure persistence.

| Scanner exit status | N | Settlement wins if held | Mean exit payoff at mid | Mean settlement payoff if held | Median exit payoff |
|---|---:|---:|---:|---:|---:|
| `edge_gone` | 268 | 66.8% | +7.6¢ | +6.9¢ | +8.0¢ |
| `edge_reversed` | 413 | 65.4% | +10.7¢ | +3.8¢ | +12.0¢ |
| No exit | 172 priced; 211 total | 56.4% priced; 55.0% total | — | −3.9¢ priced | — |

For exited signals, the mean exit benefit over holding is **+0.7¢** for `edge_gone` and **+6.9¢** for `edge_reversed`; averaging exits with unexited settlement gives **+6.8¢ versus +3.2¢** over all 853 priced signals. Individual exits still lose: **20.1%** of `edge_gone` exit mids and **11.9%** of `edge_reversed` exit mids were below entry mid. The observed exit logic appears useful at mid prices, especially for reversed edges; there is no logged executable exit bid/ask to verify the advantage after spread and fees. Hold-to-settlement accuracy of *all* Kalshi-settled rows remains **63.3%**; on the priced subset it is **64.0%**. Exit events do not change those settlement facts.

The hold guard's alert is a peak-to-current taken-side price decline, while entry-to-current paper P&L is a separate quantity. Its current `taken_price` conversion in `scripts/hold_guard.py` uses `1 − YES mid` for NO, but any advisory wording such as “protect profit” must be based on independently calculated `current taken price − entry taken price` and on that quantity being positive. Do not use the decline amount as entry P&L.

## Agreement filter and deterioration

On all 853 historical priced, settled signals, removing the 50¢ filter in retrospect gives **546/853 = 64.0%** held accuracy and **+3.2¢** mean mid payoff. Applying it gives **524/725 = 72.3%** and **+4.3¢**; the excluded `<50¢` cohort gives **22/128 = 17.2%** and **−2.6¢**. Because the excluded cohort's mean market price was only **19.8¢**, its low raw accuracy is partly expected; still, it underperformed that baseline by **2.6 pp** and had negative mid payoff. Of 1,523 shadow `contrarian_drops` scan events, 733 are distinct ticker/side combinations; only 246 have a matched Kalshi settlement in the graded-signal set, and those matched events won **83/246 = 33.7%**. That match is highly selected and may include both sides of one ticker at different times, so it is a consistency check, **not** an unbiased full-population estimate. The shadow log lacks outcomes for the other 487 distinct dropped combinations. Retain 50¢; moving it to 55¢ or 60¢ raises raw accuracy to **74.5% on 616** and **77.1% on 490**, but reduces mean mid payoff to **+3.7¢** and **+2.9¢** from **+4.3¢** at 50¢. The 50–<55¢ bucket itself won **59.6% on 109** at a **52.3¢** mean price and **+7.3¢** payoff.

| Entry date UTC | N with mid | Held win rate | Mean market price | Mean mid payoff |
|---|---:|---:|---:|---:|
| Oct 1 | 198 | 40.9% | 36.8¢ | +4.1¢ |
| Oct 2 | 242 | 66.5% | 65.8¢ | +0.7¢ |
| Oct 3 | 114 | 75.4% | 70.6¢ | +4.8¢ |
| Oct 4 | 299 | 72.9% | 68.8¢ | +4.1¢ |

The date table is truncated at the snapshot time and Oct 3 has partial-hour coverage. Oct 4 versus Oct 3 is **−2.5 pp**, roughly three wins per 114 signals; Oct 4 versus Oct 2–3 combined is **+3.5 pp**. The rolling 50 going from 74% to 68% is three outcomes, consistent with ordinary sampling variation. The latest 50 had **34 wins**, mean entry price **68.1¢**, and **−0.1¢** mean mid settlement payoff. Accuracy alone can look healthy while price-adjusted paper outcome does not. The older reported Oct 2 “475 rows, 54.11%, −$0.0505/signal” was a different snapshot/metric; the present log contains 892 Kalshi-settled rows, and its current priced-cohort entry-mid payoff is **+$0.032/signal**. Neither mid payoff includes spread/fees, and this report does not infer bot profitability.

## Prioritized improvements

1. **Fix probability reporting and calibrate edge before changing decisions.** In `scripts/same_day_loop.py`, calculate a shadow `fair_calibrated_taken = entry_price_taken + 0.4 × abs(edge)` and log it beside the current fair; leave the current decision path intact until a prospective check. The 0.4 starting coefficient is close to the post-agreement realized lift/claimed-edge ratio (**4.3/9.9 ≈ 0.43**), and the existing fair overstated outcomes by **5.6 pp** even after agreement. Validate with a chronological, ticker/window-grouped holdout: fit the coefficient on earlier days, then compare calibration tables, Brier score, and settlement payoff on new Kalshi-settled signals. If the coefficient is unstable, use price/edge buckets or a wider uncertainty range rather than one global value. Do not label uncalibrated `fair` as a reliable win probability.
2. **Measure a concrete persistence gate in shadow mode.** In `scripts/same_day_loop.py` near the `pending_entries`/dedupe path, record an eligible candidate at the first scan; issue a paper signal only after **two scans at least 90 seconds apart** for the same ticker and side, both with taken-side edge **≥5¢**, taken-side mid **≥50¢**, and **≥2 minutes left** at confirmation. Initially shadow-log both the original signal and the gate's hypothetical accept/reject decision, because the current files do not contain fair values at the next scan for rejected candidates. It targets the **613/853** exits inside 4.5 minutes, especially 50–<60¢ and 1k–<10k-volume signals, but can also reject winners: the quick-exit group held to settlement has **+6.2¢** mean mid payoff. Validate at least several full days of Kalshi outcomes, grouped by window, comparing signal count, calibrated edge, net-of-quoted-spread paper payoff and exit timing against the original path. Require improvement in **payoff**, not merely fewer exits or higher win rate, before enabling the gate.
3. **Log executable paper exit prices and compare exit policies.** In `scripts/same_day_loop.py` `check_exits`, log the live YES bid/ask, entry-side ask, taken-side exit bid, quoted spread, and a stable signal/exit link. Compute side-correct prices: YES entry ask/exit bid; NO entry `1 − YES bid`/exit `1 − YES ask`, subject to actual market complement conventions. Current logged mids show **+3.5¢** policy advantage over holding and **+6.9¢** specifically for `edge_reversed`, but execution cost could erase part of this. Validate all policy variants on the same settled entries with quote-side paper prices and fees, including no exit, current exit, and `edge_reversed`-only exit; preserve advisory/paper-only operation.
4. **Run an XRP suppression shadow test.** In the symbol decision path of `scripts/same_day_loop.py`, shadow a rule `if sym == "XRP": suppress entry`; continue logging its original paper signals and Kalshi outcomes. XRP post-agreement had **−5.4¢** mean mid payoff on **84** and Oct 4 **−8.7¢** on **36**, while SOL was **+13.7¢** on **85**. Validate on future, full-day cohorts by comparing total paper payoff per available window, not just accuracy among accepted signals, with uncertainty grouped by window/day. Enable a symbol filter only if the deficit persists after price and time-of-day adjustment. Do not increase SOL weight from this retrospective ranking.
5. **Repair hold-guard alert arithmetic and wording before treating it as decision support.** In `scripts/hold_guard.py`, add separately named `peak_to_now_drop` and `entry_to_now_pnl = now_price_taken − entry_price_taken`; compute both in taken-side terms. Show “protect paper profit” only when the latter is **strictly positive**; otherwise describe a paper loss or adverse move. The operator observed contradictory alerts, and the existing alert carries `drop_pts` but no explicit entry P&L. Validate with unit examples for YES/NO, a profitable pullback, and a losing pullback; verify messages against the logged entry/now prices. Keep this hook advisory.
6. **Improve monitoring before adding time, spread or volume gates.** In `scripts/same_day_loop.py` `accuracy_stats` or a separate analysis job, report rolling Kalshi-only win rate **beside** mean taken-side price, calibrated probability, paper mid payoff, number of independent windows, and the entry-policy cohort. Alert on prospective price-adjusted deterioration over multiple nonoverlapping windows rather than a three-win change in 50. Keep spread and volume as logged covariates: `<2¢` spread's raw **73.5%** versus `2¢+` **68.0%** mostly tracks price mix, and `<10k` volume's **70.6%** versus `10k+` **75.2%** did not have worse mid payoff. Validate any future threshold by quote-side payoff on new settled windows.

## What not to change from this evidence

- **Keep the 50¢ agreement gate.** The excluded historical cohort lost **2.6¢** per entry-mid paper contract; raising the gate to 55¢/60¢ would remove positive-payoff 50–<60¢ cohorts.
- **Do not impose a `<5` minute cutoff.** It was the strongest available minutes-left bucket on both win rate and mean entry-mid payoff (108 observations).
- **Do not use flips as directives or let Gemini advice place paper decisions automatically.** The supplied flip observations are advisory; these grading files do not establish an incremental predictive edge from them.
- **Do not remove edge exits merely because they fire quickly.** At logged mids the present exit-or-settle rule beat hold by **3.5¢** per priced signal. Reassess only with quoted exit prices and fees.
- **Do not hard-filter by UTC hour, wide spread, low volume, or promote SOL yet.** Pooled hours are date-confounded, spread/volume effects are not monotone in price-adjusted payoff, and the best-symbol selection is retrospective.
