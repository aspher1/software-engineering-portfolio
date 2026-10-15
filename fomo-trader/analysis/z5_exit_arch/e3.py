"""E3 exit-architecture harness: offline, hermetic, paper only. No bot imports.

Frozen 2026-09-27; the specification is `E3_PREREGISTRATION.md`. Every number
this module produces is a mark-implied bound. Marks are not fills.

Input: Z4 quote paths (`<runs>/quote_paths/<sanitized mint>.jsonl`), one JSON
object per line with exactly `ts`, `mint`, `chain`, `price_usd`, `liquidity_usd`.
Only `ts`, `mint`, `chain` and `price_usd` are read. `liquidity_usd` is
documented as null and is never used; undocumented extra fields are ignored and
counted. Bad lines are skipped and counted; the loader never raises on bad data.

Timestamps are local journal strings ("2026-09-24 07:56:11"). They are converted
to naive local seconds; only differences are meaningful, and a DST transition
inside a path would distort one interval by an hour.

Cost model defaults (`CostModel`), mirroring E1:
- notional_usd 10.0 per position (research normalization, equal footing)
- venue_fee_bps 30 per side
- entry_slippage_bps 100
- exit_slippage_bps 500 (config exit.slippage_bps)
- priority_fee_lamports 2,000,000 per transaction: the entry and every exit
  sell, including partials. Converted with `sol_usd`, which is required and has
  no default. Applied to both chains as a conservative proxy for BSC gas.
Net = gross exit proceeds - notional - entry venue/slippage - exit
venue/slippage on gross proceeds - priority fees.
"""
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
import hashlib
import json
from math import inf, isfinite, log, sqrt

SEED = 20260927
FREEZE_DATE = '2026-09-27'
SCHEMA_FIELDS = ('ts', 'mint', 'chain', 'price_usd', 'liquidity_usd')
CHAINS = ('solana', 'bsc')
TS_FORMAT = '%Y-%m-%d %H:%M:%S'
_EPOCH = datetime(1970, 1, 1)
_EPS = 1e-12

MIN_MARKS = 20
DATA_TRIGGER_CLOSES = 30
MIN_IS_CLOSES = 20
MAX_CENSORED_SHARE = 0.50
MIN_OOS_CLOSES = 30
STOP_ALL_USD = -21.0
RETENTION_GATE = 0.60
STRESS_MULTIPLIER = 3.0
DEGENERATE_REGIME_SHARE = 0.20

CONTROL = 'control'
AWAITING_DATA = 'awaiting data'
KILL_VARIANT = 'KILL_VARIANT'
CONTINUE = 'CONTINUE'
INSUFFICIENT_OOS_HISTORY = 'INSUFFICIENT_OOS_HISTORY'
PATH_END = 'path_end'


# ---------------------------------------------------------------- loading

@dataclass(frozen=True)
class QuoteRow:
    ts: datetime
    t: float  # naive local seconds
    mint: str
    chain: str
    price_usd: float


