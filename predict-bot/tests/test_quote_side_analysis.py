import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from quote_side_analysis import paper_fill, summarize
from same_day_loop import _kalshi_taker_fee_dollars
from same_day_loop import executable_edge_shadow, EDGE_THRESHOLD


def grade(side="YES", actual="YES", **updates):
    row = {"key": "market:" + side, "side": side, "actual": actual,
           "market_mid": .50, "bid": .48, "ask": .52}
    row.update(updates)
    return row


def test_yes_taker_buy_ask_and_sell_bid_with_both_fees():
    row = paper_fill(grade(), {"exit_price_taken": .60, "exit_yes_bid": .58,
                                    "exit_yes_ask": .62, "exit_bid_taken": .58})
    assert row["entry_executable_taken"] == .52
    assert row["exit_executable_taken"] == .58
    assert row["executable_pnl_net"] == round(.58 - .52 -
        _kalshi_taker_fee_dollars(1, .52) - _kalshi_taker_fee_dollars(1, .58), 6)
    assert row["mid_pnl_gross"] == .10


def test_no_taker_buy_one_minus_yes_bid_sell_one_minus_yes_ask():
    row = paper_fill(grade("NO"), {"exit_price_taken": .60,
        "exit_yes_bid": .38, "exit_yes_ask": .42, "exit_bid_taken": .58})
    assert row["entry_executable_taken"] == .52
    assert row["exit_executable_taken"] == .58
    assert row["executable_pnl_net"] == round(.58 - .52 -
        _kalshi_taker_fee_dollars(1, .52) - _kalshi_taker_fee_dollars(1, .58), 6)


def test_settlement_has_only_entry_fee():
    yes = paper_fill(grade())
    no = paper_fill(grade("NO", "YES"))
    assert yes["executable_pnl_net"] == round(1 - .52 - _kalshi_taker_fee_dollars(1, .52), 6)
    assert no["executable_pnl_net"] == round(-.52 - _kalshi_taker_fee_dollars(1, .52), 6)


def test_missing_quotes_unavailable_without_mid_imputation():
    row = paper_fill(grade(bid=None))
    assert row["mid_pnl_net"] is not None
    assert row["executable_pnl_net"] is None
    assert summarize([row])["unavailable_rows"] == 1
    row = paper_fill(grade(), {"exit_price_taken": .6})
    assert row["executable_pnl_net"] is None


def test_executable_edge_shadow_is_side_aware_and_conservative():
    yes = executable_edge_shadow(.07, .48, .52)
    no = executable_edge_shadow(-.07, .48, .52)
    expected = round(.07 - .02 - _kalshi_taker_fee_dollars(1, .52), 6)
    assert yes["executable_edge"] == no["executable_edge"] == expected
    assert yes["gate_executable_edge_pass"] == (expected >= EDGE_THRESHOLD)
    assert executable_edge_shadow(.1, 0, .52)["gate_executable_edge_pass"] is None
