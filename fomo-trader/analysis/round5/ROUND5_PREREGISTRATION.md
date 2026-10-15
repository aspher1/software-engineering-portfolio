# Round 5 enriched-entry veto validation — frozen

Frozen **2026-09-28 00:06:52 UTC (2026-09-27 20:06:52 EDT)**, before the first Round 5 harness run. The candidate set and cutoffs below are fixed. This document is not revised to fit outcomes. No bot behavior changes in this experiment.

## Data and trigger

Use enriched `entry` journal rows carrying `slip_from_signal_pct`, paired FIFO to `close` by `(chain, mint)`. Preserve entry append order because journal timestamps can mix time zones. First floor(2n/3) closes are IS, remainder OOS. A run with fewer than **30 enriched closes** is descriptive only: verdict `awaiting data`, no candidate evaluation. At 30 or more it is provisional. A ship verdict requires **IS ≥100 and OOS ≥30** enriched closes, so at least 150 total under this split. Missing candidate fields are never imputed. If a candidate field is missing, it cannot support a veto finding; assess field coverage before interpreting any result.

## One coherent veto at a time

Each arm is the current entry stream with exactly one veto; no combinations or retuned thresholds.

1. **Holder concentration:** veto top-1 >40% or top-5 >75%, matching the existing Solana rug guard thresholds. **`not_runnable` on the current journal**: all 32 enriched entries are BSC, with no holder percentages. An eventual Solana sample needs both fields observed for every qualifying arm entry. This candidate never weakens the existing rug guard; any proposed screen would be redundant unless later evidence establishes a distinct upstream use. The harness implements the frozen joint top-1/top-5 rule; coverage is still required before evaluating it.
2. **Chase:** veto `slip_from_signal_pct > +10%`, interpreted as buying into a post-signal rip. The recorded field is a fraction, so the code cutoff is `0.10`.
3. **Commit liquidity:** veto `liquidity_usd < $40,000` at commit. Round 2 was inconclusive. The journal's existing `liquidity_usd` is the entry record's observed value.
4. **Entry latency:** veto `entry_latency_ms > 60,000` (60 seconds). This cutoff is a fixed operational one-minute cap, not selected from outcomes.

## Costs, selection, and gates

Use `analysis/replay.py`'s `net_pnl` and `metrics`: 25 bps swap fee plus 15 bps slippage per leg, two estimated chain transaction fees, native/USD conversion from the entry/close record. Stress triples **all modeled fees, slippage, and fixed transaction fees** (`cost_multiplier=3`). Costed net USD, win rate, profit factor, max drawdown, and n are reported for both splits. For a veto's edge, compare candidate and full baseline net USD per **original** trade in the same split, including vetoed opportunities at $0. IS edge must be positive and OOS edge must retain ≥60% of it. OOS must have positive stressed net. The candidate is frozen from IS before one OOS look; no threshold selection on OOS.

**KILL_VARIANT:** at ≥30 OOS closes, kill a candidate if its OOS costed net ≤$0. **STOP_ALL:** at ≥30 OOS closes, stop subsequent candidate testing if cumulative OOS net across evaluated candidates ≤−$21. Below that size both mechanisms report insufficient OOS history and cannot decide. `STOP_ALL` precedes `KILL_VARIANT`.

Red flags reject even a numerically passing arm: retained win rate >90%; OOS edge decay >70%; edge confined to one chain/time regime; failure at 3× costs; isolated threshold peak rather than a stable neighboring plateau. Because these checks require adequate independent data and documented review, the harness cannot automatically recommend shipping. **Ship rule:** every gate passes at verdict-capable sample size with adequate field coverage and plateau/regime review; otherwise `ship_recommend=false` and nothing is wired into the bot. No profitability claim follows from a pass on paper quotes.
