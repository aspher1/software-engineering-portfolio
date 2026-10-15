"""Pure, observation-only candidate dump signal. No I/O or side effects."""
import math


def evaluate(price_history, now_ts, price, venue_m5=None,
             drop_pct=8.0, window_sec=30.0, venue_pct=-20.0):
    """Return the first applicable candidate signal, or None.

    History uses the manager's own (timestamp, native-price) deque, including
    the current tick. Venue m5 is only the value already fetched by live code.
    Invalid observations are ignored; malformed inputs never arm a signal.
    """
    try:
        now_ts, price = float(now_ts), float(price)
        drop_pct, window_sec = float(drop_pct), float(window_sec)
        venue_pct = float(venue_pct)
        if not all(map(math.isfinite, (now_ts, price, drop_pct,
                                      window_sec, venue_pct))):
            return None
        if price <= 0 or not 0 < drop_pct < 100 or window_sec <= 0:
            return None
        high = 0.0
        for item in price_history or ():
            try:
                ts, value = float(item[0]), float(item[1])
                if (math.isfinite(ts) and math.isfinite(value)
                        and now_ts - window_sec <= ts <= now_ts
                        and value > high):
                    high = value
            except (TypeError, ValueError, IndexError, KeyError, OverflowError):
                continue
        drop = (high - price) / high * 100 if high else 0.0
        if drop >= drop_pct:
            return {"source": "quote", "drop_pct": round(drop, 4),
                    "threshold_pct": drop_pct, "window_sec": window_sec}
        if venue_m5 is not None:
            venue = float(venue_m5)
            if math.isfinite(venue) and venue <= venue_pct:
                return {"source": "venue_m5", "drop_pct": venue,
                        "threshold_pct": venue_pct,
                        "window_sec": None}
    except (TypeError, ValueError, OverflowError):
        pass
    return None
