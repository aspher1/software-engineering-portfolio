# Tier 4 combo picker

The picker (`scripts/combo_picker.py`) turns confirmed single-leg edges into
multi-leg combos, the way the Kalshi app's combo builder phrases them: *pick
2+ markets, $10 pays $X*. A pick pays only if every leg wins.

**Every pick is a MODEL ESTIMATE.** It is never a guaranteed profit, and no
win rate is claimed. Each pick carries
`guarantee: "MODEL estimate — NOT guaranteed; fair-value model vs market"`.

The module is pure: it makes no HTTP calls and uses no threads or API
clients. Only `log_pick` touches the disk. A fail-closed guard,
`guard_no_write(url, method)`, refuses every non-`GET`/`HEAD` method and the
Kalshi combo-market creation, RFQ and quote paths, plus Polymarket's
`/v1/maker/*` paths. It exists so that any future HTTP call fails closed.

## 1. Leg pool

Only legs with a defensible, independent fair value enter the pool. There
are two sources:

| `fair_source` | Where it comes from | Fair value of the taken side |
| --- | --- | --- |
| `kraken_model` | confirmed Tier 3 model-edge legs (15-minute crypto windows) | the model's shrunk, agreement-filtered fair |
| `xvenue_poly` | the Kalshi leg of a confirmed Type C cross-venue pair (tiers "edge" and "locked") | `100 − Polymarket opposite-side ask` |

The `xvenue_poly` fair is a true lower bound in an uncrossed Poly book; a
locked pair (combined asks under 100c after fees) mathematically implies
the opposite-side book is crossed or the listing is stale, so for locked
legs the bound is best-effort rather than a clean bound — the same caveat
the shipped Tier 2 edge-pair legs already carry, and the reason the
conservative 5c per-leg bar still binds.

Everything else has no fair value and never enters the pool: Tier 1 locked
Type A pairs (their 100-no_ask 'fair' is the YES bid, a crossed-book
artifact, not a win probability), Type A near-arbs, Type B books (no per-leg
fair derivable from a sum edge), and any leg without a model. See
the no-edge list in [data-sources.md](data-sources.md).

Each leg must individually pass all of these:

- `fair − ask ≥ 5¢` (`LEG_MIN_EDGE_CENTS`)
- depth of at least 1 contract
- at least 5 minutes to expiry (`PICK_MIN_TTL_S`)

A combo can't ride on one good leg plus dead weight. Leg asks are the
orderbook-confirmed (re-verified where available) taker asks.

## 2. Correlation rules and the joint fair

The baseline joint fair is the product of the leg fairs, which assumes
independence:

```
joint_raw = Π (fair_i / 100)
```

**Hard rejects.** The combo is never priced if either holds:

- Two legs share an event ticker. Same-event outcomes are mutually exclusive
  or complementary, so a product of marginals is meaningless for them.
- A ticker appears twice.

**Flagged pairs.** These get a haircut, not a rejection:

- **Same game:** both legs' `game_key`s (`LEAGUE|YYYYMMDD|TEAMA|TEAMB`)
  share the same date and teams. The league code is ignored for this check,
  so market-type variants of one game (GAME, SPREAD, TOTAL, BTTS) are
  flagged in every league, not only the 11 in `SPORTS_PREFIXES`. Outside
  that list, `KXCFLGAME` and `KXCFLSPREAD` carry different league codes.
  Series with digits in the prefix (`...1HSPREAD`, `...2QSPREAD`) don't
  parse to a `game_key` at all.
- **Same league, unknown game:** both legs are sports in the same league and
  at least one game can't be identified. Unknown identity is treated as the
  same game, which is the conservative choice.
- **Opposite crypto takes:** two 15-minute crypto legs taken in opposite
  directions, where negative correlation is plausible.

Each flagged pair costs **4 percentage points** of joint fair
(`JOINT_HAIRCUT_PTS`):

```
joint_fair = max(0, joint_raw − 0.04 × n_flagged_pairs)
```

A combo is rejected outright if it has more than 2 flagged pairs, or if the
haircut joint fair is below 2% (`COMBO_MIN_JOINT_FAIR`). Below 2%, model
error dominates.

Why 4 points: under a Gaussian copula with ρ = −0.25, two 60% legs have a
joint of about 32.3% versus a product of 36%, and two 50% legs have about
21% versus 25%. Both gaps are roughly 4 points. The picker doesn't estimate
the sign of the correlation. Where the true correlation is positive, the
haircut only makes the fair more conservative.

