"""Build E3/AI-exit Position objects from the live paper journal + Z4 quote paths.

Honest join: each journal CLOSE is paired with its ENTRY record (same mint,
most recent entry at/before the close). Entry price comes from the entry
record's commit_price_usd (the paper fill), NOT from marks. Quote path comes
from runs/paper-1h/quote_paths/<sanitized mint>.jsonl via the E3 loader.
Positions with <20 marks are disqualified (E3 MIN_MARKS).

Usage:
    PYTHONPATH=. .venv/bin/python -m analysis.z5_exit_arch.positions_from_journal
prints JSON: {"qualifying": N, "disqualified": {...}, "positions": [...]}
Positions are serialized minimally (mint, chain, entry_ts, entry_price_usd,
n_marks) — full marks are reloaded by the consumer via load_quote_path.
"""
import json
import os
import sys
from datetime import datetime

from analysis.z5_exit_arch.e3 import load_quote_path, Position, qualify, MIN_MARKS

PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUN_DIR = os.path.join(PROJ, 'runs', 'paper-1h')
JOURNAL = os.path.join(RUN_DIR, 'trades.jsonl')
QP_DIR = os.path.join(RUN_DIR, 'quote_paths')
TS_FORMAT = '%Y-%m-%d %H:%M:%S'


def _sanitize(mint):
    return ''.join(c if c.isalnum() and c.isascii() else '_' for c in str(mint))[:128] or 'unknown'


def _parse_ts(s):
    try:
        return datetime.strptime(s, TS_FORMAT)
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None


def build_positions(min_marks=MIN_MARKS):
    recs = []
    with open(JOURNAL) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    entries_by_mint = {}
    for r in recs:
        if r.get('type') == 'entry' and r.get('mint'):
            entries_by_mint.setdefault(r['mint'], []).append(r)

    qualifying, disqualified = [], {}
    closes = [r for r in recs if r.get('type') == 'close' and r.get('mint')]
    for r in closes:
        mint = r['mint']
        close_ts = _parse_ts(r.get('ts'))
        cands = [e for e in entries_by_mint.get(mint, [])
                 if _parse_ts(e.get('ts')) and (close_ts is None or _parse_ts(e['ts']) <= close_ts)]
        if not cands:
            disqualified['no_entry_record'] = disqualified.get('no_entry_record', 0) + 1
            continue
        entry = max(cands, key=lambda e: _parse_ts(e['ts']))
        entry_price = entry.get('commit_price_usd') or entry.get('signal_price_usd')
        if not isinstance(entry_price, (int, float)) or entry_price <= 0:
            disqualified['no_usd_entry_price'] = disqualified.get('no_usd_entry_price', 0) + 1
            continue
        qp = os.path.join(QP_DIR, _sanitize(mint) + '.jsonl')
        if not os.path.exists(qp):
            disqualified['no_quote_path'] = disqualified.get('no_quote_path', 0) + 1
            continue
        marks, _ = load_quote_path(qp, expected_mint=mint)
        pos = Position(mint=mint, chain=r.get('chain', 'solana'),
                       entry_ts=_parse_ts(entry['ts']).timestamp(),
                       entry_price_usd=float(entry_price),
                       marks=tuple(marks), closed=True)
        reason = qualify(pos, min_marks)
        if reason:
            disqualified[reason] = disqualified.get(reason, 0) + 1
            continue
        qualifying.append(pos)
    return qualifying, disqualified


def main():
    qualifying, disqualified = build_positions()
    out = {
        'qualifying': len(qualifying),
        'disqualified': disqualified,
        'positions': [{
            'mint': p.mint, 'chain': p.chain, 'entry_ts': p.entry_ts,
            'entry_price_usd': p.entry_price_usd, 'n_marks': len(p.marks),
        } for p in qualifying],
    }
    print(json.dumps(out))


if __name__ == '__main__':
    main()
