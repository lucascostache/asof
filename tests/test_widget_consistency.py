"""The showcase widgets all come from ONE fixture (tests/fixtures/widgets/generate.py), so what one widget counts the others count too: rows, points, events and what is after T.
These tests read the committed fixtures and check the numbers against each other, and against a count made straight from the returns rows (not from any widget)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from asof.widgets import compute

FIX = Path(__file__).parent / "fixtures" / "widgets"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


@pytest.fixture(scope="module")
def F():
    return {n: load(n) for n in ("receipt", "timeline", "quality", "chain", "returns", "rolling-sharpe", "leak-overlay", "attribution")}


def test_every_widget_counts_the_same_rows(F):
    rows, T = F["returns"], F["timeline"]["as_of"]
    n = len(rows)
    after_t = sum(1 for r in rows if r["available_at"] > T)                        # counted straight from the rows
    moments = len({r["available_at"] for r in rows})                                # 504 business days of data + 40 rows loaded twice = 544 rows; a copy becomes knowable at the same moment as its original
    assert n == 544 and moments == 504 and 0 < after_t < n
    assert F["quality"]["n_records"] == n
    assert len(F["timeline"]["events"]) == n
    assert F["receipt"]["served"] + sum(F["receipt"]["blocked"].values()) == n                                             # every row is accounted for: served, withheld by the clock, or superseded
    seen, repeats_at_t = set(), 0
    for r in rows:
        repeats_at_t += r["record_id"] in seen and r["available_at"] <= T
        seen.add(r["record_id"])
    assert F["receipt"]["blocked"]["superseded"] == repeats_at_t > 0                                                         # the repeated copies visible at T are not served twice: they are counted as superseded
    assert len(F["rolling-sharpe"]["points"]) == len(F["leak-overlay"]["locked"]) == len(F["leak-overlay"]["unlocked"]) == F["leak-overlay"]["summary"]["n_points"] == moments    # one point per business day of the data (which is also the number of distinct rows)


def _business_days(first: str, last: str) -> int:
    """Monday-to-Friday days from ``first`` to ``last``, both included. Counted from the calendar, not from the fixture."""
    from datetime import date, timedelta
    a, b = date.fromisoformat(first[:10]), date.fromisoformat(last[:10])
    return sum(1 for k in range((b - a).days + 1) if (a + timedelta(days=k)).weekday() < 5)


@pytest.mark.parametrize("name,key", [("rolling-sharpe", "points"), ("leak-overlay", "locked"), ("leak-overlay", "unlocked")])
def test_the_number_of_chart_points_is_the_number_of_business_days_between_the_first_and_last_date_label(F, name, key):
    pts = F[name][key]
    first, last = pts[0]["t"], pts[-1]["t"]                                                # the two dates the chart prints under its axis
    assert len(pts) == _business_days(first, last) == 504
    assert all(_t(p["t"]).weekday() < 5 for p in pts) and len({p["t"][:10] for p in pts}) == len(pts)      # every point is a weekday, and no day has two
    days = sorted({r["event_time"][:10] for r in F["returns"]})
    assert [p["t"][:10] for p in pts] == days and (first[:10], last[:10]) == ("2021-01-04", "2022-12-08")  # exactly the business days of the data, first to last


def test_the_showcase_rows_are_business_days_so_annualising_with_252_is_right(F):
    days = [_t(r["event_time"]) for r in F["returns"]]
    assert {d.weekday() for d in days} <= {0, 1, 2, 3, 4}                                  # Monday to Friday only: no weekend rows
    uniq = sorted({d.date() for d in days})
    gaps = {(b - a).days for a, b in zip(uniq, uniq[1:], strict=False)}
    assert gaps == {1, 3}                                                                  # one day, or Friday to Monday: nothing else
    assert len(uniq) == 504                                                                # 504 business days of data = two years of 252-period annualisation
    for k in ("rolling-sharpe", "leak-overlay"):
        assert F[k]["periods_per_year"] == 252 and F[k]["calendar"] == "one return per business day, Mon–Fri"
    a = F["attribution"]["annualisation"]
    assert a == {"periods_per_year": 252, "calendar": "one return per business day, Mon–Fri"}


def test_what_is_after_T_agrees_across_the_receipt_the_clocks_and_the_points(F):
    rows, T = F["returns"], F["timeline"]["as_of"]
    after_t = sum(1 for r in rows if r["available_at"] > T)
    assert F["receipt"]["blocked"]["future"] == after_t                             # the receipt: withheld because available after T
    assert sum(1 for e in F["timeline"]["events"] if e["available_at"] > T) == after_t       # the three clocks: the dots after the T line
    days_after = sum(1 for d in {r["event_time"][:10] for r in rows} if d + "T23:00:00Z" > T)        # business days of the data whose close is after T
    assert sum(1 for p in F["rolling-sharpe"]["points"] if p["t"] > T) == sum(1 for p in F["leak-overlay"]["locked"] if p["t"] > T) == days_after > 0     # the chart points after T are those days
    assert days_after != after_t                                                    # a different count from the rows withheld at T, as it should be: late rows of earlier days are withheld too
    assert F["receipt"]["as_of"] == T and F["attribution"]["as_of"] == T


def test_the_timeline_is_the_receipts_rows_with_the_same_times(F):
    by_id = {r["record_id"]: r for r in F["returns"]}
    ev = F["timeline"]["events"]
    assert [(e["event_time"], e["available_at"]) for e in ev] == [(r["event_time"], r["available_at"]) for r in F["returns"]]
    assert all(e["recorded_at"] == F["receipt"]["recorded_at"] for e in ev)       # all recorded by the one query
    assert set(F["receipt"]["served_ids"]) <= set(by_id) and all(by_id[i]["available_at"] <= F["timeline"]["as_of"] for i in F["receipt"]["served_ids"])


def test_the_receipt_and_the_quality_panel_report_the_same_flags(F):
    panel = sorted({i["flag"].split(": ")[-1] for i in F["quality"]["issues"]})
    assert panel == ["duplicate_rows", "late_availability"]
    assert sorted(F["receipt"]["quality"]["source"]["flags"]) == panel and F["receipt"]["quality"]["flags"] == ["tied_versions"]          # the receipt's own flag: the copies the panel counts as duplicate rows are versions with identical availability
    assert F["receipt"]["quality"]["tied_versions"] == F["receipt"]["blocked"]["superseded"]


def test_the_ledger_counts_agree(F):
    assert F["chain"]["ok"] is True and F["receipt"]["ledger_seq"] <= F["chain"]["n_events"]
    assert F["receipt"]["session_id"] == "fixture-session"


def test_rolling_sharpe_is_the_locked_line_of_the_overlay_at_every_point(F):
    assert F["rolling-sharpe"]["points"] == F["leak-overlay"]["locked"]
    assert F["rolling-sharpe"]["window"] == F["leak-overlay"]["window"] == 63


def test_the_overlay_and_the_sharpe_match_a_fresh_computation_from_the_rows(F):
    grid = sorted({r["event_time"][:10] + "T23:00:00Z" for r in F["returns"]})
    cal = "one return per business day, Mon–Fri"
    assert compute.rolling_sharpe(F["returns"], window=63, at=grid, synthetic=True, calendar=cal) == F["rolling-sharpe"]
    assert compute.leak_overlay(F["returns"], window=63, at=grid, synthetic=True, calendar=cal) == F["leak-overlay"]


def test_the_quality_report_flags_exactly_the_planted_late_rows(F):
    late = [i for i in F["quality"]["issues"] if i["flag"].endswith("late_availability")]
    lagged = [r for r in F["returns"] if (_t(r["available_at"]) - _t(r["event_time"])).days >= 30]
    assert len(late) == 1 and late[0]["n"] == len(lagged) > 150                       # counted from the rows, not from the check: every third row (and the copies of those) is published 120 days late
    seen, repeats = set(), 0
    for r in F["returns"]:
        repeats += r["record_id"] in seen
        seen.add(r["record_id"])
    dup = [i for i in F["quality"]["issues"] if i["flag"].endswith("duplicate_rows")]
    assert len(dup) == 1 and dup[0]["n"] == repeats == 40                            # and the repeated copies, counted from the rows too


def _t(s):
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_the_gap_parts_add_up_to_the_gap_exactly(F):
    from decimal import Decimal
    a = F["attribution"]
    assert sum(Decimal(str(c["value"])) for c in a["components"]) == Decimal(str(a["gap"]))        # exact decimal arithmetic, not a tolerance
    assert a["reconciles"] is True


def _window_returns(rows, point, locked, skip=()):
    """The returns a run uses at ``point``: the last 252 (the gap's one-year window) it can see (locked: available_at <= point; unlocked: event_time <= point). Counted here from the rows (``skip`` = row positions to leave out), not by the code under test."""
    key = "available_at" if locked else "event_time"
    seen = sorted((i for i, r in enumerate(rows) if r[key] <= point and i not in skip), key=lambda i: (rows[i]["event_time"], rows[i]["available_at"], i))
    return [rows[i]["ret"] for i in seen[-252:]]


def test_the_measured_parts_rest_on_realistic_numbers_of_rows_and_no_single_row_drives_the_gap(F):
    a, rows = F["attribution"], F["returns"]
    b, rc = a["basis"], a["row_counts"]
    assert b["window"] == 252 and 20 <= rc["leakage"] <= 40 and 30 <= rc["data_issues"] <= 60 and rc["both"] == 0
    assert len(b["leakage_rows"]) == rc["leakage"] and len(b["data_issue_rows"]) == rc["data_issues"]
    parts = {c["id"]: c["value"] for c in a["components"]}
    worst = 0.0
    measure = lambda rs, ids: compute.measure_gap_effects(rs, at=b["point"], window=252, flagged_ids=ids, periods_per_year=252)      # noqa: E731
    assert measure(rows, b["flagged_ids"])["leakage_pp"] == parts["leakage"] and measure(rows, b["flagged_ids"])["data_issues_pp"] == parts["data_issues"]
    for i in b["leakage_rows"]:                                                               # one leaked row at a time: suppose it had been published on time
        fixed = [dict(r, available_at=r["event_time"]) if k == i else r for k, r in enumerate(rows)]
        worst = max(worst, abs(measure(fixed, b["flagged_ids"])["leakage_pp"] - parts["leakage"]))
    for i in b["data_issue_rows"]:                                                            # one bad row at a time: suppose that record had been loaded once
        once = [r for k, r in enumerate(rows) if k != i]
        worst = max(worst, abs(measure(once, [x for x in b["flagged_ids"] if x != rows[i]["record_id"]])["data_issues_pp"] - parts["data_issues"]))
    biggest = max(abs(r["ret"]) for r in rows)
    assert 0 < worst <= 2 * biggest * 100 + 1e-9                                              # the arithmetic bound: over a 252-row window one row is 1/252 of the annualised return, and putting it right swaps it for another row
    assert worst < 0.15 * abs(a["gap"]), worst                                                # so any ONE row moves a measured part by less than 15% of the gap (about 12% at worst; it was 39% when one row carried the leakage)
    assert abs(parts["leakage"]) > 2 * worst and abs(parts["data_issues"]) > 2 * worst           # and each part is the work of many rows, not of its single biggest one


def test_the_return_gap_is_additive_and_its_parts_are_measured_from_the_rows(F):
    from statistics import fmean
    a, rows = F["attribution"], F["returns"]
    point = a["basis"]["point"]
    assert a["unit"] == "pp" and a["metric"] == "annualised return"                         # parts that add up are in a return, never in Sharpe units
    ann = lambda v: fmean(v) * a["annualisation"]["periods_per_year"] * 100                 # noqa: E731
    parts = {c["id"]: c for c in a["components"]}
    seen, repeats = set(), set()
    for i, r in enumerate(rows):                                                              # the repeated copies of a record: the data-quality report flags them as duplicate rows
        if r["record_id"] in seen:
            repeats.add(i)
        seen.add(r["record_id"])
    used, ahead, tidy = _window_returns(rows, point, True), _window_returns(rows, point, False), _window_returns(rows, point, True, skip=repeats)
    assert parts["leakage"]["value"] == round(ann(used) - ann(ahead), 4) and parts["data_issues"]["value"] == round(ann(used) - ann(tidy), 4)
    assert point <= a["as_of"] and not any(p["t"] <= a["as_of"] and p["t"] > point for p in F["leak-overlay"]["locked"])       # the last point at or before T
    assert parts["leakage"]["value"] < 0 and parts["leakage"]["label"] == parts["data_issues"]["label"] == "OBSERVED" and parts["unexplained"]["label"] == "UNKNOWN"
    o = compute.leak_overlay(rows, window=252, at=[point])                                    # the overlay's own calculation, over the gap's one-year window
    assert o["unlocked"][0]["v"] > o["locked"][0]["v"]                                        # it says the same thing in Sharpe: the run that ignores publication delay looks better


def test_the_sharpe_gap_is_one_headline_number_not_a_set_of_parts(F):
    from statistics import stdev
    a, rows = F["attribution"], F["returns"]
    assert not any("sharpe" in str(c).lower() for c in a["components"]) and a["headline"]["sharpe_gap"] < 0
    vol = stdev(_window_returns(rows, a["basis"]["point"], True)) * a["annualisation"]["periods_per_year"] ** 0.5
    assert a["headline"]["volatility_pct"] == round(vol * 100, 2)                              # the volatility of the window used, recomputed from the rows, to the 2 decimals the widget prints
    assert a["headline"]["sharpe_gap"] == round(round(a["gap"], 2) / round(vol * 100, 2), 2)   # the tile is the two figures AS SHOWN divided, so it checks by hand
    assert abs(a["headline"]["sharpe_gap"] - a["gap"] / 100 / vol) < 0.01                      # and is within a rounding step of the unrounded ratio
    m = a["headline"]["method"]
    assert "not split into parts" in m and f"{a['gap']:.2f} ÷ {a['headline']['volatility_pct']:.2f}% = {a['headline']['sharpe_gap']:.2f}" in m


# ---------------------------------------------------------------- the receipt's counts are the receipt query's, at T
def _counts_at(rows, T):
    """What a point-in-time query at ``T`` must report, counted from the rows alone: record ids with a version available at T, rows not yet available, and the extra copies of a served id."""
    by_id: dict[str, int] = {}
    for r in rows:
        if r["available_at"] <= T:
            by_id[r["record_id"]] = by_id.get(r["record_id"], 0) + 1
    return {"served": len(by_id), "future": sum(1 for r in rows if r["available_at"] > T), "superseded": sum(n - 1 for n in by_id.values())}


def test_the_receipt_counts_are_the_rows_at_T_and_every_row_is_in_exactly_one_of_them(F):
    rows, rc = F["returns"], F["receipt"]
    want = _counts_at(rows, rc["as_of"])
    assert (rc["served"], rc["blocked"]["future"], rc["blocked"]["superseded"]) == (want["served"], want["future"], want["superseded"]) == (442, 62, 40)
    assert sum(want.values()) == len(rows) == 544 and len(rc["served_ids"]) == rc["served"]
    assert all(r["available_at"] <= rc["as_of"] for r in rows if r["record_id"] in set(rc["served_ids"]) and r["available_at"] <= rc["as_of"])


def test_the_receipt_does_not_borrow_its_numbers_from_the_sharpe_widget(F):
    """Before the charts were put on business days both happened to read 442 and 62 (rows served / withheld at T; points defined / blank). They are different things and now differ."""
    pts, rc = F["rolling-sharpe"]["points"], F["receipt"]
    defined, blank = sum(p["v"] is not None for p in pts), sum(p["v"] is None for p in pts)
    assert (defined, blank) == (413, 91) and (rc["served"], rc["blocked"]["future"]) == (442, 62)
    assert blank == next(i for i, p in enumerate(pts) if p["v"] is not None)                 # the blank points are the warm-up at the START (fewer than 63 rows available yet), nothing to do with T
    assert all(p["t"] < rc["as_of"] for p in pts[:blank])


@pytest.mark.parametrize("T", ["2021-06-01T00:00:00Z", "2022-03-15T12:00:00Z", "2022-10-24T00:00:00Z", "2023-06-01T00:00:00Z"])
def test_a_real_session_at_any_T_reports_the_rows_at_that_T(F, T, tmp_path):
    import importlib.util
    from asof import Session
    from asof.ledger import Ledger
    spec = importlib.util.spec_from_file_location("fx_generate_counts", FIX / "generate.py")
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    s = Session(T, ledger=Ledger(tmp_path / "l.db"), session_id="counts")
    s.add_source(g.Returns())
    s.query("returns")
    r = s.receipt().to_dict()
    s.close()
    want = _counts_at(F["returns"], T)
    assert (r["served"], r["blocked"]["future"], r["blocked"]["superseded"]) == (want["served"], want["future"], want["superseded"])
    assert r["as_of"] == T and r["served"] + sum(r["blocked"].values()) == len(F["returns"])


def test_the_lag_between_event_and_availability_is_recorded_and_matches_the_rows(F):
    rows, T = F["returns"], F["receipt"]["as_of"]
    lag = lambda r: int((_t(r["available_at"]) - _t(r["event_time"])).total_seconds())      # noqa: E731
    every = sorted(lag(r) for r in rows)
    assert F["rolling-sharpe"]["lag_seconds"] == F["leak-overlay"]["lag_seconds"] == {"median": every[len(every) // 2], "max": every[-1]}
    served = {}
    for r in rows:                                                                          # the receipt's lag is over what it served: one version per id available at T
        if r["available_at"] <= T:
            served[r["record_id"]] = lag(r)
    got = F["receipt"]["quality"]["lag_seconds"]
    assert got["max"] == max(served.values()) and got["min"] == min(served.values()) and got["max"] > 100 * 86400 > 86400 > got["median"]      # most rows lag 21 hours, a third lag 120 days
