# Allocator analyst cron

`analysis/allocator_analyst.py` is the offline warm loop for the entry
allocator. It runs from cron, never inside the bot process, and only reads
`runs/paper-1h/trades.jsonl` and `runs/paper-1h/config.json`.

## Schedule

Every 2 hours:

```cron
0 */2 * * * cd /home/hatch/workspace/fomo-trader && .venv/bin/python analysis/allocator_analyst.py >> analysis/allocator_cron.log 2>&1 && .venv/bin/python analysis/llm_analyst.py >> analysis/allocator_cron.log 2>&1 && .venv/bin/python analysis/apply_validated_weights.py >> analysis/allocator_cron.log 2>&1
```

Each tick compares the journal's close count with `closes_seen` in
`analysis/allocator_watermark.json` and counts priced closes appended after
that point. With fewer than 25 new priced closes it logs
one line (`no-op: N new priced closes since watermark (need 25)`) and exits 0
without writing anything. Otherwise it writes:

- `analysis/allocator_report_<UTC ts>.md`: attribution tables, recency
  check, proposed weights, gate numbers and verdict.
- `analysis/allocator_proposal.json`: weights, verdict and gate metrics
  (overwritten each run).
- `analysis/allocator_watermark.json`: the new `closes_seen`.

`--force` runs regardless of the watermark; `--min-new N` changes the
threshold; `--journal`, `--config`, `--out-dir` override paths.

## Optional LLM proposal pass

`analysis/llm_analyst.py` runs after the deterministic analyst in the same
offline cron. It reads the journal and current config, sends a compact evidence
pack to Codex CLI, and replays the proposed weights through the same cost and
overfit gate without refitting them. It has its own
`analysis/llm_analyst_watermark.json` and no-ops below 25 new priced closes or
when the deterministic verdict is `insufficient_data`. A passing proposal
replaces `allocator_proposal.json` with `source: llm_analyst`; a failed one is
recorded only in `llm_analyst_report_<UTC ts>.md`. Codex errors, timeouts,
quota limits, and invalid JSON leave the deterministic proposal intact.
The deterministic loop remains the fallback. Neither analyst edits bot config.

## Recency discipline

Weights are fit on the full journal with a 14-day half-life exponential
recency weight, never on a recent window. The report states explicitly when
recent results are better than the older journal and does not treat that as
a persistent edge. The gate's out-of-sample window is the most recent ~30%
of closes, so a recency-only edge cannot pass.

## Gate (all must hold for `pass`)

- At least 30 OOS closes held out at <= 40% of the journal (else
  `insufficient_data`, never a pass).
- Weights refit on IS only, replayed with costs (entry 100 bps + exit
  300 bps of notional, fixed fee SOL $0.60 / BSC $0.20 per trade, linear
  ticket scaling).
- IS edge > 0, OOS edge > 0, OOS retains >= 60% of IS edge per trade. The
  reported OOS decay <= 70% check is implied by that retention threshold.
- IS win rate of allocator-taken trades <= 90%; no single chain or time
  tercile holds > 80% of the positive edge. Edge from shrinking or skipping
  one weak chain still counts toward chain concentration.

The replay uses linear ticket scaling. Balance-aware sizing and the money.py
risk ceiling are future replay extensions; historical entry balances are not
journaled.

## Auto-apply (closes the loop)

`analysis/apply_validated_weights.py` runs third in the same cron. When
`allocator_proposal.json` carries a genuine `"verdict": "pass"` (all 7 gate
checks green, OOS >= 30 closes at <= 40% of the journal, retention >= 60%),
it copies ONLY the validated `weights` into the live
`runs/paper-1h/config.json` allocator section — mode, take_threshold, caps,
kill switches and risk settings are never touched — then gracefully
restarts the bot (SIGTERM, position-snapshot verification) so the live path
picks them up.

Safety: refuses unless `dry_run == true` (paper-only is unconditional);
backs up the config before writing; each distinct proposal applies at most
once (SHA-256 hash marker in `analysis/applied_weights.json`); on restart
failure or a position mismatch it restores the backup and logs a loud
ALERT. Every attempt is journaled in `analysis/weight_apply_log.jsonl`.
A `"fail"` or `"insufficient_data"` verdict changes nothing.

## Manual apply (revert path)

Weights now apply automatically on a passing gate. To revert an applied set:
restore the newest `runs/paper-1h/config.json.bak.<ts>`, or set
`allocator.mode` back to `"shadow"`, and restart the bot (`dry_run` stays
`true`).
