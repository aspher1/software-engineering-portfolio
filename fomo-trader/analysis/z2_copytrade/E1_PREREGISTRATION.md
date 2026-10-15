# E1 smart-wallet copy-trading pre-registration

Frozen 2026-09-27 UTC, before any E1 evaluation run. This is an offline paper validation harness, not a trading system. Characterizations below are research leads, not verified wallet identities or E1 results. Source: [d3ad-e research notes](https://github.com/d3ad-e/solana-sniper-bot/blob/HEAD/CLAUDE.md).

## Candidate leads and eligibility

| Lead | Known prefix only | Research characterization | Address status |
|---|---|---|---|
| target | `24678QK…` | 4,548 round trips / 3 days; +85.4 SOL net; 29% win rate; median entry rank 5; create-block speed likely required | address_pending_resolution |
| leader | `E4EzX…` | 1,006 round trips / 3 days; +631.6 SOL gross; 70% win rate; shred-stream read likely required | address_pending_resolution |
| drafter | `57stAM…` | 754 positions / 4 days; +960.7 SOL; 66% win rate; median entry 46 blocks after creation; zero create-block entries | address_pending_resolution |

Resolve complete addresses from the public source at experiment time and record source URL and address in the run log. Never extrapolate a prefix. Vet 3–5 generalist wallets: at least 100 distinct token creators, at least 100 round trips across at least 3 days, no insider pattern (<20 creators), and no top-three trades contributing over 50% of net profit. Missing or malformed evidence fails screening. A candidate descriptor alone is never vetted.

## Frozen protocol

- Delays: **5, 15, 30, 60, 120 seconds**; 30 seconds is the retail anchor.
- Chronological split per wallet: first two thirds of leader trades IS, last third OOS. Sort by timestamp and transaction signature before splitting. A position is assigned by its leader BUY; both legs must be observed to score a close. No split boundary may feed later trades into earlier decisions.
- Copier BUY and SELL occur at the corresponding leader action timestamp plus delay. Fill from the first available minute close at or after that time, with no lookback or interpolation. Missing bars skip the opportunity and increment a counter.
- Size cap: **$7 per entry**. Research copy fee: **100 bps per side**. Assumed venue fee: **30 bps per side**. Assumed slippage: **100 bps per side**. Priority fee: **2,000,000 lamports per side**, converted with a supplied contemporaneous SOL/USD price. Report assumptions prominently; sensitivity analysis may vary them but does not replace this primary specification.
- Net = gross proceeds minus principal minus both legs' variable fees, slippage, and priority fees. Position size and gross proceeds use the observed paper fill prices.
- Seed: **20260927** for any sampling or tie resolution; deterministic ordering takes precedence.
- **KILL_WALLET if OOS net ≤ $0 after costs at ≥30 OOS closes. STOP_ALL if cumulative OOS ≤ −$21. No verdict before ≥100 total trades.** Apply the wallet rule at the first qualifying 30th OOS close, then at each subsequent close; stop new entries at the global threshold. Report censored and insufficient-history cases explicitly.

E1 is **runnable** only when at least one wallet has a complete resolved address and at least 100 leader trades of history. It is **decision-capable** only after wallet vetting and at least 30 OOS closes. No candidate in this document currently meets those evidentiary requirements.
