# Fundamental fix validation — 2026-09-28

**Verdict: REJECT. No trading rule shipped.** The tested screens reduce some losses, but none establishes a profitable, cost-stressed, cross-chain OOS result. The historical OOS cohort contains only BSC trades, which independently prevents a cross-chain validation.

## Frozen replay and accounting

- Journal snapshot: `runs/paper-1h/trades.jsonl`, 226 lines, SHA-256 `c85b8f2ac39c7ebde405170521efeb4bd264104b6d750a22a770b32b3d2bcc54`.
- Split after append line 150 (first two thirds of journal lines). FIFO pair entries and closes by `(chain, mint)`. A pair is IS only when its close is at or before line 150; it is OOS only when its entry is after line 150. Purge three pairs crossing the boundary. This gives 72 IS pairs (49 Solana, 23 BSC) and 36 OOS pairs (0 Solana, 36 BSC). Three closes have no matching entry; one entry remains open. Neither enters the replay. Timestamps were not sorted.
- Filter decisions use only the entry record. Missing or nonfinite evidence passes through. BSC passes the Solana-only holder screen by design.
- Costed USD P&L starts from recorded realized native P&L. Subtract, on both entry stake and exit proceeds, a 0.25% estimated swap fee plus 1% slippage per leg at 1x, or 3% slippage per leg at 3x. Add two estimated transaction fees: 0.002 SOL per leg or 0.00002 BNB per leg. Convert using the recorded close/entry USD rate; use $115/SOL for early Solana closes without a rate, as in the offline replay. These are paper-quote estimates, not executable fills. The stress triples slippage, not the fixed fees.
- Edge is **(candidate net USD − full-cohort net USD) / original cohort size**. A skipped entry contributes $0. Positive edge can therefore mean an avoided loss while the retained trades still lose money. Selection maximized 1x IS edge, required at least 30 retained IS trades and at least one veto, with fewer vetoes breaking ties. One selected threshold per candidate was then evaluated on OOS once. The OOS count gate is applied to **retained** trades.

| Cohort | Original n | 1x net | 3x net | 1x win rate |
|---|---:|---:|---:|---:|
| IS baseline | 72 | −$131.23 | −$151.55 | 19.4% |
| OOS baseline | 36 | −$60.58 | −$69.03 | 16.7% |

## Candidate 1: commit-time liquidity

Swept minimum USD liquidity floors on IS: $10k, $15k, $20k, $25k, $30k, $40k, $50k, $75k, $100k. Coverage: 72/72 IS and 36/36 OOS entries. The $30k floor maximized eligible IS edge; higher floors retained fewer than 30 IS trades.

| Split | Retained / original | 1x net retained | 1x edge / original | 3x net retained | 3x edge / original | Retained 1x win rate |
|---|---:|---:|---:|---:|---:|---:|
| IS | 38 / 72 | −$106.13 | +$0.3486 | −$115.33 | +$0.5031 | 7.9% |
| OOS | 35 / 36 | −$57.35 | +$0.0898 | −$65.57 | +$0.0959 | 17.1% |

OOS retained **25.8%** of IS 1x edge (19.1% at 3x), below the required 60%. The one OOS veto avoided about $3.23 of costed loss. Retained OOS net remained negative under both costs. IS improvement was $22.30 Solana and $2.80 BSC; all OOS improvement was BSC. **Reject:** retention, profitable/stressed OOS, and chain coverage fail. OOS retained n=35 and win rate below 90% pass their respective gates.

## Candidate 2: 15m buy/sell imbalance and buy count

Swept ratio floors 1.5, 2, 2.5, 3, 4, 5 against buy-count floors 10, 20, 30, 50, 75, 100 on IS, rejecting an entry when either available field is below its floor. The entry-record `signal_ratio` covers 72/72 IS and 36/36 OOS; `m15_buys` covers only 5/72 IS and 36/36 OOS. Missing counts pass through. The eligible IS optimum was ratio ≥2 and 15m buys ≥100. The sharp change in count coverage means the chosen floor is especially weak evidence.

