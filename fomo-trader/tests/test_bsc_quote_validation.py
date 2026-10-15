"""Router quote boundary checks; no RPC or key file is used."""

from unittest.mock import Mock

import pytest

from bsc_swap import BscSwap, WBNB


@pytest.mark.parametrize("method,path", [
    ("quote_buy", [WBNB, "token"]),
    ("quote_sell", ["token", WBNB]),
])
def test_valid_direct_quote(method, path):
    swap = object.__new__(BscSwap)
    swap._amounts_out = Mock(return_value=[100, 250])
    assert getattr(swap, method)("token", 100) == 250
    swap._amounts_out.assert_called_once_with(100, path)


@pytest.mark.parametrize("amounts", [
    None, [], [100], [100, 250, 300], [99, 250], [100, 0],
    [100, -1], [100, "250"], [100, True], [float("nan"), 250],
])
def test_malformed_quote_rejected_for_buy_and_sell(amounts):
    swap = object.__new__(BscSwap)
    swap._amounts_out = Mock(return_value=amounts)
    with pytest.raises(ValueError, match="invalid BSC router quote"):
        swap.quote_buy("token", 100)
    with pytest.raises(ValueError, match="invalid BSC router quote"):
        swap.quote_sell("token", 100)


def test_nonpositive_input_rejected_before_router_call():
    swap = object.__new__(BscSwap)
    swap._amounts_out = Mock()
    for method in (swap.quote_buy, swap.quote_sell):
        with pytest.raises(ValueError, match="quote input must be positive"):
            method("token", 0)
    swap._amounts_out.assert_not_called()


def test_malformed_sell_quote_fails_honeypot_check_closed():
    swap = object.__new__(BscSwap)
    swap._amounts_out = Mock(side_effect=[[100, 250], [250]])
    assert swap.honeypot_check("token", "Token", 100) == (
        False, "sell route missing (honeypot)")
