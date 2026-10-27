# QuarterPulse: Prediction-Market Research and Paper-Execution Architecture

**Python research platform · order-book and fee simulation · risk controls · reproducible replay · local web dashboard**

QuarterPulse is an experimental system for discovering, evaluating and replaying short-duration prediction markets. Its engineering value is in separating market data, deterministic rules, execution constraints, audit evidence and researcher-facing interfaces.

[![Actual Quarter workspace in synthetic-feed and paper simulation mode, with source status, research briefing and paused engine](../assets/previews/quarter-synthetic-workspace.png)](../assets/previews/quarter-synthetic-workspace.png)

*Real October 2026 development capture. The interface explicitly says synthetic feed, simulation and paper mode. It does not depict live trades or validated returns.*

### Mobile paper-workspace evidence

[![QuarterPulse mobile positions screen showing illustrative synthetic-practice positions and paper-mode exit controls](../assets/previews/quarter-synthetic-paper-positions-mobile.png)](../assets/previews/quarter-synthetic-paper-positions-mobile.png)

*Real local mobile browser capture, October 16, 2026. The positions, example instrument names, prices and exit controls appear under the application's explicit **Synthetic feed**, **Paper** and **Simulation workspace** labels. These fictional practice examples are not live holdings, customer records or evidence of investment performance.*

## Implemented concepts in the code

- **Market discovery:** Kalshi catalog ingestion and selection of markets whose 15-minute cadence can be verified, with conservative restrictions for nonstandard settlement rules.
- **Simulation and replay:** Paper-order lifecycle, position/risk accounting, fills and fees, market-specific decision evidence and offline research modules.
- **Risk and integrity:** Limits, readiness checks, rejection reasons, source/reference provenance, loss investigation and replay-oriented regression tests.
- **Interface:** A web research/dashboard surface with market discovery and opportunity analysis, separate from order infrastructure.

The local application includes **19 illustrative fictional instruments** across crypto, metals, energy and FX for reproducible exploration. They are not proof of 19 live-tradable Kalshi markets.

## Critical trust boundary

Some research branches and local checkout files include authenticated live-order adapter code. It must **not** be assumed to be safe, enabled, audited or ready for real-money trading. Market discovery, replay and simulated profitability are distinct from verified real trading performance. No profitable trading edge or live execution readiness is claimed.

## Verification and private source review

On October 27, 2026, sanitized QuarterPulse research source was **merged into the private repository's `main` branch** through pull request #1, at merge commit `31e825f35406639a00e48ff8bf41c2c53ee9aa5f`. The merged import covers 81 source, documentation, test and workflow files. Its `PORTFOLIO_SOURCE_AUDIT.md` records exclusions for secrets, runtime databases, local research worktrees, logs and private captures. The code and review details remain accessible only to authorized private-repository reviewers.

**Verified offline CI:** GitHub Actions **Offline source verification** passed on the private PR head `2c48c93` in two runs, and the private `main` push workflow also **passed** on the exact merge commit `31e825f` (run `40600527807`, October 27, 2026). This provides GitHub-hosted confirmation that the configured offline checks passed on the merged source revision. Earlier local filesystem timeouts no longer describe the status of those checks.

**Validation boundary:** Offline CI does **not** establish safe live orders, authenticated exchange behavior, real fills, settlement accuracy, profitable returns, or production readiness. The source may contain live-order infrastructure requiring separate security and integration review. The displayed instruments and screenshots remain fictional/synthetic paper-research evidence, not live positions or verified performance.

**Status:** Sanitized source, documentation, tests and offline CI are preserved on **private `main`**, with its merge-commit offline checks passing. QuarterPulse remains a research prototype without verified live-trading readiness or profitability.

[Back to portfolio](../README.md)