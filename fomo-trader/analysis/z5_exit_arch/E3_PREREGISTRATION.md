# E3 exit-architecture pre-registration

Frozen 2026-09-27, before any E3 evaluation run and before any Z4 quote path exists. **Verdict today: awaiting data.** This is an offline, mark-based paper experiment. It changes no bot code, config, or position. The harness is `e3.py`; its defaults are the parameters below, and any divergence between this document and a run log is a protocol violation, not a tuning choice.

## Why E3

Y3 established that 74% of closed trades peaked at least +10% after entry, the median peak was +26%, the median exit/entry ratio was 0.893 and the median give-back was 29.1% of peak. Only about 11% of trades reach the first +100% take-profit rung. The ladder does not monetize the moves that actually happen. The 467-configuration exit sweep (rejected) only searched the existing design space; W3 time exits were rejected because the journal had no intratrade path. Z4 quote paths close that gap. E3 tests three architectures that are genuinely different from the ladder.

## 1. Architectures

### Common safety floor (control and all variants)

1. **Hard stop:** exit 100% of the remainder when mark ≤ entry × 0.60 (−40% from entry). Reason `hard_stop`.
2. **Dump detector:** exit 100% of the remainder when the mark is ≥12% below the maximum mark in the trailing 60 seconds (current tick included). Reason `dump`.

Per-tick order everywhere: update peaks/anchors; architecture partials; then full exits in the order dump → architecture full exit → hard stop. Only one full exit per tick. The live venue m5 tripwire needs DexScreener data and is not modeled; it is identical across arms, so its omission cannot favor one architecture.

### Control: live ladder

As `config.json` `exit` and `fomo_trader.py` defaults: `take_profits [[100,50],[200,25]]`, `trailing_stop_pct 30`, `hard_stop_pct 40`, `slippage_bps 500`, `sell_poll_sec 2`, dump 12%/60 s.

- TP fractions are of the **remaining** balance (`sell_pct_of_balance`): 50% of the original at +100% (`tp+100`), then 25% of the remainder, which is 12.5% of the original, at +200% (`tp+200`).
- Trailing stop 30% below peak; peak starts at the entry price (`trailing_stop`). After any rung, the trail tightens to 12% from the next tick onward (`trail_after_tp_pct` code default; absent from config).
- Stale exit at age ≥45 minutes with gain <10% (`stale`; code defaults, absent from config).
- Because the peak starts at entry, the 30% trail always fires before the 40% hard stop, exactly as live.

### (a) `ratchet`: peak-anchored trailing take-profit

- **Arm** when mark ≥ entry × 1.20 (X = +20%). Anchor = the arming mark, raised by every later higher mark.
- **Partials:** when mark ≤ anchor × 0.90 (Y = 10% below the running anchor), sell 50% of the remainder (`ratchet_partial_1`); then re-anchor at that mark. Repeat once (`ratchet_partial_2`). Fractions of the original: 50%, 25%.
- **Deep trail:** after both partials, the final 25% exits when mark ≤ anchor × 0.75 (`ratchet_deep_trail`).
- Before arming, only the safety floor applies.

### (b) `time_box`: time-boxed momentum exit

- Exit 100% at the first mark with age ≥ T = 20 minutes after entry (`time_box`).
- Earlier, exit 100% when mark ≤ 0.80 × mark-peak (Z = 20%; `mark_trail`). The mark-peak is the running maximum of observed marks and does not include the entry price.

### (c) `vol_scaled`: volatility-scaled partials

- **Metric:** σ = sqrt(Σ r² / Σ Δt × 60), where r = ln(mark_i / mark_{i−1}) over consecutive marks with Δt > 0 and entry_ts ≤ t < entry_ts + 300 s. This gives realized volatility per √minute and handles irregular ticks.
- **Classification** happens at the first mark with age ≥300 s, using only window marks. Regimes: `high` if σ ≥ 0.05; `low` if σ < 0.05; `low_default` if there are fewer than 10 returns. `low_default` uses the low schedule. During the window, only the safety floor applies.
- **High regime** (closer, smaller rungs; fractions of the original): 20% at +10%, 20% at +20%, and 20% at +35% (`vol_high_rung_1..3`). The remaining 40% uses a 20% trail below the mark-peak (`vol_high_trail`).
- **Low regime** (wider rungs): 30% at +30% and 30% at +60% (`vol_low_rung_1..2`). The remaining 40% uses a 30% trail below the mark-peak (`vol_low_trail`).
- Rungs are tested against the current mark only, never a past peak.
- The σ cutoff is frozen and was not fit on data. If either regime holds <20% of IS positions, the run reports `degenerate_regime_split`. The rules do not change.

### Censoring

A path ends when the real bot closed the position, or at the Z4 caps of 5,000 rows or 1 MiB. A variant still holding after the last mark is marked out at that mark (`path_end`) and flagged censored. A candidate whose IS censored share exceeds 50% is ineligible for selection. Every run reports censored shares.

## 2. Data, qualification and split

