# Backtest protocol for combo picks

**Status: no backtest results exist for the combo picker.** Picks are being
logged so they can be graded later. Nothing in this repository claims a win
rate, a return, or an edge for Tier 4.

## 1. What is logged

On marking runs (no `--no-mark`), every **new** pick is appended as one JSON
line to `data/picker_picks.jsonl`. You can override the path with
`COMBO_SCANNER_PICK_LOG`. The log is append-only, and nothing in the scanner
reads it back.

Each line is `pick_to_json(pick)`:

| Field | Meaning |
| --- | --- |
| `legs[]` | per leg: `ticker`, `side`, `ask_cents` (confirmed taker ask), `fair_cents`, `fair_source`, `event_ticker`, `game_key`, `category`, `expiry_ts`, `venue`, `fee_mult`, `fee_rate`, `depth_contracts`, `direction`, `model_version` |
| `joint_fair` | haircut joint win probability (model estimate) |
| `flagged_pairs` | `[i, j, reason]` correlation flags |
| `p_combo_cents` | parlay price at guarded asks (ask + 1¢ per leg) |
| `contracts_10`, `stake_cents_10`, `fee_cents_10` | sizing and fees on the $10 reference stake |
| `edge_cents_10`, `expected_roi_pct` | modeled expected edge (estimate) |
| `payout_ask_10`, `payout_fair_10` | "$10 pays $X" at guarded asks; model-fair payout |
| `paper_stake_dollars` | quarter-Kelly paper stake, clamped to $2–$10 |
| `guarantee` | `"MODEL estimate — NOT guaranteed; fair-value model vs market"` |
| `combo_key` | `P|TICKER:SIDE,...` |
| `model_versions` | leg model versions (for example `kraken_brownian_v1`, `xvenue_poly_v1`) |
| `priced_at`, `expiry` | pricing time and the latest leg expiry (ISO 8601, UTC) |
| `commit` | git commit of the code that priced the pick, or `"unknown"` |

Floats are rounded to 4 decimal places, and keys are sorted.

## 2. How grading will work (planned)

1. **Settlement truth comes from Kalshi.** After every leg's expiry, read the
   public `GET /trade-api/v2/markets/{ticker}` for each leg and use its
   settled `result`. A pick wins only if every leg settled on the taken side.
   Picks with any void, unsettled or ambiguous leg are excluded, and the
   exclusion is reported. Proxy labels, such as an exchange spot price near
   the expiry, are not used, because they disagree with settlement often
   enough to bias results.
2. **Fills are the logged guarded asks.** P&L per pick is
   `contracts_10 × 100 × win − stake_cents_10 − fee_cents_10`. That is the
   modeled price including the 1¢/leg guard and conservative fees, never a
   better price after the fact.
3. **Placebo.** Grade the same picks under a zero-edge counterfactual (the
   win probability equals the price paid). This shows how much of any result
   is cost drag rather than signal.
4. **Calibration.** Bucket picks by `joint_fair`, compare the predicted and
   realized win rates, and report a Brier score.
5. **Out of sample only.** Constants (`LEG_MIN_EDGE_CENTS`,
   `JOINT_HAIRCUT_PTS`, `MAKER_MARGIN_GUARD_CENTS`, `COMBO_MIN_EDGE_CENTS`)
   are frozen while picks accumulate. Any retune is evaluated only on picks
   logged after the change, identified by `commit`.

## 3. The n ≥ 100 rule

No performance statement about combo picks will be made, in this
repository or anywhere else, until **at least 100 graded picks** exist. Even
then, any figure is reported as a backtest of logged paper picks, with its
sample size, date range, confidence interval and placebo comparison. It is
never presented as a forecast or a guarantee.

## 4. Related: Tier 3 replay

`scripts/backtest_model_edge.py` replays the Tier 3 single-leg entry rule on
a graded-signals file (`--grades`, or `COMBO_SCANNER_GRADES_PATH`). That file
is not shipped. The script makes no network calls and writes nothing. It
covers a single day of data with a very small sample, so it supports no
claim either; it exists so the rule's fee and fill math can be checked.
