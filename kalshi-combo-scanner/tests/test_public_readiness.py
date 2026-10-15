"""Public-release hygiene: no private paths or internal tooling references in
shipped files, no secrets in .env.example, honest requirements, an offline
demo, and no write-capable HTTP calls."""

import ast
import importlib.util
import os
import re
import subprocess
import sys

import pytest

import scanner_env

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED_DIRS = ("scripts", "demo", "docs", "tests")
SHIPPED_FILES = ("README.md", "DISCLAIMER.md", ".env.example", "LICENSE", "requirements.txt")
SELF = os.path.abspath(__file__)

FORBIDDEN_SUBSTRINGS = ("/home/hatch", "/Users/", "workspace/goals", "~/workspace",
                        "hidden_files", "lanes/", "subagent", "agent-", "predict-bot")
# "cursor" alone is Kalshi's pagination parameter (params["cursor"]), so the
# check targets the editor/CLI product instead.
FORBIDDEN_PATTERNS = (re.compile(r"Cursor"),
                      re.compile(r"(?i)cursor[- ](agent|cli)|\.cursor/|cursor\.(com|sh)"))


def shipped_files():
    out = [os.path.join(REPO, f) for f in SHIPPED_FILES]
    for d in SHIPPED_DIRS:
        for root, dirs, files in os.walk(os.path.join(REPO, d)):
            dirs[:] = [x for x in dirs if x not in ("__pycache__", ".pytest_cache")]
            out += [os.path.join(root, f) for f in files if not f.endswith(".pyc")]
    return [p for p in out if os.path.abspath(p) != SELF]


def test_shipped_files_exist():
    for f in SHIPPED_FILES + ("CHANGELOG.md", ".gitignore", "demo/run_demo.py",
                              "demo/fixtures/legs.json"):
        assert os.path.isfile(os.path.join(REPO, f)), f
    for d in ("architecture", "combo-picker", "data-sources", "backtest-protocol", "validation"):
        assert os.path.isfile(os.path.join(REPO, "docs", f"{d}.md")), d


@pytest.mark.parametrize("path", shipped_files(), ids=lambda p: os.path.relpath(p, REPO))
def test_no_private_paths_or_internal_references(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    for s in FORBIDDEN_SUBSTRINGS:
        assert s not in text, f"{os.path.relpath(path, REPO)} contains {s!r}"
    for rx in FORBIDDEN_PATTERNS:
        m = rx.search(text)
        assert m is None, f"{os.path.relpath(path, REPO)} contains {m.group(0)!r}"


def test_env_example_has_no_secrets():
    with open(os.path.join(REPO, ".env.example")) as f:
        lines = [ln.strip() for ln in f if "=" in ln]
    assert lines
    for ln in lines:
        key, val = ln.lstrip("# ").split("=", 1)
        assert key.startswith("COMBO_SCANNER_"), ln
        assert not re.search(r"[A-Za-z0-9]{20,}", val), f"secret-looking value: {ln}"
        assert not re.search(r"(?i)key|secret|token|passphrase|password", key), ln


def requirement_names():
    with open(os.path.join(REPO, "requirements.txt")) as f:
        lines = [ln.split("#", 1)[0].strip() for ln in f]
    return [re.split(r"[<>=!~\[; ]", ln, maxsplit=1)[0] for ln in lines if ln]


def test_requirements_are_importable():
    names = requirement_names()
    assert "requests" in names
    for name in names:
        mod = name.lower().replace("-", "_")
        assert importlib.util.find_spec(mod) is not None, f"{name} not importable"


def test_requirements_cover_third_party_imports():
    local = {os.path.splitext(f)[0] for d in ("scripts", "tests")
             for f in os.listdir(os.path.join(REPO, d)) if f.endswith(".py")}
    reqs = {n.lower() for n in requirement_names()}
    for path in shipped_files():
        if not path.endswith(".py"):
            continue
        with open(path) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module]
            else:
                continue
            for m in mods:
                top = m.split(".")[0]
                if top in sys.stdlib_module_names or top in local or top == "__future__":
                    continue
                assert top.lower() in reqs, f"{os.path.relpath(path, REPO)} imports {top}"


def test_no_write_capable_http_calls():
    write_call = re.compile(r"\.(post|patch|delete)\(|requests\.(put|request)\("
                            r"|method\s*=\s*['\"](POST|PUT|PATCH|DELETE)")
    for d in ("scripts", "demo"):
        for path in shipped_files():
            if path.startswith(os.path.join(REPO, d)) and path.endswith(".py"):
                with open(path) as f:
                    m = write_call.search(f.read())
                assert m is None, f"{os.path.relpath(path, REPO)}: {m.group(0)}"


_NO_NETWORK_RUNNER = """
import runpy, socket, sys
def _blocked(*a, **k):
    raise RuntimeError("network disabled in demo test")
socket.socket.connect = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
sys.argv = ["demo/run_demo.py"]
runpy.run_path("demo/run_demo.py", run_name="__main__")
"""


def test_demo_runs_offline():
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMBO_SCANNER_")}
    r = subprocess.run([sys.executable, "-c", _NO_NETWORK_RUNNER], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "SYNTHETIC" in r.stdout
    assert re.search(r"\$10 pays \$\d+", r.stdout)
    assert "MODEL estimate — NOT guaranteed" in r.stdout


def test_env_path_resolution(monkeypatch, tmp_path):
    default = str(tmp_path / "default")
    monkeypatch.delenv("COMBO_SCANNER_TEST_DIR", raising=False)
    assert scanner_env.env_path("COMBO_SCANNER_TEST_DIR", default) == default
    monkeypatch.setenv("COMBO_SCANNER_TEST_DIR", "  ")
    assert scanner_env.env_path("COMBO_SCANNER_TEST_DIR", default) == default
    monkeypatch.setenv("COMBO_SCANNER_TEST_DIR", "./data/x")
    assert scanner_env.env_path("COMBO_SCANNER_TEST_DIR", default) == \
        os.path.join(scanner_env.REPO_ROOT, "data", "x")
    monkeypatch.setenv("COMBO_SCANNER_TEST_DIR", str(tmp_path / "abs"))
    assert scanner_env.env_path("COMBO_SCANNER_TEST_DIR", default) == str(tmp_path / "abs")


def test_load_dotenv_never_overrides_and_reads_only_prefixed_keys(monkeypatch, tmp_path):
    f = tmp_path / ".env"
    f.write_text("# comment\n"
                 "export COMBO_SCANNER_A_TEST='./a'\n"
                 "COMBO_SCANNER_B_TEST=./b\n"
                 "COMBO_SCANNER_EMPTY_TEST=\n"
                 "OTHER_VAR_TEST=nope\n"
                 "not a pair\n")
    fake_env = {"COMBO_SCANNER_B_TEST": "real"}
    monkeypatch.setattr(os, "environ", fake_env)
    scanner_env.load_dotenv(str(f))
    scanner_env.load_dotenv(str(tmp_path / "missing.env"))
    assert fake_env == {"COMBO_SCANNER_A_TEST": "./a", "COMBO_SCANNER_B_TEST": "real"}
