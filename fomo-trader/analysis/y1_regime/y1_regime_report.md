# Y1 Regime Gate — final report: REJECTED, nothing ships

**Verdict: `ship_recommend = false` for every gate tested.**

## What was built
- `analysis/y1_regime/regime.py` — per-entry binary regime gate. Every feature is
  computed from closes with `ts` strictly BEFORE the matched entry's `ts`
  (matched entry = latest entry with same mint and `ts <= close ts`). Orphans and
  missing features FAIL OPEN (trade). Pure stdlib, no shared state, lock-free.
- `analysis/y1_regime/PRE_REGISTER.md` — protocol written before any evaluation ran.
- `analysis/y1_regime/journal_snapshot.jsonl` — frozen journal (75 closes) the
  verdict was computed on. The live bot added one close mid-analysis (74 → 75);
  the snapshot keeps the result reproducible.
- `analysis/y1_regime/raw_results.json`, `latency.json` — machine-readable results.
- `tests/test_y1_regime.py` — hermetic tests (below).

Implementer note: Codex CLI was bypassed; this was implemented and validated
directly in the coordinator's shell (the brief's fallback clause). Sibling tracks
hit the read-only-sandbox failure mode repeatedly; direct implementation was the
reliable path. All validation below was run by the coordinator, not asserted by Codex.

## Data reality (the central validity threat, pre-registered)
- 75 closes on **two active trading days**: Sep 24 (60) and Sep 27 (15).
  Sep 25–26 had zero trades (bot halted).
- One cross-day position (entered Sep 24 22:08, closed Sep 27 14:01, -$5.70);
  3 orphan closes (no matched entry) fail open and are kept.
- Baselines (shared `replay.py` convention, realistic costs):
  IS (Sep 24, n=60): **-$104.36 / WR 30.0% / PF 0.268 / DD $104.36**
  OOS (Sep 27, n=15): **-$20.86 / WR 6.7% / PF 0.211 / DD $20.86**

## Results per gate (edge/trade = (filtered − baseline)/n_retained)

| Gate | IS ret / edge | OOS ret / edge | Retention | OOS 3×: kept vs base | Fate |
|---|---|---|---|---|---|
| R1 trail-12h P&L < 0 | 3 / +34.10 | 0 / +0.00 | 0% | 0.00 vs -20.86 | REJECT degenerate |
| R1 < -5 | 6 / +15.41 | 0 / +0.00 | 0% | 0.00 vs -20.86 | REJECT degenerate |
| R1 < -10 | 6 / +15.41 | 6 / +2.75 | 17.8% | -4.05 vs -20.86 | REJECT <60% |
| R1 < -20 | 14 / +5.44 | 14 / +0.41 | 7.5% | -15.13 vs -20.86 | REJECT <60% |
| R2 trail-24h P&L < 0 | 3 / +34.10 | 0 / +0.00 | 0% | 0.00 vs -20.86 | REJECT degenerate |
| R2 < -10 | 6 / +15.41 | 6 / +2.75 | 17.8% | -4.05 vs -20.86 | REJECT <60% |
| R2 < -20 | 14 / +5.44 | 14 / +0.41 | 7.5% | -15.13 vs -20.86 | REJECT <60% |
| R2 < -40 | 19 / +2.82 | 14 / +0.41 | 14.4% | -15.13 vs -20.86 | REJECT <60% |
| R3 thin-12h < 3 | 58 / -0.01 | 12 / +0.65 | — | -13.01 vs -20.86 | REJECT IS<0 |
| R3 thin-12h < 5 | 57 / +0.17 | 9 / +0.42 | 244%* | -16.81 vs -20.86 | REJECT *tiny IS edge |
| R3 thin-12h < 10 | 51 / +0.52 | 5 / +1.98 | 383%* | -10.59 vs -20.86 | REJECT (see note) |
| R4 cold-restart >12h | 60 / +0.00 | 15 / +0.00 | — | -20.86 vs -20.86 | UNTESTABLE |
| R5 stand down PM | 33 / +0.99 | 0 / +0.00 | 0% | 0.00 vs -20.86 | REJECT degenerate |
| R5 stand down AM | 30 / +2.32 | 15 / +0.00 | 0% | -20.86 vs -20.86 | REJECT no replication |
| R6 drawdown < -50 | 19 / +2.82 | 0 / +0.00 | 0% | 0.00 vs -20.86 | REJECT degenerate |