| Split | Retained / original | 1x net retained | 1x edge / original | 3x net retained | 3x edge / original | Retained 1x win rate |
|---|---:|---:|---:|---:|---:|---:|
| IS | 36 / 72 | −$87.93 | +$0.6014 | −$97.39 | +$0.7523 | 19.4% |
| OOS | 5 / 36 | −$13.25 | +$1.3150 | −$14.36 | +$1.5185 | 20.0% |

OOS improvement retained 218.7% of IS edge, but **only five OOS trades remain**, far below 30, and their net is negative at both cost levels. Every OOS improvement is BSC. **Reject:** retained OOS sample, profitable/stressed OOS, and chain coverage fail. The high avoided-loss edge is not evidence of a profitable retained strategy.

## Candidate 3: Solana top-1 holder concentration

Swept top-1 ceilings of 10%, 20%, 30%, 40%, 50%, 60% on IS. Entry-record `holder_top1_pct` is non-null for **0/49 Solana IS trades** and 0/36 OOS trades; all BSC holder values are null and would be exempt. Every threshold retains all 72 IS and all 36 OOS trades, yielding $0 edge per original trade at 1x and 3x. No threshold can be selected or validated. Existing rug-guard settings already include a 40% top-holder ceiling when usable evidence exists. **Reject:** zero feature coverage and zero edge; OOS also has no Solana trades.

## Candidate 4: tighten the venue-m5 tripwire

The current tripwire is m5 ≤−30%, checked every 30 seconds. Tested whether the recorded venue exits permit replay of tighter thresholds −25%, −20%, −15%, and −10%. The journal records only the **terminal** m5 on venue-dump closes: five IS values (−83.9, −94.5, −100, −38.9, −38.0%) and five OOS values (−47.3, −94.5, −100, −100, −94.5%). All ten are already below −30%; none is an observed crossing between −30% and −10%. The journal does not contain the earlier m5 observations, bid depth, or an exit fill at a proposed earlier crossing. Consequently an IS/OOS P&L edge, retention, win rate, or 3x result for tightening is **not identifiable** from this replay. Assigning a fill at the threshold would manufacture favorable prices. **Reject as unvalidated; no threshold change.**

## Gate checklist and diagnosis

| Gate | Liquidity $30k | Ratio 2 / buys 100 | Holder | Venue m5 |
|---|---|---|---|---|
| OOS edge > $0 | Pass: +$0.0898/trade | Pass: +$1.3150/trade | Fail: $0 | Unmeasurable |
| OOS ≥60% of IS edge | Fail: 25.8% | Pass: 218.7% | Fail | Unmeasurable |
| OOS retained net > $0 at 1x and 3x | Fail: −$57.35 / −$65.57 | Fail: −$13.25 / −$14.36 | Fail: baseline unchanged | Unmeasurable |
| Retained OOS n ≥30 | Pass: 35 | Fail: 5 | 36 unchanged; no screened trades | Unmeasurable |
| Retained win rate ≤90% | Pass: 17.1% | Pass: 20.0% | No effect | Unmeasurable |
| Edge demonstrated across chains | Fail: OOS BSC only | Fail: OOS BSC only | Fail: no holder evidence | Fail: OOS BSC only |

Six OOS exits were priced at ≤1% of entry and together lost about **$34.33** after 1x costs. Four were dump-detector exits and two were venue-dump exits. This supports the analyst's structural rug-gap diagnosis: liquidity removal can be visible only after the quote has collapsed. The screens tested here do not demonstrate a reliable pre-entry way to avoid it, and this OOS period cannot establish cross-chain generalization. More fresh, mixed-chain entry and venue-observation data are needed before a rule can meet the stated gates.

**Implementation:** no change to `fomo_trader.py`, configuration, risk controls, or `CHANGELOG.md`; no bot restart. Full suite: `.venv/bin/pytest -q` → **640 passed, 0 failed**.
