# CHANGELOG — predict-bot (paper/analysis only)

## 2026-10-03 (codex persistent drive, iter-4: exit-priority sort fix SHIPPED)
- **Exit ordering bug fix** (codex iter-4): `check_exits` sorted equal-edge
  exits by `-mins_left`, putting the LEAST urgent (most time left) first —
  contradicting the documented "most urgent first: deepest negative edge,
  then least time left" and the scan loop's "already sorted most-urgent
  first" contract. One-character fix: sort key is now
  `(now_edge, mins_left)`. Proven by new regression test
  `tests/test_iter4_exit_priority.py::test_equal_edge_exits_prioritize_less_time_left`
  (fails on old code, passes on fixed). Full suite 461/461 green.
  User-visible effect: when several exits fire in one scan with equal edge,
  the soonest-closing window is now listed first as intended.

## 2026-10-02 (iter-20 lanes: ship-nothing on decisions; two decision-neutral patches)
- **R2 residual** (codex-astra lane, iter-20): `maybe_copilot_review` moved out
  of `check_exits` entirely. The advisory is now attached by callers AFTER
  `_state_write_lock()` is released via new `attach_copilot_advice(exits)`
  (`--exits-only` locked path + scan mark phase), so the up-to-~4s LLM call
  no longer serializes the exit-watch poll against the scan's mark phase.
  Mark gating (iter-19 R2) preserved at both call sites, so `--no-mark` runs
  stay fully read-only. `check_exits` is now copilot-free (decision layer
  pure). Reworked `tests/test_iter19_lane_cursor.py` R2 contract tests:
  `test_r2_copilot_not_called_inside_check_exits` +
  `test_r2_attach_copilot_advice_attaches`.
- **flip_analyst.py entry-price display fix** (codex-astra lane, iter-20):
  open-position snapshots now derive `entry_price_taken` from side +
  `market_mid` (exactly as `hold_guard.entry_taken` does), preferring a
  stored value when one exists — state records never carry the field, so the
  analyst prompt previously rendered every open position as "entry ?".
  Display only; never read by any entry/exit decision. Regression tests in
  new `tests/test_iter20_lane_codex_astra.py`.
- Lanes verdict (unanimous, ship-nothing on decisions): 886 grades /
  475 Kalshi-settled @ 54.11% (+1.4 pt over taken-implied, net -$0.0505/sig
  after fees); forward 10-02 slice at market pace (66.0% vs 66.0% implied);
  D2 ARMED but decaying (forward gap +1.5 pt, YES-side -8.0 pt against;
  de-arm recommended, forward counter keeps running); K1 first forward
  contact inverted the mined direction (-6.7 pt, n=18, side-mix artifact in
  the IS lift); M1 strongest registered candidate (forward n=68/100,
  +7.8 pt, 8/8 coins consistent — needs >=100 rows over >=2 days); W2
  disfavored (forward -2.3 pt); E8 retired. Flip-analyst calls 5/13 (38.5%):
  MODEL+TAPE 4/8 (50.0%), TAPE-ONLY 1/5 (20.0%) — retirement rule not
  triggered (n=5 < 10). Full suite 460/460 green.

## 2026-10-02 (iter-19 lane-cursor: decision-neutral hardening, no decision changes)
- **R1** `accuracy_stats`: added `no_mid_rows` + `win_rate_priced` (win rate
  over the mid-carrying population that `_mean_taken_price` and the
  `_pnl_*` helpers use) appended at the END of the dict. Legacy keys
  (incl. `win_rate` semantics) untouched and byte-identical — the pinned
  `test_accuracy_legacy_keys_byte_identical` contract holds.
- **R2** `check_exits`: exit-copilot advisory call now gated on `mark`
  (`if exits and mark:`). `--no-mark` runs are fully read-only again: no
  outbound LLM call, no `exit_copilot_log.jsonl` append. Production paths
  (`--exits-only` watch and scan mark phase, both mark=True) unchanged.
- **R3** test-only: `_scan` in `tests/test_iter8_infra.py` freezes
  `sdl.datetime` via a `_FrozenDT` subclass and derives `now` from the
  frozen instant — removes the second-boundary race that flaked
  timestamp-sensitive assertions. A/B pin tests (`test_iter11_ab`,
  `test_iter12_d1a_shadow`) register the new R1 keys in their strip sets
  (established pattern); decision-divergence pins still hold.
- **R4** test hermeticity (test-only, new finding this iteration): a
  mark-mode `main()` run writes `data/latest_fair.json`, which the live
  flip analyst trusts for 180 s — any pytest run in the live repo was
  injecting a fake model snapshot into the live analyst's context. Fixed
  with a conftest autouse fixture redirecting `sdl.DATA` to a per-test tmp
  dir (`tests/conftest.py`), plus two regression tests
  (`tests/test_iter19_lane_cursor.py::test_r4_*`).
- New `tests/test_iter19_lane_cursor.py` (9 tests). Full suite 454/454 green.

## 2026-10-02 (exit-copilot advisory wiring, decision-neutral provenance note)
- `scripts/exit_copilot.py` wired into `same_day_loop.py` (live since
  ~15:03 EDT): Gemini-2.5-flash advisory EXIT/HOLD second opinion attached
  as a `copilot` field on the single most urgent exit payload only.
  **Decision-neutral: no entry, exit, or grading logic reads the field;**
  independently verified in iter-18 (validation lane). Provider
  `EXIT_COPILOT_PROVIDER=gemini` (free tier via Secure Vault); kill switch
  `data/exit_copilot.kill`. Provenance note added by the iter-18
  improvement loop; the wiring itself landed outside the loop.
