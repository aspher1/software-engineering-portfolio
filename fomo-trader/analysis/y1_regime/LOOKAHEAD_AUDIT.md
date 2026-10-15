# Y1 look-ahead audit

Claim: every gate feature is computable from data available at the entry decision
point. Verified by construction and by test.

## Construction
- `build_features`: for close *i* with matched entry *e*, the prior set is
  `{j != i : parse_ts(closes[j].ts) < parse_ts(e.ts)}` — strict inequality on
  parsed timestamps. The close being gated is explicitly excluded (`j != i`).
- Trailing windows (12h/24h) and `gap_hours`/`cum_usd_before` derive only from
  the prior set. `entry_half`/`entry_dow` come from the entry's own ts, known at
  decision time.
- P&L inputs use `replay.net_pnl` on *prior closes only*. The gated close's own
  `realized_*` is never an input to its own gate.
- Orphan closes (no entry with same mint and `ts <= close ts`) fail open.

## Edge cases checked
- Cross-day position (entered Sep 24 22:08, closed Sep 27 14:01): gated on
  Sep-24 trailing features — correct, the decision it models was made Sep 24.
  Its Sep-27 close is legitimately in the trailing windows of later Sep-27
  entries (the close event genuinely preceded them).
- Duplicate mints: `match_entry` takes the latest entry with `ts <= close ts`;
  a later entry cannot leak into an earlier close's features.
- Naive timestamps: journal ts has no zone; all comparisons are consistent
  naive-vs-naive, so ordering is preserved regardless of the true zone.
  AM/PM halves assume ET (user timezone); a zone error would only relabel R5,
  which was rejected anyway.

## Tests
`tests/test_y1_regime.py::test_no_lookahead_features_use_only_prior_closes`
constructs a synthetic journal where a huge win occurs *after* an entry and
asserts the gate's features for that entry exclude it. A second test asserts a
close never contributes to its own feature set even when timestamps tie
(strict `<`, plus `j != i` belt-and-braces).
