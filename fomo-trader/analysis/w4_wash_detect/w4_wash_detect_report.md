# W4 wash/bump-bot signature detection — offline paper snapshot

## Scope and evidence

This is a disabled research hypothesis. The 2026-09-27 read-only snapshot contains 73 FIFO-paired journal closes, 70 entries, and 3 closes without entry records. `replay.parse_signals()` parsed 628 signal observations from `bot.log`; `signal_filter.join_signals()` paired 70 with entered, closed trades, marked 476 never-entered and 82 best-effort guard skips, and inferred signal dates from 70 journal-backed entry anchors and 19 launcher timezone starts. No signal lacked a date or timezone. Joins use the existing same-chain, normalized-name, latest-within-30-minutes rule. Repeated signals are separate observations. The bot can continue appending records; these counts describe this fixed read.

The research report's arXiv:2601.08641 signature counts **identical-amount** matched buy/sell pairs relative to net position and proposes 50 as a starting heuristic. Our proxy is `min(m15_buys, m15_sells) / max(1, abs(m15_buys-m15_sells))`. It uses **aggregate trade counts**, not transaction amounts, wallet identity, token position, or the paper's wash score. High symmetry can reflect ordinary two-sided trading. A volume floor is intended to avoid calling a tiny quiet pool wash-like, but no floor can be calibrated here. The paper's observation that 82.8% of >100% returners showed artificial growth is descriptive and gives no forward veto edge.

The secondary `buy_sell_ratio`/journal `signal_ratio` is logged for all 628 signals/70 matched entries. A ratio near 1 could also indicate symmetric flow, but a ratio is neither proof of wash trades nor independent from the aggregate-count proxy. The prior signal-quality track already rejected a ratio ≥4 veto after OOS stress; this track does not recycle that as a new edge. The entered set contains no ratio <1.5, so there is no entered comparison for the closest-to-one band.

## Coverage and attribution

All three fields needed for the aggregate proxy (`m15_buys`, `m15_sells`, `m15_volume_usd`) are present on **3/70 entered trades (4.3%)** and null on **67/70 (95.7%)**. Including unmatched closes, coverage is 3/73 (4.1%); the three unmatched closes cannot be assigned signal fields. Chain coverage among matched entries is **Solana 0/49**, **BSC 3/21**. The aggregate fields are copied only to entry records, so **0/558 non-entered signal observations** have a corresponding aggregate-count or volume record; their wash-proxy veto footprint cannot be estimated. The logged ratio covers 558/558 non-entered signals (86 Solana, 472 BSC). Missing values always approve.

The only enriched trades are all BSC and all on 2026-09-27: 老板🦅 / WBNB (92 buys, 24 sells, $49,429 volume, proxy 0.353, costed net −$0.31), STO / WBNB (42, 9, $28,969, 0.273, −$1.69), and AICH / WBNB (94, 14, $15,884, 0.175, −$0.23). All three lost after the existing replay cost model. No observed proxy approaches the paper's 50, which is not a transferable threshold for this different score.

Bucket metrics use `replay.metrics` and its costed `net_pnl`: entered count, costed win rate, USD profit factor, and USD net. “No entry” counts signal observations without a joined trade, including guard skips. The aggregate-proxy missing bucket includes all non-entered signals because their entry-only fields are unavailable. Buckets are descriptive, not entry rules.

| Chain | Proxy bucket | Entered | No entry | WR | PF | Net USD |
|---|---|---:|---:|---:|---:|---:|
| Solana | missing | 49 | 86 | 32.7% | 0.327 | −$76.10 |
| BSC | <0.25 | 1 | 0 | 0.0% | 0.000 | −$0.23 |
| BSC | 0.25–<0.5 | 2 | 0 | 0.0% | 0.000 | −$2.00 |
| BSC | missing | 18 | 472 | 5.6% | 0.110 | −$41.48 |

