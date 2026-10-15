import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_shadow_modules_do_not_import_live_code_or_write_protected_path(tmp_path):
    banned = {'same_day_loop', 'exit_copilot', 'hold_guard', 'flip_watch'}
    for name in ('candidate_filters', 'maker_fill_analysis'):
        path = ROOT / 'scripts' / (name + '.py')
        tree = ast.parse(path.read_text())
        imports = {alias.name.split('.')[0] for node in ast.walk(tree)
                   if isinstance(node, ast.Import) for alias in node.names}
        imports |= {node.module.split('.')[0] for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module}
        assert not imports & banned
        m = module(name)
        for protected in ('same_day_loop_state.json', 'signal_grades.jsonl', 'exit_events.jsonl'):
            with pytest.raises(ValueError):
                m.safe_output(tmp_path / protected)
            assert not (tmp_path / protected).exists()


def test_splits_have_strict_time_boundary_and_calibration_fixed():
    m = module('candidate_filters')
    rows = [{'key':str(i),'time':i//2,'sym':'BTC' if i%3 else 'SOL',
             'edge':.1 if i%2 else .2,'price':.5,'pnl':.1 if i%3 else -.1}
            for i in range(100)]
    train, blocks = m.split(rows)
    assert all(train[-1]['time'] < b[0]['time'] for b in blocks)
    assert all(blocks[i][-1]['time'] < blocks[i+1][0]['time'] for i in range(2))
    c,t = m.calibrate(train)
    assert (c,t) == m.calibrate(rows[:len(train)])
    assert all(m.selected(r,c,t,'combo') == (r['sym'] in c and r['edge']>=t) for r in rows)


def test_maker_fee_and_fill_bracket():
    m = module('maker_fill_analysis')
    assert m.fee(.5) == .02
    assert m.snapshot(.4,.42,'NO') == pytest.approx((.58,.6))
    q=(.4,.42)
    assert m.fillable(.4,q,'optimistic')
    assert not m.fillable(.4,q,'mid')
    assert not m.fillable(.4,q,'pessimistic')
    assert m.fillable(.42,q,'mid')
    assert not m.fillable(.42,q,'pessimistic')
