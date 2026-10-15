# X1 — Rug-gap exit forensics

Date: 2026-09-27. Implementer note: Codex CLI has repeatedly run read-only
in this environment (3 sibling tracks hit it); this track implemented and
validated directly in the coordinator shell per the brief's fallback clause.
All numbers below are reproducible from `runs/paper-1h/trades.jsonl` via
`analysis/x1_ruggap/ruggap.py`.

## Baseline (replay.net_pnl, realistic costs)

- 75 closes / **-$124.65** / 25.3% WR / PF 0.258 / $124.65 max DD
- Rug gaps (exit < 40% of recorded peak): **19 trades / -$115.45 / 0% WR / PF 0.000**
- Non-gaps: 56 trades / -$9.20 / 33.9% WR / PF 0.825

The gaps are 25% of trades and **92.6% of the net loss**. Everything else is
roughly breakeven-minus-costs.

## Gap-event anatomy

| # | Close (journal) | Chain | Exit/peak | Exit/entry | Reason |
|---|---|---|---|---|---|
| 9 | 2026-09-24 09:04–11:51 | solana | 0.003–0.230 | 0.006–0.254 | trailing stop -77%…-99.7% |
| 3 | 2026-09-24 15:20–22:27 | solana | 0.082–0.124 | 0.144–0.179 | dump detector -87.6%…-91.8% |
| 3 | 2026-09-24 21:12–22:12 | bsc | 0.000 | 0.000 | dump detector -100% in 60s |
| 1 | 2026-09-24 22:39 | bsc | 0.040 | 0.055 | venue dump m5 -94.5% |
| 1 | 2026-09-27 14:01 | solana | 0.206 | 0.291 | trailing stop -79.4% |
| 1 | 2026-09-27 14:23 | bsc | 0.000 | 0.000 | venue dump m5 -100% |
| 1 | 2026-09-27 19:19 | bsc | 0.034 | 0.057 | dump detector -96.6% |

- 15 of 19 rugged within 60 minutes of entry; 6 within 5 minutes (median 14.2 min).
- Only 5 of 19 ever took a partial TP rung; 14 never reached +50%.
- Chain: Solana 13 / -$84.84, BSC 6 / -$30.61.
- 17 of 19 gaps happened on 2026-09-24 (regime concentration).

## The central finding: these are liquidity-vanishing events, not exit-timing failures

Decomposing each gap trade's peak-to-exit loss at the configured trigger
(dump detector 0.88×peak; venue/trailing 0.70×peak):

- **Detection-lag loss (peak → trigger price): $49.72**
- **Gap loss (trigger price → actual fill): $141.55 — 74.0% of gap losses**
- **19 of 19 exits filled below HALF the trigger price.**

Even a perfect zero-latency trigger would leave $141.55 unrecoverable — and
the $49.72 "lag" portion itself assumes fills AT the trigger price during a
liquidity pull, which is fantasy. Realizable savings from any exit-side
change (faster polling, tighter triggers, instant market sells): **≈ $0**.

Smoking gun from `bot.log` (柴犬联盟 / WBNB, 2026-09-24):
```
[22:05:56] FOMO SIGNAL [bsc]: +160.8% 15m, buys/sells 3.8 (liq $33510)
[22:05:57] entered @ 0.000000190 BNB/token
[22:06:45] DUMP DETECTOR -100.0% in 60s, selling all
[22:06:47] CLOSED: realized -0.009000 BNB ($-7.02)
```
Entered, and the token was at zero **48 seconds later**. The detector fired
correctly. There was nothing to sell into. No exit policy recovers money from
a pool that no longer exists.

## Interventions tested (only what is observable with data we have)

### A. 24h post-dump re-entry veto — SHIPPED (already live, no new change needed)

Counterfactual on the full journal: the veto would have blocked **1 entry**
(SF🐲 #2, 2026-09-27 14:23, **-$7.05**) and **0 winners**. The guard
(`_dump_cooldown_active`, 24h, checked in both `guardrails_ok` and the
commit-time `_entry_commit_ok` under `self.lock`) is live in the running bot
and has fired 4 real skips today (☀️王金融 / WBNB, 20:05–20:10). The SF case
is instructive: the bot correctly dodged the first rug at 14:05 (-$0.09 via
dump detector), then re-entered the same mint ~13 min later at 2.1× the price
on a fresh +199.5% signal and lost the full stake. That failure mode is now
closed.

### B. Pre-entry high-gain veto (signal_gain_pct > 300) — REJECTED

| Split | Base | Kept (≤300) | Gaps |
|---|---|---|---|
| IS (50) | -$71.41 | -$16.19 | 11 → 4 |
| OOS (25) | -$53.23 | -$53.23 | 8 → 8 |

IS looks miraculous; OOS the rule fires on **zero** trades — 0% retention,
textbook curve-fit. The high-gain regime was a 2026-09-24 phenomenon.
**Fails the 60% OOS-retention gate. Rejected.**

### C. Faster polling / tighter triggers — REJECTED as structurally moot

The 柴犬联盟 case (48s entry-to-zero) and the 74%-gap-loss decomposition show
the loss happens after any trigger could fire. Track A already rejected
tighter dump thresholds on observability grounds; this forensics confirms the
deeper reason: the fills, not the triggers, are the problem.

## What would actually fix the gap losses

Nothing on the exit side. The only honest fixes are pre-entry:

1. **Don't enter tokens that are about to rug** — the parked W2 (bundled
   ownership) and W4 (wash detection) tracks, which need 30–60 enriched
   trades to calibrate. This forensics strengthens their mandate: exit-side
   work on gaps is spent effort.
2. **The 24h dump-cooldown** (done, live) — closes the re-entry hole.
3. **Regime awareness** — 17/19 gaps on one day; the Y1 regime-gate track is
   the right vehicle, not entry filters.

## Validation

- Chronological IS (first 2/3) / OOS used for intervention B; A is a
  deterministic counterfactual (no fitting).
- 3× slippage stress: gap trades are unaffected by slippage stress (fills are
  already ~zero); intervention B kept-set 3×: IS -$52.48 / PF 0.299,
  OOS -$59.80 / PF 0.076 — still rejected on retention, not stress.
- Tests: 15/15 X1 pass; full repo suite run at ship time (see below).
- Lane clean: only `analysis/x1_ruggap/` + `tests/test_x1_ruggap.py` added;
  `fomo_trader.py`, config, `runs/` untouched; bot not restarted; paper-only.

## ship_recommend: false (nothing new to ship)

The one shippable intervention (A) is already live in the running bot.
Everything else was rejected or is structurally moot. Verdict-only track;
no code ships.
