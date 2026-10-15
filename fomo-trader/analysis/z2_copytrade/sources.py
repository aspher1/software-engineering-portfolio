"""All wallet-trade I/O lives here. Solscan's legacy public endpoint is best effort."""
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
import json
from math import isfinite
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class LeaderTrade:
    wallet: str
    mint: str
    side: str
    ts: float
    price_sol: float
    size_sol: float
    tx_sig: str
    slot: int | None = None


def valid_trade(t):
    try:
        return (bool(t.wallet and t.mint and t.tx_sig) and t.side in ('BUY', 'SELL')
                and all(isinstance(v, (int, float)) and isfinite(v) and v > 0
                        for v in (t.ts, t.price_sol, t.size_sol)))
    except AttributeError:
        return False


class WalletDataSource(ABC):
    @abstractmethod
    def get_trades(self, address: str, since: float, until: float) -> list[LeaderTrade]: ...


class TokenBucket:
    def __init__(self, per_minute=10, clock=time.monotonic, sleep=time.sleep):
        self.rate = min(float(per_minute), 20) / 60
        self.capacity = min(float(per_minute), 20)
        self.tokens = self.capacity
        self.last = clock()
        self.clock, self.sleep = clock, sleep

    def acquire(self):
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + max(0, now - self.last) * self.rate)
        self.last = now
        if self.tokens < 1:
            wait = (1 - self.tokens) / self.rate
            self.sleep(wait)
            self.last = self.clock()
            self.tokens = 0
        else:
            self.tokens -= 1


class SolscanSource(WalletDataSource):
    """Legacy no-key API. It may be unavailable or omit swap economics; incomplete rows skip.

    /account/transactions is paginated and may not expose full historical swaps.
    Never treat its output alone as complete vetting evidence.
    """
    def __init__(self, requester=None):
        self.requester = requester or _get_json
        self.bucket = TokenBucket(10)
        self.skipped = 0
        self.errors = 0
        self.circuit_until = 0.0

    def get_trades(self, address, since, until):
        if not address or time.monotonic() < self.circuit_until:
            self.errors += 1
            return []
        out = []
        for offset in range(0, 10000, 50):
            self.bucket.acquire()
            url = 'https://public-api.solscan.io/account/transactions?' + urlencode({'account': address, 'limit': 50, 'offset': offset})
            try:
                page = self.requester(url)
            except Exception:
                self.errors += 1
                self.circuit_until = time.monotonic() + 60
                break
            if not isinstance(page, list):
                self.errors += 1
                break
            if not page:
                break
            for row in page:
                try:
                    t = _parse_trade(row, address)
                    if valid_trade(t) and since <= t.ts <= until:
                        out.append(t)
                    else:
                        self.skipped += 1
                except (TypeError, ValueError, KeyError, AttributeError):
                    self.skipped += 1
            if len(page) < 50:
                break
        return sorted(out, key=lambda t: (t.ts, t.tx_sig))


def _get_json(url):
    req = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'fomo-copytrade-validation/1'})
    with urlopen(req, timeout=15) as response:
        return json.load(response)


def _parse_trade(row, address):
    """Only accepts explicit swap fields; never infers a trade from a transfer."""
    if not row.get('tx_sig', row.get('txHash')) or not row.get('mint'):
        raise ValueError('missing signature')
    return LeaderTrade(address, str(row['mint']), str(row['side']).upper(),
                       float(row.get('ts', row.get('blockTime'))), float(row['price_sol']),
                       float(row['size_sol']), str(row.get('tx_sig', row.get('txHash'))),
                       int(row['slot']) if row.get('slot') is not None else None)


class TapeSource(WalletDataSource):
    """Read hourly pumpapi-style JSONL files; normalized swap fields required.

    Accepts a directory of *.jsonl or one file. Raw event schemas vary; callers
    should normalize mint, side, timestamp, SOL price/size, and signature first.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.skipped = 0

    def get_trades(self, address, since, until):
        files = sorted(self.path.glob('*.jsonl')) if self.path.is_dir() else [self.path]
        out = []
        for file in files:
            try:
                stream = file.open()
            except OSError:
                self.skipped += 1
                continue
            with stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        wallet = str(row.get('wallet', row.get('trader', '')))
                        if wallet != address:
                            continue
                        t = _parse_trade(row, address)
                        if valid_trade(t) and since <= t.ts <= until:
                            out.append(t)
                        else:
                            self.skipped += 1
                    except (ValueError, TypeError, KeyError, AttributeError):
                        self.skipped += 1
        return sorted(out, key=lambda t: (t.ts, t.tx_sig))


class CachedSource(WalletDataSource):
    def __init__(self, source, cache_dir=None):
        self.source = source
        self.cache_dir = Path(cache_dir or Path(__file__).parent / 'cache')

    def get_trades(self, address, since, until):
        import hashlib
        key = hashlib.sha256(f'{address}|{since}|{until}'.encode()).hexdigest()
        path = self.cache_dir / f'{key}.jsonl'
        if path.exists():
            out = []
            with path.open() as stream:
                for line in stream:
                    try:
                        t = LeaderTrade(**json.loads(line))
                        if valid_trade(t): out.append(t)
                    except (ValueError, TypeError, KeyError): pass
            return out
        out = self.source.get_trades(address, since, until)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        with tmp.open('w') as stream:
            for t in out:
                stream.write(json.dumps(asdict(t), sort_keys=True) + '\n')
        tmp.replace(path)
        return out
