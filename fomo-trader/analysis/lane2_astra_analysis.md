# Lane 2 — pre-entry prediction of BSC venue wipes

Date: 2026-09-28. **Verdict: ship nothing. One exploratory candidate, unvalidated.** Positive signal-to-commit price drift is associated with subsequent wipes in this snapshot. It would be inaccurate to conclude that every available feature is uninformative. It would be equally inaccurate to call this a reliable predictor: the selected screen retains only 15 OOS trades, still loses money under both cost models, and was selected after substantial exploration. No bot implementation or configuration change is recommended.

## 1. Fresh diagnosis and accounting

Read-only snapshot of `runs/paper-1h/trades.jsonl`: **322 append lines; 160 entries; 162 closes**. SHA-256: `3b0a23ee3df2ed3b278af89fac37af8dffa9aa716367b879d16b07743dc722b2`. FIFO pairing by `(chain, mint)` produces **159 pairs**, three unmatched closes and one open entry. Missing chain is inferred from the mint format, as in the existing replay. No timestamp sorting: there are two backward timestamp transitions.

Primary wipe label: recorded realized native P&L / native stake ≤−0.85 **AND** close reason contains `dump detector` **OR** `venue dump` (the reason alternatives are grouped). This is a loss-and-reason proxy, not proof of an on-chain LP removal or a single-tick path. The journal does not establish the full quote path for every close. Independently report the broader near-zero quote proxy `exit / entry <= 0.01`; it is not interchangeable with realized loss because partial exits and accounting can differ. Neither label enters a filter decision.

Costs follow the currently checked-in `analysis/replay.py` convention, recomputed without importing or changing the bot:

- Let S be native stake, R recorded realized native P&L, q = max(0, exit/entry), and U the recorded native/USD rate (close, then entry; $115/SOL fallback for early Solana records).
- Net USD = `U × [R − S × (1+q) × (0.0025 + slippage) − fixed]`.
- Base slippage = 0.0015 per leg; stress = 0.0045. Swap fees stay 0.0025 per leg. Fixed round-trip estimate stays 0.00004 BNB or 0.004 SOL.
- Recorded BSC stake can be named `buy_sol`; realized BSC P&L is `realized_bnb`. These were handled explicitly. Costs use quote-implied terminal proceeds, matching replay, not a reconstructed partial-exit cash flow. These are paper estimates, not executable fills.

| Cohort | Closes | Raw net USD | Base net USD | 3× slippage net USD | Wipes | Wipe base net USD |
|---|---:|---:|---:|---:|---:|---:|
| Entire journal, including unmatched closes | 162 | −186.23 | −221.58 | −227.44 | 18 | −99.38 |
| FIFO paired | 159 | −185.79 | −219.53 | −225.21 | 18 | −99.38 |
| BSC, all closes | 110 | −135.40 | −143.42 | −146.88 | 18 | −99.38 |
| Solana, all closes | 52 | −50.83 | −78.16 | −80.56 | 0 | 0.00 |
| Enriched paired, all BSC | 92 | −95.31 | −101.94 | −104.78 | 14 | −71.51 |

The 18 wipes comprise nine dump-detector and nine venue-dump closes. Their **$99.38 loss accounts for 44.85% of total costed net loss and 69.29% of BSC net loss**. Enriched wipes account for **70.15%** of enriched net loss. Raw wipe loss is $98.42, or 52.85% of total raw net loss. These shares use net loss, not gross losing-trade dollars.

Sensitivity to the label matters: 24 closes have terminal quotes ≤1% of entry (23 BSC, one Solana), totaling **−$102.09** costed P&L. Of those, 19 are enriched, totaling −$77.28. Ignoring exit reason and using only ≥85% realized stake loss yields 23 closes (19 BSC, four Solana), totaling −$143.70. Thus “18 wipes” is a specified operational proxy, not a count of every catastrophic trade.

### Append-order cohorts

