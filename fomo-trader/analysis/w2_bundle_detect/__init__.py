"""Offline, Solana-only ownership concentration research screen."""

from .detector import CANDIDATE_RULES, detect, wash_score

__all__ = ["CANDIDATE_RULES", "detect", "wash_score"]
