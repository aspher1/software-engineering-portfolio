# Fundamental fix spec — 2026-09-27 ~21:50 EDT (analyst: GPT-6 Astra via Codex)

## 1. Diagnosis (quantified from journal replay)

**#1 P&L leak: rug-gap exits (one-tick venue dumps).** Trades closing on DexScreener m5 −100% / quote −100% within seconds of entry — liquidity removed before any exit threshold can fire. Examples 2026-09-27: DRA −$7.00, USDSM −$7.04, 爸爸金融🏆 −$6.00, MON −$4.96. These wipes are the majority of the day's ≈−$27 net.

**Secondary leaks:** gradual dumps caught by the −12%/60s detector still lose −$1 to −$3 each (they exit, just late); win rate is low (~20%).

## 2. Candidate rule tested: signal-gain veto (skip entries with signal_gain_pct > 200%)

Replay results (Astra, time-ordered split, cost model: 1%/3% slippage stress + chain fees):

| cohort | split | n | taken | stress | raw base | raw kept | net edge/trade | net WR (kept) |
|---|---|---|---|---|---|---|---|---|
| priced | IS | 57 | 39 | 1x | −46.63 | −26.36 | +0.545 | 23.1% |
| priced | IS | 57 | 39 | 3x | −46.63 | −26.36 | +0.701 | 15.4% |
| priced | OOS | 30 | 19 | 1x | −36.82 | −24.82 | +0.562 | 21.1% |
| priced | OOS | 30 | 19 | 3x | −36.82 | −24.82 | +0.740 | 15.8% |
| enriched | IS | 27 | 16 | 1x | −10.67 | −2.90 | +0.475 | 31.3% |
| enriched | IS | 27 | 16 | 3x | −10.67 | −2.90 | +0.685 | 25.0% |
| enriched | OOS | 15 | 11 | 1x | −34.06 | −26.51 | +0.613 | 9.1% |
| enriched | OOS | 15 | 11 | 3x | −34.06 | −26.51 | +0.725 | 9.1% |

**Verdict: NO-SHIP.** Reasons:
- OOS profit remains negative even before cost stress (kept −24.82 / −26.51) — the rule reduces losses but doesn't find edge.
- All OOS improvement comes from BSC → chain-concentration reject.
- Enriched OOS is only 15 trades (< 30 minimum) → inconclusive regardless.

## 3. Methodological finding: journal timestamps have backward jumps

Do NOT split IS/OOS by timestamp sort — it gives misleading results. Use journal append order for the historical split, purge trades crossing the boundary, and require fresh OOS data.

## 4. Direction for the implementer

The signal-gain veto is rejected. Next candidates to test honestly, in order:
1. **Pre-entry liquidity screen**: skip if commit-time liquidity < $X (sweep X over the journal) — directly targets rug-gap wipes.
2. **Buy/sell imbalance screen**: skip if 15m buys/sells ratio < Y or buy count < Z.
3. **Holder concentration screen** (Solana only, where data exists): skip if top-1 holder % > W.
4. If none pass: venue-m5 tripwire tightening with replay evidence, or report honestly that no pre-entry screen validates and the leak is structural (one-tick removals are unfilterable pre-entry).

Same gates: time-ordered split by APPEND ORDER, OOS retains ≥60% of IS edge, 3x-slippage stress, ≥30 OOS trades, reject on WR >90% / single-chain concentration / single-regime. Verdict-only until every gate passes — a failed validation ships nothing.
