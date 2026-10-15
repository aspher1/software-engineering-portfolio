#!/usr/bin/env python3
"""Render the sole iteration-5 report from the read-only diagnostic output."""
import json, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
d=json.loads(subprocess.check_output([sys.executable,str(ROOT/'analysis/iter5_astra_cuts.py')],env={**__import__('os').environ,'PYTHONDONTWRITEBYTECODE':'1'}))
s=d['sections']; out=[]
def text(t):out.append(t.strip()+'\n')
def table(title, rows):
 out.append('\n')
 text('### '+title+'\n\n| Bucket | n | Net $ | PF | WR % | Exp $ |\n|---|---:|---:|---:|---:|---:|')
 for k,v in rows.items():
  def fmt(x,n):return '—' if x is None else x if isinstance(x,str) else f'{x:.{n}f}'
  out.append(f"| {k} | {v['n']} | {fmt(v['net'],2)} | {fmt(v['pf'],3)} | {fmt(v['wr'],1)} | {fmt(v['exp'],4)} |\n")
 out.append('\n')
text('''# Astra analyst — iteration 5 (codex-astra)

Snapshot: 2026-09-29; last close 07:01:19 UTC. Analysis only; no bot changes or ship recommendation.

**Baseline unchanged. The requested fresh cuts mostly expose limits of identification: R3 has zero micro-positive-slip observations, S1 has only one true re-entry, and source has no variation. Positive slip is worse within every latency quintile, but the slowest cell is not the worst. Veto re-admissions are a tail-loss cohort, not a demonstrated general repeat-entry effect.**

## 1. Universe, accounting and reproducibility

Read the iteration-4 lane, validation and Astra reports first; retain their FIFO and clock conventions. All 427 trade rows were read: 212 entries, 215 closes, 206 strict FIFO pairs, 9 unmatched closes and 6 unmatched entries. Pair on the exact `(chain, mint)` key in append order, consuming the oldest queued entry, exactly as `analysis/iter1_trackb_screens.py`; do not repair legacy chain labels. All 163 BSC closes pair; the remaining 43 pairs are Solana. The 9 unpaired closes are outside feature-linked analyses, not silently assigned entry features.

**Main tables below use recorded BSC realized USD**, the validation baseline requested in this brief. PF = positive dollars / absolute negative dollars; WR counts strictly positive P&L; expectancy = net/n. Empty cells have undefined PF/WR/expectancy, not zero performance. Infinity means no observed losses, not proven edge. All-chain comparisons explicitly use the unchanged Track B conversion/cost helper, not a sum of SOL and USD. Supplemental costs subtract 0.25% swap fee + 0.15% slippage per leg and fixed round-trip native costs; stress raises slippage to 0.45%, not every fee by 3x. Paper haircuts may overlap these supplemental costs.
''')
table('Baseline reconciliation',{'BSC recorded (requested baseline)':d['baseline'],'BSC supplemental costed':d['costed_bsc'],'All 206 pairs supplemental costed':d['costed_all']})
text(f'''The requested -$172.34 / PF 0.262 / WR 27.0% (44/163) matches iteration 4 exactly. The analyst's older -$184.62 BSC / -$254.44 all-chain figures also reproduce under supplemental costs. No new closes. Trade-file SHA-256: `{d['sha256']}`.

Reproduce with `PYTHONDONTWRITEBYTECODE=1 python3 analysis/iter5_astra_cuts.py` (JSON to stdout). `PYTHONDONTWRITEBYTECODE=1 python3 analysis/iter5_astra_report.py` regenerates this report. The report renderer checks baseline and partition invariants before writing. Neither script writes runtime data.

UTC entry times reuse iteration 4's nearest same-chain/mint candidate clock-offset recovery within 30 wall-clock minutes, rounded to whole hours; the documented EDT interval has a +4h fallback. Epoch recovery supplies clocks only, never later market features. Legacy September 27 clocks remain provisional, including two negative hold intervals. Only four entry dates are represented. Repeated scans, shared market episodes, sizing, changing admission policy and bankroll resets prevent causal or independent-sample interpretations. These are descriptive full-book cuts, not new OOS tests.

## 2. Slip micro-bands — R3 remains untestable

`slip_from_signal_pct` stores a **fraction**: 0.25% = 0.0025. Boundaries below follow the requested right-closed intervals exactly.
''')
table('BSC slip micro-bands',s['slip micro-bands'])
table('Reference sign cohorts',s['slip sign'])
text('''There are **zero entries in (0,0.25%] and zero in (0.25%,1%]**. The smallest positive observation is 1.2114%; all 45 positive-slip trades belong to >1%. R3 cannot distinguish fee noise from chase, and these data cannot justify allowing the micro-band. Negative-slip reference n=100 has PF 0.563 versus positive PF 0.072; supplemental cost PFs are 0.499 versus 0.066. The two near-zero negative entries produce the tempting +$7.73/100% WR print; n=2 is not a boundary estimate. All 43 Solana pairs lack slip, so adding them cannot populate the empty test band. A future micro-band test needs contemporaneous fee-free signal and executable quote prices plus separately logged fees/taxes; the current field alone cannot attribute a small discrepancy to fees.

## 3. First actual purchase versus repeat purchase — S1

Count every entry row for each exact mint in append order, across all days, before restricting to paired BSC closes. Thus an earlier unmatched entry still counts. “Ever” means ever in this available lifetime log; no claim about missing external history or alternate address encodings.
''')
table('First entry / second-or-later entry',s['first/re-entry'])
table('Repeat status by veto era',s['first/re-entry x era'])
text('''Only **one** paired BSC trade is an actual re-entry: mint `0xa16a372b53ec6fb04ab55d5eadca7d6df615d91e`, second entry printed September 27 18:18:22, -$6.99, venue-dump close. Its close clock precedes its entry clock; append-order pairing is retained. Across all 206 pairs there is still only this one re-entry (costed -$7.049); the other 205 costed trades lose $247.39, PF 0.267. S1 is not meaningfully testable. Removing the single repeat barely changes the failing first-entry strategy. Do not equate repeated candidate sightings, a prior veto, and prior ownership: **all 48 post-ship entries, including all 20 veto re-admissions, are first actual entries.**

## 4. Entry source and window
''')
table('Source',s['source'])
table('Source × window',s['source x window'])
text('''Every paired entry, including all 43 Solana pairs, says `geckoterminal`. DexScreener appearing in an exit reason is not a DexScreener entry source. There is no venue/source effect or source interaction to estimate. Within GeckoTerminal, the 5m path loses more per trade than 15m, but has only 29 observations and different admission timing. As established in iteration 4, `signal_gain_pct` on the 5m path still records 15m gain. The window difference does not identify why one source would outperform another.

## 5. Post-veto cohort — exact clock and membership reconciliation

**No growth.** The brief's “49 post-16:34 entries” combines two different definitions. At the requested September 28 16:34 UTC entry cutoff there are **48 entries with closes**, -$38.04, PF 0.300, WR 31.25%. The validation report's **49 closes** after that time total -$36.03/PF 0.337: the extra +$2.01 close at 16:47:42 belongs to an entry at 12:16:27 (mint `0x2f9b3a72bc6e9e97c9348461a04792e6472e7ca4`). It is not post-veto entry exposure.

Iteration 4 Astra instead used the earlier 16:24 cutoff: its 49 entries include the 16:28:20 PEP entry, -$0.19. That universe is recorded -$38.23/PF 0.2985 and supplemental-costed -$42.23/PF 0.262. These are reproducible definition differences, not new losses or a numerical drift.

Define re-admission by an actual journal `slip_veto` on the same chain/mint strictly before recovered entry epoch; no future vetoes and no hypothetical historical positive-slip proxies. All 20 qualifying earlier vetoes also occur on the same UTC day as entry, so the same-day sticky rule gives the same classification here. “Fresh” means no earlier recorded veto, not proof of never being considered by the scanner. Journal coverage starts September 28.
''')
table('Strict post-16:34 entry cohort',{'all':d['post'],**s['post veto']})
table('Post-veto × window',s['post veto x window'])
table('Post-veto × fixed gain boundary',s['post veto x gain'])
table('Outcome decomposition (not entry screens)',s['post veto outcomes'])
text('''### Entry characteristics (medians; complete coverage within each listed group)

| Group | n | Gain % | Liquidity $ | Latency ms | Slip % | m15 buys | m15 sells | Wipes (recorded ≤-$3.50) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|''')
for k,f in d['features'].items():
 out.append(f"| {k} | {f['n']} | {f['signal_gain_pct']['median']:.2f} | {f['liquidity_usd']['median']:.1f} | {f['entry_latency_ms']['median']:.0f} | {100*f['slip_from_signal_pct']['median']:.2f} | {f['m15_buys']['median']:.1f} | {f['m15_sells']['median']:.1f} | {f['wipes']} |\n")
