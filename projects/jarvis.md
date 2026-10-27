# Jarvis Multi-Host Chart HUD

**Chrome extension interface and data-validation prototype**  
**Stack:** JavaScript, Chrome Manifest V3, content scripts, Shadow DOM, Node-based checks  
**Source:** [aspher1/Jarvis-main](https://github.com/aspher1/Jarvis-main)

## Problem and approach

Third-party financial chart pages use different DOM structures, so overlays should adapt or fail gracefully without blocking their core interactions. Jarvis explores one chart HUD across several hosts with a universal fallback when the expected chart is unavailable.

## Technical evidence

- Host adapters for TradingView, Webull and Yahoo Finance, plus a universal edge-dock fallback.
- Normalized plan values and freshness/quality validation to avoid fabricated or silently rounded levels.
- Shadow DOM isolation, collapsible controls and pointer-safe graphics.
- [Node validation tests](https://github.com/aspher1/Jarvis-main/tree/main/tests), [design notes](https://github.com/aspher1/Jarvis-main/blob/main/docs/DESIGN.md) and [CI](https://github.com/aspher1/Jarvis-main/actions/workflows/ci.yml).

## Current boundary

The HUD is a **mock/paper-data prototype**, not a live market feed, recommendation engine or broker integration. Its broad `<all_urls>` match pattern and third-party page interaction would need permission reduction and further security review before store distribution.

[All projects](../README.md)