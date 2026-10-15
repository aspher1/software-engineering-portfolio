import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.adapters.paper import PaperBroker
from src.adapters.base import Order, LiveTradingDisabled
from src.trader import Trader, kelly_fraction
from src.mispricing import Signal


def test_kelly_zero_when_no_edge():
    assert kelly_fraction(0.5, 0.6) == 0.0
    assert kelly_fraction(0.6, 0.6) == 0.0


def test_kelly_positive_with_edge():
    f = kelly_fraction(0.7, 0.5)
    assert 0 < f <= 1.0


def _cfg(tmp):
    return {
        "strategy": {
            "mispricing_threshold": 0.08, "kelly_fraction": 0.25,
            "max_risk_per_trade": 0.06, "max_open_positions": 10,
            "daily_trade_cap": 20, "bankroll": 1000.0,
        },
        "improvement": {"min_resolved_trades": 10},
    }


def _signal(**kw):
    d = dict(signal_id="sig-1", market_id="m1", venue="polymarket",
             side="YES", our_price=0.4, fair_price=0.6, gap=0.2,
             expected_edge=0.19, strategy_version="v0")
    d.update(kw)
    return Signal(**d)


def test_paper_fill_applies_slippage_and_journals(tmp_path):
    broker = PaperBroker(lambda mid: 0.40)
    trader = Trader(_cfg(str(tmp_path)), broker, str(tmp_path))
    fills = trader.execute([_signal()])
    assert len(fills) == 1
    assert fills[0]["fill_price"] > 0.40       # slippage against us
    assert fills[0]["live"] is False
    assert len(trader.journal_entries("fill")) == 1


def test_caps_and_dedup(tmp_path):
    broker = PaperBroker(lambda mid: 0.40)
    cfg = _cfg(str(tmp_path))
    cfg["strategy"]["max_open_positions"] = 1
    trader = Trader(cfg, broker, str(tmp_path))
    sigs = [_signal(signal_id=f"sig-{i}", market_id=f"m{i}") for i in range(3)]
    fills = trader.execute(sigs)
    assert len(fills) == 1
    # duplicate market+side skipped even with room
    cfg["strategy"]["max_open_positions"] = 10
    fills2 = trader.execute([_signal(signal_id="sig-9", market_id="m0")])
    assert fills2 == []


def test_grade_and_pnl(tmp_path):
    broker = PaperBroker(lambda mid: 0.40)
    trader = Trader(_cfg(str(tmp_path)), broker, str(tmp_path))
    trader.execute([_signal()])
    graded = trader.grade_resolutions(lambda mid, side: True)
    assert len(graded) == 1 and graded[0]["won"] is True
    assert graded[0]["pnl"] > 0
    assert graded[0]["gap"] == 0.2  # carried from fill for replay


def test_paper_broker_never_live():
    assert PaperBroker(lambda mid: 0.5).is_live is False


def test_live_adapters_refuse_without_opt_in():
    from src.adapters.kalshi_live import KalshiLiveAdapter
    from src.adapters.polymarket_live import PolymarketLiveAdapter
    cfg = {"mode": {"live_trading": False}}
    for cls in (KalshiLiveAdapter, PolymarketLiveAdapter):
        try:
            cls(cfg)
            raise AssertionError(f"{cls.__name__} should refuse")
        except (LiveTradingDisabled, NotImplementedError):
            pass
