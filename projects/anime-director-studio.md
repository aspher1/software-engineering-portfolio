# Anime Director Studio / Rainlight v8

**Private-source anime production workbench** | React, TypeScript, Vite, Electron, automated Node and Playwright tests

![Rainlight v8 editor showing project library, selected anime background, program monitor, inspector and 24 fps timeline](../assets/anime-director-studio-rainlight-v8.png)

*Actual local development-editor capture, October 2026. The scene artwork was imported; the screenshot does not claim the editor generated it. Work in progress, not a released commercial product.*

## The project

An animation editing and production workspace that brings a director's view of shots, source art, exposure sheets, timing and review into one interface. It explores how AI-assisted shot creation can coexist with explicit artist review, frame-level editing and deterministic timing. The editor and marketing site are separate codepaths within the same private development project.

## Implementation highlights

- **Timeline and exposure tooling:** Cut selection, 24 fps frame seeking, storyboard and animation views, drawing import, on-ones/on-twos/on-threes exposure workflows, and render/export controls.
- **Artist-facing tools:** Drawing Desk, camera-keyframe controls, acting/dialogue timing, source-art reference/evidence, canon consistency, retake handoffs and preflight tooling.
- **Desktop and engineering:** React/TypeScript frontend, Vite builds, Electron host, local project-oriented workflows, automated tests and browser QA.
- **Production experiment:** A 45-second, 20-cut, 1,080-frame Rainlight pilot with source art, candidate in-between artwork, manifests and assembly tests.

## Honest scope and verification

The editor is an **active prototype**, not a production-certified animation package. The 45-second episode timing and nonempty playback ledger are distinct from proof of fluid, broadcast-ready animation: AI-generated candidate frames still require artist evaluation; not every cut has sufficient distinct drawings, and some browser-dependent QA is opt-in. External AI providers may require separate credentials, which are not part of this public portfolio. No claims of 20 finished episodes, paid customers, or commercial studio adoption are made.

**Verification snapshot, October 27, 2026:** The local application and marketing builds completed. The app's main test command reported **365 passing, 0 failing and 11 skipped** checks; browser-only animation review remained separate. This is a recorded development result, not independent proof of production readiness.\n\n**Provenance:** The Rainlight visual direction originated as a design study brought into the larger application; scene art and candidate in-between artwork were imported or generated separately and remain subject to review. The marketing site uses third-party Sillage template material under its included MIT notices. This work should be evaluated for its product integration, editor/production functionality, testing and iteration, not claimed as entirely hand-drawn or authored from scratch.\n\nThe source is maintained in a **private GitHub repository**. Authorized access or an interview code walkthrough can be arranged through the contact details supplied with my application.

[Back to the portfolio](../README.md)
