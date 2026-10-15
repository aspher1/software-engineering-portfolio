import json
from unittest.mock import Mock

import pytest
import requests
import kalshi_client as client
import same_day_loop as sdl


@pytest.mark.parametrize('status', [429, 500, 503])
def test_retry_backoff(monkeypatch, status):
    responses = [Mock(status_code=status) for _ in range(3)] + [Mock(status_code=200)]
    get = Mock(side_effect=responses)
    sleep = Mock()
    monkeypatch.setattr(client.requests, 'get', get)
    monkeypatch.setattr(client.time, 'sleep', sleep)
    monkeypatch.setattr(client.random, 'uniform', lambda *a: 0.25)
    assert client.kalshi_get('https://example.test') is responses[-1]
    assert [c.args[0] for c in sleep.call_args_list] == [1.25, 2.25, 4.25]
    assert get.call_count == 4


@pytest.mark.parametrize('status, attempts', [(429, 4), (503, 4), (404, 1)])
def test_exhausted_or_immediate_error(monkeypatch, status, attempts):
    response = Mock(status_code=status)
    response.raise_for_status.side_effect = requests.HTTPError(str(status))
    get = Mock(return_value=response)
    monkeypatch.setattr(client.requests, 'get', get)
    monkeypatch.setattr(client.time, 'sleep', lambda _: None)
    with pytest.raises(requests.HTTPError):
        client.kalshi_get('https://example.test')
    assert get.call_count == attempts


def test_limiter_shared_between_instances(tmp_path):
    now = [100.0]
    sleeps = []
    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
    path = str(tmp_path / 'rate')
    a = client.RateLimiter(path, clock=lambda: now[0], sleep=sleep)
    b = client.RateLimiter(path, clock=lambda: now[0], sleep=sleep)
    a.acquire()
    b.acquire()
    a.acquire()
    assert sleeps == [2.0, 2.0]
    now[0] += 10
    b.acquire()
    assert sleeps == [2.0, 2.0]


def test_cold_cache_spawns_refresh_and_keeps_series(monkeypatch):
    # Cold sweep cache: no synchronous sweep anymore — a background refresh
    # is spawned and the 9 crypto series are still fetched fresh every scan.
    calls = []
    def get(url, params, **kwargs):
        calls.append(params)
        return Mock(json=lambda: {'markets': [{
            'ticker': params['series_ticker'], 'close_time': '2026-10-06T18:00:00Z'}]})
    monkeypatch.setattr(sdl, 'kalshi_get', get)
    monkeypatch.setattr(sdl, '_load_sweep_cache', lambda today: (None, None))
    spawned = Mock(return_value=True)
    monkeypatch.setattr(sdl, '_spawn_sweep_refresh', spawned)
    rows = sdl.kalshi_same_day('2026-10-06')
    assert len(rows) == 9
    assert len(calls) == 9
    assert all('series_ticker' in p for p in calls)
    assert spawned.call_count == 1
    assert rows.failures == ['sweep_cache=cold refresh_spawned=True']


def test_series_failure_degrades_gracefully(monkeypatch):
    # Series fetch fails AND cache cold: failures recorded, empty rows, no crash.
    monkeypatch.setattr(sdl, 'kalshi_get', Mock(side_effect=requests.HTTPError('429')))
    monkeypatch.setattr(sdl, '_load_sweep_cache', lambda today: (None, None))
    monkeypatch.setattr(sdl, '_spawn_sweep_refresh', lambda today: True)
    rows = sdl.kalshi_same_day('2026-10-06')
    assert len(rows) == 0
    assert any('sweep_cache=cold' in f for f in rows.failures)
    assert sum('series KX' in f for f in rows.failures) == 9


def test_total_fetch_failure_reports_failure(monkeypatch, capsys):
    monkeypatch.setattr(sdl, 'kalshi_get', Mock(side_effect=requests.HTTPError('429')))
    monkeypatch.setattr(sdl, '_load_sweep_cache', lambda today: (None, None))
    monkeypatch.setattr(sdl, '_spawn_sweep_refresh', lambda today: True)
    monkeypatch.setattr(sdl, 'load_state', lambda: {'alerted': {}})
    monkeypatch.setattr('sys.argv', ['scanner', '--no-mark'])
    sdl.main()
    assert json.loads(capsys.readouterr().out)['ok'] is False


