# Paper trader CHANGELOG

Every parameter change, with the evidence that motivated it. v1 is the
baseline the analysis loop tunes from.

## v1 — 2026-09-24 (baseline, no evidence yet)
- Bankroll $1,000 virtual, $100/trade, max 5 positions, 10 trades/day cap,
  24h cooldown per symbol.
- Crypto (BTC/ETH/SOL/DOGE, 24/7): enter on +2.5%/1h momentum (4h non-negative);
  TP +6%, SL -4%, trailing 25% below peak; 0.6% slippage modeled per side.
- Stocks (NVDA/TSLA/AMD/PLTR/COIN/MSTR/AAPL/META, market hours): enter on
  +4% day move; TP +8%, SL -5%, trailing 25% below peak; 0.1% slippage per side.
- Confidence bar for real money: 30-50 closed trades, PF > 1.2, 3-4 weeks,
  max drawdown < 15%.

## v1.1 — 2026-09-27 (data-layer fixes; NO threshold changes)

Thresholds unchanged: crypto +2.5%/1h (4h >= 0), stocks +4% day. Kill switch
12%/10%, SL, TP, trail, size and caps unchanged.

### Evidence: why 2026-09-26 had 26 scans / 0 signals
- 2026-09-26 was a Saturday: market_hours() was False all day, so the stock
  sleeve never scanned. Only crypto was evaluated.
- Crypto was quiet. Replaying Coinbase 5m candles for the bot's live window
  (18:30-22:50 ET), the max trailing-1h move was BTC +0.26%, ETH +0.39%,
  SOL +0.63%, DOGE +0.66% — far below +2.5%. 0 signals was correct.
