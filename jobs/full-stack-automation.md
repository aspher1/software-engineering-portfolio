# Full-Stack & Automation Software Engineering

**Role focus:** Junior full-stack software engineer, workflow automation engineer, software engineer focused on API and application development.

I work on **typed application workflows**: validated inputs, explicit state transitions, human approvals, bounded side effects and testable failure paths. My public Next.js prototypes demonstrate these patterns using synthetic or demo data. A larger private operations project explores PostgreSQL row-level security and provider adapters, with its latest local and remote revisions still being reconciled.

## Selected engineering evidence

| Project | What the implementation demonstrates | Inspect |
| --- | --- | --- |
| **[PortalOps AI](../projects/portalops.md)** | Next.js/TypeScript workflow model; deterministic step executor with bounded retries, approval stops, typed evidence records, a domain allowlist validator and PostgreSQL tenant/RLS migration design | [Source](https://github.com/aspher1/Portalops) · [Architecture](https://github.com/aspher1/Portalops/blob/main/docs/ARCHITECTURE.md) · [Tests](https://github.com/aspher1/Portalops/tree/main/tests) |
| **[CreditAsk](../projects/creditask.md)** | Next.js App Router intake and API workflows, PDF/photo uploads, Auth.js administrative review, owner-scoped job access, Stripe test-mode checkout and fallback draft generation | [Source](https://github.com/aspher1/Credit-task) · [Tests](https://github.com/aspher1/Credit-task/tree/main/tests) · [Hardening commit](https://github.com/aspher1/Credit-task/commit/757d5e7) |
| **[RelayOps / Revenue Recovery OS](../projects/relayops.md)** | Private Next.js/Supabase architecture, authorization and RLS migrations, consent/idempotency rules, provider-neutral communications and background-job adapters, and integration/E2E test infrastructure | [Public technical case study](../projects/relayops.md) · **Private source, review by arrangement** |

## Technical decisions worth discussing

- **Permission before side effects.** PortalOps models proposed workflow steps and approval boundaries; its deterministic executor and tests make failure handling inspectable. The standalone navigation allowlist validator is **not yet wired into the executor**, and no live browser adapter is implemented.
- **Clear demo versus production state.** CreditAsk explicitly distinguishes stub payments from Stripe **test mode** and requires a human reviewer before printable output. A later access-control hardening pass reduced exposed demo paths without establishing production security.
- **Database boundaries.** PortalOps includes PostgreSQL schema and RLS policies as implemented migrations, but that is **not proof of a production database deployment**. RelayOps contains deeper private Supabase/RLS work; its latest changes have not been independently reverified end to end.
- **Replaceable integrations.** RelayOps defines communication and job-dispatch boundaries that can use deterministic development simulations. Real provider connections, consent policy and operational rollouts require separate verification.

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

**Relevant technologies:** Next.js App Router, React, TypeScript, Node.js, API route handlers, Zod validation, Auth.js, Stripe test-mode SDK, PostgreSQL migrations and row-level security design, Supabase (private project), Vitest, Playwright and GitHub Actions.

The public applications use simulated healthcare records, illustrative run activity and non-production data. CreditAsk does not reliably parse inspection PDFs into structured findings, and its local/ephemeral file storage is not a production solution. RelayOps has no verified customer adoption, recovered revenue, or active production integrations. Access to private source can be arranged through contact details included with a job application.

[All projects](../README.md) · [Frontend and product evidence](frontend-software-engineer.md) · [Python and reliability evidence](python-reliability.md)