Pairs are ordered by their **close append line**, with entry line retained for purging. The full paired sample splits **106 IS / 53 OOS**, boundary close line 215, with no crossing pair. IS has 49 Solana and 57 BSC pairs; OOS is all BSC. Baseline nets are −$163.77 / −$55.76. Only **39** of those 106 IS entries have enrichment, versus all 53 OOS entries. The nominal ≥100 IS count therefore cannot supply ≥100 observations of the proposed feature.

For enriched analysis, first `floor(92×2/3)=61` closes form IS, ending at line **260**; remaining **31** form nominal OOS, starting at line 262. One OOS pair entered at line **254** and closed at **263**, so purge it from validation. This leaves **61 IS / 30 OOS**, all BSC. That pair is not a primary wipe; it lost $5.06 and is vetoed by the selected screen.

| Enriched split | n | Base net USD | Stress net USD | Wipes | Wipe base net USD |
|---|---:|---:|---:|---:|---:|
| IS | 61 | −60.79 | −62.92 | 8 | −47.46 |
| Nominal OOS | 31 | −41.15 | −41.86 | 6 | −24.05 |
| Purged OOS used below | 30 | −36.09 | −36.79 | 6 | −24.05 |

All 92 enriched entries have slip, liquidity, volume, market cap, latency, source and window. Holder top-1/top-5, LP burn and LP lock evidence have **zero non-null coverage**. All enriched sources are `geckoterminal`.

The supplied headline numbers are not this snapshot's baseline. The actual requested `round5_report.md` is an older 29-enriched-close descriptive report. The fundamental-fix report uses 1% base slippage; exit-sweep uses 0.15%; Round 5's helper named `three_x_cost_stress` multiplies all estimated costs. This analysis triples **only slippage**, per the task. Different snapshots, samples and accounting must not be conflated with the supplied −$52.21 headline.

## 2. The one candidate: positive price-drift veto

**Exact hypothetical additional filter:**

`veto = (chain == "bsc") AND is_finite(slip_from_signal_pct) AND (slip_from_signal_pct > 0.0)`

Zero passes. Missing/nonfinite evidence passes this additional screen and is separately counted as uncovered; it does not override any existing guard. Only the entry-record `chain` and `slip_from_signal_pct` determine the veto. Despite its name, the stored slip field is a **fraction**: 0.10 means +10%, not +0.10%. It equals `commit_price_usd / signal_price_usd − 1`; using that ratio separately adds no independent feature.

Hypothetical hook: in `enter_bsc()`, after the existing commit guard and allocator decision have passed, but **before** writing `self.state["positions"][mint]`, counters, cooldown or ledger state. It is an additional veto; every existing guard, risk ceiling and position-size rule remains authoritative. Use only already available quote/signal/rate inputs and deterministic arithmetic, with no extra network call, subprocess, randomization or inference.

**Timing caveat:** current `entry_evidence()` is assembled after the paper position is committed and uses a later `bnb_usd()` call. Liquidity is copied from the signal, not demonstrably refreshed at commit. The recorded slip is an entry-associated proxy, not independently verified as the identical value available immediately before the ledger write. A prospective offline/paper validation must capture the pre-write value from existing inputs and check this timing equivalence. This further prevents shipping on the present retrospective result. No implementation is supplied.

### Selection and performance

Exploration covered **107 predicates**, 60 eligible after requiring ≥30 retained IS trades and at least one IS veto. Selection maximized IS base edge, breaking ties toward fewer vetoes. The grid was fixed before its scoring pass, but this was **not preregistered before inspecting the dataset**; descriptive OOS distributions were also examined. The holdout is exploratory, not untouched confirmatory evidence.

