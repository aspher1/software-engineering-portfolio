# Y2 — Hard stop-loss recalibration: report

**Verdict: REJECT. `ship_recommend = false`.** A tighter hard stop helps a
little, in one regime, on six trades — and fails the out-of-sample retention
gate (0%). Nothing ships.

## What was tested

Hard stops at **-15/-20/-25/-30/-35%** (the journal baseline reflects the
bot's actual 40%-then-35% hard-stop regimes) on all **74 closed trades**:
same entries, same TP ladder, same dump detector. Chronological split
**IS = first 49 closes, OOS = last 25** (matching the X2 moonbag track).
Realistic costs per `analysis/replay.py` convention; 3× slippage stress;
fills only at observed prices except the stop fill itself.

## The model (assumptions A1–A4, all stated in `stoploss.py`)

- Only the **final exit leg** can change; TP rung legs are identical. The
  delta is applied to the position fraction still held (`final_portion`,
  derived from `rungs_fired`; all journal rungs are index 0 = 50% of balance).
- **A1 trailing-stop exits:** the recorded exit ≈ the trade's minimum. If not
  a gap and the stop level sits above the exit, price crossed it on the way
  down → stop fires at the stop price. Gap trades (`exit/peak < 0.4`, the
  journal's rug-gap definition) gap straight through: fill = observed exit.
- **A2 dump-detector / venue-dump exits:** unchanged — the 12%/60s detector
  is already the faster crash exit; in gaps both get the gap fill.
  Sensitivity B (optimistic) lets the stop replace dump exits when above them.
- **A3 stale exits:** fires only if the stale exit is at/below the stop
  (never triggered at the tested levels; coded generally).
- **A4 take-profit / manual rotation:** unchanged; an intratrade dip below the
  stop is unobservable → reported as UNKNOWN, not zero.

## Results

| Stop | Split | Net USD | PF | WR | Max DD | Fired | Killed/Reduced/Unknown | 3× stress |
|---|---|---|---|---|---|---|---|---|
| 15% | ALL | -116.12 | 0.272 | 25.7% | 116.30 | 6 | 0/0/4 | -119.33 |
| 15% | IS (49) | -62.20 | 0.380 | 36.7% | 68.41 | 6 | 0/0/4 | -64.54 |
| 15% | OOS (25) | -53.92 | 0.087 | 4.0% | 53.92 | 0 | 0/0/0 | -54.79 |
| 20% | ALL | -118.80 | 0.267 | 25.7% | 118.80 | 6 | 0/0/4 | -122.01 |
| 25% | ALL | -121.44 | 0.263 | 25.7% | 121.44 | 5 | 0/0/4 | -124.63 |
| 30% | ALL | -123.10 | 0.260 | 25.7% | 123.10 | 1 | 0/0/4 | -126.29 |
| 35% | ALL | -123.52 | 0.260 | 25.7% | 123.52 | 0 | 0/0/4 | -126.71 |
| — | BASELINE ALL | -123.52 | 0.260 | 25.7% | 123.52 | — | — | -126.71 |
| — | BASELINE IS | -69.60 | 0.354 | 36.7% | 75.63 | — | — | -71.92 |
| — | BASELINE OOS | -53.92 | 0.087 | 4.0% | 53.92 | — | — | -54.79 |

(Baseline ALL net -$123.52 reproduces the X2 moonbag track's baseline exactly.)

Sensitivities (ALL net USD): at 15%, fill-2%-adverse → -$117.03;
dump-flip (optimistic) → -$112.27. Fill-shock sweep on the total edge vs
baseline: 0% → +$7.40; 2% → +$6.49; 5% → +$5.12; 10% → +$2.84.

## Why it fails

1. **0% OOS retention (gate: ≥60%).** The entire +$7.40 edge is 6 IS trades
   (+$0.64 to +$1.92 each). OOS: 0 fires, $0 edge. This is the textbook shape
   the retention gate exists to catch.
2. **Single-regime dependence.** IS exit mix: 5 stop-fires, 12 trail-beats-stop,
   14 dumps, 9 trail-gaps. OOS: **23 of 25 are dump exits**, 1 stale, 1
   trail-gap — zero slow bleeds. A hard stop cannot help a rug regime by
   construction (A2); the recent regime is all rugs.
3. **No calibration plateau.** Tighter-is-better is monotonic (15% > 20% >
   … > 35%), not a plateau — the "optimum" is an artifact of the 6-trade
   sample, not a calibrated peak.
4. **Magnitude.** +$0.10/trade against a -$1.67/trade bleed fixes ~6% of the
   loss. Even validated, this is triage, not a fix.

## What survives scrutiny (for the record)

- **Zero winners killed, zero reduced**, at every level — the charleytrades
  objection does not materialize in-sample. 4 discretionary winners have
  unobservable intratrade dips (reported unknown, not zero).
- The edge survives the 3× slippage stress and a 10% adverse fill shock.
- The mechanism is economically sound (cut slow bleeds short) and directionally
  consistent with the external charleytrades calibration (-25% recommended).

## Recommendation

Do not ship any level now. Re-test after materially more **bleed-regime**
trades accumulate (the current OOS contains none). If the effect replicates
OOS, the honest form is probably a **regime-aware** rule (tighten the stop
only when exits are bleeding, not gapping), not a global -15%.

## Files

- `analysis/y2_stoploss/stoploss.py` — engine (direct implementation; see note)
- `analysis/y2_stoploss/y2_results.json` — full numbers
- `tests/test_y2_stoploss.py` — 18 hermetic tests
- `analysis/y2_stoploss/STAFF_REVIEW.md` — staff review
- `analysis/y2_stoploss/LOOKAHEAD_AUDIT.md` — look-ahead audit

## Implementation note

Built directly in the coordinator's shell, per the brief's fallback clause:
Codex CLI's read-only sandbox failed 3 of 4 sibling wave-1 tracks, and this
is a pure replay analysis with fully understood inputs. Method and numbers
are independently verifiable from the journal via the script above.
