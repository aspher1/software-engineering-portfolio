# PortalOps AI

**Full-stack workflow automation prototype**  
**Stack:** Next.js, React, TypeScript, Zod, Vitest; PostgreSQL schema and row-level security design  
**Source:** [aspher1/Portalops](https://github.com/aspher1/Portalops)

## Problem and approach

Administrative operations often involve multi-step tasks where external sites are unreliable and mistakes have consequences. PortalOps explores a supervised workflow design: represent actions as typed steps, validate their scope, require explicit approval for side effects, and preserve evidence for review.

## Technical evidence

- **Workflow execution prototype:** A typed `BrowserAdapter` interface and deterministic `executeStep()` function with a bounded retry budget, an approval stop before side effects, and a recovery re-read hook. The repository does not yet implement a live browser adapter.
- **Navigation policy module:** A separately tested HTTPS/domain allowlist validator. It is not yet wired into `executeStep()`, so it must not be described as enforced browser-navigation control.
- **Review controls:** Approval states before proposed submissions or updates, plus typed run and evidence records.
- **Data modeling:** PostgreSQL migration and tenant-aware schema with row-level security policies. This is a design and migration, **not evidence of a production database deployment**.
- **Verification:** Tests covering workflow navigation, untrusted-content inspection, approval logic and retries. A [GitHub Actions workflow](https://github.com/aspher1/Portalops/actions/workflows/ci.yml) runs automated checks.

## Architecture and code

[Architecture](https://github.com/aspher1/Portalops/blob/main/docs/ARCHITECTURE.md) · [Security notes](https://github.com/aspher1/Portalops/blob/main/docs/SECURITY.md) · [Test suite](https://github.com/aspher1/Portalops/tree/main/tests) · [README and setup](https://github.com/aspher1/Portalops#readme)

## Current boundary

This is a **synthetic-data MVP**. The included payer portals are simulated, dashboard run statistics are illustrative, the demo's approval API validates requests but does not persist decisions, and the current identity handling is not production authentication. It has no live payer connectivity or healthcare compliance certification and must not be used with real patient records.

[All projects](../README.md)