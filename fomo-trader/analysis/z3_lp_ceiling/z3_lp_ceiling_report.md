# Z3 — $7 DLMM LP fee-ceiling test

**Verdict: KILLED for the paper bot's $7 ticket.** Even with every fee-side
assumption set absurdly in the LP's favor, a $7 budget cannot fund the
refundable rent deposits a Meteora DLMM position needs ($7.17 at SOL $115).
The upper-bound net over 30 pool-days is **$0.00**, because no position can be
opened. The LP direction is closed for this program.

This is a synthetic upper bound. No trades were placed, no journal data was
used, and nothing here says any LP strategy is profitable. Regenerate every
number with `python analysis/z3_lp_ceiling/z3_lp_ceiling.py` (add `--json`
for the full result).

## 1. Assumptions (every input, labeled)

Labels: **measured** = observed in our journal or a cited ledger;
**grounding** = from today's research report
(`research_notes/copy-trading-track-records-20260927-2020/report.md`, topic 2),
used as given; **protocol constant** = Solana rule; **assumption** = chosen
here. "Bias" is the direction the choice pushes the ceiling.

| # | Input | Value | Label | Bias |
|---|---|---|---|---|
| A1 | Fee yield per pool-day | 0.7% (top of cited 0.1–0.7%) | grounding (the period is unspecified; treating it as *per day* is our choice) | toward LP, heavily |
| A2 | Compounding | fees re-deposited daily, free | assumption | toward LP |
| A3 | Bin placement | perfect: always in the active bin, no rebalance cost | assumption (from the brief) | toward LP |
| A4 | Inventory loss / IL | zero | assumption (from the brief) | toward LP, heavily |
| A5 | Failed transactions | none | assumption (from the brief) | toward LP |
| A6 | Priority fees | 0 lamports | assumption (the report's standard is 0.01–0.03 SOL per tx) | toward LP |
| A7 | Base fee | 5,000 lamports per signature; 3 signatures per episode (open with position keypair = 2, close = 1) | grounding (base fee) + assumption (signature count, a minimum) | toward LP |
| A8 | Protocol cut | 20% (LP keeps 80%) on launch pools; grid also shows 0% | grounding | the 0% rows lean toward LP |
| A9 | Bin arrays | already initialized; 0 non-refundable array rent | assumption | toward LP |
| A10 | Minimum viable bins | 1 (spans 1 bin array of 70 bins) | assumption | toward LP |
| A11 | Rent-exempt formula | (bytes + 128) × 3,480 lamports × 2 years | protocol constant (from Solana docs, not re-verified in this hermetic session) | neutral |
| A12 | Position account | 8,120 bytes → 0.0574 SOL refundable deposit (fixed 70-bin account) | assumption: recalled Meteora layout, **not verified live**; consistent with the report's "rent makes sub-~$100 positions impractical" | **the verdict depends on this; see section 4** |
| A13 | Token accounts | 2 × 165 bytes (memecoin + wSOL) → 0.00204 SOL each, refundable | protocol constant (SPL account size) + assumption (both must be funded at open) | neutral |
| A14 | Wallet rent-exempt minimum | 0.00089 SOL | protocol constant | neutral |
| A15 | Bin-array account (sensitivity only) | 10,136 bytes → 0.0714 SOL, non-refundable | assumption (recalled layout) | used only in a sensitivity that hurts LP |
| A16 | SOL price | $115 | assumption: repo fallback, **below every Solana `sol_usd` in the journal (measured ≈ $116–$117.3)**; cheaper SOL makes rent cheaper in USD | toward LP |
| A17 | Capital budget | the ticket must cover deposits + liquidity + fees | from Z1 §3B ("each episode has a $7 total capital budget") | neutral (mechanical) |
| A18 | Horizon | 30 pool-days, one position held throughout | from Z1 §4 | neutral |

How absurd A1 is: 0.7% per day compounded over a year is about
**1,180% APY**. The cited range comes from SOL-USDC, the deepest pair on
Solana, with the period unspecified. The only honest retail DLMM ledger found
(cryptognome/dlmmbot, **measured**, n=1) went 10 SOL → 9.98 SOL over 4 days
across 112 positions. That is **−0.05% per day**, 0.75 percentage points per
day worse than A1, at about 160× our ticket size.

What the ceiling ignores: adverse selection (**grounding**). One-sided
liquidity below the price is short-gamma, buying every dip into a
distribution where about 43% of tokens rug and about 73% lose more than 60%
within 20 minutes of migration. A4 prices that at zero.

## 2. The ceiling math

For ticket `T` (USD) and SOL price `S`:

- `deposit = position_rent + 2 × token_account_rent + wallet_min`
  = 57,406,080 + 2 × 2,039,280 + 890,880 = **62,375,520 lamports**
  (0.0624 SOL = **$7.17** at $115). This is refundable, but it must be held
  while the position is open.
- `nonrefundable = episodes × (signatures × 5,000 + priority) + new_bin_arrays × array_rent`
  = **15,000 lamports** ($0.0017).
- `deployable = T − (deposit + nonrefundable) × S`
- `fee_income_upper = deployable × ((1 + y)^30 − 1) × (1 − cut)`, where
  `(1.007)^30 − 1 = 0.2328`
- `net_upper = fee_income_upper − nonrefundable`, or $0.00 if
  `deployable ≤ 0` (the position cannot be opened, so nothing is spent)

## 3. Results

**Headline ($7, SOL $115, 0.7%/pool-day, 20% cut):** deposits are
$7.1732, which is **102.5% of the ticket**. Deployable liquidity is
−$0.17, so the position is **infeasible**. Upper-bound net: **$0.00**.

Grid (30 pool-days, SOL $115, net upper bound in USD):

| Ticket | 0.1%/day, 0% cut | 0.1%, 20% | 0.4%, 0% | 0.4%, 20% | 0.7%, 0% | 0.7%, 20% |
|---|---|---|---|---|---|---|
| $7 | 0.00 (infeasible) | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| $25 | 0.54 | 0.43 | 2.27 | 1.81 | 4.15 | 3.32 |
| $100 | 2.82 | 2.26 | 11.81 | 9.45 | 21.61 | 17.28 |

$7 sensitivities (each changes one thing from the headline):

| Change | Net upper bound, 30 days |
|---|---|
| None (base) | $0.00, infeasible |
| wSOL account treated as not needed | +$0.01 |
| SOL $100 (below every journal price) | +$0.14 |
| SOL $100 and 0% protocol cut | +$0.18 |
| SOL $100 plus one standard 0.01 SOL priority fee on each of 2 txs | $0.00, infeasible |
| One uninitialized bin array | $0.00, infeasible |
| 30 one-day episodes (rotation) | $0.00, infeasible |
| 5%/pool-day launch-pool stress (beyond the cited range) | $0.00, infeasible |
| **Hypothetical 1-bin position account (392 bytes, unverified)** | **+$1.12** |

Breakevens at the headline assumptions:

- $7 is infeasible at any SOL price at or above **$112.20**.
- At SOL $115, the smallest ticket whose ceiling clears $0 is **$7.18**.

Also for context: with one uninitialized bin array, the $25 ceiling is
**−$6.43** and the $100 ceiling is **+$7.54**.

## 4. Verdict

**KILLED.** The pre-registered rule is "upper-bound net ≤ $0 kills". At the
headline assumptions the $7 upper bound is $0.00: the ticket cannot fund the
position's refundable deposits, so nothing can be deployed.

Stated plainly, the kill is a knife-edge in arithmetic but not in economics:

- The $7 budget sits just under the ~$7.17 deposit floor. Cheaper SOL (below
  $112.20) or skipping the wSOL account makes the ceiling a few cents
  positive: **at most +$0.18 over 30 days** in any grounded sensitivity. A
  single standard priority fee (0.01 SOL ≈ $1.00–$1.15) erases that by
  about 6× or more.
- The one change that flips the verdict materially is **A12**. If Meteora
  lets a position be opened with a small, bin-proportional account (the
  hypothetical 392-byte, 1-bin case), the $7 ceiling is **+$1.12 over 30
  days**. That rests on A1's absurd yield plus zero IL. At the bottom of the
  cited range (0.1%/day) it would be about +$0.14. We could not check this
  in a hermetic session. **Checking the current Meteora position rent costs
  nothing and should be done before anyone cites this kill as permanent**
  (see STAFF_REVIEW objection 1).
- The $25 and $100 rows clear $0. That is what an upper bound with near-zero
  unavoidable costs looks like. It says nothing about realistic net: the
  only honest ledger (−0.05%/day) and the adverse-selection base rates both
  point to ≤ $0. Those tickets are also outside this program: raising the
  ticket is a position-sizing change, which this track may not make.

Written kill: **the LP / fee-earning direction is closed for the $7 paper
program.** It reopens only if (a) the A12 rent check shows small positions
are possible, or (b) a larger LP budget is separately authorized. Either
condition triggers the pre-registered test in section 5. Nothing is run
until then.

## 5. Pre-registered follow-up (conditional; NOT run)

Applies only if a reopening condition from section 4 is met. Taken from Z1
§3B and fixed now so it cannot be tuned later.

- **Data:** one publicly observable pool, chosen before looking at its
  returns. Use public swaps, fee rates, and active-bin liquidity. No paid
  data.
- **Policy:** exactly one fixed LP policy (one-sided SOL below price, fixed
  bin width, fixed rebalance rule). No range-width sweep.
- **IS/OOS:** 30 days in-sample, then **60 days untouched out-of-sample**.
  Each episode has a fixed total capital budget that includes rent deposits.
  Inventory is valued at executable liquidation prices. Include range
  management, transaction costs including priority fees, failed
  withdrawals, and adverse inventory changes.
- **Minimum sample:** at least 30 completed OOS episodes; fewer means no
  advancement.
- **Kill (any one):** cumulative OOS net reaches −3× the ticket (−$21 at
  $7); OOS net or 3×-cost-stressed net ≤ $0; fails to beat holding the
  initial token mix by more than $0; day-block lower confidence bound for
  mean net ≤ $0.
- **Budget:** at most 4 analyst hours for data, zero new trades, $0 paper
  P&L until the OOS window opens.

## 6. Limits

- A12, A13 and A15 are recalled account layouts, not verified live. The
  sensitivity table shows exactly which of them can change the verdict.
- A1 is a yield on active liquidity. A perfectly placed concentrated
  position could, in principle, earn more than 0.7%/day in a hot launch
  pool for a few hours. The 5%/day stress row shows that even this cannot
  help a position that cannot be opened.
- This covers one pool and one schedule. Per Z1: do not stretch one failed
  ceiling into a universal LP conclusion, or a passing one into a trading
  recommendation.
