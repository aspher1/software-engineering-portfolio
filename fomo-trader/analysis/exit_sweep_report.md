# Track B exit sweep — 2026-09-27

**Verdict: no exit change is recommended.** This is offline analysis of paper journal data. The most favorable in-sample region improved the modeled relative return, but its out-of-sample net remained negative under both base costs and 3× slippage. No parameter set passed every validation gate. The exact JSON Patch for integration is `[]` (no changes to `runs/paper-1h/config.json` → `exit`).

## Data and method

The read-only journal snapshot contained 70 entries and 73 closes; three closes have no paired entry. Closes were sorted by `close_ts`. The first 45 are in sample (IS), and the remaining 28 are out of sample (OOS). Round 2 used 45/22 because its journal snapshot had 67 closes. The journal can grow while the paper bot runs, so these numbers describe this sweep's snapshot.

The sweep evaluated 467 distinct sets: one-factor coarse changes from the current exit config, followed by pairwise combinations around the eight strongest IS candidates. It swept single TP rungs at +30/+40/+50/+60/+80/+100% and 25/50/75/100% sold; ascending two-rung ladders with 25/50/75% first and 25/50/75/100% second fractions whose sum does not exceed 100%; trailing stop 10/15/20/25/30/40%; hard stop 20/25/30/35/45/50%; stale timeout disabled/10/15/30/45/60 minutes and max gain 5/10/15/20%; dump drop disabled/8/12/15/20% and window 30/60/120 seconds; and trail after TP 8/12/15/20/25%. Mode, venue dump tripwire, risk caps, and position size were held fixed.

The replay uses the recorded realized paper P&L as its anchor. It adjusts only the difference between a candidate's modeled proceeds and the current config's modeled proceeds, then applies the existing estimated cost model to each trade: 0.25% swap fee and 0.15% slippage per leg, plus the priority-fee cap or BNB gas estimate. Stress triples slippage. This subtraction avoids crediting a candidate merely for baseline price-math errors. Native balances are kept by chain; aggregate net, profit factor, and drawdown are in approximate USD using replay's recorded quote or disclosed fallback.

## Baseline and strongest exploratory region

The strongest IS point was TP `[[30,100]]` with trailing stop `10` and every other exit parameter at the current value. It is **not a recommendation**. Nearby `[[30,75],[40,25]]` and `[[40,100]]` ladders and 15–20% trails also had positive IS edge, so the IS peak has a local plateau across TP shape and trail. The 10% vs 15–20% trail made no OOS difference in this snapshot; that axis has no OOS support. The leading point retained 86.4% of its IS per-trade edge OOS (+$0.818 IS, +$0.706 OOS), but OOS stress net was **−$27.62**. Its OOS base net was also **−$26.47**. The stress gate alone rules it out.

| Scenario | Split | Closes | Net USD | Win rate | Profit factor | Max drawdown USD |
|---|---:|---:|---:|---:|---:|---:|
| Current exit, base costs | IS | 45 | −$75.63 | 35.6% | 0.282 | $75.63 |
| Current exit, 3× slippage | IS | 45 | −$77.75 | 35.6% | 0.270 | $77.75 |
| Current exit, base costs | OOS | 28 | −$46.24 | 10.7% | 0.227 | $52.27 |
| Current exit, 3× slippage | OOS | 28 | −$47.27 | 10.7% | 0.220 | $53.10 |
| Exploratory TP +30%/100%, trail 10%, base | IS | 45 | −$38.83 | 48.9% | 0.385 | $42.14 |
| Exploratory TP +30%/100%, trail 10%, 3× | IS | 45 | −$41.17 | 48.9% | 0.359 | $44.04 |
| Exploratory TP +30%/100%, trail 10%, base | OOS | 28 | −$26.47 | 35.7% | 0.374 | $30.55 |
| Exploratory TP +30%/100%, trail 10%, 3× | OOS | 28 | −$27.62 | 35.7% | 0.357 | $31.60 |

The recommendation's exact JSON Patch against the current `exit` object is:

```json
[]
```

No config change is proposed. The exploratory point would alter TP and trail, but it failed the mandatory OOS stress gate and therefore has no integration patch.

## Counterfactual limits

The journal records entry, lifetime peak, final exit, reason, and sometimes entry time; it does not record the quote path, rung timing, pool depth, or executable liquidity. TP rungs are credited only when lifetime peak reaches their threshold, with sold fractions at the threshold and the remainder at a modeled final exit. Those threshold fills may never have been available for the desired size. A tighter trail replaces a recorded trail exit; a wider trail cannot infer a later fill. A tighter dump threshold is estimated only for a recorded dump with the same time window, using the recent maximum implied by the recorded drop and exit. Other windows, a disabled detector, and wider thresholds cannot reconstruct future prices. Stale thresholds can be classified at the recorded close, but an earlier timeout has no recorded price, so changing stale settings creates no invented fill. A hard stop can improve a lower recorded final exit unless the trade has a rug gap. For any exit over 60% below lifetime peak, the final exit remains recorded: stops cannot fill through a liquidity vacuum. TP proceeds on such a trade remain an optimistic threshold assumption. These limitations make the analysis unsuitable as evidence of executable returns.

Validation requires an OOS per-trade edge at least 60% of IS, win rate at most 90%, no single chain/bucket/reason dominating positive edge, a broad IS plateau rather than an isolated point, and positive OOS net under 3× slippage. The sweep produced no passing set. All work stayed offline; the paper config and bot were untouched.

Verification: `tests/test_exit_sweep.py` has 9 hermetic tests; the full hermetic suite finished **100 passed, 0 failed** with `.venv/bin/python -m pytest tests/ -q`.
