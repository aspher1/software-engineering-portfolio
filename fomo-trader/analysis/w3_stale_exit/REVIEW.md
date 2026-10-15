# W3 staff review

Built an offline timeout sweep for 3, 5, 10, 15, 30, 60, 120 minutes, 4 hours, and 8 hours. It uses `replay.net_pnl`, `replay.estimated_cost`, and `replay.metrics` with the required `min(0, actual net P&L)` convention, a nominal entry-time two-thirds/one-third split, chain checks, 3× estimated-cost stress, and an adjacent-timeout plateau gate. The JSON verdict is disabled. No live bot file or run artifact was changed.

## Three strongest objections

1. **There is no intratrade price path, so the timeout fill is unknowable.** Answer: the scoring rule is an explicit imposed bound, not an executable fill estimate. It sets winners to breakeven and leaves losers unchanged. Algebraically, it cannot produce positive edge over the baseline, so the gates cannot pass under this convention. Shipping is rejected without claiming that a real timeout would fail. A timestamped quote path would be needed for a genuine fill study.
2. **EDT/UTC clock switching can turn minutes into apparent hours and corrupt the split.** Answer: negative duration and same-day 2–5 hour pairs are censored from timeout treatment, with counted warnings. All timestamps are naive, so some same-day entry ordering remains uncertain; this is a reason to reject, not to infer a winner. The report identifies the affected records and does not claim exact duration correction.
3. **Only a small, changing journal supports IS/OOS, and chain coverage is uneven.** Answer: the report fixes one read and shows counts, per-chain OOS edges, stress metrics, and every gate. No timeout has positive IS edge or a broad passing plateau. New closes would require a new fixed snapshot and independent validation; the current sample cannot justify a rule.

**Concurrency:** analysis is a pure function of the caller's input rows after the journal is read; no shared mutable decision state, locks, parameter mutation, or hot-path I/O exists. Each sweep owns its local counters and copies any adjusted trade.

**Latency:** analysis only. Any later hot-path implementation must prove p50/p99 below 50 ms across at least 10,000 realistic iterations on both chains, including worst-case shapes and bounded memory.
