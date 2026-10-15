# Changelog

## 0.5.11 — 2026-10-02
- `docs/validation.md` ship-bar item 3: the old prose claimed "96¢ plus
  fees is under 100¢" for a 48¢+48¢ Yes+No pair. With the corrected fee
  formula the exact per-order fees are 2¢/leg (ceil(1.7472)), so a 1-set
  fill costs exactly 100¢ (zero edge — the suite already enforced this;
  docs-only fix, no code or behavior change). The probe locks because
  per-order fee ceil amortizes at size (20-set fill: 1,920 + 70 = 1,990¢
  vs 2,000¢ payout, 10¢ profit); 49¢+49¢ is rejected at every fill size.
- `brackets_exhaustive` now parses strikes through `_strike_value` and
  fails closed. A needed `floor_strike`/`cap_strike` that is not a finite
  int/float (non-numeric or numeric string, bool, NaN/inf) makes the event
  unverifiable: it is skipped and counted in `B_unverified_exhaustive`.
  Before, bare `float()` raised on one malformed row, and since
  `screen_type_b` runs unguarded inside `run_scan`, that killed the whole
  sweep; a bool strike silently coerced to 1.0/0.0. Tighten-only (can only
  reject, never admit); no behavior change on well-formed numeric data.

## 0.5.10 — 2026-10-02
- `_poly_fee_rate` now fails closed. Only an explicit `feesEnabled: false`
  means fee-free. A record with no fee info, or with a `feeSchedule`
  exponent other than 1, gets `POLY_DEFAULT_TAKER_RATE` (0.07, the top
  category rate). A NaN or non-dict schedule also gets the default.
  The rate can only stay the same or go up, so candidates can only be
  rejected, never admitted. No change on the 5,296 live gamma records
  audited 2026-10-02.

## 0.5.9 — 2026-10-02
- New offline tool `scripts/analyze_capped_events.py` (analysis only, not
  in the 5-min scan). It sweeps the listing once with
  `kalshi_open_markets` (same rate limiting, sweep-plan cache left
  untouched), takes the events `screen_type_b` skips for having more than
  `MAX_EVENT_LEGS` outcomes, and records for each: `brackets_exhaustive`,
  the listing-ask price screen (`event_yes_legs` + `screen_edge_cents`,
  fee multiplier 1, the same math as `screen_type_b`), the outcome count,
  the strike-type signature, the title pattern and the series. It writes a
  JSON report (`--out`, default `data/analysis/`, gitignored) and prints a
  summary.
  Live run 2026-10-02 (131,610 markets, 30.7s, coverage complete,
  0 errors): 386 capped events (30,147 markets; matches
  `B_too_many_outcomes` exactly). Outcomes run from 41 to 400 (median
  60). 12 are exhaustive, all crypto range brackets (BTC, ETH, SOL, HYPE,
  XRP, BNB). 8 of those quote every leg, with yes-ask sums of 183–3,344c
  (best screen edge −93.6c); the other 4 are missing a Yes ask on at least
  one leg. 1 event passes the price screen: `KXNCAAMBUNDEFEATED-26` (43
  "will team X go undefeated" binaries, +7.0c). It is not exhaustive
  (zero teams can go undefeated) and the event has
  `mutually_exclusive: false`, so it is not an arb. 0 events are both
  exhaustive and price-passing. The capped cumulative ladders (12
  single-title `greater` events, e.g. NASDAQ-100 400-leg, crypto
  "above" ladders) are already screened by Type D, which has no outcome
  cap; their best pair is −0.14c. Conclusion: the cap hides no tradeable
  class. Keep `MAX_EVENT_LEGS` at 40 and re-run the tool if market
  structure changes.
- `screen_type_b`'s leg-building loop is extracted into
  `event_yes_legs(ms)` (one Yes leg per market at the listing ask, or None
  if any market has no Yes ask) so the tool reuses the screen's exact
  math. Behavior is unchanged: on a live listing (131,660 markets) and on
  20,000 randomized events, old and new `screen_type_b` returned
  identical candidates and stats. 9 new tests; 402 passed.

## 0.5.8 — 2026-10-02
- Type C `same_proposition`: the outcome-subset guard is now skipped for
  generic yes/no sub-titles (live binary markets use "Yes", which names no
  outcome). The old guard silently rejected 100% of true binary matches —
  the same defect PolySentry's improvement loop proved and fixed in its
  `xvenue_legs` port (0 legs before, 1 correct leg after). Named outcomes
  ("Yes, Trump") still enforce their outcome words; digit/qualifier guards
  unchanged. 4 new tests; 383 passed.
- New `xvenue_matches.json` feed: every completed sweep writes the
  surviving Type C matches (post-reverify, one record per matched pair)
  with both venues' screen-time quotes to the state dir. Consumed by
  PolySentry's paper loop as its `xvenue_poly` leg pool (the live caller);
  a data feed, not dedupe state — written even under `--no-mark`, and a
  write failure is recorded, never raised. 2 new tests; 383 passed.
