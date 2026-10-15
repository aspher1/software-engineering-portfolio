import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scanner import Market, normalize_title, PolymarketScanner
from src.mispricing import MispricingDetector, jaccard


def test_normalize_title_drops_filler():
    a = normalize_title("Will the Fed cut rates by December?")
    b = normalize_title("Fed cut rates December")
    assert jaccard(a, b) > 0.5, (a, b)


def test_to_market_parses_gamma_format():
    raw = {
        "id": "123", "question": "Will X happen?",
        "outcomes": ["Yes", "No"],
        "outcomePrices": '["0.62", "0.38"]',
        "volume": "50000", "liquidity": "12000",
        "category": "Politics", "closed": False,
    }
    m = PolymarketScanner.to_market(raw)
    assert m is not None
    assert m.yes_price == 0.62
    assert m.volume_usd == 50000


def test_to_market_rejects_near_certain():
    raw = {"id": "1", "question": "q", "outcomes": ["Yes", "No"],
           "outcomePrices": '["0.995", "0.005"]'}
    assert PolymarketScanner.to_market(raw) is None


def test_to_market_rejects_non_binary():
    raw = {"id": "2", "question": "q", "outcomes": ["A", "B", "C"],
           "outcomePrices": '["0.3", "0.3", "0.4"]'}
    assert PolymarketScanner.to_market(raw) is None


def test_detector_flags_gap_and_picks_cheaper_side():
    det = MispricingDetector(threshold=0.08)
    poly = [Market("polymarket", "p1", "Federal Reserve cut interest rates December", 0.40)]
    kal = [Market("kalshi", "k1", "Federal Reserve cut interest rates December meeting", 0.55)]
    sigs = det.detect(poly, kal)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.side == "YES"          # our venue cheaper → buy YES
    assert abs(s.gap - 0.15) < 1e-9


def test_detector_skips_unmatched_and_small_gaps():
    det = MispricingDetector(threshold=0.08)
    poly = [Market("polymarket", "p1", "Completely unrelated obscure event xyz", 0.40)]
    kal = [Market("kalshi", "k1", "Federal Reserve cut interest rates", 0.55)]
    assert det.detect(poly, kal) == []
    poly2 = [Market("polymarket", "p2", "Federal Reserve cut interest rates December", 0.52)]
    assert det.detect(poly2, kal) == []  # gap 0.03 < threshold