- The feed was not broken apart from one Coinbase ProxyError round at 19:30
  (every symbol failed at once with "Unable to connect to proxy ...
  Connection refused" — the local egress proxy, not Coinbase).
- Thresholds are not too strict for the validation bar: over the last 63
  sessions the 8 stocks touched +4% intraday on 112 symbol-days (>= 1 hit on
  47/63 sessions); crypto (last 14 days, trailing windows) produced signals
  on 2/1/3/3 distinct days for BTC/ETH/SOL/DOGE. With the 24h cooldown that
  is roughly 10+ entries/week — enough for 30-50 closes in 3-4 weeks.
  Reproduce with `python3 analysis/signal_frequency.py 14`.

### Fix: crypto "1h" momentum was not a 1h window (logic change)
- Old code compared spot to the close of the last completed *clock hour*
  (hourly candle c[1]), so "1h" actually spanned 0-60 min depending on
  minutes past the hour (at :05 it was a 5-minute move); "4h" spanned 3-4h.
  Positional indexing also silently shifted if Coinbase omitted a candle.
- Now: 5m candles keyed by timestamp; references are the price 60 and 240
  minutes ago (`momentum_from_candles`). Matches the documented rule.
- Evidence (14 days, 5m scan points, +2.5% & 4h >= 0): old rule 20 hits
  (BTC 4 / ETH 1 / SOL 5 / DOGE 10), trailing rule 60 hits (13/4/16/27).
  More signals come from measuring the documented window, not a lower bar.

### Fix: fallback crypto price source, fail closed
- Sources tried in order: Coinbase, then Kraken public API (spot + 5m OHLC).
  Spot and candles always come from the same source (no cross-exchange mix).
- Data is rejected (next source tried) on exception, non-positive price,
  newest candle older than 15 min, or a missing reference candle.
- If no source works for a symbol it is skipped and logged. If no crypto
  symbol has usable data the scan logs "crypto scan skipped" and increments
  new counter `scan_stats.crypto_skipped` (additive; reset daily with the
  other counters) so a blind day can be told apart from a quiet one.
  The rollover log line for 2026-09-26 reads "0 crypto skipped" only
  because the counter did not exist yet; by the new definition it was 1
  (the 19:30 all-symbol proxy failure).
- Exits (manage) now read spot only, with the same fallback chain, instead
  of requiring candles — a candle outage no longer blocks stop-loss checks.
- Caveat: the 19:30 incident was the local egress proxy refusing
  connections; that takes out every source at once and the bot correctly
  skips (fails closed) rather than trading.
- Live check 2026-09-27: Coinbase vs Kraken spot within 0.1%, 1h/4h
  momentum within 0.1pp for all four symbols.

### Fix: analyze.py throughput read the wrong keys
- analyze.py read `signals_found` / `signals_entered`; the bot writes
  `signals` / `entered`, so the report always showed 0 signals and could
  recommend loosening filters on false evidence. Now reads the bot's keys
  (old names still accepted), shows crypto_skipped, and flags a failing
  feed before blaming filters.

### Record: undocumented trail change found in config
- config.json has trail_pct crypto 3.0 / stocks 4.0, but v1 above says
  "trailing 25%". The rationale is in config `_note` (a 25% trail can never
  fire inside the TP/SL band) but it was never logged here. Recorded now;
  values not changed by this entry.

### Ops: why the bot stopped on 2026-09-26
- 18:35:24 exit=0: SIGTERM, clean shutdown (manual restart).
- 18:36:50 exit=137: pid 4720 SIGKILLed externally 3s after a second
  instance was refused by the single-instance guard (manual kill).
- 18:37 and 21:38 instances (latter pid 3044) died with no shutdown line and
  no deaths.log entry — i.e. run_bot.sh was killed too. The host rebooted
  (uptime from 2026-09-27 09:09) and state.json was last written 22:49:37,
  so the runtime was torn down under the bot. Not a bot crash. Nothing
  restarts the bot after a host restart; it must be relaunched.

## v2 — 2026-09-27 (AI entry gate; strategy regime change)

### What changed
- New `ai_gate.py`: every rules-engine candidate signal is now screened by
  an LLM (Codex CLI on the operator's ChatGPT login, account default model)
  before entry. The model votes APPROVE/VETO with confidence + one-line
  reason; only approvals reach `enter()`.
- Exits, sizing, and all risk rails (SL/TP/trailing, 12% drawdown kill,
  daily caps, cooldown) remain 100% deterministic. The gate votes on
  entries only.
- Fail-closed: model timeout, unparseable output, missing binary, quota
  exhaustion, or daily call cap (20/day) -> VETO, never enter. Quota
  vetoes are counted separately from judgment vetoes so the analysis can
  tell "AI said no" apart from "AI was unavailable".
- Journal: entries carry `"strategy": "v2-ai-gate"` plus the model's
  verdict; vetoes are journaled as `ai_veto` records with the reason.
- Config: new `ai_gate` section (enabled, backend codex, stdin prompt,
  120s timeout, 20 calls/day).

### Evidence / rationale
- v1 (pure rules) produced 0 entries in its first weekend: correct behavior
  (flat crypto, stocks closed), but the operator asked for AI judgment in
  the entry loop to filter low-quality breakouts before they cost paper
  money.
- Validation note: v1 closed 0 trades, so the 30-50 trade validation bar
  starts clean under v2. v1 vs v2 trades are separable in the journal via
  the `strategy` tag.

### Known limits
- Backend depends on the ChatGPT quota behind the Codex CLI; when the
  quota is exhausted (e.g. 2026-09-27 ~10:43-13:07 ET) the gate vetoes
  everything. Possible v2.1: Cursor-agent CLI fallback.
- 0.149.0 CLI reads the prompt from stdin and needs --skip-git-repo-check;
  the gate auto-resolves the binary from a candidate list so VM rebuilds
  don't silently disable it (missing binary = veto, logged).

## v2.1 — 2026-09-27 (Cursor primary backend for the AI gate)

### What changed
- The AI gate now tries backends as a chain: `["cursor", "codex"]`
  (config `ai_gate.backends`). Cursor's agent CLI
  (`~/.local/bin/agent -p --trust --output-format text`, prompt as
  positional arg) is primary; Codex CLI remains as fallback.
- First parseable vote wins. A backend that is missing, quota-exhausted,
  timed out, or answers unparseably is skipped; all backends failing ->
  fail-closed VETO.
- The deciding backend is recorded on approvals (`ai.backend`) and vetoes
  (`ai_backend`) so the analysis can compare model quality.
- Quirk: the Cursor agent stalls on a confirmation prompt when run with
  cwd inside the bot dir, so it runs from a neutral cwd (`/tmp`).

### Evidence / rationale
- Codex backend depends on the ChatGPT quota shared with dev work; it was
  exhausted 2026-09-27 ~10:43 AM-1:07 PM ET, which would have blinded the
  gate. Cursor diversifies the quota risk. Live smoke test 2026-09-27:
  Cursor returned a genuine skeptical veto ("thin weekend volume ...
  isn't the sustained upward pressure a momentum entry needs") in ~13s.
