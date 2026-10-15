# Y3 fade-the-signal diagnostic — report

**Verdict: `ship_recommend: false`. Nothing ships.**

## The question

Is our 100%+/15m breakout entry signal *backwards* — are we systematically
buying the top of a spike that mean-reverts? And if so, is there a tradeable
inverse: wait for the pullback, then enter ("buy the retest")?

## Diagnostic: the signal is NOT backwards (74 closed trades)

| Metric | Value |
|---|---|
| Median peak / entry | **1.260** (+26% median run after entry) |
| P(peak ≥ 1.10× entry) | **0.74** |
| P(peak ≥ 2.00× entry) | 0.11 |
| Median exit / entry | 0.893 |
| Median give-back (peak→exit)/peak | **0.291** |
| Winners (exit > entry) | 27/74 (36.5%), median peak 1.505 |
| Losers that peaked ≥10% | 29/74, median peak 1.267, median exit/peak **0.206** |

Tokens do NOT die at entry. Four out of five run at least +5% after we buy;
the median token runs +26%. Then it fades: the median trade gives back 29%
of its peak, and 29 losers ran +27% on average before collapsing to ~21% of
peak. The first take-profit rung (+100%) is reached by only ~11% of trades,
so the book lives and dies on the trailing stop.

**The disease is exits, not entries.** The entry timing is fine; the exit
ladder harvests badly.

### Fade mirror (directional diagnostic only, NOT tradeable)

Shorting every entry and covering at the actual exit: median gross return
**+10.7%**, mean +18.0%, positive on 63.5% of trades. This is a measurement
of directional bias, not a strategy — there is no venue to short fresh
Solana/BSC memecoins, and borrow/cover costs are unmodellable. It confirms
the drift after entry is real but modest, and fully explained by the
give-back above.

### Splits

- Solana (52): med peak 1.284, med exit 0.926, WR 42%. BSC (22): med peak
  1.171, med exit 0.821, WR 23%.
- Signal gain <100% (31): med peak 1.300, med exit **1.024**, WR 55% — the
  only bucket whose median trade ends green (consistent with Track C).
  400%+ (11): med exit 0.254, WR 9%.

## Tradeable variant: pullback entry — REJECTED (structural)

Rule tested: enter at `entry × (1−X)` for X ∈ {10%, 15%, 20%, 30%}, but only
if the price demonstrably touches that level after the signal. The journal
has no intraday price path, so a fill is **provable** only when
`exit ≤ entry×(1−X)` (the price demonstrably got there). Consequences:

1. **Every provable fill is a loser by construction.** All 27 winners have
   unprovable fills — we cannot show a single winner would ever trigger a
   pullback entry.
2. Provable-fill P&L, chronological IS (49) / OOS (25), realistic costs:

| X | IS fills | IS net | IS PF/WR | OOS fills | OOS net | OOS PF/WR | OOS 3× stress net |
|---|---|---|---|---|---|---|---|
| 10% | 20 | −$88.75 | 0.000 / 0.00 | 17 | −$64.84 | 0.000 / 0.00 | −$65.36 |
| 15% | 18 | −$84.09 | 0.000 / 0.00 | 15 | −$61.72 | 0.000 / 0.00 | −$62.17 |
| 20% | 16 | −$79.52 | 0.000 / 0.00 | 13 | −$59.21 | 0.000 / 0.00 | −$59.58 |
| 30% | 11 | −$71.73 | 0.000 / 0.00 | 9 | −$56.33 | 0.000 / 0.00 | −$56.54 |

(Baselines on the same cost convention: IS −$47.18 / PF 0.535 / WR 0.45;
OOS −$63.55 / PF 0.122 / WR 0.20.)

3. Even the **impossible best case** — every winner dips exactly X% then rips
   to its actual exit — only turns positive in-sample at X ≥ 15%, and stays
   negative OOS at every X. It requires all 27 winners to dip-then-rip, a
   fantasy the data cannot support.

**Structural kill:** a pullback entry adversely selects. The observable
evidence can only fill it on losers; the winners it needs are unobservable.
This is not a calibration problem — no threshold fixes it. A live pullback
entry is additionally untestable without intraday price capture (new data
field required).

## What this rules in / out

- OUT: "buy the breakout is backwards", "fade the signal", "buy the retest"
  on current data.
- IN (for future tracks): the exit ladder is the bleed. Median +26% runs
  with 29% give-back and a +100% first rung that ~89% of trades never reach
  is an exit-design problem, not a signal problem. Any future exit work
  should start here, not at entries.
- The 29 "ran then collapsed to 0.2× peak" trades overlap the rug-gap
  population — X1's forensics remain the highest-value open question.

## Files

- `fade.py` — loader (fail-open), forward diagnostic, pullback bounds engine
- `y3_fade.json` — full numbers + `ship_recommend: false`
- `LOOKAHEAD_AUDIT.md`, `STAFF_REVIEW.md`

## Verification

- 6/6 Y3 tests pass; full repo suite **238 passed, 0 failed**.
- Lane clean: only `analysis/y3_fade/` + `tests/test_y3_fade.py` added;
  `fomo_trader.py`, config, `runs/` untouched; bot not restarted.
- Implemented directly in the coordinator's shell (Codex CLI sandbox was
  read-only in prior tracks; X2 set the precedent for direct implementation
  of pure replay analyses). Logged in `~/workspace/tmp/codex_y3_run.log`.
