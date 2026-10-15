# Y1 Regime Gate — Pre-registered protocol

Written BEFORE any gate evaluation ran. Any deviation is documented in the final report.

## Question
Stop asking WHICH token to buy; ask WHEN to trade at all. Does a binary regime gate
(trade / stand down), computed only from data available at entry time, separate
winning regimes from losing ones in our journal?

## Data
- Journal: `runs/paper-1h/trades.jsonl` — 72 entries, 74 closes, 2026-09-24 → 2026-09-27.
- Only TWO active trading days (Sep 24: 60 closes; Sep 27: 14 closes; Sep 25–26: zero
  trades, bot halted). This thinness is the central validity threat and is pre-registered
  as an expected-failure reason.
- P&L convention: reuse `analysis/replay.py` `net_pnl` / `metrics`
  (realistic costs; `SOL_FALLBACK_USD=115.0`; `--stress` triples slippage).

## Decision point and features (no look-ahead)
- Gate decision is made per ENTRY. For each close, its gate features are computed from
  closes with `ts` strictly BEFORE its matched entry's `ts`
  (matched entry = latest entry with same mint and `ts <= close ts`).
- Orphan closes (no matched entry; expected ~3) FAIL OPEN (kept), documented.
- Features: trailing-12h net USD, trailing-24h net USD, trailing-12h trade count,
  hours since previous close, cumulative net USD before entry, entry AM/PM half (ET),
  entry day-of-week.
- Missing features (no prior closes, e.g. first trades of Sep 24) FAIL OPEN (trade).

## Pre-registered IS/OOS split (by time, never by outcome)
- IS: closes with `ts < 2026-09-26T00:00:00` (all Sep 24, n=60).
- OOS: closes with `ts >= 2026-09-26T00:00:00` (all Sep 27, n=14).

## Gate candidates (fixed before evaluation)
- R1: skip if trailing-12h net USD < T,  T in {0, -5, -10, -20}
- R2: skip if trailing-24h net USD < T,  T in {0, -10, -20, -40}
- R3: skip if trailing-12h trade count < N (thin regime), N in {3, 5, 10}
- R4: skip if hours since previous close > 12 (cold-restart regime)
- R5a: skip if entry in PM half (12:00–23:59 ET); R5b: skip if entry in AM half
- R6: skip if cumulative net USD before entry < -50 (drawdown regime)

## Ship bar (all must hold)
1. Plateau: ≥2 adjacent thresholds in the same direction both beat baseline.
2. OOS edge retains ≥60% of IS edge, where edge/trade =
   (filtered_usd − baseline_usd) / n_retained, computed separately per split.
3. Filtered OOS beats unfiltered OOS under 3×-slippage stress.
4. No single-regime dependence: a gate whose entire effect comes from one of the two
   active days is rejected regardless of numbers.
5. Full repo test suite green; gate check benchmarked (p50/p99 over ≥10k decisions).

## Expected honest failure modes (pre-registered)
- Only 2 active days → day-granularity gates cannot clear bar #4.
- Sep 27 OOS is 14 trades; any OOS "win" may be noise.
- If nothing clears the bar: report the negative with numbers so nobody re-tests blind.
