# X3 low-gain signal re-test: preregistration

Written before running the X3 outcome calculation. This is an offline paper-journal exclusion test, not a live trading rule.

## Population and time

Use `replay.parse_signals`, `replay.load_journal`, `signal_filter.join_signals`, and `signal_filter.signal_values` without replacing their join logic. Include every joined row with a close record, an entry timestamp, and finite non-null `m15_gain_pct`. Report missing or invalid evidence separately. Sort by the journal `entry_ts` as an instant: the launcher mixed EDT and UTC, so add the join's UTC offset to each naive timestamp. Ties use log line index. First `floor(2N/3)` rows are in-sample (IS); the rest are out-of-sample (OOS). Calculate N and boundaries from the snapshot. Do not move the boundary after seeing returns.

## Fixed candidates and measurements

Candidate T in {75, 100, 125, 150} approves a signal only when its logged gain is strictly below T. At the exact threshold it vetoes. Missing, negative, nonfinite, or malformed gain fails open in the runtime evaluator and is counted as a fallback; invalid rows are outside the measured population. Select the best T by highest IS edge in USD per retained trade against the IS unfiltered baseline, breaking ties toward the lower T. Inspect all four OOS candidates without retuning T.

For each split and threshold, report retained n, costed USD P&L, profit factor, win rate, maximum drawdown, USD per retained trade, improvement in USD per retained trade versus unfiltered, and P&L with replay's threefold *slippage* stress. Use `replay.net_pnl`; max drawdown follows retained entry order and begins at zero. Also show the unfiltered baseline. No counterfactual exit changes are assumed. Avoid treating different retained counts as a causal comparison.

Concentration: list the largest positive contributors in each T/split and recalculate retained P&L with its largest one and two winners removed. Regimes: for the IS-selected T, compare Solana and BSC, then chronological early and late OOS halves, including empty cells. A regime with no observations cannot validate a rule. Plateau: the IS-selected T must have at least one adjacent threshold in the ordered grid with positive OOS edge improvement of the same direction. An isolated T fails. An improvement alone is insufficient if retained OOS P&L is negative.

If the selected gain-only rule has a positive OOS edge improvement, test exactly one corroborating feature: require `buy_sell_ratio < 4` alongside the IS-selected gain threshold. This band is fixed from Track C's descriptive ratio bucket. Report IS and OOS and accept it only if OOS edge improvement exceeds gain-only, with no regime, concentration, or stress failure. No other feature or band is searched.

## Shipping decision

All are necessary: positive costed retained OOS P&L and positive threefold-slippage-stress OOS P&L; OOS edge improvement in USD/trade is positive and at least 60% of IS edge improvement (equivalently no more than 40% decay, thus also satisfying the stated 70% decay ceiling); OOS win rate at most 90%; adjacent-threshold OOS plateau; no dependence on only one observed chain or only one OOS half; and no one- or two-trade concentration that overturns the outcome. An empty split or zero/negative IS edge cannot qualify. Failed gates produce `ship_recommend=false` and a written reason. A positive paper backtest does not establish live fills or profitability.

The evaluator must never raise on bad params, bad signal structure, None, NaN, negative gains, or empty splits. It must approve on uncertainty and count fallbacks. A `ship_recommend=false` file always approves. The report will audit signal timestamps against entry timestamps and the journal's commit-time evidence.
