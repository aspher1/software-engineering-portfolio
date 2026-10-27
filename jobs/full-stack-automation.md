# Full-Stack & Automation Software Engineering

**Role focus:** Junior full-stack software engineer, workflow automation engineer, software engineer focused on API and application development.

I build **typed, reviewable application workflows** in Next.js and TypeScript, covering input validation, approval gates, access checks and bounded retries. PortalOps and CreditAsk offer publicly inspectable code and automated tests. Acre, RelayOps and LoopLab provide additional private architecture examples, with separate verification limits described in their case studies.

## Selected engineering evidence

| Project | What the implementation demonstrates | Inspect |
| --- | --- | --- |
| **[PortalOps AI](../projects/portalops.md)** | TypeScript workflow executor with bounded retries and approval stops; input policy checks; tenant-aware PostgreSQL/RLS migration design | [Executor](https://github.com/aspher1/Portalops/blob/main/lib/execution.ts) · [Policy](https://github.com/aspher1/Portalops/blob/main/lib/policy.ts) · [Schema migration](https://github.com/aspher1/Portalops/blob/main/db/migrations/0001_initial.sql) · [Tests](https://github.com/aspher1/Portalops/tree/main/tests) |
| **[CreditAsk](../projects/creditask.md)** | Next.js intake/API flow, per-job access tokens, Auth.js admin review, Stripe test-mode checkout and printable-output approval gate | [Intake API](https://github.com/aspher1/Credit-task/blob/main/app/api/intake/route.ts) · [Job access](https://github.com/aspher1/Credit-task/blob/main/lib/job-access.ts) · [Print gate](https://github.com/aspher1/Credit-task/blob/main/lib/pdf-gate.ts) · [Tests](https://github.com/aspher1/Credit-task/tree/main/tests) |
| **[RelayOps / Revenue Recovery OS](../projects/relayops.md)** | Private Next.js/Supabase architecture, authorization and RLS migrations, consent/idempotency rules, provider-neutral communications and background-job adapters, and integration/E2E test infrastructure | [Public technical case study](../projects/relayops.md) · **Private source, review by arrangement** |
| **[Acre Real-Estate Intelligence](../projects/acre.md)** | Private Next.js application with RentCast/FHFA source-aware adapters, deterministic property-cost models and Clerk/Drizzle data boundaries | [Architecture and actual UI previews](../projects/acre.md) · **Private source; full build/test verification pending** |
| **[LoopLab Video Studio](../projects/looplab.md)** | Private asynchronous media workflow architecture with Cloudflare D1/R2 state, operation leases, signed callbacks and idempotent usage accounting | [Architecture case study](../projects/looplab.md) · **Private source; paid-provider end-to-end checks unverified** |

## Technical decisions worth discussing

- **Permission before side effects.** PortalOps models proposed workflow steps and approval boundaries; its deterministic executor and tests make failure handling inspectable. The standalone navigation allowlist validator is **not yet wired into the executor**, and no live browser adapter is implemented.
- **Clear demo versus production state.** CreditAsk explicitly distinguishes stub payments from Stripe **test mode** and requires a human reviewer before printable output. A later access-control hardening pass reduced exposed demo paths without establishing production security.
- **Database boundaries.** PortalOps includes PostgreSQL schema and RLS policies as implemented migrations, but that is **not proof of a production database deployment**. RelayOps contains deeper private Supabase/RLS work; its latest changes have not been independently reverified end to end.
- **Replaceable integrations.** RelayOps defines communication and job-dispatch boundaries that can use deterministic development simulations. Real provider connections, consent policy and operational rollouts require separate verification.
- **Data freshness and asynchronous jobs.** Acre separates listing-source metadata and missing inputs from estimates; LoopLab models leased asynchronous tasks, callbacks and usage records. These are private implementation examples whose complete current integration tests remain pending.

## Public reproduction paths

PortalOps documents these commands for Node.js 22+:

```bash
npm ci
npm test
npm run typecheck
npm run lint
npm run build
```

CreditAsk documents a reproducible demo and tests; the application uses only test/stub payments:

```bash
npm ci
npm test
npm run lint
npm run build
```

Review the [PortalOps CI workflow](https://github.com/aspher1/Portalops/actions/workflows/ci.yml) and [CreditAsk CI workflow](https://github.com/aspher1/Credit-task/actions/workflows/ci.yml) for their actual configured environments. Reproducing a demo does not certify production authorization, storage, external service availability or compliance.

## Scope and fit

**Relevant technologies:** Next.js App Router, React, TypeScript, Node.js, API route handlers, Zod, Auth.js, Stripe test-mode SDK, PostgreSQL migrations and row-level security design, Supabase, Drizzle, Cloudflare D1/R2 (private prototypes), Vitest, Playwright and GitHub Actions.

The public applications use simulated healthcare records, illustrative run activity and non-production data. CreditAsk does not reliably parse inspection PDFs into structured findings, and its local/ephemeral file storage is not a production solution. PortalOps's demo request context relies on trusted headers, not verified production sessions. RelayOps has no verified customer adoption, recovered revenue or active production integrations. Acre and LoopLab need complete current build, test and integration checks. Private-source review can be arranged through contact details included with a job application.

[All projects](../README.md) · [Frontend and product evidence](frontend-software-engineer.md) · [Python and reliability evidence](python-reliability.md)
