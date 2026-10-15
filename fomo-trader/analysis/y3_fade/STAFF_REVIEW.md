# Y3 staff review — fade-the-signal diagnostic

Verdict under review: **do not ship** (`ship_recommend: false`).
Reviewer stance: adversarial. Three strongest objections, answered honestly.

## Objection 1: "Peak is a lifetime max — of course it looks like tokens run.
You're measuring the best moment, not the typical path."

Partly conceded. `peak` is the maximum favorable excursion, so "median +26%
run" overstates what a holder experiences moment-to-moment. But the
diagnostic question was directional — *do tokens systematically die at
entry?* — and a lifetime max is the right instrument for that: if entries
bought the top, peaks would cluster at ~1.00×. They don't (p25 = 1.09×,
74% ≥ 1.10×). The give-back metric (median 29%) then correctly attributes
the loss to the post-peak fade rather than the entry. Counted against
shipping? N/A — nothing ships. Counted against overclaiming: yes, and the
report states the limitation.

## Objection 2: "The pullback bounds are a strawman. A real pullback trader
uses live price action, not your provable-fill trick."

Conceded in full — and that's exactly the point. A real pullback entry
needs the intraday price path, which we do not record. The bounds analysis
doesn't claim to evaluate the platonic pullback strategy; it evaluates
*what our evidence can support*, and the answer is: only the adversely
selected part. The honest conclusion is not "pullbacks don't work" but
"pullbacks are untestable on our data AND the testable shadow of the idea
is pure adverse selection." Both halves are in the report. If anyone wants
to revive this, the prerequisite is a new captured field (post-signal price
path), not a new threshold.

## Objection 3: "The fade mirror (+10.7% median) suggests fading IS the edge,
and you dismissed it as 'untradeable' too fast."

Not conceded. Fading requires shorting fresh memecoins. There is no borrow
market for 15-minute-old Solana/BSC tokens; perps don't list them. The only
"tradeable fade" is *not entering* — which is a filter, and every filter
we've tested has died OOS. The mirror is reported as a directional bias
measurement because that's all it is. Presenting it as a strategy would be
exactly the kind of unvalidated edge-claim this program exists to kill.

## Residual risks admitted

- Cost convention drops the fixed lamport/gas legs (dust at $7 stakes) and
  reuses the journal's own `sol_usd` rate convention (BNB/USD for BSC).
  Absolute dollar levels may differ ~10–20% from `replay.py`'s convention;
  the *relative* verdict (PF 0.000, WR 0.00 on provable fills) is
  convention-independent.
- `peak`/`exit`/`entry` are paper quotes, not fills; the pullback lower
  bound additionally assumes the pullback entry gets the *same* exit price
  as the actual (earlier, higher) entry — optimistic for the pullback, which
  strengthens the rejection.
- 74 trades is small; the diagnostic describes our history, not the market.
