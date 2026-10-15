# W2 staff review — coordinated/bundled ownership detection

## What was built

A Solana-only, pure-stdlib, offline pre-entry screen (`analysis/w2_bundle_detect/detector.py`):
`detect(snapshot, rule=None)` returns `{risk_score, veto, rule, features, status}` and **never
raises**. Twelve candidate veto rules from the research (top1/top5 thresholds, top5−top1
spread as a "disguised distribution" proxy, first-10-buyer and launch-block-buyer concentration,
wash-score) are predeclared but **disabled by default** — with no rule, veto is always False.
`calibrate.py` rebuilds `verdict.json` from the journal and **refuses** to write a verdict once
enough evidence exists (raises, forcing a real calibration pass). `wash_score` is an explicit
stub returning None: the journal has aggregate buy/sell counts, not per-wallet matched
identical-amount pairs, so the MELT bundle-trace signal is not reconstructible from our data.

## Independent verification (coordinator, not the implementer)

- **Full suite: 179 passed, 0 failed** (repo venv, 27.3s). Lane discipline confirmed via
  `git status`: only `analysis/w2_bundle_detect/` and `tests/test_w2_bundle_detect.py` added;
  `fomo_trader.py`, config, and `runs/` untouched.
- **Latency (10k+ iterations per input class, realistic inputs):** full-evidence p50 0.0054ms /
  p99 0.0128ms / max 0.89ms; partial-evidence p50 0.0055ms / p99 0.0149ms / max 2.28ms;
  malformed p50 0.0036ms / p99 0.0084ms / max 0.15ms. The 50ms budget holds with 20–9000x
  headroom. **Memory:** 0.7 KiB peak over 50k calls — no per-call state.
- **Adversarial fuzz (20k random malformed inputs:** NaN/±inf/−5/101/1e999/"NaN"/bools/
  lists/dicts/objects as fields; non-dict snapshots; unknown rules; wrong-case chains):
  **0 raises**, 19,858 fail-open fallbacks. **Log discipline:** 500 fallback calls produced
  exactly 500 "W2 FALLBACK" lines — every fallback explicit, logged, counted.
- **Concurrency audit:** module is a stateless pure function. Only module-level state is
  `LOG`, `CANDIDATE_RULES` (single definition, never mutated — grep-verified), and imports.
  Python logging is thread-safe. 8 threads × 5k concurrent calls: 0 errors. **Race-free by
  construction; no locks needed.** `calibrate.py` is single-threaded offline tooling writing
  only its own `verdict.json`. Integration caveat: a future caller must pass the rule
  explicitly and must never mutate `CANDIDATE_RULES`; the scan/manage loops share nothing
  with this module.
- **Look-ahead audit (written, not assumed):**
  - `holder_top1_pct`/`holder_top5_pct` are captured inside `rug_check`
    (`fomo_trader.py` ~L1152), which runs **before the entry commit** — strictly prior to
    the decision point a W2 veto would gate. No look-ahead, *provided the screen reads the
    same pre-commit evidence dict*.
  - `calibrate.py` gates candidate rules on `entry_record` fields only; outcomes come from
    close records. No close-time field feeds any veto decision. Chronological split uses
    journal append order (avoids mixed-tz parsing).
  - Pre-round-3 entries have null holder fields, so no retroactive fitting is possible —
    this is why the verdict is INCONCLUSIVE rather than negative.
  - `first10_buyers_pct` / `launch_block_buyers_pct` are not captured anywhere yet; if
    added later they must be captured pre-commit to preserve this audit.

## The three strongest objections

**1. "You built a detector with zero validation data. This is engineering theater."**
*Answer: partially conceded.* The verdict is honestly INCONCLUSIVE with explicit unlock
criteria (≥60 enriched Solana trades, 40/20 IS/OOS, ≥10 vetoed and ≥10 kept per split,
≥2 regimes per split, then the standard gates). `calibrate.py` refuses to bless a verdict
early. What the work buys: the moment data exists, validation is a one-command rerun, and
the fail-open/logging contract is already tested. What it doesn't buy: any evidence the
proxy predicts anything. This objection counts against shipping and is recorded in
`verdict.json`.

**2. "The existing rug guard already vetoes top1 > 40% and top5 > 75% (fail-closed). Your
screen is redundant — and worse, it tests visible concentration while MELT's 36.5% finding
is about *hidden* coordinated ownership that top5 cannot see."**
*Answer: conceded as the central scientific weakness.* The only non-redundant region is
stricter patterns like top5 > 60 with top1 ≤ 20 (the "disguised distribution" proxy) —
exactly where visible metrics might catch what the coarse guard misses. But the proxy is
not the phenomenon: a negative result would not refute MELT, and a positive result would
demand bundle-trace follow-up before being trusted. Even with data, the validation bar for
shipping should be higher than the standard gates for this track.

**3. "Fail-open on missing data means the screen does nothing precisely when data is
thinnest — and fail-open on a safety screen is the wrong default."**
*Answer: the default is deliberate and documented.* This is an *enhancement* screen, not
the primary guard — the primary rug guard stays fail-closed and untouched ("holder
concentration unverifiable" already skips). Failing closed here would duplicate the
guard's behavior and add nothing while risking a full Solana halt on RPC gaps. Every
fallback logs exactly one counted "W2 FALLBACK" line. *Residual risk (admitted):* if a
future integrator wires `veto` into the hot path while weakening the primary guard, the
fail-open becomes a hole — mitigated by `rule=None` default (never vetoes unasked) and
the module docstring warning, but not eliminable by this track.

## Verdict

**`ship_recommend=false`. Nothing ships.** The detector is built, tested (179 green), and
benchmarked, but there are **zero** completed Solana trades with holder evidence in the
journal, so no threshold can be fit or tested. Baseline replay for context only: IS
(32 trades) −$71.56 / PF 0.22 / 25.0% WR / $73.77 max DD; OOS (17 trades) −$4.55 / PF 0.78
/ 47.1% WR / $11.73 max DD (small-sample recent regime — not validation). Revisit after
≥60 enriched Solana trades accumulate.
