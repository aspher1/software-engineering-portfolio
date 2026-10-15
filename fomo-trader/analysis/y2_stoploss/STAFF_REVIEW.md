# Y2 staff review — hard stop-loss recalibration

Verdict under review: **REJECT** (ship_recommend = false).

## The three strongest objections to this work

### 1. "Six trades is not a finding; the OOS half of your own split says the effect is zero."

**Answer: conceded — this is the reason for rejection.** The +$7.40 lives in
6 IS slow-bleed trades. OOS contains zero slow bleeds (23/25 dump exits), so
the retention gate reads 0%. A fairer split (e.g., stratified by exit regime)
might show the bleed-regime effect replicating, but inventing a favorable
split after seeing the data is exactly the p-hacking the chronological gate
exists to prevent. Counted against shipping.

### 2. "Fill at exactly the stop price is optimistic — the bot polls every 2–5s and paper fills slip."

**Answer: partially conceded, quantified.** The base case fills at exactly S.
The fill-shock sensitivity shows the edge degrades gracefully (+$7.40 →
+$2.84 at 10% adverse), and these were slow bleeds (trail, not dump, exits),
where 2–5s of drift is small relative to the 5–12% saved per trade. But live
trading would add real slippage the paper journal never shows, and the edge
is thin enough that this matters. Counted against shipping at any level
tighter than -20% without live-paper confirmation.

### 3. "A2 (dump exits unchanged) assumes the dump detector always beats the stop. In a slow-rolling dump the -15% stop could fire first and save more."

**Answer: genuine limitation, bounded.** 8 non-gap dump exits sit below the
-15% stop level — the interaction zone. Sensitivity B (stop replaces those
exits) is the optimistic bound: -$112.27 vs -$116.12 base. It cannot be
resolved from the journal (no intratrade path), and Track A's round-4 work
already rejected exactly this class of unobservable-fill counterfactual.
Taking the optimistic bound as the base case would repeat that error.
Counted against shipping; flagged as the precise question for the X1
rug-gap forensics track, which owns dump-exit microstructure.

## What the reviewer (me) would need to flip to ship

1. A second sample: ≥10 stop-firing trades in a fresh chronological OOS with
   ≥60% edge retention — i.e., the bleed regime reappearing and the effect
   replicating.
2. A plateau, not a monotonic curve: adjacent levels (-12.5%/-15%/-17.5%)
   showing a flat optimum rather than "tighter is always better."
3. Live-paper confirmation that stop fills land within ~2% of the trigger on
   slow bleeds (the fill-shock assumption made empirical).

## Residual risks if anyone ships this anyway

- Regime risk: in a 100%-rug regime the stop is dead code that adds
  complexity and a false sense of safety.
- The 4 discretionary winners' intratrade dips are unobservable; the "zero
  winners killed" claim is conditional on the trailing-exit minimum
  assumption (A1), which is solid for trail exits but says nothing about
  TP/manual exits.
