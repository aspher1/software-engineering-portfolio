#!/usr/bin/env python3
"""Offline, paper-only LLM allocator proposal; never used by the bot path.

Run after allocator_analyst.py. The journal and run config are read-only.
Only a passing replay can replace the deterministic proposal; applying it
remains a manual operation.
"""
import argparse
from datetime import datetime, timezone
import json
import math
import os
import subprocess
import sys

try:
    from . import allocator_analyst as analyst
except ImportError:  # direct cron invocation
    import allocator_analyst as analyst

allocator = analyst.allocator
CODEX = "/usr/bin/codex"
TIMEOUT_SEC = 600
QUOTA_MARKERS = ("quota", "usage limit", "rate limit", "limit reached",
                 "out of credits")


def log(message):
    print("[llm_analyst] %s" % message, flush=True)


def _pnl(rows):
    vals = [t["realized_usd"] for t in rows]
    gains = sum(v for v in vals if v > 0)
    losses = -sum(v for v in vals if v < 0)
    return {"count": len(vals), "realized_usd": round(sum(vals), 2),
            "profit_factor": round(gains / losses, 3) if losses else None}


def build_evidence_pack(trades, acfg):
    """Small descriptive pack, with entry-only features and journaled scores."""
    weights = allocator.weights_of(acfg)
    threshold = allocator.threshold_of(acfg)
    buckets = {}
    for name in ("signal_gain_pct", "liquidity_usd", "buy_sell_ratio"):
        groups = {"low": [], "mid": [], "high": [], "missing": []}
        for t in trades:
            raw = t["features"].get(name)
            value = allocator._num(raw)
            if value is None or (name in ("liquidity_usd", "buy_sell_ratio") and value <= 0):
                label = "missing"
            else:
                norm = allocator.normalize(t["features"])[name]
                label = "low" if norm < -1 / 3 else "high" if norm > 1 / 3 else "mid"
            groups[label].append(t)
        buckets[name] = {k: _pnl(v) for k, v in groups.items()}
    chains = {}
    for t in trades:
        chains.setdefault(t["chain"], []).append(t)
    latest = []
    for t in trades[-30:]:
        entry = t["entry"] or {}
        latest.append({
            "trade_id": "%s:%s" % (t["idx"], t["close"].get("mint", "")),
            "closed_at": t["ts_str"], "chain": t["chain"],
            "normalized_features": allocator.normalize(t["features"]),
            "allocator_score_at_entry": analyst._f(entry.get("allocator_score")),
            "allocator_multiplier_at_entry": analyst._f(entry.get("allocator_multiplier")),
            "realized_usd": round(t["realized_usd"], 4),
        })
    return {"trade_summary": _pnl(trades),
            "by_chain": {k: _pnl(v) for k, v in chains.items()},
            "buckets_by_normalized_feature": buckets,
            "bucket_boundaries": "low < -1/3, mid -1/3..1/3, high > 1/3; missing separate",
            "current_weights": weights, "current_take_threshold": threshold,
            "last_priced_closes": latest}


def build_prompt(pack):
    return ("You are tuning entry-allocator weights for a paper memecoin bot. "
            "Propose EITHER weight deltas from the current weights OR a full "
            "replacement weight vector. Output JSON ONLY matching this schema: "
            '{"weights": {<feature>: <float>, ...}, "take_threshold": <float|null>, '
            '"rationale": [{"change": ..., "evidence": <trade ids or bucket names>}]}. '
            "Use all known weight keys for a full replacement; a proper subset "
            "means deltas to the current weights. Keep the final values in [-3, 3]. "
            "Cite specific trade IDs or named buckets for each change. "
            "No narrative fluff, no markdown, no extra keys. "
            "Use only evidence in this JSON pack; null entry scores mean they "
            "were not journaled. Evidence pack:\n" +
            json.dumps(pack, separators=(",", ":"), allow_nan=False))


def _unique_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key: %s" % key)
        obj[key] = value
    return obj


def parse_proposal(output, current_weights, current_threshold):
    raw = json.loads(output, object_pairs_hook=_unique_pairs)
    if not isinstance(raw, dict) or set(raw) != {"weights", "take_threshold", "rationale"}:
        raise ValueError("proposal must have exactly weights, take_threshold, rationale")
    changes = raw["weights"]
    known = set(allocator.FEATURES) | {"intercept"}
    if not isinstance(changes, dict) or not changes or not set(changes) <= known:
        raise ValueError("unknown or empty weight keys")
    full_replacement = set(changes) == known
    if full_replacement:
        weights = {}
    else:
        weights = dict(current_weights)
    for key, value in changes.items():
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError("weight must be a finite number: %s" % key)
        if not -3 <= value <= 3:
            raise ValueError("weight out of range: %s" % key)
        weights[key] = float(value) if full_replacement else weights[key] + float(value)
    if set(weights) != known or any(not math.isfinite(v) or not -3 <= v <= 3
                                   for v in weights.values()):
        raise ValueError("final weights must cover all features within [-3, 3]")
    threshold = raw["take_threshold"]
    if threshold is None:
        threshold = current_threshold
    elif isinstance(threshold, bool) or not isinstance(threshold, (float, int)) \
            or not math.isfinite(threshold) or not 0.1 <= threshold <= 0.9:
        raise ValueError("take_threshold outside [0.1, 0.9]")
    rationale = raw["rationale"]
    if not isinstance(rationale, list) or not rationale or any(
            not isinstance(item, dict) or set(item) != {"change", "evidence"}
            or not isinstance(item["change"], str) or not item["change"].strip()
            or not isinstance(item["evidence"], (str, list))
            or not item["evidence"]
            or (isinstance(item["evidence"], list) and any(
                not isinstance(citation, str) or not citation.strip()
                for citation in item["evidence"])) for item in rationale):
        raise ValueError("rationale must cite evidence for each change")
    return {"weights": weights, "take_threshold": float(threshold),
            "rationale": rationale}


