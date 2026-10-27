# QuarterPulse: Prediction-Market Research and Paper-Execution Architecture

**Python research platform · order-book and fee simulation · risk controls · reproducible replay · local web dashboard**

QuarterPulse is an experimental system for discovering, evaluating and replaying short-duration prediction markets. Its engineering value is in separating market data, deterministic rules, execution constraints, audit evidence and researcher-facing interfaces.

## Implemented concepts in the code

- **Market discovery:** Kalshi catalog ingestion and selection of markets whose 15-minute cadence can be verified, with conservative restrictions for nonstandard settlement rules.
- **Simulation and replay:** Paper-order lifecycle, position/risk accounting, fills and fees, market-specific decision evidence and offline research modules.
- **Risk and integrity:** Limits, readiness checks, rejection reasons, source/reference provenance, loss investigation and replay-oriented regression tests.
- **Interface:** A web research/dashboard surface with market discovery and opportunity analysis, separate from order infrastructure.

The local application includes **19 illustrative fictional instruments** across crypto, metals, energy and FX for reproducible exploration. They are not proof of 19 live-tradable Kalshi markets.

## Critical trust boundary

Some research branches and local checkout files include authenticated live-order adapter code. It must **not** be assumed to be safe, enabled, audited or ready for real-money trading. Market discovery, replay and simulated profitability are distinct from verified real trading performance. No profitable trading edge or live execution readiness is claimed.

## Verification status

Earlier development work documented extensive offline regression tests and isolated development branches. For this October 27 review, a fresh test run was **blocked by filesystem timeouts**, so those historical counts are not treated as a current green build. The canonical main checkout also contains uncommitted changes, multiple isolated worktrees and local paper databases. Those worktrees and databases must be excluded from any private source backup until reconciled.

**Status:** Substantial research prototype. Source kept private during security and branch-history review. The case study demonstrates data provenance, risk-modeling, replay and conservative operational design, not trading returns.

[Back to portfolio](../README.md)