Search scope, for reproducibility: both `<` and `>` on slip {−0.5, −0.25, −0.1, 0, 0.1, 0.25, 0.5, 1}; volume/liquidity {0.1, 0.25, 0.5, 1, 2, 4, 8}; market-cap/liquidity {1, 2, 4, 8, 16, 32}; latency milliseconds {2000, 3000, 5000, 10000, 30000, 60000}; recorded stake USD/liquidity {0.000025, 0.00005, 0.0001, 0.0002, 0.0005}. Also 40 interactions: slip greater than {−0.25, −0.1, 0, 0.1, 0.25} AND liquidity below/above {$40k, $60k, $80k, $100k}; and three source/window categorical predicates. No prohibited gain, liquidity-floor or imbalance candidate was rerun. The following is the **only nominated candidate**.

Edge = `(retained candidate net − full net) / original split size`; skipped trades contribute zero.

| Measure | IS | Purged OOS |
|---|---:|---:|
| Original / retained | 61 / 33 | 30 / 15 |
| Candidate base net | −$2.31 | −$3.35 |
| Candidate stress net | −$3.58 | −$3.81 |
| Base edge per original trade | +$0.9588 | +$1.0913 |
| Stress edge per original trade | +$0.9728 | +$1.0992 |
| Retained base win rate | 42.4% | 26.7% |
| Primary wipes caught / total | 6 / 8 | 6 / 6 |
| Wipes / vetoed trades (precision) | 6 / 28 = 21.4% | 6 / 15 = 40.0% |
| Near-zero quotes caught / total | 7 / 9 | 8 / 9 |

OOS retains **113.82%** of IS base edge and **113.00%** of IS stress edge. The edge-retention gate passes; sample size and positive retained net do not. IS vetoes include five winning trades, OOS vetoes include one. IS wipe incidence is 21.4% in vetoed versus 6.1% in retained trades; OOS is 40% versus 0%. Six observed OOS wipes caught is too little evidence to infer universal detection: even under independent sampling, 6/6 has an approximate exact two-sided 95% sensitivity lower bound of only 54%; clustering weakens that interpretation further.

At the fundamental-fix report's harsher 1%/3% slippage assumptions, retained IS nets are −$5.91/−$14.39 and retained purged OOS nets −$4.66/−$7.73. The failure is not rescued by changing cost convention.

## 3. Rejections and robustness limits

- **Tighter/looser drift cutoffs:** neighboring +10% and +25% vetoes have positive IS edges (+$0.6320 and +$0.3901), but purged OOS retained nets are −$15.25 and −$23.03, with only 19 and 24 retained trades. Requiring a ≥10% pullback produces positive IS net, but retains only 24 IS and nine OOS, and OOS loses $4.82. Reject threshold shopping for a profitable-looking IS sample. The selected zero cutoff is a broad risk association, not a proven discontinuity.
- **Slip × liquidity:** the IS-best eligible interaction underperforms the simple drift screen in IS. It catches only two of six nominal OOS wipes and retains a losing cohort. More dimensions do not improve the evidential case.
- **Volume/liquidity:** wipe/non-wipe IS medians are 0.496/0.422; OOS medians reverse to 0.374/0.503. The IS-best family screen catches only two of six nominal OOS wipes, with 16 retained trades and −$23.96 net. No stable wipe separation.
- **Market-cap/liquidity:** wipe/non-wipe medians shift from 4.30/4.01 IS to 0.858/3.63 OOS. Extreme IS ratios exceed 180,000 in both labels, raising valuation-quality concerns. The family winner leaves only 11 nominal OOS trades and −$17.72 net.
- **Latency:** IS wipes range from 1,266–2,972 ms; non-wipes from 604–45,698 ms. OOS wipes also occur below one second. The family winner catches only two of six nominal OOS wipes. “Slow entry causes the wipes” is unsupported.
- **Entry size/liquidity:** small endogenous tickets do not identify safety. Wipe/non-wipe IS medians are approximately 0.000066/0.000110, reversing OOS to 0.000117/0.000073. The IS-best direction catches only two OOS wipes and retains nine nominal OOS trades. Allocator and bankroll drift confound causal interpretation; no sizing increase is proposed.
- **Source/window:** there is no source variation to test. The 5m bucket has only four IS trades (one wipe) and five OOS (three wipes). A window screen cannot establish stable performance; excluding 5m retains only 26 nominal OOS trades and still loses $23.90.
- **Time/sequence:** first six disjoint 15-trade enriched blocks have 1, 0, 5, 2, 2, 3 wipes, then one in the final two trades. Five wipes cluster in the third block. The drift screen's retained block profits alternate signs; sequence is not a causal feature or an independently validated cutoff. Timestamp reversals and the prior report's timezone warning rule out a trustworthy hour-of-day claim here. No sequence/timestamp trading rule is nominated.
- **Previously rejected five:** signal gain >200%, $30k liquidity floor, imbalance/buy-count floors, Solana holder concentration and tighter venue-m5 remain rejected; they were not retuned. Prior liquidity edge retention was only 25.8%; imbalance left five OOS trades; holder evidence remains entirely absent; terminal m5 cannot identify an earlier executable exit. The requested fundamental-fix file actually enumerates four candidates, not the gain veto; the gain rejection is accepted from the user's stated prior evidence, without inventing fresh test results.