- Tier 3 `_finalize_model_edge` now re-shrinks the model fair toward the
  FRESH orderbook mid. It used to re-check the lean on the fresh book but
  size EV on the screen-time `fair_taken_cc`, whose 0.40–0.60 midrange
  shrink was computed against the listing mid; when the mid fell between
  screen and finalize, the stale shrink overstated fair by up to
  0.25 × the move, and a marginal take could clear the 5c EV bar on a
  stale fair (e.g. fair_raw 0.60, mid 0.52 → 0.50, 51c ask: 5.24c stale
  vs < 5c fresh). The candidate is also dropped if the fresh shrunk fair
  flips the signal side. The fresh fair is written back to
  `cand["fair_taken_cc"]`, so the Tier 4 picker prices the same number.
  Candidates without `model.fair_raw` keep the old path. Same shrink rule
  as the screen, applied to fresher data; an unchanged book reproduces the
  screen values. Not strictly tighten-only: when the mid moves toward the
  take (e.g. the bid rises at an unchanged ask), the fresh fair can be up
  to 0.25 × the move higher, never above `fair_raw`; fees, depth/outlay
  gates and the 5c bar are unchanged.
  5 new tests (side flip, stale-vs-fresh separation, unchanged-book
  regression ×3, missing fair_raw fallback).
- `model_edge.market_window` fails closed when a listing row's 900s
  open/close times are not the ticker's own window (previously trusted
  as-is, which could price the wrong window against Kraken data).
  Tighten-only; live check 2026-10-02: 9/9 open windows match exactly.
  1 new test; the test fixture `window()` now derives its ticker from
  `w_close` (it paired wall-clock times with a fixed ticker).
- `screen_type_b` counts events skipped for having more than
  `MAX_EVENT_LEGS` outcomes in the new additive stats key
  `B_too_many_outcomes` (same pattern as `B_unverified_exhaustive`).
  Observability only; screening behavior unchanged. 1 new test; 391 passed.

## 0.5.7 — 2026-10-02
- Conservative hardening (fail closed). Expected live impact is zero.
- `brackets_exhaustive` now accounts for strict `less`/`greater` bounds.
  Kalshi contract language (`rules_primary`) confirms `less` caps and
  `greater` floors are STRICT while `between` is inclusive both ends. The
  old one-strike-step adjacency rule only holds for two inclusive bounds;
  with at least one strict bound the neighbour must meet with no gap
  (two strict bounds must overlap). The old code certified
  e.g. `less 3.19 / between 3.20–3.29 / greater 3.29` as exhaustive, but a
  settlement of exactly 3.19 pays nothing on any leg — a real
  false-positive path for Type B. The fix is tighten-only (every
  new-pass shape also passed the old rule; verified on 20,000 randomized
  bracket shapes); whole-degree live events still pass. 2 new tests, 1
  corrected (the old assertion certified the hole); 377 passed.
- `reverify_new` now DROPS new locked / Type C combos whose orderbooks
  cannot be re-fetched instead of keeping them marked unverified. The
  verify-before-alert promise now means something: a combo that cannot be
  re-checked within the budget is not output, not stamped in dedupe state,
  and not fed to the Tier 4 picker; a still-live combo is re-detected on
  the next 5-min sweep. `reverify_unverified` counts these drops;
  `reverify_dropped` keeps its old meaning (re-checked, edge vanished or
  book too thin). 4 new tests; test that asserted the old keep contract
  updated; a previously network-dependent picker test now stubs both
  orderbook fetches. 377 passed.

## 0.5.6 — 2026-10-02
- Conservative hardening (fail closed). Expected live impact is zero.
- Type D `confirm_ladder` now re-checks expiry at confirm time. Each live
  leg's `expiry_of` must be non-empty and string-equal to the expiry seen at
  screen time, and the two live legs must share one non-empty expiry.
  Before this, confirmation re-checked strike type and strike but not
  expiry, so a rescheduled or split-expiry event could pass the re-fetch.
- A failed Kalshi `/series` fee-multiplier fetch now drops the candidate.
  `kalshi_fee_multiplier` still returns 1.0 on failure, but it now records
  the series in `_FEE_MULT_FAILED`, and a later successful fetch clears it.
  `run_scan` drops every Tier 1/2/3 candidate that has a Kalshi leg in an
  unknown-multiplier series before `finalize`, because 1.0 could understate
  a surcharged series' taker fees. Polymarket legs are unaffected. The
  reverify path only sees legs that passed this filter.
- New additive stats keys, present only when non-empty:
  `fee_mult_unknown` (sorted list of series whose fetch failed this run) and
  `fee_mult_unknown_rejects` (`{"cands", "edge_cands", "model_cands"}` drop
  counts). No existing key changed. 7 new tests; 371 passed.

## 0.5.5 — 2026-10-02
- Type D hardening (validation-lane recommendation, iteration 9):
  `ladder_kind` now also requires one shared expiry across the event's
  markets (missing or differing expiries skip the event). The >= $1 locked
  guarantee assumes both legs settle on the same value at the same time;
  live samples showed homogeneous expiries, so this changes nothing on
  current market structure but closes the only theoretical false-positive
  path. One new test; 364 passed.
