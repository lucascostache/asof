"""Data-quality summaries (SPEC 1.2, section 4.5): one test per flag, a control for each (a clean source must not raise it), and the claims that matter most:
a per-query summary adds nothing about the future, nothing in it is a float or a record value, and an older ledger is reported as 'not recorded', never as clean."""
import json
import shutil
from datetime import timedelta, timezone
from pathlib import Path

import pytest

from asof import Ledger, quality as dq
from asof.receipt import receipt_from_ledger
from asof.records import Record
from asof.sources.base import RowSource
from conftest import ListSource, T, rec

UTC = timezone.utc
NOW = T("2023-03-01T00:00:00Z")
D0 = T("2023-01-01T00:00:00Z")


def day(i, hour=0):
    return D0 + timedelta(days=i, hours=hour)


def daily(n, *, start=0, available_hour=21, prefix="d", payload=None):
    return [Record("s", f"{prefix}{start + i}", day(start + i), day(start + i, available_hour), (payload or {"v": start + i}), order=i) for i in range(n)]


class Rows(RowSource):
    def __init__(self, rows, **kw):
        self._rows = rows
        super().__init__(name="s", event_time="event", available_at="avail", **kw)

    def rows(self):
        return iter(self._rows)


def src(records, now=NOW):
    return dq.analyse_source(records, now=now)


# ---------------------------------------------------------------- timestamp coverage
def test_coverage_counts_missing_event_and_available_times_and_a_clean_source_has_none():
    clean = src(daily(5))
    assert clean["event_time"]["n_missing"] == clean["available_at"]["n_missing"] == 0 and clean["flags"] == []
    recs = daily(3) + [Record("s", "x", None, day(5), {"v": 1}, order=3), Record("s", "y", day(6), None, {"v": 2}, order=4), Record("s", "z", None, None, {"v": 3}, order=5)]
    q = src(recs)
    assert q["event_time"]["n_present"] == 4 and q["event_time"]["n_missing"] == 2 and q["available_at"]["n_present"] == 4 and q["available_at"]["n_missing"] == 2
    assert {"missing_event_time", "missing_available_at"} <= set(q["flags"])
    assert dq.coverage(q["available_at"]["n_present"], q["n_records"]) == pytest.approx(4 / 6) and dq.coverage(0, 0) is None      # no records: unknown, not full


def test_naive_and_unparseable_timestamps_are_counted_from_the_source_flags():
    rows = [{"event": "2023-01-01T00:00:00Z", "avail": "2023-01-01T12:00:00"},            # naive: no zone, never assumed
            {"event": "2023-01-02T00:00:00Z", "avail": "not a time"},
            {"event": "2023-01-03T00:00:00Z", "avail": "2023-01-03T12:00:00Z"}]
    q = src(list(Rows(rows).records()))
    assert q["available_at"]["n_naive"] == 1 and q["available_at"]["n_unparseable"] == 1 and q["available_at"]["n_missing"] == 2 and "unusable_timestamps" in q["flags"]
    assert src(list(Rows(rows[2:]).records()))["available_at"]["n_naive"] == 0


# ---------------------------------------------------------------- suspicious timing
def test_available_before_event_is_counted_without_relying_on_the_source_flag():
    recs = daily(3) + [Record("s", "bad", day(10), day(9), {"v": 1}, order=3)]                # declared available a day BEFORE it happened
    q = src(recs)
    assert q["timing"]["available_before_event"] == 1 and "available_before_event" in q["flags"]
    assert src(daily(3))["timing"]["available_before_event"] == 0


def test_future_dated_records_are_counted_against_the_wall_clock_at_load_time():
    recs = daily(3) + [Record("s", "f1", day(0), T("2023-04-01T00:00:00Z"), {"v": 1}, order=3), Record("s", "f2", T("2023-05-01T00:00:00Z"), T("2023-05-02T00:00:00Z"), {"v": 2}, order=4)]
    q = src(recs, now=NOW)
    assert q["timing"]["future_dated"] == 2 and "future_dated" in q["flags"] and q["evaluated_at"] == "2023-03-01T00:00:00Z"
    assert src(recs, now=T("2024-01-01T00:00:00Z"))["timing"]["future_dated"] == 0             # the same data, judged later: nothing is in the future any more


