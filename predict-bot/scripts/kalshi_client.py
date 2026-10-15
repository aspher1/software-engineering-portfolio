"""Public GETs only; shared host pacing across scanner processes.

The /tmp coordination file is transport bookkeeping, never scanner state.
A one-token bucket spaces attempts by two seconds (30/minute sustained).
"""
import fcntl
import os
import random
import tempfile
import time

import requests


class RateLimiter:
    def __init__(self, path=None, interval=2.0, clock=time.monotonic,
                 sleep=time.sleep):
        self.path = path or os.path.join(
            tempfile.gettempdir(), f"predict-bot-kalshi-{os.getuid()}.rate")
        self.interval = interval
        self.clock = clock
        self.sleep = sleep

    def acquire(self):
        # Keep the lock across the short pacing wait: no reservation backlog
        # survives a killed process, and concurrent threads/processes serialize.
        with open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                try:
                    previous = float(f.read())
                except ValueError:
                    previous = -self.interval
                now = self.clock()
                delay = max(0, min(self.interval, previous + self.interval - now))
                if delay:
                    self.sleep(delay)
                f.seek(0)
                f.truncate()
                f.write(str(self.clock()))
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


limiter = RateLimiter()
stats = {"attempts": 0, "retries": 0}


def kalshi_get(url, **kwargs):
    """Retry 429/5xx at most three times, with bounded exponential jitter."""
    for attempt in range(4):
        limiter.acquire()
        stats["attempts"] += 1
        response = requests.get(url, **kwargs)
        status = getattr(response, "status_code", 200)
        if (status == 429 or 500 <= status < 600) and attempt < 3:
            response.close()
            stats["retries"] += 1
            time.sleep(2 ** attempt + random.uniform(0, 0.5))
            continue
        response.raise_for_status()
        return response
