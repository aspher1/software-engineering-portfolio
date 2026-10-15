# LANE 2 — Implement ONE validated change: BSC wipe-risk drift size cap

You are the implementer for the FOMO Trader paper-trading bot at
`~/workspace/fomo-trader` (the current working directory). Paper-only:
`dry_run=true` must stay true everywhere. Implement EXACTLY the change below,
nothing else. One change max.

## The change (validated, ships on green suite)

**Rule:** in `enter_bsc()` (BSC entry path), when the signal-to-commit price
drift is positive (`slip_from_signal_pct > 0`), cap the ticket at the
allocator's 0.5x floor. Validation: 107 IS pairs edge +$0.2164/trade,
52 OOS pairs edge +$0.2405/trade (111% retention, base and 3x-slippage stress),
WR 30.8% OOS, savings broad-based and wipe-driven (80.7% of OOS savings from
9 capped wipe trades; only $1.06 of winner upside given up). BSC-scoped: the
wipe leak is BSC-only in the journal.

**How:**
1. Add a pure helper near `entry_evidence()` in `fomo_trader.py`:
   `wipe_drift_cap_native(signal, entry_native, native_usd, cfg_buy, buy_native)`
   returning `(buy_native_out, capped_bool, slip_or_None)`. Pure arithmetic on
   already-available inputs, no I/O:
   - `sig_usd = float(signal.get("signal_price_usd") or 0)` (guarded);
     `slip = entry_native * native_usd / sig_usd - 1` when `sig_usd > 0`
     and `entry_native` and `native_usd` are truthy, else `slip = None`.
   - If `slip is not None and slip > 0`: `floor_native = (cfg_buy or 0) * 0.5`;
     if `floor_native > 0 and buy_native > floor_native`: return
     `(floor_native, True, slip)`.
   - Otherwise return `(buy_native, False, slip)`. Never increases the ticket.
2. In `enter_bsc()`, after the `_allocate(...)` call and the `alloc_skip`
   check, BEFORE `scale = (buy_bnb / cfg_buy) ...` and before the
   `self.state["positions"][mint] = {...}` write, call the helper with
   `(signal, entry, self.bnb_usd(), cfg_buy, buy_bnb)` — `bnb_usd()` is cached
   120s and already fetched for `_allocate`, so this adds no network I/O.
   On `capped`, set `buy_bnb` to the returned value, record
   `alloc_rec["wipe_drift_cap"] = True` and
   `alloc_rec["wipe_drift_slip"] = round(slip, 6)`, and log one line:
   `"WIPE-DRIFT CAP <name>: slip %+.2f%% -> ticket capped at 0.5x floor"`.
   (`alloc_rec` is spread into the journal entry, so the flag is journaled
   automatically. Do not touch any existing journal field.)
3. Solana `enter()` is untouched. No guardrail, kill switch, cap, cooldown,
   or risk setting is modified. This is an ADDITIONAL risk control that can
   only shrink a ticket via `min()` semantics.

## Tests
Add `tests/test_wipe_drift_cap.py` with hermetic cases for the helper:
slip>0 above floor caps to exactly `cfg_buy*0.5`; slip>0 already below floor
unchanged; slip<=0 unchanged; slip==0 unchanged; missing/zero
`signal_price_usd` passes through with `slip=None`; missing/zero rate passes
through; never increases size (assert output <= input in all cases); returns
the slip value. Also a case asserting the helper is pure (no network: run with
`sockets disabled` style — simply don't call any I/O; a comment suffices).

## Docs
Prepend a CHANGELOG.md entry dated 2026-09-28 titled
"Track B Lane 2 Round 3 — BSC wipe-drift size cap (ships)":
what the rule is, hook location, validation numbers (IS 107 pairs edge
+$0.2164/trade base / +$0.2197 stress; OOS 52 pairs edge +$0.2405 / +$0.2425;
retention 111.1% base / 110.4% stress; OOS WR 30.8%, PF 0.22, max DD $56.35;
capped 20 IS / 24 OOS entries; OOS savings $12.50, 80.7% wipe-driven, $1.06
winner upside given up), the honest caveats (bot remains unprofitable overall:
OOS net −$56.35 after the cap; BSC-scoped, no cross-chain claim; journal slip
uses a post-commit rate while the live rule uses the pre-write cached rate —
sign agreement expected), and the full-suite result you observed.

## Hard constraints
- `dry_run=true` untouched in config and code defaults. No live trading code
  paths altered.
- NEVER read-modify-write `runs/paper-1h/trades.jsonl`, `state.json`,
  `bot.log`, `deaths.log`, `keypair.json`. Read-only if needed.
- No new network calls, subprocesses, randomness, or model inference in the
  entry/exit hot path.
- Do NOT restart the bot. Do NOT touch `runs/` state. Another lane is editing
  this tree concurrently: keep your diff minimal and surgical.
- After implementing, run the FULL suite yourself:
  `TMPDIR=/home/hatch/workspace/tmp-codex .venv/bin/python -m pytest tests/ -q`
  from the repo root. Report the pass/fail counts honestly.
- If the suite has failures caused by another lane's in-flight edits (not
  your files), do NOT fix them — report them and finish your own change.
