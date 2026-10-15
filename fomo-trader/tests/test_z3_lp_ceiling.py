"""Hermetic tests for the Z3 DLMM fee-ceiling model. No network, no I/O."""

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from z3_lp_ceiling.z3_lp_ceiling import (  # noqa: E402
    DEFAULTS,
    TICKETS,
    breakevens,
    ceiling,
    fee_income_upper,
    fixed_costs_lower,
    main,
    rent_exempt_lamports,
    resolve_params,
    run,
)


def test_run_is_deterministic():
    assert json.dumps(run(), sort_keys=True) == json.dumps(run(), sort_keys=True)


def test_rent_formula_matches_solana_constants():
    assert rent_exempt_lamports(0) == 890_880
    assert rent_exempt_lamports(165) == 2_039_280
    assert rent_exempt_lamports(8120) == 57_406_080
    assert rent_exempt_lamports(10136) == 71_437_440


def test_ceiling_monotone_in_ticket_size():
    # Infeasible tickets open nothing (net 0); once feasible, a bigger
    # ticket never lowers the ceiling and never becomes infeasible again.
    for y in (0.0, 0.001, 0.007, 0.05):
        for cut in (0.0, 0.2):
            rows = [ceiling(t, {"fee_yield_per_pool_day": y,
                                "protocol_cut": cut})
                    for t in (1, 7, 7.18, 7.2, 25, 100, 1000)]
            feasible = [r["feasible"] for r in rows]
            assert feasible == sorted(feasible)
            fees = [r["fee_income_upper_usd"] for r in rows]
            assert fees == sorted(fees)
            nets = [r["net_upper_usd"] for r in rows if r["feasible"]]
            assert nets == sorted(nets)


def test_rent_dominates_the_7_dollar_ticket():
    row = ceiling(7.0)
    assert row["valid"] and not row["feasible"]
    assert row["deposit_usd"] == pytest.approx(7.1731848, abs=1e-6)
    assert row["deposit_share_of_ticket"] > 1.0
    assert row["fee_income_upper_usd"] == 0.0
    assert row["net_upper_usd"] == 0.0


def test_headline_verdict_is_killed_with_disclosed_breakevens():
    res = run()
    assert res["verdict"] == "KILLED"
    assert res["verdict_line"].startswith("VERDICT: KILLED")
    be = breakevens()
    assert be["sol_usd_ceiling_infeasible_at_or_above"] == pytest.approx(
        112.1965, abs=0.01)
    assert be["min_ticket_usd_to_clear_zero"] == pytest.approx(7.1842, abs=0.01)


def test_larger_tickets_clear_zero_under_the_absurd_yield():
    by = {(g["ticket_usd"], g["fee_yield_per_pool_day"], g["protocol_cut"]): g
          for g in run()["grid"]}
    assert by[(25.0, 0.007, 0.2)]["net_upper_usd"] == pytest.approx(3.3177, abs=0.01)
    assert by[(100.0, 0.007, 0.2)]["net_upper_usd"] == pytest.approx(17.2842, abs=0.01)
    assert by[(25.0, 0.001, 0.2)]["net_upper_usd"] == pytest.approx(0.4323, abs=0.01)
    assert all(not g["feasible"] for g in run()["grid"] if g["ticket_usd"] == 7.0)


def test_sensitivities_disclose_every_flip():
    sens = {s["label"]: s for s in run()["sensitivities_7usd"]}
    positive = {k for k, s in sens.items() if s["net_upper_usd"] > 0}
    assert positive == {
        "wSOL account treated as not needed (1 token account)",
        "SOL $100 (below every journal SOL price)",
        "SOL $100 and zero protocol cut",
        "HYPOTHETICAL 1-bin position account (392 bytes, unverified)",
    }
    assert max(s["net_upper_usd"] for k, s in sens.items()
               if not k.startswith("HYPOTHETICAL")) < 0.20
    assert sens["HYPOTHETICAL 1-bin position account (392 bytes, unverified)"][
        "net_upper_usd"] == pytest.approx(1.1176, abs=0.01)


@pytest.mark.parametrize("ticket", [None, float("nan"), float("inf"), -7, 0,
                                    "abc", [], {}, True, 1e12])
def test_malformed_tickets_never_raise(ticket):
    row = ceiling(ticket)
    assert row["valid"] is False and row["feasible"] is False
    assert row["net_upper_usd"] == 0.0 and "error" in row


@pytest.mark.parametrize("params", [
    None, "junk", 42, [], {"sol_usd": "x"}, {"sol_usd": float("nan")},
    {"pool_days": -3}, {"pool_days": 2.5}, {"protocol_cut": 1.5},
    {"fee_yield_per_pool_day": None}, {"episodes": 0}, {"nonsense": 1},
    {"token_accounts": True}, {"min_bins": 10**9},
])
def test_corrupt_params_fall_back_explicitly_and_never_raise(params):
    res = run(params)
    assert res["verdict"] in ("KILLED", "CEILING_CLEARS_ZERO")
    if params not in (None,):
        assert res["param_errors"], "fallback must be reported, not silent"
    assert res["assumptions"] == DEFAULTS


def test_fee_income_upper_rejects_bad_capital():
    p, _ = resolve_params()
    for bad in (None, float("nan"), -1, 0, "x"):
        assert fee_income_upper(bad, p) == 0.0


def test_fixed_costs_lower_components():
    p, _ = resolve_params({"uninitialized_bin_arrays": 5, "min_bins": 1})
    c = fixed_costs_lower(p)
    assert c["bin_arrays_spanned"] == 1 and c["new_bin_arrays"] == 1
    assert c["nonrefundable_lamports"] == 15_000 + 71_437_440


def test_verdict_schema():
    res = run()
    assert set(res) == {"schema", "assumptions", "param_errors", "headline",
                        "grid", "sensitivities_7usd", "breakeven",
                        "realism_anchor", "verdict", "verdict_line"}
    assert len(res["grid"]) == len(TICKETS) * 3 * 2
    for row in res["grid"] + res["sensitivities_7usd"] + [res["headline"]]:
        for key in ("ticket_usd", "valid", "feasible", "deployable_usd",
                    "fee_income_upper_usd", "net_upper_usd", "deposit_usd"):
            assert key in row
        assert all(v is None or not isinstance(v, float) or math.isfinite(v)
                   for v in row.values())
    json.dumps(res, allow_nan=False)


def test_main_prints_verdict(capsys):
    assert main([]) == 0
    assert "VERDICT: KILLED" in capsys.readouterr().out
