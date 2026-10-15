# E1 tape replay run log (PAPER only, verdict-only)

Offline research. Nothing here is wired into live entries; no position size, kill switch, rug guard or keypair was touched. No result below is a profitability or live-readiness claim.

## Sources

- Research doc: [d3ad-e/solana-sniper-bot CLAUDE.md](https://github.com/d3ad-e/solana-sniper-bot/blob/HEAD/CLAUDE.md) (local copy `~/workspace/e1-tape/CLAUDE.md`).
- Tape: `https://replay.pumpapi.io/YYYY/MM/DD/HH.jsonl.zst` (UTC hours; days 2026-08-13, 08-20, 08-23, 08-24), stream-filtered by `~/workspace/e1-tape/dl_filter.py` to rows whose `txSigner` or a `transfers[].from/to` party is one of the three wallets, staged at `.e1_tape_staging/<day>/<hour>.jsonl`.
- Wallets (full addresses from the research doc): target `24678QKx2Dy8ZCw6Ra8o9DeTqPLL5GR9ZQKxt5FddHmq`, leader `E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz`, drafter `57stAMFvwctAjkBS76RXGoK4QKyS1QoxbGMbzFFe4DyZ`.
- Target reference signatures: `~/workspace/e1-tape/target_sigs.json` (about 30k signatures with slot and blockTime, no prices), used only for the recall cross-check.
- SOL/USD (CoinGecko daily): 08-13 $75.56, 08-20 $85.33, 08-24 $95.41; 08-23 not retrieved, so it is null and affected closes are skipped and counted.
- Baseline: `runs/paper-1h/trades.jsonl` (bot paper journal). Reported alongside, never an E1 input.

## Assumptions and deviations from the frozen pre-registration

- **Field mapping (verified schema):** actor `txSigner`; size `quoteAmount` (SOL); price `price` (SOL/token, post-trade bonding-curve spot, about `vQuoteInBondingCurve / vTokensInBondingCurve`); timestamp `timestamp` in ms; signature `signature` (base58); slot `block`; side `action` (buy/sell only; transfer/create/migrate are not trades).
- **Price choice:** `price` (spot after the print) is used rather than the effective average `quoteAmount / tokenAmount`. The spot after a print is what the next taker faces, and it is reported for every print. On the sampled row the two differ by about 2%.
- **Attribution:** `txSigner` when it is a known wallet, else the first known `transfers[].from/to` party, else the row is skipped and counted (`unattributed_trade_row`). Unattributed but complete prints still feed the price path.
- **Fill rule (deviation):** the pre-registration specifies minute closes. This replay fills from the first same-mint staged print at/after the delayed decision time, within 60 s (the same staleness cap as the GeckoTerminal path). Later prints skip the opportunity and are counted.
- **Sparse, wallet-conditioned prints (major caveat):** the staged tape keeps only rows that touch the three wallets, so the price path mostly sees the leaders' own prints (for example, ladder legs), not the full market. Fills are therefore not representative of what a copier competing with all takers would get, and many lookups will be missing. A full same-mint tape would be needed for fill fidelity.
- **Screen inputs:** `distinct_creators` is null-carried (the rows' `creatorFeeAddress` is a fee recipient, not a verified creator, and is not used). `net_sol` and `top3_net_sol` are not derived. The screen therefore fails closed and each wallet is `paper_only_unvetted`; `allow_unvetted=True` lets the paper replay compute, but no wallet is decision-capable.
- **Kill rules (unchanged):** KILL_WALLET if OOS net ≤ $0 after costs at ≥30 OOS closes; STOP_ALL if cumulative OOS ≤ −$21; runnable only with ≥100 leader trades per wallet; seed 20260927. Delays 5/15/30/60/120 s with a 30 s retail anchor. Costs: $7/entry cap, 100 bps copy fee/side, 30 bps venue/side, 100 bps slippage/side, 2,000,000 lamports priority/side.
- **Power warning:** OOS is the last third of leader trades, and each close is assigned by its leader BUY. With laddered exits (the drafter averages about 5 sells per buy), 30 OOS closes needs several hundred leader trades per wallet. The downloader's stop rule (≥150 rows per wallet, transfer rows included, max 24 hour-files) is expected to leave every wallet short of 30 OOS closes, which means no kill-rule verdict is possible.

## Drafter published-trades leg

**not_runnable.** The research doc publishes only aggregate drafter statistics (754 positions, +960.7 SOL, 66% win, ROI quantiles, entry offsets). The per-position table it references, `analysis/data/w57st_positions.parquet`, is local to the researcher and not in this repo (`analysis/data/` does not exist). No per-trade timestamps or prices are available, and no data was invented.

## Session log

### 2026-09-27 ~21:35-21:50 UTC: Cursor fallback after Codex quota exhaustion

- Fixed the adapter field mapping, added `tests/test_z2_tape_adapter.py` and `run_tape_e1.py`.
- **Execution was blocked in this session:** tool approval rejected every repo write and every shell command except `ls`, so the files were staged under `/tmp/e1_staged/` and **no tests or replay were run**. No results were produced in this session.
- Final tape state: `DOWNLOAD_DONE` written 21:36 UTC (`done=True`, 3 files). Hours `2026-08-13/00`, `/01` and `/02` were reported `ok=True` with no curl WARN, totalling 1,527 rows (cumulative per-wallet rows, transfers included: target 492, leader 496, drafter 539). Only 08-13 is covered, so every close uses $75.56 SOL/USD, and the 08-23 null rate never applies.
- Machine-generated run sections are appended below by `run_tape_e1.py`.

## Run 2026-09-27T21:36:47+00:00: tape E1 replay (PAPER only, verdict-only)

- Runner: `analysis/z2_copytrade/run_tape_e1.py`; seed 20260927; DOWNLOAD_DONE present: True
- Tape source: https://replay.pumpapi.io/{YYYY}/{MM}/{DD}/{HH}.jsonl.zst (filtered to the three wallets by `~/workspace/e1-tape/dl_filter.py`)
- Hours processed (3): 2026-08-13/00, 2026-08-13/01, 2026-08-13/02
- Hours excluded (in progress / not reported complete): none
- Hours excluded (truncated download): none
- Window: 2026-08-13T00:00:00+00:00 to 2026-08-13T03:00:00+00:00
- Adapter counts: `{"market_prints": 788, "non_trade_action": 739, "non_trade_action:transfer": 739, "rows_seen": 1527, "valid_trades": 788}`
- Price path: first same-mint staged print (any wallet) at/after decision ts, within 60 s; missing lookups 2070
- SOL/USD by day: `{"2026-08-13": 75.56, "2026-08-20": 85.33, "2026-08-23": null, "2026-08-24": 95.41}`; missing lookups (one per opportunity per delay): `{}`

| wallet | label | screen | status | leader trades | delay s | OOS net $ | OOS closes | IS net $ | decision |
|---|---|---|---|---|---|---|---|---|---|
| `24678Q…` | target | FAIL: window_days_below_3, invalid_distinct_creators, nonpositive_or_invalid_net, missing_or_invalid_top3_net | paper_only_unvetted | 246 | 5 | -0.35 | 2 | -0.62 | INSUFFICIENT_OOS_CLOSES |
| `24678Q…` | target | FAIL: window_days_below_3, invalid_distinct_creators, nonpositive_or_invalid_net, missing_or_invalid_top3_net | paper_only_unvetted | 246 | 15 | -1.25 | 1 | -0.62 | INSUFFICIENT_OOS_CLOSES |
| `24678Q…` | target | FAIL: window_days_below_3, invalid_distinct_creators, nonpositive_or_invalid_net, missing_or_invalid_top3_net | paper_only_unvetted | 246 | 30 | 0.00 | 0 | -0.62 | INSUFFICIENT_OOS_CLOSES |
| `24678Q…` | target | FAIL: window_days_below_3, invalid_distinct_creators, nonpositive_or_invalid_net, missing_or_invalid_top3_net | paper_only_unvetted | 246 | 60 | 0.00 | 0 | 0.00 | INSUFFICIENT_OOS_CLOSES |
| `24678Q…` | target | FAIL: window_days_below_3, invalid_distinct_creators, nonpositive_or_invalid_net, missing_or_invalid_top3_net | paper_only_unvetted | 246 | 120 | 0.00 | 0 | 0.00 | INSUFFICIENT_OOS_CLOSES |
| `E4EzXd…` | leader | FAIL: round_trips_below_100, window_days_below_3, invalid_distinct_creators, nonpositive_or_invalid_net, missing_or_invalid_top3_net | paper_only_unvetted | 262 | 5 | -2.50 | 4 | -6.48 | INSUFFICIENT_OOS_CLOSES |
| `E4EzXd…` | leader | FAIL: round_trips_below_100, window_days_below_3, invalid_distinct_creators, nonpositive_or_invalid_net, missing_or_invalid_top3_net | paper_only_unvetted | 262 | 15 | 0.00 | 0 | -3.18 | INSUFFICIENT_OOS_CLOSES |
| `E4EzXd…` | leader | FAIL: round_trips_below_100, window_days_below_3, invalid_distinct_creators, nonpositive_or_invalid_net, missing_or_invalid_top3_net | paper_only_unvetted | 262 | 30 | 0.00 | 0 | -0.52 | INSUFFICIENT_OOS_CLOSES |
| `E4EzXd…` | leader | FAIL: round_trips_below_100, window_days_below_3, invalid_distinct_creators, nonpositive_or_invalid_net, missing_or_invalid_top3_net | paper_only_unvetted | 262 | 60 | 0.00 | 0 | 0.00 | INSUFFICIENT_OOS_CLOSES |
| `E4EzXd…` | leader | FAIL: round_trips_below_100, window_days_below_3, invalid_distinct_creators, nonpositive_or_invalid_net, missing_or_invalid_top3_net | paper_only_unvetted | 262 | 120 | 0.00 | 0 | 0.00 | INSUFFICIENT_OOS_CLOSES |
| `57stAM…` | drafter | FAIL: round_trips_below_100, window_days_below_3, invalid_distinct_creators, nonpositive_or_invalid_net, missing_or_invalid_top3_net | paper_only_unvetted | 280 | 5 | 1.26 | 9 | -12.22 | INSUFFICIENT_OOS_CLOSES |
| `57stAM…` | drafter | FAIL: round_trips_below_100, window_days_below_3, invalid_distinct_creators, nonpositive_or_invalid_net, missing_or_invalid_top3_net | paper_only_unvetted | 280 | 15 | -0.62 | 1 | -2.50 | INSUFFICIENT_OOS_CLOSES |
| `57stAM…` | drafter | FAIL: round_trips_below_100, window_days_below_3, invalid_distinct_creators, nonpositive_or_invalid_net, missing_or_invalid_top3_net | paper_only_unvetted | 280 | 30 | 0.00 | 0 | -0.62 | INSUFFICIENT_OOS_CLOSES |
| `57stAM…` | drafter | FAIL: round_trips_below_100, window_days_below_3, invalid_distinct_creators, nonpositive_or_invalid_net, missing_or_invalid_top3_net | paper_only_unvetted | 280 | 60 | 0.00 | 0 | -0.62 | INSUFFICIENT_OOS_CLOSES |
| `57stAM…` | drafter | FAIL: round_trips_below_100, window_days_below_3, invalid_distinct_creators, nonpositive_or_invalid_net, missing_or_invalid_top3_net | paper_only_unvetted | 280 | 120 | 0.00 | 0 | 0.00 | INSUFFICIENT_OOS_CLOSES |

Global per delay: 5s stop_all=False cum_oos=$-1.59; 15s stop_all=False cum_oos=$-1.87; 30s stop_all=False cum_oos=$0.00; 60s stop_all=False cum_oos=$0.00; 120s stop_all=False cum_oos=$0.00

Per-wallet verdicts (30 s retail anchor): target=INSUFFICIENT_OOS_CLOSES (decision_capable=False); leader=INSUFFICIENT_OOS_CLOSES (decision_capable=False); drafter=INSUFFICIENT_OOS_CLOSES (decision_capable=False)

- Target signature cross-check (no_overlap): reference 30000 sigs spanning 2026-08-14T19:16:45+00:00 to 2026-08-24T16:47:14+00:00; 0 in processed hours; matched 0; recall all rows —, successful only —, trade rows —; tape sigs found in reference —
- Drafter published-trades leg: **not_runnable**. The research doc publishes only aggregate drafter statistics (754 positions, +960.7 SOL, 66% win, ROI quantiles, entry offsets). The per-position table it references (analysis/data/w57st_positions.parquet) is local to the researcher and absent here. No per-trade timestamps/prices are available; no data was invented.
- Baseline (NOT an E1 input): 83 closes {'solana': 52, 'bsc': 31}; net -0.440976 SOL (Solana), -0.058225 BNB (BSC); net $-49.60 over 59 USD-valued closes (22 derived from realized_sol x sol_usd); 24 closes without a USD value (-0.404968 SOL)
