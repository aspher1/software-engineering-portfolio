#!/usr/bin/env python3
"""Offline journal field-usage audit (Z1 recommendation D: optional fields
that no experiment uses are reviewed for sunset after 14 days).

For every field that appears in the trade journal, reports:
  - first_seen / last_non_null: journal timestamps (None, NaN and "" count
    as null),
  - days_since_used: days from last_non_null to `now`,
  - days_tracked: days from first_seen to `now`,
  - analysis_refs: analysis/**/*.py files that mention the field as a quoted
    string literal (analysis/obs itself is excluded),
  - sunset_review: True if the field has had no non-null value for >= 14
    days, was never populated after >= 14 days, or no analysis script
    references it after >= 14 days.

Flags only. Nothing is deleted or edited; a human decides. Not on the bot's
hot path. Journal stamps are naive and mix local/UTC time (see
analysis/x1_ruggap), so day counts can be off by a few hours, which doesn't
matter against a 14-day threshold.

    python analysis/obs/field_usage_audit.py [--journal P] [--analysis-dir D]
        [--out P] [--now "YYYY-MM-DD HH:MM:SS"]
"""
import argparse
import json
import math
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DEFAULT_JOURNAL = os.path.join(ROOT, "runs", "paper-1h", "trades.jsonl")
DEFAULT_ANALYSIS = os.path.join(ROOT, "analysis")
DEFAULT_OUT = os.path.join(HERE, "field_usage_audit.json")
SUNSET_DAYS = 14
TS_FMT = "%Y-%m-%d %H:%M:%S"
QUOTED = re.compile(r"""["']([A-Za-z_][A-Za-z0-9_]*)["']""")


def _non_null(v):
    if v is None or v == "":
        return False
    if isinstance(v, float) and not math.isfinite(v):
        return False
    return True


def _parse_ts(value):
    try:
        return datetime.strptime(value, TS_FMT)
    except (TypeError, ValueError):
        return None


def journal_fields(path):
    """({field: recency info}, stats). Missing/corrupt input never raises."""
    stats = {"lines": 0, "corrupt_lines": 0, "records_without_ts": 0,
             "missing": False}
    fields = {}
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except (OSError, TypeError, ValueError):
        stats["missing"] = True
        return fields, stats
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            stats["lines"] += 1
            try:
                rec = json.loads(line)
            except (ValueError, RecursionError):
                stats["corrupt_lines"] += 1
                continue
            if not isinstance(rec, dict):
                stats["corrupt_lines"] += 1
                continue
            ts = _parse_ts(rec.get("ts"))
            if ts is None:
                stats["records_without_ts"] += 1
            for key, value in rec.items():
                info = fields.setdefault(key, {
                    "first_seen": None, "last_non_null": None,
                    "records_with_field": 0, "non_null_records": 0})
                info["records_with_field"] += 1
                if ts is not None and (info["first_seen"] is None
                                       or ts < info["first_seen"]):
                    info["first_seen"] = ts
                if _non_null(value):
                    info["non_null_records"] += 1
                    if ts is not None and (info["last_non_null"] is None
                                           or ts > info["last_non_null"]):
                        info["last_non_null"] = ts
    return fields, stats


def scan_references(analysis_dir, names, exclude=None):
    """({field: sorted relative paths}, stats) for quoted-literal mentions.
    `exclude` defaults to <analysis_dir>/obs (this audit's own directory)."""
    names = set(names)
    refs = {n: set() for n in names}
    stats = {"files_scanned": 0, "scan_errors": 0}
    if not isinstance(analysis_dir, str) or not os.path.isdir(analysis_dir):
        stats["missing"] = True
        return {n: [] for n in names}, stats
    if exclude is None:
        exclude = (os.path.join(analysis_dir, "obs"),)
    excluded = {os.path.abspath(e) for e in exclude}
    for dirpath, dirnames, filenames in os.walk(analysis_dir):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__"
                             and os.path.abspath(os.path.join(dirpath, d))
                             not in excluded)
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                stats["scan_errors"] += 1
                continue
            stats["files_scanned"] += 1
            rel = os.path.relpath(full, os.path.dirname(analysis_dir))
            for token in set(QUOTED.findall(text)) & names:
                refs[token].add(rel)
    return {n: sorted(v) for n, v in refs.items()}, stats


def _days(later, earlier):
    if later is None or earlier is None:
        return None
    return round((later - earlier).total_seconds() / 86400, 2)


def audit(journal_path=DEFAULT_JOURNAL, analysis_dir=DEFAULT_ANALYSIS,
          now=None, sunset_days=SUNSET_DAYS):
    now = now or datetime.now()
    fields, jstats = journal_fields(journal_path)
    refs, sstats = scan_references(analysis_dir, fields)
    out, candidates = {}, []
    for name in sorted(fields):
        info = fields[name]
        since = _days(now, info["last_non_null"])
        tracked = _days(now, info["first_seen"])
        reasons = []
        if since is not None and since >= sunset_days:
            reasons.append("no non-null value in %.1f days" % since)
        if (info["non_null_records"] == 0 and tracked is not None
                and tracked >= sunset_days):
            reasons.append("never populated in %.1f days" % tracked)
        if not refs[name] and tracked is not None and tracked >= sunset_days:
            reasons.append("no analysis script references it after %.1f days"
                           % tracked)
        flagged = bool(reasons)
        if flagged:
            candidates.append(name)
        out[name] = {
            "first_seen": (info["first_seen"].strftime(TS_FMT)
                           if info["first_seen"] else None),
            "last_non_null": (info["last_non_null"].strftime(TS_FMT)
                              if info["last_non_null"] else None),
            "days_since_used": since,
            "days_tracked": tracked,
            "records_with_field": info["records_with_field"],
            "non_null_records": info["non_null_records"],
            "analysis_refs": refs[name],
            "sunset_review": flagged,
            "reason": "; ".join(reasons),
        }
    return {
        "schema": "field_usage_audit/v1",
        "now": now.strftime(TS_FMT),
        "journal": journal_path,
        "analysis_dir": analysis_dir,
        "sunset_days": sunset_days,
        "journal_stats": jstats,
        "scan_stats": sstats,
        "fields": out,
        "sunset_candidates": candidates,
        "note": "Flags only; nothing was deleted or changed. A human decides "
                "whether a flagged field stops being collected.",
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="journal field-usage audit")
    ap.add_argument("--journal", default=DEFAULT_JOURNAL)
    ap.add_argument("--analysis-dir", default=DEFAULT_ANALYSIS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--now", default=None)
    args = ap.parse_args(argv)
    now = _parse_ts(args.now) if args.now else None
    if args.now and now is None:
        print("--now must be 'YYYY-MM-DD HH:MM:SS'", file=sys.stderr)
        return 2
    res = audit(args.journal, args.analysis_dir, now)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(res, f, indent=2)
        f.write("\n")
    print("%d fields, %d flagged for sunset review: %s" % (
        len(res["fields"]), len(res["sunset_candidates"]),
        ", ".join(res["sunset_candidates"]) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