Family diagnostics above use nominal 31-trade OOS and are not additional proposals. The sole candidate's decision table uses the purged 30-trade OOS. Multiple comparisons, already-examined historical data, one venue source, all-BSC enrichment and changing allocation make formal predictive claims premature. BSC is the intended scope; this does not establish cross-chain generalization or profit in multiple regimes.

## 4. Falsifiable validation plan

1. Freeze the single zero-drift predicate and these definitions before collecting new evaluation data. Treat all 92 enriched historical pairs as explored. Do not repeatedly optimize thresholds on the accumulating holdout. Collect sufficient *feature-complete* paper pairs for append-order ≥100 IS and ≥30 original OOS, with **≥30 retained OOS**. At the observed roughly 50% retention, 30 original OOS is unlikely to suffice; approximately 60 would be needed, with the actual count gate binding.
2. Specify the split in append-line order; purge entries opened before the boundary and closed after it. Never timestamp-sort. Exclude unmatched closes from rule evaluation and wait for open entries to close. Fix the feature-capture instant before the ledger write and verify equivalence with the entry record. Disclose missingness and all excluded pairs.
3. Preserve paper stakes and observed close P&L. Give a veto $0 and compute base and stressed edge over the **original**, not retained, cohort size. Do not credit replacement entries, freed capital or hypothetical fills. This is a static filter counterfactual; altered bankroll, cooldowns and future allocation require a subsequent prospective paper test.
4. Require positive IS and OOS edge, with OOS ≥60% of IS edge at base and stress. Require positive retained OOS net at both costs. Triple only the slippage component; state fees and fixed transaction costs separately. Current candidate passes relative edge but fails retained profitability and count.
5. Freeze the primary wipe and near-zero sensitivity labels. Report recall, precision, retained wipe incidence, dollar losses avoided, winners vetoed, coverage, and uncertainty alongside P&L. Fail a claim of wipe prediction if improved net comes only from ordinary-loss rejection without reproducible wipe discrimination.
6. Flag WR >90%, OOS edge decay >70%, isolated threshold peaks, results driven by one cluster/regime/chain, repeated addresses and valuation anomalies. The ≥60% retention requirement is stricter than the >70%-decay red flag: even 41% decay fails the gate. Fewer than 30 retained OOS is **inconclusive**, regardless of apparent recall. Report separate chronological blocks and window strata; do not call an all-BSC result cross-chain validation.

**Final decision:** there is a measurable price-drift association, but no validated commit-time venue-wipe predictor. Nothing ships. `dry_run=true` remains unchanged. Only this report was written in the repository; temporary offline calculations were kept under `/tmp`. No protected runtime file, bot code, risk control or position size was changed by this analysis.
