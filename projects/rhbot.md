# rhbot: paper-trading research engine

**Simulation, risk controls and auditability in Python**  
**Stack:** Python 3.12, SQLite, Coinbase public bid/ask data, pytest, GitHub Actions  
**Source:** [aspher1/robinhood-bot](https://github.com/aspher1/robinhood-bot)

## Problem and approach

A strategy can look good before realistic transaction costs and operational failures. This research project compares simulated BTC/ETH buy-and-hold, periodic purchasing and trend-following strategies while recording fills, modeled fees, risk events and audit records.

## Technical evidence

- Independent simulated strategy books on a common starting balance.
- Quote-age and bid/ask validation, missing-data hard stops, configurable exposure rules and human acknowledgments.
- SQLite ledger, operational heartbeat, kill switch and audit verification.
- Offline unit tests, integration checks and [GitHub Actions CI](https://github.com/aspher1/robinhood-bot/actions/workflows/ci.yml).

[Architecture](https://github.com/aspher1/robinhood-bot/blob/main/ARCHITECTURE.md) · [Security](https://github.com/aspher1/robinhood-bot/blob/main/SECURITY.md) · [Tests](https://github.com/aspher1/robinhood-bot/tree/main/tests)

## Current boundary

Despite its repository name, **this engine cannot place real Robinhood orders**. Returns are simulated, fee and fill assumptions are models, and strategy performance is not proof of an investing edge. The engineering value is in reproducible comparison, state management, fail-closed handling and testability.

[All projects](../README.md)