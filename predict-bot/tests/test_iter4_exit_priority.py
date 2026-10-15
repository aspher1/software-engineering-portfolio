import same_day_loop as sdl


def test_equal_edge_exits_prioritize_less_time_left(monkeypatch):
    now = 1_000_000_000
    monkeypatch.setattr(sdl, "fair_15m", lambda *args: (0.55, 100, 100, 0.001, 5, now))
    alerted = {}
    live = {}
    for ticker, minutes in (("KXBTC15M-LATER", 10), ("KXBTC15M-SOONER", 4)):
        close = now + minutes * 60
        alerted[ticker + ":YES"] = {
            "ticker": ticker, "side": "YES", "edge": 0.1,
            "market_mid": 0.5, "window_open_utc": now - 300,
            "window_close_utc": close,
        }
        live[ticker] = (
            {"ticker": ticker, "yes_bid_dollars": 0.595,
             "yes_ask_dollars": 0.605},
            "BTC", now - 300, close,
        )

    exits = sdl.check_exits(alerted, live, now, mark=False)

    assert [exit_["ticker"] for exit_ in exits] == [
        "KXBTC15M-SOONER", "KXBTC15M-LATER"
    ]