| Chain | Logged buy/sell ratio bucket | Entered | No entry | WR | PF | Net USD |
|---|---|---:|---:|---:|---:|---:|
| Solana | <1.5 | 0 | 0 | — | — | — |
| Solana | 1.5–<2 | 23 | 32 | 39.1% | 0.363 | −$21.85 |
| Solana | 2–<4 | 20 | 36 | 35.0% | 0.356 | −$44.42 |
| Solana | ≥4 | 6 | 18 | 0.0% | 0.000 | −$9.83 |
| BSC | <1.5 | 0 | 0 | — | — | — |
| BSC | 1.5–<2 | 8 | 162 | 0.0% | 0.000 | −$11.82 |
| BSC | 2–<4 | 8 | 206 | 12.5% | 0.251 | −$15.27 |
| BSC | ≥4 | 5 | 104 | 0.0% | 0.000 | −$16.62 |

## Chronological validation

The 70 joined, closed entries were sorted by UTC-normalized entry time using the join's launcher timezone, then split into the first 47 IS and last 23 OOS. Lexically sorting journal clock strings would mix EDT and UTC. All three enriched trades occur in OOS; **IS has zero enriched trades on both chains**. Consequently, every possible wash threshold and volume floor is observationally identical on IS. There is no IS edge ranking, broad plateau test, or legitimate IS-selected threshold to carry into OOS. Sweeping 50 or any other number would be an empty sweep. Both chain thresholds and floors remain **none**. The JSON holds `0.0` inert placeholders under `ship_recommend=false`; these are not calibrated thresholds.

| Evaluated rule | Split | n | Net USD | WR | PF | Max drawdown USD | Edge/trade vs baseline |
|---|---|---:|---:|---:|---:|---:|---:|
| No threshold (pass-through baseline) | IS | 47 | −$69.36 | 34.0% | 0.348 | $76.24 | $0.000 |
| No threshold (pass-through baseline) | OOS | 23 | −$50.46 | 4.3% | 0.092 | $50.46 | $0.000 |
| No threshold (3× slippage) | OOS | 23 | −$51.26 | 4.3% | 0.090 | $51.26 | $0.000 |

Across all 73 closes, the costed baseline is **−$121.87 net, 26.0% WR, PF 0.262, $121.87 max drawdown**. The 70 joined entries account for the analysis split; the three closes without entry records cannot be filtered and are excluded from feature validation. Edge/trade means retained-set net/trade minus unfiltered net/trade on the same split. The pass-through edge is exactly zero. OOS retention of ≥60% of IS edge is undefined when IS edge is zero; an OOS decay percentage is likewise undefined. The >90% WR flag does not fire for pass-through, but there is no validated candidate. All three enriched outcomes come from one chain/day, so even a post-hoc OOS effect would have single-regime dependence. The OOS 3× stress net is negative. Insufficient enriched IS and per-chain samples alone require rejection.

This is an offline exclusion analysis. A veto could free capacity and alter later opportunities; the journal cannot replay those counterfactual trades. USD marks and fees are estimates in `analysis/replay.py`, including a fallback SOL price for older entries. No profitability or live-readiness follows from these tables.

## Untestable signatures and implementation

The launch-block/first-five-block non-creator buy heuristic is untestable: the journal contains no block-level transactions or creator attribution. Identical transaction amounts, matched wallet pairs, and net token position are also absent, so the paper's actual wash score cannot be computed. The hot path computes only the disclosed aggregate-count proxy. `approve()` returns true for missing counts, missing volume, unknown chains, malformed input, and invalid/unreadable parameters. It performs no network access and uses only the standard library. The parameter schema requires separate Solana and BSC entries. The current disabled file passes every signal. No bot, config, or run artifact was modified.

## Verification

`python3 -m pytest tests/ -x -q` could not start because system Python has no pytest. The repository environment completed `.venv/bin/python -m pytest tests/ -x -q`: **124 passed, 0 failed** in 26.71 seconds. The new hermetic tests cover score math, missing/corrupt fail-open behavior, separate chain settings, schema rejection, and a cold/cached `approve()` call under 50 ms.

