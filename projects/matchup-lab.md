# Matchup Lab: Probabilistic Card-Game Research Application

**Next.js · TypeScript · Supabase/PostgreSQL · Vitest · Playwright · probability and simulation**

Matchup Lab explores a quantitative deck-analysis workflow for a collectible-card-game community. The research and application code are original work in the reviewed workspace; this is an **unofficial fan-oriented tool** and is not affiliated with or endorsed by any game publisher.

## Engineering highlights

- **Exact models:** Hypergeometric and multivariate probability calculations for drawing events.
- **Simulation:** Exact multiset redraw logic and seeded Monte Carlo estimates with Wilson confidence intervals.
- **Validation:** Deck-format checks including a 50+1 deck structure, typed inputs, score/weight calculations and validation error states.
- **Application:** Next.js and React PWA interface, browser-local project storage, optional Supabase-authenticated snapshot sync and an opt-in image-to-metadata vision pathway.
- **Testing architecture:** Seven authored unit suites, a browser journey suite, SQL checks and GitHub Actions configuration exist in the local source.

## Scope and technical debt

A normalized PostgreSQL schema and RLS rules are present, but the web client presently synchronizes a single owner-scoped JSON snapshot rather than using all normalized domain tables. Snapshot overwrites use simple last-write-wins behavior. Two-user database authorization, browser journeys and deployment must be independently tested.

Fresh lint, TypeScript, unit and build checks were **blocked by filesystem read timeouts**, so automated coverage cannot yet be reported as passing. Photos or card text imported by a user may have separate rights and privacy obligations. The public case study intentionally contains no official card art, licensed dataset, customer exports or proprietary game marks.

**Status:** Substantial local prototype with promising mathematical engineering depth; private code review and end-to-end validation pending.

[Back to portfolio](../README.md)