def parse_ts(value):
    """Local journal timestamp string -> naive datetime, or None."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.strptime(value, TS_FORMAT)
    except ValueError:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    return dt if dt.tzinfo is None else None


def ts_seconds(value):
    """datetime, journal string or number -> naive local seconds (or None)."""
    if isinstance(value, str):
        value = parse_ts(value)
    if isinstance(value, datetime):
        return None if value.tzinfo else (value - _EPOCH).total_seconds()
    if _finite(value):
        return float(value)
    return None


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v)


def _valid_price(v):
    return _finite(v) and v > 0


def _new_stats():
    keys = ('lines', 'rows_ok', 'blank', 'malformed_json', 'not_object', 'bad_ts',
            'bad_mint', 'mint_mismatch', 'bad_chain', 'missing_price', 'null_price',
            'invalid_price', 'out_of_order', 'extra_fields_ignored', 'file_error')
    return dict.fromkeys(keys, 0)


def load_quote_path(path, expected_mint=None):
    """Parse one Z4 quote-path JSONL file. Returns (rows, stats); never raises.

    Rows keep file order; a row whose ts precedes the last kept row is dropped
    (fail-closed, never reordered). Only priced rows are returned.
    """
    stats = _new_stats()
    rows = []
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            lines = fh.read().splitlines()
    except (OSError, ValueError, TypeError):
        stats['file_error'] = 1
        return rows, stats
    last = None
    for line in lines:
        stats['lines'] += 1
        if not line.strip():
            stats['blank'] += 1
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            stats['malformed_json'] += 1
            continue
        if not isinstance(obj, dict):
            stats['not_object'] += 1
            continue
        if set(obj) - set(SCHEMA_FIELDS):
            stats['extra_fields_ignored'] += 1
        dt = parse_ts(obj.get('ts'))
        mint, chain = obj.get('mint'), obj.get('chain')
        if dt is None:
            stats['bad_ts'] += 1
        elif not isinstance(mint, str) or not mint:
            stats['bad_mint'] += 1
        elif expected_mint is not None and mint != expected_mint:
            stats['mint_mismatch'] += 1
        elif chain not in CHAINS:
            stats['bad_chain'] += 1
        elif 'price_usd' not in obj:
            stats['missing_price'] += 1
        elif obj['price_usd'] is None:
            stats['null_price'] += 1
        elif not _valid_price(obj['price_usd']):
            stats['invalid_price'] += 1
        else:
            t = ts_seconds(dt)
            if last is not None and t < last:
                stats['out_of_order'] += 1
                continue
            last = t
            rows.append(QuoteRow(dt, t, mint, chain, float(obj['price_usd'])))
            stats['rows_ok'] += 1
    return rows, stats


# ---------------------------------------------------------------- costs

@dataclass(frozen=True)
class CostModel:
    sol_usd: float
    notional_usd: float = 10.0
    venue_fee_bps: float = 30.0
    entry_slippage_bps: float = 100.0
    exit_slippage_bps: float = 500.0
    priority_fee_lamports: float = 2_000_000

    def __post_init__(self):
        for name, value in asdict(self).items():
            if not _finite(value) or value < 0:
                raise ValueError('invalid cost parameter %s=%r' % (name, value))
        if self.sol_usd <= 0 or self.notional_usd <= 0:
            raise ValueError('sol_usd and notional_usd must be positive')

    @property
    def priority_fee_usd(self):
        return self.priority_fee_lamports / 1e9 * self.sol_usd

    def scaled(self, k):
        """All variable costs multiplied by k (the 3x stress uses k=3)."""
        return replace(self, venue_fee_bps=self.venue_fee_bps * k,
                       entry_slippage_bps=self.entry_slippage_bps * k,
                       exit_slippage_bps=self.exit_slippage_bps * k,
                       priority_fee_lamports=self.priority_fee_lamports * k)


# ---------------------------------------------------------------- variants

@dataclass(frozen=True)
class SafetyFloor:
    """Common to control and every variant: isolates architecture, not safety."""
    hard_stop_pct: float = 40.0
    dump_drop_pct: float = 12.0
    dump_window_sec: float = 60.0


@dataclass(frozen=True)
class LadderSpec:
    """Control: the live ladder. TP fractions are of the REMAINING balance
    (fomo_trader.sell_pct_of_balance), so rung 2 sells 12.5% of the original.
    trail_after_tp_pct and the stale exit are code defaults absent from config."""
    take_profits: tuple = ((100.0, 0.50), (200.0, 0.25))
    trailing_stop_pct: float = 30.0
    trail_after_tp_pct: float = 12.0
    stale_exit_min: float = 45.0
    stale_exit_max_gain_pct: float = 10.0
    floor: SafetyFloor = field(default_factory=SafetyFloor)
    kind: str = 'ladder'


@dataclass(frozen=True)
class RatchetSpec:
    """(a) Peak-anchored trailing take-profit. Fractions are of the remaining."""
    arm_gain_pct: float = 20.0
    pullback_pct: float = 10.0
    partial_fractions: tuple = (0.50, 0.50)
    deep_trail_pct: float = 25.0
    floor: SafetyFloor = field(default_factory=SafetyFloor)
    kind: str = 'ratchet'


@dataclass(frozen=True)
class TimeBoxSpec:
    """(b) Time-boxed momentum exit with a mark-peak trailing stop."""
    time_box_min: float = 20.0
    trail_pct: float = 20.0
    floor: SafetyFloor = field(default_factory=SafetyFloor)
    kind: str = 'time_box'


@dataclass(frozen=True)
class VolScaledSpec:
    """(c) Volatility-scaled partials. Rung fractions are of the ORIGINAL."""
    window_sec: float = 300.0
    min_returns: int = 10
    high_vol_cutoff: float = 0.05  # realized log-return vol per sqrt(minute)
    high_rungs: tuple = ((10.0, 0.20), (20.0, 0.20), (35.0, 0.20))
    high_trail_pct: float = 20.0
    low_rungs: tuple = ((30.0, 0.30), (60.0, 0.30))
    low_trail_pct: float = 30.0
    floor: SafetyFloor = field(default_factory=SafetyFloor)
    kind: str = 'vol_scaled'


VARIANTS = {
    CONTROL: LadderSpec(),
    'ratchet': RatchetSpec(),
    'time_box': TimeBoxSpec(),
    'vol_scaled': VolScaledSpec(),
}


# ---------------------------------------------------------------- simulation

@dataclass(frozen=True)
class Exit:
    ts: float
    mark: float
    fraction: float  # of the original position
    reason: str


@dataclass
class ExitReport:
    variant: str
    exits: list
    gross_proceeds_usd: float
    gross_pnl_usd: float
    costs_usd: dict
    net_usd: float
    censored: bool
    close_ts: float
    regime: str = None
    vol_per_min: float = None
    marks_used: int = 0


class _Book:
    def __init__(self):
        self.remaining = 1.0
        self.exits = []

    @property
    def open(self):
        return self.remaining > _EPS

    def sell(self, t, mark, fraction, reason):
        fraction = min(fraction, self.remaining)
        if fraction <= _EPS:
            return
        self.remaining -= fraction
        if self.remaining <= _EPS:
            self.remaining = 0.0
        self.exits.append(Exit(t, mark, fraction, reason))

    def sell_all(self, t, mark, reason):
        self.sell(t, mark, self.remaining, reason)


class _DumpWindow:
    """Max mark over the trailing window, current tick included."""
    def __init__(self, floor):
        self.floor = floor
        self.marks = deque()

    def dumped(self, t, price):
        self.marks.append((t, price))
        while self.marks and self.marks[0][0] < t - self.floor.dump_window_sec:
            self.marks.popleft()
        wmax = max(p for _, p in self.marks)
        return (wmax - price) / wmax * 100 >= self.floor.dump_drop_pct


def _hard(price, entry, floor):
    return (entry - price) / entry * 100 >= floor.hard_stop_pct


def _ticks(marks, entry_ts):
    out = []
    for m in marks:
        if isinstance(m, QuoteRow):
            t, p = m.t, m.price_usd
        else:
            t, p = ts_seconds(m[0]), m[1]
        if t is not None and t >= entry_ts and _valid_price(p):
            out.append((t, float(p)))
    return sorted(out, key=lambda x: x[0])


def _run_ladder(ticks, entry, entry_ts, spec, book):
    peak, fired, dump = entry, set(), _DumpWindow(spec.floor)
    for t, p in ticks:
        peak = max(peak, p)
        gain = (p / entry - 1) * 100
        trail = spec.trailing_stop_pct
        if fired:
            trail = min(trail, spec.trail_after_tp_pct)
        dumped = dump.dumped(t, p)
        for i, (tp_pct, frac) in enumerate(spec.take_profits):
            if i not in fired and gain >= tp_pct:
                fired.add(i)
                book.sell(t, p, book.remaining * frac, 'tp+%g' % tp_pct)
        if not book.open:
            break
        if dumped:
            book.sell_all(t, p, 'dump')
        elif (peak - p) / peak * 100 >= trail:
            book.sell_all(t, p, 'trailing_stop')
        elif _hard(p, entry, spec.floor):
            book.sell_all(t, p, 'hard_stop')
        elif (spec.stale_exit_min and (t - entry_ts) / 60 >= spec.stale_exit_min
              and gain < spec.stale_exit_max_gain_pct):
            book.sell_all(t, p, 'stale')
        if not book.open:
            break
    return {}


def _run_ratchet(ticks, entry, entry_ts, spec, book):
    armed, anchor, done, dump = False, None, 0, _DumpWindow(spec.floor)
    for t, p in ticks:
        dumped = dump.dumped(t, p)
        if not armed and p >= entry * (1 + spec.arm_gain_pct / 100):
            armed, anchor = True, p
        if armed:
            anchor = max(anchor, p)
            if done < len(spec.partial_fractions) and p <= anchor * (1 - spec.pullback_pct / 100):
                book.sell(t, p, book.remaining * spec.partial_fractions[done],
                          'ratchet_partial_%d' % (done + 1))
                done += 1
                anchor = p
        if not book.open:
            break
        if dumped:
            book.sell_all(t, p, 'dump')
        elif (armed and done >= len(spec.partial_fractions)
              and p <= anchor * (1 - spec.deep_trail_pct / 100)):
            book.sell_all(t, p, 'ratchet_deep_trail')
        elif _hard(p, entry, spec.floor):
            book.sell_all(t, p, 'hard_stop')
        if not book.open:
            break
    return {}


def _run_time_box(ticks, entry, entry_ts, spec, book):
    peak, dump = None, _DumpWindow(spec.floor)
    for t, p in ticks:
        dumped = dump.dumped(t, p)
        peak = p if peak is None else max(peak, p)
        if dumped:
            book.sell_all(t, p, 'dump')
        elif p <= peak * (1 - spec.trail_pct / 100):
            book.sell_all(t, p, 'mark_trail')
        elif t - entry_ts >= spec.time_box_min * 60:
            book.sell_all(t, p, 'time_box')
        elif _hard(p, entry, spec.floor):
            book.sell_all(t, p, 'hard_stop')
        if not book.open:
            break
    return {}


def realized_vol_per_min(marks):
    """sqrt(sum of squared log returns / elapsed seconds * 60); (vol, n_returns)."""
    ss = elapsed = 0.0
    n = 0
    for (t0, p0), (t1, p1) in zip(marks, marks[1:]):
        if t1 <= t0:
            continue
        r = log(p1 / p0)
        ss += r * r
        elapsed += t1 - t0
        n += 1
    return (sqrt(ss / elapsed * 60) if n else None), n


def classify_regime(window_marks, spec):
    vol, n = realized_vol_per_min(window_marks)
    if n < spec.min_returns:
        return 'low_default', vol
    return ('high' if vol >= spec.high_vol_cutoff else 'low'), vol


def _run_vol_scaled(ticks, entry, entry_ts, spec, book):
    regime = vol = None
    window, fired, peak, dump = [], set(), None, _DumpWindow(spec.floor)
    for t, p in ticks:
        dumped = dump.dumped(t, p)
        peak = p if peak is None else max(peak, p)
        if regime is None:
            if t - entry_ts < spec.window_sec:
                window.append((t, p))
            else:
                regime, vol = classify_regime(window, spec)
        if regime is not None:
            tag = 'high' if regime == 'high' else 'low'
            rungs = spec.high_rungs if tag == 'high' else spec.low_rungs
            trail = spec.high_trail_pct if tag == 'high' else spec.low_trail_pct
            gain = (p / entry - 1) * 100
            for i, (rung_pct, frac) in enumerate(rungs):
                if i not in fired and gain >= rung_pct:
                    fired.add(i)
                    book.sell(t, p, frac, 'vol_%s_rung_%d' % (tag, i + 1))
        if not book.open:
            break
        if dumped:
            book.sell_all(t, p, 'dump')
        elif regime is not None and (peak - p) / peak * 100 >= trail:
            book.sell_all(t, p, 'vol_%s_trail' % tag)
        elif _hard(p, entry, spec.floor):
            book.sell_all(t, p, 'hard_stop')
        if not book.open:
            break
    return {'regime': regime, 'vol_per_min': vol}


_RUNNERS = {'ladder': _run_ladder, 'ratchet': _run_ratchet,
            'time_box': _run_time_box, 'vol_scaled': _run_vol_scaled}


def account(exits, entry_price_usd, notional_usd, costs):
    """Gross, cost breakdown and net in USD for a list of exits."""
    gross = sum(notional_usd * e.fraction * e.mark / entry_price_usd for e in exits)
    tx = 1 + len(exits)
    breakdown = {
        'entry_venue_fee': notional_usd * costs.venue_fee_bps / 1e4,
        'entry_slippage': notional_usd * costs.entry_slippage_bps / 1e4,
        'exit_venue_fee': gross * costs.venue_fee_bps / 1e4,
        'exit_slippage': gross * costs.exit_slippage_bps / 1e4,
        'priority_fee': tx * costs.priority_fee_usd,
    }
    total = sum(breakdown.values())
    breakdown['total'] = total
    breakdown['transactions'] = tx
    return gross, breakdown, gross - notional_usd - total


def simulate_variant(marks, entry_price_usd, entry_ts, notional_usd, variant, costs, name=None):
    """Walk marks in time order under one variant. Decisions at tick t use only
    marks <= t. A position still open after the last mark is marked out at that
    mark with reason `path_end` and flagged censored."""
    entry_ts = ts_seconds(entry_ts)
    if entry_ts is None or not _valid_price(entry_price_usd) or not _valid_price(notional_usd):
        raise ValueError('invalid entry price, entry ts or notional')
    ticks = _ticks(marks, entry_ts)
    if not ticks:
        raise ValueError('no usable marks at or after entry')
    book = _Book()
    extra = _RUNNERS[variant.kind](ticks, entry_price_usd, entry_ts, variant, book)
    censored = book.open
    if censored:
        book.sell_all(ticks[-1][0], ticks[-1][1], PATH_END)
    gross, breakdown, net = account(book.exits, entry_price_usd, notional_usd, costs)
    return ExitReport(variant=name or variant.kind, exits=book.exits,
                      gross_proceeds_usd=gross, gross_pnl_usd=gross - notional_usd,
                      costs_usd=breakdown, net_usd=net, censored=censored,
                      close_ts=book.exits[-1].ts, regime=extra.get('regime'),
                      vol_per_min=extra.get('vol_per_min'), marks_used=len(ticks))


# ---------------------------------------------------------------- experiment

@dataclass(frozen=True)
class Position:
    """A closed journal position joined to its quote path by mint.
    entry_price_usd is the journal's paper entry fill converted to USD."""
    mint: str
    chain: str
    entry_ts: float
    entry_price_usd: float
    marks: tuple
    closed: bool = True


