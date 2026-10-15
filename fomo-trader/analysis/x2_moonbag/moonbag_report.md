# X2 — Moonbag exit policy paper test

Decision-support analysis for the user's open moonbag question. **Not a ship
candidate.** No bot code was touched; this is replay arithmetic on the 74
closed journal trades.

## The question

> Sell half at +100% to recover the initial, let the rest ride with a wide
> trailing stop and no take-profit ceiling. A 20x runner pays ~10x on the
> remaining half, but most +100% coins fade back to zero.

## Method

`analysis/x2_moonbag/moonbag.py` replays each closed trade under the pure
moonbag policy (50% @ +100%, remainder trails T% ∈ {30, 40, 50} from the
recorded lifetime peak, no TP ceiling, no dump detector, no stale exit).
Only (entry, peak, exit) are observed per trade; every fill assumption is
listed in `moonbag.py`'s `ASSUMPTIONS` (A1–A8) and the sensitive ones have
dedicated sensitivity columns:

- **Gap fills** (exit/peak < 0.4, the journal's "rug gap" definition): the true
  gap fill is unobservable → conservative fill at the *observed* exit.
- **Censored** (recorded exit ≥ trail level → trail never fired → position
  still open at close): primary marks to market at the recorded exit;
  sensitivity drops censored trades entirely.
- **Dump guard**: sensitivity keeps the actual dump exit for the riding half
  (pure moonbag has no dump detector).

Costs mirror `replay.py`: baseline = journal realized − `estimated_cost`;
moonbag = gross proceeds − `estimated_cost` − one extra fixed leg (two sells).
Same 3×-slippage stress layer both sides. Chronological split: IS = first 49
closes, OOS = last 25.

## Results

| Split | Policy | Net USD | PF | WR | Max DD | 3× stress |
|---|---|---|---|---|---|---|
| IS (49) | ladder (actual) | -69.60 | 0.354 | 36.7% | $75.63 | -71.92 |
| IS (49) | moonbag T=30 | -61.93 | 0.441 | 32.7% | $72.74 | -64.31 |
| IS (49) | moonbag T=40 | -68.81 | 0.398 | 30.6% | $79.13 | -71.16 |
| IS (49) | moonbag T=50 | -69.06 | 0.397 | 30.6% | $79.39 | -71.42 |
| OOS (25) | ladder (actual) | -53.92 | 0.087 | 4.0% | $53.92 | -54.79 |
| OOS (25) | moonbag T=30 | -66.14 | 0.118 | 20.0% | $66.14 | -67.01 |
| OOS (25) | moonbag T=40 | -67.67 | 0.109 | 20.0% | $67.67 | -68.53 |
| OOS (25) | moonbag T=50 | -67.67 | 0.109 | 20.0% | $67.67 | -68.53 |
| ALL (74) | ladder (actual) | -123.52 | 0.260 | 25.7% | $123.52 | -126.71 |
| ALL (74) | moonbag T=30 | -128.07 | 0.311 | 28.4% | $128.56 | -131.32 |
| ALL (74) | moonbag T=40 | -136.47 | 0.283 | 27.0% | $136.47 | -139.70 |
| ALL (74) | moonbag T=50 | -136.73 | 0.282 | 27.0% | $136.73 | -139.95 |

Sensitivities (ALL): drop-censored makes moonbag *worse* at every width
(T=30: −134.49 on 36 scored trades; the plain version was flattered by
mark-to-market free-riding on ladder exits). Dump-guard-retained is nearly
identical to pure (−129.59 at T=30) — the gap rule already captures it.

Runner drought (moonbag T=30, ALL): dropping the top 1/2/3 trades moves
−128.07 → −136.85 → −144.49 → −151.65. No runner dependence — the best
moonbag trade in the whole sample is worth **+$8.78**. There is no Kamat-style
concentration because there are no runners to concentrate on.

## Why it loses: the structural facts

1. **Only 8 of 74 trades ever reach +100%** (peak ≥ 2× entry). The +100%
   sale — the entire first half of the strategy — fires on ~11% of trades.
2. **Max observed peak multiple is 2.21×.** The sample contains zero 10×+
   runners. A hypothetical 20x runner at T=50 nets ≈ **+$34**; the T=50
   deficit (−$136.73) needs **~5 such runners** to break even — roughly one
   20x runner per 15 trades, vs the observed zero per 74.
3. **19 of 74 closes are rug gaps.** Without a dump detector, the riding half
   is wiped at the observed gap exit — pure moonbag forfeits the one guard
   that actually fires on the worst trades.
4. **55 of 74 positions would still be open** under T=50 (trail never fired).
   Moonbag converts a day-trading journal into an open-ended bag-holding
   book — unscored risk the table above doesn't even charge for.

## The one place moonbag "wins" — and why it doesn't count

IS at T=30 beats the ladder (−61.93 vs −69.60, +$0.157/trade). OOS it loses
badly (−66.14 vs −53.92, −$0.489/trade). Negative retention of the IS edge:
**the textbook shape of noise, not signal.** The 60%-retention gate fails.

## Recommendation

**Do not switch.** On 74 closed trades the moonbag policy underperforms the
current ladder in-sample-adjusted terms, out-of-sample, under 3× stress, and
under every sensitivity. Its edge case (letting runners run) has no material:
8 lifetime +100% touches, zero 10× runners, and 19 gap-wipes that the pure
policy has no answer for. Revisit only if the journal ever shows a sustained
population of 5×+ runners — i.e., if the market regime, not the exit policy,
changes.
