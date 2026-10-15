# FOMO Trader CHANGELOG

## 2026-09-30 — 6h loop Track A: auto-tuner P&L goes chain-aware USD (C1 integrity fix, shipped)
- `analyze.py` computed every close's P&L from `realized_sol` only. All 223
  BSC closes journal `realized_sol=None`, so they became $0 phantoms --
  counted as zero-value LOSSES (`realized <= 0`), diluting win rate and
  profit factor while contributing nothing. The tuner's win/loss stats,
  bucket/source attribution, and every proposal rule ran on a SOL-only view
  of ~19% of the book. This morning's `hunter.min_buy_sell_ratio` 2.0 -> 2.5
  (2026-09-30 11:14 UTC) was auto-applied on that corrupted evidence.
- Fix: per-close P&L is now USD and chain-aware via new
  `close_pnl_usd()` -- journaled `realized_usd` first (present on every BSC
  close; the mislabeled `sol_usd`-holds-BNB/USD field on BSC rows is never
  touched), legacy SOL-era closes priced as realized_sol x stamped/fallback
  SOL price. Enrichment extracted to `enrich_closes()`; closes that cannot
  be priced are EXCLUDED from stats (counted and reported), never $0.
  New fail-closed guard `priced_data_ok()`: the tuner proposes/applies
  nothing when >20% of closes are unpriced. Every tuning rule is unit-free
  (signs, ratios, counts), so no rule threshold or allowlist bound changed
  meaning. Console + dated report now print USD (standing dollars
  convention); tweak/revert rationales quote USD figures.
- Corrected book on the live journal (report-only run): 275 closes, WR
  32.0%, **-$251.22** total, PF 0.38 -- vs the old phantom-diluted SOL-only
  view. Next proposal on the full book would be `hunter.min_m15_gain_pct`
  20 -> 35 (<150% bucket: 122 trades, -$62.69); proposal only, nothing
  applied (the daily 07:14 cron evaluates with the real SOL fallback price).
- The 2.0 -> 2.5 buy/sell tightening from this morning is LEFT in place: it
  is a conservative tightening inside the allowlist, and loosening it needs
  its own validated proposal, not a cleanup revert.
- Tests: 14 new in `tests/test_analyze_usd_2026_09_30.py` (BSC USD pricing,
  unpriced-exclusion, no-$0-phantom regression, 20%-guard boundary, rule-4
  USD rationale, revert rationale, end-to-end `main()`). Full suite:
  **806 passed, 0 failed** (venv pytest, coordinator shell). dry_run=true
  intact. No bot restart: `analyze.py` is a standalone cron script, not
  imported by the running bot (PID 1837, healthy); the fix takes effect at
  the next 07:14 tuner run.
- Deferred, not this change (sibling deep-loop iter-13 hardening
  candidates): `analyze.py --apply` restarts the bot via raw Popen,
  bypassing `run_bot.sh`; C2 stale FX caches; C7 allocator 3x rule.
- Ops note: `/usr/bin/codex` had vanished again; restored via the documented
  symlink shortcut (0.158.0 musl build). Codex CLI itself is still
  quota-exhausted ("try again at Oct 3rd, 2026 5:50 PM") -- one status probe
  only, no quota burned; this run's work was done directly by the
  coordinator.

## 2026-09-30 — 6h loop Track B: LP-lock enforce gate VALIDATED on measure-mode data (verdict only, DO NOT SHIP)
- First real validation of the enforce gate that iter-4's "measure" mode was
  built for: 60 measured BSC entries (all with `lp_lock_verdict`), FIFO-paired
  to closes via the established `analysis/replay.py` machinery, chronological
  30 IS / 30 OOS split, journal paper USD minus modeled costs (0.25% swap fee
  + 0.15% slippage/leg + 0.00002 BNB gas/leg; stress = 3x slippage).
  Enforce semantics exactly as pre-registered: veto `unlocked`, keep
  `burned`/`no_pair`/`unknown` (fail open).
- Verdicts: 57 `unlocked` / 3 `burned` — the gate would veto 95.0% of entries.
  The 3 kept `burned` trades ALL exited via the dump detector (-13% to -23%
  in 60s); kept IS edge -$1.438/trade (n=1), kept OOS edge -$0.190/trade
  (n=2), kept OOS 3x-stress net -$0.46. There is no positive IS edge to
  retain: IS full edge -$0.686/trade.
- Ship gates: FAIL OOS n>=30 (kept OOS n=2), FAIL kept OOS stress net > 0,
  FAIL retention >=60% (13.2%), FAIL kept IS edge positive, FAIL kept IS
  n>=30 (n=1); the IS>=100 significance bar cannot be met on the 60-trade
  measure dataset. **ship_recommend=false; the gate stays in "measure" mode.**
- Measurement note: all 3 `burned` closes avoided the -100% venue-dump wipes
  that hit 7 of the `unlocked` closes — mildly consistent with burn mattering,
  but n=3 proves nothing. `lp_lock_burn_pct` ranged 0.00-100.00 and was
  present on all 60 rows; no measurement bias was found in this review.
- Script: `analysis/lplock_gate_2026_09_30.py` (read-only; re-runnable as the
  measure dataset grows). Full report: `analysis/lplock_gate_2026_09_30_report.txt`.
  No bot code, config, journal, or run file was touched; no restart.
  dry_run=true intact. Note: this run's Codex session was blocked by the
  usage-limit message ("try again at Oct 3rd, 2026 5:50 PM"); the validation
  was run directly by the coordinator instead.

## 2026-09-30 — run_bot.sh self-detaches: worker-timeout cleanup can no longer kill the bot (robustness, shipped)
- `run_bot.sh` now double-forks at launch: the middle process exits immediately
  and the daemon (wrapper + bot) is reparented to init (PPID 1) in its own
  session. Protection lives in the script itself, so EVERY launcher (fix loop,
  watchdog, manual) gets it; callers no longer need their own `setsid/nohup/&`.
- Why: the 10-min fix-loop worker launched via `setsid nohup ./run_bot.sh &`.
  setsid() only moves the wrapper to a new session/process group -- the bot
  stayed a *descendant* of the worker, so when a worker hit its 600s timeout
  the cleanup SIGKILLed the whole descendant tree: runs.log showed
  `"./run_bot.sh: line 42: <pid> Killed"` (exit 137) and deaths.log showed
  "previous pidfile held dead PID ..." every ~10 min (pids 4235, 4923, 1813).
- Also: daemon re-checks pgrep after detaching (two racing launchers can't
  start two bots); launcher waits up to 10s and confirms the new bot pid is
  live before exiting; exit-code logging in deaths.log unchanged.
- Tests: new `tests/test_run_bot_launch.py` (2 passed) -- hermetic A/B proved
  the OLD script's bot dies in a simulated worker tree-kill while the NEW
  script's daemon (ppid 1) and bot both survive; duplicate-start refusal
  verified live (bot pid 2756 untouched, still exactly 1 bot). No trader,
  config, keypair, rug-guard, sizing, kill-switch, or dry_run changes
  (dry_run=true intact).

## 2026-09-29 — session iter4: LP-lock screen gains "measure" mode (instrumentation, shipped)
- `hunter.entry.verify_lp_lock` now takes modes: False/"off" (default,
  screen disabled, zero RPC traffic), "measure" (run the check, journal
  `lp_lock_verdict`/`lp_lock_burn_pct` on the entry record, never skip),
  True/"enforce" (skip the entry on a measured "unlocked").
- Why: enforcing the LP-burn gate without prospective data would be an
  unvalidated strategy change. "measure" accumulates the verdict/burn-pct
  dataset on real would-be entries so the gate can pass the OOS walk-forward
  bar (≥30 OOS trades, ≥60% retention, 3x-fee stress) before any veto goes
  live. Adds a distinct `LP-LOCK MEASURE` log line.
- Tests: 4 new (measure+unlocked journals and enters, measure+burned,
  "off"/"" never call the screen). Full suite: **783 passed, 0 failed**
  (venv pytest, coordinator shell). dry_run untouched (still true).

Every behavior change, with the evidence that motivated it. This is the
paper-validation record: nothing here touches real money (dry_run=true).

## 2026-09-29 — session iter3: BSC client reconnects with backoff (robustness, shipped)
- `fomo_trader.py` `bscswap()`: a failed `BscSwap` construction no longer
  latches BSC off until restart. It now schedules a retry with exponential
  backoff (60s, doubling, 30min cap; reset on success) and logs at most one
  "BSC unavailable" line per retry window.
- Why: a transient RPC outage at startup silently disabled ALL BSC entries
  (where ~all entries happen) with no recovery path.
- Tests: 5 new in `tests/test_bsc_reconnect_2026_09_29.py` (fake clock +
  fake BscSwap: retry scheduled, no re-attempt inside window, success resets
  backoff, doubling + 1800s cap, one log line per window). Full suite:
  **779 passed, 0 failed** (venv pytest, coordinator shell). dry_run
  untouched (still true).

## 2026-09-29 — iteration 2: BSC LP-lock/burn verification screen (mechanism only, default OFF)
- **Mechanism only. Default OFF. No live behavior change.** Enabling
  `hunter.entry.verify_lp_lock` requires prospective validation first; the
  flag is deliberately absent from config.json (absent = False).
- `bsc_swap.py`: `BscSwap.lp_lock_status(token, threshold_pct=50.0)`.
  Read-only, sequential eth_calls on the existing `self.w3`: factory
  `getPair(token, WBNB)` → pair `totalSupply()` → `balanceOf(0x…dEaD)`.
  Returns `no_pair` / `burned` (burn ≥ threshold) / `unlocked` / `unknown`
  (any exception; never raises). No retries, 8s budget across the calls,
  no key (`_ensure_key` never called). `PANCAKE_V2_FACTORY =
  0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73`, verified as the mainnet V2
  router's own `factory()` on two BSC RPCs.
- `fomo_trader.py` `enter_bsc`: after the honeypot check, only when the flag
  is true: `unlocked` → `LP-LOCK SKIP` + `lp_lock_skip` event in
  candidates.jsonl, no entry; `no_pair` / `unknown` fail open (logged as
  `LP-LOCK GAP`). Threshold: `hunter.entry.lp_burn_threshold_pct` (default 50).
- Journal (additive, flag on only): `lp_lock_verdict` and `lp_lock_burn_pct`
  on the entry record (null unless measured). Named `lp_lock_burn_pct`, not
  `lp_burn_pct`, because `lp_burn_pct` already carries scanner LP evidence
  (read by replay) and must not be overwritten.
- Why: ~78% of lifetime BSC losses are one-tick LP-pull wipes; BSC has no
  LP screen today. This adds the measurement so a future gate can be
  validated on journaled verdicts before it ever filters an entry.
