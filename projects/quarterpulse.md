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

On October 27, 2026, a reviewed source snapshot and offline test/CI workflow were uploaded to the **private** repository `aspher1/quarterpulse-research-engine`. Private pull request **#1**, from `portfolio/source-audit-20261027` into `main`, remains open and unmerged. The snapshot includes Python application code, interface assets, tests and research documentation; its `PORTFOLIO_SOURCE_AUDIT.md` documents exclusions for secrets, runtime databases, local worktrees, logs and private captures. Access to the pull request and source requires repository authorization.

**Independently verified offline CI:** GitHub Actions workflow **Offline source verification** completed successfully on pull request #1 at commit `a20a108df020190a44f4edae0c911562fc61a141` (October 27, 2026; private run `40600086392`). This confirms that the configured GitHub-hosted offline checks passed on that revision. The earlier local test attempt had been blocked by filesystem timeouts; the GitHub Actions result supersedes that earlier test-status gap for the checks actually run.

**Validation boundary:** Passing offline CI does **not** establish real exchange integration behavior, authenticated order safety, live settlement or execution accuracy, profitability, full production readiness or the safety of every research branch. The original local checkout has additional worktrees and uncommitted changes requiring reconciliation. The displayed instrument examples and UI screenshots remain fictional/synthetic paper research evidence.

**Status:** Private research prototype with source uploaded and independently verified offline PR CI passing. Further security review and real-world integration validation are needed before any live-trading claim or release.

[Back to portfolio](../README.md)