# Z3 staff review — three strongest objections to "KILLED"

Rule: an unanswered objection counts against the verdict. Each answer below
is marked **answered**, **partly answered**, or **open**.

## Objection 1: the kill rests on a rent number you recalled but didn't verify

The whole $7 result depends on A12, a fixed 8,120-byte (70-bin) position
account holding a 0.0574 SOL refundable deposit. If Meteora now sizes
positions by bin count, a 1-bin position might need only about 0.0036 SOL.
The model's own hypothetical row then puts the $7 ceiling at **+$1.12 over
30 days**, and by the pre-registered rule the ceiling would clear $0.

**Partly answered.**

- The research grounding supports the fixed-rent regime. The report cites
  practitioner rent budgets and states that rent makes sub-~$100 positions
  impractical and that $7 tickets cannot LP meaningfully. If small
  positions were cheap, practitioners would not budget that way.
- The threshold is disclosed rather than hidden. At SOL $115 the $7 ceiling
  stays ≤ $0 only if the position deposit is at least about 0.0558 SOL. The
  recalled value is only about 3% above that.
- Even in the flip case, the +$1.12 needs 0.7%/day for 30 days with zero
  IL. At the bottom of the cited range it is about +$0.14. The measured
  honest ledger ran at −0.05%/day.
- **Still open:** the current Meteora position rent has not been checked
  live. That is a zero-cost docs check. Until it is done, the kill is
  "KILLED, conditional on A12", and section 4 of the report says so. If
  the check shows small positions exist, the pre-registered follow-up
  (report section 5) applies.

## Objection 2: 0.7%/day is not a ceiling for memecoin launch pools

The cited 0.1–0.7% is a SOL-USDC number. Hot memecoin launch pools with
dynamic fees can show daily fee/TVL far above 0.7% for hours. A true upper
bound should use those.

**Answered for the $7 verdict.** The $7 result is infeasibility: the
deposits exceed the budget, so fee yield never enters. The 5%/pool-day
stress row (about 7× the cited top, sustained for 30 days) still gives
$0.00. For the larger tickets, the objection is correct that 0.7% is not a
hard ceiling in launch pools. But those high fee rates exist *because* of
the flow the ceiling assumes away: dumps through one-sided bins, which
MemeTrans puts at about 73% of tokens losing more than 60% within 20
minutes. High launch-pool fee/TVL paired with zero IL is not a coherent
state. It is two favorable assumptions that exclude each other.

## Objection 3: counting refundable rent against the ticket is a choice, not a cost

Rent comes back when the position closes, so it is not a loss. Treat it as
a separate deposit and $7 of liquidity earns fees.

**Answered.** The budget constraint is pre-registered, not chosen here: Z1
§3B says "each episode has a $7 total capital budget". The deposits are
indeed not counted as a loss. They count only as capital that must be
posted, and `net_upper` includes only non-refundable costs. Dropping the
constraint means testing a ~$14 position (a $7 ticket plus $7.17 of
deposits). That changes position sizing, which this track may not do. The
grid already answers the larger-capital question: the $25 row clears $0
under the absurd assumptions. A different program could run the report
section 5 test at that size if it were authorized.

## Net assessment

Objections 2 and 3 are answered. Objection 1 is partly answered: its
factual core (current Meteora position rent) is unverified, so the verdict
stands as **KILLED, conditional on A12**. The conditional form appears in
the report's section 4 and in the CHANGELOG.