- New Tier 1 combo **Type D: cumulative-threshold ladder pairs**
  (`screen_type_d`, `confirm_ladder`). In a "below" ladder (every market
  `strike_type == "less"`), YES on the higher cap plus NO on the lower cap
  pays 100¢ or 200¢ on any settlement value. In an "above" ladder (every
  market `"greater"`), YES on the lower floor plus NO on the higher floor does
  the same. The reverse pairing can pay 0 and is never emitted. Type D is
  locked tier only. `locked_profit_cents` assumes the 100¢ minimum payout, so
  it is a conservative lower bound. The guarantee reads "cumulative threshold
  ladder; the two legs always pay >= $1 on the same settlement value".
- Detector guards (fail closed, the whole event is skipped): one
  `event_ticker` with at least 2 markets, one identical non-empty `title`,
  one shared expiry, ALL `"less"` with a numeric `cap_strike` or ALL
  `"greater"` with a numeric `floor_strike`, and pairwise-distinct strikes.
  Mixed, `"between"`, missing or non-numeric strikes, or missing/differing
  expiries all skip the event. Pairs are screened on
  listing asks with the same optimistic after-fee edge as Types A and B.
  Confirmation fetches the event and requires `mutually_exclusive` not
  `True`, both legs present and unresolved, and each leg's strike type and
  strike unchanged since the screen. Orderbook re-pricing, rounded-up fees,
  depth/outlay gates and the verify-before-alert re-check are unchanged and
  apply to D.
- Output: new `combo_type` value `"D"`, plus new `screen_candidates` stats
  `D`, `D_ladder_events` and `D_event_confirmed`. No existing key changed.
  Dedup keys use the existing Tier 1 format (`D|tickers|yes+no`).
- Live measurement 2026-10-02: 121 genuine ladder events. The best pair was
  at −0.14¢ after fees, so there are 0 live candidates. This class is priced
  tight, and the scanner now watches it.
- Tests: 16 new (363 passed) at ship; +1 expiry-equality guard test (364 passed).

## 0.5.4 — 2026-10-02
- Type C: `same_proposition`'s outcome-subset guard ("the Kalshi outcome is
  named on Polymarket") now compares accent-folded text (new `_nfkd_fold`:
  NFKD plus stripping combining marks). `normalize_title` turns a precomposed
  "é" into a space but keeps an ASCII or combining-form "e", so the venues
  spelled the same name differently: Kalshi `KXBALLONDOR-26-KMBA` has
  "Kylian Mbappe" while Polymarket has "Mbappé" ("mbappe" vs "mbapp"). Only
  this guard is folded. Jaccard, the inverted index, best-match selection,
  the digit and qualifier guards, the expiry gap, and the edge math are
  unchanged. A full fold of `normalize_title` was rejected because it drops
  real matches below `XVENUE_MATCH_MIN`. Folding hits both token sets the
  same way, so a pair that passed before keeps passing. The one exception
  would be a word that only matched as a precomposed-accent fragment
  ("José" -> "jos" vs a literal "Jos").
- Live check 2026-10-02 (130,907 Kalshi / 1,714 Polymarket markets, 0
  errors): of 378 best matches at or above the jaccard minimum, 24 now pass
  the guard and 0 stop passing. Only the 2 Ballon d'Or pairs (KMBA, ODEM)
  are within the 48h expiry gap. Their best after-fee edges are +0.64c and
  -0.78c, below the 4c Tier 2 floor, so there are 0 new live candidates. The
  other 22 (Brazil, France, Venezuela, Romania) fail the expiry gap. Some of
  them are wrong matches the accent bug used to hide:
  `KXBRPRESIDENT4/5-*` ("finish 4th/5th") best-match Polymarket's "3rd place"
  market, because ordinals like "4th" are neither digit tokens nor
  qualifiers. Only the 30-day expiry gap blocks them today.
- Type C: `same_proposition`'s number guard now treats ordinals ("4th",
  "3rd"; new `_num_tokens`) as numbers. This blocks placement mismatches
  such as Kalshi "finish 4th/5th" vs Polymarket's "3rd place" market. The
  check is strictly tighter: a pair rejected before stays rejected, and pairs
  whose ordinals agree (Kalshi "finish 3rd" vs Polymarket "3rd place") still
  pass. Live check 2026-10-02 (130,909 Kalshi / 1,723 Polymarket markets, 0
  errors; 378 best matches at or above the jaccard minimum, 107 -> 99
  passing): 8 pairs are newly rejected and 0 newly pass. All 8 are
  `KXBRPRESIDENT4/5-BRPRES26-*` (`FBOL`, `PMAR`, `RSAN`, `RZEM`) matched to
  the "3rd place" market, with a 30.5-day expiry gap. This includes the 4
  accent-hidden pairs above. `RSAN`/`RZEM` have plain-ASCII names and were
  already passing before the accent fix. All 5 `KXBRPRESIDENT3-*` pairs still
  pass. No live candidates change.