\* Retention >100% is an artifact of near-zero IS edges ($0.17, $0.52/trade), not strength.

## Why each family fails
- **R1/R2 (trailing-P&L veto):** degenerate. Sep 24 lost -$104, so any "stand down
  after losses" gate vetoes the entire OOS day (ret 0) — a permanent off-switch,
  not a regime discovery. All signal comes from a single day (fails bar #4);
  non-degenerate thresholds retain ≤18% of IS edge (fail bar #2).
- **R3 (thin trailing window):** the only non-degenerate pattern. R3<10 vetoes the
  first ~9 Sep-24 trades (-$26.33, incl. two -90%+ exits) and first ~10 Sep-27
  trades (-$9.89). But: (a) the "regime" is just "bot just (re)started" — a
  warm-up artifact, not a market regime; (b) OOS kept set still loses (-$10.97,
  WR 0%, PF 0.000); (c) it vetoed a +$5.12 OOS winner — no directional
  intelligence; (d) two regime instances total; (e) no clean plateau
  (<3 negative, <5 tiny, <10 moderate). Not shippable. Kept as the sole lead for
  re-testing once ≥10 active sessions exist.
- **R4 (cold restart):** vetoes nothing as specified (gap measured to last close;
  the one 63h halt gap is masked because the first Sep-27 session's closes chain
  through the cross-day close). Conceptually untestable: exactly one halt event
  in the data. Revisit with more halt/restart cycles.
- **R5 (AM/PM halves):** IS suggests PM-only is better (+2.32 vs +0.99 edge),
  but OOS is 100% PM entries and loses — no replication. PM-standdown vetoes all
  of OOS (degenerate). The "effect" is day-composition, not time-of-day.
- **R6 (drawdown):** degenerate permanent-off after a losing day (fails bar #4).

## Honest summary
With two active trading days, no WHEN-to-trade rule separates regimes — every
candidate either turns the bot off after Sep 24's losses or chases a warm-up
artifact on single-digit OOS samples. Both splits lose money under every gate,
including 3× stress. This is a documented negative: do not re-test day/session
regime gates until ≥10 active trading sessions accumulate.

## Not tested (documented, not faked)
- BTC/SOL trend direction: no hermetic historical intraday price source was
  wired in; with 2 active days it could not have validated anyway.
- Signal-rate regimes (signals/hour from bot.log): log lines carry no dates,
  so session mapping is unreliable. Revisit with dated signal logs.
- Day-of-week effects: 2 distinct weekdays in the data (Thu, Sun) — hopeless.

## Verification
- Gate check latency (n=20k, warmed): **p50 0.28µs / p99 0.50µs** — ~100,000×
  inside the 50ms budget (moot: nothing ships).
- Adversarial tests: malformed ts, None/NaN/inf features, orphan closes,
  unknown mints, empty journal, duplicate mints — all fail open, never raise.
- Concurrency: gates are pure functions of the feature dict; no shared mutable
  state; nothing for the bot's scan/manage threads to race on.
- Look-ahead audit: see `LOOKAHEAD_AUDIT.md` — every feature predates its
  entry's decision point by construction (strict `<` on parsed timestamps).
- Full repo suite: see test run below. Lane clean: only `analysis/y1_regime/` +
  `tests/test_y1_regime.py` added; `fomo_trader.py`, config, `runs/` untouched;
  bot not restarted; `dry_run` untouched; no profitability claimed.
