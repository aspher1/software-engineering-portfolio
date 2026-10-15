# Phase B: Honest replay validation + ship-or-reject (implementer role)

You are the implementer for a fundamental improvement session on the FOMO Trader memecoin paper-trading bot at `~/workspace/fomo-trader`. The analyst (a separate model) has already done the diagnosis — read its spec FIRST: `~/workspace/fomo-trader/analysis/fundamental_fix_spec.md`. Its verdict on the signal-gain veto was NO-SHIP. Your job: test the next candidates honestly and ship at most ONE rule, only if it passes every gate.

## Candidate rules to test (in order), all as pre-entry ADDITIONAL filters
1. **Commit-time liquidity screen**: sweep minimum-liquidity thresholds over the journal; skip entries below the threshold.
2. **Buy/sell imbalance screen**: sweep 15m buy/sell ratio and buy-count floors.
3. **Holder concentration screen** (Solana only): skip if top-1 holder % above a swept threshold. BSC holder data is null — handle honestly (screen applies to Solana only; report chain coverage).
4. If none pass: test venue-m5 tripwire tightening with replay evidence. If that fails too, report honestly that no fix validates — do NOT ship anything.

## Replay methodology (non-negotiable)
- Journal: `~/workspace/fomo-trader/runs/paper-1h/trades.jsonl`. Pair type "entry" records with their "close" records.
- Split by journal APPEND ORDER (timestamps have backward jumps — do not sort by time). Purge trades crossing the IS/OOS boundary. Require ≥30 OOS trades or the result is inconclusive.
- Use ONLY features present on the "entry" record (commit-time data). No look-ahead.
- Cost model: apply 1x and 3x slippage stress + chain fees like the analyst did.
- Gates (ALL must pass): OOS edge > 0; OOS retains ≥60% of IS edge; survives 3x-slippage stress; reject if kept win rate >90% (overfit flag), if edge concentrates in one chain, or if OOS < 30 trades.
- Sweep thresholds on IS only; evaluate the single chosen threshold on OOS once (no OOS peeking during the sweep).

## If a rule passes every gate — ship it
- Implement as an additional pre-entry filter in `fomo_trader.py` (hook it in the same place the rug guard runs, after existing guardrails; it must be pure local logic on commit-time data — no network, no subprocess, no model inference).
- Add/extend tests (new test file or extend existing), run the FULL suite: 0 failures required.
- Update `CHANGELOG.md` with the rule, the validation numbers (IS/OOS edge, retention %, stress results, OOS n), and the gate checklist.
- Write the validation report to `~/workspace/fomo-trader/analysis/fundamental_fix_report_<date>.md`.

## If nothing passes — ship nothing
- Write `~/workspace/fomo-trader/analysis/fundamental_fix_report_<date>.md` documenting each candidate, the numbers, and why it failed. Say plainly the leak is structural if the evidence points there.

## Hard constraints
- Paper-only: dry_run=true stays intact. NEVER touch trades.jsonl, state.json, bot.log, deaths.log, keypair.json. Never install/read/log private keys.
- Never loosen risk caps, kill switches, rug guard, cooldowns, max-position limits, or increase position size for profit.
- No network calls, subprocesses, or model inference in the entry/exit hot path. GeckoTerminal stays sequential and rate-limited.
- Do NOT restart the bot. The coordinator handles restarts. Your job ends at green suite + report.
- Note: a sibling lane (the 6h self-improvement cron) may be editing files concurrently. If you find test failures that look like another lane's in-flight edits (not your changes), do NOT repair them — report them and stop.

Report at the end: shipped or rejected, the numbers, test counts (e.g. "N passed, 0 failed"), and files changed.
