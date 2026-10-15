# X1 look-ahead audit

Every feature used in the forensics and counterfactuals, with proof it
predates the decision point it is evaluated against.

## Forensic measurements (no decisions — descriptive only)
- `entry`, `peak`, `exit`, `reason`, `realized_*`, `rungs`: all recorded in
  the journal close record, i.e. AFTER the trade completed. Used only to
  measure what happened, never to simulate a decision. No look-ahead surface.
- `anatomy()` decomposition: uses peak/exit/reason post-hoc to split a
  realized loss into lag vs gap portions. The trigger ratios (0.88/0.70) are
  the bot's configured thresholds, read from `fomo_trader.py`, not fitted.
- Chain heuristic (mint starts with `0x` → bsc): matches `replay.chain_of`,
  the same heuristic the bot's journal pipeline uses. Entry records carry
  explicit `chain` on newer trades; heuristic agrees on all 75 closes
  (verified: no close has an explicit chain contradicting the heuristic).

## Intervention A — 24h dump-cooldown counterfactual
- Decision point: each entry's commit time.
- Information used: only close records with `close_ts` EARLIER in journal
  append order than the entry (journal is append-only and chronological;
  Track C verified append order is chronological across the EDT/UTC launcher
  switches). No future closes are consulted.
- The live bot's `_dump_cooldown_active` compares real timestamps with a
  true 24h window; the counterfactual's any-prior-dump rule is conservative
  (vetoes a superset), so the $7.05 saving is an upper bound, not an
  overstatement of precision.
- State persistence (`state.json["dump_cooldown"]`) is written at close time
  in the live bot; the audit confirms the write path
  (`fomo_trader.py:1913`) executes on every close, before any later entry
  decision.

## Intervention B — gain veto IS/OOS
- Decision point: entry commit.
- Feature: `signal_gain_pct`, logged on the signal line which precedes the
  entry (Track C's join: signal ≤ 30 min before entry; entry never precedes
  its signal).
- Split: chronological first-2/3 IS by journal append order, decided before
  thresholds were evaluated. Threshold (300%) was a round-number candidate,
  not fitted — and it still failed OOS, which is the point.
- Costs: `replay.net_pnl` with the same fee/slippage model as all other
  tracks; 3× stress triples per-leg slippage.

## Latency (elite-bar item 1)
- `ruggap.py` is offline forensics; it does not run in the hot path.
  Benchmarked anyway: 20k calls, `is_rug_gap` and `anatomy` each < 1ms mean
  (test asserts). The live 24h-cooldown check it references is an O(1) dict
  lookup executed in `guardrails_ok` and under `self.lock` in
  `_entry_commit_ok` — no measurable hot-path impact.

## Concurrency (elite-bar item 3)
- `ruggap.py`: pure functions, no module-level mutable state, no I/O.
  Race-free by construction.
- Live guard referenced: `_dump_cooldown_active` reads `self.state` dict;
  called (a) in `guardrails_ok` (scan loop, no lock — read-only dict lookup,
  atomic under GIL), and (b) in `_entry_commit_ok` under `self.lock` at
  commit time, which closes the scan→commit race (a dump exit landing
  between scan and commit is still vetoed). The write at close
  (`state.setdefault("dump_cooldown", {})[mint] = time.time()`) occurs in
  the manage/close path; dict setitem is atomic under the GIL. No deadlock
  surface: no locks taken inside the check itself.

## Adversarial review (elite-bar item 2)
- 15 hermetic tests cover: None/NaN/inf/zero/negative/missing fields,
  non-dict inputs, BSC-without-rate (unscorable → (0,0), never raises),
  malformed reason strings, cooldown with non-dump closes and malformed
  journal rows. All fallbacks return safe defaults; nothing raises.
