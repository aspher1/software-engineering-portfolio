#!/usr/bin/env python3
"""Apply a fully validated allocator proposal to the paper bot, once."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import allocator  # noqa: E402

CHECKS = {
    "is_edge_positive", "oos_edge_positive", "oos_retention", "oos_decay",
    "is_win_rate_not_overfit", "edge_not_single_chain",
    "edge_not_single_time_regime",
}


def log(message):
    print("[apply_validated_weights] " + message, flush=True)


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def passing_gate(proposal):
    if not isinstance(proposal, dict) or proposal.get("verdict") != "pass":
        return False
    gate = proposal.get("gate")
    if not isinstance(gate, dict) or gate.get("verdict") != "pass":
        return False
    counts = (gate.get("n"), gate.get("n_is"), gate.get("n_oos"))
    if any(type(n) is not int or n <= 0 for n in counts):
        return False
    if counts[0] != counts[1] + counts[2] or counts[2] < 30 or counts[2] / counts[0] > .4:
        return False
    checks = gate.get("checks")
    if not isinstance(checks, list) or len(checks) != len(CHECKS):
        return False
    seen = set()
    for check in checks:
        if not isinstance(check, dict) or check.get("name") not in CHECKS:
            return False
        name = check["name"]
        if name in seen or check.get("passed") is not True or not isinstance(check.get("threshold"), str) or not check["threshold"]:
            return False
        seen.add(name)
        value = check.get("value")
        if name in ("is_win_rate_not_overfit", "edge_not_single_chain", "edge_not_single_time_regime") and value is None:
            # The analyst allows unevaluable checks; no partial gate is safe to apply.
            return False
        if not finite_number(value):
            return False
        limits = {"is_edge_positive": value > 0, "oos_edge_positive": value > 0,
                  "oos_retention": value >= .60, "oos_decay": value <= .70,
                  "is_win_rate_not_overfit": value <= .90,
                  "edge_not_single_chain": value <= .80,
                  "edge_not_single_time_regime": value <= .80}
        if not limits[name]:
            return False
    if seen != CHECKS:
        return False
    for side in ("is", "oos"):
        summary = gate.get(side)
        if not isinstance(summary, dict) or not finite_number(summary.get("edge_per_trade")):
            return False
    for field in ("retention", "decay"):
        if not finite_number(gate.get(field)):
            return False
    for field in ("chain_concentration", "time_concentration"):
        concentration = gate.get(field)
        if (not isinstance(concentration, dict) or concentration.get("evaluable") is not True
                or not finite_number(concentration.get("share"))):
            return False
    return True


def checked_weights(weights):
    if not isinstance(weights, dict) or set(weights) != set(allocator.DEFAULT_WEIGHTS):
        raise ValueError("weights must cover exactly the allocator feature set and intercept")
    if any(not finite_number(v) or not -3 <= v <= 3 for v in weights.values()):
        raise ValueError("weights must be finite numbers within [-3, 3]")
    return {k: float(v) for k, v in weights.items()}


def read_json(path):
    with path.open() as handle:
        return json.load(handle)


def write_atomic(path, value):
    temporary = path.with_name(path.name + ".tmp.%d" % os.getpid())
    try:
        with temporary.open("w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def bot_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        cmdline = Path("/proc/%d/cmdline" % pid).read_bytes().replace(b"\0", b" ")
        return b"fomo_trader" in cmdline
    except (OSError, PermissionError):
        return False


def read_pid(path):
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def position_ids(path):
    state = read_json(path)
    positions = state.get("positions") if isinstance(state, dict) else None
    if not isinstance(positions, dict):
        raise ValueError("state.json has no positions mapping")
    return set(positions)


def stop_bot(pid):
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not bot_alive(pid):
            return True
        time.sleep(.25)
    return not bot_alive(pid)


def start_bot(root, pid_path):
    # run_bot.sh owns its child and its PID file; the shell exits after launch.
    proc = subprocess.Popen(
        ["bash", "-c", "setsid nohup ./run_bot.sh >> runs.log 2>&1 < /dev/null &"],
        cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    proc.wait(timeout=5)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        pid = read_pid(pid_path)
        if bot_alive(pid):
            return pid
        time.sleep(.25)
    return None


def append_journal(path, record):
    with path.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def apply(root):
    root = Path(root)
    analysis = root / "analysis"
    run = root / "runs" / "paper-1h"
    proposal_path = analysis / "allocator_proposal.json"
    config_path = run / "config.json"
    pid_path = run / "bot.pid"
    state_path = run / "state.json"
    try:
        proposal = read_json(proposal_path)
    except (OSError, ValueError) as exc:
        log("no-op: proposal unavailable or invalid (%s)" % exc)
        return 0
    if not passing_gate(proposal):
        log("no-op: proposal has no complete passing gate")
        return 0
    try:
        config = read_json(config_path)
        if not isinstance(config, dict) or config.get("dry_run") is not True:
            raise ValueError("REFUSE: dry_run must be true (paper-only)")
        weights = checked_weights(proposal.get("weights"))
        if not isinstance(config.get("allocator"), dict):
            raise ValueError("allocator config section missing")
        proposal_hash = hashlib.sha256(json.dumps(proposal, sort_keys=True, allow_nan=False).encode()).hexdigest()
        marker_path = analysis / "applied_weights.json"
        if marker_path.exists() and read_json(marker_path).get("proposal_hash") == proposal_hash:
            log("already applied: %s" % proposal_hash)
            return 0
        old_pid = read_pid(pid_path)
        if not bot_alive(old_pid):
            raise ValueError("bot PID missing, dead, or not fomo_trader")
        before = position_ids(state_path)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        log("REFUSE: %s" % exc)
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = config_path.with_name(config_path.name + ".bak." + stamp)
    record = {"utc_ts": datetime.now(timezone.utc).isoformat(),
              "source": proposal.get("source", "allocator_analyst"),
              "verdict_summary": "pass; 7/7 checks green",
              "weights_hash": hashlib.sha256(json.dumps(weights, sort_keys=True).encode()).hexdigest(),
              "proposal_hash": proposal_hash, "old_pid": old_pid, "new_pid": None,
              "position_check": "not_run", "config_backup": str(backup)}
    try:
        shutil.copy2(config_path, backup)
        config["allocator"]["weights"] = weights
        write_atomic(config_path, config)
        if not stop_bot(old_pid):
            raise RuntimeError("old bot did not exit within 30 seconds")
        new_pid = start_bot(root, pid_path)
        record["new_pid"] = new_pid
        if new_pid is None or new_pid == old_pid:
            raise RuntimeError("restart produced no new live bot PID")
        after = position_ids(state_path)
        if after != before:
            record["position_check"] = "mismatch"
            raise RuntimeError("open positions changed across restart: %s -> %s" %
                               (sorted(before), sorted(after)))
        record["position_check"] = "match"
        # Record success only after the new process and saved positions verify.
        write_atomic(marker_path, {"proposal_hash": proposal_hash, "applied_at": record["utc_ts"]})
        record["result"] = "applied"
        append_journal(analysis / "weight_apply_log.jsonl", record)
        log("applied %s: PID %s -> %s; positions match" % (proposal_hash, old_pid, new_pid))
        return 0
    except Exception as exc:
        # If a new bot loaded the proposed config, stop it before restoring.
        candidate = record["new_pid"]
        if candidate is not None and candidate != old_pid and bot_alive(candidate):
            if not stop_bot(candidate):
                log("ALERT: failed to stop new bot PID %s; inspect immediately" % candidate)
        try:
            shutil.copy2(backup, config_path)
            record["rollback"] = "config_restored"
        except OSError as rollback_exc:
            record["rollback"] = "FAILED: %s" % rollback_exc
        # Never launch a duplicate while an old/new bot remains alive.
        recovery_pid = None
        if not bot_alive(old_pid) and not (candidate and bot_alive(candidate)):
            try:
                recovery_pid = start_bot(root, pid_path)
            except Exception as start_exc:
                record["recovery_error"] = str(start_exc)
        record["recovery_pid"] = recovery_pid
        record["result"] = "rollback"
        record["error"] = str(exc)
        append_journal(analysis / "weight_apply_log.jsonl", record)
        log("ALERT: allocator weight apply failed: %s\nALERT: backup=%s rollback=%s recovery_pid=%s" %
            (exc, backup, record.get("rollback"), recovery_pid))
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    return apply(args.root)


if __name__ == "__main__":
    sys.exit(main())