- Tests: 7 new (347 passed).

## 0.5.3 — 2026-10-02
- Tier 4 picker: the `same_game` correlation flag now compares only the
  date and teams of two legs' `game_key`s (new `combo_picker.game_tail`),
  not the league code. The league code is the ticker's alpha run unless it
  starts with one of the 11 `SPORTS_PREFIXES`, so in every other league the
  market-type series of one game (`KXAFCONGAME-26OCT06ANGMAW` vs
  `KXAFCONSPREAD-26OCT06ANGMAW`) got different keys and were priced as
  independent with no haircut. Live check 2026-10-02: 2,747 of 3,816 Kalshi
  sports series sit outside `SPORTS_PREFIXES`. Across 444 open events from
  those leagues there were 315 same-game cross-market-type event pairs, and
  0 were flagged. All 315 are now flagged. Strictly additive: every pair
  flagged before is still flagged, and the haircut, cap, joint floor and
  JSON shape are unchanged. Series with digits in the prefix (`...1HSPREAD`)
  still don't parse to a `game_key` (unchanged).
- Tier 4 picker: `price_combo` computes the parlay price exactly
  (`fractions.Fraction`) before flooring the $10 contract count. The float
  product overshot exact prices (`100*.05*.80 = 4.000000000000001`) and
  dropped one contract. That hit 11 of 4,851 two-leg integer-ask pairs, for
  example asks 4c+79c gave "$10 pays $249" instead of $250. The stake still
  never exceeds $10, and depth must now cover the corrected (never smaller)
  contract count. Fees, bars and JSON shape are unchanged.
- Tests: 6 new (340 passed). Live `--no-mark` sweep (temp state/cache):
  134,129 markets, 25.2s, coverage complete, 0 errors, 0 picker legs/picks.

