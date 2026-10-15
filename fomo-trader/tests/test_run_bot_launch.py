"""Regression tests for run_bot.sh launch hardening.

Background: the 10-minute fix-loop worker launches the bot. The old wrapper
relied on the *caller's* `setsid nohup ... &`, which only moves the wrapper
into a new session/process group -- the bot stayed a *descendant* of the
worker, so a worker hitting its 600s timeout had its whole descendant tree
SIGKILLed and the bot died with it (exit 137, "line 42: <pid> Killed").

run_bot.sh now double-forks and reparents its daemon to init (PPID 1), so no
ancestor-tree SIGKILL sweep from any launcher can reach the bot again.

These tests are hermetic: they run against a scratch FOMO_PROJ dir with a
fake `python` (a bash script, so its argv still matches the pgrep pattern)
and never touch the real bot.
"""
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUN_BOT = REPO / "run_bot.sh"


def _pgrep(pattern):
    p = subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True, text=True, check=False
    )
    return [int(x) for x in p.stdout.split() if x.strip().isdigit()]


def _killtree(pid):
    """Recursively SIGKILL a process tree (like the worker-timeout cleanup)."""
    try:
        kids = subprocess.run(
            ["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False
        ).stdout.split()
    except Exception:
        kids = []
    for k in kids:
        if k.isdigit():
            _killtree(int(k))
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _ppid(pid):
    out = subprocess.run(
        ["ps", "-o", "ppid=", "-p", str(pid)],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    return int(out) if out.isdigit() else None


@pytest.fixture()
def sandbox(tmp_path):
    """Scratch FOMO_PROJ with a fake python that sleeps (keeps argv)."""
    sb = tmp_path / "proj"
    (sb / ".venv" / "bin").mkdir(parents=True)
    (sb / "runs" / "paper-1h").mkdir(parents=True)
    fake_python = sb / ".venv" / "bin" / "python"
    fake_python.write_text("#!/bin/bash\nsleep 120\n")
    fake_python.chmod(0o755)
    shutil.copy(RUN_BOT, sb / "run_bot.sh")
    (sb / "run_bot.sh").chmod(0o755)
    env = dict(os.environ, FOMO_PROJ=str(sb))
    yield sb, env
    # best-effort cleanup of anything the test left behind
    pat = f"fomo_trader[.]py --config {sb}/runs/paper-1h/config[.]json"
    for pid in _pgrep(pat):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _wait_for_bot_pid(sb, timeout=20):
    pidfile = sb / "runs" / "paper-1h" / "bot.pid"
    end = time.time() + timeout
    while time.time() < end:
        try:
            pid = int(pidfile.read_text().strip())
        except (ValueError, OSError):
            pid = None
        if pid and _alive(pid):
            return pid
        time.sleep(0.5)
    raise AssertionError("bot.pid never named a live bot")


def test_detached_daemon_survives_worker_tree_kill(sandbox):
    """The bot must survive a SIGKILL sweep of its launcher's process tree."""
    sb, env = sandbox
    # worker: launches run_bot.sh the fix-loop way, then idles (as a worker
    # would until its 600s timeout cleanup kills its tree)
    worker = subprocess.Popen(
        ["bash", "-c", 'setsid "$1/run_bot.sh" >> "$1/runs.log" 2>&1 & sleep 300',
         "worker", str(sb)],
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        bot_pid = _wait_for_bot_pid(sb)
        daemon_pid = _ppid(bot_pid)
        assert daemon_pid, "bot has no parent?"
        # daemon must be reparented to init -- outside the worker's tree
        assert _ppid(daemon_pid) == 1, (
            f"daemon ppid={_ppid(daemon_pid)}, expected 1 (init)"
        )
        _killtree(worker.pid)  # simulated worker-timeout cleanup
        time.sleep(2)
        assert _alive(bot_pid), "bot died in the launcher tree-kill (bug is back)"
        assert _alive(daemon_pid), "daemon died in the launcher tree-kill"
    finally:
        if worker.poll() is None:
            _killtree(worker.pid)


def test_refuses_duplicate_start(sandbox):
    """Launcher must refuse (exit 0, no new bot) when a bot is already live."""
    sb, env = sandbox
    run_dir = sb / "runs" / "paper-1h"
    pat = f"fomo_trader[.]py --config {run_dir}/config[.]json"
    fake_bot = subprocess.Popen(
        [str(sb / ".venv" / "bin" / "python"),
         str(sb / "fomo_trader.py"), "--config", str(run_dir / "config.json")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        (run_dir / "bot.pid").write_text(str(fake_bot.pid))
        # pidfile branch
        r = subprocess.run([str(sb / "run_bot.sh")], env=env,
                           capture_output=True, text=True, check=False)
        assert r.returncode == 0
        assert "refusing to start" in (run_dir / "deaths.log").read_text()
        assert _pgrep(pat) == [fake_bot.pid], "a second bot was started!"
        # pgrep branch (stale/missing pidfile)
        (run_dir / "bot.pid").unlink()
        r = subprocess.run([str(sb / "run_bot.sh")], env=env,
                           capture_output=True, text=True, check=False)
        assert r.returncode == 0
        assert "already alive (pgrep)" in (run_dir / "deaths.log").read_text()
        assert _pgrep(pat) == [fake_bot.pid], "a second bot was started!"
    finally:
        fake_bot.kill()
