# Architecture

`combo_scan.py` is a single-shot CLI built for a cron loop. Each run sweeps
the open Kalshi market listing, screens it for four kinds of opportunity,
confirms candidates against live orderbooks, builds combo picks from the
confirmed legs, dedupes against a local state file, and prints one JSON
object to stdout. It always exits 0. Partial failures land in `errors`.

Everything is paper/analysis only. Every HTTP call is an unauthenticated
public `GET`.

## Pipeline

```
listing sweep ──► screens (Tier 1 / 2 / 3) ──► confirm (orderbooks, sizing)
      │                                              │
      │                                              ▼
      └──► Polymarket + Kraken fetches      dedup ──► verify-before-alert
                                                     │
                                                     ▼
                                    Tier 4 combo picker ──► JSON output
```

1. **Listing sweep.** `GET /markets?status=open&mve_filter=exclude` pages
   about 131k open markets. Multivariate combo markets, which number in the
   millions, are excluded. One cursor chain takes about 100s, so the
   listing is split into disjoint close-time ranges (`min_close_ts` /
   `max_close_ts`, inclusive, integer seconds) and paged by 10 workers. A
   complete sweep saves a balanced shard plan (about 3,000 markets per
   shard, 24h TTL) to the cache directory. The next run pages that plan. If
   the plan is missing or invalid, coarse seed ranges are used, with known
   close-time point masses pre-split. Ranges unfinished at the main budget
   get one finer-split rescue pass.
2. **Side fetches.** In parallel with the listing, a background thread
   fetches Polymarket events from gamma (top 3,000 by 24h volume, binary
   markets with at least $5k volume). For Tier 3, it fetches Kraken trades
   and 1-minute candles, but only for crypto pairs that have a live
   15-minute window where a take is still possible.
3. **Screens.** These run on listing prices only:
   - **Tier 1 (locked):** single-market Yes+No (Type A), exhaustive
     mutually-exclusive event books (Type B), and Kalshi-vs-Polymarket
     cross-venue pairs at least 8¢ under 100¢ after fees (Type C).
   - **Tier 2 (edge):** pre-fee Yes+No near-arbs (A) and cross-venue gaps
     from 4¢ up to 8¢ (C).
   - **Tier 3 (model-edge):** 15-minute crypto windows where a fair-value
     model beats the ask by at least 5¢.
4. **Confirm.** Orderbooks (`GET /markets/{t}/orderbook`, plus the CLOB book
   for Polymarket legs), events and series fee multipliers are fetched only
   for candidates. Each candidate is re-priced at the book, sized against
   top-of-book depth and the outlay window ($10–$20 by default), and charged
   cent-rounded taker fees.
5. **Dedup and verify-before-alert.** New combos are those whose key was
   never alerted before. New Tier 1 combos and new Type C edge combos get
   one fresh orderbook fetch per leg right before output. A combo whose edge
   vanished is dropped and not stamped in state.
6. **Tier 4 combo picker.** Confirmed Tier 3 legs and the Kalshi legs of
   confirmed Type C edge pairs become 2–4-leg parlay picks. The picker makes
   no HTTP calls. See [combo-picker.md](combo-picker.md).
7. **Output.** One JSON object goes to stdout. One coverage summary line,
   plus one line per new pick, goes to stderr.

## Module map

| Module | Role |
| --- | --- |
| `scripts/combo_scan.py` | CLI entry point: sweep, Tier 1/2 screens, Polymarket matching, orderbook sizing, fees, dedup state, verify-before-alert, output |
| `scripts/model_edge.py` | Tier 3: zero-drift Brownian fair value for 15-minute crypto windows, Kraken fetchers, per-run cache |
| `scripts/combo_picker.py` | Tier 4: leg pool, correlation-aware joint fair, parlay pricing, beam search, paper sizing, pick log. Pure apart from `log_pick` |
| `scripts/scanner_env.py` | Optional `.env` loading and `COMBO_SCANNER_*` path resolution |
| `scripts/backtest_model_edge.py` | Offline replay of the Tier 3 entry rule on a graded-signals file, which is not shipped |
| `demo/run_demo.py` | Offline picker demo on synthetic fixture legs |

## Data flow and files

| Path (default) | Written by | Contents |
| --- | --- | --- |
| `data/state/combo_scanner_state.json` (+ `.bak`) | marking runs only | alerted dedup keys with expiry; atomic write, previous copy kept |
| `data/cache/sweep_plan.json` | complete sweeps | close-time shard plan (24h TTL) |
| `data/cache/series_fee_mult.json` | live fee fetches | series fee multipliers (24h TTL, floored at 1.0, failures never cached) |
| `data/picker_picks.jsonl` | marking runs only | append-only log of new combo picks (see [backtest-protocol.md](backtest-protocol.md)) |

`--no-mark` writes no state and no pick log. Caches may still refresh.
Every path can be overridden; see the configuration table in the README.

Dedup keys: Tier 1 keys are `(combo_type, sorted tickers, side)`, written as
`A|T,T|yes+no`. Tier 2 keys get an `edge|` prefix, Tier 3 keys are
`model-edge|M|<ticker>|<side>`, and Tier 4 keys are
`picker|P|TICKER:SIDE,...`. The prefixes keep the tiers from ever colliding.

## Budgets (one run stays under 60s)

| Constant | Value | Meaning |
| --- | --- | --- |
| `SWEEP_BUDGET_S` | 30s | main listing pass |
| `RESCUE_BUDGET_S` | 10s | one rescue pass for unfinished ranges |
| `MODEL_KRAKEN_BUDGET_S` | 53s | stop waiting for Kraken |
| `EDGE_DETAIL_BUDGET_S` | 55s | no Tier 2/3 orderbook fetch starts after this |
| `REVERIFY_BUDGET_S` | 10s | verify-before-alert window, never past 58s |
| `RUN_HARD_BUDGET_S` | 58s | nothing optional, including the picker, starts after this |

Other limits:

- HTTP concurrency is capped at 11 calls in flight, about 7 listing pages
  per second, well under Kalshi's documented Basic read limit.
- Each (type, tier) pair gets at most 40 depth-checked candidates.
- Sessions are per thread and per host; they are never shared across hosts.

## Coverage proof

`coverage_complete` is `true` only when every close-time range finished. It
is `false` when any range is still unfinished after the rescue pass, and
`null` when no sweep stats exist. `stats` lists the raw and unique market
counts, pages, ranges, any `ranges_budget_hit`, and cache and re-verify
counters, so a short sweep can never pass silently as a full one.

## Fees

- **Kalshi taker fee:** `round up(M × 0.07 × C × P × (1 − P))`, from the fee
  schedule effective 2026-07-07. It is rounded up to a whole cent per order
  leg, which is coarser than Kalshi's centicent rounding and therefore
  conservative. The series multiplier `M` is never taken below 1. No
  settlement fee is modeled, because the schedule lists none.
- **Polymarket:** fee-enabled markets are charged `C × rate × p × (1 − p)`,
  using each market's `feeSchedule.rate`. A fee-enabled market with no
  stated rate is charged 0.07, the highest category rate.
- **Pricing assumptions:** every leg is priced as an immediately matched
  taker order. Fees are never loosened.

## Websocket (investigated, not used)

Kalshi's websocket requires API-key authentication even for public
channels. This project uses no credentials, so it polls the REST listing.
The cached shard plan keeps a full sweep at roughly 20–30s.

## Running on a schedule

```
*/5 * * * * cd /path/to/kalshi-combo-scanner && python3 scripts/combo_scan.py >> runs.jsonl 2>&1
```
