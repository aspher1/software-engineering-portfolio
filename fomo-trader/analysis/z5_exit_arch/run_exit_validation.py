"""Run the exit validation when 30+ qualifying paper positions exist.

Leg 1 (E3, frozen pre-registration): run_e3() over qualifying positions with
the four variants (control ladder + ratchet + time_box + vol_scaled).

Leg 2 (AI exit advisor): walk each position's marks chronologically with the
LocalExitPolicy. The deterministic control's exits are the floor (replayed via
the E3 control simulation); the advisor may only ADD an earlier full exit.
Both legs are accounted with the same E3 CostModel. Kill criterion
(AI_EXIT_PREREGISTRATION.md): advisor net <= control net at >=30 closes -> KILL.

Output: JSON to stdout AND analysis/z5_exit_arch/validation_results_<ts>.json
{"status": "awaiting_data"|"verdict", "qualifying": N, ...}

Marks are not fills: every number is a mark-implied bound (frozen honesty clause).
"""
import json
import os
import sys
from datetime import datetime, timezone

from analysis.z5_exit_arch.e3 import (
    VARIANTS, CONTROL, CostModel, run_e3, simulate_variant, account, Position,
)
from analysis.z5_exit_arch.positions_from_journal import build_positions
from analysis.ai_exit.advisor import (
    ExitAdvisor, PositionState, QuoteTick, combine_with_deterministic,
    EXIT as ADVISOR_EXIT,
)
from analysis.ai_exit.local_policy import LocalExitPolicy

PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUN_DIR = os.path.join(PROJ, 'runs', 'paper-1h')
OUT_DIR = os.path.join(PROJ, 'analysis', 'z5_exit_arch')
TRIGGER = 30
NOTIONAL = 10.0


def _recent_sol_usd():
    vals = []
    with open(os.path.join(RUN_DIR, 'trades.jsonl')) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            v = r.get('sol_usd')
            if isinstance(v, (int, float)) and v > 0:
                vals.append(v)
    vals = vals[-200:]
    if not vals:
        raise RuntimeError('no sol_usd in journal')
    vals.sort()
    return vals[len(vals) // 2]


def _advisor_net(position: Position, costs: CostModel, advisor: ExitAdvisor):
    """Advisor+floor vs control on one position. Returns (advisor_net, control_net)."""
    marks = position.marks
    # Control replay for the floor schedule.
    control_rep = simulate_variant(marks, position.entry_price_usd,
                                   marks[0].t, NOTIONAL, VARIANTS[CONTROL],
                                   costs, CONTROL)
    control_exit_ts = {e.ts for e in control_rep.exits}
    first_control_exit = min(control_exit_ts) if control_exit_ts else None

    advisor_exits = []
    ticks = sorted(((m.t, m.price_usd) for m in marks), key=lambda x: x[0])
    peak = position.entry_price_usd
    for t, price in ticks:
        if price is None or price <= 0:
            continue
        peak = max(peak, price)
        if first_control_exit is not None and t >= first_control_exit:
            break  # floor fired; advisor cannot act after
        pos_state = PositionState(
            mint=position.mint, chain=position.chain,
            entry_price=position.entry_price_usd, peak_price=peak,
            current_price=price, opened_ts=ticks[0][0], now_ts=t,
            rungs_fired=0, exit_cfg={},
        )
        quotes = [QuoteTick(ts=tt, price_usd=pp) for tt, pp in ticks]
        try:
            advice = advisor.advise(pos_state, quotes, as_of_ts=t)
        except Exception:
            continue  # fail-closed: treat as HOLD
        if advice.decision == ADVISOR_EXIT:
            advisor_exits.append((t, price, 'ai-advisor(%s): %s' % (advice.backend, advice.reason)))
            break
    # Account the advisor leg: exits are (ts, mark, fraction, reason).
    from analysis.z5_exit_arch.e3 import Exit as _Exit
    exits = [_Exit(ts=t, mark=p, fraction=1.0, reason=r) for t, p, r in advisor_exits]
    if not exits:
        # Advisor never fired: mark out at last tick (censored, same as control).
        t, p = ticks[-1]
        exits = [_Exit(ts=t, mark=p, fraction=1.0, reason='path_end')]
    gross, _breakdown, net = account(exits, position.entry_price_usd, NOTIONAL, costs)
    return net, control_rep.net_usd


def main():
    qualifying, disqualified = build_positions()
    result = {
        'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'qualifying': len(qualifying),
        'required': TRIGGER,
        'disqualified': disqualified,
    }
    if len(qualifying) < TRIGGER:
        result['status'] = 'awaiting_data'
        print(json.dumps(result, indent=1))
        return 0

    sol_usd = _recent_sol_usd()
    costs = CostModel(sol_usd=sol_usd)
    result['sol_usd'] = sol_usd
    result['status'] = 'verdict'

    # Leg 1: E3 frozen evaluation.
    e3 = run_e3(qualifying, VARIANTS, costs)
    result['e3'] = e3.to_dict()

    # Leg 2: AI advisor vs control.
    advisor = LocalExitPolicy()
    adv_total, ctl_total, adv_wins = 0.0, 0.0, 0
    per_position = []
    for p in qualifying:
        try:
            a_net, c_net = _advisor_net(p, costs, advisor)
        except Exception as ex:  # never let one bad position kill the run
            per_position.append({'mint': p.mint[:8], 'error': type(ex).__name__})
            continue
        adv_total += a_net
        ctl_total += c_net
        adv_wins += 1 if a_net > c_net else 0
        per_position.append({'mint': p.mint[:8], 'advisor_net': round(a_net, 2),
                             'control_net': round(c_net, 2)})
    result['advisor'] = {
        'backend': 'local_policy',
        'total_net_usd': round(adv_total, 2),
        'control_total_net_usd': round(ctl_total, 2),
        'delta_vs_control_usd': round(adv_total - ctl_total, 2),
        'wins_vs_control': adv_wins,
        'n': len(per_position),
        'kill': adv_total <= ctl_total,  # pre-registered kill criterion
    }
    result['positions'] = per_position

    out_path = os.path.join(OUT_DIR, 'validation_results_%s.json'
                            % datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    with open(out_path, 'w') as fh:
        json.dump(result, fh, indent=1)
    result['results_path'] = out_path
    print(json.dumps(result, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