## 0.5.2 — 2026-10-01
- Tier 4 picker leg pool now also accepts the Kalshi leg of confirmed Tier 1
  locked Type C cross-venue pairs, not just Tier 2 edge pairs. Same
  `xvenue_poly` fair (`100 - Polymarket opposite-side ask`, a lower bound on
  the taken side's win probability in an uncrossed Poly book), same 5c
  per-leg bar, same depth/TTL gates and fee math; a locked pair's >= 8c
  after-fee gap clears the 5c bar by construction. Type A locked pairs stay
  excluded (their `100 - no_ask` "fair" is the YES bid, a crossed-book
  artifact) and so do Type B books (no per-leg fair from a sum edge). No
  threshold, fee, depth/outlay, flag or JSON-shape change. Live behavior is
  unchanged (zero Type C pairs live). Full suite green (334 passed).

## 0.5.1 — 2026-10-01
- Doc-only: `fetch_kraken_for_windows` docstring in `scripts/combo_scan.py`
  said Kraken calls go through "plain requests.get"; they actually go through
  `http_get_json` with a per-host pooled Session on the shared semaphore
  (per the egress-proxy lesson in AGENTS.md). No behavior, fee, threshold,
  output, or performance change. Full suite green; live `--no-mark` sweep
  unchanged (0 combos, honest zero baseline preserved).

## 0.5.0 — 2026-10-01 — Tier 4 combo picker (paper only)
Adds Kalshi-app-style combo picks ("Pick 2+ markets -> $10 pays $X") as a
new, additive Tier 4. Tier 1/2/3 detection, thresholds, fees, ranking,
depth/outlay gates and every existing JSON field are unchanged.

- **New module `scripts/combo_picker.py`** (pure: no HTTP, no threads, no
  API clients; only `log_pick` writes to disk). It builds 2-4 leg parlays from
  confirmed edge legs, using beam search: a pool of the top 60 legs by edge,
  canonical subsets, the top 200 per size, and only passing combos are
  extended.
- **Leg sources (phase 1 only):** confirmed Tier 3 model-edge legs
  (`kraken_model`, fair = model fair of the taken side) and the Kalshi leg of
  confirmed Tier 2 Type C edge pairs (`xvenue_poly`, fair = 100 - Polymarket
  opposite-side ask). Each leg must individually clear fair - ask >= 5c, have
  depth >= 1, and expire >= 5 min out. No title parsing, no other sources;
  ESPN, VoteHub, Stooq and nowcast legs are phase 2.
- **Joint fair:** the product of the leg fairs. Hard rejects: same event
  ticker, duplicate ticker. Pairs are flagged when they are the same game
  (same `game_key`, or same sports league with an unknown game) or are
  opposite-direction 15-minute crypto takes. Each flagged pair costs 4
  percentage points of joint fair; more than 2 flagged pairs, or a joint
  below 2%, rejects the combo. `game_key` maps market-type variants (GAME,
  SPREAD, TOTAL) of one league to the same league code, so legs from two
  markets on one game are flagged rather than treated as independent.
- **Pricing (true parlay):** each leg ask gets a 1c guard, because the RFQ
  maker margin is unknown and not constant. The parlay price is the product
  of the guarded asks. Size is the number of whole contracts $10 buys. The
  fee is `max(one combo-order taker fee, sum of per-leg taker fees)`, using
  `taker_fee_cents` / `leg_fee_cc` from `combo_scan.py` verbatim (rounded
  up). A pick needs an expected edge of >= 50c on the $10 reference, and
  every leg's depth must cover the contract count. Paper sizing is
  quarter-Kelly on a $1,000 paper bankroll, clamped to $2-$10.
- **Output:** new keys `combo_picks_found` (all picks this run) and
  `new_combo_picks` (deduped through the existing state file under
  `picker|P|TICKER:SIDE,...` keys, which never collide with Tier 1/2/3 keys).
  There is one stderr summary line, plus one `PICK: ... -> $10 pays $X (fair
  $Y, edge +Zc)` line per new pick. Every pick carries
  `guarantee: "MODEL estimate — NOT guaranteed; fair-value model vs market"`.
- **Backtest log:** on marking runs (not `--no-mark`), each new pick is
  appended to `data/picker_picks.jsonl`, with the full leg detail, pricing,
  `priced_at` and `commit`, so phase 2 can grade the picks against Kalshi
  settlement.
- **Flags and budget:** `--combo-picks / --no-combo-picks` (default on). With
  `--no-combo-picks` the output is identical to 0.4.1. The picker runs after
  Tiers 1-3 and only inside `RUN_HARD_BUDGET_S`; when skipped it sets
  `stats.picker_skipped_budget`. A picker exception becomes an `errors`
  entry and never blocks Tier 1-3 output or state saving.
- **Safety:** paper/analysis only, with no orders and no credentials. A
  module-level `_WRITE_BLACKLIST` plus `guard_no_write(url, method)` refuses
  every write method and the Kalshi combo-market creation, RFQ and quote
  paths and the Polymarket `/v1/maker/*` paths. The picker makes no HTTP
  calls at all.
- **Limits:** picks are rare by construction. Two legs at the minimum +5c
  edge pass at asks of about 66c or higher (verified against
  `price_combo`/`price_passes`; the 1c-per-leg maker guard means cheap
  legs make the same cent edge large in relative terms), and +8c legs
  pass from asks of about 7c up. The live 2026-10-01 sweep had 0
  qualifying legs, so both lists were `[]`, which is an honest empty
  result.
- Tests: `tests/test_combo_picker.py` (45 tests). Full suite: 302 passed.

## 0.4.1 — 2026-10-01
- Tier 2 Type C edge entries now require a strictly positive after-fee edge
  per set (was >= 0): pairs whose cent-rounded taker fees exactly erase the
  edge are no longer flagged (they previously reported edge_cents_per_set =
  0.0). Aligns Tier 2 C with Tier 1, which already required profit > 0.
  Thresholds, fee handling, depth/outlay gates, screening, and JSON output are
  unchanged.

## 0.4.0 — 2026-10-01 — Faster sweep, coverage proof, verify-before-alert
- **Merge-integration fixes (post-merge verification, same day).** The Tier 3
  + 0.4.0 merge had accidentally duplicated the `run_scan(args, errors)` call
  in `main()`, so every sweep ran twice (doubling API load and Kraken calls);
  removed the duplicate. Also made `tests/test_model_edge.py`'s window setup
  wall-clock independent (it was flaky in the first ~4 min of each 15-min
  window) and taught its on/off JSON comparison that the new fee-multiplier
  stats counters legitimately differ when the model tier adds a series.
  Full suite: 255 passed; live `--no-mark` sweep: 130,349 markets in ~19–24s,
  `coverage_complete=true`, 0 errors.

Speed and data layer only. Fees, depth and outlay gates, Tier 1/2 detection,
thresholds, ranking and existing output fields are all unchanged; every
output change is a new field.

- **The sweep can no longer silently lose coverage.** A baseline run scanned
  only 50,060 of about 130k markets in 48.6s: 10 ranges hit the 45s budget
  with 0 pages each. Ranges that haven't finished by the main budget
  (`SWEEP_BUDGET_S`, now 30s) get one **rescue pass** (`RESCUE_BUDGET_S` = 10s),
  split finer. Anything still unfinished is listed in
  `stats.ranges_budget_hit` and `errors`, and `coverage_complete` is false.
- **Sweep plan cache.** A complete sweep saves balanced close-time shards of
  about 3,000 markets each (46 shards on 2026-10-01) to
  `hidden_files/cache/sweep_plan.json`, with a 24h TTL. The next run pages
  those shards, longest first, with no split overhead. A missing, stale or
  invalid plan falls back to the old seeds. Plans must tile the whole
  close-time line to be used, and partial sweeps never write one.
- **Order-aware splitting.** The listing returns a range's rows by close time,
  descending. This was checked live on about 22k rows and is re-checked on
  every range. While that order holds, a long range splits its *unseen*
  remainder `[lo, c]` (where `c` is the last close time seen), so the halves
  never re-fetch pages already read. The last second `c` gets its own range so
  a heavily shared close time is re-paged at most once. If the order doesn't
  hold, the old full-range median split is used. Planned shards re-split after
  8 pages (`RESPLIT_AFTER_PAGES`) or 8s (`RANGE_SLICE_S`), and seeds split after
  1 page. `MAX_RANGE_SPLITS` went from 64 to 128 (plus 64 for the rescue pass),
  and `MAX_PAGES_TOTAL` from 300 to 400.
- **Point masses are pre-split.** Seed ranges containing 2027-11-03 14:00Z
  (about 10k markets) or 15:00Z (about 9k) become `[lo, ts-1]`, `[ts, ts]`,
  `[ts+1, hi]`, and they're paged first.
- **10 listing workers** (`LISTING_WORKERS`, up from 4), with the HTTP semaphore
  scaled to 11 (one slot for the Polymarket thread). That's about 7 pages/s,
  far under Kalshi's 20 req/s Basic read limit, and live runs saw no 429s.
  Detail fetches stay on 4 threads (`IO_WORKERS`). Listing pages use a 10s
  timeout and 3 retries, and no retry starts past the pass deadline, so one
  stalled page can't eat the budget.
- **Connection reuse everywhere.** `http_get_json` now defaults to
  `pooled=True`, with one Session per thread **per host**. Kalshi, gamma and
  CLOB each get their own, and a Session is never shared across hosts. Detail
  calls (orderbook, event, series) run on a persistent 4-thread pool, so their
  sessions survive across phases.
- **Series fee-multiplier disk cache:** `hidden_files/cache/series_fee_mult.json`,
  24h TTL. Only successful live fetches are cached; a failed fetch still
  returns 1.0 for this run and is never cached. Cached values are floored at
  1.0. Any cache error falls back to a live fetch.
- **Verify-before-alert.** NEW Tier 1 combos and NEW Type C edge combos get one
  fresh orderbook fetch per leg right before output, and the same depth/edge
  evaluation runs again. Combos whose edge vanished or that no longer fill
  the minimum outlay are dropped. They aren't stamped in dedup state, so they
  alert if they come back. Re-checked combos carry `"depth_reverified": true`.
  If the books can't be fetched within `REVERIFY_BUDGET_S` (10s, and never
  past 58s into the run), the combo is kept with `"depth_reverified": false`
  and the skip is logged in `errors`. Type A near-arb edge entries are
  informational and aren't re-fetched.