- Test-only: `tests/test_flip_analyst.py` updated for `build_tape()`'s
  3-value return `(tape_lines, alerted_dirs, batch_id)` and isolated from
  the live `~/hooks/state/kalshi-flip-watch-latest.json` stash
  (cursor lane, iter-18); full suite 429/429 green.

## 2026-10-02 (hold-guard: rapid-exit alerts for held positions, additive)
- **Decision-neutral w.r.t. entries/exits/grading: no existing logic touched.**
  New `scripts/hold_guard.py` + `tests/test_hold_guard.py` (20 tests) and
  hook wrapper `~/hooks/scripts/kalshi-hold-guard.sh` (not yet registered).
- Reads `data/same_day_loop_state.json` read-only for open held signals
  (side YES/NO, no exit_ts, window still open, >=1 min left); keeps a
  per-signal taken-side price history in
  `~/hooks/state/kalshi-hold-guard.json` (never writes scanner state).
- Fires 🚨 HOLD EXIT when the taken-side price collapses fast: big tier
  >=10c drop within 60s, fast tier >=7c drop within 30s (adverse moves only;
  favourable runs never fire). One alert per position; re-arm only after a
  further >=10c drop; 5-min per-signal cooldown; never after exit_ts or
  window close.
- Full suite 391/391 green.

## 2026-10-02 (iter-16 lane-cursor: E.2 volume, cluster rank shadow, vendored fixtures)
- **All three changes are decision-neutral: no entry/exit/grading decision
  path reads any new field.** No threshold, filter, edge, sizing, fee, or
  cron-delivery change; `data/` history untouched.
  - E.2: `volume` (`num(m["volume_fp"])`, the same value already printed on
    the alert signal) is now persisted on each new 15m state entry and
    copied to the grade row by `grade_pending()` (None on legacy rows).
    Written only on mark runs, on entries that were already being written.
    Supersedes the iter-15 F7 note that `volume` is never persisted.
  - Cluster rank (log-first shadow): new `annotate_cluster_rank()` stamps
    `cluster_rank_at_entry` (1-based, ticker-ascending) and
    `cluster_size_at_entry` on the run's pending 15m entries grouped by
    int `window_close_utc` (xvenue entries skipped). Runs in the lock-free
    scan phase before the mark phase, in-memory only; under `--no-mark`
    there are no pending entries, so nothing is written and the printed
    payload is unchanged. Note: the group counts only this run's NEW
    entries in that window (signals already alerted on earlier scans are
    not in the group). Both fields copied to grade rows (None on
    legacy/xvenue rows).
  - Test hermeticity: the iter-11 and iter-12 pre-patch A/B baselines are
    vendored as `tests/fixtures/pre_patch_iter11_same_day_loop.py` (md5
    c084867bb89062987a4857dafe3a4adf, verified) and
    `tests/fixtures/pre_patch_iter12_same_day_loop.py`;
    `test_iter11_ab.py` / `test_iter12_d1a_shadow.py` now load them relative
    to the test file. The suite no longer reads anything outside the repo.
  - A/B allow-lists (`NEW_KEYS`) extended with the three new keys; the
    printed signal's `volume` is pinned identical pre/post.
  - New `tests/test_iter16_lane_cursor.py` (13 tests): volume carry-through,
    rank/size by window, xvenue/legacy None, decision independence with
    ranks absent or garbage, `--no-mark` byte-identical state, fixture md5,
    and load-path checks. Full suite 371/371 green (358 baseline + 13).

## 2026-10-02 (iter-15 coordinator: doc corrections F7/F8/F9, text-only)
- **Docs only — no code, no decisions, no behavior changed.**
  - F7: README said "`spread`/`volume` are logged only" on the 15-min path.
    Only half true: `spread` is persisted to state entries and grade rows,
    but `volume` is printed in the alert signal only — never persisted to
    state or grade rows (0/388 rows carry it; E.2 spec for persisting it is
    queued for a future lane). README corrected. (iter-13's F3 CHANGELOG
    line carries the same overclaim; historical line left intact.)
  - F8: `_pnl_1usd_stake` docstring said "at the taken price". The function
    prices at the mid-derived taken-side price (`market_mid` for YES takes,
    `1 - market_mid` for NO takes), NOT the tradeable taken price
    (ask / 1-bid). Docstring corrected — this is the mandated $/sig metric
    for all lane gate bars (iter-15 validation flag).
  - F9: the D1a p-now-staleness-shadow comment prescribed refusing the spot
    "when its alert-time age (spot_age_s or spot_age_candle_s) exceeds 180s".
    `spot_age_candle_s` is the interval-open ts, not an alert-time age
    (observed negative, min -118s) and must never gate a rule directly; a
    future 180s rule must use `spot_age_s` (alert-time trade age) or
    `spot_age_fetch_s` (fetch-time age). Comment corrected.
  - Verified against the live code this iteration; full suite 358/358 green
    after the edits.

## 2026-10-02 (iter-14 coordinator: doc corrections F5/F6, text-only)
- **Docs only — no code, no decisions, no behavior changed.**
  - F5: module docstring said "Hourly" scan — the cron runs every 2 minutes
    (`same-day-prediction-loop__interval@2m.md`). Corrected to "2-minute".
  - F6: module docstring said dedupe key is "(ticker, window)" — code at
    L1277 uses `f"{m['ticker']}:{side}"` (ticker:side); both sides of one
    window can and do alert (14 (ticker, window) pairs graded on both YES
    and NO). Corrected.
  - Verified against the live code this iteration; full suite 358/358 green
    after the edits.