## W4 VERDICT

- **Signatures tested:** aggregate buy/sell count symmetry (`min(buys,sells)/max(1,abs(buys-sells))`) with an m15 USD-volume floor; logged buy/sell ratio near 1.0 as descriptive secondary evidence. The count proxy is not the paper's identical-amount wash score.
- **Calibrated thresholds:** Solana **none** (0/49 entered with aggregate fields); BSC **none** (3/21 enriched, all OOS). The JSON's zero values are disabled placeholders, not selected cutoffs. IS has 0 enriched trades; the threshold sweep, broad-plateau test, ≥60% OOS-edge retention, and OOS decay are undefined.
- **Candidates and validation:** no wash-veto candidate could be selected. Pass-through baseline: IS **47 / −$69.36 / PF 0.348 / WR 34.0% / $76.24 drawdown**; OOS **23 / −$50.46 / PF 0.092 / WR 4.3% / $50.46 drawdown**. OOS at 3× slippage: **23 / −$51.26 / PF 0.090 / WR 4.3% / $51.26 drawdown**. Pass-through edge is $0.000/trade in both splits. Full 73-close baseline: −$121.87, PF 0.262, WR 26.0%.
- **Ship recommendation:** **false**. Exact shipped rule: **none**; the offline `approve()` passes all signals while disabled. Sample sufficiency fails, all enriched cases are one BSC day, and OOS 3× net is negative. Revisit after at least 30–50 enriched closed trades per chain span both chronological splits and multiple days; this is a collection target, not a validated threshold.
- **Verification:** `.venv/bin/python -m pytest tests/ -x -q` → **124 passed, 0 failed**. System `python3` lacked pytest; the repository environment completed the suite.
- **Untestable:** identical per-transaction amounts, wallet-matched buy/sell pairs, net position, creator attribution, and non-creator buys in the launch block or first five blocks. Those fields do not exist in these journals.

## Post-implementation review (coordinator, elite bar)

After the implementer run, the coordinator applied the raised verification bar:

- **Silent fail-open fixed:** `approve()` previously returned `True` silently on all
  seven fallback paths. It now counts every fallback by reason in a thread-safe
  `fallback_counts()` (`non_dict_signal`, `unknown_chain`, `unhashable_chain`,
  `missing_evidence`, `bad_volume`, `bad_counts`, `bad_params`, `exception`) and
  optionally logs `WASH FALLBACK <reason>` via a `log` callable. The disabled
  state is configuration, not a fallback, and is not counted. A zero wash
  threshold with `ship_recommend=true` is rejected at params load.
- **Latency proven:** n=20k journal-shaped inputs, no tracemalloc: mean ~10.5 us,
  p50 ~9.7 us, p99 ~22 us, max 0.6-1.1 ms — ~2,250x headroom under the 50 ms
  budget at p99. Cold start ~300 us one-time. Steady-state allocation 0.2
  bytes/call (effectively zero).
- **Adversarial battery:** NaN/inf/negative/bool/object counts, unhashable and
  unknown chains, empty 15m windows, raising loggers, corrupt params — all
  fail-open, counted, never raise. 8 threads x 500 calls: exactly 4000 counted.
- **Concurrency audit:** bot spawns one thread per signal in `enter()`; the
  module touches no bot state. `_params_cache` check-then-set is benign under
  the GIL (atomic, idempotent); the fallback counter is lock-guarded, taken
  only on fallback paths.
- **Look-ahead audit:** all veto features (`m15_*` at commit, ratio/chain at
  signal time) predate the decision point; validation ran pass-through only, so
  no retrospective veto was ever applied.
- **Staff review:** `w4_staff_review.md` — verdict do-not-ship, three strongest
  objections (proxy-is-not-the-paper's-score; zero calibration sample; premise
  may be directionally wrong), all counted against shipping.
- Full suite after hardening: **170 passed, 0 failed**.

The verdict is unchanged: **ship_recommend=false**. The module is hardened and
waiting for enriched data.