- **New output fields:** `coverage_complete` (true/false, or null when there
  are no sweep stats) and `stats`: `markets_scanned` (raw listing rows),
  `markets_unique`, `pages_fetched`, `ranges_completed`, `ranges_retried`,
  `ranges_budget_hit`, `sweep_seconds` (listing phase), `sweep_plan`,
  `range_splits`, `plan_saved` / `markets_unscanned_est`, `fee_mult_cache_*`
  and `reverify_*`. One summary line goes to stderr, for example
  `combo_scan sweep complete | unique 130,313 (rows 130,313) | pages 143 | …`.
- **Websocket investigated, not built.** Kalshi's websocket requires API-key
  authentication even for public channels; see the README.
- Live `--no-mark` results, 2026-10-01:
  - Baseline: 50,060 markets in 48.6s, with 10 ranges lost.
  - Cold start, no plan: 130,317 markets in 33.4s, 268 pages, complete.
  - Cached plan: 130,313 markets in 20.3s, 143 pages, 0 duplicate rows,
    complete, 27.1s wall.
  - Merged with 0.3.0 (Tier 3 on): 130,321 markets, 18.7s sweep, 24.0s wall.
- Tests: 29 new tests in `tests/test_speed_data.py`, plus `tests/conftest.py`,
  which points every test at a temporary cache directory. One existing
  assertion now allows the new `depth_reverified` field. The suite is 196
  tests on top of 0.2.1, or 245 together with 0.3.0's Tier 3 tests. That
  needs `"stats"` added to `VOLATILE` in `tests/test_model_edge.py`, because
  `stats` is per-run diagnostics.

- **Merged with Tier 3 (0.3.0).** The faster sweep, fee-multiplier cache and
  verify-before-alert cover all tiers; Tier 3 model-edge entries keep their
  own orderbook re-check inside the model pass and pass through
  verify-before-alert untouched (informational for that step).

## 0.3.0 — 2026-10-01 — Tier 3 "model-edge"

- **New Tier 3, `"tier": "model-edge"`, `combo_type: "M"`.** These are
  directional bets on live Kalshi 15-minute crypto up/down windows
  (`KX{SYM}15M-*`, for BTC ETH SOL XRP DOGE HYPE ZEC NEAR BNB), priced against
  public Kraken spot. They're labelled
  `"MODEL estimate — NOT guaranteed; fair-value model vs live spot"`, and
  nothing in this tier is ever called "locked". On by default;
  `--no-model-edge` turns it off.
