# Acre: Real-Estate Decision Intelligence

## Application preview

![Acre agent workspace showing its ranked buyer-fit view, synthetic preview buyer, source label and summary metrics](../assets/previews/acre-workspace-preview.png)

*Actual local Acre UI screenshot, cropped to exclude a real listing address and asking price. The banner identifies **workspace preview / sample buyers**; “Maya Chen” is illustrative, not a customer record. Source labels and fit figures are a captured development snapshot, not a promise of current listings or a deployed service.*

**Next.js application prototype · TypeScript · React · Clerk · Drizzle/PostgreSQL · RentCast · FHFA**

Acre explores a buyer, seller and agent workspace that connects property discovery with evidence-aware decision support rather than presenting a property search page alone.

## What the code implements

- **Discovery and property detail:** A source-aware RentCast listing adapter, property pages, listing freshness/source metadata, photo fallback handling, and location-aware search.
- **Market context:** A distinct FHFA house-price-index provider that falls back to state-level information when precise locality is unavailable. It is *not* a home appraisal, local median price, or valuation model.
- **Decision tools:** Cost Intelligence with deterministic monthly expense assumptions, local-device Save/Compare (up to four homes), saved searches, Decision Memory and an Ask Acre interface with an explicitly labeled non-LLM fallback.
- **Agent and pipeline workflows:** Buyer/seller intake and stages, follow-up drafting, workspace components, and a Clerk-authenticated agent boundary with PostgreSQL/Drizzle schema and API routes.
- **Trust boundaries:** Missing/unknown values are kept distinct from zeroes; fair-housing steering controls and source/freshness disclosure inform output.

## Engineering map

The repository is organized into Next.js `app/` routes, React `components/`, service/business logic under `lib/`, schema in `db/` and `drizzle/`, and contract/deployment verification scripts under `scripts/`.

**Code review focus:** provider-adapter isolation, fallback states, typed cost/fit assumptions, transparent evidence metadata, authentication boundaries and product state transitions.

## Verification and limits

An offline product contract check passed during the October 27, 2026 review. A fresh production build, TypeScript check and lint run **could not be completed on the audit machine** because of filesystem/dependency read timeouts. The application does not currently have a conventional automated unit-test suite.

Local configuration includes a server-side RentCast credential, which is **not published**. Live integration behavior requires authorized credentials. The current local environment did not prove configured Clerk, Neon/Postgres, MLS coverage, production billing, customer adoption, or scheduled alert delivery. Saved/compare state is browser-local in several flows.

**Status:** Substantial prototype, not a launched real-estate brokerage product or verified live service. Source is private and under security/verification review; a code walkthrough can be arranged through application contact information.

[Back to portfolio](../README.md)