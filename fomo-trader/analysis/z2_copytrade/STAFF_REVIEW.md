# Staff review — 2026-09-27

Built an offline, source-injected paper validation harness: conservative wallet screen, best-effort no-key Solscan adapter, hourly JSONL tape adapter, disk cache, GeckoTerminal minute OHLCV price path, chronological pairing and delay replay, fully costed USD P&L, E1 split and kill rules. It never imports or modifies the live bot. See `E1_PREREGISTRATION.md` for the frozen specification.

## Three strongest objections

1. **Wallet identities or histories may be unavailable.** The research notes provide only prefixes, and the legacy Solscan public endpoint may reject requests or omit swap economics. Answer: candidates remain unresolved; the runner requires complete addresses, explicit normalized trade fields and vetting data. This objection remains unanswered until a public source or tape supplies them.
2. **The edge may disappear before 30 seconds.** The target and leader appear to need create-block or shred-stream speed. Answer: the 30-second OOS anchor and 5–120 second decay regression test this directly, with $7 sizing and both-leg costs. Minute closes are a coarse proxy and cannot establish live fillability. This objection remains unanswered until real timestamped data are replayed.
3. **Wallet selection can leak future winners.** Published profitable wallets were characterized after their winning windows. Answer: the run log records provenance, and an independently dated pre-IS screening window is required for a defensible OOS result. Using the same history to screen and score would be exploratory only. This objection remains unanswered for the listed leads.

Additional limitation: a constant SOL/USD conversion and preselected pool map require provenance and timestamp checks. No profitability or live-readiness claim follows from this harness. A live system would need to prove a <50 ms decision budget and executable quotes; this offline benchmark only measures local generation cost.
