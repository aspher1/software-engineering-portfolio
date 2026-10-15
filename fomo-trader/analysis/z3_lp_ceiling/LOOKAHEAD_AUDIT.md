# Z3 lookahead / data-leakage audit

## What kind of model this is

`z3_lp_ceiling.py` is a synthetic upper-bound calculator. It does **not**
read `runs/paper-1h/trades.jsonl`, any other journal, any price history, or
the network. It imports only `json`, `math` and `sys`, and it writes no
files. Every number it produces is a deterministic function of the
constants in `DEFAULTS` and the sensitivity overrides listed in the source.

- **Journal backfill:** none. No journal record is read, joined, labeled or
  replayed.
- **Time ordering:** not applicable. There is no time series and no
  in-sample/out-of-sample split. The IS/OOS design in report section 5 is
  a pre-registration for a test that has not been run.
- **Parameter fitting:** none. No input was tuned toward a verdict. Every
  input sits at a documented value, most at the LP-favorable end.
- **Measured values used:** only as context, never as model inputs:
  - the journal's Solana `sol_usd` range (≈ $116–$117.3), cited only to
    show that the assumed $115 is below it;
  - the dlmmbot ledger (10 → 9.98 SOL over 4 days), reported as a realism
    anchor.

## Bias direction of every assumption

| Input | Bias |
|---|---|
| A1 fee yield 0.7% per pool-day (period actually unspecified) | toward LP |
| A2 free daily compounding | toward LP |
| A3 perfect bin placement, no rebalance cost | toward LP |
| A4 zero inventory loss / IL | toward LP |
| A5 no failed transactions | toward LP |
| A6 zero priority fee | toward LP |
| A7 minimum signature count, base fee only | toward LP |
| A8 protocol cut 20% (grid also shows 0%) | the 0% rows lean toward LP; 20% is the grounded launch-pool figure |
| A9 all bin arrays pre-initialized | toward LP |
| A10 minimum viable bins = 1 | toward LP |
| A11 Solana rent formula | neutral (protocol constant) |
| A12 fixed 70-bin position account (0.0574 SOL deposit) | **possibly against LP.** This is the single input not set at its favorable end, because the grounding evidence supports it. The hypothetical small-account row shows the favorable alternative (+$1.12 at $7). See STAFF_REVIEW objection 1. |
| A13 two token accounts funded at open | neutral (mechanical); the one-account sensitivity is shown |
| A14 wallet rent-exempt minimum | neutral (protocol constant) |
| A15 bin-array rent (sensitivity only) | used only in a row that hurts LP; not in the headline |
| A16 SOL $115, below every journal observation | toward LP |
| A17 ticket = total capital budget | neutral (pre-registered in Z1 §3B) |
| A18 one position held 30 days (one set of open/close fees) | toward LP; rotation is shown as a sensitivity |

Every assumption except A12 is neutral or biased toward the LP. A12's
direction is disclosed and its favorable alternative is computed next to the
headline, so the reader can see exactly what the verdict depends on.
