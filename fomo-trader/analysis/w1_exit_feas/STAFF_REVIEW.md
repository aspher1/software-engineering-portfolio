# Staff review: W1 pre-entry exit-feasibility simulation

Author: W1 track coordinator. Date: 2026-09-27.
Verdict: **REJECT — do not ship** (`ship_recommend: false`).

## What was built
`analysis/w1_exit_feas/exit_feas.py`: an entry-time veto that estimates the
slippage a planned position would suffer on exit and blocks entries above a
threshold. Estimator: constant-product average impact
`100·P/(L/2+P)`, maxed with a 15m-volume absorption bound. Pure stdlib,
fail-open with every fallback logged and counted (`fallback_counts`,
lock-guarded), `Params` frozen dataclass loaded once at startup, `approve()`
otherwise pure. Accompanied by `exit_feas.json` (params + numbers),
`exit_feas_report.md`, and 21 hermetic tests.

## Validation result
71 matched closes, chronological 47 IS / 24 OOS, realistic costs, 3× stress.
49/71 entries scorable; estimated impacts max **0.1697%**. Every threshold
10–40% retained every trade; per-trade edge $0 IS and OOS; stressed OOS
unchanged at -$58.50. The veto cannot fire at this bot's operating point
(~$7 positions vs $15k+ liquidity floor).

## The three strongest objections

**1. "Your estimator is a toy. Real exits happen into crashed books, not
entry-time books — the dump dominates, not your $7-vs-$40k arithmetic."**
Agreed, and it strengthens the rejection. The estimator is a *lower bound*
on exit cost. The veto's job was to discriminate ex ante from entry-time
data; since even the lower bound maxes at 0.17%, no entry-time threshold
≥10% can fire. A more sophisticated estimator would only veto more, and any
threshold below 0.17% would be fit to noise or veto everything. The failure
is absence of entry-time signal, not estimator crudeness.

**2. "69% coverage is too thin — the 22 unscorable entries fail open, so
'retains everything' is partly a missing-data artifact."**
Partially fair, and I can't fully eliminate it. Counter: unscorable entries
are old-format rows missing the position-USD leg, not thin-liquidity rows —
position size is ~$7 for all of them by config, so there is no mechanism by
which they'd score systematically worse. But if a fully-enriched history
existed and revealed something, this analysis would miss it. I count this
against shipping, not for it: it weakens the *generality* of the rejection,
not the verdict on the data we have.

**3. "Paper fills are optimistic quotes, and 3× stress is arbitrary — your
validation understates real exit costs."**
True. But the bias direction supports rejection: real costs ≥ paper costs,
and the veto failed to fire on the *optimistic* numbers. Making costs worse
doesn't make a 0.17%-max estimator fire at a 10% threshold. The stress gate
is reported for completeness; the rejection does not depend on it.

## Audits

**Latency (measured, n=20k warmed iterations, realistic inputs).**
Fast path: p50 1.42µs, p99 2.03µs, mean 1.60µs — ~25,000× inside the 50ms
budget. Fallback path (logged + counted): p50 17.8µs, p99 49µs —
logging-dominated, still ~1,000× inside budget. p100 outliers (2–3ms) are
GC/scheduler noise. Memory: 0 KiB RSS growth over 200k calls; no per-call
allocation beyond temporaries, no caches, no unbounded structures.

**Adversarial self-review (all in tests).** NaN/Inf liquidity and position
fields route to counted fallbacks (not silent zeros); stringified numbers
score correctly; corrupt/missing params file → defaults + counted;
Solana vs BSC payload shapes (sol_usd vs bnb_usd vs commit triple, None
holder fields) all handled; empty 15m windows ignore the volume bound;
duplicate mints evaluated independently; a hostile entry object raising in
`.get()` is caught, counted, and approved. 8-thread hammering: counters sum
exactly (no lost updates).

**Concurrency.** Integration contract: caller caches one `Params` (frozen,
immutable → race-free) and passes it in; `load_params()` does file I/O and
must run at startup, never in the hot path. Only shared mutable state is
`fallback_counts`, guarded by a single non-nested `_fallback_lock` (no
deadlock: lock ordering is always `_fallback_lock` → logging internals,
never reversed). `approve()` takes no locks on the fast path and never
mutates the entry dict (read-only `.get`). No interaction with the bot's
`self.lock` or position state.

**Look-ahead.** Every feature precedes the decision point. `liquidity_usd`,
`m15_volume_usd`, `m15_buys/sells` come from the signal snapshot built in the
scan loop (fomo_trader.py ~L392-403, 701-702, 767-768) *before* the entry
decision; `buy_sol/buy_bnb`, `commit_price_*` are the fill itself;
`sol_usd()` is contemporaneous (≤120s cache). `entry_evidence()` assembles
them post-commit but from pre-decision observations — verified by reading
the entry path; no post-entry price action enters any feature. The label
(close-row P&L) is post-decision, which is correct supervised semantics.
Caveat (staleness, not look-ahead): the signal snapshot may be
seconds–minutes old at commit (pullback wait); this biases the estimator
optimistic, which only strengthens the rejection. `evaluate()` splits by
journal append order per the replay-harness convention; it does not sort by
timestamp (documented in a test).

## Bottom line
The engineering is sound — sub-microsecond fast path, no leaks, race-free,
no look-ahead, every fallback explicit. The hypothesis is dead at this
operating point: exit impact is negligible by construction at ~$7 size, so
no entry-time threshold can discriminate. Revisit only if position sizes or
the liquidity floor change materially. Keep `exit_feas.py` as the estimator
for that day.
