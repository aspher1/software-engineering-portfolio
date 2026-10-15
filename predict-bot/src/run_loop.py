"""Main loop: scan → detect → paper-trade → mark → grade → improve.

PAPER-ONLY ENFORCEMENT: the loop instantiates PaperBroker directly and refuses
to start if config says live_trading=true but no live adapter is wired (live
adapters are stubs until implemented with credentials).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import yaml

from .adapters.paper import PaperBroker
from .improve import load_champion_strategy, maybe_improve, strategy_version
from .mispricing import MispricingDetector
from .scanner import KalshiScanner, PolymarketScanner
from .trader import Trader


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_price_fn(poly: PolymarketScanner):
    cache: dict[str, float] = {}

    def price_fn(market_id: str) -> float:
        if market_id not in cache:
            try:
                cache[market_id] = poly.live_price(market_id)
            except Exception:
                cache[market_id] = 0.5
        return cache[market_id]

    # allow the loop to refresh quotes each cycle
    price_fn.refresh = cache.clear  # type: ignore[attr-defined]
    return price_fn


def fetch_resolution_poly(poly: PolymarketScanner):
    def _fetch(market_id: str, side: str):
        try:
            raw = poly.get_market(market_id)
        except Exception:
            return None
        if not raw.get("closed"):
            return None
        # Gamma marks resolved outcomes; fall back to price extremes.
        outcome = str(raw.get("resolution") or raw.get("outcome") or "")
        if outcome:
            return (outcome.upper() == side)
        try:
            import json
            prices = raw.get("outcomePrices")
            if isinstance(prices, str):
                prices = json.loads(prices)
            p = float(prices[0])
            if p >= 0.99:
                return side == "YES"
            if p <= 0.01:
                return side == "NO"
        except (ValueError, TypeError, IndexError):
            pass
        return None

    return _fetch


def run_once(config: dict, data_dir: str) -> dict:
    if config.get("mode", {}).get("live_trading"):
        raise RuntimeError(
            "live_trading=true is set, but no live adapter is implemented. "
            "Implement src/adapters/kalshi_live.py / polymarket_live.py with "
            "credentials before enabling."
        )

    poly = PolymarketScanner(config["venues"]["polymarket"]["gamma_api"])
    kalshi = KalshiScanner(config["venues"]["kalshi"]["api"])
    kalshi.refresh()

    version, strategy = load_champion_strategy(data_dir, config)
    cfg = {**config, "strategy": strategy}

    detector = MispricingDetector(
        threshold=strategy["mispricing_threshold"],
        slippage_haircut=0.01,
    )
    price_fn = build_price_fn(poly)
    broker = PaperBroker(price_fn)
    trader = Trader(cfg, broker, data_dir)

    markets = poly.scan(cfg)
    reference = kalshi.markets() if kalshi.available else []
    signals = detector.detect(markets, reference, strategy_version=version)
    fills = trader.execute(signals)
    broker.mark_to_market()
    graded = trader.grade_resolutions(fetch_resolution_poly(poly))

    report = None
    grades = trader.journal_entries("grade")
    imp_cfg = cfg.get("improvement", {})
    min_trades = int(imp_cfg.get("min_resolved_trades", 10))
    retune_every = int(imp_cfg.get("retune_every_resolved", 10))
    state_path = os.path.join(data_dir, "improve_state.json")
    last_tuned_at = 0
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                last_tuned_at = int(json.load(f).get("tuned_at_resolved", 0))
        except (ValueError, OSError):
            last_tuned_at = 0
    if len(grades) >= min_trades and len(grades) - last_tuned_at >= retune_every:
        report = maybe_improve(cfg, data_dir, grades)
        with open(state_path, "w") as f:
            json.dump({"tuned_at_resolved": len(grades)}, f)

    return {
        "strategy_version": version,
        "markets_scanned": len(markets),
        "kalshi_reference_available": kalshi.available,
        "signals": len(signals),
        "fills": len(fills),
        "graded": len(graded),
        "open_positions": len(broker.get_open_positions()),
        "improvement": report,
        "live": False,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.yaml"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", type=int, default=0,
                    help="run continuously, cycle every N seconds")
    args = ap.parse_args()

    config = load_config(args.config)
    data_dir = os.path.join(os.path.dirname(os.path.abspath(args.config)),
                            "data")

    def cycle():
        summary = run_once(config, data_dir)
        print(
            f"[{summary['strategy_version']}] scanned={summary['markets_scanned']} "
            f"signals={summary['signals']} fills={summary['fills']} "
            f"graded={summary['graded']} open={summary['open_positions']} "
            f"kalshi_ref={summary['kalshi_reference_available']} live=False"
        )
        if summary["improvement"] and summary["improvement"].get("accepted"):
            imp = summary["improvement"]
            print(f"  improved: {imp['from_version']} → {imp['to_version']} "
                  f"({imp['change']}) pnl {imp['base_pnl']} → {imp['new_pnl']}")

    if args.once or args.loop <= 0:
        cycle()
    else:
        while True:
            try:
                cycle()
            except Exception as e:  # never die on a bad cycle
                print(f"cycle error: {e}", file=sys.stderr)
            time.sleep(args.loop)


if __name__ == "__main__":
    main()
