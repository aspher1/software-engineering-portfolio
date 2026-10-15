# Z4 data capture

Z4 is optional, additive paper-trading research data. It does not change the trade journal, state, order decisions, or the existing Z3 observation flags. Both capture flags default to off; `config.json` has no `research` section. No bot restart was performed for this change.

Enable with a `research` object containing `"enabled": true` and either or both boolean flags: `"capture_quote_path": true`, `"capture_failed_quotes": true`. A missing, disabled, or malformed section leaves capture off. No new price, liquidity, or RPC request is made for capture.

## Files

- `<runs directory>/quote_paths/<sanitized mint>.jsonl`: one row for every manage tick of that position, including a tick with no usable price. Fields: `ts` (local journal timestamp format), `mint`, `chain`, `price_usd` (the already fetched native mark times the normal native/USD rate when its 120-second cache is fresh; otherwise null), and `liquidity_usd` (null: the manage loop has no fresh liquidity observation). Files remain after close. Sanitization replaces non-ASCII alphanumeric filename characters with `_`.
- `<runs directory>/failed_quotes.jsonl`: append-only shared file, protected by a process lock. Fields: `ts`, `mint`, `chain`, `reason`, plus available signal and quote economics. Reasons are `entry_quote_failed`, `dust_quote`, `honeypot_no_sell_route`, `honeypot_quote_unreachable`, `bsc_honeypot`, `commit_cap`, and `commit_kill_switch`. Solana and BSC entry quote and dust failures are both captured.

Each quote-path file stops growing at 5,000 rows or 1 MiB, whichever comes first. A capped mint is logged once. The failed-quotes file has no size cap; normal log rotation or archival remains an operator task. All write failures are logged once per cause and ignored by trading.

The Solana `Rpc` fallback is **not captured**. `Rpc.call` handles methods that lack a mint, and it has neither research config nor a state path. Adding capture there would require passing trading context into the general RPC retry client. Its retry behavior was left untouched.

## Research uses

W3 stale-position time exits can now examine observed intratrade mark paths and approximate the value at proposed timeout times. Y2 stop-tightening can compare proposed mark triggers with later marks. Round-4 pre-entry rug screens can join failed-entry reasons and known quote economics with subsequent observations. These are mark-based diagnostics: a mark is not a fill, and absent sell quotes or thin liquidity can make an apparent exit impossible at the marked price.
