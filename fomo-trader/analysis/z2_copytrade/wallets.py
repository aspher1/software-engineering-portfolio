"""Candidate leads and conservative generalist screening; no invented addresses."""
from dataclasses import dataclass
from math import isfinite
from typing import Any

SOURCE_URL = 'https://github.com/d3ad-e/solana-sniper-bot/blob/HEAD/CLAUDE.md'
GENERALIST_MIN_CREATORS = 100
INSIDER_MAX_CREATORS = 20


@dataclass(frozen=True)
class Wallet:
    address: str | None
    label: str
    source_url: str
    round_trips: int | None = None
    net_sol: float | None = None
    win_rate: float | None = None
    distinct_creators: int | None = None
    window_days: float | None = None
    status: str = 'address_pending_resolution'
    prefix: str = ''


CANDIDATES = (
    Wallet(None, 'target', SOURCE_URL, 4548, 85.4, .29, None, 3, prefix='24678QK'),
    Wallet(None, 'leader', SOURCE_URL, 1006, None, .70, None, 3, prefix='E4EzX'),
    Wallet(None, 'drafter', SOURCE_URL, 754, 960.7, .66, None, 4, prefix='57stAM'),
)


def screen_wallet(stats: Any) -> tuple[bool, list[str]]:
    """Fail closed on absent/invalid evidence; top-3 values must be *net* SOL."""
    get = (lambda k: stats.get(k)) if isinstance(stats, dict) else (lambda k: getattr(stats, k, None))
    reasons = []
    for key, minimum in [('round_trips', 100), ('window_days', 3), ('distinct_creators', GENERALIST_MIN_CREATORS)]:
        value = get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
            reasons.append(f'invalid_{key}')
        elif value < minimum:
            reasons.append(f'{key}_below_{minimum}')
    creators = get('distinct_creators')
    if isinstance(creators, (int, float)) and not isinstance(creators, bool) and isfinite(creators) and 0 <= creators < INSIDER_MAX_CREATORS:
        reasons.append('insider_pattern')
    net = get('net_sol')
    top = get('top3_net_sol')
    if not isinstance(net, (int, float)) or isinstance(net, bool) or not isfinite(net) or net <= 0:
        reasons.append('nonpositive_or_invalid_net')
    if not isinstance(top, (int, float)) or isinstance(top, bool) or not isfinite(top) or top < 0:
        reasons.append('missing_or_invalid_top3_net')
    elif isinstance(net, (int, float)) and isfinite(net) and net > 0 and top > .5 * net:
        reasons.append('top3_over_half_net')
    return not reasons, reasons
