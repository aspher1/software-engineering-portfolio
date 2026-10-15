# Y1 staff review — regime gate

## What was built
Per-entry binary WHEN-to-trade gate over 6 pre-registered gate families
(trailing-P&L veto, thin-window veto, cold-restart, AM/PM halves, drawdown veto),
evaluated on a frozen 75-close snapshot (IS: Sep 24 n=60; OOS: Sep 27 n=15).

## Verdict: do not ship (all gates)

## The three strongest objections to shipping — and answers
1. **"R3<10 shows 383% OOS retention — ship it."**
   No. The retention ratio divides by a $0.52/trade IS edge; the OOS kept set is
   5 trades that still lose -$10.97 at 0% win rate. A gate that vetoes a +$5.12
   winner while keeping five losers has no directional intelligence — it profits
   (less negative) purely by trading less. Shipping "trade less after restarts"
   on n=5 OOS evidence would be curve-fitting with extra steps.
2. **"Just combine R3<10 with the existing filters; partial credit counts."**
   The IS edge is $0.52/trade ($26 over 51 trades) and comes from dodging two
   specific -90%+ exits in the bot's first hour of existence. There is no
   evidence this generalizes beyond "the first hour of Sep 24 was bad." A
   combined rule would inherit the same two-instance sample.
3. **"The degenerate gates (R1<0, R6) 'save' the OOS loss — standing down after
   a -$104 day is just prudent risk management."**
   It is prudent — and it is also indistinguishable from turning the bot off
   forever after any bad day. A regime gate must discriminate *between* regimes;
   with one losing day in-sample, "don't trade after losing days" has exactly
   one training example. That is a kill-switch policy decision for the user,
   not a validated edge, and it should not be smuggled in as one.

## Conceded weaknesses (count against shipping)
- Only 2 active trading days: the entire track is underpowered; the negative
  result is "no evidence," not "evidence of absence."
- OOS is 15 trades: any OOS number here is noise-dominated.
- R4's gap feature measures time-since-last-close, which the cross-day close
  contaminates; the cold-restart concept deserves a cleaner feature when more
  halt events exist.
- Market-trend and signal-rate regimes were not tested (documented as such).

## What would change the verdict
≥10 active trading sessions with dated signal logs; then re-test R3 (warm-up
effect) and R4 (cold restart) first — they are the only families with a
mechanistic story rather than a degenerate one.
