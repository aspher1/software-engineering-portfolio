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

[Source README](https://github.com/aspher1/Credit-task#readme) · [Tests](https://github.com/aspher1/Credit-task/tree/main/tests) · [CI](https://github.com/aspher1/Credit-task/actions/workflows/ci.yml)

## Current boundary

The optional model triage currently works from submitted fields and attachment filenames; it does **not** establish reliable extraction of actual PDF pages or image contents. Files and job data use local or ephemeral storage, and demo credentials, session/payment paths and access controls need further hardening before any public deployment. The example price and turnaround are product concepts, not evidence of customer orders.

[All projects](../README.md)