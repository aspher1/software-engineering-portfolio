# Phase A: Diagnosis + fix spec (analyst/architect role)

You are the analyst/architect for a fundamental improvement session on the FOMO Trader memecoin paper-trading bot at `~/workspace/fomo-trader`. The bot is PAPER-ONLY (dry_run=true) trading memecoins on Solana and BSC. Another model will implement your spec, so be precise and concrete.

## Context you must read first
1. `~/workspace/fomo-trader/runs/paper-1h/trades.jsonl` — the full trade journal. Focus on the last ~24h (2026-09-27). Entries have type "entry" with enriched fields: signal_price, commit price (native+USD), signal-to-fill slippage %, commit-time liquidity USD, entry latency sec, 15m buy/sell counts, 15m volume, mcap, Solana top-1/top-5 holder concentration, chain. Closes have type "close" with realized_usd and exit reason.
2. `~/workspace/fomo-trader/runs/paper-1h/bot.log` — recent log (tail).
3. `~/workspace/fomo-trader/fomo_trader.py` — skim the rug guard (`rug_check`, honeypot screen), dump detector thresholds (live: quote −12%/60s, venue m5 −30%), cooldown/cap logic, and the allocator (`allocator.py`, currently live, multiplier 0.5x–3.0x).
4. `~/workspace/fomo-trader/analysis/shadow_dump.py` — the shadow faster detector (8%/30s) and any shadow log showing would-have-fired events.

## Known evidence (verify, don't assume)
- The dominant P&L leak is rug-gap exits: one-tick −100% venue dumps (DRA −$7.00, USDSM −$7.04, 爸爸金融🏆 −$6.00, MON −$4.96 on 2026-09-27). Day P&L ≈ −$27.
- Tightening exit thresholds can't fix one-tick liquidity removals (replay showed only $0.26 recoverable). The fix must be PRE-ENTRY: screen out likely ruggers before capital commits.
- A prior pre-entry rug screen was rejected (data-capture gap), but entries are now enriched — 30+ enriched closes exist. Re-examine with the new data.
- Current live thresholds: quote dump −12%/60s, venue m5 −30%. Don't propose touching these without replay evidence.

## Your deliverable
Write `~/workspace/fomo-trader/analysis/fundamental_fix_spec.md` containing:
1. **Diagnosis**: the #1 P&L leak quantified from the journal (counts, $, share of net loss). Secondary leaks ranked.
2. **Proposed rule**: ONE fundamental fix, specified exactly — the features it uses (only features available at commit time, i.e. present on "entry" journal records), the exact condition/threshold, where it hooks in (which function, before/after which existing guardrail). If it's a pre-entry rug screen, give the exact screen (e.g. "skip if commit-time liquidity < X AND 15m buys/sells ratio < Y AND ...").
3. **Replay validation plan**: how to test it on the journal — in-sample/out-of-sample split (time-ordered), exact gates: OOS must retain ≥60% of IS edge, 3x-slippage cost stress, overfitting red-flag checks (win rate >90% reject, single-chain concentration reject, <30 OOS trades = inconclusive), and what "fail" looks like.
4. **What NOT to do**: list the tempting-but-unsupported changes you considered and rejected, and why (be honest — a failed validation is a valid outcome).

## Hard constraints (the implementer will enforce these)
- Paper-only; dry_run=true untouched. Never touch trades.jsonl, state.json, bot.log, deaths.log, keypair.json. No private keys.
- Never loosen risk caps, kill switches, rug guard, cooldowns, max-position limits, or increase position size for profit. Your rule is an ADDITIONAL filter only.
- No network calls, subprocesses, or model inference in the entry/exit hot path. GeckoTerminal stays sequential and rate-limited.
- Verdict-only until every gate passes: if validation fails, the implementer ships nothing.

Be skeptical of your own proposal. If the data doesn't support a pre-entry screen, say so and propose the next highest-value fundamental fix with the same rigor.