- **Join (real run; not implemented now):** join `runs/paper-1h/trades.jsonl` entry rows to `<runs>/quote_paths/<sanitized mint>.jsonl` by `mint`. `entry_ts` is the journal entry `ts`. `entry_price_usd` is the journal's recorded paper entry fill (`entry`, native units) times the entry row's `sol_usd` (the contemporaneous native/USD rate; on BSC rows this field holds the BNB/USD rate — verified 2026-09-27 against the live journal, e.g. 776.78 on a BSC entry). Entry rows predating the round-3 enrichment (22/82 as of 2026-09-27) lack `sol_usd`; those positions are disqualified, fail-closed. Close rows carry no rate field, so there is no substitute rate. Only marks with ts ≥ entry_ts are used.
- **Qualifying position:** closed; valid entry price and timestamp; ≥20 non-null marks at or after entry (`min_marks = 20`). The loader drops malformed lines, null, missing, or invalid `price_usd`, wrong chain or mint, and out-of-order timestamps, counting each case. It never raises.
- **Split:** chronological by entry ts. The first ⌊2n/3⌋ qualifying positions are IS; the rest are OOS. Ties are ordered by a SHA-256 hash of seed 20260927 and position identity, independent of input order. No split boundary may feed later trades into earlier decisions.
- Variant selection is frozen on IS. There is exactly one OOS evaluation pass. Within a path, the decision at tick t uses only marks ≤ t; this is enforced by a prefix-equivalence test.

## 3. Costs (mirror E1)

Research notional is **$10 per position** for every arm. Venue fee is **30 bps/side**; entry slippage is **100 bps**; exit slippage is **500 bps** (`exit.slippage_bps`). Priority fee is **2,000,000 lamports per transaction**, converted with a supplied SOL/USD rate. The entry and every exit sell each count as one transaction, so partial-heavy architectures pay for each partial. The same USD-converted fee is applied on BSC as a gas proxy. Net = gross exit proceeds − $10 − entry venue fee and slippage on $10 − exit venue fee and slippage on gross proceeds − priority fees. Seed is **20260927**.

## 4. Selection rule (IS)

The frozen variant is the candidate with the highest IS after-cost net-per-close among those with ≥20 IS closes, IS censored share ≤50%, IS net-per-close > $0, and IS net-per-close strictly greater than the control's on the same IS positions. Exact ties break by the seeded hash. **If no candidate qualifies, ship nothing and record `NO_VARIANT_BEATS_CONTROL`.**

## 5. Kill criteria (dollars, mirror E1)

- **KILL_VARIANT** if a candidate's OOS net ≤ $0 after costs at ≥30 OOS closes. The rule applies at the 30th close and at every later close.
- **STOP_ALL** if cumulative OOS net across the three candidates (control excluded) ≤ −$21.
- The replay is chronological. Closes settle before each later entry. KILL and STOP_ALL block later entries; the triggering close and already-open positions still count. Below 30 OOS closes the decision is `INSUFFICIENT_OOS_HISTORY`; there is no kill verdict either way.

E3 is **runnable** at the data trigger. It is **decision-capable** only after the frozen variant has ≥30 OOS closes. With 30 qualifying positions, the first run has 20 IS and 10 OOS positions: **runnable but not decision-capable**. That run's verdict is `RUNNABLE_NOT_DECISION_CAPABLE`, and it ships nothing. The run repeats as paths accumulate; the IS/OOS rules and parameters stay frozen.

## 6. Gates for the frozen variant (all required)

1. ≥30 OOS closes, and not killed.
2. **60% retention:** OOS net-per-close ≥ 0.60 × IS net-per-close, both after costs.
3. **3× cost stress:** re-run on the same accepted OOS positions with venue fee, both slippages, and priority fee tripled. The stressed OOS net must be ≥ $0. The stressed number is reported and does not replace the primary specification.

Verdict precedence: `awaiting data` → `STOP_ALL` → `NO_VARIANT_BEATS_CONTROL` → `KILL_VARIANT` → `RUNNABLE_NOT_DECISION_CAPABLE` → `FAIL_RETENTION` → `FAIL_3X_COST_STRESS` → `PASS_MARK_BOUND`.

## 7. Data trigger

E3 executes when **at least 30 closed journal positions each have a Z4 quote path with ≥20 non-null marks at or after entry and a valid USD entry anchor.** Below that, `run_e3` returns `awaiting data`. Z4 capture is default-off, so no qualifying path exists today.

## 8. Honesty clause

**Marks are not fills.** E3 measures mark-implied edge **bounds**. A positive result, `PASS_MARK_BOUND` included, is not evidence of profitability or live-readiness. Before any variant may be wired into the bot, it needs a fill-feasibility study: size-aware sell quotes at each simulated exit, measured decision-to-fill latency, and failed-sell and partial-fill accounting. Sell failures and vanishing depth can dominate the gap (for example, the −92.6% rug gap). The rug guard and risk caps are out of scope and never loosened by E3.