- **Model:** new module `scripts/model_edge.py`, ported from predict-bot's
  `scripts/same_day_loop.py` and copied, not imported.
  - `fair_raw = Phi(log(p_now / p_ref) / (sigma·√t))`, then
    `shrink_midrange` (α 0.25, band 0.40–0.60).
  - Also ported: agreement filter, 3-minute warmup, ticker parsing in New York
    time, the Kraken fetchers, and a per-run `KrakenCache`.
- **Entry rule, as validated:**
  - The taken side's mid is ≥ 0.5.
  - `fair_taken − ask_taken ≥ 5¢` against the ask, not the mid.
  - At the orderbook price: the agreement filter is re-checked on the fresh
    book, the usual depth and outlay checks apply ($10 minimum, $20 maximum),
    and expected value after cent-rounded Kalshi taker fees is ≥ 5¢ per
    contract.
  - New `evaluate_model_edge` reuses `_fill`, `_sizing`, `combo_cost_cc` and
    `leg_fee_cc`. There is no new fee math.
- **Spot freshness:** Kraken `Trades` returns at most 1000 trades. A live
  check found BTC's last returned trade 416s stale, a problem the predict-bot
  original also has. `p_now` is now the newer of the last trade and the
  latest 1-minute candle close, and any spot older than 180s is refused.
- **Cost and isolation:**
  - No extra Kalshi listing fetch.
  - Kraken is called only for pairs that have a live window on which a take
    is still possible (`model_take_possible`), two calls each, through the
    shared semaphore with a plain `requests.get`. Each call takes 1–2s through
    the proxy, so 9 pairs take about 7–8s. The fetch runs in the background
    from the end of the listing.
  - The scan stops waiting for Kraken at 53s (`MODEL_KRAKEN_BUDGET_S`).
  - Tier 3 runs only after all Tier 1/2 orderbooks are in. Its own books
    (`MAX_MODEL_ORDERBOOK_FETCHES = 60`) stop at the 55s edge-detail budget.
- **Output and dedup:**
  - Two new top-level fields, `model_edge_combos_found` and
    `new_model_edge_combos`.
  - `combos_found`, `new_combos` and `repeat_combos_count` keep their Tier 1/2
    meaning, so the 5-minute loop's alerts don't change until its prompt
    learns about Tier 3.
  - New `screen_candidates` stats: `M_live_windows`, `M_kraken_pairs`,
    `M_no_fair` and `M`. They're present only when the tier is on.
  - Dedup keys look like `model-edge|M|<ticker>|<YES|NO>`.
- **Backtest:** new `scripts/backtest_model_edge.py`. It reads predict-bot's
  `signal_grades.jsonl` read-only and makes no network calls. Fills are at the
  ask (mid plus half of an assumed 4¢ spread), with Kalshi taker fees rounded
  up and a $15 stake. On the 168 Kalshi-settled signals graded on 2026-10-01:
  - Overall: **21 wins out of 24 takes, +0.251 $/$ net**.
  - First half: 9/10, +0.242. Second half: 12/14, +0.258.
  - Threshold sweep on the first half: 3¢ +0.084, 5¢ +0.242, 7¢ +0.174.
  - Placebo (the same takes with zero true edge): −0.051 $/$.

  A row graded since then brings the current file to 169 rows: 22/25, +0.271.
  This is one day of data with a small n, so expect regression.
- Tier 1, Tier 2, fees, caps, `rank_combos` and every existing field are
  unchanged. With `--no-model-edge`, a run makes no Kraken calls and produces
  the same output as before apart from two zeroed fields.
- Tests: new `tests/test_model_edge.py` with 59 tests, 226 in total. The
  original `tests/test_combo_math.py` is unmodified.
- Files: `scripts/model_edge.py` (new), `scripts/backtest_model_edge.py` (new),
  `scripts/combo_scan.py`, `tests/test_model_edge.py` (new), `README.md`,
  `CHANGELOG.md`.

## 0.2.1 — 2026-10-01 — Type A Tier 2 redefined as pre-fee near-arb

- **Fix:** the 0.2.0 Type A edge rule (combined after-fee price ≤ 101.5¢)
  flagged only guaranteed losers. A Yes+No pair on one market always pays
  exactly 100¢, so anything priced above 100¢ plus fees can't profit. A live
  sweep listed 38 of them, all at `edge_cents_per_set = -0.21`.
- **Type A edge is now a pre-fee near-arb:** the combined ask before any fees is
  strictly < 100¢ (`A_NEAR_ARB_MAX_ASK_CENTS = 100`, replacing
  `A_EDGE_MIN_CENTS`), excluding pairs that are already Tier 1 locked. These
  would lock a profit if fees were lower (for example, maker orders). At
  current taker fees they lose, and the output says so.
  - `screen_type_a(markets, near_arb=False)` replaces the `edge_floor`
    parameter. The Tier 1 branch is unchanged.
  - `evaluate_edge(..., pre_fee=True)` runs the same depth and outlay checks
    and reports `edge_cents_per_set` as the **pre-fee** edge (always > 0). A
    pair whose orderbook price is back at or above 100¢ is dropped.
  - Guarantee text: "near-arb — profitable before taker fees; current taker
    fees erase the edge; needs lower fees (e.g. maker) to be actionable".
  - Candidates are capped by largest pre-fee edge first. In output, Type A
    near-arbs rank after Type C edges, by pre-fee edge descending.
