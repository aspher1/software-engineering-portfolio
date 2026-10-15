# X2 look-ahead audit

Every input feature used by the moonbag counterfactual, with its timestamp
relative to the decision point. Decision-support analyses are retrospective
by construction; the question this audit answers is whether any input
postdates the point it is used at, which would silently inflate the
counterfactual.

## Decision points in the model

The moonbag policy has two decision points per trade:
1. **The +100% sale** — decided at the moment price touches 2× entry.
2. **The trailing-stop exit** — decided at the moment price falls T% from peak.

## Feature audit

| Feature | Source | Timestamp | Used at | Verdict |
|---|---|---|---|---|
| `entry` | entry record `entry` | entry commit time | both decisions | prior ✓ |
| `peak` | close record `peak` (lifetime max while open) | accrues during the trade, final at close | +100% sale: the *touch* of 2× necessarily precedes or equals the peak observation — using peak ≥ 2× as the trigger condition is equivalent to "a 2× touch occurred while open". No future information: the sale is modeled at 2×, never above. | prior ✓ |
| `peak` (trail) | same | same | trail exit: the trail is computed *from* the ultimate peak, which is only known at close. **This is retrospective.** | ⚠ acknowledged |
| `exit` | close record `exit` | close time | gap-fill branch (A4) and censored mark-to-market (A5) | at/after close — see below |
| `reason` | close record `reason` | close time | dump-guard sensitivity only | sensitivity-only ✓ |
| `buy_sol`/`buy_bnb` | entry record | entry commit time | sizing | prior ✓ |
| `sol_usd` | entry/commit record | entry commit time | USD conversion | prior ✓ |

## The peak-lookahead question, answered honestly

A live trailing stop trails the *running* peak and fires T% below it
intratrade; our model trails the *final recorded* peak. These differ when the
true running peak at the firing moment was lower than the ultimate peak —
i.e., when price made a higher high *after* the trail would have fired. In
that case the model credits the wider trail with a later, higher anchor than
a live implementation could have had... no wait, reversed: if the ultimate
peak comes *after* the trail fired, the live trail would have fired from the
earlier lower peak (worse fill), while our model fires from the ultimate peak
(better fill). **Direction: optimistic for moonbag.** This is the same
optimism the replay module's own `counterfactual_exit` docstring admits
("optimistic fills cannot establish executable performance").

Why the verdict still stands: the optimism applies to the *trail-fired*
subset (19–38 trades depending on width); the rejection rests additionally on
(a) the censored mass (unaffected — no fill assumed), (b) the gap subset
(filled at observed exit — conservative), (c) the 8-touch structural fact
(peak ≥ 2× is a pure observation, no fill assumed), and (d) the OOS reversal.
Removing the optimistic subset would widen, not narrow, the gap to the ladder.

## Gap-fill branch (A4)

Uses the observed exit as the fill. The observed exit is the ladder policy's
*actual* fill — a live moonbag riding half would likely fill *worse* (no dump
detector, wider trail → later reaction). Direction: optimistic for moonbag.
Strengthens rejection.

## Censored mark-to-market (A5)

Credits the recorded exit to a position the policy would still hold. The
drop-censored sensitivity (−137.12 vs −136.73 at T=50, ALL) shows the
accounting choice doesn't move the verdict.

## Conclusion

Two retrospective inputs (ultimate-peak trail anchor, observed-exit fills)
both err optimistic *for moonbag*, and the policy still loses to the ladder
on every cut. No feature manufactures the rejection; if anything, a live
implementation would do worse than these numbers.
