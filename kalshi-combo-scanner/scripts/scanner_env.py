"""Local configuration: optional <repo>/.env plus COMBO_SCANNER_* path overrides.

Stdlib only. Importing this module loads <repo>/.env once; real environment
variables always win over .env values. Only COMBO_SCANNER_* keys are read.
"""

from __future__ import annotations

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFIX = "COMBO_SCANNER_"


def load_dotenv(path=None):
    """Set COMBO_SCANNER_* variables from KEY=VALUE lines in `path` (default
    <repo>/.env) without overriding variables already in the environment.
    Blank lines, # comments, an `export ` prefix and surrounding quotes are
    tolerated. A missing or unreadable file is ignored."""
    path = path or os.path.join(REPO_ROOT, ".env")
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = (s.strip() for s in line.split("=", 1))
        val = val.strip("'\"")
        if key.startswith(PREFIX) and val:
            os.environ.setdefault(key, val)


def env_path(var, default):
    """Path from environment variable `var`, else `default`. `~` is expanded;
    a relative value is taken relative to the repo root, not the cwd."""
    val = os.environ.get(var, "").strip()
    if not val:
        return default
    return os.path.normpath(os.path.join(REPO_ROOT, os.path.expanduser(val)))


load_dotenv()
