# RelayOps: Revenue Recovery OS

**Private-source full-stack product and workflow prototype**  
**Stack:** Next.js App Router, React, strict TypeScript, Supabase Auth/PostgreSQL/RLS, Zod, pnpm, Vitest and Playwright

## Problem

Missed calls and forgotten follow-ups create operational gaps for home-service teams. RelayOps explores a single workspace for contact intake, follow-ups, appointments, status tracking and auditable estimates of recovered revenue, beginning with fictional plumbing and HVAC scenarios.

## Technical design

- **Modular monolith:** Separate presentation, use-case, domain and infrastructure layers for clearer code ownership.
- **Tenant-aware data model:** Supabase/PostgreSQL schema migrations and row-level security policy tests to define access boundaries.
- **Provider adapters:** Interfaces for message delivery and background work, with development simulators separated from production-provider configuration.
- **Workflow correctness:** Validation, idempotency keys, consent/suppression rules, audit events and explicit handling of ambiguous delivery outcomes.
- **Verification tooling:** Vitest, React Testing Library, Playwright, TypeScript checks and a local verification command are present in the private repository.

## Current status and review

This is a **private development prototype**, not a claim of verified customer acquisition, live recovered revenue, production-grade compliance or active third-party integrations. Demonstration data is fictional, and the selected architecture describes design intent as well as implemented components. Independent production security and deployment claims are not made.

The project source is private under the repository name `aspher1/revenue-reco`. I can arrange authorized access or an architecture walkthrough for interview reviewers using the contact details provided with my application.

[Back to portfolio](../README.md)