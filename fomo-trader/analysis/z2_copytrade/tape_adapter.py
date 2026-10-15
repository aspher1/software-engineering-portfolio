"""Offline adapter for the staged, wallet-filtered pumpapi replay JSONL.

Verified buy/sell row schema (replay.pumpapi.io, 2026-08 tape):

    txSigner     transaction actor (fee payer)
    transfers    [{from, to, amount, mint, ...}] on some rows
    action       buy | sell; transfer/create/migrate are not trades
    mint         token mint
    price        SOL per token, post-trade bonding-curve spot
                 (~= vQuoteInBondingCurve / vTokensInBondingCurve)
    quoteAmount  swap size in SOL
    timestamp    Unix milliseconds
    signature    base58 transaction signature
    block        slot

Prices use ``price``, not the effective average ``quoteAmount / tokenAmount``:
the spot after a print is what the next taker (the copier) faces, and it is
the quantity reported for every print, including prints by other wallets.
Creator fields are not published in this schema and are carried as null.
"""
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime
import base64
import json
from math import isfinite
from pathlib import Path

from .prices import PricePath
from .sources import LeaderTrade, WalletDataSource, valid_trade

KNOWN_WALLETS = (
    '24678QKx2Dy8ZCw6Ra8o9DeTqPLL5GR9ZQKxt5FddHmq',
    'E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz',
    '57stAMFvwctAjkBS76RXGoK4QKyS1QoxbGMbzFFe4DyZ',
)
TRADE_ACTIONS = {'buy': 'BUY', 'sell': 'SELL'}


def _number(value):
    if isinstance(value, bool):
        raise ValueError('boolean number')
    number = float(value)
    if not isfinite(number) or number <= 0:
        raise ValueError('nonpositive number')
    return number


def _signature(value):
    """Canonicalize full base58/64-byte base64/hex signatures; reject abbreviations."""
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError('missing signature')
    if '…' in value or '...' in value:
        raise ValueError('truncated signature')
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    if 80 <= len(value) <= 90 and all(c in alphabet for c in value):
        return value
    try:
        raw = bytes.fromhex(value) if len(value) == 128 else base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError('unrecognized signature') from None
    if len(raw) != 64:
        raise ValueError('signature is not 64 bytes')
    integer = int.from_bytes(raw, 'big')
    chars = ''
    while integer:
        integer, digit = divmod(integer, 58)
        chars = alphabet[digit] + chars
    return '1' * (64 - len(raw.lstrip(b'\0'))) + chars


def _timestamp(value):
    if isinstance(value, str) and ('T' in value or '-' in value):
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    ts = _number(value)
    return ts / 1000 if ts > 1e12 else ts


def attributed_wallet(row, wallets=KNOWN_WALLETS):
    """txSigner when it is a known wallet, else the first known transfers[].from/to party."""
    signer = row.get('txSigner')
    if isinstance(signer, str) and signer in wallets:
        return signer
    transfers = row.get('transfers')
    for transfer in transfers if isinstance(transfers, list) else ():
        if isinstance(transfer, dict):
            for key in ('from', 'to'):
                party = transfer.get(key)
                if isinstance(party, str) and party in wallets:
                    return party
    return None


def parse_trade(row, wallet, side):
    """Map one buy/sell row to a LeaderTrade; raise ValueError/TypeError when incomplete."""
    slot = row.get('block')
    trade = LeaderTrade(wallet, str(row.get('mint') or ''), side,
                        _timestamp(row.get('timestamp')),
                        _number(row.get('price')), _number(row.get('quoteAmount')),
                        _signature(row.get('signature')),
                        int(slot) if slot is not None and not isinstance(slot, bool) else None)
    if not valid_trade(trade):
        raise ValueError('invalid trade')
    return trade


def iter_rows(paths, counts):
    """Yield (path, row) for JSON-object lines; a partially written final line is counted."""
    for path in paths:
        try:
            with path.open(encoding='utf-8', errors='replace') as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        counts['incomplete_or_invalid_json'] += 1
                        continue
                    counts['rows_seen'] += 1
                    if not isinstance(row, dict):
                        counts['invalid_object'] += 1
                        continue
                    yield path, row
        except OSError:
            counts['unreadable_files'] += 1


