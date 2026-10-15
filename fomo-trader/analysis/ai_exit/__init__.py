"""AI exit-manager prototype for the FOMO Trader memecoin bot.

SHADOW-ONLY. Nothing here is wired into fomo_trader.py, the manage loop,
entries, position sizing, kill switches, or the rug guard. The deterministic
exits are the FLOOR: this package can only ever recommend an EARLIER exit,
never delay, override, or weaken one.

Submodules:
  advisor      - framework: ExitAdvice, advisor interface, the floor gatekeeper
  local_policy - fast local explainable policy (default backend, <50ms budget)
  llm_advisor  - network-LLM advisory backend (default OFF, never hot path)
  shadow_eval  - offline evaluator: deterministic baseline vs advisor on
                 synthetic + (when available) real Z4 quote paths
"""
