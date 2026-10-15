# HANDOFF — FOMO Trader (paper memecoin bot) + Spike Radar

**For:** the Cursor agent receiving this project.
**Date:** 2026-09-24. **Status:** paper mode ONLY (`dry_run=true`). No live authorization exists.

## What this is

An automated memecoin rotation bot: it scans Solana + BSC for FOMO-style
momentum setups, paper-buys them, and manages exits aggressively
(12% local dump detector, DexScreener m5 venue tripwire, trailing stops,
TP ladder, stale exit). Goal: rotate in and out of coins, bank profit fast,
never ride a rug to zero.

Two components in this package:
- `fomo-trader/` — the trading bot (this doc's focus).
- `memecoin-radar/` — a separate 5-min Solana spike alert script (`radar.py`);
  chat alerts only, no trading.

## Quick start (paper)

```bash
cd fomo-trader
bash setup.sh            # creates .venv, installs deps
source .venv/bin/activate
python fomo_trader.py --config runs/paper-1h/config.json
```

- `dry_run=true` in the config = full paper simulation (no keys needed, not
  even for BSC — quotes are read-only `eth_call`).
- The run dir `runs/paper-1h/` holds `config.json`, `state.json` (positions,
  counters, realized P&L), `trades.jsonl` (append-only journal), `bot.log`.
- Never run two copies against the same run dir (pidfile + pgrep guard, but
  don't rely on it). **A copy of this bot is still paper-trading on the
  machine it came from** — coordinate before starting a second instance or
  journals will diverge.

## Architecture

| File | Role |
|---|---|
| `fomo_trader.py` (~2900 lines) | Everything: hunter scan loop, entry filters, per-position manage threads (2s poll), exits, state/journal |
| `bsc_swap.py` | BSC swaps via PancakeSwap V2 router: read-only `eth_call` quotes, honeypot round-trip screen, paper/live execute paths |
| `keystore.py` | Multi-format Solana/BSC key loading, 0600-perm warnings, never logs key material |
| `bsc_wallet.py` | BSC RPC fallback, chain-id 56 verify, BNB balances, local sign proof |
| `analyze.py` | P&L analysis + `--apply` self-tuner (max 1 conservative tweak/day, paper-only guard, auto-revert) |
| `dashboard.py` | Stdlib-only mobile web UI (status, positions, Start/Stop/Kill, token auth) |
| `deploy.sh` | One-command Ubuntu VPS setup (systemd units for bot + dashboard) |
| `run_bot.sh` | Launch wrapper: stays alive as parent, logs exit codes to `deaths.log` so silent deaths identify themselves (137=SIGKILL, 139=segfault, 143=SIGTERM, 42=halt) |
| `prepump_watch.py`, `diag_nearmiss.py` | Diagnostics |
| `tests/test_manage_cycle.py` | Manage-cycle + regression tests (41 passing as of handoff) |
| `CHANGELOG.md` | Human-readable change history — read this first for "why is it like this" |

## Strategy (from `runs/paper-1h/config.json`)

- Size: 0.06 SOL / 0.009 BNB (~$7 each). Max 3 open positions, 30 trades/day, 6/hour (rolling 60-min window, race-proof commit-time cap check under lock).
- Entry: ≥+20% 15m momentum, liquidity ≥$8k, buy/sell ratio ≥1.5, honeypot screen (SOL: Jupiter route sanity; BSC: buy+sell quote round-trip ≥50%, fail closed), pullback wait, dust-quote rejection.
- Exits: +50% → sell half, rest runs with 12% trailing stop (no upside ceiling); base trailing 25%; hard stop -35%; **dump detector** ≥12% fall from 60s window max → sell all instantly; **venue tripwire** DexScreener m5 ≤-30% (chain-aware) → sell all; **stale exit** 30 min & <+10% → sell all.
- Daily realized-loss lockout: $23 USD across both chains (entry lockout, not position kill).
- Moonbag mode: disabled (user hasn't decided).

## Data feeds — read this before touching scan code

- **GeckoTerminal free tier: ~30 req/min, and concurrent bursts get 429s that
  escalate to dropped connections lasting hours.** All GT traffic goes through
  `gt_get()` → token bucket (≤20/min) + circuit breaker + 429 backoff with
  jitter. Never fetch pages with a thread pool.
- **DexScreenerSource fallback** (`/latest/dex/tokens/{addrs}`, 5m window)
  engages only when GT yields 0 pages. Rug guard is source-agnostic.
- Manage loop polls positions every 2s and tracks quote blindness (loud BLIND
  warning if no price for 5+ min; never exits on stale prices).

## Known issues / pending work

1. **Silent external deaths (UNRESOLVED):** the bot periodically dies with no
   traceback — pidfile survives, so it's SIGKILL/segfault from outside Python.
   `run_bot.sh` + `faulthandler` + `deaths.log` exist to diagnose; a 3-min
   watchdog restarts it. Permanent fix = VPS/systemd supervision (`deploy.sh`).
2. **Emergency-exit tests missing:** add tests for venue-triggered sell with no
   executable route, repeated failed emergency sells, no false close after
   failure, correct accounting after eventual execution.
3. **No per-position route/feed health tracking yet:** want last executable
   quote, last independent price, quote-blind/execution-blind durations.
4. **Live BSC path untested:** `execute_buy/sell` live paths are dormant and
   have never run — harden + test with throwaway keys before any real use.
5. **In-memory vs on-disk state:** the bot holds state in memory; editing
   `state.json` externally gets overwritten on next save. Change counters via
   restart or not at all.
6. `realized_usd` only accumulates on closes since the BSC accounting migration
   (2026-09-24 ~16:10); older closes have SOL-only figures. Journal
   `trades.jsonl` has per-trade `sol_usd` stamps for full history.

## Hard rules (user's standing constraints)

- **Paper only until the user explicitly authorizes live trading.** The
  self-tuner refuses to run if `dry_run=false`.
- Never request or accept private keys / seed phrases in chat. For eventual
  live use, the user installs the exported private key directly on the bot host
  with `0600` perms (paths in `wallets` config). **Wallet files are
  intentionally EXCLUDED from this package** (`runs/paper-1h/keypair.json`
  stayed behind).
- Report P&L in dollars; distinguish current-state from lifetime journal.
- No strategy guarantees profit or prevents every rug — exits minimize, not
  eliminate, tail risk.

## Current state at handoff

- Bot alive on source machine, paper trading. Today: 12/30 trades, realized
  ≈ **-$5.25** (BSC got rugged repeatedly tonight; exits capped each loss at
  ~$1–2.60 instead of full position).
- Proofs from today: dump detector caught -88.6%/60s (STARBUCKS, -$1.31 not
  -$7); venue tripwire caught DexScreener m5 -83.9% (.BOT, -$0.08); stale exit
  freed a dead slot at +6.2%.

## memecoin-radar/

`radar.py` — scans Solana pools every 5 min, alerts on 20x–100000x surges in
15m–6h windows, one alert per token per tier. Alert-only, no trading. No
liquidity filter, no +300% cutoff (user's explicit setting — don't re-add).