def qualify(position, min_marks=MIN_MARKS):
    """None when qualifying, else the disqualification reason."""
    if not position.closed:
        return 'not_closed'
    entry_ts = ts_seconds(position.entry_ts)
    if entry_ts is None:
        return 'bad_entry_ts'
    if not _valid_price(position.entry_price_usd):
        return 'bad_entry_price'
    if len(_ticks(position.marks, entry_ts)) < min_marks:
        return 'too_few_marks'
    return None


def _seeded_key(text):
    return hashlib.sha256(('%d:%s' % (SEED, text)).encode()).hexdigest()


def _position_key(p):
    return '%s:%s:%r' % (p.chain, p.mint, ts_seconds(p.entry_ts))


def split_is_oos(positions):
    """Chronological by entry ts: first 2/3 IS, last 1/3 OOS. Ties broken by a
    seed-20260927 hash of position identity, independent of input order."""
    ordered = sorted(positions, key=lambda p: (ts_seconds(p.entry_ts), _seeded_key(_position_key(p)),
                                               p.entry_price_usd))
    cut = (2 * len(ordered)) // 3
    return ordered[:cut], ordered[cut:]


def summarize(values):
    values = [float(v) for v in values]
    n = len(values)
    return {'n': n, 'net_usd': sum(values), 'net_per_close_usd': sum(values) / n if n else None,
            'win_rate': sum(v > 0 for v in values) / n if n else None}


