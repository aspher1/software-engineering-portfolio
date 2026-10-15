# Builds — Randy Kawka

I build software against real data and measure what happens. Every project
here runs against live public data, has a test suite or validation
methodology, and reports honest numbers — including negative results.

## Projects

### 1. [kalshi-combo-scanner](kalshi-combo-scanner/) — prediction-market combo picker
Sweeps ~131,000 open Kalshi markets every 5 minutes hunting mispriced
multi-leg combos (single-market Yes+No, exhaustive event books,
Kalshi-vs-Polymarket gaps), with real taker-fee math and depth checks.
**The honest finding:** after-fee locked edges are effectively zero, and the
code documents exactly why (38 live near-arb hits, all −0.21¢/set). 332
tests. Paper/analysis only — no orders, no credentials, ever.

### 2. [predict-bot](predict-bot/) — 15-minute crypto signal engine
Prices Kalshi 15-minute crypto up/down windows against public Kraken spot
with a fair-value model. Every signal is graded against actual Kalshi
settlement (not backtest fantasy), and P&L is quoted net of fees on
executable bid/ask quotes only. Paper-only by construction — the live
adapters refuse to run without explicit credentials that are never
configured.

### 3. [fomo-trader](fomo-trader/) — real-time memecoin system (Solana)
Hunter scans ~450 pools every 30s for violent buy-pressure pumps; a
per-position manager runs take-profit ladders, trailing stops, and hard
stops on a 5-second loop. Hard risk guardrails throughout: max 3 positions,
10 trades/day, daily loss kill-switch, rug guard on every entry. Operated
in paper/dry-run mode.

### 4. [paper-trader](paper-trader/) — validation-first paper trading framework
A paper trading framework with a falsification-first bar: no strategy ships
unless it clears 30–50 closed paper trades, profit factor > 1.2,
walk-forward validated, max drawdown < 15%. The framework exists to kill
bad ideas with data, not to confirm them.

### 5. [pricestalker](pricestalker/) — self-hosted price tracking (full-stack)
Fork of PriceStalker: FastAPI backend, Postgres, Docker Compose, web
frontend with price/stock tracking, multi-currency alerts, and
retailer-specific selectors. Deployed self-hosted.

## How this was built

These projects were built with AI coding assistants (OpenAI Codex, Cursor)
as pair programmers. I directed architecture, chose what to validate,
decided the ship criteria, and killed what the data didn't support. The
trading and prediction-market systems are paper/analysis only — they never
place real orders or move real money.

## Contact

- GitHub: [@aspher1](https://github.com/aspher1)
- LinkedIn: [randy-kawka](https://www.linkedin.com/in/randy-kawka)
