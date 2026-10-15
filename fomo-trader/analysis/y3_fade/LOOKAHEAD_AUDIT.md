# Y3 look-ahead audit

For every feature used in the diagnostic and the pullback evaluation, its
timestamp relative to the decision point.

## Diagnostic features (no decision simulated — measurement only)

| Feature | Source | Timestamp vs entry |
|---|---|---|
| `entry` | entry record (`entry_ts`) | **at** the decision point |
| `peak` | close record (lifetime max observed during the hold) | **after** — used only as a retrospective drift proxy, never as a decision input |
| `exit` | close record (`close_ts`) | **after** — retrospective only |
| `signal_gain_pct`, `signal_ratio`, `liquidity_usd` | entry record, captured at signal/commit | **before/at** entry |
| `chain` | mint prefix | static |

The diagnostic makes no trade decision, so there is no decision point to
leak into. Peak/exit are explicitly labeled retrospective.

## Pullback variant (hypothetical decision: enter at entry×(1−X))

- The fill proof (`exit ≤ entry×(1−X)`) uses the close record — retrospective
  proof of a past event, not a forecast. Valid as a *lower bound* on fills:
  any fill the bound counts genuinely happened; fills the bound misses are
  unknown, not assumed.
- The variant assumes the pullback entry receives the **same exit price**
  as the actual entry. The actual entry was earlier and higher; a later,
  lower entry would face a different trailing-stop/TP path. This assumption
  is **optimistic for the pullback** (better entry, same exit) — it can only
  flatter the variant, and the variant still fails. Strengthens rejection.
- The variant assumes the retrace level is *detected* costlessly in real
  time. Live detection needs the price path we don't record — the variant
  is untestable live without new data capture (stated in the report).
- No signal, commit, or pre-entry field is used at any post-entry point.
  Chronological IS/OOS split is by journal append order (first 49 / last 25),
  fixed before results were viewed.
- Costs: round-trip variable costs recomputed at the hypothetical entry
  price via the same convention as `replay.py` (fee + slippage per leg,
  3× slippage stress column). Fixed legs dropped as dust — documented.

## Conclusion

No feature post-dates its use. The one retrospective input (exit price as
fill proof) is fenced as a bound, and every approximation in the variant
errs optimistic — the rejection survives its own best case.