def kill_decisions(oos_by_variant, min_closes=MIN_OOS_CLOSES, stop_all_usd=STOP_ALL_USD):
    """E1-style chronological OOS replay. oos_by_variant maps name -> list of
    (entry_ts, close_ts, key, net_usd). KILL_VARIANT and STOP_ALL stop new
    entries; a triggering close and already-open positions remain included."""
    events = sorted(((e, c, name, key, net) for name, rows in oos_by_variant.items()
                     for e, c, key, net in rows), key=lambda x: (x[0], x[2], x[3]))
    pending, killed = [], set()
    accepted = {name: [] for name in oos_by_variant}
    state = {'cumulative': 0.0, 'stop_all': False}

    def settle(before):
        nonlocal pending
        ready = sorted((p for p in pending if p[0] <= before), key=lambda p: (p[0], p[1], p[2]))
        pending = [p for p in pending if p[0] > before]
        for _close_ts, name, key, net in ready:
            accepted[name].append((key, net))
            state['cumulative'] += net
            if len(accepted[name]) >= min_closes and sum(v for _, v in accepted[name]) <= 0:
                killed.add(name)
            if state['cumulative'] <= stop_all_usd:
                state['stop_all'] = True

    skipped_stop = skipped_kill = 0
    for entry_ts, close_ts, name, key, net in events:
        settle(entry_ts)
        if state['stop_all']:
            skipped_stop += 1
        elif name in killed:
            skipped_kill += 1
        else:
            pending.append((close_ts, name, key, net))
    settle(inf)
    variants = {}
    for name, rows in accepted.items():
        row = summarize(v for _, v in rows)
        row['accepted_keys'] = [k for k, _ in rows]
        row['decision'] = (KILL_VARIANT if name in killed else
                           INSUFFICIENT_OOS_HISTORY if len(rows) < min_closes else CONTINUE)
        variants[name] = row
    return {'variants': variants, 'stop_all': state['stop_all'],
            'cumulative_oos_net_usd': state['cumulative'],
            'skipped_after_stop': skipped_stop, 'skipped_after_kill': skipped_kill}