## 2026-10-02 (iter-13 lane-cursor: doc corrections F3/F4, text-only)
- **Docs only — no code, no decisions, no behavior changed.**
  - F3: two CHANGELOG lines and the README implied the 15-min entry path has
    spread/volume filters. Corrected: the 15-min path gates on quote sanity
    (`b > 0 and a > b`, `0.01 <= mid <= 0.99`), warmup, the edge gate, and
    the agreement filter only; `spread`/`volume` are logged, never gated.
    Volume gates (Polymarket >= 5000, Kalshi `volume_fp` >= 500) exist only
    in the cross-venue section (`same_day_loop.py` L1384/L1389). README now
    also notes `scripts/same_day_loop.py` does not read `config.yaml`
    (decision constants hardcoded at the top of the script). Verified
    against the live code this iteration.
  - F4: provenance correction on the agreement-filter entry — its
    "Kalshi-settlement truth, n=604" validation mixed 411 legacy
    Kraken-proxy labels with ~193 Kalshi-settled rows, and n=92 + n=356 !=
    604. Recomputed on Kalshi-settled rows only: 57/68 = 83.8% vs 68.3%
    implied (the single-session selection that motivated the filter);
    forward since: 55/84 = 65.5% vs 66.3% implied (no edge either way).
    Numbers recomputed from `data/signal_grades.jsonl` this iteration.

