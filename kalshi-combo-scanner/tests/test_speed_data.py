"""Offline tests for the speed/data layer: sharding, sweep coverage, caches,
per-host sessions and the verify-before-alert re-check."""

import io
import json
import os
import threading
import time
from contextlib import redirect_stdout
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import combo_scan as cs
from test_combo_math import listing, run_main_stubbed

PM = int(datetime(2027, 11, 3, 15, tzinfo=timezone.utc).timestamp())


def tiles(ranges, lo=cs.CLOSE_TS_MIN, hi=cs.CLOSE_TS_MAX):
    rs = [tuple(r[:2]) for r in ranges]
    return (rs[0][0] == lo and rs[-1][1] == hi and all(a <= b for a, b in rs)
            and all(n[0] == p[1] + 1 for p, n in zip(rs, rs[1:])))


# ------------------------------------------------------------ pre-split ----

def test_point_mass_constants_are_2027_11_03_1400z_and_1500z():
    assert cs.point_mass_ts() == [PM - 3600, PM] == [1825250400, 1825254000]


def test_presplit_isolates_point_mass_and_keeps_coverage():
    seeds = cs.seed_ranges(1_790_000_000)
    rs = cs.presplit_point_masses(seeds, [PM])
    assert tiles(rs) and (PM, PM) in rs and len(rs) == len(seeds) + 2
    assert cs.presplit_point_masses([(PM, PM + 9)], [PM]) == [(PM, PM), (PM + 1, PM + 9)]
    assert cs.presplit_point_masses([(PM - 9, PM)], [PM]) == [(PM - 9, PM - 1), (PM, PM)]
    assert cs.presplit_point_masses([(PM, PM)], [PM]) == [(PM, PM)]
    assert cs.presplit_point_masses([(0, 5)], [PM]) == [(0, 5)]


def test_sweep_seeds_without_plan_start_with_point_mass():
    items = cs.sweep_seeds(None, 1_790_000_000)
    assert [(i["lo"], i["hi"]) for i in items[:2]] == [(PM - 3600, PM - 3600), (PM, PM)]
    assert tiles(sorted((i["lo"], i["hi"]) for i in items))
    assert all(i["split_after"] == 1 and i["cursor"] is None for i in items)


# ----------------------------------------------------------------- plan ----

def test_plan_ranges_tile_balance_and_isolate_point_mass():
    ts = [100 + i for i in range(10)] + [500] * 7 + [900, 901, 902]
    plan = cs.plan_ranges(ts, target=4)
    assert tiles(plan) and cs.valid_plan(plan)
    assert sum(n for _, _, n in plan) == len(ts)
    assert [500, 500, 7] in plan
    assert all(n <= 4 for lo, hi, n in plan if lo != hi)


def test_plan_ranges_point_mass_first_and_last_and_empty():
    assert cs.plan_ranges([]) == [[cs.CLOSE_TS_MIN, cs.CLOSE_TS_MAX, 0]]
    assert cs.plan_ranges([0] * 5, target=3)[0] == [0, 0, 5]
    p = cs.plan_ranges([cs.CLOSE_TS_MAX] * 5, target=3)
    assert tiles(p) and p[-1] == [cs.CLOSE_TS_MAX, cs.CLOSE_TS_MAX, 5]


def test_valid_plan_rejects_gaps_overlaps_and_junk():
    good = cs.plan_ranges([10, 20, 30], target=1)
    assert cs.valid_plan(good)
    gap = [list(r) for r in good]
    gap[1][0] += 1
    assert not cs.valid_plan(gap)
    assert not cs.valid_plan([[0, 5, 1]])
    assert not cs.valid_plan([[0, cs.CLOSE_TS_MAX, 1.5]])
    assert not cs.valid_plan(None) and not cs.valid_plan("x")


def test_sweep_plan_cache_ttl_and_corruption():
    now = 1_790_000_000
    ms = [{"close_time": "2026-10-02T00:00:00Z"}] * 3 + [{"close_time": None}]
    assert cs.save_sweep_plan(ms, now)
    plan = cs.load_sweep_plan(now + 60)
    assert plan and tiles(plan) and sum(r[2] for r in plan) == 3
    assert cs.load_sweep_plan(now + cs.PLAN_TTL_S + 1) is None       # stale
    assert cs.load_sweep_plan(now - 10) is None                      # from the future
    with open(cs._cache_path("sweep_plan.json"), "w") as f:
        f.write("{corrupt")
    assert cs.load_sweep_plan(now) is None


