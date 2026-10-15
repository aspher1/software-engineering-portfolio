"""Offline tests for scripts/analyze_capped_events.py and the shared
event_yes_legs helper it reuses from combo_scan."""

import json

import analyze_capped_events as ace
import combo_scan as cs


def listing(ticker, yes_ask, event="EV", **kw):
    m = {"ticker": ticker, "event_ticker": event, "title": ticker,
         "yes_ask_dollars": f"{yes_ask / 100:.4f}", "no_ask_dollars": "0.0000",
         "yes_ask_size_fp": "100.00", "yes_bid_size_fp": "100.00",
         "close_time": "2026-10-02T00:00:00Z"}
    m.update(kw)
    return m


def bracket_event(prices, event="EVB"):
    """less / between... / greater markets that tile the number line."""
    n = len(prices)
    ms = []
    for i, p in enumerate(prices):
        if i == 0:
            kw = {"strike_type": "less", "cap_strike": 10}
        elif i == n - 1:
            kw = {"strike_type": "greater", "floor_strike": 10 + 2 * (n - 2) - 1}
        else:
            kw = {"strike_type": "between", "floor_strike": 10 + 2 * (i - 1),
                  "cap_strike": 11 + 2 * (i - 1)}
        ms.append(listing(f"{event}-{i}", p, event=event, title="Temp on Oct 2, 2026?", **kw))
    return ms


def named_event(prices, event="EVN"):
    return [listing(f"{event}-{i}", p, event=event, title=f"Will team {i} win the 2026 cup?")
            for i, p in enumerate(prices)]


# ------------------------------------------------------- event_yes_legs ----

def test_event_yes_legs_matches_listing_asks():
    ms = bracket_event([30, 25, 40])
    assert cs.event_yes_legs(ms) == [
        {"ticker": "EVB-0", "side": "yes", "price_cents": 30},
        {"ticker": "EVB-1", "side": "yes", "price_cents": 25},
        {"ticker": "EVB-2", "side": "yes", "price_cents": 40}]
    ms[1]["yes_ask_dollars"] = "0.0000"
    assert cs.event_yes_legs(ms) is None
    ms[1]["yes_ask_dollars"] = "1.0000"
    assert cs.event_yes_legs(ms) is None


# ------------------------------------------------------- capped_events ----

def test_capped_events_agrees_with_screen_type_b_counter():
    cap = cs.MAX_EVENT_LEGS
    ms = (bracket_event([1] * (cap + 1), event="BIG") + bracket_event([1] * cap, event="MAX")
          + named_event([1] * (cap + 10), event="HUGE")
          + [listing(f"NOEV-{i}", 1, event=None) for i in range(cap + 5)])
    capped = ace.capped_events(ms)
    assert sorted(capped) == ["BIG", "HUGE"]
    assert len(capped["BIG"]) == cap + 1 and len(capped["HUGE"]) == cap + 10
    stats = {}
    cs.screen_type_b(ms, stats)
    assert stats["B_too_many_outcomes"] == len(capped)


# ------------------------------------------------------- analyze_event ----

def test_analyze_event_cheap_exhaustive_bracket_uses_screen_math():
    ms = bracket_event([1] * 41, event="BIG")
    r = ace.analyze_event("BIG", ms)
    assert r["exhaustive"] and r["all_quoted"] and r["price_screen_pass"]
    assert r["outcomes"] == 41 and r["yes_quoted"] == 41
    assert r["strike_types"] == "between+greater+less"
    assert r["title_pattern"] == "temp on oct #, #?"
    assert r["yes_ask_sum_cents"] == 41
    assert r["screen_edge_cents"] == round(cs.screen_edge_cents(cs.event_yes_legs(ms)), 4)


def test_analyze_event_fees_can_fail_a_sub_100_ask_sum():
    ms = bracket_event([2] * 40 + [19], event="FEE")       # 99c of asks, > 1c of fees
    r = ace.analyze_event("FEE", ms)
    assert r["yes_ask_sum_cents"] == 99 and r["exhaustive"] and r["all_quoted"]
    assert r["screen_edge_cents"] < 0 and not r["price_screen_pass"]


