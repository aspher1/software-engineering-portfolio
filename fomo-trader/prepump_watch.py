#!/usr/bin/env python3
"""Pre-pump phone alert watcher: prints NEW PRE-PUMP / FOMO SIGNAL lines from
the FOMO Trader bot log since the last run. Watermark is a line count stored
in the goal's hidden_files. Prints JSON: {"new": [lines]}."""
import json
import os
import re

LOG = os.path.expanduser("~/workspace/fomo-trader/runs/paper-1h/bot.log")
WM = os.path.expanduser(
    "~/workspace/goals/fomo-trader-memecoin-bot/hidden_files/"
    "prepump_watermark.json")

PAT = re.compile(r"(PRE-PUMP|FOMO SIGNAL)")


def main():
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        print(json.dumps({"new": [], "error": "bot.log missing"}))
        return
    last = 0
    try:
        with open(WM) as f:
            last = int((json.load(f) or {}).get("lines", 0))
    except Exception:
        last = 0
    if last > len(lines):
        last = 0  # log rotated/truncated: start over
    new = [ln for ln in lines[last:] if PAT.search(ln)]
    os.makedirs(os.path.dirname(WM), exist_ok=True)
    with open(WM, "w") as f:
        json.dump({"lines": len(lines)}, f)
    print(json.dumps({"new": new}))


if __name__ == "__main__":
    main()