text('''
Re-admissions and fresh entries have almost identical median gains (177.3%/175.75%), but re-admissions have lower median quoted liquidity and much **faster** entry latency (1,415/3,154ms). Among re-admissions, losers have higher gain than winners (186.6%/160.4%) and less-negative slip (-9.38%/-16.38%), yet higher liquidity ($81,432/$73,352) and nearly identical latency (1,394/1,436ms). There is no clean low-liquidity or slow-fill discriminator. These medians are descriptive; the cells are too small to claim an adjusted predictive effect.

The important distinction is loss severity: re-admissions actually win more often (35% versus 28.6%) but have smaller average winners ($0.779/$1.353) and larger average losers ($2.224/$1.270). Three re-admission wipes total -$20.70, **88.2% of their net loss**; one fresh wipe is -$6.87. Excluding wipes as an explicitly hindsight-only diagnostic reverses the expectancy ordering: re-admissions n=17, -$2.76, PF 0.664, exp -$0.1624; fresh n=27, -$7.71, PF 0.584, exp -$0.2856. A sticky veto mainly avoided observed catastrophic exposure here; it did not identify universally worse ordinary trades.

The association is also unstable across subgroups: re-admissions are worse on 15m, better on 5m; at gain ≥100% their expectancy is slightly less negative than fresh (-$0.834/-$0.873). The extreme <100% contrast is only 2 versus 4 entries. No stronger claim of an independent veto-memory effect survives this small-sample audit. Removing re-admissions retrospectively saves $23.46 but leaves a losing fresh cohort; it is neither a policy replay nor evidence of profitable redeployment.

## 6. Latency × slip

Nearest-rank quintiles on all 145 observed BSC latencies, keeping ties together: Q1 [586,1252]ms; Q2 (1252,1577]; Q3 (1577,1949]; Q4 (1949,2836]; Q5 (2836,45698]. These match iteration 4 because all enriched records are BSC. Missing n=18 is explicit. Thresholds characterize this sample only.
''')
table('Latency marginal',dict(sorted(s['latency'].items())))
table('Latency × slip sign (nonempty cells)',{k:v for k,v in s['latency x slip'].items() if v['n']})
text('''All zero-slip cells have n=0; observed latency never pairs with missing slip, and missing latency never pairs with observed slip. These empty cells have net $0 and undefined PF/WR/expectancy.

**Slow + positive is toxic, but is not uniquely or maximally toxic.** Q5 positive has exp -$1.568/PF 0.235; Q3 positive is worse at -$3.179/PF 0, and Q2 positive -$2.717/PF 0.013. Positive slip underperforms negative in all five quintiles, including the fastest. The positive-minus-negative expectancy gaps Q1→Q5 are -$0.385, -$2.243, -$2.669, -$2.071, -$0.959: no monotonic interaction. Even negative-slip cells all lose. Each sign cell has only 7–22 trades, and positive slips come from the pre-veto policy era; this is association, not a latency intervention. Quote/data age remains unmeasured by `entry_latency_ms`.

## 7. UTC hour and session refresh

The literal “no profitable hour” claim is **false** and was not iteration 4 Astra's qualified finding. The correct statement is **no profitable hour established at n≥30**; no individual hour even has 30 observations (maximum 13). Below, best/worst means ranked by net dollars, not optimized expectancy. For BSC, 02/14/12 are also the top-three expectancy hours (different order); the bottom-three expectancy hours are 17/21/22, whereas net-loss ranking includes 20 because it has more observations. Hours 09 and 13 have no BSC entries.
''')
h=s['hour UTC BSC']; order=sorted(h,key=lambda k:h[k]['net'],reverse=True)
table('BSC best three / worst three by net',{k:h[k] for k in order[:3]+list(reversed(order[-3:]))})
table('All BSC hours (UTC)',dict(sorted(h.items())))
table('BSC fixed six-hour sessions',{f'{int(k):02}–{int(k)+5:02}':v for k,v in sorted(s['six-hour UTC BSC'].items(),key=lambda kv:int(kv[0]))})
h=s['hour UTC all pairs costed']; order=sorted(h,key=lambda k:h[k]['net'],reverse=True)
table('Full 206-pair book, supplemental costed, best/worst three by net',{k:h[k] for k in order[:3]+list(reversed(order[-3:]))})
text('''The fourth positive hour is 04 UTC (BSC n=6 +$1.69; all-chain costed n=6 +$1.18). All four fixed six-hour sessions lose; three BSC sessions have n≥30 and one has n=29. The all-chain best hours reproduce iteration 4 exactly (14/02/12); all-chain worst are 09/17/21. Chain/date mix and uncertain legacy clocks are sufficient reasons not to promote hourly scheduling.

## 8. Updated structural ranking and falsification

The four original hypotheses retain their order; confidence shifts within them. The criteria below are prospective tests, not claims that these data already establish mechanisms. Use fixed definitions, same-day comparable cohorts and executable net outcomes; aggregate buy counts and exit-rule names do not establish holder behavior or LP removal.

| Rank | Mechanism | New evidence and confidence update | Concrete evidence that would kill the mechanism |
|---|---|---|---|
| 1 — late distribution / exit liquidity | Large observed spikes select pools where earlier holders are distributing into new demand; both ordinary continuation and tails fail. | Still broadest explanation. First actual entries alone lose $165.35; fresh non-vetoed entries also lose. Re-admission losers have higher median gain than their winners, but subgroup reversals and near-identical cohort gains prevent claiming gain explains veto memory. Retain the prior gain inversion; do not rerun it as new evidence. | In ≥200 fresh comparable candidate outcomes across ≥20 UTC days, ≥100%-gain candidates have positive costed expectancy and the day-block 95% CI for high-minus-low-gain expectancy is entirely ≥0 after fixed age/liquidity/window stratification, with holder-flow tapes showing no excess prior-holder distribution. A verified price/timestamp correction eliminating the gain inversion would also invalidate the current evidence. |
| 2 — discontinuous exits / removable liquidity | Reserves or executable sell depth disappear faster than exit triggers can obtain a viable fill. LP removal is one possible cause, not an observed fact. | Strengthened as a tail-focused diagnosis: re-admission wipes account for 88.2% of cohort net loss; excluding them reverses cohort expectancy ordering. BSC lifetime 24 recorded-dollar wipes total -$134.52 (78.1% of net loss). Liquidity medians fail to identify losers. | Audit ≥30 new wipe events with adjacent-block reserves and executable quotes. If ≥90% retain sellable depth within 1% of the pre-event mark through the paper exit, and corrected accounting removes the wipes, reject discontinuous execution as dominant. If quotes collapse through swaps while LP supply/ownership stays stable, reject LP-removal specifically, not the broader discontinuity mechanism. |
| 3 — scanner ranking / eligibility selection | Ranking toward extreme gain and available slots selects worse opportunities from the candidate universe. | No source variation; the worse 5m path is a window association only. First-entry losses rule out repeated ownership as a sufficient explanation. Source and S1 cuts do not directly test ranking, so this remains weakly identified despite the code-level rationale established in iteration 4. | On ≥200 new same-scan eligible selected/alternative shadow comparisons across ≥20 days, with identical notional/exits/costs, selected candidates have no disadvantage: the 95% day-block CI for selected-minus-alternative net expectancy is entirely ≥0, including within fixed window and slot-availability strata. This kills the proposed adverse ranking direction. |
| 4 — stale signals / signal-to-fill repricing | Stale upstream marks or repricing cause the bot to buy exhausted flow. | Repricing remains strongly associated with losses within every latency quintile. A pure slow-entry story is weakened: Q3 positive is worse than Q5, and veto re-admissions are faster than fresh. Empty micro-bands leave the fee-noise explanation untested. | Collect synchronized market timestamp, receive/decision/quote/fill epochs and fee-separated executable quotes. On ≥200 fresh observations across ≥20 days, if verified fresh quotes reproduce the same losses and 95% day-block CIs for both stale-minus-fresh and positive-minus-nonpositive expectancy sit wholly inside ±$0.10 per standardized $7 entry, kill a materially important staleness/repricing explanation. If only latency lacks an effect, reject the speed-only variant, not upstream data-age effects. |

**One additional structural hypothesis (no second proposed): episode-level hazard persists after a veto.** A token that first triggers a chase veto may remain in a volatile distribution episode when its price later falls enough for admission. Treating each fresh quote as a fresh opportunity forgets adverse episode history. This is distinct from S1, which requires previous ownership: all 20 observations here were first purchases. Support is the re-admission tail concentration despite faster fills and similar median gain. Weakness is the reversal after removing wipes and the window/gain interaction reversals. Falsification: prospectively compare prior-veto and never-vetoed admissions, with at least 100 per arm across ≥20 days, matched within day/window and predeclared gain/liquidity bands and standardized notional. Kill a material persistent-hazard effect if the 95% upper confidence bounds are <5 percentage points for excess wipe rate and <$0.10 per $7 entry for excess expected loss. Confidence intervals must respect episode/mint clustering. Merely reproducing PF<1 in both groups does not confirm it.

**What remains unknowable:** LP/holder fields have 0/206 usable entry values; no reserve/sell-tax tape or consistent executable counterfactual fills; no complete historical rank/reject universe, scan IDs, or randomized latency intervention; no micro-positive entries, no source alternatives, only one repeat purchase; pool-age coverage 6/206. Missing observations cannot be relabeled zero or inferred from outcomes. Candidate repeat counters are not lifelong ownership counters. A quote mark and a pool liquidity scalar cannot prove executable depth. Recorded realized P&L need not equal a simple entry-to-exit mark return because rungs and paper execution conventions matter; this report consistently uses recorded USD for the requested baseline. Risk-state resets and policy changes preclude interpreting pre/post PF as a treatment effect.

## 9. F2 pool-age prospective protocol — freeze before new outcomes

**Rule and rationale:** test “retain pool age ≥900 seconds at entry; reject 0≤age<900 seconds.” Equality is retained. This is one complete 15m signal window since pool creation, a structural maturation boundary rather than an optimized cut. It is a hypothesis that a full observation window reduces launch transients, not a claim that 15 minutes guarantees safety. The six pilot ages are 5.47, 5.78, 6.10, 7.40, 8.38 and 10.12 minutes: all young, recorded net -$11.23, costed -$11.70, one wipe. They show launch exposure and **no older control**, so they cannot locate or validate a cutoff. Exclude all six from confirmatory data.

**Collection and missingness:** start at the coordinator's recorded freeze epoch after this report; BSC only, otherwise unchanged guardrails/strategy. Run the age rule in shadow while the existing paper policy supplies both arms; do not enable an age veto during collection. Age = verified entry epoch minus numeric pool creation epoch known before admission; candidate backfill requires an exact pool/entry join to a pre-entry record. Missing/nonfinite/negative ages are “unknown,” excluded from the two-arm test and separately reported; proposed operational fallback remains existing policy for unknown age. Require ≥90% valid age coverage. Count one first actual entry per mint after freeze, no pilot/historical backfill, and keep open/unresolved positions visible rather than selectively dropping them. Use fixed $7 notional shadow P&L with the same recorded price path, exits and Track B native fixed-cost model in both arms, plus 0.45%-per-leg slippage stress; reconcile rungs/taxes before scoring.

**Gates and verdict:** first diagnostic checkpoint is ≥30 completed outcomes per age arm; do not peek for an earlier success. KILL the profitability proposal if older-arm stressed PF≤1 or its costed expectancy is no better than young at that checkpoint; this is not proof that age has zero effect. Otherwise EXTEND without changing 900s. SHIP eligibility requires ≥200 fresh outcomes across ≥20 UTC days, ≥10 days containing both arms, first 100 older outcomes as calibration and a subsequent frozen chronological holdout containing ≥30 per arm. In that holdout require older costed/stressed PF>1, positive expectancy, PF retention ≥60% versus calibration, WR≤90%, and older PF improvement ≥0.25 over the same holdout's all-age baseline. Additionally require positive one-sided 97.5% lower bounds for stressed older expectancy and older-minus-young stressed expectancy, using 10,000 UTC-day block bootstrap replicates, seed 5; report mint/episode concentration. Require profitability after deleting the largest two older holdout winners. Unknown-age fallback must not make the combined proposed policy's stressed expectancy nonpositive. Failure of coverage, sample or uncertainty gates means EXTEND at the 30/side checkpoint; at the single final ≥200/20-day checkpoint, failure of any performance gate means KILL, while insufficient sample/coverage permits extension to 400 outcomes/40 active days, with no intervening success looks. At that cap, failure of any gate means KILL; all gates passing means SHIP eligibility for coordinator review under existing safety constraints. “Ship” here means the paper admission screen only, never authorization for live funds. No extension changes threshold, outcome model or cohort rules.

## 10. Disposition and scope

No edge demonstrated and no bot change proposed. Preserve the distinctions: R3 unobserved, S1 almost unobserved, source unidentifiable, sticky-veto tail association descriptive, latency speed-only account weakened. F2 remains prospective and unverified.

Only the two analysis scripts and this one report were written in the repository. Runtime trades/state/logs/keys/wrapper/ACTIVE were not modified; no bot code, process signals, restarts, or subagents. No strategy test suite was run because no strategy code changed. Validation here is baseline reproduction, strict-pair reconciliation, complete partition checks and explicit reproduction of the prior 20/28 cohort.
''')
assert d['close_count']==215 and d['pairs']==206 and d['baseline']['n']==163
assert abs(d['baseline']['net']+172.34)<1e-6
for name in ['slip micro-bands','first/re-entry','source','source x window','latency','latency x slip','hour UTC BSC','six-hour UTC BSC']:
 assert sum(v['n'] for v in s[name].values())==163,name
 assert abs(sum(v['net'] for v in s[name].values())+172.34)<.001,name
assert s['post veto']['re-admission']['n']==20 and s['post veto']['fresh']['n']==28
assert s['post veto']['re-admission']['net']==-23.46 and s['post veto']['fresh']['net']==-14.58
(ROOT/'hidden_files/deep-improvement-loop/iter-5/astra_analysis.md').write_text(''.join(out))
print('Report written; baseline and partition checks passed.')
