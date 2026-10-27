# Frontend & Product Software Engineering

**Role focus:** Junior frontend engineer, frontend-focused software engineer, product engineer, UI engineer.

I build interactive software prototypes in **React, TypeScript and JavaScript**, with attention to state transitions, user-facing error states, source integrity and testable behavior. My strongest examples are a browser extension with a cross-site overlay and an animation editor with frame-addressed controls. The editor source is private; the extension and web-application source can be reviewed publicly.

## Selected engineering evidence

| Project | What I can demonstrate | Inspect |
| --- | --- | --- |
| **[Jarvis Chart HUD](../projects/jarvis.md)** | Chrome Manifest V3 content scripts, host-specific adapters, Shadow DOM isolation, pointer-safe chart overlays and a fallback when a chart mount is unavailable | [Extension source](https://github.com/aspher1/Jarvis-main) · [Adapters](https://github.com/aspher1/Jarvis-main/blob/main/adapters.js) · [Plan validation](https://github.com/aspher1/Jarvis-main/blob/main/desk-plan.js) · [Tests](https://github.com/aspher1/Jarvis-main/tree/main/tests) |
| **[Anime Director Studio / Rainlight v8](../projects/anime-director-studio.md)** | React/TypeScript editor workflows, project panels, imported artwork, 24 fps drawing-exposure controls, timeline interaction, and browser QA | [Public case study and actual editor screenshot](../projects/anime-director-studio.md) · **Private source, review by arrangement** |
| **[CreditAsk](../projects/creditask.md)** | Next.js intake forms, PDF/photo upload workflows, administrative review states and a printable output page | [Public source](https://github.com/aspher1/Credit-task) · [Tests](https://github.com/aspher1/Credit-task/tree/main/tests) |
| **[PortalOps AI](../projects/portalops.md)** | Next.js/React workflow screens, typed run/approval states, guarded proposed actions and synthetic-data interfaces | [Public source](https://github.com/aspher1/Portalops) · [Architecture](https://github.com/aspher1/Portalops/blob/main/docs/ARCHITECTURE.md) |

## Product decisions worth discussing

- **Visible state and fallback behavior.** Jarvis renders a safe universal dock when it cannot identify a supported chart and labels its data as mock rather than inventing levels. Its DOM overlay is designed not to intercept ordinary chart pointer interaction.
- **Frame-level interaction.** Rainlight's editor separates imported artwork from source-generated previews, assigns imported drawings to exposure holds, and allows frame-addressed playback/review. The 45-second, 20-cut study is a timing and candidate-artwork exercise, not evidence of finished fluid animation.
- **Human review in the UI.** CreditAsk and PortalOps expose explicit approval states instead of presenting generated drafts or proposed external actions as completed work.

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

The public projects are prototypes. Jarvis has mock financial data and broad host permissions that need review before store distribution. CreditAsk uses demo/test payments and local or ephemeral storage. PortalOps has synthetic data rather than real healthcare integrations. Rainlight source access is restricted and its imported/AI-generated animation assets require further artistic evaluation.

[All projects](../README.md) · [Full-stack and automation evidence](full-stack-automation.md) · [Python and reliability evidence](python-reliability.md)