def test_mark_excludes_new_entries_until_next_run(tmp_path, monkeypatch, capsys):
    from test_iter8_infra import _scan
    checked = []
    def exits(alerted, *args, **kwargs):
        checked.append(dict(alerted))
        for rec in alerted.values():
            rec['exit_ts'] = 'next-run'
        return []
    monkeypatch.setattr(sdl, 'check_exits', exits)
    _, alerted, _, _ = _scan(tmp_path, monkeypatch, capsys)
    assert alerted
    assert checked == [{}]
    assert all('exit_ts' not in rec for rec in alerted.values())
    # A subsequent scan reloads the stamped keys, making them exit-eligible.
    monkeypatch.setattr(sdl, 'load_state', lambda: {'alerted': alerted})
    sdl.main()
    capsys.readouterr()
    assert set(checked[-1]) == set(alerted)
    assert all(rec['exit_ts'] == 'next-run' for rec in alerted.values())


def test_degraded_scan_prices_and_no_mark_is_read_only(tmp_path, monkeypatch, capsys):
    from test_iter8_infra import _scan
    # _scan supplies its own market fixture; wrap at main entry to annotate it.
    main = sdl.main
    def degraded_main():
        fetch = sdl.kalshi_same_day
        def degraded(today):
            rows = sdl.KalshiMarkets()
            rows.extend(fetch(today))
            rows.failures.append('sweep exhausted 429')
            return rows
        monkeypatch.setattr(sdl, 'kalshi_same_day', degraded)
        sp = tmp_path / 'state.json'
        before = sp.stat().st_mtime_ns
        main()
        assert sp.stat().st_mtime_ns == before
    monkeypatch.setattr('test_iter8_infra.main', degraded_main)
    def no_write(*a, **k):
        pytest.fail('read-only scan attempted a write')
    monkeypatch.setattr(sdl, 'save_state', no_write)
    monkeypatch.setattr(sdl, 'append_grades', no_write)
    out, alerted, _, _ = _scan(tmp_path, monkeypatch, capsys,
                               argv=('scanner', '--no-mark'))
    assert out['ok'] is True
    assert any('kalshi_degraded=' in n for n in out['notes'])
    assert alerted == {}


def test_limiter_serializes_processes(tmp_path):
    import subprocess
    import sys
    path = str(tmp_path / 'shared.rate')
    code = ('import sys,time; sys.path.insert(0,"scripts"); '
            'from kalshi_client import RateLimiter; '
            'RateLimiter(sys.argv[1],interval=0.05).acquire(); '
            'print(time.monotonic())')
    workers = [subprocess.Popen([sys.executable, '-c', code, path],
                                stdout=subprocess.PIPE, text=True)
               for _ in range(3)]
    stamps = sorted(float(p.communicate(timeout=10)[0]) for p in workers)
    assert stamps[-1] - stamps[0] >= 0.09
    assert all(p.returncode == 0 for p in workers)


def test_request_volume_cold_cache_is_nine_series(monkeypatch):
    # Cadence fix: a routine scan costs the 9 series fetches, never the
    # ~61-page synchronous sweep (served from cache / background refresh).
    calls = []
    def get(url, params, **kwargs):
        calls.append(dict(params))
        return Mock(json=lambda: {'markets': [{
            'ticker': params['series_ticker'], 'close_time': '2026-10-06T18:00:00Z'}]})
    monkeypatch.setattr(sdl, 'kalshi_get', get)
    monkeypatch.setattr(sdl, '_load_sweep_cache', lambda today: (None, None))
    monkeypatch.setattr(sdl, '_spawn_sweep_refresh', lambda today: True)
    rows = sdl.kalshi_same_day('2026-10-06')
    assert len(calls) == 9
    assert sum('series_ticker' in p for p in calls) == 9
    assert len(rows) == 9


def test_request_volume_warm_cache_merges_without_sweep_calls(monkeypatch):
    calls = []
    def get(url, params, **kwargs):
        calls.append(dict(params))
        return Mock(json=lambda: {'markets': [{
            'ticker': params['series_ticker'], 'close_time': '2026-10-06T18:00:00Z'}]})
    monkeypatch.setattr(sdl, 'kalshi_get', get)
    cached = [{'ticker': 'CACHED-1', 'close_time': '2026-10-06T18:00:00Z',
               'title': 'cached market', 'yes_bid_dollars': 0.4,
               'yes_ask_dollars': 0.45}]
    monkeypatch.setattr(sdl, '_load_sweep_cache', lambda today: (cached, 60.0))
    rows = sdl.kalshi_same_day('2026-10-06')
    assert len(calls) == 9  # sweep served from cache: zero sweep HTTP
    assert len(rows) == 10
    assert rows.cache_notes == ['sweep_cache=hit age_s=60 markets=1']
    assert not rows.failures
