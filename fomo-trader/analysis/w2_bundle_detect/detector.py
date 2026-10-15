"""Pure-stdlib W2 detector. Candidate rules are disabled unless supplied.

Percent fields use percentage points (0..100), never fractions. The score is
an uncalibrated descriptive indicator; it must not be treated as a probability.
"""

import logging
import math

LOG = logging.getLogger(__name__)

# Predeclared research candidates; none is a shipping threshold.
CANDIDATE_RULES = {
    "top1_gt_20": "holder_top1_pct > 20",
    "top1_gt_30": "holder_top1_pct > 30",
    "top1_gt_40": "holder_top1_pct > 40",
    "top5_gt_50": "holder_top5_pct > 50",
    "top5_gt_60": "holder_top5_pct > 60",
    "top5_gt_70": "holder_top5_pct > 70",
    "spread_gt_20": "holder_top5_pct - holder_top1_pct > 20",
    "spread_gt_30": "holder_top5_pct - holder_top1_pct > 30",
    "spread_gt_40": "holder_top5_pct - holder_top1_pct > 40",
    "top5_gt_60_top1_le_20": "holder_top5_pct > 60 AND holder_top1_pct <= 20",
    "first10_gt_35": "first10_buyers_pct > 35",
    "launch_block_gt_20": "launch_block_buyers_pct > 20",
}


def _pct(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def wash_score(_trades=None):
    """Unavailable: journal has no per-wallet, per-transaction buy/sell amounts.

    A count-level m15 buys/sells ratio cannot reconstruct matched identical
    amount pairs or a wallet's net position. Returning None fails open.
    """
    return None


def detect(snapshot, rule=None, logger=None):
    """Return score, veto, exact fired rule, and features; never raise.

    ``rule`` is a key in CANDIDATE_RULES. With no rule, veto is always False.
    Missing/invalid evidence and BSC fail open with one W2 FALLBACK log line.
    Caller must not pass a candidate rule in production without validation.
    """
    log = logger if hasattr(logger, "warning") else LOG
    if not isinstance(snapshot, dict):
        log.warning("W2 FALLBACK: malformed snapshot; no veto")
        return {"risk_score": 0.0, "veto": False, "rule": None, "features": {}, "status": "fallback"}
    if snapshot.get("chain", "solana") != "solana":
        log.warning("W2 FALLBACK: non-Solana snapshot; no veto")
        return {"risk_score": 0.0, "veto": False, "rule": None, "features": {}, "status": "fallback"}

    top1 = _pct(snapshot.get("holder_top1_pct"))
    top5 = _pct(snapshot.get("holder_top5_pct"))
    first10 = _pct(snapshot.get("first10_buyers_pct"))
    launch = _pct(snapshot.get("launch_block_buyers_pct"))
    # An inconsistent pair is unusable as a pair; independent top1 remains usable.
    if top1 is not None and top5 is not None and top5 < top1:
        top5 = None
    spread = top5 - top1 if top1 is not None and top5 is not None else None
    features = {"holder_top1_pct": top1, "holder_top5_pct": top5,
                "top5_minus_top1_pct": spread, "first10_buyers_pct": first10,
                "launch_block_buyers_pct": launch, "wash_score": wash_score()}
    # Fixed scales are descriptive only. No probability or fitted weights.
    components = [v for v in (top1 / 40 if top1 is not None else None,
                               top5 / 75 if top5 is not None else None,
                               spread / 60 if spread is not None else None,
                               first10 / 50 if first10 is not None else None,
                               launch / 30 if launch is not None else None) if v is not None]
    score = min(1.0, max(components)) if components else 0.0

    valid_rule = rule in CANDIDATE_RULES if isinstance(rule, str) else rule is None
    needed = []
    if isinstance(rule, str) and valid_rule:
        if rule.startswith("top1"):
            needed = [top1]
            if "top5" in rule:
                needed.append(top5)
        elif rule.startswith("top5"):
            needed = [top5]
            if "top1" in rule:
                needed.append(top1)
        elif rule.startswith("spread"):
            needed = [spread]
        elif rule.startswith("first10"):
            needed = [first10]
        elif rule.startswith("launch_block"):
            needed = [launch]
    fallback = not components or not valid_rule or (rule is not None and any(v is None for v in needed))
    if fallback:
        log.warning("W2 FALLBACK: missing or invalid W2 evidence/rule; no veto")
    veto = False
    if not fallback and isinstance(rule, str):
        if rule.startswith("top1_gt_"):
            veto = top1 > float(rule.removeprefix("top1_gt_"))
        elif rule.startswith("top5_gt_60_top1_le_20"):
            veto = top5 > 60 and top1 <= 20
        elif rule.startswith("top5_gt_"):
            veto = top5 > float(rule.removeprefix("top5_gt_"))
        elif rule.startswith("spread_gt_"):
            veto = spread > float(rule.removeprefix("spread_gt_"))
        elif rule == "first10_gt_35":
            veto = first10 > 35
        elif rule == "launch_block_gt_20":
            veto = launch > 20
    return {"risk_score": score, "veto": veto,
            "rule": CANDIDATE_RULES[rule] if veto else None,
            "features": features, "status": "fallback" if fallback else "ok"}
