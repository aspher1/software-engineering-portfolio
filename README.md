# Randy Kawka | Software Engineering Portfolio

[![Portfolio local Markdown link check](https://github.com/aspher1/software-engineering-portfolio/actions/workflows/portfolio-check.yml/badge.svg)](https://github.com/aspher1/software-engineering-portfolio/actions/workflows/portfolio-check.yml)


**Full-stack software · Python systems · Applied automation · Creative developer tools**

A curated set of software projects with source code, implementation notes, tests and clear boundaries between working prototypes and unshipped features. I am interested in **junior software engineering, full-stack, frontend and automation roles**.

[GitHub profile](https://github.com/aspher1) · **Role paths:** [Frontend](jobs/frontend-software-engineer.md) · [Full-stack](jobs/full-stack-automation.md) · [Python / reliability](jobs/python-reliability.md) · [All case studies](projects/)

## Where to start

For **web application engineering**, start with [PortalOps AI](projects/portalops.md). For **Python, state management and reliability**, review [rhbot](projects/rhbot.md). For **React/TypeScript, desktop workflows and UI engineering**, explore [Anime Director Studio / Rainlight v8](projects/anime-director-studio.md).

## Recruiter quick paths

Choose a track that matches the role. Each page links specific code, documented limitations, and source-backed résumé bullets instead of broad marketing claims.

- **[Frontend & Product Software Engineer](jobs/frontend-software-engineer.md):** React, Next.js, TypeScript, Electron, browser UI and responsive product workflows.
- **[Full-Stack & Automation Engineer](jobs/full-stack-automation.md):** API routes, Auth.js, validation, human approval, workflow state and SQL/RLS design.
- **[Python & Reliability Engineer](jobs/python-reliability.md):** Simulation, risk controls, data validation, SQLite, automated testing and reproducible research.

[Technical evidence matrix](projects/technical-evidence-matrix.md) · [Reusable, verified résumé statements](resume/engineering-project-bullets.md)

## Selected projects

These examples offer public source and test evidence, except Rainlight, whose source is private. Open each case study for implementation details and limitations.

### [PortalOps AI](projects/portalops.md)

**Next.js · TypeScript · Vitest · Synthetic-data MVP**

Human-supervised automation with typed workflow steps, bounded retries, explicit approval stops and tenant-aware PostgreSQL/RLS design. The payer portals and activity are synthetic.

**Inspect:** [Source](https://github.com/aspher1/Portalops) · [Architecture](https://github.com/aspher1/Portalops/blob/main/docs/ARCHITECTURE.md) · [CI](https://github.com/aspher1/Portalops/actions/workflows/ci.yml)

### [rhbot: Python research engine](projects/rhbot.md)

**Python · SQLite · pytest · Paper-only**

Simulates strategy accounts with modeled fees, stale-data safeguards, exposure limits, operator controls and auditable state. It cannot place live Robinhood orders.

**Inspect:** [Source](https://github.com/aspher1/robinhood-bot) · [Architecture](https://github.com/aspher1/robinhood-bot/blob/main/ARCHITECTURE.md) · [CI](https://github.com/aspher1/robinhood-bot/actions/workflows/ci.yml)

### [Anime Director Studio / Rainlight](projects/anime-director-studio.md)

**React · TypeScript · Electron · Private-source prototype**

Animation editor exploring frame-level playback, exposure timelines, drawing tools, desktop workflows and production review. The linked case study includes an actual local editor capture; imported artwork is identified as such.

**Inspect:** [Case study, screenshot and verification scope](projects/anime-director-studio.md). Source review can be arranged where authorized.

### [CreditAsk](projects/creditask.md)

**Next.js · Auth.js · Stripe test mode · Demo-only**

Guided home-inspection intake with file uploads, human-reviewed drafts, payment-state experiments and gated printable output. The current triage does not parse the uploaded PDF or photos into findings.

**Inspect:** [Source](https://github.com/aspher1/Credit-task) · [Tests](https://github.com/aspher1/Credit-task/tree/main/tests) · [CI](https://github.com/aspher1/Credit-task/actions/workflows/ci.yml)

### [Jarvis Chart HUD](projects/jarvis.md)

**JavaScript · Chrome Manifest V3 · Shadow DOM · Mock data**

Browser extension prototype with chart-host adapters, pointer-safe overlays, plan-value validation and a universal fallback. It has no live market feed or broker integration.

**Inspect:** [Source](https://github.com/aspher1/Jarvis-main) · [Offline validation](https://github.com/aspher1/Jarvis-main/tree/main/tests) · [CI](https://github.com/aspher1/Jarvis-main/actions/workflows/ci.yml)

## Real application previews

Actual local development captures, with their technical case studies one tap away. Screenshots document interface work, **not deployed products, verified integrations or customer use**.

### [Acre: buyer and agent workspace](projects/acre.md)

[![Acre local agent workspace with illustrative sample buyers](assets/previews/acre-workspace-preview.png)](projects/acre.md)

*Local UI with sample buyers. A real listing address and asking price were excluded from the capture. [Acre case study](projects/acre.md).*

### [Rainlight: anime production editor](projects/anime-director-studio.md)

[![Rainlight v8 local editor with project library, program monitor and frame-based timeline](assets/anime-director-studio-rainlight-v8.png)](projects/anime-director-studio.md)

*Local prototype showing imported illustrated scene artwork, editor panels and a timeline. The editor is not a released production package. [Rainlight case study](projects/anime-director-studio.md).*

### [QuarterPulse: synthetic market research workspace](projects/quarterpulse.md)

[![QuarterPulse local research dashboard labeled synthetic feed and paper simulation](assets/previews/quarter-synthetic-workspace.png)](projects/quarterpulse.md)

*October 2026 local research capture. [Mobile paper-positions preview](assets/previews/quarter-synthetic-paper-positions-mobile.png) shows fictional practice positions. No real trades, verified returns or production performance are implied. [QuarterPulse case study and source-review status](projects/quarterpulse.md).*

## Engineering approach

- **Type-safe product workflows:** Next.js and TypeScript applications, input validation, explicit workflow states and human review.
- **Reliability and testing:** Vitest, pytest, offline regression tests, GitHub Actions, error handling and approval boundaries.
- **Data and integrations:** SQLite audit ledgers, PostgreSQL schema/RLS design, public market-data inputs and API stubs.
- **UI and desktop development:** React, Electron, Chrome Manifest V3, adaptive panels, frame timing and browser interaction testing.

The strongest signal is not the number of prototypes. It is how they handle validation, failure, state transitions, human review and clearly documented limitations. Individual project pages link to code and tests so a reviewer can evaluate these decisions directly.

## Additional systems and architecture case studies

These projects have substantial application or research code. Verification varies by project; the case studies distinguish completed checks, checks blocked during the October 27, 2026 audit, and runtime behavior still awaiting review. Private source can be discussed in authorized walkthroughs.

### [Acre: Real-Estate Intelligence](projects/acre.md)

**Next.js · TypeScript · Clerk/Drizzle.** Typed property-listing adapters, comparison tools, deterministic monthly-cost modeling and source-aware data presentation. An offline product-contract check passed; a full build and test suite remain pending.

### [QuarterPulse Research Engine](projects/quarterpulse.md)

**Python · Simulation and replay.** Market discovery, simulated order matching, fee/risk constraints and audit evidence. Sanitized source, tests and CI were **merged into private `main`** through PR #1 (`31e825f`, October 27, 2026). **GitHub Actions offline checks passed on the private main merge commit.** They do **not** establish live-order safety, real execution, profitability or production readiness. [Public verification summary](projects/quarterpulse.md#verification-and-private-source-review).

### [LoopLab Video Studio](projects/looplab.md)

**Cloudflare D1/R2 · Typed media workflows.** Asynchronous provider orchestration, signed webhooks, job state and usage metering. Offline end-to-end and paid-provider verification remain pending.

### [Matchup Lab](projects/matchup-lab.md)

**Next.js · Supabase · Probability research.** Exact probability, seeded Monte Carlo, confidence intervals and deck validation. Tests are present, but the latest executions were blocked by filesystem issues.

### [RelayOps: Revenue Recovery OS](projects/relayops.md)

**Next.js · Supabase/PostgreSQL.** Tenant-aware schema/RLS design, consent and idempotency rules, and provider adapters. The private repository contains source; local and remote revisions still require reconciliation.

For high-tech job applications, the strongest **publicly inspectable, recently verified source** remains the featured repositories above. These additional case studies document research and architecture rather than completed code review, reproducible builds or production deployments.

## Scope, review and opportunities

Most repositories are **prototypes or research**, not deployed customer systems. Some work uses AI-assisted development tools. A green CI workflow supports the tested checks but does not establish production security, regulatory compliance, user adoption or commercial performance. Each case study distinguishes implementation from plans and known limitations.

I am exploring opportunities in **software engineering, product engineering, frontend development, workflow automation and applied AI tooling**. Public repositories and tests are available for review. For private projects, sanitized code walkthroughs or access to reviewed source branches can be arranged where authorized, using the contact details included with my job application.