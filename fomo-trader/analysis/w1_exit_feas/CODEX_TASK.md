# CODEX TASK: FOMO Trader wave track W1 — pre-entry exit-feasibility simulation

You are the implementer for one of four parallel hypothesis tracks on the FOMO Trader paper memecoin bot. A coordinator verifies everything you produce; nothing ships on your word alone.

## Grounding (read first)
- Repo: `~/workspace/fomo-trader` (your cwd). Paper-only project. `dry_run=true` always.
- Deep-research report (your hypothesis menu): `~/workspace/research_notes/profitable-memecoin-tactics-20260927-1949/report.md` — read the exit-strategy and failure-modes sections first. Key high-confidence finding: **exit failure kills more than entry failure** — simulate the exit BEFORE entering.
- Existing offline harness: `analysis/replay.py` — reuse `load_journal()`, `net_pnl(trade, config, stress, cost_multiplier)`, `metrics()`, `filter_sweep()` semantics. Read it before writing anything.
- Journal: `runs/paper-1h/trades.jsonl` (entries + closes, ~70 entries / 73 closes). READ-ONLY for analysis; never modify it or anything under `runs/`.

## Hypothesis
Simulate the exit at entry time. For each candidate entry, estimate the slippage the planned position would suffer on exit, using only data available at entry: `liquidity_usd`, `m15_volume_usd`, `m15_buys`/`m15_sells`, `mcap_usd`, `commit_price_usd`, and the planned position size in USD (derive from `buy_sol`/`buy_bnb` or commit price fields; paper position is ~$5–10). Veto the entry when estimated exit slippage exceeds a calibrated threshold.

Starter model (improve, don't worship): constant-product price impact ≈ position_usd / liquidity_usd; activity-adjusted variants using m15 volume. Calibrate the veto threshold empirically — try 10, 15, 20, 25, 30, 40%. The research suggests 20–30% as a starting hypothesis only.

## Hard constraints
- **Write ONLY to** `analysis/w1_exit_feas/` and `tests/test_w1_exit_feas.py`. NEVER edit `fomo_trader.py`, config files, or anything under `runs/`. Never restart the bot. Never read/touch `keypair.json` or any secret.
- Hot-path suitability: the veto function must be pure-stdlib, local, and <50ms per call (prove with a timing test). No network calls, no LLM calls.
- Fail OPEN: missing/corrupt params or malformed signal → approve the entry (log one line), never raise. A veto must never silently stop the bot.
- One coherent rule. Do NOT split thresholds by chain unless you can show each chain's split independently validates (Solana and BSC evidence are separate — nearly all research evidence is Solana; do not transfer Solana numbers to BSC unmeasured).
- Never claim profitability or live-readiness. Never weaken rug/honeypot protections or loosen risk caps (you're not touching them anyway).

## Validation (mandatory — no shortcuts)
1. Build the offline join: entry → close pairs from `replay.load_journal()`, chronological order.
2. Compute each entry's feasibility score at entry time. Nulls: historical rows lack enrichment (expect ~95% null on old rows) → those entries are NOT vetoable; report veto coverage honestly.
3. Split chronologically: first ~2/3 closes = IS, last ~1/3 = OOS. For each threshold, report IS and OOS: net P&L, profit factor, win rate, max drawdown, trades retained, edge per retained trade.
4. Ship gate (all must hold): OOS retains ≥60% of IS per-trade edge; filtered OOS stays better than baseline OOS under replay's 3×-slippage stress (`cost_multiplier=3` semantics in `net_pnl`); win rate stays sane (<90%); no single-regime dependence (check per-chain results don't flip).
5. REJECT the rule if any gate fails. Rejection is a successful outcome — report it with numbers.
6. Write `analysis/w1_exit_feas/exit_feas.py` (estimator + `approve()` veto + `Params` JSON loader), `analysis/w1_exit_feas/exit_feas.json` (`{"threshold_pct": ..., "ship_recommend": true|false, ...}`), `analysis/w1_exit_feas/exit_feas_report.md` (full method, tables, caveats), and `tests/test_w1_exit_feas.py` (hermetic: estimator math, veto boundaries, fail-open, null handling, <50ms timing).
7. Run the FULL repo test suite (`python3 -m pytest tests/ -q`) — everything must pass, yours and the repo's. Fix what you break.

## What to print at the end (your coordinator parses this)
- Hypothesis tested and exact rule evaluated
- Per threshold: IS P&L / PF / WR / DD, OOS P&L / PF / WR / DD, 3× stress OOS P&L
- ship_recommend: true/false and the exact rule that would ship (or why not)
- Test counts: passed/failed for your suite and the full repo suite
- Files written, with one-line descriptions

Work carefully and honestly. A clean rejection with good numbers beats a shaky accept.