- Tests: 45 new in `tests/test_lp_lock_2026_09_29.py` (mocked w3, sockets
  blocked). With the flag off, the screen is never called, no RPC traffic
  is made, and entry/position output is byte-identical to the golden entry.
  With the flag on, `unlocked` skips and every inconclusive result enters.
  Full suite: **774 passed, 0 failed** (venv pytest, two consecutive runs).
  dry_run untouched (still true).

## 2026-09-29 — iteration 1: dump cooldown armed by explicit flag (robustness, shipped)
- `fomo_trader.py`: `close_trade(..., dump_exit=False)`. The exit path sets
  `dump_exit=True` exactly when it takes the venue-dump or dump-detector
  branch and passes it through; `close_trade` arms `dump_cooldown[mint]`
  from that flag instead of substring-matching `reason`.
- Why: arming depended on the free-text reason containing "dump detector" /
  "venue dump"; rewording either string would have silently disabled the
  24h cooldown. Same exits arm, same mints, same 24h window; reason strings
  unchanged. No entry/exit/risk behavior change.
- Tests: 9 new in `tests/test_dump_cooldown_flag_2026_09_29.py` (real
  `_manage_once` path: dump detector and venue dump arm for 24h; trailing,
  hard, stale, take-profit do not; reworded reasons still arm; text alone
  never arms). `test_round4`'s two direct `close_trade` calls now pass
  `dump_exit` explicitly. Full suite: **729 passed, 0 failed** (venv
  pytest, two consecutive runs). dry_run untouched (still true).

## 2026-09-29 — 6h loop Track A: bot calendar day pinned to America/New_York (shipped)
- `fomo_trader.py`: new `DAY_TZ = ZoneInfo("America/New_York")` and
  `_bot_today_str()`; both the state-load fallback and `_roll_day` use it
  instead of host-local `time.strftime("%Y-%m-%d")`. `money.py` never reads
  the day string (it only snapshots `day_start_bankroll_usd` from
  `_roll_day`), so it is unchanged.
- Why: the VM's host TZ flipped across restarts (2026-09-28/29). A flip
  changes the host-local day string, so `_roll_day` silently reset
  `trades_today`, `realized_*`, and the day-start bankroll mid-session,
  disarming the kill switch and daily trade cap. No threshold changed; this
  only stabilizes the day boundary.
- Legacy `state.json` day strings self-heal on the next roll check (at most
  one benign extra roll); no migration.
- Tests: 12 new in `tests/test_day_tz_2026_09_29.py` (TZ=UTC/Kiritimati
  flips, no reset on pure flip, reset on real NY date change, legacy
  self-heal). Three existing fixtures (`test_trackA_robustness`,
  `test_round4`, `test_z4_capture`) now seed `day` via `_bot_today_str()`.
  Full suite: **720 passed, 0 failed** (venv pytest; also 720/720 under
  `TZ=Pacific/Kiritimati`). dry_run untouched (still true).

## 2026-09-29 — 6h loop: journal pool_created_at learns ISO-8601 (instrumentation only, shipped)
- `journal.pool_created_at()` now parses ISO-8601 strings (GeckoTerminal
  `created_at`, e.g. `2026-09-28T16:25:16Z`) into epoch seconds via
  `datetime.fromisoformat`; naive strings (no tz) and unparseable values
  still journal null, never crash. Numeric ms/seconds handling unchanged.
- Why: the deep-research lane found pool_created_at null on 100% of
  journaled candidates, blocking its F2 pool-age gate validation; the
  iter-2 patch was drafted and self-tested (6/6) but never applied.
- Guarantees unchanged: journaling never raises, never mutates inputs,
  never feeds decisions; `STRATEGY_VERSION` stays `"1"` (instrumentation
  only, no decision-behavior change).
- 5 new tests in `tests/test_fixup_2026_09_28.py` (zulu, offset, key
  priority, bad/naive/null safety, version-not-bumped). Full suite:
  **703 passed, 0 failed** (venv pytest, coordinator shell).

## 2026-09-28 — Follow-up fix: research instrumentation + allocator back to shadow + positive-slip veto (shipped)
Three changes from the deep-research pass, all paper-only (dry_run=true
intact). Full suite: 698 passed, 0 failed (venv pytest, coordinator shell).

### A. Research instrumentation (journal.py, new; additive, zero behavior change)
- New `journal.py`: `STRATEGY_VERSION = "1"` (bump only on strategy-behavior
  changes, never for instrumentation). Appends scanner candidate
  observations to `runs/paper-1h/candidates.jsonl` (NEW file; the protected
  `trades.jsonl` is untouched) for EVERY signal, including guardrail rejects
  (verdict `enter` vs `guardrail_skip`).
- Each observation journals: symbol/mint/pool, chain, timestamp,
  price/liquidity/volume, second-observation price via an in-memory
  `ObservationTracker` (repeat sightings report `first_ts` +
  `second_price_usd`), `pairCreatedAt`/pool-age (DexScreener ms normalized to
  seconds; missing -> null, never crashes), source/window, and strategy
  version. Rotation guard: single backup past 50 MB.
- Every entry journal record now carries `strategy_version`,
  `pool_created_at`, and gap-filled signed slip / liquidity / signal gain
  (`finalize_entry_record`; never clobbers existing values).
- Guarantees: journal functions never raise, never mutate inputs, never feed
  back into decisions. 29 new tests in
  `tests/test_fixup_2026_09_28.py`, including a no-mutation parity check.

### B. Allocator reverted to shadow (runs/paper-1h/config.json)
- `allocator.mode`: `live` -> `shadow`. It went live with uncalibrated priors
  against the standing shadow verdict; no pass verdict exists in
  `analysis/allocator_proposal.json`. Shadow still scores every commit and
  journals `allocator_score/take/multiplier/would_be_native` but never skips
  and never changes ticket size (1.0x). Tests assert shadow logs-but-does-not-
  act and that the shipped live config is shadow.

### C. Positive-slip (chase) veto — SHIPPED ON USER ORDER (behavior change)
- New entry rule, both chains: at the commit point, if measured slip > 0
  (fill above the signal print), the entry is vetoed — no position, journaled
  as a `slip_veto` event in `candidates.jsonl` with full would-be details
  (symbol/pool, price, slip, liquidity, signal gain, timestamp) for
  retrospective validation. Config flag `hunter.entry.veto_positive_slip`
  (default true; user-togglable). Strict > 0, no tolerance band.
- Fail-OPEN on missing data: unknown slip logs a `SLIP-VETO GAP` line and
  proceeds. With the veto enabled the pre-existing wipe-drift 0.5x cap never
  fires (veto runs first); it still applies when the veto is toggled off.
- HONEST CAVEAT: this overrides the deep pass's "inconclusive, don't ship
  yet" verdict for this candidate ONLY. The validated walk-forward was PF
  0.254 -> 0.796 at 91% OOS retention on a SMALL sample (n_IS=29 / n_OOS=18).
  The 4h deep-improvement loop keeps validating with fresh data; everything
  else keeps the verdict-only rule.
- Unit tests: slip>0 vetoed + journaled; slip<=0 proceeds; slip unknown
  proceeds + gap logged; flag off never vetoes; precomputed slip (BSC drift
  path) honored.

## 2026-09-28 — Track B Lane 3 — early-entry gain veto (verdict only, unshipped)
- **Experimental, unshipped.** Frozen pre-entry predicate: veto when
  `(signal_gain_pct or 0) >= 100.0`. If shipped, it would be an additional
  filter in the hunter-to-entry handoff; it would not loosen guards or grow
  tickets. The literal predicate keeps a missing gain (`None`), despite the
  conflicting test instruction to veto `None`; the journal has no missing
  gains in these pairs. No bot/config edit or restart was made.
- Read-only FIFO journal replay: 160 pairs, 3 unmatched closes, 0 unmatched
  entries. Close append-order split: 106 IS / 54 OOS; 0 boundary-crossing
  pairs purged. Base/stress add 0.15%/0.45% slippage per leg, respectively,
  while the 0.25% swap fee and fixed costs stay unchanged. Skipped trades
  contribute $0; edge is divided by original cohort size.

  | Gate or metric | IS base | IS stress | OOS base | OOS stress |
  |---|---:|---:|---:|---:|
  | Original / kept | 106 / 40 | 106 / 40 | 54 / 20 | 54 / 20 |
  | Retention | 37.7% | 37.7% | 37.0% | 37.0% |
  | Full net | -$163.77 | -$168.06 | -$56.78 | -$58.19 |
  | Kept net | -$6.42 | -$8.22 | -$16.29 | -$16.75 |
  | Edge per original trade | +$1.4844 | +$1.5079 | +$0.7498 | +$0.7674 |
  | Kept WR / PF | 42.5% / 0.891 | 42.5% / 0.862 | 40.0% / 0.413 | 40.0% / 0.401 |
  | Kept max drawdown | $24.14 | $25.47 | $18.59 | $18.78 |
  | Wipes vetoed / kept | 4 / 4 | 4 / 4 | 7 / 3 | 7 / 3 |
  | Winners vetoed | 7 | 7 | 9 | 8 |

- **Gate table:** OOS edge retention is 50.5% base and 50.9% stress: FAIL
  both >=60% gates. Original counts 106 IS / 54 OOS: PASS both minimums.
  Kept OOS net -$16.29 base / -$16.75 stress: FAIL both positive-net
  gates. Kept OOS count 20: FAIL the 30-retained minimum, so verdict is
  **INCONCLUSIVE** regardless of other gates. Kept OOS WR 40.0%: PASS the
  <=90% red-flag check. Edge decay 49.5% base / 49.1% stress: PASS the
  <=70% red-flag check. The largest 20-close OOS block contributes 46.9%
  of savings (blocks: $10.02, $19.00, $11.47, with 2/3/2 vetoed wipes):
  no single block exceeds 70%. Kept OOS is 20 BSC trades, -$16.29 base;
  there is no later Solana sample, so cross-chain robustness FAILS.
- Verdict: **INCONCLUSIVE; do not ship.** Retained OOS losses and edge
  retention also fail the shipping gates. Full suite: **669 passed, 0 failed**
  (`TMPDIR=/home/hatch/workspace/tmp-codex .venv/bin/python -m pytest tests/ -q`).

## 2026-09-28 — Track B Lane 2 Round 3 — BSC wipe-drift size cap (ships)
- In `enter_bsc()`, after allocation and its skip check, positive
  signal-to-commit price drift caps the paper ticket at the allocator's 0.5x
  floor (`buy_bnb <= buy_bnb_before`). The pure `wipe_drift_cap_native()`
  helper uses the already available entry price, signal USD price, and cached
  BNB/USD rate. Capped entries log the decision and journal `wipe_drift_cap`
  plus `wipe_drift_slip`; Solana entry and existing risk controls are unchanged.
