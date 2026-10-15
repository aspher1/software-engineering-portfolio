# E1 offline validation harness

The pre-registration is frozen in `E1_PREREGISTRATION.md`. No wallet lead has a verified full address, creator-diversity history, or replay tape in this workspace. `e1_results.json` therefore records `not_runnable`; it is **not** an empirical result. `latency.json` is a synthetic local benchmark.

Use `run_e1(resolved_addresses, source, price_path, stats_by_address, since, until, sol_usd, output_path)` from `e1.py`. Inject a `TapeSource` or `CachedSource` and a `GeckoTerminalPricePath`. For hermetic replay, inject a local `PricePath` implementation backed by archived bars. Supply a timestamped SOL/USD rate and a preselected mapping from token mint to **SOL-quoted** pool. `write_run_log` records resolved addresses and the public source URL before the run. Do not infer an address from a prefix.

A normalized hourly tape JSONL row needs `wallet`, `mint`, `side` (`BUY`/`SELL`), Unix `ts`, `price_sol`, `size_sol`, and `tx_sig`; optional `slot` is accepted. The adapter also accepts `trader`, `blockTime`, and `txHash` aliases. Research tape schemas that lack explicit SOL swap economics require a separate documented normalization step. Incomplete rows are skipped and counted. The legacy keyless Solscan endpoint is best effort and may provide no normalized swap rows; an empty response does not establish inactivity.

GeckoTerminal provides minute OHLCV by pool. The harness treats a bar's close as available at its minute end and uses the first close at or after the delayed decision time, within 60 seconds. This is an approximate paper fill; it does not measure spread, market impact, or executable depth. The public API rate bucket defaults to 10 requests/minute, caps at 20, and opens a 60-second circuit on errors.

Run focused tests with `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_z2_copytrade.py`; run the synthetic benchmark with `PYTHONPATH=. .venv/bin/python -m analysis.z2_copytrade.benchmark`. A live implementation would need to prove a <50 ms decision budget, separate from this offline analysis.
