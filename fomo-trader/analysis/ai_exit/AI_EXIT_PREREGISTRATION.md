# AI Exit-Manager — Preregistration (FROZEN 2026-09-27)

Shadow-only prototype. Nothing in `analysis/ai_exit/` is wired into
`fomo_trader.py`, the manage loop, entries, position sizing, kill switches,
or the rug guard. This document freezes what "try it" means, so a later
result cannot be reinterpreted as a success after the fact.

## What is being tested

Whether an exit advisor (default backend: `LocalExitPolicy`, a fast local
fade-signature policy; optional backend: `LLMAdvisor`, default OFF) can
improve realized exit outcomes OVER the deterministic floor (TP ladder,
trailing stop, hard stop, dump detector, venue tripwire, stale exit) on
the same trades, without ever weakening the floor.

The advisor may only recommend an EARLIER exit. The floor invariant
(`combine_with_deterministic`): any deterministic trigger fires regardless
of advice. There is no veto/stay decision in the vocabulary.

## Data gate (when "try it for real" is even possible)

Quote-path data is the fuel, and as of 2026-09-27 ~17:45 EDT the tank is
empty:

1. Z4 capture code exists but is DISABLED in the live config
   (`runs/paper-1h/config.json` has no `research` section; `quote_paths/`
   contains 0 files). Capture must be enabled by the operator first.
   (This lane does not touch `runs/` or config; enabling is an operator
   decision.)
2. After enablement, accumulate **>= 30 closed trades with >= 20 usable
   quote-path marks each**. Marks are ~2-5s manage ticks; a typical 30-min
   position yields hundreds.

No verdict before 30 qualifying closes. Synthetic-path results
(`shadow_eval --synthetic`) are engineering smoke tests only and count
for nothing.

## Baseline

For each qualifying real trade, replay its quote path twice through
`shadow_eval.replay` with the E3-mirrored cost model (notional $10,
venue 30bps/side, entry slip 100bps, exit slip 500bps, priority fee
2M lamports @ day's SOL/USD):

- BASELINE: deterministic exits only.
- ADVISOR: deterministic exits + advisor (floor invariant enforced).

Marks are not fills; both sides get the same haircut, so the comparison
is fair but both are mark-implied bounds, not realizable P&L.

## Metrics (per side)

closes, net USD, win rate, profit factor, avg net/trade, exit-reason
breakdown, advisor-attributed exit count.

## Pre-registered decision rules

Evaluated chronologically (walk-forward; no shuffling). At >= 30 closes:

- **KILL** the advisor backend if OOS net USD <= baseline net USD.
  (Advisor must beat the floor it advises on top of, net of identical costs.)
- **KILL** immediately if, in any shadow replay, the combination logic ever
  suppresses or delays a deterministic trigger (the invariant is structural,
  but a bug here is a kill offense, not a fix-and-continue).
- **KILL** (or fix-and-remeasure) the local policy if advise() p99 latency
  exceeds 50ms on real quote paths (standing hot-path budget).
- **KILL the LLM backend permanently** if it ever emits EXIT on timeout,
  error, or unparseable output (fail-open), or if it exceeds its daily call
  cap / advisory cadence in shadow eval.
- **PASS** requires: advisor net > baseline net at >= 30 closes AND all
  guardrails above hold AND the win is not concentrated in a single trade
  (>50% of delta from one close -> inconclusive, keep gathering).
- A PASS authorizes a shadow paper trial (advice logged next to live
  deterministic exits, still not wired in). It does NOT authorize wiring
  the advisor into the manage loop. That needs a separate user decision.

## What does NOT count

- Synthetic-path deltas (engineering only).
- In-sample threshold tuning: POLICY_DEFAULTS are placeholders. Any
  threshold change after seeing real quote paths restarts the 30-trade
  clock for the changed backend.
- The drafter/whale problem: none of this applies to copy-trading; this
  lane is exits for the bot's own entries only.

## Current status (2026-09-27)

Built: advisor framework + floor gatekeeper, local policy, LLM backend
(default OFF), shadow evaluator, test suite. Synthetic eval runs clean.
Real-data gate: 0/30 qualifying trades (capture off). No verdict possible.
