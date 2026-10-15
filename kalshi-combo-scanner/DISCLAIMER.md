# Disclaimer

**Paper / analysis only.** This software reads public market data and prints
analysis. It places no orders, routes no orders, holds no account credentials,
and never moves money. Every network call is a public, unauthenticated read.
Write-capable trading surfaces (order, RFQ, quote and combo-market creation
endpoints) are refused in code by a fail-closed guard.

**Estimates, not guarantees.** Every Tier 3 model-edge entry and every Tier 4
combo pick is a model **estimate** of fair value. It is never a guaranteed
profit, and no win rate is promised or implied. Markets can move between a
scan and any later action, fees and fills can differ from what is modeled, and
the models can simply be wrong. Tier 1 "locked" combos are arithmetic on quoted
prices at scan time; they too can vanish before anyone could act, and
cross-venue resolution rules can differ.

**Backtests are backtests.** Any replay of historical data in this repository
is labeled as a backtest, uses small samples, and says nothing reliable about
future results. No backtest of the combo picker exists yet.

**Not financial advice.** Nothing here is investment, trading, legal or tax
advice. You are responsible for your own decisions and for complying with the
laws and venue terms that apply to you.

**No insider or copy-trading functionality.** The software does not identify,
follow or copy other traders, wallets or accounts, and it contains no volume
incentives or wash-trading mechanics.

**Data terms.** Market data comes from public endpoints (Kalshi, Polymarket,
Kraken) and is used within a personal, research and analysis scope. It is not
intended for resale or redistribution, including to market-data distributors
or institutional ("capital market") clients. Check each venue's current terms
before using its data.

**US persons.** Polymarket's international platform is close-only for US
persons. This software does not execute or route orders on any venue, for any
person, in any jurisdiction.
