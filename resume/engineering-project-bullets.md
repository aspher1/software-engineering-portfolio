# Resume-ready engineering project statements

This is a **source-backed wording library** for tailoring junior software engineering applications. Select statements relevant to the role and adapt them to work you can personally explain in an interview. They describe **implemented projects or verified development work**, not professional employment, paying customers, or production impact.

## Junior frontend / product software engineer

- **Anime Director Studio / Rainlight:** Developed and iterated on a React/TypeScript/Electron animation editor with scene and cut navigation, imported artwork, frame-addressed exposure timing at 24 fps, and production review controls. The local application audit reported 365 passing checks, 0 failures and 11 skips; generated in-betweens still require artist review. [Case study](../projects/anime-director-studio.md)
- **Jarvis Chart HUD:** Built a Chrome Manifest V3 interface prototype using JavaScript content scripts, Shadow DOM, host-specific adapters and fallback layouts to preserve chart interactions while validating plan-data freshness. [Source](https://github.com/aspher1/Jarvis-main) · [Validation](https://github.com/aspher1/Jarvis-main/tree/main/tests)
- **PortalOps AI:** Implemented a responsive Next.js/TypeScript workflow prototype with synthetic operating dashboards, human review surfaces and typed approval and execution states. Real portal automation and persisted approvals are not implemented. [Source](https://github.com/aspher1/Portalops)

## Junior full-stack / automation engineer

- **PortalOps AI:** Designed and implemented supervised workflow policies in Next.js and TypeScript, including approval checkpoints, bounded retries, evidence records, tenant-aware API boundaries and a PostgreSQL/RLS migration design, with automated Vitest checks. Synthetic-only; not a production healthcare system. [Architecture](https://github.com/aspher1/Portalops/blob/main/docs/ARCHITECTURE.md) · [Tests](https://github.com/aspher1/Portalops/tree/main/tests)
- **CreditAsk:** Built a Next.js inspection intake and human-reviewed drafting workflow with local file uploads, Auth.js administrative screens and Stripe test-mode Checkout. Added owner-cookie access checks, disabled remote demo shortcuts by default and tightened payment verification; the hardening commit passed tests, lint, TypeScript and a production build. [Source](https://github.com/aspher1/Credit-task) · [Security hardening](https://github.com/aspher1/Credit-task/commit/757d5e7)
- **RelayOps:** Prototyped a Next.js/Supabase revenue-workflow architecture with domain/application/provider separation, PostgreSQL/RLS schemas and idempotent messaging concepts. Provider activation and commercial outcomes are not independently verified. [Case study](../projects/relayops.md)

## Junior Python / reliability / data engineer

- **rhbot:** Developed a Python/SQLite paper-trading research engine comparing independent simulated BTC/ETH strategies with modeled fees, data freshness checks, drawdown controls, operator kill switches and an audit trail. Risk and safety tests run in CI; real brokerage orders are disabled. [Source](https://github.com/aspher1/robinhood-bot) · [CI](https://github.com/aspher1/robinhood-bot/actions/workflows/ci.yml)
- **QuarterPulse:** Designed a Python market-research and replay prototype with fee-aware simulated fills, order-book data modeling, risk limits and evidence capture. The 19 test instruments are synthetic and latest test runs were blocked by local filesystem issues; **do not describe this as a profitable or trading-ready bot**. [Case study](../projects/quarterpulse.md)
- **Matchup Lab:** Implemented exact probability and seeded Monte Carlo components for card-game deck analysis, including multivariate draw models and Wilson confidence intervals within a Next.js/TypeScript application. Auth/database and tests are present but fresh verification remains pending. [Case study](../projects/matchup-lab.md)

## How to use these responsibly

A technical résumé works best when each project statement is tied to source code, a design tradeoff and a verification result. Link to the actual public code for the first group; use the public case-study link for private projects. Avoid unsupported metrics such as users, revenue, returns, time savings, accuracy or model-training results. If you used AI-assisted development, describe your specific product decisions, implementation review, debugging, testing, and integration work accurately.

[Return to portfolio](../README.md)