@dataclass
class E3Result:
    verdict: str
    status: str
    qualifying: int
    required: int
    min_marks: int
    disqualified: dict
    n_is: int = 0
    n_oos: int = 0
    variants: dict = field(default_factory=dict)
    frozen_variant: str = None
    stop_all: bool = False
    cumulative_oos_net_usd: float = 0.0
    gates: dict = field(default_factory=dict)
    costs: dict = field(default_factory=dict)
    seed: int = SEED
    freeze_date: str = FREEZE_DATE

    def to_dict(self):
        return asdict(self)


def _simulate_all(positions, name, spec, costs):
    return [simulate_variant(p.marks, p.entry_price_usd, p.entry_ts, costs.notional_usd,
                             spec, costs, name) for p in positions]


def select_variant(is_rows, control_npc, min_is_closes=MIN_IS_CLOSES,
                   max_censored_share=MAX_CENSORED_SHARE):
    """Highest IS net-per-close among eligible candidates, or None (ship nothing)."""
    eligible = [name for name, row in is_rows.items()
                if row['n'] >= min_is_closes and row['censored_share'] <= max_censored_share
                and row['net_per_close_usd'] is not None and row['net_per_close_usd'] > 0
                and (control_npc is None or row['net_per_close_usd'] > control_npc)]
    if not eligible:
        return None
    return max(eligible, key=lambda n: (round(is_rows[n]['net_per_close_usd'], 9), _seeded_key(n)))


