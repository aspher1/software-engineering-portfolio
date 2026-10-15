# paper-trader

A paper-trading framework for stocks and crypto with a falsification-first
validation bar. The framework exists to kill bad ideas with data, not to
confirm them.

## The bar

No strategy is considered viable unless it clears, on paper:

- **30–50 closed trades** (no cherry-picked windows)
- **Profit factor > 1.2**
- **Walk-forward validated** (out-of-sample ≥ 60% of in-sample)
- **Max drawdown < 15%**

Anything that fails any of these is rejected. Most ideas fail. That's the
point.

## How it works

- `paper_trader.py` — the trading loop: scans on a schedule, evaluates
  signals, manages paper positions against the risk config
- `ai_gate.py` — an AI-assisted review gate on trade candidates
- `analyze.py` + `analysis/` — performance analysis: profit factor,
  drawdown, win rate, per-regime breakdowns
- `test_paper_trader.py` — test suite
- `run_bot.sh` — idempotent launcher (refuses duplicate starts)

## Risk config

Position sizing, max positions, max trades/day, cooldowns, and a drawdown
kill-switch are all declarative in the config — the loop enforces them, and
tests verify the enforcement.

> **Paper only.** This framework never places real orders and has no broker
> wiring. It is a measurement instrument for strategies, not a trading
> system.
