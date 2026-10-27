# Frontend & Product Software Engineering

**Role focus:** Junior frontend engineer, frontend-focused software engineer, product engineer, UI engineer.

My public frontend work focuses on **browser integration and stateful React workflows**. Jarvis adapts a Chrome extension to different chart-page structures without blocking chart input; CreditAsk and PortalOps demonstrate user-facing workflows backed by explicit state and approval rules. Private Rainlight and Acre prototypes add screenshot-documented editor and real-estate interfaces. These are development projects, not released commercial products.

## Selected engineering evidence

| Project | What I can demonstrate | Inspect |
| --- | --- | --- |
| **[Jarvis Chart HUD](../projects/jarvis.md)** | Chrome Manifest V3 content scripts, multi-host adapters, Shadow DOM isolation, pointer-safe overlays, exact-price validation and an edge-dock fallback | [Host adapters](https://github.com/aspher1/Jarvis-main/blob/main/adapters.js) · [Overlay implementation](https://github.com/aspher1/Jarvis-main/blob/main/content.js) · [Data validation](https://github.com/aspher1/Jarvis-main/blob/main/desk-plan.js) · [Offline checks](https://github.com/aspher1/Jarvis-main/blob/main/tests/validate.mjs) |
| **[Anime Director Studio / Rainlight v8](../projects/anime-director-studio.md)** | React/TypeScript editor workflows, project panels, imported artwork, 24 fps drawing-exposure controls, timeline interaction, and browser QA | [Public case study and actual editor screenshot](../projects/anime-director-studio.md) · **Private source, review by arrangement** |
| **[Acre Real-Estate Intelligence](../projects/acre.md)** | Next.js buyer/agent workspace, four-home comparison, source/freshness disclosure, unknown cost-input handling and a built-in guided fallback | [Actual Acre UI captures](../projects/acre.md#application-preview) · **Private source, review by arrangement** |
| **[CreditAsk](../projects/creditask.md)** | Next.js intake UI, upload flow, review status and a printable output view gated by payment and approval state | [Intake UI](https://github.com/aspher1/Credit-task/blob/main/app/intake/page.tsx) · [Print gate](https://github.com/aspher1/Credit-task/blob/main/lib/pdf-gate.ts) · [Tests](https://github.com/aspher1/Credit-task/tree/main/tests) |
| **[PortalOps AI](../projects/portalops.md)** | Next.js/React operational screens, typed workflow states, explicit approval handling and synthetic-data interfaces | [State model](https://github.com/aspher1/Portalops/blob/main/lib/domain.ts) · [Executor](https://github.com/aspher1/Portalops/blob/main/lib/execution.ts) · [Executor tests](https://github.com/aspher1/Portalops/blob/main/tests/execution.test.ts) |

## Product decisions worth discussing

- **Visible state and fallback behavior.** Jarvis renders a safe universal dock when it cannot identify a supported chart and labels its data as mock rather than inventing levels. Its DOM overlay is designed not to intercept ordinary chart pointer interaction.
- **Frame-level interaction.** Rainlight's editor separates imported artwork from source-generated previews, assigns imported drawings to exposure holds, and allows frame-addressed playback/review. The 45-second, 20-cut study is a timing and candidate-artwork exercise, not evidence of finished fluid animation.
- **Human review in the UI.** CreditAsk and PortalOps expose explicit approval states instead of presenting generated drafts or proposed external actions as completed work.
- **Clear assumptions in decision tools.** Acre shows source/freshness metadata, leaves missing cost inputs unknown and labels its built-in Ask Acre guidance when AI is unavailable. The [local workspace screenshot](../projects/acre.md#application-preview) uses a sample buyer.

## Public reproduction paths

The [Jarvis extension README](https://github.com/aspher1/Jarvis-main#readme) documents how to load its source with Chrome's **Load unpacked** flow and run its offline checks:

```bash
node tests/validate.mjs
```

The [PortalOps README](https://github.com/aspher1/Portalops#readme) documents a Node.js 22+ setup and verification steps:

```bash
npm ci
npm test
npm run typecheck
npm run lint
npm run build
```

These commands are provided for reviewers to run; their presence does not imply that every browser host, external integration or deployment is validated. [GitHub Actions](https://github.com/aspher1/Portalops/actions/workflows/ci.yml) also runs the documented PortalOps checks.

## Scope and fit

**Relevant technologies:** React, TypeScript, JavaScript, Next.js, Vite, Electron (private prototype), Chrome Manifest V3, Shadow DOM, component-based UI, Playwright, Vitest, Node-based validation and GitHub Actions.

The public projects are prototypes. Jarvis uses mock financial data and broad host permissions that need review before store distribution. CreditAsk uses demo/test payments and local or ephemeral storage. PortalOps has synthetic data rather than real healthcare integrations. Rainlight and Acre are private development prototypes; their screenshots document local UI states, not deployed products or completed integrations. Rainlight's imported and AI-generated animation assets require further artistic evaluation.

[All projects](../README.md) · [Full-stack and automation evidence](full-stack-automation.md) · [Python and reliability evidence](python-reliability.md)