def call_codex(prompt):
    try:
        result = subprocess.run([CODEX, "exec", "--model", "gpt-6-sol"],
                                input=prompt, capture_output=True, text=True,
                                timeout=TIMEOUT_SEC, cwd=analyst.ROOT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log("deterministic-only: Codex unavailable (%s)" % type(exc).__name__)
        return None
    combined = (result.stdout + " " + result.stderr).lower()
    if any(marker in combined for marker in QUOTA_MARKERS):
        log("deterministic-only: Codex quota or usage limit")
        return None
    if result.returncode:
        log("deterministic-only: Codex exited %d" % result.returncode)
        return None
    return result.stdout


def _report(proposal, gate, pack):
    failed = [c["name"] for c in gate["checks"] if not c["passed"]]
    return ("# LLM allocator proposal\n\n"
            "Verdict: **%s**. This is offline paper replay; manual apply only.\n\n"
            "Failed checks: %s\n\n"
            "## Proposal and cited rationale\n\n```json\n%s\n```\n\n"
            "## Gate metrics\n\n```json\n%s\n```\n\n"
            "Evidence: %d priced closes; current weights and buckets were "
            "provided to Codex.\n" %
            (gate["verdict"], ", ".join(failed) or "none",
             json.dumps(proposal, indent=2, sort_keys=True),
             json.dumps(gate, indent=2, sort_keys=True),
             pack["trade_summary"]["count"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--journal", default=analyst.DEFAULT_JOURNAL)
    ap.add_argument("--config", default=analyst.DEFAULT_CONFIG)
    ap.add_argument("--out-dir", default=analyst.ANALYSIS_DIR)
    args = ap.parse_args(argv)
    watermark_path = os.path.join(args.out_dir, "llm_analyst_watermark.json")
    proposal_path = os.path.join(args.out_dir, "allocator_proposal.json")
    rows, _ = analyst.load_journal(args.journal)
    n_closes = sum(r.get("type") == "close" for r in rows)
    wm = analyst._read_json(watermark_path, {})
    seen = wm.get("closes_seen", 0) if isinstance(wm, dict) else 0
    if not isinstance(seen, int) or seen < 0 or seen > n_closes:
        seen = 0
    close_idx = new_priced = 0
    for row in rows:
        if row.get("type") == "close":
            close_idx += 1
            if close_idx > seen and analyst.close_usd(row) is not None:
                new_priced += 1
    if new_priced < analyst.MIN_NEW_CLOSES:
        log("no-op: %d new priced closes since watermark (need %d)" %
            (new_priced, analyst.MIN_NEW_CLOSES))
        return 0
    prior = analyst._read_json(proposal_path, {})
    if not isinstance(prior, dict) or prior.get("verdict") == "insufficient_data":
        log("no-op: deterministic gate has insufficient data or no proposal")
        return 0
    cfg = analyst._read_json(args.config, {})
    acfg = cfg.get("allocator", {}) if isinstance(cfg, dict) else {}
    if not isinstance(acfg, dict):
        acfg = {}
    trades, _ = analyst.join_trades(rows)
    pack = build_evidence_pack(trades, acfg)
    output = call_codex(build_prompt(pack))
    if output is None:
        return 0
    try:
        proposal = parse_proposal(output, pack["current_weights"],
                                  pack["current_take_threshold"])
    except (ValueError, TypeError) as exc:
        log("deterministic-only: invalid Codex JSON (%s)" % exc)
        return 0
    gate = analyst.run_gate(trades, proposal["take_threshold"],
                            proposed_weights=proposal["weights"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report_path = os.path.join(args.out_dir, "llm_analyst_report_%s.md" % stamp)
    analyst._write_atomic(report_path, _report(proposal, gate, pack))
    if gate["verdict"] == "pass":
        accepted = {"generated_at": datetime.now(timezone.utc).isoformat(),
                    "verdict": "pass", "source": "llm_analyst",
                    "weights": proposal["weights"],
                    "take_threshold": proposal["take_threshold"],
                    "rationale": proposal["rationale"], "gate": gate,
                    "costs": analyst.DEFAULT_COSTS,
                    "report": os.path.basename(report_path),
                    "apply": "Manual only; this script never edits the bot config."}
        analyst._write_atomic(proposal_path, json.dumps(accepted, indent=1,
                                                        sort_keys=True) + "\n")
        analyst._write_atomic(watermark_path, json.dumps({
            "closes_seen": n_closes, "updated_at": accepted["generated_at"],
            "report": os.path.basename(report_path)}, indent=1) + "\n")
    log("verdict=%s trades=%d report=%s" % (gate["verdict"], len(trades), report_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
