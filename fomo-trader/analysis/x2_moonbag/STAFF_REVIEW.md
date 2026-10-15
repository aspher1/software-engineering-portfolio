# X2 staff review — moonbag paper test

Written as if submitting to a staff engineer. Verdict first: **do not
switch to moonbag** (see `moonbag_report.md`). These are the three strongest
objections to that verdict — the cases *for* moonbag — and why they fail.

## Objection 1: "The sample has no runners because the ladder policy never lets them run."

The strongest pro-moonbag argument: this is survivorship bias in reverse.
The journal was generated *under the ladder policy* — TP rungs at +50% and a
25% trailing stop (12% after TP) cap every trade's upside by construction.
Maybe the market serves 10× runners routinely and the ladder murders them at
+50%, so replaying the ladder's journal can never reveal moonbag's edge.

**Answer:** The objection is valid in principle but defeated by the `peak`
field. Peak is the *lifetime* max price observed while the position was open —
recorded independently of where the ladder chose to sell. If 10× runners were
being capped, we would see peaks at 5×/10×/20× with exits far below. The max
observed peak multiple across all 74 trades is **2.21×**, and only 8 trades
ever touch 2×. The ladder didn't cap runners; there were no runners. The
bottleneck is the entry universe / market regime, not the exit policy.
*Counts against moonbag, decisively.*

## Objection 2: "Mark-to-market flatters moonbag — but dropping censored trades is unfair in the other direction."

55 of 74 positions never trigger the T=50 trail, so they're still open at
close. The primary analysis credits them at the recorded exit (free-riding
ladder exits); the sensitivity drops them (throwing away 74% of the sample,
including the 8 +100% touches that are moonbag's whole point).

**Answer:** Both framings are shown precisely so neither is hidden. The
honest reading: *neither* framing rescues moonbag. Plain: −136.73.
Drop-censored: −137.12. The censored mass is not hiding a profit — the
uncensored remainder (trades where the wide trail actually fired) loses money
on its own. And the deeper point stands regardless of accounting: a policy
that leaves 74% of day-trade positions open indefinitely is not an exit
policy, it's a different strategy (bag-holding) with unmeasured overnight /
multi-day risk the journal cannot price. *Counts against moonbag.*

## Objection 3: "The +100% sale at least locks in profit on the 8 trades that get there — that's real risk reduction even if the total is negative."

True as far as it goes: on the 8 +100% touches, moonbag banks 0.5× stake at
2× (recovering the initial, per the user's description) while the ladder's
+50% rung banks less. The second half then rides.

**Answer:** Run the numbers on those 8. Of the 8, the riding halves face the
same trail math as everything else — and the sample's two best multiples
(2.21×, 2.09×) still leave the riding half exposed to the gap risk that
defines this journal (19 rug gaps). The "recover the initial" framing is also
doing quiet work: recovering the initial on 8 trades while the other 66 bleed
is not risk reduction, it's consolation. The portfolio-level question is the
only one that matters, and at portfolio level moonbag trails the ladder by
$5–$13 across every width, split, and sensitivity. *Counts against moonbag.*

## Residual risks in this analysis (admitted)

- **A3 ordering risk:** the trail-fill-at-level assumption can't see
  intratrade path; a dump-then-recover path would wipe the riding half before
  the trail "fires" in our model. Direction: our numbers are *optimistic* for
  moonbag, which strengthens the rejection.
- **Regime risk:** 74 trades over ~3 days. If the market starts printing 5×+
  runners, every number above must be recomputed. The report states the
  revisit condition explicitly (sustained 5×+ runner population).
- **Cost model risk:** replay.py's variable-cost model is an estimate, not a
  measurement; but it is applied identically to both policies, so it cannot
  manufacture a *ranking* reversal unless moonbag's true costs are
  systematically lower — unlikely, since moonbag does more sells per trade.
