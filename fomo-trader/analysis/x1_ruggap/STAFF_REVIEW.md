# X1 staff review — rug-gap forensics

## What was built
`analysis/x1_ruggap/ruggap.py`: pure-function forensics (gap ID, trigger
ratios, lag-vs-gap loss anatomy, cooldown counterfactual, gain-veto split).
15 hermetic tests. No hot-path code; the only live component referenced is
the already-shipped 24h dump-cooldown guard.

## Verdict
Nothing new ships. The 24h cooldown (the one shippable item) is already live.
Pre-entry gain veto rejected on 0% OOS retention. Exit-side changes rejected
as structurally moot.

## The three strongest objections to this conclusion

### 1. "The $141.55 gap loss assumes the trigger price was real — maybe faster
polling WOULD have caught a fillable price between peak and zero."
Partly conceded, and it cuts both ways. The decomposition's $49.72 "lag"
portion already grants the optimistic assumption (fill at trigger price).
But the 柴犬联盟 log sequence shows entry-to-zero in 48 seconds with the
detector firing at 48s — the 5s manage loop was already polling; there was
no intermediate fillable quote, the pool was drained. For the Sep-24-morning
Solana trailing-stop gaps, exits printed at -77% to -99.7% from peak against
a -30% trailing stop: the price never "passed through" -30% on a poll. Could
a 1s poll have caught something? Possibly a few cents on a few trades — but
the venue-dump cases (m5 candle -100%) prove whole 5-minute windows contained
no exit. This objection argues for tens of dollars at best, not the $141.55.
Counted against shipping any exit-side change: even the optimistic bound
doesn't clear the bar.

### 2. "n=19 gaps, 17 on one day — you're declaring exit-side work dead on a
single regime's evidence."
Conceded as a limitation, but the direction is robust. The claim is not
"exits don't matter" in general — non-gap trades (56, PF 0.825) are where
exit optimization lives, and Tracks B/X2/Y2 work that side. The claim is
narrow: FOR gap-type events (exit < 40% of peak), the loss occurs after any
trigger. That mechanism (liquidity pull → no quotes) doesn't depend on the
day; it depends on the event type. If a future regime produces slow-bleed
gaps instead of cliff gaps, this conclusion must be revisited — the
`anatomy()` function makes that re-check one command.

### 3. "If pre-entry is the only fix and W2/W4 need data we don't have, this
track is just an expensive way of saying 'we can't fix it yet.'"
Partially conceded — and that's exactly why the verdict is honest. The
value of this track is negative knowledge with teeth: it kills an entire
avenue (exit-side gap mitigation) that the team would otherwise keep
spending cycles on, it quantifies the bound ($0 realizable), it ships the
one thing that was shippable (already live), and it redirects the remaining
effort to pre-entry (W2/W4 calibration as enriched trades accumulate) and
regime gating (Y1). "Stop digging here" is a result.

## Residual risks admitted
- The 0.4 exit/peak gap definition is a judgment call; moving it to 0.3 or
  0.5 changes the gap count by ±3 but not the 74%-gap-loss headline.
- Journal `peak` is the recorded lifetime peak; if peaks are under-recorded,
  the lag portion is overstated and the gap portion understated — which only
  strengthens the conclusion.
- The cooldown counterfactual is conservative (any-prior-dump vetoes, not a
  true 24h window); the true-24h saving is ≤ $7.05, not more.
