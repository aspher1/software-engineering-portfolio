# Y2 look-ahead audit

Every feature used to decide "would the stop have fired" is timestamped at
or before the entry/commit decision point, or is the trade's own recorded
outcome used only to *classify* the exit — never to peek at prices after a
hypothetical stop decision.

## Feature inventory

| Feature | Source | Timestamp vs decision |
|---|---|---|
| `entry` | journal entry record | the decision point itself (commit fill) |
| `peak` | journal close record (lifetime peak) | recorded during the trade; used ONLY for the gap classifier (`exit/peak < 0.4`) and never as a tradable price |
| `exit` | journal close record | the actual exit price; used to classify the exit reason and as the no-stop counterfactual (what happened) |
| `reason` | journal close record | exit classification (trail/dump/stale/TP/manual) |
| `rungs` | journal close record (`rungs_fired`) | TP legs are held identical between baseline and counterfactual, so rung data cannot leak edge into the comparison |
| `buy_sol` / `buy_bnb` | journal entry record | pre-decision |
| `sol_usd` | journal entry record | pre-decision rate |

## Why the firing logic is not retrospective cheating

The counterfactual asks: "had a live -15% stop been armed, would it have
fired?" For a trailing-stop exit, the recorded exit X ≈ the trade's minimum
(the position was sold when the trail fired). The inference "S > X ⟹ price
crossed S" uses only the *ordering* implied by the exit mechanism, not any
price observed after the hypothetical stop would have fired. The stop fill
is set to S (the trigger), never to a later observed price.

The one genuinely retrospective input is the exit *classification* (we know
it was a trail exit, not a dump). A live stop doesn't know the future exit
type — but the classification only selects which firing rule applies; in
live trading the stop and the trail/dump detector race on the same price
feed, and the model reproduces that race's outcome under the stated
assumptions (A1–A4). The dump-flip sensitivity B exists precisely to bound
the assumption most sensitive to this (A2).

## Known biases (direction noted)

- Fill at exactly S is **optimistic for the stop** (poll delay, paper
  slippage). Bounded by the fill-shock sensitivity (edge survives 10%).
- Gap classifier uses the lifetime peak, which is only known at close;
  a live implementation would use the rolling peak. Direction: the rule is
  *conservative* for the stop (more trades classified as gaps → fewer
  stop fires), so this bias strengthens the rejection.
- Stale-exit and discretionary categories never fire the stop: conservative
  for the stop's measured edge, honest about unobservability.