def run_e3(positions, variants, costs, min_marks=MIN_MARKS, trigger=DATA_TRIGGER_CLOSES):
    """Pre-registered E3 evaluation. Verdict is "awaiting data" below the trigger."""
    if CONTROL not in variants:
        raise ValueError('variants must include the control ladder')
    qualifying, disqualified = [], {}
    for p in positions:
        reason = qualify(p, min_marks)
        if reason:
            disqualified[reason] = disqualified.get(reason, 0) + 1
        else:
            qualifying.append(p)
    cost_row = dict(asdict(costs), priority_fee_usd=costs.priority_fee_usd)
    result = E3Result(verdict=AWAITING_DATA, status=AWAITING_DATA, qualifying=len(qualifying),
                      required=trigger, min_marks=min_marks, disqualified=disqualified,
                      costs=cost_row)
    if len(qualifying) < trigger:
        return result
    is_set, oos_set = split_is_oos(qualifying)
    result.n_is, result.n_oos, result.status = len(is_set), len(oos_set), 'runnable'
    candidates = [n for n in variants if n != CONTROL]
    oos_reports, is_rows = {}, {}
    for name, spec in variants.items():
        is_reports = _simulate_all(is_set, name, spec, costs)
        oos_reports[name] = _simulate_all(oos_set, name, spec, costs)
        row = {'IS': summarize(r.net_usd for r in is_reports),
               'OOS_all': summarize(r.net_usd for r in oos_reports[name])}
        row['IS']['censored_share'] = sum(r.censored for r in is_reports) / len(is_reports)
        if spec.kind == 'vol_scaled':
            regimes = {}
            for r in is_reports:
                regimes[r.regime] = regimes.get(r.regime, 0) + 1
            row['IS']['regimes'] = regimes
            row['IS']['degenerate_regime_split'] = (
                min(regimes.get('high', 0), len(is_reports) - regimes.get('high', 0))
                < DEGENERATE_REGIME_SHARE * len(is_reports))
        result.variants[name] = row
        if name in candidates:
            is_rows[name] = row['IS']
    control_npc = result.variants[CONTROL]['IS']['net_per_close_usd']
    frozen = select_variant(is_rows, control_npc)
    result.frozen_variant = frozen
    keyed = {name: [(ts_seconds(p.entry_ts), r.close_ts, _position_key(p), r.net_usd)
                    for p, r in zip(oos_set, oos_reports[name])] for name in candidates}
    kd = kill_decisions(keyed)
    result.stop_all = kd['stop_all']
    result.cumulative_oos_net_usd = kd['cumulative_oos_net_usd']
    for name in candidates:
        result.variants[name]['OOS'] = kd['variants'][name]
    if frozen is not None:
        oos = kd['variants'][frozen]
        is_npc = is_rows[frozen]['net_per_close_usd']
        retention = oos['net_per_close_usd'] / is_npc if oos['n'] and is_npc > 0 else None
        keys = set(oos['accepted_keys'])
        stressed = _simulate_all([p for p in oos_set if _position_key(p) in keys], frozen,
                                 variants[frozen], costs.scaled(STRESS_MULTIPLIER))
        stress_net = sum(r.net_usd for r in stressed)
        result.gates = {'oos_closes': oos['n'], 'decision_capable': oos['n'] >= MIN_OOS_CLOSES,
                        'is_net_per_close_usd': is_npc, 'oos_net_per_close_usd': oos['net_per_close_usd'],
                        'retention': retention,
                        'retention_pass': retention is not None and retention >= RETENTION_GATE,
                        'stress_multiplier': STRESS_MULTIPLIER, 'stress_oos_net_usd': stress_net,
                        'stress_pass': stress_net >= 0, 'decision': oos['decision']}
        if oos['n'] >= MIN_OOS_CLOSES:
            result.status = 'decision_capable'
    result.verdict = _verdict(result, kd)
    return result


def _verdict(result, kd):
    g, frozen = result.gates, result.frozen_variant
    if result.stop_all:
        return 'STOP_ALL: cumulative OOS net <= -$21 across variants; ship nothing'
    if frozen is None:
        return 'NO_VARIANT_BEATS_CONTROL: ship nothing'
    if g['decision'] == KILL_VARIANT:
        return 'KILL_VARIANT: %s OOS net <= $0 at >= 30 closes; ship nothing' % frozen
    if not g['decision_capable']:
        return ('RUNNABLE_NOT_DECISION_CAPABLE: insufficient OOS history (%d/%d OOS closes for %s)'
                % (g['oos_closes'], MIN_OOS_CLOSES, frozen))
    if not g['retention_pass']:
        return 'FAIL_RETENTION: %s OOS net-per-close < 60%% of IS; ship nothing' % frozen
    if not g['stress_pass']:
        return 'FAIL_3X_COST_STRESS: %s OOS net < $0 at 3x costs; ship nothing' % frozen
    return ('PASS_MARK_BOUND: %s (mark-implied bound only; fill-feasibility study required '
            'before any wiring)' % frozen)