def test_analyze_event_missing_ask_never_passes():
    ms = bracket_event([1] * 41, event="GAP")
    ms[5]["yes_ask_dollars"] = "0.0000"
    r = ace.analyze_event("GAP", ms)
    assert not r["all_quoted"] and not r["price_screen_pass"] and r["yes_quoted"] == 40
    assert "screen_edge_cents" not in r and r["yes_ask_sum_cents"] == 40


def test_analyze_event_named_outcomes_are_not_exhaustive():
    r = ace.analyze_event("EVN", named_event([1] * 45))
    assert r["strike_types"] == "none" and not r["exhaustive"]
    assert r["price_screen_pass"]                         # cheap, but unprovable
    assert r["title_pattern"] == "will team # win the # cup?"


def test_analyze_event_bad_strike_fails_closed():
    ms = bracket_event([1] * 41, event="BAD")
    ms[3]["floor_strike"] = "n/a"
    assert ace.analyze_event("BAD", ms)["exhaustive"] is False


# ----------------------------------------------------------- summarize ----

def test_summarize_counts_and_buckets():
    recs = [ace.analyze_event("BIG", bracket_event([1] * 41, event="BIG")),
            ace.analyze_event("FEE", bracket_event([2] * 40 + [19], event="FEE")),
            ace.analyze_event("EVN", named_event([1] * 45)),
            ace.analyze_event("HUGE", named_event([1] * 250, event="HUGE"))]
    s = ace.summarize(recs)
    assert s["events"] == 4 and s["markets"] == 41 + 41 + 45 + 250
    assert (s["outcomes_min"], s["outcomes_median"], s["outcomes_max"]) == (41, 45, 250)
    assert s["outcome_buckets"] == {"41-50": 3, "201+": 1}
    assert s["exhaustive"] == 2 and s["all_quoted"] == 4
    assert s["price_screen_pass"] == 2 and s["exhaustive_and_pass"] == 1
    assert s["by_strike_types"]["between+greater+less"] == {
        "events": 2, "exhaustive": 2, "all_quoted": 2, "price_screen_pass": 1}
    assert [r["event_ticker"] for r in s["price_screen_passes"]] == ["BIG", "EVN"]
    assert [r["event_ticker"] for r in s["closest_to_pass"]] == ["BIG", "EVN", "FEE", "HUGE"]
    assert ace.summarize([])["events"] == 0


# ----------------------------------------------------------------- run ----

def test_run_writes_report_without_touching_the_sweep_plan(tmp_path, monkeypatch):
    original_save = cs.save_sweep_plan
    plan_writes = []
    monkeypatch.setattr(cs, "write_cache", lambda name, obj: plan_writes.append(name) or True)

    def fake_sweep(now_ts, errors, deadline):
        res = cs.SweepResult(bracket_event([1] * 41, event="BIG") + named_event([5] * 3))
        res.stats = {"sweep_seconds": 1.5, "coverage_complete": True,
                     "plan_saved": cs.save_sweep_plan(res, now_ts)}
        return res

    monkeypatch.setattr(cs, "kalshi_open_markets", fake_sweep)
    out = tmp_path / "sub" / "report.json"
    report = ace.run(str(out), 5)
    assert plan_writes == [] and cs.save_sweep_plan is original_save
    saved = json.loads(out.read_text())
    assert saved["markets_scanned"] == 44 and saved["coverage_complete"] is True
    assert saved["max_event_legs"] == cs.MAX_EVENT_LEGS
    assert saved["screen_type_b_too_many_outcomes"] == 1 == saved["summary"]["events"]
    assert [e["event_ticker"] for e in saved["events"]] == ["BIG"]
    assert saved == json.loads(json.dumps(report))
    assert "capped-events analysis" in ace.format_summary(report)