def test_write_cache_never_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(cs, "CACHE_DIR", str(blocker / "sub"))
    assert cs.write_cache("a.json", {"a": 1}) is False
    assert cs.read_cache("a.json") is None


# ------------------------------------------------- ordered remainder ----

def test_ordered_remainder_and_halve():
    assert cs.ordered_remainder(0, [9, 9, 7, 5]) == 5
    assert cs.ordered_remainder(6, [9, 5]) == 6            # clamped to lo
    assert cs.ordered_remainder(0, [5, 7]) is None          # not descending
    assert cs.ordered_remainder(0, [9, None]) is None
    assert cs.ordered_remainder(0, []) is None
    assert cs.halve(0, 9) == ((0, 4), (5, 9))
    assert cs.halve(4, 5) == ((4, 4), (5, 5))
    assert cs.halve(5, 5) is None


def test_remainder_split_isolates_last_second_so_it_is_never_refetched_twice():
    assert cs.remainder_split(0, 9) == [(0, 3), (4, 8), (9, 9)]
    assert cs.remainder_split(4, 5) == [(4, 4), (5, 5)]
    assert cs.remainder_split(5, 5) is None and cs.remainder_split(6, 5) is None
    # the isolated [c, c] child can't split again, so a heavy second is re-paged once
    assert cs.remainder_split(9, 9) is None


def test_heavy_close_second_is_not_refetched_per_split(monkeypatch):
    now = 1_790_000_000
    ms = [{"ticker": f"H{i}", "_ts": now + 5000} for i in range(1500)]      # top-end mass
    ms += [{"ticker": f"L{i}", "_ts": now + i} for i in range(3000)]
    for m in ms:
        m["close_time"] = iso(m["_ts"])
    fake = FakeListing(ms)
    monkeypatch.setattr(cs, "http_get_json", fake)
    monkeypatch.setattr(cs, "load_sweep_plan", lambda now_ts: [(cs.CLOSE_TS_MIN, cs.CLOSE_TS_MAX, 0)])
    monkeypatch.setattr(cs, "RESPLIT_AFTER_PAGES", 1)
    st = cs.kalshi_open_markets(now, [], time.time() + 20).stats
    assert st["coverage_complete"] and st["markets_unique"] == len(ms)
    assert st["markets_scanned"] <= len(ms) + 1500            # the mass is re-paged at most once


# ------------------------------------------------------ fake listing ----

def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class FakeListing:
    """GET /markets over canned markets: close-ts filters, close time
    descending, offset cursors. fail(lo, hi) -> True makes that call fail."""

    def __init__(self, markets, fail=None, delay=0.0):
        self.markets = sorted(markets, key=lambda m: -m["_ts"])
        self.fail, self.delay, self.calls = fail, delay, 0
        self.lock = threading.Lock()

    def __call__(self, url, params=None, **kw):
        with self.lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        lo, hi = params["min_close_ts"], params["max_close_ts"]
        if self.fail and self.fail(lo, hi):
            raise RuntimeError("HTTP 503 fake")
        rows = [m for m in self.markets if lo <= m["_ts"] <= hi]
        off = int(params.get("cursor") or 0)
        page = rows[off:off + params["limit"]]
        nxt = off + len(page)
        return {"markets": page, "cursor": str(nxt) if nxt < len(rows) else ""}


def universe(now, n=4500, mass=2600):
    ms = [{"ticker": f"M{i}", "_ts": now + 600 * i} for i in range(n)]
    ms += [{"ticker": f"P{i}", "_ts": PM} for i in range(mass)]
    for m in ms:
        m["close_time"] = iso(m["_ts"])
    return ms


def test_fetch_range_ordered_split_covers_unseen_remainder(monkeypatch):
    now = 1_790_000_000
    ms = universe(now, n=2500, mass=0)
    monkeypatch.setattr(cs, "http_get_json", FakeListing(ms))
    lo, hi = now, now + 600 * 2500
    out, more, rest, err, pages = cs.fetch_range(cs._item(lo, hi, 1), time.time() + 30,
                                                 cs._Counter(99), cs._Counter(99))
    assert pages == 1 and len(out) == 1000 and rest is None and err is None
    c = min(m["_ts"] for m in out)
    assert [(i["lo"], i["hi"]) for i in more] == cs.remainder_split(lo, c)
    assert tiles([(i["lo"], i["hi"]) for i in more], lo, c)
    seen = {m["ticker"] for m in out}
    left = {m["ticker"] for m in ms if lo <= m["_ts"] <= c}
    assert seen | left == {m["ticker"] for m in ms}             # nothing falls between


