# predict-bot

A paper-trading bot for prediction markets (Polymarket + Kalshi) that combines
the two patterns people are actually running online:

1. **Mispricing detection** — scan hundreds of markets, estimate a "fair"
   probability independently of the quoted price, flag gaps above a threshold.
2. **Self-improving loop** — paper-trade the flags, grade every outcome, and
   tune the strategy parameters (threshold, sizing, filters) from realized
   results instead of vibes.

## Paper-only, by construction

`live_trading` defaults to `false` and is enforced in three places:

- `run_loop.py` refuses to start unless the configured broker is the paper broker.
- The live adapters (`KalshiLiveAdapter`, `PolymarketLiveAdapter`) raise
  `LiveTradingDisabled` unless `live_trading: true` is set in `config.yaml`
  **and** the required API credentials are present in the environment.
- There is no code path that silently upgrades a paper fill to a real order.

Live integration is supported as an architecture, not a switch: implement the
`BrokerAdapter` interface for each venue (order placement, cancellation, fill
reconciliation), keep every paper-mode test passing against the same interface.

## How the mispricing estimate works (v1)

No LLM, no API keys, no vibes. The independent "fair probability" for a market
is the **other venue's price for the same event**:

- Scan Polymarket (public Gamma API, no auth) for active binary markets above
  minimum volume/liquidity.
- Normalize event titles and match them against Kalshi's public market list.
- A signal fires when `|P_polymarket - P_kalshi| >= mispricing_threshold`
  (default 8%) on the same side after fees/slippage haircut.

Markets with no cross-venue match are logged and skipped — the bot only trades
what it can independently price. This is deliberately conservative: the
self-improvement loop's job is to find which gaps actually converge, not to
hallucinate edges.

## Self-improvement loop

Every signal and every paper fill is journaled to `data/journal.jsonl` with the
strategy version that produced it. When enough trades resolve:

1. Grade each resolved trade (win/loss, realized edge captured vs. expected).
2. Coordinate-descent the strategy parameters — mispricing threshold, Kelly
   fraction, minimum liquidity, category allowlist — against realized P&L.
3. Persist the winner as a new `strategies/strategy_v{N}.yaml`; the runner
   always loads the latest champion.

Losing configurations are kept in the journal, never deleted — the bot learns
from the full history.

## Quick start (paper)

```bash
cd ~/workspace/predict-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m src.run_loop --once        # single scan → detect → paper trade → grade
python -m src.run_loop --loop 600    # continuous, one cycle every 600s
```

## Layout

```
config.yaml            # strategy + venue config (paper by default)
src/
  run_loop.py          # main loop: scan → detect → trade → mark → grade → improve
  scanner.py           # Polymarket Gamma API scanner, Kalshi public list
  mispricing.py        # cross-venue matching + gap detection
  paper_broker.py      # paper positions, Kelly sizing, settlement, journal
  improve.py           # grading + parameter tuning + strategy versioning
  adapters/
    base.py            # BrokerAdapter interface
    paper.py           # paper implementation
    kalshi_live.py     # stub — live order routing for Kalshi (disabled by default)
    polymarket_live.py # stub — live order routing for Polymarket (disabled by default)
tests/                 # unit tests, paper-mode only
data/                  # journal + strategies (created at runtime)
```

> **Note (2026-10-02):** `scripts/same_day_loop.py` — the live 15-min
> scanner — does not read `config.yaml`. Its decision constants
> (`EDGE_THRESHOLD`, `AGREE_LEAN_MIN`, `WINDOW_WARMUP_MIN`, …) are hardcoded
> at the top of the script, and the 15-min entry path has no spread or volume
> filter (`spread` is logged, never gated; `volume` is printed in the alert
> signal but is now also persisted to state/grade rows (iter-16 E.2);
> volume gates exist only in the
> cross-venue section).

## Safety notes

- Prediction markets are restricted in some jurisdictions; Polymarket does not
  serve US users for real-money trading. Paper mode has no such restriction —
  it only reads public data.
- The paper broker marks to market and settles at resolution; unresolved
  positions sit in the journal as open, never force-closed.
- Set `max_open_positions`, `daily_trade_cap`, and `bankroll` in `config.yaml`.
  The improvement loop may only tighten risk parameters, never loosen them.