- Validation: 107 IS pairs, edge +$0.2164/trade base / +$0.2197 at 3x
  slippage stress; 52 OOS pairs, edge +$0.2405 / +$0.2425, with 111.1% base
  / 110.4% stress retention. OOS win rate 30.8%, PF 0.22, max drawdown
  $56.35. The cap affected 20 IS and 24 OOS entries. OOS savings were
  $12.50, 80.7% from 9 capped wipe trades; winner upside given up was $1.06.
- Caveats: the bot remains unprofitable overall (OOS net -$56.35 after the
  cap). The observed wipe leak is BSC-only; there is no cross-chain claim.
  Journal slip uses a post-commit rate, while the live rule uses the cached
  pre-write rate; sign agreement is expected. Paper mode stays enabled.
- Full suite: **664 passed, 0 failed**
  (`TMPDIR=/home/hatch/workspace/tmp-codex .venv/bin/python -m pytest tests/ -q`).

## 2026-09-28 — Track B Round 5 and dump-shadow recheck (verdict only)
- Read-only journal snapshot: 157 closes, 154 entries, 3 unmatched closes; FIFO
  `(chain, mint)` pairing gives 87 enriched closes (58 IS / 29 OOS). The
  pre-registered 30-close trigger is met, but the 100 IS / 30 OOS ship gate
  is not. All 87 are BSC. Signal USD price, m15 buys/sells/volume, mcap,
  liquidity, entry latency and slip are present on all 87; holder top-1/top-5
  and LP fields are present on none. Holder veto remains `not_runnable`.
- Costed baseline: IS -$61.43 on 58 closes (27.6% wins, PF 0.33, max drawdown
  $61.63); OOS -$36.97 on 29 (20.7% wins, PF 0.07, max drawdown $37.60).
  The frozen chase veto keeps 42 IS / 22 OOS, with -$22.88 / -$21.59 net;
  OOS edge retention is 79.8%, but 3x-cost OOS is -$24.58. Commit-liquidity
  keeps 50 / 22, with -$55.41 / -$24.63 net; retention is 410.1%, but
  3x-cost OOS is -$27.51. Latency veto removes no trades and has zero edge.
  `KILL_VARIANT` / `STOP_ALL` have insufficient OOS history (29 < 30), and
  all-chain BSC concentration remains a red flag. Verdict: provisional,
  `ship_recommend=false`; no bot rule is wired in.
- Since the first shadow-marked close (2026-09-27 20:20:05 journal time), 55
  closes include 42 dump, 9 venue-dump and 4 other exits. 54 carry a
  `shadow_dump_first` marker: all 51 dump/venue closes and 3 other closes.
  Marker source is quote for 52 and venue m5 for 2. In 38/42 dump and 6/9
  venue closes, the recorded shadow price equals the exit mark, giving no
  observed earlier price; the 3 other marked closes include 2 ordinary
  trailing stops and 1 near-total trailing exit. Marks are not executable
  fills. Verdict: **stay shadow**; no change to sell behavior.
- Changed only this validation record. `dry_run=true` in the paper config and
  code default remains true; no bot restart or run-file modification. Full
  suite: **654 passed, 0 failed**
  (`TMPDIR=/home/hatch/workspace/tmp-codex .venv/bin/python -m pytest tests/ -q`).

## 2026-09-28 — Reject malformed BSC router quotes
- BSC buy and sell quotes now require a positive input and an exact two-amount
  router response with positive integer amounts whose first amount matches
  the request. A malformed or zero-output quote raises instead of becoming
  a paper fill; the existing entry and exit callers handle quote failures,
  and the honeypot check fails closed.
- Evidence: `quote_buy` and `quote_sell` previously accepted any response's
  last element, including zero or a one-element response. Added 14 hermetic
  cases in `tests/test_bsc_quote_validation.py` covering both directions,
  malformed responses, input validation, and honeypot failure.
- Full suite: **654 passed, 0 failed**
  (`.venv/bin/python -m pytest tests/ -q`). No bot restart or run-file edit;
  paper mode and risk settings are unchanged.

## 2026-09-28 — Auto-apply validated allocator weights (closes the loop)
- Added `analysis/apply_validated_weights.py`, run by the 2h cron after the
  deterministic and LLM analyst passes. On a genuine "pass" verdict (all 7
  gate checks green, OOS >= 30 closes, retention >= 60%), it copies ONLY the
  validated `weights` from `analysis/allocator_proposal.json` into the live
  `runs/paper-1h/config.json` allocator section — mode, take_threshold,
  caps, kill switches and risk settings are never touched.
- Safety: refuses unless `dry_run == true`; backs up config before writing;
  applies each distinct proposal at most once (hash marker); graceful
  SIGTERM restart with position-snapshot verification; on restart failure
  or position mismatch it restores the backup and logs a loud ALERT.
- A "fail"/"insufficient_data" verdict still changes nothing. Paper-only.
- 16 hermetic tests in `tests/test_apply_validated_weights.py` (all process
  interaction mocked; the real bot is never touched in tests).

## 2026-09-28 — Offline LLM allocator analyst pass
- Added `analysis/llm_analyst.py` after the deterministic cron pass. It uses
  its own 25-new-priced-close watermark and sends compact journal evidence to
  Codex via stdin. The bot entry and exit paths remain untouched.
- Proposed weights use the existing replay costs, IS/OOS edge and retention
  checks, and win-rate/chain/time concentration rejects. Only a passing gate
  replaces `analysis/allocator_proposal.json`; failures are journaled in an
  LLM report. Config changes remain manual.
- Codex errors, timeouts, quota exhaustion, and invalid JSON exit 0 without
  replacing the deterministic proposal or updating the LLM watermark.

## 2026-09-28 — Paper allocator ticket aggression
- Raised `MULT_MAX` from 2.0x to 3.0x; `MULT_MIN` remains 0.5x. The
  deterministic, monotone piecewise-linear curve now passes through score
  0 = 0.5x, 0.5 = 1.0x, 0.7 = 2.5x, and 1 = 3.0x. A strong 0.7 score reaches
  the upper range while a typical 0.5 score stays at the base ticket.
- The unconditional `money.py` risk ceiling still limits final size to
  `min(configured ticket × multiplier, ceiling)`. At about $918 bankroll,
  1.0% risk per trade and a 35% hard stop, the ceiling is approximately
  0.01 × $918 / 0.35 = $26.2. A 3.0x multiplier on the $7 base ticket
  therefore yields `min($21, $26.2) = $21`. The ceiling shrinks automatically
  as bankroll falls; drawdown scaling can reduce the multiplier further.
- Default weights remain uncalibrated priors. The previous offline validation
  failed, and this sizing change makes no profitability or signal-edge claim.

## 2026-09-28 — Dump latency candidate, shadow only
- Added a pure local shadow detector (`shadow_dump.py`) using the manager's
  existing per-mint quote ring and only the DexScreener m5 value already read
  by the live tripwire. Candidate: quote drop >=8% within 30s; venue m5 <=-20%
  on the existing 30s venue-check cadence. On paper ticks it logs the first
  would-have-fired timestamp, price, source and params, and adds these to the
  eventual close record. It never sells. `exit.shadow_dump_detector` defaults
  on for dry-run measurement and cannot run with `dry_run=false`; the example
  config documents the knobs. The live 12%/60s and -30%/30s thresholds, 2s
  poll, TP ladder, trailing stop and hard stop are unchanged. No extra venue
  request was added.
- Read-only replay (`analysis/dump_latency_replay.py`) of all 64 dump/venue
  closes: 12 near-total gap exits had $49.33 aggregate realized loss; 52
  other exits had $52.00 aggregate realized loss (winning closes excluded from
  the loss sums). DRA was a $7.00 loss, with captured quote marks jumping
  directly from about $0.0005525 to $0.000000001642. For near-total gaps,
  candidate savings are set to $0; older gaps lack tick histories and this
  instant/gradual split is provisional.
- Illustrative gradual savings: $48.71, comprising $0.26 from observed quote
  crossings and $48.44 modeled for missing paths (rounding). The model uses a
  linear 30s peak-to-exit descent plus one 2s poll after the 8% crossing;
  prices are marks, not executable fills. Among 29 historical TP/trailing
  closes, only one has a captured quote path; it showed zero early cuts and
  $0 observed false-positive cost. The illustrative net is +$48.71 before
  unknown false positives, impact and unavailable venue m5 history. Real
  dumps can reverse or gap in one tick, so the model is not a forecast.
- Recommendation: **stay shadow** at 8%/30s and m5 -20%/existing 30s check.
  The observed-path benefit is just $0.26 and false-positive coverage is 1/29.
  A one-tick -100% liquidity pull cannot be saved by a faster threshold.
- Validation: `.venv/bin/python -m pytest -q` finished with 614 passed,
  0 failed. Pure detector tests cover threshold/window, malformed history,
  no I/O and timing; manage tests cover shadow-only observation, dry-run
  gating and identical live sell decisions with shadow on/off.

## 2026-09-27 — Round 5 enriched-entry pre-entry screen validation (awaiting data)
- Diagnostic: holder and LP fields are genuinely unavailable on the current enriched entries, not lost by journal plumbing. All 32 enriched entries are BSC; BSC has no holder endpoint in this path, source LP fields are absent, and Solana's existing guard passes observed holder values through or skips unverifiable candidates. No bot code changed; holder screens are `not_runnable` on this journal. See `analysis/round5/DIAGNOSTIC.md`.
- Added stdlib-only `analysis/round5/round5.py` with FIFO `(chain, mint)` joining, replay cost reuse, chronological IS/OOS metrics, fixed candidate gates, and KILL_VARIANT/STOP_ALL mechanics. `ROUND5_PREREGISTRATION.md` froze the four candidate vetoes at 2026-09-28 00:06:52 UTC before evaluation. The report is descriptive only: 29 paired enriched closes, below the 30-close trigger; **verdict: awaiting data, ship_recommend=false**. IS 19 closes costed −$13.84; OOS 10 costed −$3.81. The eight ≥10% slip entries contributed −$17.41, an unvalidated BSC-only concentration, not a filter result.
- Added ten hermetic Round 5 tests. Refreshed stale Z3 golden test expectations for existing bankroll fields and BSC paper token rounding; no production behavior changed. Final full suite: **608 passed, 0 failed** (`.venv/bin/python -m pytest tests/ -q`). `dry_run=true` remains intact. The bot was not restarted; no `runs/paper-1h/` files were modified.

