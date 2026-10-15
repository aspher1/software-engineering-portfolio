# W4 staff review — wash/bump-bot signature detection

Verdict: **do not ship** (`ship_recommend: false`). This document records what was
built, the elite-bar verification, the three strongest objections, and answers.

## 1. What was built

`analysis/w4_wash_detect/wash_detect.py` (pure stdlib): an aggregate wash-proxy
`min(buys,sells)/max(1,abs(buys-sells))` computed from hot-path 15m fields
(`m15_buys`, `m15_sells`, `m15_volume_usd`), gated on a per-chain volume floor.
`approve(signal, *, path, log=None)` fails open on missing evidence, malformed
signals, unknown chains, or bad params — never raises. Params schema in
`w4_wash_detect.json` requires `ship_recommend: bool` and per-chain
`{wash_proxy, min_volume_usd}` floats; an active config with a zero wash
threshold is rejected at load (it would veto everything above the volume floor).

Post-implementation hardening (added during this review): every fail-open path
is now **counted** in a thread-safe `fallback_counts()` and optionally logged as
`WASH FALLBACK <reason>` (reasons: `non_dict_signal`, `unknown_chain`,
`unhashable_chain`, `missing_evidence`, `bad_volume`, `bad_counts`,
`bad_params`, `exception`). Previously these were silent. The disabled state
(`ship_recommend=false`) is configuration, not a fallback, and is not counted.

## 2. Elite-bar verification

**Latency (measured, n=20k realistic journal-shaped inputs, no tracemalloc):**
two stable runs. Mean ~10.5 us, p50 ~9.7 us, p99 ~22 us, p999 ~130–180 us,
max 0.6–1.1 ms. The 50 ms budget holds with **~2,250x headroom at p99** and
44–83x even at the absolute observed max (VM scheduling outliers). Cold start
(one-time file read + JSON parse): ~300 us. Budget proven, not asserted.

**Memory:** steady-state net allocation 0.2 bytes/call over 5k calls —
effectively zero; the happy path allocates nothing. Module state is one cached
params dict plus a small Counter. No growth, no leak.

**Adversarial battery (all pass):** NaN/±inf/negative/bool/string/object counts →
fail-open + counted; unhashable chain (`[]`) → counted; unknown/None chain →
counted; empty 15m window (0,0) → proxy 0.0 → approve; integral floats accepted;
10^15 counts → correct math; extra keys ignored; raising log callable cannot
break approve; corrupt/missing/schema-bad params → `bad_params` counted;
zero-threshold-with-ship=true rejected at load. 8 threads × 500 hammering
calls → exactly 4000 counted, no lost increments.

**Concurrency audit:** the bot spawns one thread per signal in `enter()`
(`fomo_trader.py` ~L2003), so an integrated `approve()` runs concurrently. The
module touches no bot state (no trader locks, no state.json/journal/config).
Shared state is (a) `_params_cache`: check-then-set is lock-free and benign —
CPython dict assignment is atomic and values are idempotent; worst case is a
redundant ~300 us file parse; and (b) `_fallback_counts` under a single
`_fallback_lock`, taken only on fallback paths (happy path is lock-free; no
nesting, no lock-ordering risk, no deadlock surface).

**Look-ahead audit:** every veto feature predates the decision point.
`m15_buys`/`m15_sells`/`m15_volume_usd` are Round-3 enrichment fields captured at
commit time (entry record; `entry_latency_ms` proves capture during entry) —
timestamp = entry commit ≤ veto evaluation. `buy_sell_ratio` and `chain` come
from the signal log line, which precedes entry (Track C's join enforces
signal_ts ≤ entry_ts within 30 min). The params file is static config, not
market data. Validation ran the pass-through baseline only — no threshold was
selected, so no veto was ever applied retrospectively; there is no
retrospective-decision look-ahead surface. If a threshold is ever calibrated,
the veto must be evaluated on entry-ordered trades using only these pre-entry
fields (same pattern as Track C's `validate_ratio_veto`).

**Tests:** full repo suite 170 passed, 0 failed (includes 5 new W4 fallback/
concurrency tests; sibling W1/W2 tracks contributed the rest of the growth
from 124).

## 3. The three strongest objections

**Objection 1 — The proxy is not the paper's wash score.**
The paper computes matched *identical-amount* buy-sell pairs over net position
from per-transaction data. We compute *count* symmetry from 15m aggregates.
Symmetric counts also arise in organic cooling; nothing in the construction ties
it to wash trading. *Answer:* agreed — that is precisely why the paper's
threshold (50) was refused unvalidated and per-chain calibration on our own
journals was required. The proxy is a hypothesis awaiting data, not a
measurement. **Cannot be resolved today → counts against shipping.**

**Objection 2 — There is no calibration sample.**
0 of 49 Solana entries and 3 of 21 BSC entries carry the aggregate fields; all 3
BSC cases are one day sitting entirely in OOS. Any threshold would be fiction,
and the 60%-OOS-retention gate is undefined on an empty IS. *Answer:* agreed —
this is the primary reason for rejection. Revisit after 30–50 enriched closed
trades per chain spanning both chronological splits and multiple days.
**Counts against shipping.**

**Objection 3 — The premise may be directionally wrong.**
The paper associates wash signatures with *winners*: 82.8% of >100% returners
showed artificial growth. If wash-heavy flow is what produces the only real
runners, vetoing it removes the upside tail while keeping slow bleeds — and
Track C found the low-gain bucket is our only PF>1 territory, which is where
organic cooling (high count symmetry) lives. The evidence, read straight,
argues *for* riding wash momentum, not vetoing it. *Answer:* this is a genuine
theoretical objection to the track's premise. The honest resolution is
empirical — only our own journals can say whether high-proxy signals lose —
and that data does not exist yet. **Cannot be answered today → counts against
shipping.**

## 4. Untestable from our journals (do not revisit without new data)

Identical per-transaction amounts, wallet-matched buy/sell pairs, net position,
creator attribution, non-creator buys in the launch block / first five blocks.
None of these fields exist in the journals; the aggregate proxy is the closest
reachable construct.

## 5. Decision

**Do not ship.** Three independent objections stand unanswered, the calibration
sample is empty, and the veto's theoretical direction is disputed by the very
paper that motivated it. The module is built, hardened, benchmarked, and
waiting: when enriched trades accumulate, calibration can run without new
engineering. Until then `approve()` passes everything, and any future
enablement must re-clear IS/OOS ≥60% retention and the 3×-slippage stress bar.
