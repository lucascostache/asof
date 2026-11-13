"""Look-ahead leakage and data issues are measured from DISJOINT sets of rows: a row is counted in one of them, never both.
leakage = rows used before their ``available_at``; data issues = wrong or revised values (here: the repeated copies of rows the quality report flags as duplicates)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from asof.widgets import compute

FIX = Path(__file__).parent / "fixtures" / "widgets"
D0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def iso(d: datetime) -> str:
    return d.isoformat().replace("+00:00", "Z")


def row(rid: str, day: int, ret: float, *, delay_days: int = 0) -> dict:
    ev = D0 + timedelta(days=day)
    return {"record_id": rid, "event_time": iso(ev), "available_at": iso(ev + timedelta(hours=18, days=delay_days)), "ret": ret}


def series(n: int = 12) -> list[dict]:
    return [row(f"r{i}", i, 0.001 * ((i % 5) - 2)) for i in range(n)]


# ---------------------------------------------------------------- the rule, on small hand-built rows
def test_leakage_rows_are_exactly_the_rows_the_unlocked_run_uses_before_they_are_available():
    rows = series()
    rows[9] = row("r9", 9, 0.02, delay_days=5)                      # event day 9, published on day 14
    m = compute.measure_gap_effects(rows, at=iso(D0 + timedelta(days=11, hours=23)), window=5)
    assert m["leakage_rows"] == [9] and m["data_issue_rows"] == []
    assert m["leakage_pp"] is not None and m["leakage_pp"] < 0       # the unlocked run used the big row early: it looks better than the locked one
    assert m["data_issues_pp"] == 0                                  # nothing flagged: no effect, not an unknown


def test_the_repeated_copy_of_a_flagged_row_is_the_data_issue_and_the_first_copy_is_kept():
    rows = series()
    rows[4]["ret"] = 0.05
    rows.insert(5, dict(rows[4]))                                    # r4 loaded twice, same values, same available_at
    m = compute.measure_gap_effects(rows, at=iso(D0 + timedelta(days=11, hours=23)), window=12, flagged_ids=["r4"])
    assert m["data_issue_rows"] == [5] and m["leakage_rows"] == []   # position 5 is the repeat; position 4 (the original) stays
    assert m["data_issues_pp"] != 0


def test_a_flagged_row_that_is_not_yet_published_is_leakage_only_never_also_a_data_issue():
    rows = series()
    late = row("r10", 10, -0.05, delay_days=9)                       # published on day 19, used by the unlocked run on day 11
    rows[10], rows[11] = late, row("r11", 11, 0.001)
    rows.append(dict(late))                                          # and loaded twice: the report flags it as a duplicate
    at = iso(D0 + timedelta(days=11, hours=23))
    m = compute.measure_gap_effects(rows, at=at, window=6, flagged_ids=["r10"])
    leak, data = set(m["leakage_rows"]), set(m["data_issue_rows"])
    assert leak == {10, 12} and data == set() and not (leak & data)
    assert m["data_issues_pp"] == 0                                  # not in the run the locked series uses, so it cannot move the data-issue effect
    # the same two sets worked out the naive way (every flagged row is a data issue, whether or not it was available) DO overlap: this test would catch that
    prep_sorted = sorted(range(len(rows)), key=lambda i: (rows[i]["event_time"], rows[i]["available_at"], i))
    unlocked = [i for i in prep_sorted if rows[i]["event_time"] <= at][-6:]
    naive_leak = {i for i in unlocked if rows[i]["available_at"] > at}
    naive_data = {i for i in unlocked if rows[i]["record_id"] == "r10"}
    assert naive_leak & naive_data == {10, 12}


@pytest.mark.parametrize("flag", [[], ["r3"], ["r3", "r8"], ["r10"]])
def test_the_two_sets_never_overlap_at_any_point_in_time(flag):
    rows = series(20)
    rows[10] = row("r10", 10, 0.03, delay_days=6)
    rows.insert(4, dict(rows[3]))
    rows.insert(12, dict(rows[10]))
    rows.insert(14, dict(rows[8]))
    for k in range(0, 30):
        m = compute.measure_gap_effects(rows, at=iso(D0 + timedelta(days=k, hours=23)), window=7, flagged_ids=flag)
        assert not (set(m["leakage_rows"]) & set(m["data_issue_rows"])), (flag, k)


# ---------------------------------------------------------------- the committed showcase fixture
@pytest.fixture(scope="module")
def F():
    return {n: json.loads((FIX / f"{n}.json").read_text()) for n in ("returns", "attribution", "quality")}


def test_no_row_counts_in_both_in_the_showcase_fixture(F):
    b, rc = F["attribution"]["basis"], F["attribution"]["row_counts"]
    leak, data = set(b["leakage_rows"]), set(b["data_issue_rows"])
    assert leak and data and not (leak & data)                       # both sets are non-empty, so "disjoint" is not vacuous
    assert rc == {"leakage": len(leak), "data_issues": len(data), "both": 0}


def test_the_showcase_sets_are_what_the_rows_say_counted_without_the_library(F):
    rows, b = F["returns"], F["attribution"]["basis"]
    p = b["point"]
    order = sorted(range(len(rows)), key=lambda i: (rows[i]["event_time"], rows[i]["available_at"], i))
    unlocked = [i for i in order if rows[i]["event_time"] <= p][-b["window"]:]
    locked = [i for i in order if rows[i]["available_at"] <= p][-b["window"]:]
    assert sorted(i for i in unlocked if rows[i]["available_at"] > p) == b["leakage_rows"]
    seen, repeats = set(), set()
    for i in order:
        if rows[i]["record_id"] in b["flagged_ids"] and rows[i]["record_id"] in seen:
            repeats.add(i)
        seen.add(rows[i]["record_id"])
    assert sorted(i for i in locked if i in repeats) == b["data_issue_rows"]
    assert all(rows[i]["record_id"] in b["flagged_ids"] for i in b["data_issue_rows"]) and all(rows[i]["available_at"] > p for i in b["leakage_rows"])


def test_the_showcase_never_overlaps_at_any_point_of_its_grid(F):
    rows, ids = F["returns"], F["attribution"]["basis"]["flagged_ids"]
    for t in sorted({r["available_at"] for r in rows})[::4]:
        m = compute.measure_gap_effects(rows, at=t, window=63, flagged_ids=ids)
        assert not (set(m["leakage_rows"]) & set(m["data_issue_rows"]))


def test_the_quality_report_flags_those_same_rows_as_duplicates(F):
    dup = [i for i in F["quality"]["issues"] if i["flag"].endswith("duplicate_rows")]
    assert len(dup) == 1 and dup[0]["n"] == len(F["attribution"]["basis"]["data_issue_rows"]) == 40