## 3. Parlay pricing

The combo is priced as a true parlay: you stake once, and it pays only if
every leg wins.

```
guarded_ask_i  = ask_i + 1¢                       # MAKER_MARGIN_GUARD_CENTS, per leg
p_combo        = 100 × Π (guarded_ask_i / 100)    # cents per $1 payout
contracts      = floor($10 / p_combo)              # $10 reference stake
stake          = contracts × p_combo
fee            = max(one combo-order taker fee, Σ per-leg taker fees)
edge           = joint_fair × contracts × 100 − stake − fee     (cents)
payout_ask     = contracts dollars                 # "$10 pays $X"
payout_fair    = $10 / joint_fair                  # MODEL ESTIMATE of a fair payout
```

- **The 1¢/leg guard.** A real app combo is filled by a market maker's RFQ
  quote, not at the product of the leg asks. Public measurements show the
  combo ask carries a spread whose size isn't constant, so no fixed margin
  can be subtracted. The guard raises every leg ask by 1¢ before taking the
  product. It only tightens the bar.
- **Fees.** The scanner's own fee helpers are reused verbatim, rounded up.
  The fee is the larger of a single combo-order fee and the sum of per-leg
  fees. The first is how an app combo is charged; the second is the cost of
  legging in by hand. Taking the larger is strictly conservative.

## 4. Pass bars

A combo becomes a pick only if all of these hold:

1. It has 2–4 legs (`MAX_COMBO_LEGS`).
2. Every leg passed the per-leg filter in section 1.
3. `joint_fair ≥ 2%`.
4. `edge ≥ 50¢` on the $10 reference stake, which is 5¢ per $1
   (`COMBO_MIN_EDGE_CENTS`).
5. `$10` buys at least one whole contract, and every leg's depth covers the
   contract count.

**Search.** The search is a beam search. It takes a pool of the top 60 legs
by edge, enumerates canonical subsets, keeps the top 200 combos per size,
and extends only passing combos. Results are deduplicated by
`P|TICKER:SIDE,...` and ranked by edge, then joint fair. The search is
deterministic: ties break on pool order.

## 5. Paper sizing (Kelly-capped)

Each pick carries a suggested **paper** stake. It's quarter-Kelly on a
$1,000 paper bankroll, clamped to $2–$10:

```
o       = 100 / p_combo                 # decimal odds
f*      = (joint_fair × o − 1) / (o − 1)
stake   = clamp(0.25 × f* × $1,000, $2, $10)
```

This is a bookkeeping number for forward paper evaluation. It is not a
recommendation to bet, and nothing is ever placed.

## 6. Output

- `combo_picks_found`: every pick this run.
- `new_combo_picks`: picks not alerted before.
- stderr: one summary line, plus one `PICK: ... -> $10 pays $X (fair $Y,
  edge +Zc; MODEL estimate, not guaranteed)` line per new pick.
- `--no-combo-picks`: removes both keys. The rest of the output is identical.
- Marking runs append each new pick to the pick log; see
  [backtest-protocol.md](backtest-protocol.md).
- The picker runs last, only inside the 58s budget. If it raises, the
  failure becomes an `errors` entry and never affects Tier 1–3 output.

## 7. Honest limits

- **The RFQ margin is unknown.** Live app combos are priced by maker
  quotes. The 1¢/leg guard is a heuristic, not a measured margin, so a real
  quote can be worse than the modeled price.
- **Same-game correlation is refused, not modeled.** No public data measures
  same-game correlation, so same-event legs are rejected and same-game pairs
  are only haircut. A real same-game parlay can be priced very differently.
- **Leg fairs are themselves estimates.** The joint fair multiplies their
  errors.
- **Picks are rare by construction.** Two equal legs at the minimum +5¢ edge
  pass only at asks of about 66¢ or more. Legs at +7¢ or more pass across
  most of the price range, because a cheap leg makes the same cent edge
  large in relative terms; the 2% joint-fair floor then binds at the low
  end. Most live sweeps have zero qualifying legs, so both pick lists are
  usually empty. That is the expected result, not a bug.
- **No track record.** Picks are logged for later grading against Kalshi
  settlement. No results exist yet; see
  [backtest-protocol.md](backtest-protocol.md).
