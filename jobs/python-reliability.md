# Python, Data & Reliability Software Engineering

**Role focus:** Junior Python software engineer, backend/reliability-focused engineer, data tooling engineer, QA and test automation engineer.

My strongest public Python example is **rhbot**, a deliberately paper-only strategy research system. It is useful engineering evidence because it makes input freshness, failure behavior, risk limits, state persistence and audit history explicit. I also explore larger private prediction-market research software, with clear limits on what has been freshly verified.

## Selected engineering evidence

| Project | Engineering substance | Inspect |
| --- | --- | --- |
| **[rhbot: Python paper-trading research](../projects/rhbot.md)** | Python 3.12 engine with independent simulated strategy books, Coinbase public bid/ask ingestion, SQLite state, modeled costs, operator CLI, safety checks and an audit record | [Source](https://github.com/aspher1/robinhood-bot) · [Architecture](https://github.com/aspher1/robinhood-bot/blob/main/ARCHITECTURE.md) · [Tests](https://github.com/aspher1/robinhood-bot/tree/main/tests) · [CI](https://github.com/aspher1/robinhood-bot/actions/workflows/ci.yml) |
| **[QuarterPulse research engine](../projects/quarterpulse.md)** | Private Python modules for short-duration prediction-market discovery, paper execution/replay, fee modeling, risk rules and review interfaces | [Public architecture case study](../projects/quarterpulse.md) · **Private source; current end-to-end checks not confirmed** |
| **[PortalOps AI](../projects/portalops.md)** | A second, TypeScript-based example of bounded retries, audit/evidence models, approval checks and automated validation | [Public source](https://github.com/aspher1/Portalops) · [Test suite](https://github.com/aspher1/Portalops/tree/main/tests) |

## Failure modes I design around

- **Missing or stale market input.** rhbot rejects new simulated fills if required Coinbase quotes are missing, malformed or too old. Its documented quote threshold is 30 seconds, and the code does not silently substitute an unrelated provider's price.
- **Deterministic order checks.** A risk module checks symbol allowlists, duplicate order IDs, exposure caps, kill state, drawdown and other constraints before recording a paper fill. Configuration can tighten its code-level hard caps.
- **Auditable state.** SQLite stores separate simulated strategy accounts, fills and decision history; a heartbeat records cycle health. The architecture also documents a hash-chained decision log.
- **Same rules in research and operation.** The strategy code supports offline replay and comparison against a buy-and-hold benchmark, with conservative modeled fees. This is a reproducible experiment design, not an assertion of a profitable signal.
- **Private research with unresolved verification.** QuarterPulse has paper simulation and loss-analysis tooling, but its recent local test run was interrupted by filesystem problems. Its private branches can include authenticated execution adapters and must not be described as safe for live trading.

## Reproduce the public Python checks

The [rhbot README](https://github.com/aspher1/robinhood-bot#readme) documents Python 3.12, local development installation, a safe offline self-test and the pytest suite:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
rhbot selftest
python -m pytest
```

Its [GitHub Actions test workflow](https://github.com/aspher1/robinhood-bot/actions/workflows/ci.yml) installs the project and runs pytest. The self-test and unit tests are evidence for their specific checked paths, not proof of profitable strategies or production operations.

## Scope and fit

**Relevant technologies:** Python 3.12, SQLite, typed configuration/data validation, CLI tools, public HTTP market-data APIs, simulation, state machines, error handling, safety invariants, offline replay, pytest and GitHub Actions. Related application work provides TypeScript, Node.js and SQL exposure.

rhbot has **no real Robinhood order execution**, even though its repository name refers to Robinhood. Modeled fills and transaction costs are assumptions rather than observed trading performance. QuarterPulse is a separate private experiment whose source, runtime behavior and branch integrity require further review; its documented fictional instruments are not verified live exchange listings.

[All projects](../README.md) · [Frontend and product evidence](frontend-software-engineer.md) · [Full-stack and automation evidence](full-stack-automation.md)