def test_fetch_range_budget_and_failure_return_unfinished(monkeypatch):
    now = 1_790_000_000
    ms = universe(now, n=2500, mass=0)
    monkeypatch.setattr(cs, "http_get_json", FakeListing(ms))
    item = cs._item(now, now + 600 * 2500, 8)
    out, more, rest, err, pages = cs.fetch_range(item, time.time() - 1, cs._Counter(9), cs._Counter(9))
    assert (out, more, pages) == ([], [], 0) and rest == item and "budget" in err
    calls = {"n": 0}
    fake = FakeListing(ms)

    def flaky(url, params=None, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("HTTP 503 fake")
        return fake(url, params, **kw)
    monkeypatch.setattr(cs, "http_get_json", flaky)
    out, more, rest, err, pages = cs.fetch_range(item, time.time() + 30, cs._Counter(9), cs._Counter(9))
    c = min(m["_ts"] for m in out)
    assert pages == 1 and "503" in err and (rest["lo"], rest["hi"], rest["cursor"]) == (now, c, None)


def test_fetch_range_unsorted_rows_fall_back_to_full_range_split(monkeypatch):
    now = 1_790_000_000
    ms = universe(now, n=2500, mass=0)
    fake = FakeListing(ms)
    fake.markets.reverse()                                       # ascending: no shortcut
    monkeypatch.setattr(cs, "http_get_json", fake)
    lo, hi = now, now + 600 * 2500
    _, more, rest, _, _ = cs.fetch_range(cs._item(lo, hi, 1), time.time() + 30,
                                         cs._Counter(9), cs._Counter(9))
    assert rest is None and tiles([(i["lo"], i["hi"]) for i in more], lo, hi)


# ------------------------------------------------------ sweep coverage ----

STATS_KEYS = {"markets_scanned", "markets_unique", "pages_fetched", "ranges_completed",
              "ranges_retried", "ranges_budget_hit", "sweep_seconds", "sweep_plan",
              "coverage_complete"}


def test_sweep_complete_saves_plan_then_reuses_it(monkeypatch):
    now = int(time.time())
    ms = universe(now)
    fake = FakeListing(ms)
    monkeypatch.setattr(cs, "http_get_json", fake)
    errors = []
    got = cs.kalshi_open_markets(now, errors, time.time() + 20)
    st = got.stats
    assert STATS_KEYS <= set(st) and st["coverage_complete"] is True and errors == []
    assert {m["ticker"] for m in got} == {m["ticker"] for m in ms}
    assert st["markets_unique"] == len(ms) <= st["markets_scanned"]
    assert st["ranges_budget_hit"] == [] and st["sweep_plan"] == "seeded" and st["plan_saved"]
    assert st["pages_fetched"] == fake.calls
    again = cs.kalshi_open_markets(now, errors, time.time() + 20).stats
    assert again["sweep_plan"] == "cached" and again["coverage_complete"] is True
    assert again["markets_unique"] == len(ms) and again["markets_scanned"] == len(ms)


def test_sweep_rescue_pass_recovers_failed_ranges(monkeypatch):
    now = int(time.time())
    ms = universe(now)
    first = {"done": False}

    def fail(lo, hi):              # the point mass fails throughout the main pass
        return lo == hi == PM and not first["done"]
    fake = FakeListing(ms, fail=fail)
    monkeypatch.setattr(cs, "RESCUE_BUDGET_S", 20)
    orig = cs._work_queue

    def wq(items, fn, workers=cs.LISTING_WORKERS):
        res = orig(items, fn, workers)
        first["done"] = True
        return res
    monkeypatch.setattr(cs, "_work_queue", wq)
    monkeypatch.setattr(cs, "http_get_json", fake)
    errors = []
    st = cs.kalshi_open_markets(now, errors, time.time() + 20).stats
    assert st["ranges_retried"] == 1 and st["coverage_complete"] is True
    assert st["markets_unique"] == len(ms) and errors == []


def test_sweep_unrecoverable_range_is_explicit_never_silent(monkeypatch):
    now = int(time.time())
    ms = universe(now)
    monkeypatch.setattr(cs, "RESCUE_BUDGET_S", 1)
    monkeypatch.setattr(cs, "http_get_json", FakeListing(ms, fail=lambda lo, hi: lo == hi == PM))
    errors = []
    got = cs.kalshi_open_markets(now, errors, time.time() + 20)
    st = got.stats
    assert st["coverage_complete"] is False and "plan_saved" not in st
    assert st["ranges_budget_hit"] == [e for e in errors if e.startswith("range (")]
    assert any(f"({PM}, {PM})" in r for r in st["ranges_budget_hit"])
    assert st["markets_unique"] == len(ms) - 2600
    assert cs.load_sweep_plan(now) is None                   # partial sweeps never become plans
    line = cs.coverage_line(st, False)
    assert line.startswith("combo_scan sweep INCOMPLETE: 1 range(s) unscanned") and "\n" not in line


def test_sweep_budget_hit_with_zero_time_reports_every_range(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(cs, "RESCUE_BUDGET_S", 0)
    monkeypatch.setattr(cs, "http_get_json", FakeListing(universe(now)))
    errors = []
    st = cs.kalshi_open_markets(now, errors, time.time() - 1).stats
    seeds = cs.sweep_seeds(None, now)
    assert st["coverage_complete"] is False and st["markets_unique"] == 0
    assert len(st["ranges_budget_hit"]) == len(seeds)
    assert all("budget hit after 0 pages" in r for r in st["ranges_budget_hit"])


# -------------------------------------------------------- fee mult cache ----

def stub_fetch(monkeypatch, values):
    calls = []

    def fetch(s):
        calls.append(s)
        if s not in values:
            return 1.0                       # failed fetch: not recorded, never cached
        cs._FEE_MULT_LIVE[s] = values[s]
        return values[s]
    monkeypatch.setattr(cs, "kalshi_fee_multiplier", fetch)
    return calls


def test_fee_cache_hits_within_ttl_and_refetches_after(monkeypatch):
    now = 1_790_000_000
    calls = stub_fetch(monkeypatch, {"KXA": 2.0, "KXB": 1.0})
    st = {}
    assert cs.fee_multipliers(["KXA", "KXB"], now, st) == {"KXA": 2.0, "KXB": 1.0}
    assert calls == ["KXA", "KXB"] and st == {"fee_mult_cache_hits": 0, "fee_mult_fetched": 2}
    calls.clear()
    assert cs.fee_multipliers(["KXA", "KXB"], now + 3600, st) == {"KXA": 2.0, "KXB": 1.0}
    assert calls == [] and st["fee_mult_cache_hits"] == 2
    cs._FEE_MULT_LIVE.clear()
    assert cs.fee_multipliers(["KXA"], now + cs.FEE_MULT_TTL_S + 1) == {"KXA": 2.0}
    assert calls == ["KXA"]


def test_fee_cache_never_caches_failures_and_floors_at_one(monkeypatch):
    now = 1_790_000_000
    calls = stub_fetch(monkeypatch, {})
    assert cs.fee_multipliers(["KXDOWN"], now) == {"KXDOWN": 1.0}
    assert cs.read_cache("series_fee_mult.json") is None
    cs.write_cache("series_fee_mult.json", {"series": {"KXLOW": {"mult": 0.25, "ts": now},
                                                       "KXBAD": {"mult": "x", "ts": now}}})
    calls.clear()
    assert cs.fee_multipliers(["KXLOW", "KXBAD"], now) == {"KXLOW": 1.0, "KXBAD": 1.0}
    assert calls == ["KXBAD"]


def test_fee_cache_corrupt_or_unwritable_falls_back_to_live(monkeypatch, tmp_path):
    now = 1_790_000_000
    calls = stub_fetch(monkeypatch, {"KXA": 3.0})
    os.makedirs(cs.CACHE_DIR, exist_ok=True)
    with open(cs._cache_path("series_fee_mult.json"), "w") as f:
        f.write("[not a dict")
    assert cs.fee_multipliers(["KXA"], now) == {"KXA": 3.0} and calls == ["KXA"]
    blocker = tmp_path / "blk"
    blocker.write_text("x")
    monkeypatch.setattr(cs, "CACHE_DIR", str(blocker / "c"))
    assert cs.fee_multipliers(["KXA"], now) == {"KXA": 3.0}


def test_fee_multiplier_failure_is_recorded_and_cleared_by_success(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("timeout")
    monkeypatch.setattr(cs, "http_get_json", boom)
    assert cs.kalshi_fee_multiplier("KXDOWN") == 1.0
    assert cs._FEE_MULT_FAILED == {"KXDOWN"} and "KXDOWN" not in cs._FEE_MULT_LIVE
    monkeypatch.setattr(cs, "http_get_json", lambda *a, **k: {"series": {"fee_multiplier": 2}})
    assert cs.kalshi_fee_multiplier("KXDOWN") == 2.0
    assert cs._FEE_MULT_FAILED == set() and cs._FEE_MULT_LIVE["KXDOWN"] == 2.0


def test_fee_multipliers_reports_unknown_misses_only(monkeypatch):
    now = 1_790_000_000
    cs.write_cache("series_fee_mult.json", {"series": {"KXHIT": {"mult": 1.0, "ts": now}}})

    def fake_get(url, *a, **k):
        if url.endswith("/KXDOWN"):
            raise RuntimeError("timeout")
        return {"series": {"fee_multiplier": 1}}
    monkeypatch.setattr(cs, "http_get_json", fake_get)
    cs._FEE_MULT_FAILED.add("KXHIT")   # stale failure on a cache hit is not unknown
    st = {}
    assert cs.fee_multipliers(["KXHIT", "KXOK", "KXDOWN"], now, st) == \
        {"KXHIT": 1.0, "KXOK": 1.0, "KXDOWN": 1.0}
    assert st["fee_mult_unknown"] == ["KXDOWN"] and st["fee_mult_fetched"] == 2
    st = {}
    cs.fee_multipliers(["KXOK"], now, st)
    assert "fee_mult_unknown" not in st


def test_drop_unknown_fee_mult_drops_only_kalshi_legs_in_unknown_series():
    k_bad = {"legs": [{"ticker": "KXDOWN-26OCT02-T1", "side": "yes"},
                      {"ticker": "KXOK-26OCT02-T1", "side": "no"}]}
    k_ok = {"legs": [{"ticker": "KXOK-26OCT02-T2", "side": "yes"}]}
    poly = {"legs": [{"ticker": "KXDOWN-26OCT02-T1", "venue": "polymarket", "token": "t"},
                     {"ticker": "KXOK-26OCT02-T3", "venue": "kalshi"}]}
    kept, n = cs._drop_unknown_fee_mult([k_bad, k_ok, poly], {"KXDOWN"})
    assert kept == [k_ok, poly] and n == 1
    assert cs._drop_unknown_fee_mult([k_bad], set()) == ([k_bad], 0)


# ------------------------------------------------------------ sessions ----

def test_sessions_are_per_host_and_per_thread():
    k1 = cs._host_session(cs.KALSHI_API + "/markets")
    assert cs._host_session(cs.KALSHI_API + "/series/X") is k1
    g, c = cs._host_session(cs.GAMMA_API + "/events"), cs._host_session(cs.CLOB_API + "/book")
    assert len({id(k1), id(g), id(c)}) == 3
    other = []
    t = threading.Thread(target=lambda: other.append(cs._host_session(cs.KALSHI_API)))
    t.start()
    t.join()
    assert other[0] is not k1


def test_http_get_json_stops_retrying_at_deadline(monkeypatch):
    calls = []

    class R:
        status_code, headers = 503, {}

    class S:
        def get(self, url, params=None, timeout=None):
            calls.append(url)
            return R()
    monkeypatch.setattr(cs, "_host_session", lambda url: S())
    t = time.time()
    with pytest.raises(RuntimeError):
        cs.http_get_json("https://x/y", retries=4, deadline=time.time() + 0.1)
    assert len(calls) == 1 and time.time() - t < 1


# ----------------------------------------------------- verify-before-alert ----

ARGS = SimpleNamespace(max_outlay_cents=2000, min_outlay_cents=1000, min_roi=0.0)


def finalized(ticker, yes, no, near_arb=False):
    cand = cs.screen_type_a([listing(ticker, yes, no)], near_arb=near_arb)[0]
    out = cs.finalize(cand, {ticker: {"yes": (yes * 100, 500.0), "no": (no * 100, 500.0)}},
                      {}, ARGS, "t0")
    out["_cand"] = cand
    return out


def test_reverify_keeps_live_drops_vanished_and_skips_near_arbs(monkeypatch):
    live, gone = finalized("LIVE", 40, 50), finalized("GONE", 40, 50)
    near = finalized("NEAR", 49, 50, near_arb=True)
    books = {"LIVE": {"yes": (4000, 500.0), "no": (5000, 500.0)},
             "GONE": {"yes": (5000, 500.0), "no": (5000, 500.0)}}
    fetched = []
    monkeypatch.setattr(cs, "kalshi_orderbook_top", lambda t: fetched.append(t) or books[t])
    errors, st = [], {}
    kept, dropped = cs.reverify_new([live, gone, near], ARGS, errors, "t1", time.time() + 5, st)
    assert dropped == [gone] and sorted(fetched) == ["GONE", "LIVE"]
    assert [c["legs"][0]["ticker"] for c in kept] == ["LIVE", "NEAR"]
    assert kept[0]["depth_reverified"] is True and kept[0]["detected_at"] == "t0"
    assert "depth_reverified" not in kept[1] and errors == []
    assert st == {"reverify_checked": 2, "reverify_dropped": 1, "reverify_unverified": 0}


def test_reverify_shallow_book_drops_below_min_outlay(monkeypatch):
    c = finalized("THIN", 40, 50)
    monkeypatch.setattr(cs, "kalshi_orderbook_top",
                        lambda t: {"yes": (4000, 5.0), "no": (5000, 500.0)})
    kept, dropped = cs.reverify_new([c], ARGS, [], "t1", time.time() + 5)
    assert kept == [] and dropped == [c]


def test_reverify_timeout_drops_unverified_combo(monkeypatch):
    c = finalized("SLOW", 40, 50)
    monkeypatch.setattr(cs, "kalshi_orderbook_top", lambda t: pytest.fail("past deadline"))
    errors, st = [], {}
    kept, dropped = cs.reverify_new([c], ARGS, errors, "t1", time.time() - 1, st)
    assert kept == [] and dropped == [c]
    assert st == {"reverify_checked": 1, "reverify_dropped": 0, "reverify_unverified": 1}
    assert any("dropped unverified" in e for e in errors)


def test_reverify_missing_book_drops_locked_combo(monkeypatch):
    live, broken = finalized("LIVE", 40, 50), finalized("BROKEN", 40, 50)

    def book(t):
        if t == "BROKEN":
            raise RuntimeError("HTTP 503")
        return {"yes": (4000, 500.0), "no": (5000, 500.0)}
    monkeypatch.setattr(cs, "kalshi_orderbook_top", book)
    errors, st = [], {}
    kept, dropped = cs.reverify_new([live, broken], ARGS, errors, "t1", time.time() + 5, st)
    assert dropped == [broken] and [c["legs"][0]["ticker"] for c in kept] == ["LIVE"]
    assert all(c["legs"][0]["ticker"] != "BROKEN" for c in kept)
    assert st["reverify_unverified"] == 1 and st["reverify_dropped"] == 0
    assert any("reverify orderbook BROKEN" in e for e in errors)
    assert any("1 new combo(s) dropped unverified" in e for e in errors)


def test_reverify_unverified_drop_leaves_near_arb_untouched(monkeypatch):
    near = finalized("NEAR", 49, 50, near_arb=True)
    slow = finalized("SLOW", 40, 50)
    monkeypatch.setattr(cs, "kalshi_orderbook_top", lambda t: pytest.fail("past deadline"))
    st = {}
    kept, dropped = cs.reverify_new([near, slow], ARGS, [], "t1", time.time() - 1, st)
    assert kept == [near] and kept[0] is near and "depth_reverified" not in near
    assert dropped == [slow] and st["reverify_checked"] == 1


def test_reverify_all_books_present_keeps_verified(monkeypatch):
    a, b = finalized("ONE", 40, 50), finalized("TWO", 41, 50)
    monkeypatch.setattr(cs, "kalshi_orderbook_top",
                        lambda t: {"yes": (4000, 500.0), "no": (5000, 500.0)})
    errors, st = [], {}
    kept, dropped = cs.reverify_new([a, b], ARGS, errors, "t1", time.time() + 5, st)
    assert dropped == [] and len(kept) == 2 and errors == []
    assert all(c["depth_reverified"] is True for c in kept)
    assert st == {"reverify_checked": 2, "reverify_dropped": 0, "reverify_unverified": 0}


# --------------------------------------------------------------- main ----

def test_main_output_has_coverage_fields_and_strips_internals(monkeypatch, tmp_path):
    d = run_main_stubbed(monkeypatch, tmp_path, ["--no-mark"])
    assert "coverage_complete" in d and isinstance(d["stats"], dict)
    assert all("_cand" not in c for c in d["new_combos"])
    locked = [c for c in d["new_combos"] if c["tier"] == "locked"]
    edge_c = [c for c in d["new_edge_combos"] if c["combo_type"] == "C"]
    edge_a = [c for c in d["new_edge_combos"] if c["combo_type"] == "A"]
    assert all(c["depth_reverified"] is True for c in locked + edge_c)
    assert all("depth_reverified" not in c for c in edge_a)


def test_main_with_sweep_stats_reports_complete(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "STATE_PATH", str(tmp_path / "s.json"))
    res = cs.SweepResult([listing("X", 50, 51)])
    res.stats = {"markets_scanned": 1, "markets_unique": 1, "pages_fetched": 1,
                 "ranges_completed": 1, "ranges_retried": 0, "ranges_budget_hit": [],
                 "sweep_seconds": 0.1, "sweep_plan": "cached", "coverage_complete": True}
    monkeypatch.setattr(cs, "kalshi_open_markets", lambda now_ts, errors, deadline: res)
    monkeypatch.setattr(cs, "poly_markets", lambda: [])
    buf, err = io.StringIO(), io.StringIO()
    from contextlib import redirect_stderr
    with redirect_stdout(buf), redirect_stderr(err):
        cs.main(["--no-mark"])
    d = json.loads(buf.getvalue())
    assert d["coverage_complete"] is True and d["stats"]["markets_unique"] == 1
    assert STATS_KEYS <= set(d["stats"])
    assert err.getvalue().startswith("combo_scan sweep complete | unique 1")


def test_main_drops_vanished_combo_and_does_not_stamp_it(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "STATE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(cs, "kalshi_open_markets",
                        lambda now_ts, errors, deadline: [listing("LOCK", 40, 50)])
    monkeypatch.setattr(cs, "poly_markets", lambda: [])
    monkeypatch.setattr(cs, "kalshi_fee_multiplier", lambda s: 1.0)
    seq = iter([{"yes": (4000, 500.0), "no": (5000, 500.0)},     # sizing fetch: live
                {"yes": (5000, 500.0), "no": (5000, 500.0)}])    # re-check: gone
    monkeypatch.setattr(cs, "kalshi_orderbook_top", lambda t: next(seq))
    buf = io.StringIO()
    with redirect_stdout(buf):
        cs.main([])
    d = json.loads(buf.getvalue())
    assert d["new_combos"] == [] and d["combos_found"] == 0
    assert d["stats"]["reverify_dropped"] == 1
    with open(cs.STATE_PATH) as f:
        assert json.load(f)["alerted"] == {}


def test_main_drops_unverifiable_combo_and_does_not_stamp_it(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "STATE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(cs, "kalshi_open_markets",
                        lambda now_ts, errors, deadline: [listing("LOCK", 40, 50)])
    monkeypatch.setattr(cs, "poly_markets", lambda: [])
    monkeypatch.setattr(cs, "kalshi_fee_multiplier", lambda s: 1.0)
    calls = []

    def book(t):
        calls.append(t)
        if len(calls) > 1:                                          # re-check fetch fails
            raise RuntimeError("HTTP 503")
        return {"yes": (4000, 500.0), "no": (5000, 500.0)}         # sizing fetch: live
    monkeypatch.setattr(cs, "kalshi_orderbook_top", book)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cs.main([])
    d = json.loads(buf.getvalue())
    assert d["new_combos"] == [] and d["combos_found"] == 0
    assert d["stats"]["reverify_unverified"] == 1 and d["stats"]["reverify_dropped"] == 0
    assert any("dropped unverified" in e for e in d["errors"])
    with open(cs.STATE_PATH) as f:
        assert json.load(f)["alerted"] == {}