## 2026-09-27 20:11 EDT - Balance-aware allocator sizing
- Added `balance_factor`: the in-memory bankroll's drawdown from its equity
  peak linearly reduces sizing from 1.0x to 0.5x at the configured maximum
  drawdown; missing or invalid balance data is neutral.
- Added `score_signal_with_balance`, which preserves the existing take and
  score, applies the balance factor to the score multiplier, and clamps the
  result to 0.5x-2.0x. Entry journals now record
  `allocator_balance_factor` in shadow and live modes.
- The money.py risk ceiling, rug guard, kill switches, trade caps, cooldowns,
  and shadow/live/off behavior are unchanged.

## 2026-09-28 - Entry allocator (shadow by default) + offline analyst loop
- New `allocator.py`: a deterministic, pure-local scorer that runs at entry
  commit (after `_entry_commit_ok()`, next to `_bankroll_ticket()`) in both
  `enter()` (SOL) and `enter_bsc()`. No network, no AI/LLM, no subprocess,
  no randomness; ~9 us/call. Score = logistic(intercept + sum of weights x
  features normalized to [-1, 1]), from signal gain, liquidity, 15m
  buys/sells/ratio/volume, mcap, Solana top-1/top-5 holder concentration,
  signal-to-fill slippage (recomputed from the in-lock native/USD rate, since
  the journal's `commit_price_usd` uses a post-commit fetch), entry latency and
  chain. Null features are neutral. `take = score >= take_threshold` (0.35);
  multiplier is monotone in score, 0.5x at 0, 1.0x at 0.5, 2.0x at 1. Any
  allocator error falls back to take at 1.0x.
- Sizing in live mode: `min(configured ticket x multiplier, money.py risk
  ceiling)`. The ceiling always wins; with no USD rate the allocator can
  shrink but never grow the ticket. `ALLOCATOR SKIP <name> score=<s>` opens
  nothing, releases `pending_entries`, and does not count as a trade. The rug
  guard, trade caps, kill switches, drawdown brake and cooldowns are
  unchanged and are evaluated before the allocator.
- Config: new `allocator` section (`mode`, `take_threshold`, `weights`) in
  `runs/paper-1h/config.json` and `config.example.json`, **mode = shadow**.
  Shadow trades exactly as before (1.0x, never skips) and journals
  `allocator_mode/score/take/multiplier/base_native/would_be_native` on entry
  records. Live adds `allocator_applied_native`. A missing section = off,
  with byte-identical legacy journals. Default weights are mild priors: on
  the 98 journaled entries they take 100% at 0.95x-1.15x (median 1.00x).
- New `analysis/allocator_analyst.py` (cron, `0 */2 * * *`, see
  `analysis/allocator_cron.md`). It reads the journal read-only, joins entries
  to closes, attributes P&L by score/gain/liquidity/ratio quintiles and
  chain, and fits a recency-weighted (14-day half-life, full journal) L2
  logistic model. It then runs the gate: OOS = most recent max(30%, 30)
  closes (else `insufficient_data`); IS edge > 0; OOS edge > 0 with >= 60%
  retention (decay <= 70%); IS taken win rate <= 90%; no chain or time
  tercile holding > 80% of the edge. Replay costs: 100 + 300 bps plus
  SOL $0.60 / BSC $0.20 per trade, with linear ticket scaling. It no-ops
  unless 25+ new closes have landed since `analysis/allocator_watermark.json`,
  and it never writes the config.
- First run on the live journal (75 priced closes, 45 IS / 30 OOS): verdict
  **fail**. The edge passes the IS/OOS checks (+$0.53 vs +$0.59 per trade,
  retention 1.11) but 98.5% of it comes from shrinking or skipping BSC, so it
  is a chain filter, not a feature edge. Recency check: the recent 30% is
  less negative than the older journal (-$0.44 vs -$0.95 per trade, z = 0.76,
  within noise). The report flags this as recency-concentrated and does not
  overweight it. Allocator stays in shadow.
- Manual shadow -> live flip (only on verdict `pass`): copy `weights` from
  `analysis/allocator_proposal.json` into `allocator.weights`, set
  `allocator.mode` to `"live"`, restart the bot. `dry_run` stays true.
- Tests: new `tests/test_allocator.py` (76 tests: bounds, determinism,
  threshold boundary, error fallback, ceiling precedence, shadow vs live on
  both chains, skip bookkeeping, analyst pass/fail/insufficient_data,
  recency weights, watermark no-op). Exit path, manage loop and guardrails
  untouched; bot not restarted.

## 2026-09-27 17:43 EDT - E3 exit-architecture experiment pre-registered (awaiting data)
- Pre-registered E3 in analysis/z5_exit_arch/E3_PREREGISTRATION.md (frozen
  2026-09-27). Verdict: awaiting data. Z4 quote-path capture is still off,
  so no qualifying path exists yet.
- Built the offline harness analysis/z5_exit_arch/e3.py (stdlib only, no bot
  import). It has a fail-closed Z4 loader, a tick-by-tick simulator, E1-style
  costs ($10 notional, 30 bps venue, 100/500 bps entry/exit slippage,
  2,000,000 lamports per transaction), a seeded 2/3-1/3 chronological split,
  KILL_VARIANT/STOP_ALL, 60% OOS retention and a 3x cost stress. Proven on
  synthetic paths only: tests/test_z5_exit_arch.py (11 tests) passes, and
  the full suite stays green.
- Variants vs the live ladder, all sharing the -40% hard stop and 12%/60s dump
  detector:
  (a) ratchet: arm at +20%; sell 50% of the remainder on each 10% pullback from
      the running anchor (twice); 25% deep trail on the rest.
  (b) time box: exit all at 20 minutes, or earlier on a 20% drop below the
      mark-peak.
  (c) vol-scaled: 5-minute realized vol per sqrt(minute), cutoff 0.05. High
      vol: 20% rungs at +10/+20/+35% with a 20% trail. Low vol: 30% rungs at
      +30/+60% with a 30% trail.
- Data trigger: at least 30 closed trades, each with a Z4 path of at least 20
  non-null marks. The first run will be runnable but NOT decision-capable
  until the frozen variant has 30 or more OOS closes.
- Marks are not fills: any positive result is a mark-implied bound that
  still needs a fill-feasibility study. No profitability claim. No bot code
  or config changed; nothing restarted.
## 2026-09-27 — Track A robustness audit and conservative hardening
- Added `analysis/trackA_robustness/AUDIT.md` and `STAFF_REVIEW.md`. Source review and a two-thread cap test verify the commit check and appends are atomic under the state lock; fallback engages only after zero GT pages; Solana and BSC entry paths retain their rug/honeypot screens regardless of signal source. The stale-signal network fetch still holds the state lock; production latency impact is unverified and no sequencing change was made.
- Reproduced a lost hourly append: unlocked prune iterated an old list while another thread appended under the lock, then rebound the list to zero entries. A direct `save` also completed while another thread held the state lock. `Trader.lock` is now an `RLock`; prune and day rollover take it, and `save` takes it around temp write and replace. Both race tests pass after the patch; golden tests preserve normal prune/roll values and a two-thread one-slot cap accepts exactly one entry.
- Failing-first tests demonstrated GT NaN gain passing, NaN liquidity crashing, DS NaN gain passing, GT `data: null` aborting the scan, malformed main/early pool fields or a non-object item aborting other pools, wrong-schema valid JSON breaking startup/guardrails, and one corrupt replay line aborting analysis. The corresponding fixes reject non-finite numeric input, treat null page data as empty, skip malformed pools, log and use default state on required-key schema failure, and count skipped malformed journal lines. Normal GT/DS signal values, finite conversions, and replay pairing have golden tests.
- Six `Trader.__new__` lock assignments in four existing test files now use `RLock` like production; the first full-suite run hung on a test's old plain-lock fixture after introducing reentrant helpers. Final suite: **436 passed** (`TMPDIR=/home/hatch/workspace/tmp-codex .venv/bin/python -m pytest tests/ -q`), comprising 416 prior tests plus 20 Track A tests. No bot restart or run-file mutation.

## 2026-09-27 — Track Z4: optional quote-path and failed-quote capture
- Added opt-in `research.capture_quote_path` and `research.capture_failed_quotes` hooks in `fomo_trader.py`; both default off and the existing `research_settings` 3-tuple remains unchanged. No config, trade journal, state, decision rule, running process, or live run file was changed.
- Quote paths append one row per manage tick under `quote_paths/<mint>.jsonl`, using the already fetched mark and only a fresh cached native/USD conversion; unavailable USD conversion and liquidity are explicit nulls. Each mint stops at 5,000 rows or 1 MiB. Failed entry quotes, dust quotes, honeypot screens, and commit cap/kill rejections append known economics to a locked `failed_quotes.jsonl`. General Solana RPC fallback is excluded because its retry client lacks trading context; see `analysis/z4_capture/README.md`.
- Added `analysis/z4_capture/README.md`, `STAFF_REVIEW.md`, and 26 hermetic tests. Off-state Solana/BSC entry, position, and close fixtures remain byte-identical. Z4 tests: 26 passed; full suite: 416 passed (`PYTHONPATH=. .venv/bin/pytest tests/ -q`). The bare `pytest tests/ -q` command fails during collection because two existing test modules import project packages before adding the project root to Python's path.
- Capture helper latency over 10,000 calls each: quote off p50/p99 0.000300/0.000391 ms, failed off 0.000291/0.000371 ms, quote on 0.042845/1.068002 ms, failed on 0.032188/1.027200 ms; all p99s below the 50 ms budget. At a 2-second poll, one full 5,000-row path spans 2.78 hours; three continuously occupied slots produce about 26 such paths/day, conservatively at most 26 MiB/day at the 1 MiB per-file cap. Failed-quote storage remains unbounded.

## 2026-09-27 — Track Z3: $7 DLMM fee-ceiling test (KILLED) + observation-mode instrumentation (flags off)
- Added `analysis/z3_lp_ceiling/` (`z3_lp_ceiling.py`, report, `STAFF_REVIEW.md`, `LOOKAHEAD_AUDIT.md`) and `tests/test_z3_lp_ceiling.py`. This is a synthetic upper bound: hermetic, no journal data, every input a labeled assumption biased toward the LP. Model output: `VERDICT: KILLED - $7 ticket, 30 pool-days: upper-bound net $0.00 (infeasible: required refundable deposits $7.17 exceed the $7 budget); the ceiling clears $0 only at tickets >= $7.18`.
- **Verdict: KILLED for the $7 ticket.** The refundable rent deposits a DLMM position and its token accounts need total 0.0624 SOL, which is $7.17 at SOL $115 (below every journal SOL price). That exceeds the $7 budget, so the 30-pool-day upper-bound net is $0.00, even with 0.7%/pool-day compounded fees, zero IL, perfect placement, no failed transactions and zero priority fees. It is a knife-edge, disclosed in the report. At SOL below $112.20, or without the wSOL account, the ceiling is at most +$0.18 over 30 days, and one 0.01 SOL priority fee erases that. A hypothetical small (1-bin) position account would make it +$1.12. Current Meteora position rent is unverified (a zero-cost check, STAFF_REVIEW objection 1), so the kill is conditional on it. The $25 and $100 ceilings clear $0 ($3.32 and $17.28), but raising the ticket is out of scope. A conditional 30d IS / 60d OOS follow-up is pre-registered in the report and was not run.
- Added observation mode, strictly additive and off by default:
  - A new top-level `research` config section (`enabled`, `tag_trades`, `question_id`, `dashboard`, `field_audit`). A missing section is all-off. Malformed values fail safe to off with one log line per cause and never raise. No config file was changed; the flags ship off.
  - In `fomo_trader.py`: `research_settings`, `registered_question_ids`, `maybe_tag_research` and `maybe_research_dashboard`. `enter`/`enter_bsc` tag the entry enrichment, so both the journal record and the position carry `research_question`, and `close_trade` copies it from the position. The dashboard runs after a close only when `research.dashboard` is on.
  - New files: `research_questions.json` (Q1–Q8, open); `obs_dashboard.py`, which scores the report's 8 consolidated kill criteria into `analysis/obs/kill_dashboard.json` as ok/TRIPPED plus `evaluable`; `analysis/obs/field_usage_audit.py` (offline; flags fields unused for 14+ days and deletes nothing); `analysis/obs/bench_research_hooks.py`. On the current journal the dashboard reports: `program_killed=True tripped=[6] not_evaluable=[1, 2, 3, 4, 5, 7, 8]`.
- Behavior-neutral proof: with `research` absent, null, disabled or malformed (13 variants × Solana/BSC), entry records, position dicts and close records are byte-identical to golden records captured from the pre-change code, and the dashboard is never called. Tag helper p50/p99 with flags on: 0.003575/0.004046 ms over 10,000 calls (budget 50 ms). Dashboard update on the live journal: p50/p99 3.129174/5.190015 ms, and it only runs when its flag is on.
- No journal, risk guard, position sizing or kill switch changed; `dry_run=true` intact; no restart. New Z3 tests: 113 passed, 0 failed. Full hermetic suite: 390 passed, 0 failed (`.venv/bin/python -m pytest tests/ -q`). Built by Cursor session B, which could not write to the repo or run Python, so the files were staged in /tmp and applied with `/tmp/z3_apply.py`.

## 2026-09-27 — Track Y3 fade-the-signal diagnostic (no change recommended)
- Added `analysis/y3_fade/fade.py`, 6 hermetic tests, `y3_fade.json`, `y3_fade_report.md`, `LOOKAHEAD_AUDIT.md`, `STAFF_REVIEW.md`. 74 closed trades.
- Diagnostic REJECTS the premise: median peak/entry 1.260 (+26% post-entry run), 74% of trades peak ≥10% above entry, median give-back 29%. The signal is not backwards — the exit ladder is the bleed (first +100% TP rung reached by ~11% of trades; 29 losers ran +27% then collapsed to 0.2× peak).
- Pullback-entry variant (enter at entry×(1−X), X=10/15/20/30%) structurally killed: fills are provable from the journal only when exit ≤ entry×(1−X), so every provable fill is a loser — PF 0.000, WR 0.00 at every X, IS/OOS, under 3× slippage. All 27 winners have unprovable fills; even the impossible best case (every winner dips exactly X% then rips) stays negative OOS.
- **ship_recommend: false.** No bot, config, journal, or risk guard changed; no restart. Full hermetic suite: 238 passed, 0 failed. Implemented directly in coordinator shell (Codex CLI read-only-sandbox failure mode; X2 precedent).
- Ops note: host rebooted ~20:16 UTC during this track; the pre-reboot bot process is gone and was not restarted per lane restriction — relaunch via run_bot.sh.

## 2026-09-27 — Track B offline exit sweep (no exit change recommended)
- Added `analysis/exit_sweep.py`, 9 hermetic tests, and `analysis/exit_sweep_report.md`. The read-only journal snapshot had 73 closes; used first 45 by `close_ts` as IS and final 28 as OOS (round 2's 45/22 split had 67 closes).
- Swept 467 sets across one- and two-rung TP thresholds/fractions, trailing and hard stops, stale timeout and gain threshold, dump threshold and window, and post-TP trail. The strongest IS region was TP `[[30,100]]` plus 10% trail, with nearby ladders and 15–20% trails retaining IS edge; OOS trail sensitivity was absent. No set passed all gates.
- Current exit base IS: 45 / −$75.63 / 35.6% wins / PF 0.282 / $75.63 max drawdown; OOS: 28 / −$46.24 / 10.7% / PF 0.227 / $52.27. Current exit 3× slippage: −$77.75 IS and −$47.27 OOS.
- Exploratory top point base IS: 45 / −$38.83 / 48.9% / PF 0.385 / $42.14 drawdown; OOS: 28 / −$26.47 / 35.7% / PF 0.374 / $30.55. At 3× slippage: IS −$41.17, PF 0.359, drawdown $44.04; OOS −$27.62, PF 0.357, drawdown $31.60. It retained 86.4% of IS per-trade edge OOS but failed the required positive OOS stress net. Counterfactual TP fills and stop timing remain uncertain from entry/peak/exit alone.
- **Exact recommended JSON Patch for `exit`: `[]`**. No bot, paper config, journal, or risk guard was changed; no restart. Full hermetic suite: 100 passed, 0 failed (`.venv/bin/python -m pytest tests/ -q`). Paper-only `dry_run=true` remains required.

## 2026-09-27 — Track C signal quality
- Offline paper snapshot: 628 parsed FOMO/PRE-PUMP signals, 70 entries and
  73 closes (3 closes without entry evidence). Join uses normalized names,
  journal-anchored log dates, same chain, and a 30-minute signal-to-entry
  window. It assigns 17 entered winners, 53 entered losers, 0 entered without
  a close, 476 never-entered, and 82 best-effort guard-skip observations.
  Repeated signals are individual observations; the latest eligible signal
  is paired to each entry. The full method, caveats, and all selection and
  outcome buckets are in `analysis/signal_quality_report.md`.
- Strong descriptive splits: logged gain <100% had 30 entries, 43.3% wins,
  PF 1.055, +$1.88 costed net; gain >=400% had 11, 9.1%, PF 0.092,
  -$45.85. Ratio >=4 had 11 entries, zero winners, -$26.46; liquidity
  >=$50k had 23, one winner, -$62.70. These overlap and are not validated
  entry edges. UTC hours 12–17 and 18–23 had similar win rates (24.1% and
  22.5%) and both lost money (-$71.91 and -$50.12); 06–11 had one entry.
  Launcher zone switches in `deaths.log` were matched to bot-start lines
  before converting hours and entry timestamps to UTC. Chain results are
  confounded with the later BSC-heavy period.
  Weak or untestable features: ratio <2 and 2–4 had nearly identical win
  rates (29.0% and 28.6%); only 3/70 entries (4.3%) recorded each of
  m15 buy/sell counts, volume, market cap, slip, and latency; no matched
  entry came from DexScreener or a pre-pump line. The prior gain and
  liquidity caps remain rejected.
- Added a pure-stdlib, fail-open `analysis/signal_filter.py` and parameter
  file. The one-threshold offline candidate vetoes buys/sells ratio >=4;
  the ratio >=3 and >=4 family was compared on the first 47 entries only,
  ordered by UTC-normalized entry time. The >=4 threshold had the better IS per-trade
  edge (+$0.024 versus -$0.001). IS unfiltered: **47 / -$69.36 / 34.0% /
  PF 0.348 / $76.24 max drawdown**; filtered: **41 / -$59.53 / 39.0% /
  PF 0.383 / $66.75**. OOS unfiltered: **23 / -$50.46 / 4.3% /
  PF 0.092 / $50.46**; filtered: **18 / -$33.83 / 5.6% / PF 0.132 /
  $33.83**. OOS per-trade edge was +$0.314, above 60% of IS edge,
  but the retained OOS set had only one winner. Under 3x estimated
  slippage, IS unfiltered/filtered were -$71.53/-$61.45 and OOS
  unfiltered/filtered were -$51.26/-$34.49 (filtered OOS PF 0.128,
  $34.49 max drawdown). **Reject shipping:** the retained positive
  outcome is concentrated in one winner and filtered OOS is negative
  under stress. `ship_recommend=false` makes `approve()` pass through.
  Nothing is connected to the bot. The snapshot does not establish future
  returns or executable fills.
- Added hermetic join, bucket, veto, fallback, cached-load, and <50ms
  inference tests. Full suite: **100 passed, 0 failed** with
  `.venv/bin/python -m pytest tests/ -q`. The bot was not restarted;
  `fomo_trader.py` and `runs/paper-1h/` were not edited.

## 2026-09-27 — round 4 loss forensics and dump re-entry safety
- **Snapshot:** first 69 journal closes, ending with BT at the displayed
  `2026-09-27 15:06:33` (EDT). The bot continued appending later closes while
  this review ran; they are outside this fixed snapshot. FIFO mint pairing and
  the existing `analysis/replay.py` cost model were reused. Local journal
  timestamps mix EDT and UTC; `deaths.log` shows launcher TZ changes. Replay
  now uses append order for chronological IS/OOS, since lexically sorting the
  local clock strings misorders Bukangi and SF #2. For duration math, the
  18:18:22 UTC SF #2 entry is 14:18:22 EDT, and its 14:23:59 EDT close is
  about 5m37s later. Its prior SF close was 14:05:13 EDT, so re-entry came
  about 13m09s later, not four hours. ARCHIBROWN's 2026-09-24 22:08:01 UTC
  entry to 2026-09-27 14:01:35 EDT close spans about 2d19h54m.
- The nine specified closes total **-$13.11** from unrounded journal native
  returns converted at recorded USD marks; the row-rounded values sum to
  **-$13.12**. The replay's additional fee/slippage estimates make those nine
  **-$14.31**. Causes below use row-rounded *journal* USD, so the operator's
  loss taxonomy is comparable:

  | Close | Signal gain | Entry to peak | Exit versus entry | Journal USD | Cause |
  | --- | ---: | ---: | ---: | ---: | --- |
  | ARCHIBROWN / SOL | 18.9% | +41.1% | -70.9% | -$5.18 | Legacy frozen bleed; 79.4% below peak |
  | 本命年十 / BSC | 257.4% | +8.6% | -22.1% | -$0.44 | Bought near top |
  | SF🐲 #1 / BSC | 103.0% | +36.0% | +5.0% | -$0.09 | Costs ate gross winner |
  | WOJ / BSC | 183.0% | +68.0% | +9.4% | -$0.12 | Costs ate gross winner |
  | 😂SC / BSC | 220.8% | +16.8% | -13.8% | -$0.89 | Bought near top |
  | Bukangi / BSC | 25.1% | +140.0% | +108.7% | +$5.24 | Winner, +50% TP rung fired |
  | SF🐲 #2 / BSC | 199.5% | +2.5% | ~-100% | -$6.99 | Venue dump to near zero |
  | STO / BSC | 224.4% | +9.4% | -27.0% | -$1.77 | Bought near top |
  | BT / BSC | 290.6% | +2.3% | -28.9% | -$2.88 | Bought near top |

- Cause totals from those rounded rows: bought near top **-$5.98**; venue rug
  **-$6.99**; legacy bleed **-$5.18**; two gross winners consumed by costs
  **-$0.21**; Bukangi **+$5.24**. BSC's 17 closes lose **-$40.51 costed**
  (-$2.38/trade), versus SOL's 52 at **-$78.16** (-$1.50/trade).
  The supplied claim of only two wins across 69 closes does not match the
  journal: 23 raw wins, 19 after estimated costs. Nor is every coarse bucket
  negative: the `<100%` signal-gain bucket is +$1.88 over 30 closes. These
  corrections do not establish a repeatable entry edge.
- **Validation split:** first 46 closes IS, last 23 OOS in journal close
  order. Metrics are `n / costed net USD / net win rate / profit factor /
  max USD drawdown`. Baseline IS **46 / -$71.88 / 37.0% / 0.318 / $75.63**;
  OOS **23 / -$46.80 / 8.7% / 0.173 / $49.07**. With 3x all modeled
  round-trip costs (swap fee, slippage and fixed transaction allowance),
  baseline net is -$120.31 IS and -$55.69 OOS. Filter edge means
  kept-set net USD/trade minus the same split's unfiltered net USD/trade;
  missing entry evidence passes through. Entry-filter replay does not model
  freed capacity or later opportunities.
- **Fix 1, shipped safety guard:** persist `dump_cooldown[mint]` at close for
  dump-detector or venue-dump exits. Both scan guardrails and commit guard
  reject that mint for 24 hours with `DUMP-COOLDOWN SKIP <name>`. Other close
  reasons do not arm it, and the existing 10-minute entry cooldown remains.
  Historical support is exactly one repeat, SF #2: its -$6.99 journal loss
  would have been vetoed had this guard existed. This is a safety tightening,
  not a statistically validated P&L edge. Logic, expiry, non-dump behavior,
  commit check and persisted state have hermetic tests.
- **Fix 2, tighter dump detector: rejected.** The counterfactual assumes a
  monotonic fall from each recorded *lifetime* peak through an exact
  `peak*(1-threshold)` fill on the remaining bag; it cannot observe the
  60-second rolling peak, gap, quote depth, liquidity or execution price.
  Current configured trigger remains 12%/60s. At 8%: IS **46 / -$56.70 /
  41.3% / 0.415 / $72.72**, OOS **23 / +$15.29 / 52.2% / 2.231 / $5.70**;
  per-trade edges +$0.330 / +$2.700 (819% retention); 3x-cost net
  -$105.28 / +$5.76. At 10%: IS **46 / -$58.52 / 41.3% / 0.399 /
  $72.86**, OOS **23 / +$12.27 / 47.8% / 1.895 / $5.70**; edges
  +$0.290 / +$2.568 (885%); 3x-cost net -$107.08 / +$2.77. IS-only
  choice is 8%, but its estimated OOS uplift comes $51.04 of $62.09 from
  BSC, while its IS uplift comes entirely from SOL ($15.17). The edge
  switches regimes and the exact fill assumption is especially fragile for
  the venue rug. Both stressed IS nets stay negative. Neither threshold
  passed the single-regime/3x-cost acceptance discipline; no exit threshold
  was wired.
- **Fix 3, gain caps: rejected.** Caps 150/200/250% kept IS respectively
  **32 / -$4.18**, **32 / -$4.18**, **33 / -$13.80**; IS per-trade edges
  +$1.432, +$1.432, +$1.144. Their OOS kept sets were **9 / -$20.91**,
  **13 / -$36.97**, **19 / -$46.17**, with edges -$0.289, -$0.809,
  -$0.396 (negative retention). OOS 3x-cost net: -$24.10, -$41.57,
  -$52.65. IS ties favor the smaller 150% kept set; all reverse OOS and
  remain negative under stress. The +437.1% winner `dap / SOL` was a regular
  15m GeckoTerminal signal (not early), SOL, ratio 3.1, liquidity $9,087;
  its peak reached +148% and its costed net was +$4.67. It would be cut by
  every candidate cap, illustrating the nonmonotonic signal relationship.
- **Fix 4, ratio floors: rejected.** Floors 2.0/2.5 kept IS **26 / -$57.43**
  and **17 / -$27.22** with per-trade edges -$0.647 and -$0.038. OOS kept
  **12 / -$27.57** and **10 / -$19.99**, edges -$0.263 and +$0.035;
  no positive IS edge exists to retain. OOS 3x-cost net -$31.97 and
  -$24.12. The 42 recorded sub-2.5 ratio entries include 12 costed wins,
  so the premise that this band had zero wins was also false.
- Pullback entry remains at 0% in paper config. Historical entry/peak/exit
  points cannot replay a pullback order or fill path. Round 3's
  `slip_from_signal_pct` field will allow later measurement on enriched
  entries. Full hermetic suite: **90 passed, 0 failed** via
  `.venv/bin/python -m pytest tests/ -q`. Paper `dry_run=true` and its code
  default remain unchanged. The bot was not restarted.

## 2026-09-27 — round 3 entry evidence for offline review
- Paper entry records and in-memory positions now retain the signal USD price
  (`priceUsd` from DexScreener or `base_token_price_usd` from GeckoTerminal),
  native commit price (the existing entry quote), USD commit price (entry
  multiplied by the existing SOL/USD or BNB/USD journal rate), and
  `slip_from_signal_pct` as the requested fractional change
  `(commit USD / signal USD) - 1`. Missing or unusable prices remain null.
- Both chains also record nonnegative signal-to-commit latency in milliseconds,
  15m buy and sell counts, 15m USD volume, market cap, and the previously
  journaled liquidity, all copied from the signal without fetching data.
  The DexScreener fallback uses its 5m window under the existing `m15_*`
  keys and records `window=5m` as before.
- Solana holder top 1 and top 5 percentages come from the already required
  `getTokenLargestAccounts` and `getTokenSupply` rug check. The guard's
  mint/freeze, concentration and fail-closed decisions are unchanged. BSC
  holder fields remain null because its existing honeypot quote check does
  not return holder accounts; no extra RPC call was added.
- The pool payload inspection checks the explicit `lp_burn_pct` and
  `lp_locked` fields in GeckoTerminal attributes and DexScreener pair data.
  The sampled payload shapes do not contain either field, so both remain
  null unless a future payload explicitly provides them. Liquidity is not
  treated as LP lock or burn evidence. No new API or entry fetch was added.
- Replay retains old entries unchanged and adds holder buckets `<10`,
  `10-25`, `25-40`, `>40` plus LP buckets `burned`, `locked`, `unknown`.
  All 67 historical closes have missing holder evidence and unknown LP
  evidence; these buckets support future review only.
- Field assembly measured 0.002174 ms per call over 100,000 hermetic calls
  in `test_entry_evidence_assembly_timing` on this workspace. This measures
  local arithmetic and dictionary creation,
  not network or disk timing. Persisting the evidence adds one local state
  save after the existing post-commit USD-rate lookup. Rejected commits do
  not make a USD-rate request. Full hermetic suite: 73 passed, 0 failed via
  `.venv/bin/python -m pytest tests/ -q`. Paper config and code default
  remain `dry_run=true`; the bot was not restarted.

## 2026-09-27 — round 2 entry-filter validation (no filters shipped)
- Reused `analysis/replay.py` and its cost model on all 67 closes, sorted by
  `close_ts`: first 45 IS, last 22 OOS. Three unmatched closes have no entry
  fields and pass through candidate filters. Metrics below are `n / net USD /
  win rate / profit factor / max USD drawdown`. Unfiltered IS: **45 / -$75.63 /
  35.6% / 0.282 / $75.63**; unfiltered OOS: **22 / -$38.24 / 13.6% /
  0.262 / $49.39**. With 3x estimated slippage, those baselines are
  -$77.75 IS and -$39.05 OOS.
- **Fix 1, pre-entry rug avoidance: rejected as untestable with the recorded
  entry fields.** The 18 rug-gap closes lost about $114.49 (10 IS, -$75.60;
  8 OOS, -$38.89). The existing SOL path checks mint/freeze authorities,
  largest-holder concentration and a sell route; BSC checks a buy/sell quote
  round trip. GeckoTerminal supplies pool liquidity, volume, transactions
  and price-change windows; DexScreener supplies pair liquidity, transactions
  and price-change windows. No journal entry records holder percentages,
  LP lock/burn evidence, signal price, or commit-time price. The optional
  pullback reference price is disabled in paper config. Therefore no
  entry-time veto set, filtered IS/OOS metrics, edge retention, or stress
  result can be established for a dump or tighter-holder screen. The IS/OOS
  baselines above remain the only measurable results for this fix. No screen
  was wired and no LP evidence was assumed.
- **Fix 2, liquidity cap: rejected.** IS-only comparison of veto thresholds
  found $40k strongest: IS filtered **36 / -$29.60 / 41.7% / 0.493 /
  $33.86**, vetoing 8 losers and 1 winner (vetoed net -$46.03). OOS filtered
  **8 / -$7.79 / 25.0% / 0.519 / $15.61**, vetoing 13 losers and 1 winner
  (vetoed net -$30.45). Its per-trade edge was +$0.859 IS and +$0.764 OOS,
  retaining 89.0%. However, the OOS retained sample is only 8 trades, its
  2 winners are both on Solana, and its 3x-slippage OOS net remains -$8.10
  (PF 0.506, drawdown $15.76; baseline -$39.05). This is inconclusive and
  fails the stress bar. At 3x slippage, filtered IS net is -$31.39 (PF 0.471,
  drawdown $35.25). The >$50k candidate retained only 14.6% of its IS
  per-trade edge on OOS (+$0.598 vs +$0.087). The SOL-only >$50k candidate
  reversed on OOS (+$0.598 vs -$0.030); BSC-only had zero IS vetoes, so it
  could not be tuned on IS. No liquidity cap was wired.
- **Fix 3, signal-gain cap: rejected.** IS-only selection favored 350%:
  IS filtered **34 / -$17.26 / 47.1% / 0.633 / $17.94**, vetoing 11 losers
  and no winners (vetoed net -$58.37). OOS filtered **20 / -$41.10 / 10.0% /
  0.178 / $49.97**, vetoing 1 loser and 1 winner (vetoed net +$2.85).
  Per-trade edge reversed from +$1.173 IS to -$0.317 OOS (-27.0% retained).
  At 3x slippage, filtered OOS net was -$41.82 (PF 0.173, drawdown $50.57)
  versus -$39.05 unfiltered; filtered IS net was -$18.97 (PF 0.603,
  drawdown $19.06). The 400% and 300% candidates also had -$0.317
  OOS per-trade edge. No gain cap was wired.
- No entry, risk, rug-guard, or config behavior changed. The rejected filters
  are offline counterfactuals: skipping entries would also change capacity
  and later trade opportunities, which this journal cannot replay.
- Full hermetic suite: 52 passed, 0 failed via
  `.venv/bin/python -m pytest tests/ -q`. Paper config and code default
  remain `dry_run=true`; the bot was not restarted.

## 2026-09-27 — offline profitability replay and signal-model validation
- Added `analysis/replay.py`: FIFO mint pairing of 64 entries and 67 closes
  (3 closes have no entry record), parsing of 599 FOMO/PRE-PUMP log signals,
  per-trade paper P&L, cost stress, attribution, and an explicitly approximate
  counterfactual exit calculator. Recorded peak/exit cannot reveal intratrade
  path, executable rung timing, quote depth, or gap fills. No HTTP is used.
- Costs are **additional estimates** on top of paper journal returns: 0.25%
  swap fee and 0.15% slippage per leg, 0.002 SOL priority-fee cap per leg
  from paper config, or 0.00002 BNB gas per leg. Stress triples slippage.
  USD marks use recorded close prices except 24 early SOL closes with no
  quote, for which a disclosed $115/SOL approximation is used. Aggregate
  native balances are kept separate by chain; PF and drawdown use USD.
- Costed baseline: 67 closes, -0.676571 SOL and -0.045862 BNB, about
  -$113.87 total; win rate 28.36%, profit factor 0.276, max USD drawdown
  $118.99. Rug-gap exits (exit over 60% below peak): 18, about -$114.49.
  Liquidity above $50k: 19 trades, one win, about -$56.78. Signal gain above
  400%: 11 trades, one win, about -$45.85. These buckets overlap.
- Added pure-stdlib L2 logistic training/scoring and serialized
  `analysis/signal_model.json`. Features are seven recorded entry fields or
  transforms; absent 15m volume is not invented. Labels use net P&L after
  modeled costs. A time-ordered 43/21 IS/OOS split tuned threshold 0.41 on
  IS only. IS unfiltered: 43 trades, -$69.82, 34.9% wins, PF 0.317,
  max DD $75.19; filtered: 13 trades, +$11.92, 69.2% wins, PF 2.569,
  max DD $6.72. OOS unfiltered: 21 trades, -$41.99, 9.5% wins,
  PF 0.189, max DD $44.27; filtered: 15 trades, -$29.61, 6.7% wins,
  PF 0.147, max DD $29.61. The OOS per-trade improvement of $0.026 is
  below 60% of the IS $2.540 improvement, retained winners concentrate in
  one regime, and filtered OOS remains negative under 3x slippage (-$30.16).
  **The filter did not validate and was not wired into entries or config.**
  The model artifact is for offline review only; no result establishes
  future returns or executable fills.
- Added hermetic replay/model tests. Full suite: 52 passed, 0 failed via
  `.venv/bin/python -m pytest tests/ -q`. Paper config remains `dry_run=true`;
  entry guards, position caps, kill switch, and rug checks were not changed.

## 2026-09-27 — entry signal freshness re-validation at commit
- Gap: entry work can take minutes after a scanner detects a pump. The
  commit guard rechecked trade caps and the kill switch, but could still
  enter after the pool's gain or liquidity had faded.
- Fix: SOL and BSC GeckoTerminal FOMO signals, GeckoTerminal early signals,
  and DexScreener fallback signals now carry a scan timestamp. At commit,
  signals older than `hunter.entry.signal_max_age_sec` get one rate-limited
  fetch of the same pool. The entry proceeds only if gain, liquidity, and
  buy/sell ratio still meet that scanner path's bars. Missing timestamps,
  invalid pool data, and fetch failures skip with `STALE SIGNAL SKIP`.
  Fresh signals add no HTTP request. DexScreener fallback uses its 5m gain
  bar because that feed has no 15m bucket.
- Config: `hunter.entry.signal_max_age_sec: 180` in the example and paper
  config; missing or invalid values fall back to 180 seconds. Paper config
  remains `dry_run=true`.
- Tests: full suite 85 passed, 0 failed (operator re-ran: 85/0, hermetic);
  HTTP blocked throughout and zero real requests made.
- Restart: bot relaunched 2026-09-27 14:13 EDT, pid 9869 -> 12010,
  dry_run=true, config unchanged apart from the new key.

## 2026-09-27 — kill switch re-checked at entry commit (mid-flight race fix)
- Gap: `guardrails_ok()` runs at signal time, but an entry then spends
  minutes in flight (rug screens, pullback waits, decimals lookups, entry
  quotes, honeypot checks). Meanwhile another manage thread can close a
  losing position and trip the daily-loss kill switch - and the in-flight
  entry would still commit, entering a trade AFTER the guardrail fired.
- Fix: new `_entry_commit_ok()` helper, called under the lock at commit
  time in both `enter()` (SOL) and `enter_bsc()` (BSC). It re-checks the
  daily/hourly trade caps (moved verbatim out of the inline commit blocks)
  AND `_kill_switch_tripped()`; on refusal it discards the pending-entry
  slot and logs the reason. No other entry logic changed; the pre-entry
  scan-time checks are untouched.
- Tests: 3 new cases (kill switch tripped mid-flight blocks, daily and
  hourly caps still block at commit, clear path still allows). Full suite:
  74 passed, 0 failed; no HTTP requests made.
- Config: untouched (dry_run=true intact); halt.flag, trades.jsonl,
  state.json, bot.log, keypair.json untouched.

## 2026-09-27 — daily-loss protect mode for open positions
- When either configured daily USD or SOL loss cap trips, existing positions
  enter persistent PROTECT MODE and use at most a 10% trailing stop. The
  tighter trail composes with post-TP and moonbag trails; invalid settings
  fall back to 10%. Protection stays on through the next daily reset.
- This limits further drawdown on open positions after the entry kill switch
  trips. Price gaps and unavailable quotes can still produce larger losses.
- Manage-cycle tests cover USD and SOL cap parity with entry guardrails,
  normal versus protected exits, post-TP and moonbag composition, persistence,
  and invalid trail settings.
- Config: `risk.kill_switch_protect_trail_pct: 10` added to
  `runs/paper-1h/config.json` and `config.example.json`; dry_run=true
  intact. Run verification (2026-09-27 ~08:15 EDT): the Codex session hit
  its usage limit right after the code edit, so the assistant reviewed the
  diff, fixed 7 test assertions (they expected the bare "trail-10%" label,
  but sell_pct_of_balance wraps labels as "SELL <name> (trail-10%)" —
  product behavior was correct), and re-ran the full suite: 70 passed,
  0 failed. halt.flag, trades.jsonl, state.json, bot.log, keypair.json
  untouched.

## 2026-09-27 — halt responsiveness and conservative entry guards
- **Halt flag:** check immediately after SIGTERM at the start of every main
  loop and during each 0.5s poll wait. A flag appearing mid-wait now exits
  with code 42 promptly; the startup check retains its existing behavior.
- **Solana rug guard:** reject truncated mint account data, zero supply, and
  empty largest-holder results. Those responses cannot establish disabled
  authorities or holder concentration, so an entry must fail closed.
- **Daily loss limits:** enforce the SOL cap even when a USD cap is also
  configured. Previously the USD branch skipped the SOL limit entirely;
  crossing either configured limit now blocks new entries.
- **Hermetic tests:** stub BNB/USD in the BSC entry test and block HTTP for
  the whole test suite. This prevents a paper test from fetching live data.

## v2 — 2026-09-24 (polish for live-money confidence)
- **Virtual token ledger (dry_run):** paper sells now simulate real swaps.
  Entry stores the quoted token amount; each rung sell quotes mint→SOL and
  decrements the virtual bag; realized P&L = simulated proceeds − stake.
  Previously rung sells were no-ops against the real (empty) wallet and P&L
  was price-math. Old positions (opened before this) still close on the
  legacy price-math path.
- **exit.mode: "ladder" | "moonbag"** (default ladder). Moonbag banks 50% at
  +100% and lets the rest ride with a 50% trailing stop, no TP ceiling.
  One-word flip in config; ladder stays live until paper data says otherwise.
- **Manage threads can no longer die silently:** the per-position loop body
  moved to `_manage_once()` wrapped in try/except with full tracebacks.
- **Heartbeat** every 30 min (open count, realized SOL, uptime) — silence in
  the log now means death, not quiet markets.
- **Watchdog cron** every 15 min restarts the bot if the pidfile process is
  gone (2026-09-24: bot died silently ~08:04 during a proxy blip, no
  traceback; positions unmanaged for ~1h until manual restart).
- **Daily analysis cron** (~07:00 ET): scores the journal against the
  confidence bar and proposes at most one evidence-backed tweak.

## v1 — 2026-09-24 (baseline)
- Hunter: GeckoTerminal (15 pages, token bucket ≤20/min) + DexScreener
  fallback; FOMO filter 100%+/15m, buys/sells ≥10, ≥50 buys, liq ≥$15k,
  15m vol ≥$5k. Rug guard (mint/freeze authority, holder concentration,
  honeypot quote) fail-closed on every entry.
- Exits: TP ladder 50% @+100%, 25% @+200%; trailing −30% from peak;
  hard stop −40% from entry. 5s manage poll per position.
- Guardrails: max 3 positions, 10 trades/day, 2h cooldown/mint,
  0.2 SOL daily-loss kill switch, halt.flag exits 42 (never auto-restarted).
- First paper round trip 07:58: miao coin trailing stop −39.3% from peak,
  realized −0.023 SOL. MultiChain exited 08:00: +0.024 SOL (trailing stop).

## v1.1 — 2026-09-24 (rug autopsy: GP / SOL)
- **What happened:** 08:03:24 paper-bought GP / SOL on a +131.2%/15m signal
  (buy/sell 3.4, liq ~$30k; honeypot check passed at entry). The bot process
  died silently at 08:04:24 (proxy blip, no traceback) and the position sat
  **unmanaged for ~60 minutes** while the token collapsed. On manual restart
  at 09:04 the manager sold immediately at −98.9% from peak, realizing
  −0.085894 SOL. With the bot alive, the −30% trailing stop would have fired
  for roughly −0.03 SOL — the outage tripled the loss.
- **Lesson:** a rug that collapses faster than the recovery loop can respond
  is the worst case. The entry guard (honeypot quote at entry) passed — a
  token can be sellable at entry and still go to zero an hour later. No
  entry-time check can fully prevent this; survival depends on (a) the bot
  never being unmanaged and (b) exits firing the moment price data returns.
- **Fixes applied:**
  - Watchdog (15 min) + log-staleness check already in place; worst-case
    unmanaged window is now ~15 min, not 60.
  - Entry quote failures (e.g. Jupiter 400s like the Felis signals) now log
    one clean SKIP line instead of a full traceback — entry still fails
    closed, but the log stays readable.
- **Implication for live-money confidence:** this is exactly why the 30-trade
  paper bar exists. The strategy's designed worst case per trade is the
  −40% hard stop; the realized −98.9% came from an infrastructure outage,
  not the strategy. Both must be proven before real money.

## v1.2 — 2026-09-24 (stale exit / position rotation)
- User directive: don't babysit dead positions; free the slot for fresh
  signals. New `exit.stale_exit_min` (45) + `exit.stale_exit_max_gain_pct`
  (10): a position older than 45 min and up less than +10% is sold in full
  ("stale exit"), freeing one of the 3 slots. Positions in real profit are
  untouched (trailing stop still guards them). Unit-checked on 4 cases.
- Stale exit tightened 45 -> 15 min per user ("it can be quicker than that").
  One full 15m signal window with no real move = momentum's gone, free the slot.
- Stale exit set to 30 min — middle ground between 45 and 15 per user.
- 2026-09-24 ~10:24: user-requested reset of the daily-loss kill switch
  (state.realized_sol -0.354 -> 0.0, clean stop + restart, pid 9498).
  Trade journal (trades.jsonl) untouched, so analyzer history is intact.

## v1.3 — 2026-09-24 (data-driven risk tightening, 19-trade review)
Diagnosis from 19 closed trades: 7 wins (+0.212 SOL) vs 12 losses (-0.567).
Two structural findings: (1) TP rung at +100% fired only 2/19 times — 6
winners peaked +40-88% and were trailed out before banking; (2) six
gap-down/rug events (-0.065 to -0.086 each) blew straight through the
trailing stop, exiting -83% to -99% from peak — no stop setting can
execute through a liquidity vacuum. Plus a real bug: 2 non-SOL entries
(Claude/ANTHRP, USDC/USDC) leaked through GeckoTerminal, costing -0.10.
Changes:
- Strict SOL-pair enforcement in BOTH feeds (GT quote_token relationship
  + pool-name fallback; DexScreener quoteToken address). Non-SOL quotes
  are now rejected at scan time.
- First TP rung +100% -> +60% (6 of 8 winners peaked above +60%).
- Trailing stop 30% -> 25%, hard stop 40% -> 35% (gradual fades cluster
  at -31% to -42% from peak; tighter keeps more of winners too).
- Entry bar raised: min 15m gain 10% -> 20%, min liquidity $3k -> $8k
  (weakest entries never ran; thin books gap harder).
- Position size 0.087 -> 0.06 SOL: the 0.2 SOL kill switch now needs 3+
  full-stake rug losses to trip instead of ~2.5.
- All values kept inside the auto-tuner's allowlist bounds so the daily
  self-improvement loop can continue from here.
Honest limit: nothing here prevents gap-rug losses; it only makes them
smaller and less frequent. Expectancy is still unproven at 19 trades.

## v1.4 — 2026-09-24 (USD P&L reporting)
- Per user request: P&L analysis now in dollars. Bot stamps `sol_usd`
  (Jupiter price API, 120s cache, fail-soft) on every entry and close
  journal record. analyze.py shows $ next to every SOL figure, using each
  trade's own stamped price with --sol-usd as fallback for older records.
  Close log lines now include the $ amount. Daily cron fetches SOL/USD and
  passes --sol-usd so reports always carry dollars.

## 2026-09-24 11:51 EDT - TP rung marked only on real fills (manual fix)
- Bug: a take-profit rung was marked "fired" even when the sell failed or
  returned dust. CTNT / SOL's +60% rung "fired" at a +120.9% feed print while
  the Jupiter fill for the actual size was 3.3555e-05 SOL (phantom depth on a
  thin pool) - the rung was recorded as a +120% take-profit.
- sell_pct_of_balance now returns (sig, proceeds_sol); the TP loop marks a
  rung ONLY when the sell succeeds, records the realized gain on the sold
  slice (not the feed gain), stores proceeds in the rung record, and logs a
  PRICE-IMPACT WARNING when the fill is <25% of the feed-implied value.
  Failed sells are retried next cycle instead of silently skipped.
- Corrected the live CTNT rung record to its honest -99.89% realized value.

## 2026-09-24 14:15 EDT - Dump detector: instant exits on violent drops (manual fix)
- New anti-rug reflex in the manage loop: if a position falls 12% or more
  from its max price within 60 seconds, the bot sells everything immediately
  instead of waiting for the 25% trailing stop. Rationale: today's exits kept
  printing "-80% to -99% from peak" - by the time the wide trail triggered,
  the liquidity was gone. The detector reacts to drop VELOCITY, so slow
  bleeds still use the normal trail/stale exits.
- Config: exit.dump_drop_pct=12, exit.dump_window_sec=60 (tunable).
- Per-mint in-memory price history (deque, 300 samples); cleared on close.

## 2026-09-24 14:20 EDT - Silent-death forensics (manual fix)
- Root-cause investigation found the bot is NOT crashing in Python: the
  pidfile survives every death, and Trader.run's try/finally always removes
  the pidfile on any interpreter-level exit. Verdict: external SIGKILL or a
  native segfault. Some "deaths" today were deliberate agent restarts.
- Added faulthandler.enable() at main() start so a native crash dumps a
  stack trace into bot.log instead of vanishing.
- New run_bot.sh wrapper: stays alive as the bot's parent, waits on it, and
  records the exit code in runs/paper-1h/deaths.log (137=SIGKILL,
  139=segfault, 143=SIGTERM). Next death identifies itself.
- Watchdog cron now restarts through run_bot.sh; bot relaunched under it.

## 2026-09-24 14:26 EDT - Fixed NameError that disabled ALL exits (watchdog-caught)
- The dump-detector edit referenced `ex` (exit config) inside
  _manage_once(), where it was not in scope. Every manage cycle raised
  NameError, the manage thread swallowed it, and no exit (TP, trail, hard
  stop, stale, dump) could fire for ~10 minutes. Caught by the watchdog's
  log scan. Fix: `ex = self.cfg["exit"]` at the top of _manage_once().
- Lesson: any edit touching the exit path gets a manage-cycle smoke test
  before the bot is considered healthy.

## auto-tune — 2026-09-28 11:14:11
- Applied `hunter.min_buy_sell_ratio`: 1.5 -> 2.0
- Rationale: raise hunter.min_buy_sell_ratio 1.5 -> 2.0 (geckoterminal-sourced: 153 trades, -0.437174 SOL - tighten buy pressure requirement)
- Context: 157 closed trades, PF 0.49. Auto-applied by the daily self-improvement loop (paper mode only).

## 2026-09-29 12:30 EDT - R9: ts_epoch on trades.jsonl records (deep-improvement loop iter-6)
- Instrumentation only, zero strategy-behavior change (validation-lane PASS,
  iter-5): entry records (SOL + BSC sites) and close records in trades.jsonl
  now carry `ts_epoch` (epoch seconds at write time). candidates.jsonl
  already had ts_epoch (committed in journal.py).
- Motivation: 12 close rows have impossible holds from mixed-zone `ts`
  strings; ts_epoch restores trustworthy hour/hold/ts-split analysis.
- Additive field: no strategy logic, risk math, guard, or cap reads it.
  Tests: tests/test_r9_ts_epoch.py (5 tests: all 3 write sites stamp it,
  finalize_entry_record passthrough, no strategy consumption, record shape
  unchanged).

## 2026-09-29 16:30 EDT - R10: early_accel_reject instrumentation (deep-improvement loop iter-7, codex-astra lane)
- Instrumentation only, zero strategy-behavior change: the Hunter's early
  (5m-window, pre-peak) path now journals every pool rejected by its
  acceleration gate as an `early_accel_reject` event row in candidates.jsonl
  (new `journal.sanitize_early_accel_reject`; written via `journal_event`
  directly so reject rows never pollute the ObservationTracker repeat
  counts). Rows carry m5/m15 gains, liquidity, volume, buys/sells, mcap,
  pool_created_at, and `decel_ratio` = p15/(p5*accel_ratio).
- Motivation: the accel gate (`p15 <= p5 * accel_ratio` rejects, accel_ratio
  1.2) is inverted vs its documented "accelerating / pre-peak / most of the
  move still ahead" intent -- it admits stalling moves and rejects fresh
  accelerations. Directional evidence on 15 paired early trades: the more-
  decelerating half went 0/8 wins, -$13.91, PF 0.000 vs 2/7, -$1.23, PF
  0.767 for the less-decelerating half (n=15, below every gate -- NOT a
  profitability claim). The rejected pools were never journaled before, so
  the corrected orientation cannot be backtested; these rows make the
  corrected-admission set observable for a prospective Track B gate.
- The gate itself is UNCHANGED (no entry-behavior change); a characterization
  test documents the inversion (must be updated if the gate is corrected).
  STRATEGY_VERSION untouched. Tests: tests/test_iter7_accel_reject.py (7).

## auto-tune — 2026-09-30 11:14:09
- Applied `hunter.min_buy_sell_ratio`: 2.0 -> 2.5
- Rationale: raise hunter.min_buy_sell_ratio 2.0 -> 2.5 (geckoterminal-sourced: 270 trades, -0.437174 SOL - tighten buy pressure requirement)
- Context: 275 closed trades, PF 0.49. Auto-applied by the daily self-improvement loop (paper mode only).
