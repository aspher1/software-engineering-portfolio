"""Rebuild verdict from the local journal; no network access or writes outside W2."""

import json
from pathlib import Path

from analysis import replay
from analysis.w2_bundle_detect import CANDIDATE_RULES

OUT = Path(__file__).with_name("verdict.json")
MIN_ENRICHED = 60  # chronological first 40 IS, last 20 OOS at minimum


def _safe_metrics(trades, config, stress=False):
    result = replay.metrics(trades, config, stress=stress)
    # JSON has no Infinity. Undefined PF means no losing trades.
    if result["pf"] == float("inf"):
        result["pf"] = None
    return result


def build_verdict():
    trades, counts = replay.load_journal()
    config = json.loads(replay.CONFIG.read_text())
    sol = [t for t in trades if t["chain"] == "solana" and t["entry_record"] is not None]
    eligible = [t for t in sol if t["entry_record"].get("holder_top1_pct") is not None
                or t["entry_record"].get("holder_top5_pct") is not None]
    # Replay convention: closed-trade journal append order, avoiding mixed TZ parsing.
    cut = len(sol) * 2 // 3
    base_is, base_oos = sol[:cut], sol[cut:]
    thresholds = []
    for key, expression in CANDIDATE_RULES.items():
        fields = (["first10_buyers_pct"] if key.startswith("first10") else
                  ["launch_block_buyers_pct"] if key.startswith("launch_block") else
                  ["holder_top1_pct", "holder_top5_pct"] if key.startswith("spread") or "top1_le" in key else
                  ["holder_top1_pct"] if key.startswith("top1") else
                  ["holder_top5_pct"])
        n_with_field = sum(all(t["entry_record"].get(field) is not None for field in fields)
                           for t in sol)
        if n_with_field >= MIN_ENRICHED:
            raise RuntimeError("W2 has enough evidence for full IS/OOS validation; extend calibration before writing a verdict")
        thresholds.append({"id": key, "rule": expression, "n_with_required_field": n_with_field,
                           "is": None, "oos": None, "stress_3x_slippage": {"is": None, "oos": None},
                           "status": "untestable: no field data" if n_with_field == 0 else
                                     "untestable: insufficient enriched trades"})
    return {
        "track": "W2 coordinated/bundled ownership detection", "scope": "Solana only",
        "validation": "INCONCLUSIVE", "ship_recommend": False, "ship_rule": None,
        "reason": ("No completed Solana trade has non-null holder concentration evidence; no W2 threshold can be fit or tested."
                   if not eligible else
                   "Entry evidence exists but candidate thresholds have not passed the minimum sample and validation gates."),
        "minimum_enriched_completed_solana_trades": MIN_ENRICHED,
        "minimum_split": {"is": 40, "oos": 20},
        "additional_requirements": ["At least 10 vetoed and 10 kept trades in each split for a candidate rule",
                                    "At least two observed market regimes in each split",
                                    "OOS edge retention >= 60%, OOS decay <= 70%, win rate <= 90%, acceptable P&L under 3x slippage"],
        "journal": str(replay.JOURNAL.relative_to(replay.ROOT)), "journal_counts": counts,
        "completed_solana_with_entry": len(sol), "completed_solana_with_holder_evidence": len(eligible),
        "completed_bsc": sum(t["chain"] == "bsc" for t in trades),
        "bsc_holder_data": "not used; BSC lacks this Solana RPC enrichment and thresholds do not transfer",
        "split_method": "chronological closed-trade journal append order, first 2/3 IS and final 1/3 OOS",
        "baseline": {"is": _safe_metrics(base_is, config), "oos": _safe_metrics(base_oos, config),
                     "stress_3x_slippage": {"is": _safe_metrics(base_is, config, True),
                                            "oos": _safe_metrics(base_oos, config, True)}},
        "thresholds_tested": thresholds,
        "cost_model": "analysis.replay.metrics: journal realized P&L minus estimated fees, priority fees and 0.15% slippage per leg; stress triples only that slippage leg",
        "notes": "Threshold outcomes are null because no entry has the required evidence. Baseline metrics are context, not W2 validation. Score scales and candidate rules are unfitted."
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build_verdict(), indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(OUT)
