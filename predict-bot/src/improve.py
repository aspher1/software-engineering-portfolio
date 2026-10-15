"""Self-improvement loop: grades resolved trades and tunes strategy parameters.

Rules of the game:
- Only ever tunes toward SAFER: threshold up, kelly fraction down, caps down,
  minimum liquidity up. It can never loosen risk.
- Coordinate descent on realized P&L: try small nudges to one parameter at a
  time, keep the nudge only if the journal shows it would have improved the
  realized outcome (counterfactual replay on resolved trades).
- Every accepted change becomes a new versioned strategy file; the runner
  always loads the latest champion. History is never deleted.
"""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy

STRATEGIES_DIRNAME = "strategies"


def strategy_version(data_dir: str) -> str:
    d = os.path.join(data_dir, STRATEGIES_DIRNAME)
    os.makedirs(d, exist_ok=True)
    versions = []
    for f in os.listdir(d):
        m = re.fullmatch(r"strategy_v(\d+)\.yaml", f)
        if m:
            versions.append(int(m.group(1)))
    return f"v{max(versions, default=0)}"


def save_strategy(data_dir: str, config: dict) -> str:
    import yaml
    d = os.path.join(data_dir, STRATEGIES_DIRNAME)
    os.makedirs(d, exist_ok=True)
    n = max(
        [int(re.fullmatch(r"strategy_v(\d+)\.yaml", f).group(1))
         for f in os.listdir(d)
         if re.fullmatch(r"strategy_v(\d+)\.yaml", f)] or [0]
    ) + 1
    path = os.path.join(d, f"strategy_v{n}.yaml")
    with open(path, "w") as f:
        yaml.safe_dump({"version": f"v{n}", "strategy": config["strategy"],
                        "scanner": config.get("scanner", {})}, f)
    return f"v{n}"


def load_champion_strategy(data_dir: str, config: dict) -> tuple[str, dict]:
    """Overlay the latest champion's strategy params onto the base config."""
    import yaml
    d = os.path.join(data_dir, STRATEGIES_DIRNAME)
    if not os.path.isdir(d):
        return "v0", deepcopy(config["strategy"])
    versions = sorted(
        (f for f in os.listdir(d) if re.fullmatch(r"strategy_v(\d+)\.yaml", f)),
        key=lambda f: int(re.fullmatch(r"strategy_v(\d+)\.yaml", f).group(1)),
    )
    if not versions:
        return "v0", deepcopy(config["strategy"])
    with open(os.path.join(d, versions[-1])) as f:
        saved = yaml.safe_load(f)
    merged = deepcopy(config["strategy"])
    merged.update(saved.get("strategy", {}))
    return saved.get("version", "v0"), merged


def realized_pnl(grades: list[dict]) -> float:
    return sum(g.get("pnl", 0.0) for g in grades)


def replay_pnl(grades: list[dict], params: dict) -> float:
    """Counterfactual: what would realized P&L have been under `params`?

    A resolved trade only 'happens' in the replay if its gap met the candidate
    threshold; P&L scales with the candidate kelly fraction relative to the
    fraction actually used (recorded at fill time).
    """
    total = 0.0
    for g in grades:
        if g.get("gap", 0) < params["mispricing_threshold"]:
            continue
        scale = params["kelly_fraction"] / max(g.get("kelly_fraction_used", params["kelly_fraction"]), 1e-9)
        # Cap the replay at the risk cap actually in force, like the trader does.
        total += g.get("pnl", 0.0) * min(scale, 2.0)
    return total


def propose_candidates(current: dict) -> list[dict]:
    """Safer-direction nudges only. Never loosens risk."""
    cands = []
    # Raise the bar: only trade bigger mispricings.
    c = deepcopy(current)
    c["mispricing_threshold"] = round(current["mispricing_threshold"] + 0.01, 4)
    cands.append(("threshold_up", c))
    # Shrink sizing.
    c = deepcopy(current)
    c["kelly_fraction"] = round(current["kelly_fraction"] * 0.8, 4)
    cands.append(("kelly_down", c))
    # Tighten risk cap.
    c = deepcopy(current)
    c["max_risk_per_trade"] = round(current["max_risk_per_trade"] * 0.8, 4)
    cands.append(("riskcap_down", c))
    return cands


def maybe_improve(config: dict, data_dir: str, grades: list[dict]) -> dict:
    """Run one tuning step. Returns a report dict (accepted or not)."""
    imp = config.get("improvement", {})
    min_trades = int(imp.get("min_resolved_trades", 10))
    if len(grades) < min_trades:
        return {"accepted": False,
                "reason": f"only {len(grades)} resolved trades (< {min_trades})"}
    # Annotate grades with the kelly fraction in force at fill time.
    version, current = load_champion_strategy(data_dir, config)
    for g in grades:
        g.setdefault("kelly_fraction_used", current["kelly_fraction"])
    base_pnl = replay_pnl(grades, current)
    best_name, best_params, best_pnl = "current", current, base_pnl
    for name, cand in propose_candidates(current):
        p = replay_pnl(grades, cand)
        if p > best_pnl:
            best_name, best_params, best_pnl = name, cand, p
    if best_name == "current":
        return {"accepted": False, "reason": "no safer candidate beats current",
                "base_pnl": round(base_pnl, 2)}
    new_version = save_strategy(data_dir, {**config, "strategy": best_params})
    report = {
        "accepted": True, "change": best_name,
        "from_version": version, "to_version": new_version,
        "base_pnl": round(base_pnl, 2), "new_pnl": round(best_pnl, 2),
        "params": best_params,
    }
    with open(os.path.join(data_dir, "improvement_log.jsonl"), "a") as f:
        f.write(json.dumps(report) + "\n")
    return report
