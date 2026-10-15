# kalshi-combo-scanner

A paper-only **Kalshi combo picker**. It sweeps every open Kalshi market
across categories (sports, crypto, economics, politics, culture and more),
looking for legs with a defensible fair value. It then assembles multi-leg
combo picks phrased the way the Kalshi app's combo builder phrases them:
*pick 2+ markets, $10 pays $X*. Each pick is shown next to a model fair
payout and an estimated edge. A lightweight CLI prints one JSON object per
run, which makes it easy to put on a schedule.

> **Paper / analysis only.** This tool places no orders, uses no
> credentials, has no account wiring and never moves money. Every network
> call is a public, unauthenticated read. All model outputs are
> **estimates, never guarantees**. See [DISCLAIMER.md](DISCLAIMER.md).

## Features

- **Tier 1, locked sweep.** It looks for multi-leg positions whose quoted
  prices sum under 100¢ after taker fees: single-market Yes+No (Type A),
  proven exhaustive event books (Type B), Kalshi-vs-Polymarket pairs
  (Type C), and cumulative-threshold ladder pairs (Type D). Type D buys YES
  on the looser threshold and NO on the stricter one in a same-title,
  same-expiry all-"less" or all-"greater" event, so it always pays at least
  $1. Tier 1 usually finds zero, and that is the normal baseline.
- **Tier 2, edge.** It flags cross-venue gaps of 4–8¢ and pre-fee Yes+No
  near-arbs. These are labeled "NOT a locked profit" and are usually empty.
- **Tier 3, crypto model-edge.** It prices live 15-minute crypto up/down
  windows against public Kraken spot with a fair-value model. These are
  directional estimates that can lose.
- **Tier 4, combo picker.** It turns confirmed Tier 3 and cross-venue legs
  into 2–4-leg parlay picks. The picks use correlation rules, a 1¢/leg
  pricing guard, conservative fees and Kelly-capped paper sizing. Picks are
  rare, and empty lists are the expected result on most runs.

## Quickstart

Requires Python 3.10+.

```bash
git clone https://github.com/<your-account>/kalshi-combo-scanner.git
cd kalshi-combo-scanner
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optional; defaults work as-is
python3 scripts/combo_scan.py --no-mark
```

`--no-mark` is a read-only diagnostic run that writes no dedup state and no
pick log. A full sweep takes roughly 20–40 seconds and prints one JSON
object. A one-line coverage summary goes to stderr.

Useful flags:

| Flag | Effect |
| --- | --- |
| `--edge-tier` / `--no-edge-tier` | turn Tier 2 on or off |
| `--model-edge` / `--no-model-edge` | turn Tier 3 on or off |
| `--combo-picks` / `--no-combo-picks` | turn Tier 4 on or off |
| `--max-outlay`, `--min-outlay` | outlay window in dollars (defaults 20 and 10) |
| `--min-roi` | Tier 1 minimum after-fee ROI, in percent |

All four tiers are on by default.

## Architecture

Each run pages the full open-market listing in parallel close-time shards
and screens it on listing prices. In the background it fetches Polymarket
and Kraken data. It confirms candidates against live orderbooks with
depth-aware sizing and rounded-up taker fees, re-checks new alerts just
before output, and then runs the combo picker on the confirmed legs.
Details: [docs/architecture.md](docs/architecture.md).

| Module | Role |
| --- | --- |
| `scripts/combo_scan.py` | CLI entry point: sweep, Tier 1/2 screens, sizing, fees, dedup, output |
| `scripts/model_edge.py` | Tier 3 fair-value model and Kraken fetchers |
| `scripts/combo_picker.py` | Tier 4 combo picker (pure; no HTTP) |
| `scripts/scanner_env.py` | `.env` and path configuration |
| `scripts/backtest_model_edge.py` | offline replay of the Tier 3 rule on a graded-signals file (not shipped) |
| `scripts/analyze_capped_events.py` | offline analysis of events `screen_type_b` skips for exceeding the 40-outcome cap (not in the 5-min scan) |
| `demo/run_demo.py` | offline picker demo on synthetic legs |

## Configuration

All settings are optional. Set them in the environment or in `.env`. Real
environment variables take precedence, and relative paths are resolved
against the repo root.

| Variable | Default | Purpose |
| --- | --- | --- |
| `COMBO_SCANNER_STATE_DIR` | `./data/state` | dedup state (`combo_scanner_state.json` + `.bak`) |
| `COMBO_SCANNER_CACHE_DIR` | `./data/cache` | sweep shard plan and series fee-multiplier caches |
| `COMBO_SCANNER_PICK_LOG` | `./data/picker_picks.jsonl` | append-only log of new combo picks (marking runs only) |
| `COMBO_SCANNER_GRADES_PATH` | `./data/signal_grades.jsonl` | input for `backtest_model_edge.py` |

## Demo

```bash
python3 demo/run_demo.py
```

The demo runs the real picker on a hand-written **synthetic** fixture
(`demo/fixtures/legs.json`). It needs no network and no setup. It shows a
same-event rejection and prints the top pick as
`$10 pays $X (model fair $Y, edge +Zc)` with the model-estimate disclaimer.
The numbers are invented and are not market data.

## Testing

```bash
pytest tests/ -q
```

The suite is fully offline. It covers fee-table rows, exact-cent combo math,
depth gating, dedup, sweep coverage, cross-venue guards, the Tier 3 model,
the Tier 4 picker and public-release hygiene. It also includes a Tier 1
spot-probe: 48¢ + 48¢ must be locked and 49¢ + 49¢ must be rejected.

## Docs

- [Architecture](docs/architecture.md): pipeline, module map, data flow, budgets, fees.
- [Combo picker](docs/combo-picker.md): leg pool, correlation rules, parlay pricing, pass bars, sizing, limits.
- [Data sources](docs/data-sources.md): every feed used, and the no-edge list.
- [Backtest protocol](docs/backtest-protocol.md): the pick-log schema, grading against Kalshi settlement, and the n ≥ 100 rule.
- [Validation](docs/validation.md): the ship bar and what was tried and falsified.
- [CHANGELOG](CHANGELOG.md): release history.

## Contributing

Issues and pull requests are welcome. Keep changes small and keep
`pytest tests/ -q` green. Some lines are hard:

- Never loosen fees, depth or outlay gates, or edge thresholds to produce
  more hits.
- Never add order placement, credentials or account wiring.
- Label every estimate as an estimate and every backtest as a backtest.
- New leg sources need a validated probability model before their legs can
  enter the picker.

## Disclaimer

Paper/analysis only, and not financial advice. Read
[DISCLAIMER.md](DISCLAIMER.md) before using this software. Released under
the [MIT License](LICENSE).