- Type C edge, Tier 1, fees, caps, dedup keys, `--edge-tier` and the output
  fields are unchanged.
- Live: 130,427 markets in 35.3s; 0 Tier 1 and 0 Tier 2 candidates. A pre-fee
  sum under 100¢ needs a crossed Kalshi book, so Type A edge is expected to be
  rare.
- Tests: the degenerate 101.5¢ boundary tests were replaced with pre-fee ones
  (99.9¢ flagged, 100.0¢ rejected, Tier 1 still locked). 167 tests in total;
  the original 144 are unmodified.

## 0.2.0 — 2026-10-01 — Tier 2 "edge" layer

- Added Tier 2 **edge** combos alongside the Tier 1 **locked** ones. Tier 2 is on by
  default; `--edge-tier` / `--no-edge-tier` controls it. With `--no-edge-tier` the
  scan behaves exactly as before, apart from the new labels and zero counts.
  - **Type A edge:** Yes+No pairs with a combined after-fee price ≤ 101.5¢
    (`A_EDGE_MIN_CENTS = -1.5`). The existing `screen_type_a` loop takes an
    `edge_floor` parameter. Pairs that already qualify for Tier 1 are excluded.
  - **Type C edge:** cross-venue gaps of 4¢ or more (`XVENUE_EDGE_MIN_CENTS = 4`)
    and under 8¢. The locked bar is unchanged at 8¢.
  - **Type B:** Tier 1 only, unchanged.
- New `evaluate_edge()` applies the same depth and outlay gates as
  `evaluate_combo()`, which is unchanged: shared sizing helpers, same fields,
  same order. The per-set edge floor after cent-rounded fees is −1.5¢ for A and
  0¢ for C. `--min-roi` applies to Tier 1 only. Fee handling is unchanged and
  still conservative.
- Output: every combo gets `"tier"`. Edge entries leave out
  `locked_profit_cents` and `roi_pct`, add `edge_cents_per_set`, and carry the
  guarantee "edge only — NOT a locked profit; edge can vanish with
  fees/slippage". The locked and edge lists are ranked separately, with locked
  first. New top-level fields `edge_combos_found` and `new_edge_combos`, and new
  stats `A_edge` and `C_edge`. All existing fields are unchanged, and
  `combos_found` still counts Tier 1 only.
- Dedup: Tier 1 keys keep the exact old format. Tier 2 keys are prefixed
  `edge|`.
- Caps: 40 depth-checked candidates per (type, tier). Tier 2 orderbooks (up to
  120) are fetched after Tier 1's and are skipped once the run passes 55s.
  Measured at 46.5s live with the edge tier on, versus 36.9s off.
- Tests: 19 new tests, 163 in total. The original 144 are unmodified.

## 0.1.0 — 2026-10-01 — initial build

- New `scripts/combo_scan.py`, a paper-only all-market Kalshi combo scanner.
  It looks for three kinds of combo:
  - **A:** single-market Yes+No.
  - **B:** mutually-exclusive event books. These must be proven exhaustive:
    the strikes tile the number line and the event is `mutually_exclusive`.
  - **C:** Kalshi vs Polymarket, at least 8¢ after fees, using the title
    matching from `same_day_loop.py` plus guards for sub-titles, numbers and
    qualifiers.
- Fees: Kalshi taker fee `round up(M × 0.07 × C × P × (1−P))` from the
  official schedule **"Fee Schedule for July 2026 – 7.7.26 Update"**
  (<https://kalshi.com/docs/kalshi-fee-schedule.pdf>, effective 2026-07-07,
  verified 2026-10-01). The fee is rounded up to a whole cent per order leg,
  and the series multiplier is never taken below 1. The schedule lists no
  settlement fee. `fees_model = "kalshi-taker-2026-07-07"`.
- Polymarket fees are **not** zero any more. The scanner charges
  `C × rate × p × (1−p)` using each market's `feeSchedule.rate`
  (<https://docs.polymarket.com/trading/fees>, checked 2026-10-01).
- Sizing: `$20` maximum and `$10` minimum outlay. The scanner reads
  top-of-book depth for every leg, then computes
  `sets = min(depth_sets, affordable_sets)`. It flags a combo only when after-fee
  profit is above 0 and the book can fill the minimum outlay.
- Sweep: about 131k open non-multivariate markets, split into adaptive
  close-time ranges across 4 concurrent workers. Requests back off with jitter.
  Measured at 36–38s.
- Dedup state lives in the goal's `hidden_files/state/`. It is written
  atomically, with a `.bak` copy to fall back on. `--no-mark` never writes it.
- Tests: 144 offline pytest cases in `tests/test_combo_math.py`.
