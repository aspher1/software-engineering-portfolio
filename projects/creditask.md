# CreditAsk

**Inspection-intake and review workflow prototype**  
**Stack:** Next.js App Router, React, TypeScript, Auth.js, Stripe test mode, optional LLM calls  
**Source:** [aspher1/Credit-task](https://github.com/aspher1/Credit-task)

## Problem and approach

The prototype organizes a home-inspection request into an intake, issue triage, a repair or credit letter draft, and a human approval stage. The focus is on clearly separated workflow states and testable delivery gates rather than an autonomous negotiation service.

## Technical evidence

- **Application flow:** Form intake and local PDF/photo uploads, job statuses, draft review and a printable letter page.
- **Access and payment experiments:** Auth.js administrative review and Stripe test Checkout or labeled stub-payment paths. No live charges are supported.
- **Human-in-the-loop design:** Draft output remains subject to approval, revisions or rejection.
- **Resilience:** Conservative offline fallback when an optional LLM cannot be called, plus scripts and tests for the demo workflow.

[Source README](https://github.com/aspher1/Credit-task#readme) · [Access-control hardening](https://github.com/aspher1/Credit-task/commit/757d5e7) · [Tests](https://github.com/aspher1/Credit-task/tree/main/tests) · [CI](https://github.com/aspher1/Credit-task/actions/workflows/ci.yml)

## Current boundary

The optional model triage currently works from submitted fields and attachment filenames; it does **not** establish reliable extraction of actual PDF pages or image contents. A GitHub-reviewed October 27 hardening pass added owner-cookie checks to job API, job pages and printable output; removed remote demo shortcuts by default; and tightened demo credentials and Stripe paid-state checks. This is limited risk reduction, **not a production security certification**. Files and job data still use local or ephemeral storage, so real-user deployment requires proper persistence, identity and comprehensive security review. The example price and turnaround are product concepts, not evidence of customer orders.

[All projects](../README.md)