def test_a_record_dated_in_the_future_by_its_event_time_alone_is_future_dated():
    odd = [Record("s", "e", T("2023-05-01T00:00:00Z"), T("2023-01-05T00:00:00Z"), {"v": 1}, order=0)]          # claims to be available long before it happened, and happens after 'now'
    q = src(odd, now=NOW)
    assert q["timing"]["future_dated"] == 1 and q["timing"]["available_before_event"] == 1


def test_the_lag_between_event_and_availability_is_summarised_in_whole_seconds():
    q = src(daily(4))
    assert q["timing"]["lag_seconds"] == {"min": 21 * 3600, "median": 21 * 3600, "max": 21 * 3600}


def test_a_record_available_before_its_event_is_left_out_of_the_lag_rather_than_making_it_negative():
    q = src(daily(4) + [Record("s", "bad", day(10), day(9), {"v": 1}, order=4)])
    assert q["timing"]["lag_seconds"]["min"] == 21 * 3600 and q["timing"]["available_before_event"] == 1


# ---------------------------------------------------------------- bulk backfill
def burst(n, span_days, hour="2023-06-01T10:15:00Z"):
    return [Record("s", f"b{i}", day(i * span_days // max(n - 1, 1)), T(hour), {"v": i}, order=i) for i in range(n)]


def test_a_long_history_that_became_available_in_one_hour_is_a_bulk_backfill():
    q = src(burst(40, 39))
    assert q["bursts"]["n_bursts"] == 1 and q["bursts"]["n_records_in_bursts"] == 40 and "bulk_backfill" in q["flags"]
    assert q["bursts"]["largest"] == {"available_at_hour": "2023-06-01T10:00:00Z", "n_records": 40, "event_span_days": 39}


def test_bursts_need_both_the_count_and_the_span_so_normal_batches_are_not_flagged():
    assert src(burst(19, 90))["bursts"]["n_bursts"] == 0                                        # too few records
    assert src(burst(40, 10))["bursts"]["n_bursts"] == 0                                        # one batch of recent days: a normal load
    assert src(daily(40))["bursts"]["n_bursts"] == 0                                            # a daily feed: one availability per day
    assert src(burst(20, 30))["bursts"]["n_bursts"] == 1                                        # exactly at both thresholds


# ---------------------------------------------------------------- restatements, duplicates
def test_restatements_are_detected_by_record_id_and_counted():
    recs = [rec("a", "2023-01-02T00:00:00Z", {"v": 1}, "2023-01-01T00:00:00Z", 0), rec("a", "2023-01-10T00:00:00Z", {"v": 2}, "2023-01-01T00:00:00Z", 1),
            rec("a", "2023-01-20T00:00:00Z", {"v": 3}, "2023-01-01T00:00:00Z", 2), rec("b", "2023-01-02T00:00:00Z", {"v": 9}, "2023-01-01T00:00:00Z", 3)]
    q = src(recs)
    assert q["identity"] == "record_id" and q["restatements"] == {"detectable": True, "n_ids_restated": 1, "n_extra_versions": 2} and "restatements" in q["flags"]
    assert src(daily(5))["restatements"]["n_ids_restated"] == 0


def test_without_a_record_key_restatements_are_unknown_not_zero():
    q = src([Record("s", f"row:{i}", day(i), day(i, 21), {"v": i}, order=i) for i in range(5)])
    assert q["identity"] == "row_index" and q["restatements"] == {"detectable": False, "n_ids_restated": None, "n_extra_versions": None}
    assert "no_record_identity" in q["flags"] and "restatements" not in q["flags"]


def test_duplicate_rows_are_found_by_id_availability_and_payload_or_by_payload_alone_without_a_key():
    a = rec("a", "2023-01-02T00:00:00Z", {"v": 1}, "2023-01-01T00:00:00Z", 0)
    q = src([a, a, rec("b", "2023-01-02T00:00:00Z", {"v": 2}, "2023-01-01T00:00:00Z", 2)])
    assert q["duplicates"] == {"basis": "record_id+available_at+payload", "n_duplicate_rows": 1} and "duplicate_rows" in q["flags"]
    assert src(daily(4))["duplicates"]["n_duplicate_rows"] == 0
    rows = [Record("s", f"row:{i}", day(i), day(i, 21), {"v": 7}, order=i) for i in range(3)]      # the same payload three times under row ids
    assert src(rows)["duplicates"] == {"basis": "payload", "n_duplicate_rows": 2}


# ---------------------------------------------------------------- gaps
def test_a_hole_in_the_history_is_a_gap_and_weekends_are_not():
    q = src(daily(10) + daily(10, start=30))
    g = q["gaps"]
    assert g["median_step_seconds"] == 86400 and g["n_gaps"] == 1 and g["largest_gap_seconds"] == 21 * 86400 and g["largest_gap_after"] == "2023-01-10T00:00:00Z" and "gaps" in q["flags"]
    weekdays = [Record("s", f"w{i}", day(i), day(i, 21), {"v": i}, order=i) for i in range(21) if (D0 + timedelta(days=i)).weekday() < 5]          # Monday to Friday
    wk = src(weekdays)["gaps"]
    assert wk["n_gaps"] == 0 and wk["median_step_seconds"] == 86400


def test_too_few_distinct_times_means_gaps_are_unknown():
    q = src(daily(2))
    assert q["gaps"] == {"median_step_seconds": None, "n_gaps": None, "largest_gap_seconds": None, "largest_gap_after": None} and "too_few_points_for_gaps" in q["flags"]


def test_an_empty_source_reports_unknowns_not_clean():
    q = src([])
    assert q["n_records"] == 0 and q["gaps"]["n_gaps"] is None and q["event_time"]["min"] is None and q["flags"] == []


# ---------------------------------------------------------------- per query: staleness and the rest
def run_query(make_session, records, as_of, **kw):
    s = make_session(as_of)
    s.add_source(ListSource("s", records))
    s.query("s", **kw)
    return s.receipt()


def test_served_data_is_stale_when_the_newest_record_is_many_steps_older_than_t(make_session):
    fresh = run_query(make_session, daily(10), "2023-01-11T00:00:00Z", limit=1).quality
    assert fresh["freshness"] == {"newest_event_time": "2023-01-10T00:00:00Z", "age_seconds": 86400, "median_step_seconds": 86400, "stale": False} and "stale" not in fresh["flags"]
    stale = run_query(make_session, daily(10), "2023-02-20T00:00:00Z", limit=1).quality
    assert stale["freshness"]["stale"] is True and stale["freshness"]["age_seconds"] == 41 * 86400 and "stale" in stale["flags"]


def test_staleness_is_unknown_when_no_step_can_be_measured(make_session):
    q = run_query(make_session, daily(2), "2023-02-20T00:00:00Z").quality
    assert q["freshness"]["stale"] is None and q["freshness"]["median_step_seconds"] is None and "stale" not in q["flags"]


def test_restated_ids_count_only_versions_visible_at_t(make_session):
    recs = [rec("a", "2023-01-02T00:00:00Z", {"v": 1}, "2023-01-01T00:00:00Z", 0), rec("a", "2023-01-10T00:00:00Z", {"v": 2}, "2023-01-01T00:00:00Z", 1),
            rec("b", "2023-01-02T00:00:00Z", {"v": 5}, "2023-01-01T00:00:00Z", 2), rec("b", "2023-03-01T00:00:00Z", {"v": 6}, "2023-01-01T00:00:00Z", 3)]
    assert run_query(make_session, recs, "2023-01-15T00:00:00Z").quality["restated_ids"] == 1          # a was corrected before T; b's correction is still in the future
    assert run_query(make_session, recs, "2023-01-05T00:00:00Z").quality["restated_ids"] == 0


def test_served_records_in_a_backfill_burst_available_before_event_and_duplicates_are_flagged(make_session):
    recs = burst(40, 39) + [Record("s", "dup", day(1), T("2023-06-01T11:00:00Z"), {"v": "same"}, order=40), Record("s", "dup2", day(2), T("2023-06-01T11:30:00Z"), {"v": "same"}, order=41),
                             Record("s", "early", day(50), T("2023-06-01T11:40:00Z"), {"v": "e"}, order=42)]
    q = run_query(make_session, recs, "2023-07-01T00:00:00Z").quality
    assert q["in_backfill_burst"] == 40 and "bulk_backfill" in q["flags"] and q["duplicate_rows"] == 1 and "duplicate_rows" in q["flags"]
    clean = run_query(make_session, daily(10), "2023-01-11T00:00:00Z").quality
    assert clean["flags"] == [] and clean["in_backfill_burst"] == clean["restated_ids"] == clean["duplicate_rows"] == clean["available_before_event"] == 0


def test_an_unknown_timing_record_served_on_request_is_flagged(make_session):
    s = make_session("2023-01-11T00:00:00Z", allow_untimestamped=True)
    s.add_source(ListSource("s", daily(3) + [Record("s", "u", None, None, {"v": 1}, order=3)]))
    s.query("s")
    q = s.receipt().quality
    assert q["served_with_available_at"] == 3 and q["served"] == 4 and {"unknown_availability", "missing_event_time"} <= set(q["flags"])


def test_gaps_between_served_records_use_the_visible_median_step(make_session):
    q = run_query(make_session, daily(10) + daily(10, start=30), "2023-02-15T00:00:00Z").quality
    assert q["gaps_between_served"] == 1 and "gaps" in q["flags"]


def test_nothing_served_is_said_so(make_session):
    q = run_query(make_session, daily(3), "2023-01-11T00:00:00Z", v=999).quality
    assert q["served"] == 0 and "nothing_served" in q["flags"] and q["freshness"]["newest_event_time"] is None and q["freshness"]["stale"] is None


# ---------------------------------------------------------------- the claims that matter
def test_a_query_summary_adds_nothing_about_the_future(make_session):
    """The same session clock over two versions of a source that differ ONLY after T: the per-query summary is identical (only its pointer to the whole-source summary may differ)."""
    base = daily(10)
    future = daily(30, start=100) + burst(40, 39) + [rec("d1", "2024-01-01T00:00:00Z", {"v": "restated later"}, "2023-01-02T00:00:00Z", 99)] + [Record("s", "d3", day(3), T("2024-02-01T00:00:00Z"), {"v": 3}, order=98)]
    a = run_query(make_session, base, "2023-01-08T00:00:00Z").quality
    b = run_query(make_session, base + future, "2023-01-08T00:00:00Z").quality
    strip = lambda q: {k: v for k, v in q.items() if k != "source"}                              # noqa: E731
    assert strip(a) == strip(b)
    assert a["source"]["summary_sha256"] != b["source"]["summary_sha256"]                         # while the whole-source (operator) summary does see the future, which is why it is operator-only


def test_the_source_summary_does_see_the_whole_source_including_after_t(make_session):
    s = make_session("2023-01-08T00:00:00Z")
    s.add_source(ListSource("s", daily(10) + burst(40, 39)))
    assert "bulk_backfill" in s.quality("s")["flags"] and "bulk_backfill" not in s.query("s") and "bulk_backfill" not in s.receipt().quality["flags"]


def walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            assert isinstance(k, str)
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)
    else:
        yield o


def test_summaries_hold_only_integers_strings_booleans_and_none_so_they_hash_the_same_in_every_language(make_session):
    s = make_session("2023-02-15T00:00:00Z")
    s.add_source(ListSource("s", daily(10) + daily(10, start=30) + burst(40, 39)))
    s.query("s")
    for q in (s.quality("s"), s.receipt().quality):
        for leaf in walk(q):
            assert leaf is None or isinstance(leaf, (str, bool, int)) and not isinstance(leaf, float), leaf


@pytest.mark.parametrize("privacy", ["full", "hashes_only"])
def test_a_summary_never_contains_a_record_value_and_is_stored_identically_in_both_privacy_modes(make_session, privacy):
    s = make_session("2023-01-11T00:00:00Z", privacy=privacy)
    secret = "SECRET-VALUE-4711"
    s.add_source(ListSource("s", [Record("s", f"id-{secret}-{i}", day(i), day(i, 21), {"note": secret, "v": i}, order=i) for i in range(10)]))
    s.query("s")
    loaded = s.events("source_loaded")[0].data()
    queried = s.events("query")[0].data()
    for blob in (json.dumps(loaded["quality"]), json.dumps(queried["quality"])):
        assert secret not in blob
    assert queried["quality"]["served"] == 10 and loaded["quality"]["n_records"] == 10                # the same numbers in both modes
    assert s.verify_ledger(anchor="skip").ok


def test_the_agent_view_of_a_receipt_leaves_out_quality_and_universe(make_session):
    r = run_query(make_session, daily(10), "2023-01-11T00:00:00Z")
    assert r.quality is not None and "quality" not in r.agent_view() and "universe" not in r.agent_view()


def test_the_receipt_summary_prints_the_quality_lines(make_session):
    text = run_query(make_session, daily(10) + burst(40, 39), "2023-07-01T00:00:00Z", limit=5).summary()
    assert "data quality: timestamps 100% event_time / 100% available_at; newest served record" in text and "quality flags:" in text
    assert "source: bulk_backfill" in text


def test_the_receipt_survives_a_roundtrip_through_the_ledger_with_the_same_quality(make_session, ledger_path):
    s = make_session("2023-01-11T00:00:00Z")
    s.add_source(ListSource("s", daily(10)))
    s.query("s")
    r = s.receipt()
    again = receipt_from_ledger(Ledger(ledger_path), r.call_id)
    assert again.quality == r.quality and again.to_dict() == r.to_dict()


def test_a_ledger_written_before_the_summary_existed_says_not_recorded_instead_of_clean(tmp_path):
    shutil.copy(Path(__file__).parent / "fixtures" / "v1_0" / "ledger.db", tmp_path / "ledger.db")
    led = Ledger(tmp_path / "ledger.db")
    call = next(e.call_id for e in led.events(type="query"))
    r = receipt_from_ledger(led, call)
    assert r.quality is None and r.universe is None and "data quality: not recorded (this ledger predates the data-quality summary)" in r.summary()
    assert "quality flags" not in r.summary()


def test_session_quality_lists_every_loaded_source_and_refuses_an_unknown_one(make_session):
    s = make_session("2023-01-11T00:00:00Z")
    s.add_source(ListSource("one", daily(3)), lazy=True)
    s.add_source(ListSource("two", daily(4)))
    assert set(s.quality()) == {"one", "two"} and s.quality("two")["n_records"] == 4
    with pytest.raises(Exception, match="unknown source"):
        s.quality("nope")


def test_the_summary_is_deterministic_given_the_records_and_the_clock():
    recs = daily(10) + burst(40, 39)
    a, b = src(recs), src(list(reversed(recs)))                                                   # the order records arrive in must not change any count
    for k in ("event_time", "available_at", "timing", "bursts", "restatements", "duplicates", "gaps", "flags"):
        assert a[k] == b[k], k


def test_the_thresholds_used_are_written_into_every_summary(make_session):
    q = run_query(make_session, daily(10), "2023-01-11T00:00:00Z")
    assert q.quality["thresholds"] == src(daily(3))["thresholds"] == {"gap_factor": 5, "stale_factor": 5, "burst_min_records": 20, "burst_min_span_days": 30, "late_lag_factor": 10, "late_lag_min_seconds": 86400}


# ---------------------------------------------------------------- late availability, severities, evidence (Phase 2)
def lagged(n, lag_days_each, *, start=0, prefix="l"):
    """n daily records whose available_at is `lag_days_each` days after the event (a long, steady lag)."""
    return [Record("s", f"{prefix}{start + i}", day(start + i), day(start + i + lag_days_each, 21), {"v": start + i}, order=i) for i in range(n)]


def test_a_record_available_much_later_than_its_event_is_flagged_late_and_a_steady_lag_is_not():
    steady = src(lagged(12, 3))
    assert steady["timing"]["late_availability"] == 0 and "late_availability" not in steady["flags"]        # every record is 3 days late: that is the source's normal lag
    recs = lagged(12, 0) + [Record("s", "backfilled", day(2), day(120, 9), {"v": 99}, order=99)]            # normally same-day, but this one arrived four months after its event
    q = src(recs)
    assert q["timing"]["late_availability"] == 1 and "late_availability" in q["flags"]
    assert src(lagged(12, 0))["timing"]["late_availability"] == 0                                           # control: a source that is always same-day raises nothing


def test_a_change_log_does_not_measure_lateness():
    recs = [Record("u", f"c{i}", day(i + 10), day(i), {"v": i}, order=i) for i in range(5)]               # announced ahead of effect: normal for a universe
    q = dq.analyse_source(recs, now=NOW, kind=dq.CHANGE_LOG)
    assert q["timing"]["late_availability"] is None and "late_availability" not in q["flags"]


def test_every_flag_has_a_severity_and_the_counts_by_severity_are_there_and_there_is_no_single_score():
    recs = daily(10) + [Record("s", "bad", day(5), day(2), {"v": 1}, order=50)] + lagged(1, 200, start=30, prefix="old")
    q = src(recs)
    sev = {i["flag"]: i["severity"] for i in q["issues"]}
    assert sev["available_before_event"] == "error" and sev["late_availability"] == "warning"
    assert q["by_severity"] == {"error": sum(1 for v in sev.values() if v == "error"), "warning": sum(1 for v in sev.values() if v == "warning"), "info": sum(1 for v in sev.values() if v == "info")}
    assert [i["severity"] for i in q["issues"]] == sorted((i["severity"] for i in q["issues"]), key=["error", "warning", "info"].index)       # most serious first
    assert not any("score" in k for k in q) and all(i["flag"] in dq.SEVERITY for i in q["issues"])
    assert set(dq.SEVERITY) >= set(src(daily(2) + [Record("s", "n", None, None, {"v": 1}, order=9)])["flags"])           # every flag the module can raise has a severity


def test_a_clean_source_has_no_issues():
    q = src(daily(8))
    assert q["issues"] == [] and q["by_severity"] == {"error": 0, "warning": 0, "info": 0}


def test_each_issue_says_whether_its_count_is_exact_or_a_heuristic_and_lists_affected_rows_as_hashes():
    from asof.privacy import digest
    recs = daily(6) + [Record("s", "dup-a", day(8), day(8, 21), {"v": 1}, order=20), Record("s", "dup-a", day(8), day(8, 21), {"v": 1}, order=21)] + lagged(1, 90, start=40, prefix="late")
    q = src(recs)
    by = {i["flag"]: i for i in q["issues"]}
    assert by["duplicate_rows"]["basis"] == "counted" and by["duplicate_rows"]["n"] == 1 and by["duplicate_rows"]["example_record_ids_sha256"] == [digest("dup-a")]
    assert by["late_availability"]["basis"] == "heuristic" and by["late_availability"]["n"] == 1 and by["late_availability"]["example_record_ids_sha256"] == [digest("late40")]
    assert "dup-a" not in json.dumps(q) and "late40" not in json.dumps(q)                                  # the summary holds hashes, never a record id


def test_examples_are_capped_and_the_true_count_is_still_reported():
    recs = daily(10) + [Record("s", f"b{i}", day(i), day(300 + i, 3), {"v": i}, order=100 + i) for i in range(9)]
    late = {i["flag"]: i for i in src(recs)["issues"]}["late_availability"]
    assert late["n"] == 9 and len(late["example_record_ids_sha256"]) == dq.EXAMPLES == 5


def _leaves(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from _leaves(v)
    elif isinstance(x, list):
        for v in x:
            yield from _leaves(v)
    else:
        yield x


def test_the_new_keys_are_still_only_ints_strings_booleans_and_none():
    q = src(daily(6) + lagged(1, 90, start=40, prefix="late") + [Record("s", "x", None, None, {"v": 1}, order=9)])
    assert all(v is None or isinstance(v, (int, str, bool)) and not isinstance(v, float) for v in _leaves(q))


def test_the_issue_hashes_match_the_record_id_hash_a_hashes_only_ledger_records_for_the_same_row(make_session):
    """Evidence back to the exact row: an issue's example hash equals `record_id_sha256` on the served record in the ledger, so a platform can open the row without a raw id."""
    recs = daily(8) + [Record("s", "backfill-1", day(2), day(150, 9), {"v": 7}, order=60)]
    s = make_session("2023-12-01T00:00:00Z", privacy="hashes_only")
    s.add_source(ListSource("s", recs))
    s.query("s")
    quality = [e for e in s.ledger.events(session_id=s.id, type="source_loaded")][0].data()["quality"]
    issue = {i["flag"]: i for i in quality["issues"]}["late_availability"]
    served = [e.data() for e in s.ledger.events(session_id=s.id, type="record_served")]
    assert issue["example_record_ids_sha256"][0] in {x["record_id_sha256"] for x in served}
    assert "backfill-1" not in json.dumps([e.payload for e in s.ledger.events(session_id=s.id)])
