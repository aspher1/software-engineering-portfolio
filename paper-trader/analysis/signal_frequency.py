#!/usr/bin/env python3
"""Offline evidence: how often would the crypto entry rule have fired?

PAPER ONLY - read-only public market data, no orders.

Replays 5-minute Coinbase candles for the last DAYS days and evaluates the
entry rule at every 5-minute scan point two ways:
  as-coded (pre-2026-09-27): 1h ref = close of the last completed *clock*
      hour, 4h ref = close three hours before that (so "1h" really spans
      0-60 min depending on minutes past the hour)
  trailing: 1h ref = price exactly 60 min ago, 4h ref = 240 min ago

Run:  python3 analysis/signal_frequency.py [days]
"""
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
SYMS = ["BTC", "ETH", "SOL", "DOGE"]
GRAN = 300
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 14
ENTRY_1H = 2.5


def fetch(sym, days):
    end = int(time.time()) // GRAN * GRAN
    start = end - days * 86400
    out = {}
    t = start
    while t < end:
        t2 = min(t + 299 * GRAN, end)
        r = requests.get(
            "https://api.exchange.coinbase.com/products/%s-USD/candles" % sym,
            params={"granularity": GRAN,
                    "start": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                    "end": datetime.fromtimestamp(t2, timezone.utc).isoformat()},
            headers={"User-Agent": "paper-trader-analysis"}, timeout=20)
        r.raise_for_status()
        for c in r.json():
            out[int(c[0])] = float(c[4])   # candle start -> close
        t = t2
        time.sleep(0.4)
    # key by candle *end* time so closes[t] is the price at time t
    return {k + GRAN: v for k, v in out.items()}


def pct(a, b):
    return (a - b) / b * 100


def main():
    print("window: last %d days, 5m scans, entry +%.1f%%/1h, 4h >= 0\n"
          % (DAYS, ENTRY_1H))
    print("%-5s %6s | %8s %8s %8s | %8s %8s %8s"
          % ("sym", "scans", "coded", "p99", "max",
             "trail", "p99", "max"))
    for sym in SYMS:
        closes = fetch(sym, DAYS)
        coded, trail = [], []
        hits_c, hits_t, days_c, days_t = 0, 0, set(), set()
        for t in sorted(closes):
            px = closes[t]
            hour = t // 3600 * 3600
            ref1, ref4 = closes.get(hour), closes.get(hour - 3 * 3600)
            if ref1 and ref4:
                a, b = pct(px, ref1), pct(px, ref4)
                coded.append(a)
                if a >= ENTRY_1H and b >= 0:
                    hits_c += 1
                    days_c.add(datetime.fromtimestamp(t, ET).date())
            r1, r4 = closes.get(t - 3600), closes.get(t - 4 * 3600)
            if r1 and r4:
                a, b = pct(px, r1), pct(px, r4)
                trail.append(a)
                if a >= ENTRY_1H and b >= 0:
                    hits_t += 1
                    days_t.add(datetime.fromtimestamp(t, ET).date())
        coded.sort()
        trail.sort()
        q = lambda xs, p: xs[int(p * (len(xs) - 1))] if xs else float("nan")
        print("%-5s %6d | %8d %+7.2f%% %+7.2f%% | %8d %+7.2f%% %+7.2f%%"
              "   (signal days: coded %d, trailing %d)"
              % (sym, len(trail), hits_c, q(coded, .99), coded[-1],
                 hits_t, q(trail, .99), trail[-1], len(days_c), len(days_t)))


if __name__ == "__main__":
    main()