class FilteredTapeSource(WalletDataSource):
    """Re-scan <day>/<hour>.jsonl files on every load.

    ``include`` optionally restricts the scan to relative paths such as
    ``2026-08-13/00.jsonl`` so an hour still being written can be excluded.
    ``market_prints`` holds every complete buy/sell print, attributed or not,
    for the price path; ``trades`` holds only attributed leader trades.
    """

    def __init__(self, root, wallets=KNOWN_WALLETS, include=None):
        self.root = Path(root)
        self.wallets = frozenset(wallets)
        self.include = None if include is None else frozenset(include)
        self.counts = Counter()
        self.rows_by_file = Counter()
        self.files = []
        self.trades = []
        self.market_prints = []
        self.all_sigs = {}
        self.creator_by_wallet = {}  # unpublished in the staged schema; null-carried

    def hour_files(self):
        if not self.root.exists():
            return []
        files = sorted(self.root.rglob('*.jsonl'))
        if self.include is not None:
            files = [p for p in files if p.relative_to(self.root).as_posix() in self.include]
        return files

    def scan(self):
        self.counts = Counter()
        self.rows_by_file = Counter()
        self.files = self.hour_files()
        all_sigs = defaultdict(dict)
        rows, prints = [], []
        for path, row in iter_rows(self.files, self.counts):
            self.rows_by_file[path.relative_to(self.root).as_posix()] += 1
            wallet = attributed_wallet(row, self.wallets)
            if wallet is not None:
                try:
                    all_sigs[wallet][_signature(row.get('signature'))] = _timestamp(row.get('timestamp'))
                except (ValueError, TypeError, OverflowError):
                    self.counts['unparseable_signature_or_timestamp'] += 1
            action = str(row.get('action') or '').lower()
            side = TRADE_ACTIONS.get(action)
            if side is None:
                self.counts['non_trade_action'] += 1
                self.counts[f'non_trade_action:{action or "missing"}'] += 1
                continue
            try:
                trade = parse_trade(row, wallet or str(row.get('txSigner') or 'unattributed'), side)
            except (ValueError, TypeError, OverflowError):
                self.counts['incomplete_trade'] += 1
                continue
            prints.append(trade)
            if wallet is None:
                self.counts['unattributed_trade_row'] += 1
                continue
            rows.append(trade)
        rows.sort(key=lambda t: (t.ts, t.tx_sig, t.wallet, t.mint, t.side))
        seen = set()
        self.trades = []
        for trade in rows:
            # The opportunity builder de-duplicates by transaction signature.
            # Keep one action per wallet/signature, deterministically first.
            key = (trade.wallet, trade.tx_sig)
            if key in seen:
                self.counts['duplicate_wallet_signature'] += 1
                continue
            seen.add(key)
            self.trades.append(trade)
        self.market_prints = sorted(prints, key=lambda t: (t.ts, t.tx_sig, t.mint, t.side))
        self.all_sigs = dict(all_sigs)
        self.counts['valid_trades'] = len(self.trades)
        self.counts['market_prints'] = len(self.market_prints)
        return self.trades

    def get_trades(self, address, since, until):
        self.scan()
        return [t for t in self.trades if t.wallet == address and since <= t.ts <= until]


class TapePrintPricePath(PricePath):
    """First staged print for the same mint at/after decision time, within ``max_wait_s``.

    The 60 s cap matches the GeckoTerminal minute-close rule; a later print is
    not a fill at the decision time and the opportunity is skipped and counted.
    """

    def __init__(self, trades, max_wait_s=60):
        grouped = defaultdict(list)
        for trade in trades:
            if valid_trade(trade):
                grouped[trade.mint].append((trade.ts, trade.tx_sig, trade.price_sol))
        self.data = {}
        for mint, prints in grouped.items():
            prints.sort()
            self.data[mint] = ([p[0] for p in prints], [p[2] for p in prints])
        self.max_wait_s = max_wait_s
        self.missing = 0

    def price_at(self, mint, decision_ts):
        if not isinstance(decision_ts, (int, float)) or not isfinite(decision_ts):
            self.missing += 1
            return None
        times, prices = self.data.get(mint, ([], []))
        index = bisect_left(times, decision_ts)
        if index == len(times) or times[index] - decision_ts > self.max_wait_s:
            self.missing += 1
            return None
        return prices[index]