## 2026-10-02 (iter-12 lane-codex-astra: D1a p_now-staleness shadow, log-only)
- **Logging only — no entry/exit/grading decision changed.**
  `fair_15m`'s `meta` gains `p_now_candle` (latest 1-min Kraken candle close)
  and `p_now_candle_ts` (its exchange open-ts, seconds), from the already-
  fetched candle series (zero new network). The scan records `p_now_candle`,
  `p_now_candle_ts`, `p_now_ts` (last-trade print's exchange ts) and
  `spot_age_candle_s` (candle age at alert time) on the alert-time state
  entry; `grade_pending` copies all four to the grade row (None on legacy
  entries). Nothing reads these fields for any decision (verified: absurd
  shadow values move zero decision outputs; A/B replay pre-vs-post module,
  0 divergence modulo the 4 new keys; `--no-mark` state byte-identical).
  Makes each grade row self-contained for the future staleness-guard replay:
  `p_now' = p_now_candle iff p_now_candle_ts > p_now_ts`, refuse spot when
  its alert-time age exceeds 180s. The guard itself ships only after >=30
  Kalshi-settled full-page rows show a Brier gain (currently 7 such rows).
  358/358 tests green (7 new in `tests/test_iter12_d1a_shadow.py`).
  Pre-patch backup: improvement-loop `iter-12/pre-patch-backup/`.

## 2026-10-02 (iter-11: decision-neutral infra bundle — jump_flag log-first, state/grades locking, DST bound, $1-stake pnl, bid/ask, contrarian shadow log)
- **Logging/reporting only — no entry/exit/grading decision changed.**
  Eight additive items, all verified decision-neutral (351 tests green,
  A/B replay pre-vs-post module on a frozen-clock multi-scenario fixture:
  0 divergence modulo the new metadata keys):
  (0) `jump_flag` log-first (Lee–Mykland 2008): standardized 1-min Kraken
  returns over window-so-far, bipower-variation robust sigma, flag iff
  |r|/sigma_robust > 4.0; computed at signal entry from the already-fetched
  1-min candles (`meta["candles"]`, zero new network), logged as
  `jump_flag`/`jump_stat` on the state entry AND the grade row; never read
  by any decision; never raises (fail-safe -> None). (1) state-lock sidecar:
  flock now on `STATE_PATH + ".lock"` instead of `STATE_PATH` (the old lock
  broke when `save_state` replaced the file inode); `load_state` file-not-
  found now falls back to the `.bak` instead of returning empty (which would
  wipe dedupe history and re-alert everything). (2) `load_grades` fail-
  closed: new `GradesUnavailable` exception — suppressed reads, never
  fabricated; one bad JSON line no longer hides the rest. (3) grading under
  the grades sidecar lock: `grade_pending` re-reads + filters inside
  `GRADE_PATH + ".lock"` before appending (no lost/duplicate rows under
  concurrent writers). (4) DST end-of-day bound: `kalshi_same_day` bound is
  the next ET midnight inclusive (DST-aware) — fixes the 2026-11-01 25-hour
  drop and the daily 23:45–00:00 ET never-scanned window. (5) `$1-stake`
  gross/net-of-cost P&L (`pnl_1usd_stake_gross/net`, Kalshi 7% taker fee
  schedule) beside the legacy keys; legacy `pnl_per_dollar_staked`
  byte-identical. (6) `bid`/`ask` persisted on state entries and grade rows
  (metadata only). (7) shadow log of contrarian drops to
  `data/contrarian_drops.jsonl` (lock-free scan phase, bounded 512 KB with
  rotation, never raises, `--no-mark` stays read-only).
- **Not quoted forward.** All new metrics are reporting context only.

## 2026-10-01 (iter-8: decision-neutral infra bundle — spot-age/fetch logging, Kraken error fail-closed, exit_direction, post-filter stats, ~19:45 EDT)
- **Logging/reporting only — no entry/exit/grading decision changed.**
  Six additive items, all verified decision-neutral by two independent lanes:
  (B1) fetch-time `spot_age_fetch_s` — the iter-5 `spot_age_s` is run-start-
  based and goes negative on 20/27 logged rows (min -48 s, median -13 s);
  `fair_15m(..., meta=None)` now records `trades_fetch_ts`/`trades_rows`
  into a caller-provided dict (6-tuple return unchanged) and `main` logs
  `spot_age_fetch_s` measured when the trades page came back. (B2) full-page
  flag `trades_full_page` — True iff the Kraken trades page returned all
  1000 rows (deterministic staleness indicator; reconstruction: 18/196 priced
  rows >60 s stale at fetch). (B3) additive `exit_direction` on exit dicts
  (favourable/adverse/None at 3-dp display precision; NOT stamped into state,
  never read by `decide_exit`) — documents the iter-7 finding that 113/141
  historical `edge_reversed` exits fired with the taken side *above* entry;
  the cron copy's "market moved against the thesis" wording is the delivery
  layer and needs the user's call to change. (C-i) explicit Kraken `error`-
  array check (`_kraken_result`): non-warning errors -> {} (fail-closed; can
  only suppress signals/exits, never create them; W-prefixed entries treated
  as warnings per Kraken's contract). (C-ii) log-only `p_ref`/`p_now`/`sigma`/
  `floor_strike` on pending entries and grade rows (strike from the already-
  fetched market dict — zero new network calls in the scan path), enabling
  staleness/basis/straddle replays without API reconstruction. (C-iii)
  `accuracy_stats` gains `mean_taken_price` and a `post_filter` split (last
  50 settled rows the agreement filter admits); all legacy keys byte-
  identical (verified on every grade-file prefix).
- **Not quoted forward.** `post_filter.win_rate` (currently 0.84) and
  `mean_taken_price` are reporting context only — same never-quote-forward
  treatment as `pnl_per_dollar_staked`. Grading is Kalshi-settlement since
  fix 2c (235/235 labels re-verified against Kalshi's public `result` this
  run); the old "Kraken-spot proxy" wording in earlier entries and loop
  notes is stale. Agreement-filter read this run: market-favored 57/68 =
  83.8% vs contrarian 22/128 = 17.2%, rolling-50 35/50 = 70.0% — single
  trading session, not a forward expectation; no profitability claimed.
- **Decision candidates this iteration: all FAIL/REJECT.** Re-tested with
  walk-forward (OOS = most recent 40%, n>=50): drop |edge|>=0.11 (+2.0 pt,
  weakening on fresh data), drop |edge|>=0.15, taken-floor 0.55/0.60 (lose
  pnl/$), YES-only (-6.2 pt), NO-only (+7.3 pt REJECTED: single-coin-driven,
  IS/OOS 66.7% -> 91.3% inconsistency), straddle vetoes, one-alert-per-
  window, stale-spot/full-page vetoes, taken-price caps, NO-side gates —
  none clear the +5 pt / pnl gain bar, several fail red-flag checks. The
  fade-the-model idea measured +0.0332 pnl/$ pre-fee, negative after Kalshi
  taker fees — not an edge, not proposed. Ship-nothing on decisions.
- **Tests:** 296 passed in the coordinator's own shell (230 pre-existing +
  66 new in `tests/test_iter8_infra.py`, incl. poison tests feeding absurd
  metadata through decision paths asserting identical outcomes). Neutrality:
  cursor lane 3,331 A/B scenarios (historical entries, exits-only runs,
  grade-only, seeded fuzz incl. crossed/empty books, Kraken error bodies,
  HTTP 500s) with 0 decision diffs and identical HTTP call mix; validation
  lane 243/243 green with zero flips on A/B + 196-row replay. Dual-gated
  per the iter-7 deferral (cursor + validation PASS); the three core items
  (error check, log-only fields, post-filter split) additionally carry the
  codex-astra lane's iter-7 PASS. `EDGE_THRESHOLD` = 0.05, `AGREE_LEAN_MIN` = 0.5
  and the 15-min quote sanity checks (`b > 0 and a > b`, `0.01 <= mid <=
  0.99`) untouched. Correction: the 15-min path has no spread or volume
  filter (`spread`/`volume` are logged only); volume gates exist only in the
  cross-venue section. Paper/analysis only.

## 2026-10-01 (iter-5: alert-time spot-age + entry-context logging, ~13:50 EDT)
- **Logging only — no gating.** `fair_15m` now returns a 6-tuple: the old
  5-tuple plus `spot_ts`, the exchange timestamp of the last-trade print
  used as `p_now` (first five values unchanged, byte-identical decisions).
  Marked 15-min entries record `spot_age_s` (seconds, at alert time); grade
  rows copy `spot_age_s`, `spread`, and `mins_left` (None on legacy rows).
  Additive metadata only — entry, exit, grading, and delivery logic never
  read these fields (grep-verified: `spot_age_s` appears only at the two
  write sites). Unlocks future regime slicing by entry-time market context
  and measurement of Kraken spot staleness on new data (the Trades
  endpoint caps at 1000 rows, so `p_now` can lag minutes on busy pairs —
  confirmed live on BTC this iteration; the guard itself stays replay-only
  per the standing freeze).
- 7 existing `fair_15m` stubs extended to the 6-tuple (`a[3]` = now_ts ->
  age 0); `test_extra_fields_do_not_affect_grading` now asserts the
  spread/mins_left copy-through (grading assertions untouched).
- **Tests:** 230 passed (225 + 5 in new `tests/test_iter5_spot_age.py`).
  Dual-gated: implemented by the codex-astra lane, independently
  decision-neutrality-verified (diff review + A/B) by the validation lane.
  Paper/analysis only — no orders, no credentials, no threshold/filter/
  cron/delivery changes.
- Deferred/frozen (no ship): the Kraken spot-staleness guard (C1, ported
  from the combo scanner's `model_edge.py`) stays replay-only/frozen;
  filter candidates F1–F6 all failed/rejected walk-forward; F-NO falsified
  on settlement truth (OOS 27.27% vs baseline 32.14%).

## 2026-10-01 (idea #1 log-first: order-book imbalance at entry, ~11:40 EDT)
- **Logging only — no gating.** This is the log-first phase of idea #1:
  the imbalance is recorded for later validation against Kalshi-settled
  outcomes; nothing reads it for any decision. No new entry gate, no exit
  trigger, no threshold on the imbalance anywhere.
- New `book_imbalance(orderbook, depth=5)`: signed YES-side depth imbalance
  `(V_bid - V_ask)/(V_bid + V_ask)` in [-1, 1] (+1 = bid-heavy) over the
  top-5 levels, best price first. V_bid = YES bids; V_ask = NO bids (the
  YES asks at 1 - no_price). Accepts the API body `{"orderbook_fp":
  {"yes_dollars", "no_dollars"}}` or a raw/legacy book; empty, missing, or
  malformed -> None, never raises.
- New `entry_ob_imbalance(ticker)`: best-effort
  `GET /markets/{ticker}/orderbook` (10s timeout) -> `book_imbalance`; any
  failure -> None.
- `main()`: fetched only for new 15-min signals in mark mode, strictly after
  every gate (incl. `agrees_with_lean`), and stored as `ob_imb_at_entry`
  (3dp or None) on the alerted state entry only. The printed signal
  payload / stdout JSON contract is unchanged; `--no-mark` never fetches.
- `grade_pending()`: copies `ob_imb_at_entry` onto grade rows (None for
  legacy entries); never read by grading math.
- **Tests:** 225 passed (187 + 38 in new `tests/test_ob_imbalance.py`).
  New `tests/conftest.py` autouse guard makes orderbook URLs fail offline so
  existing mark-mode `main()` tests stay network-free.
- Gates unchanged: `EDGE_THRESHOLD=0.05`, `AGREE_LEAN_MIN=0.5`,
  `shrink_midrange(fair, mid, alpha=0.25, lo=0.40, hi=0.60)`,
  `WINDOW_WARMUP_MIN=3.0`, `STRIKE_STRADDLE_GATE=False`, `EXIT_EDGE`,
  `decide_exit`; patch is purely additive (0 lines removed). Paper/analysis
  only — nothing about orders or live trading.

## 2026-10-01 (fix 2c: Kalshi-only settlement grading, ~11:35 EDT)
- **Grading ground truth is now Kalshi's published settlement ONLY.**
  `grade_pending()` accepts only settled YES/NO from the public
  `GET /markets/{ticker}` `result` field. Unpublished/unsettled/failed
  results (None) write NOTHING and the window is retried on later runs,
  indefinitely, at any age — there is no proxy fallback and no retry
  window.
- **Deleted:** the entire Kraken-spot-proxy fallback branch, the
  `KALSHI_SETTLE_RETRY_S` constant and its retry-window logic, the
  Kraken-pair prerequisite and candle cache from the grading path, and the
  now-unused `kraken_price_at()` helper. Grading never touches Kraken.
- **Legacy rows:** `data/signal_grades.jsonl` was backed up (copy) to
  `~/workspace/goals/same-day-prediction-market-opportunity-loop/hidden_files/improvement-loop/grades-backup-2026-10-01.jsonl`,
  then atomically migrated (write tmp + os.replace): all 411 legacy
  `proxy="kraken"` rows now carry `"excluded_from_accuracy": true`.
  `accuracy_stats()` computes rolling stats over Kalshi-settled rows only;
  its note text now reads "settlement per Kalshi's published result only
  (legacy Kraken-proxy rows excluded); gross of fees".
- **Tests:** 187 passed. `tests/test_kalshi_settlement.py` rewritten for
  Kalshi-only truth (indefinite deferral at any window age, proxy rows
  excluded from accuracy); `test_seed_coverage.py` grade fixture now stubs
  `kalshi_settlement` instead of the proxy; `test_delivery_safety.py` B6
  case is now a settlement-fetch failure (defers, never raises);
  `test_alert_context.py` / `test_fair_raw_logging.py` grading tests stub
  settlements. New guards assert grading never calls Kraken.
- **Cron text:** `same-day-prediction-loop` body step 2 updated via
  `cron.update` to describe Kalshi-only settlement grading.
- Gates unchanged: `EDGE_THRESHOLD=0.05`, `AGREE_LEAN_MIN=0.5`,
  `shrink_midrange(fair, mid, alpha=0.25, lo=0.40, hi=0.60)`,
  `WINDOW_WARMUP_MIN=3.0`, `STRIKE_STRADDLE_GATE=False`, `EXIT_EDGE`,
  `decide_exit` logic; entry/exit/scan code untouched. Paper/analysis only
  — nothing about orders or live trading.

## 2026-10-01 (known-fixes 2a+2b: exit-payload taken-side fields + duplicate-exit lock, ~11:20 EDT)
- **FIX 2a — NO-side display bug in exit payload.** `check_exits` exit dicts
  mixed conventions: `entry_edge`/`entry_mid`/`now_mid`/`now_fair` were
  YES-oriented while `now_edge` was taken-side, so NO takes reported e.g.
  "Entry 41.5c @ edge -7.7" for a real taken-side entry of 58.5c @ +7.7
  (KXETH15M-26OCT011015-15 NO: entry `market_mid=0.415`, `edge=-0.077`).
  Exit dicts now carry canonical taken-side fields `entry_price_taken`,
  `entry_edge_taken`, `now_price_taken`, `now_fair_taken` (`now_edge` was
  already taken-side); the old YES-oriented fields are kept for backward
  compatibility and marked DEPRECATED in a code comment. The exit-reporting
  instructions in the `same-day-exit-watch` and `same-day-prediction-loop`
  cron bodies were updated (via `cron.update`) to read the taken-side
  fields and never the deprecated ones for display.
- **FIX 2b — duplicate-exit race.** Two concurrent writers (5-min scan +
  exit-watch poll, or two polls) could both load state, both see `exit_ts`
  unset, both emit, both save (double-fires observed 3x, e.g. SOL 11:00 NO).
  New `_state_write_lock()` (exclusive `fcntl.flock` on the state file)
  wraps the load→decide→save critical section in `exits_only_run` (via new
  `exits_only_locked`, which re-reads state from disk inside the lock) and
  in `main`'s scan mark phase (scan accumulates `pending_entries` lock-free,
  then merges them into freshly re-read state under the lock — first writer
  wins, losers get their `new` flag flipped so nothing double-alerts —
  before `check_exits(mark=True)` + `save_state`). `--no-mark` paths never
  take the lock or write. JSON stdout contract unchanged (exit dicts only
  gain the four additive `*_taken` fields).
- **No decision change.** `EDGE_THRESHOLD` 0.05, `AGREE_LEAN_MIN` 0.5,
  `shrink_midrange(fair,mid,alpha=0.25,lo=0.40,hi=0.60)`,
  `WINDOW_WARMUP_MIN` 3.0, `STRIKE_STRADDLE_GATE=False`, `EXIT_EDGE` 0.0 and
  all entry/exit decision logic untouched (grep-verified).
- **Tests:** `tests/test_exits.py` (+3): taken-side fields for a YES exit,
  the ETH NO example (`entry_price_taken=0.585`, `entry_edge_taken=0.077`),
  and a two-thread barrier concurrency regression asserting exactly one
  exit emitted total and exactly one exit stamp on disk; the three existing
  `exits_only_run(mark=True)` tests were updated to seed a tmp state file
  (mark now re-reads from disk inside the lock). Full suite: 184 passed.

## 2026-10-01 (iter-3: raw-fair alert-time logging, ~07:45 EDT)
- **Non-decision metadata capture.** Alerted 15-min entries now persist
  `"fair_raw"` (pre-shrink fair); grade rows copy it at grade time (legacy
  entries get `None`). Enables future shrink re-validation on fresh data —
  inverting the shrink from the recorded edge is not exact when the market
  mid sits outside the shrink band (73/301 post-ship rows were
  inversion-ambiguous), which was making `shrink_midrange` un-re-measurable.
  Additive only: `fair_raw` has three write sites and zero readers anywhere
  in scripts/src; the printed delivery payload is unchanged.
- **No decision change.** Gated by the Codex-Astra and validation lanes;
  stubbed old-vs-new A/B in the coordinator shell: payload, state file, and
  grade rows byte-identical (modulo the additive field + timestamps);
  `EDGE_THRESHOLD` 0.05, `WINDOW_WARMUP_MIN` 3.0 untouched. Live 5-min scan
  untouched by the change (additive metadata; no readers).
- **Tests:** `tests/test_fair_raw_logging.py` (3): pre-shrink value exact
  while the printed edge stays shrunk, `--no-mark` writes nothing, grade
  rows carry `fair_raw` through with legacy entries at `None`. Full suite:
  175 passed.

## 2026-10-01 (iter-2: delivery-safety + state/grade-file robustness, ~01:45 EDT)
- **Grading failure can no longer swallow alert delivery.** `main` wraps
  `grade_pending` in try/except: on any grading exception the JSON payload
  still prints (signals were already stamped+saved), with
  `grading_failed=<err>` appended to `notes` and accuracy recomputed from
  the on-disk grades. Previously an unguarded Kraken `raise_for_status`
  inside the grading path exited with empty stdout while state claimed the
  alerts were sent — stamped as sent, never delivered.
- **Kraken grading-path failures defer instead of raise.** A Kraken fetch
  error while pricing a window now routes to the existing "retry next run"
  branch, never propagates out of `grade_pending`.
- **Atomic `save_state` + backup restore.** State writes go to a tmp file and
  `os.replace` into place (readers never see a half-written file); the
  previous good copy is kept as `same_day_loop_state.json.bak`, and
  `load_state` restores it on a corrupt read instead of defaulting to empty
  (which would have wiped dedupe history and re-alerted everything).
- **`load_grades` survives one bad line** (per-line try/continue + dedupe by
  key — a torn line no longer hides every row after it, which previously
  froze accuracy on the pre-corruption prefix and caused re-grade dupes);
  `append_grades` repairs a missing trailing newline before appending.
- **11h Kraken-candle-history cutoff now gates only the Kraken-proxy
  branch.** Old windows grade from Kalshi's published settlement at any age;
  the proxy path keeps the cutoff (avoids infinite retry where candles
  don't reach).
- **No decision change.** Entry/exit behavior byte-identical (stubbed A/B:
  full JSON payload equal except timestamps). Live 5-min scan ran the new
  code cleanly at 05:19 UTC (backup file created as designed).
- **Tests:** `tests/test_delivery_safety.py` (7): atomic save + backup
  restore, missing-file load, bad-line survival + dedupe, trailing-newline
  repair, Kraken-failure deferral, >11h window graded from settlement,
  end-to-end grading-failure delivery. Full suite: **172 passed**
  (162 pre-existing + 3 alert-context + 7 delivery-safety). One iteration
  lesson: module-level `STATE_BACKUP` broke tests that monkeypatch
  `STATE_PATH` (cross-device `os.replace`); backup path is now derived
  dynamically from `STATE_PATH`.
- Gates: Cursor-lane audit (code-verified findings, ranked candidates);
  coordinator implementation in a clean repo copy (172/172) + own full-suite
  run on the live repo + stubbed decision-equivalence A/B. Note: the
  validation lane finished before the audit report existed, so it did not
  gate these candidates — the coordinator served as the second gate under
  clean-copy discipline; flagged transparently here.

## 2026-10-01 (iter-2: alert-context persistence, ~01:30 EDT)
- **Non-decision metadata capture.** New 15-min `alerted` entries now persist
  `"spread"` (ask−bid at alert time) and `"mins_left"` (minutes left in the
  window) alongside the existing fields. Additive metadata only: nothing in
  entry, exit, or grading logic reads these fields — entry/exit behavior is
  byte-identical; verified by grep (no readers anywhere in scripts/src).
- **Why:** regime analysis by spread bucket was previously impossible — spread
  was computed at alert time but never persisted (only in the printed signal
  dict). Enables spread-slice diagnosis in future iterations.
- **Tests:** `tests/test_alert_context.py` (3): entry carries both fields with
  exact values; `--no-mark` writes nothing; grading ignores the new fields and
  grade rows keep their schema. Full suite: 165 passed (162 pre-existing).
- Independent gates: developed+tested in a clean repo copy by the
  Codex-Astra lane; independently re-gated by the coordinator (field-reader
  grep + full suite in coordinator shell). Note: the validation lane had no
  sibling reports on disk when it finished, so the coordinator served as the
  second gate.

## 2026-10-01 (grading truth fix, ~00:25–00:45 EDT)
- **Grade from Kalshi's own settlement.** `grade_pending` now reads the
  public `GET /markets/{ticker}` `result` field ("yes"/"no") as ground truth
  instead of the Kraken-spot proxy (which disagreed with settlement ~20% of
  the time due to the off-by-one-candle bug documented in round 3).
- **Defer, don't misgrade:** if Kalshi hasn't published `result` yet and the
  window closed <30 min ago, grading is skipped and retried next run; the
  Kraken proxy is only a fallback for older windows. Settlement fetches are
  parallel (≤4 workers) and best-effort — any failure falls through to the
  proxy/defer path, never raises.
- **No decision change.** Entry/exit logic untouched; grading output only
  feeds `new_grades`/`accuracy` in the JSON. Grade rows carry
  `"proxy": "kalshi"|"kraken"` and the accuracy note now reports the actual
  source mix (e.g. "settlement per Kalshi's published result (12/50), else
  Kraken-spot proxy").
- **New tests:** `tests/test_kalshi_settlement.py` (21). Full suite: 162
  passed. Live read-only smoke test (`--grade-only --no-mark`) clean; live
  `kalshi_settlement` verified against a real expired ticker.
- Note: historical rows in `data/signal_grades.jsonl` keep
  `"proxy": "kraken"`; the rolling-50 accuracy will transition to Kalshi
  truth as new windows settle. Win-rate comparisons across the transition
  mix two label sources — interpret accordingly.

## 2026-09-30 → 10-01 (round 3 accuracy upgrade, ~23:40–00:05 EDT)
- **No decision change shipped.** Live entry/exit behavior is byte-identical
  to the previous code (recorded-response replay: 97/97 historical scans +
  1 live scan, stdout + state + grades).
- **Added, disabled:** `STRIKE_STRADDLE_GATE = False` + `straddles_strike()`
  — would skip an entry when the Kraken window-open reference and Kalshi's
  `floor_strike` sit on opposite sides of spot. Tighten-only, no new API
  calls. Not validated: fails the proxy-label gate (newer half +0.0042) and its
  settled-label Brier gain (−0.0091) is matched by a probability-matched
  placebo. Enable only after forward validation on Kalshi-settled outcomes.
- **New tests:** `tests/test_strike_straddle.py` (20). Full suite: 141 passed.
- **Audit findings (no code change yet):**
  - Grading proxy is off by one candle: `kraken_price_at` returns the close of
    the candle that *opens* at the boundary (price 60 s after it); Kalshi
    settles on the 60-s average *before* it. Proxy labels agree with Kalshi's
    published `result` only 79.9% of the time (219/274).
  - On settled outcomes the model has no edge: live-equivalent set (n=188)
    Brier 0.1507 vs market mid 0.1431; settled win rate 32.4% vs 31.0% implied
    by price. Proxy's "model beats market" is a label-noise artifact.
  - Shipped `shrink_midrange` and warmup both re-validated on settled labels
    (−0.0053 same-row; −0.0366 beyond a matched placebo).
  - `fair_15m` uses `trades[-1]` from a ≤1000-trade batch starting at
    window_open−300; for busy pairs `p_now` can be minutes stale (22/188
    signals). No measurable accuracy effect on this data; not changed.
  - Bracket-ladder check is docstring-only; Polymarket cross-venue cannot match
    15-min markets (title Jaccard 0.14 < 0.5).
  Reports: goal `hidden_files/improvement-loop/{audit,impl,validation}-round3-2026-09-30.md`.

## 2026-09-30 (iteration 1 of the 6h improvement loop, ~19:10–19:45 EDT)
- **Reporting fix (shipped, no entry/exit decision changes):** `accuracy_stats`
  now computes `pnl_per_dollar_staked` as true per-dollar-staked = total
  profit / total dollars paid for contracts (previously returned the raw
  total mislabeled as per-dollar). Last-50 signal set now reads **+0.0901**
  gross of fees, mid-priced (old code printed +1.74). Zero decision changes.
- **New tests:** `tests/test_seed_coverage.py` — 25 tests covering ticker
  parsing (SEP/EDT, JAN/EST, roundtrip ET-minute, invalid forms), `--no-mark`
  state/grades byte-immutability, grade idempotency, opposite-side
  same-window independent grading, PUSH handling, legacy string entries,
  and the pnl-per-dollar normalization itself. Full suite: 121 passed.
- **Validated, not shipped:** taken-side win-prob floor (skip if
  p_taken < 0.30) passed the accuracy bar (+8.8 OOS accuracy pts) but was
  REJECTED by independent validation — Brier +0.021 worse on every split,
  pnl sign flips IS→OOS, accuracy gain is degenerate mechanical selection;
  also conflicts with the 19:16 EDT no-decision-changes directive. Diff
  preserved in the iteration-1 lane report for reference if the freeze is
  lifted. Falsified candidates 2–4 (mins-left floor, floor+mins-left,
  tail-mid guard): documented, do not retry unchanged.
- **Independent re-validation:** both earlier shipped changes re-verified —
  `shrink_midrange` (Brier −0.0056 clean-set, exact 4-decimal match on
  claimed older half) and `WINDOW_WARMUP_MIN=3.0` (Brier −0.0307 overall,
  OOS −0.0176 n=67, robust across falsification attempts). No mismatches.
- Audit headline (honest negative): market mid Brier 0.1995 beats model
  0.2032; edge magnitude does not separate wins from losses; all 286 grades
  are one ~6h session (~43 correlated windows). System has no demonstrated
  edge; never call it profitable or validated. Grading is a Kraken-spot
  proxy, not Kalshi-settlement-verified.

## 2026-10-01 (agreement filter: drop contrarian takes)
- **Agreement filter on 15-min entries.** A signal is now dropped unless the
  taken side agrees with the market's directional lean, i.e. the taken-side
  price (mid for YES, 1-mid for NO) is >= `AGREE_LEAN_MIN` = 0.5. Contrarian
  takes — the model fading a market that leans >=50% the other way — are
  skipped before both the alerted-stamping and `signals.append`, so they
  neither alert nor count as signals.
- **Validation (Kalshi-settlement truth, n=604 graded signals).** Agreeing
  signals: +0.117/signal, 78.3% win rate, n=92, bootstrap 95% CI entirely
  positive; holds across 4 forward time splits, 5 of 6 coins, both days.
  Contrarian signals: -0.022/signal, 20.8% win rate, n=356.
- **Correction (2026-10-02, iter-13 cursor audit — provenance).** The line
  above over-claims: the n=604 set mixed 411 legacy Kraken-proxy labels with
  only ~193 Kalshi-settled rows, so "Kalshi-settlement truth" is wrong, and
  the agreeing/contrarian counts (n=92 + n=356 = 448) do not sum to 604.
  Recomputed on Kalshi-settled rows only (235 rows with windows closed on or
  before 2026-10-01T23:35Z, 196 with a logged mid): agreeing signals were
  57/68 = 83.8% vs 68.3% implied by price — the single-session selection that
  motivated the filter, never a forward expectation. Forward Kalshi-settled
  result since (now 84 rows): 55/84 = 65.5% vs 66.3% implied —
  market-calibrated, no edge either way.
- **Tightening only.** This drops ~80% of signals; no other decision changes:
  `shrink_midrange`, `WINDOW_WARMUP_MIN`, `EDGE_THRESHOLD`, the 15-min quote
  sanity checks (`b > 0 and a > b`, `0.01 <= mid <= 0.99`), exit-watch
  (`decide_exit`/`check_exits`), grading, state handling, and the cross-venue
  (xvenue) section are untouched. Correction: no spread or volume filter
  exists on the 15-min path (`spread`/`volume` are logged only); volume gates
  (Polymarket >= 5000, Kalshi `volume_fp` >= 500) exist only in the
  cross-venue section. Paper/analysis only —
  nothing about orders or live trading.
- **Tests:** `tests/test_agreement_filter.py` (6): agreement YES kept,
  contrarian YES dropped, agreement NO kept, contrarian NO dropped, boundary
  taken-price 0.5 kept, constant equals 0.5.
# 2026-10-05 — quote-side paper accounting and shadow validation

- Added `scripts/quote_side_analysis.py`, a read-only one-contract replay of
  Kalshi-settled grades. It writes paired mid and executable gross/net P&L,
  charges the existing taker fee on each traded leg, and marks missing quotes
  unavailable. Historical quote coverage: 884/1,165; mean paired net gap
  $0.008339 per contract (median $0.005).
- Added an executable-edge shadow value and boolean to new alert state and
  grade rows. It does not affect entry, exit, ranking, alerts, or grading.
- Added `scripts/validate_quote_shadows.py` for chronological, read-only gate
  and model-feature replays. The gate remains shadow-only after a negative
  final forward block. Book, volatility-regime, and jump probes failed the
  Brier/no-degraded-bin ship bar; no model decision change was made.
- Full suite after each workstream: 483, 484, and 484 tests passed.
