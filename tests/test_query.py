"""R1 (inclusive boundary), R2 (untimestamped), R3 (restatements)."""
import random
from datetime import timedelta

import pytest

from asof import Session
from asof.query import pit_query

from conftest import ListSource, T, rec

AS_OF = T("2023-01-21T14:00:00Z")


def ids(result):
    return [r.record_id for r in result.records]


# ------------------------------------------------------------------ R1
def test_r1_a_record_available_exactly_at_T_is_served():
    assert ids(pit_query([rec("a", "2023-01-21T14:00:00Z")], AS_OF)) == ["a"]


def test_r1_one_microsecond_later_is_blocked():
    r = rec("a", "2023-01-21T14:00:00Z")
    late = type(r)(r.source_id, "b", None, AS_OF + timedelta(microseconds=1), {"id": "b"})
    out = pit_query([r, late], AS_OF)
    assert ids(out) == ["a"] and out.counts["blocked_future"] == 1


@pytest.mark.parametrize("delta", [timedelta(milliseconds=1), timedelta(seconds=1), timedelta(microseconds=1)], ids=["T+1ms", "T+1s", "T+1us"])
def test_r1_boundary_equal_to_T_is_served_and_anything_after_it_is_blocked_through_a_session_too(delta, make_session):
    """available_at <= T, inclusive, in the pure query AND in a real session (which is what an agent actually calls)."""
    on, after = rec("on", "2023-01-21T14:00:00Z"), rec("after", "2023-01-21T14:00:00Z")
    after = type(after)(after.source_id, "after", None, AS_OF + delta, {"id": "after"})
    out = pit_query([on, after], AS_OF)
    assert ids(out) == ["on"] and out.counts["blocked_future"] == 1
    s = make_session("2023-01-21T14:00:00Z")
    s.add_source(ListSource("d", [on, after]))
    served = s.query("d")
    r = s.receipt()
    assert [x.record_id for x in served] == ["on"] and r.served == 1 and r.blocked["future"] == 1


def test_r1_blocked_records_are_counted_and_listed_but_never_returned():
    out = pit_query([rec("old", "2023-01-01T00:00:00Z"), rec("f1", "2023-02-01T00:00:00Z"), rec("f2", "2030-01-01T00:00:00Z")], AS_OF)
    assert ids(out) == ["old"] and out.counts["blocked_future"] == 2
    assert [i for i, _ in out.blocked_future] == ["f1", "f2"]


def test_r1_other_timezones_compare_as_instants():
    from datetime import datetime, timedelta as td, timezone
    plus9 = datetime(2023, 1, 21, 23, 0, tzinfo=timezone(td(hours=9)))              # = 14:00Z
    r = type(rec("x", None))("s", "x", None, plus9, {"id": "x"})
    assert ids(pit_query([r], AS_OF)) == ["x"]


# ------------------------------------------------------------------ R2
def test_r2_missing_available_at_is_not_served_by_default():
    out = pit_query([rec("u", None), rec("ok", "2023-01-01T00:00:00Z")], AS_OF)
    assert ids(out) == ["ok"] and out.counts["blocked_untimestamped"] == 1 and out.counts["served_unknown_timing"] == 0


def test_r2_allow_untimestamped_serves_them_labelled_unknown():
    out = pit_query([rec("u", None), rec("ok", "2023-01-01T00:00:00Z")], AS_OF, allow_untimestamped=True)
    assert sorted(ids(out)) == ["ok", "u"] and out.served_unknown_ids == ("u",) and out.counts["served_unknown_timing"] == 1


def test_r2_a_timestamped_version_always_beats_an_untimestamped_copy():
    out = pit_query([rec("k", None, {"v": "unknown"}), rec("k", "2023-01-01T00:00:00Z", {"v": "dated"})], AS_OF, allow_untimestamped=True)
    assert [r.payload["v"] for r in out.records] == ["dated"] and out.served_unknown_ids == ()


# ------------------------------------------------------------------ R3
def versions():
    return [rec("rpt", "2023-01-10T00:00:00Z", {"eps": 1.00}, order=0), rec("rpt", "2023-01-20T00:00:00Z", {"eps": 1.10}, order=1),
            rec("rpt", "2023-02-15T00:00:00Z", {"eps": 0.40}, order=2)]


def test_r3_the_latest_version_available_at_T_is_served_never_a_later_correction():
    out = pit_query(versions(), AS_OF)
    assert [r.payload["eps"] for r in out.records] == [1.10] and out.counts["superseded"] == 1 and out.counts["blocked_future"] == 1


def test_r3_before_the_first_version_nothing_is_served():
    assert ids(pit_query(versions(), T("2023-01-05T00:00:00Z"))) == []


def test_r3_after_the_correction_the_correction_is_served():
    assert [r.payload["eps"] for r in pit_query(versions(), T("2023-03-01T00:00:00Z")).records] == [0.40]


def test_r3_filters_apply_to_the_chosen_version_not_to_older_ones():
    # eps==1.00 matched the first version, but that version was superseded at T: it must NOT be served
    out = pit_query(versions(), AS_OF, filters={"eps": 1.00})
    assert out.records == ()


def _tied(**kw):
    """Three versions of one record that all became available at the same instant, in every row order."""
    from dataclasses import replace
    from itertools import permutations
    base = [rec("k", "2023-01-01T00:00:00Z", {"v": name}) for name in ("first", "second", "third")]
    rows = [replace(r, **{k: v[i] for k, v in kw.items()}) for i, r in enumerate(base)]
    return [[replace(r, order=n) for n, r in enumerate(p)] for p in permutations(rows)]


def test_r3_an_exact_tie_is_settled_by_the_explicit_version_whatever_the_row_order():
    for rows in _tied(version=[3, 1, 2]):
        out = pit_query(rows, AS_OF)
        assert [r.payload["v"] for r in out.records] == ["first"]                          # version 3, wherever it sits in the source
        assert out.counts["ambiguous_versions"] == 1 and out.counts["superseded"] == 2     # the two that lost the tie are counted as superseded
        assert "never by row order" in out.warnings[0]


def test_r3_without_a_version_the_latest_recorded_at_wins_whatever_the_row_order():
    stamps = [T("2023-01-02T00:00:00Z"), T("2023-01-04T00:00:00Z"), T("2023-01-03T00:00:00Z")]
    for rows in _tied(recorded_at=stamps):
        assert [r.payload["v"] for r in pit_query(rows, AS_OF).records] == ["second"]


def test_r3_a_version_beats_recorded_at_and_a_stated_version_beats_none():
    stamps = [T("2023-01-09T00:00:00Z"), T("2023-01-02T00:00:00Z"), T("2023-01-03T00:00:00Z")]
    for rows in _tied(version=[1, 2, None], recorded_at=stamps):
        assert [r.payload["v"] for r in pit_query(rows, AS_OF).records] == ["second"]


def test_r3_with_neither_the_choice_depends_on_the_content_only_never_on_the_row_order():
    picks = {pit_query(rows, AS_OF).records[0].payload["v"] for rows in _tied()}
    assert len(picks) == 1                                                                 # all six row orders serve the same version (the old rule, "the later row", gave three different answers)
    by_row_order = {max(rows, key=lambda r: r.order).payload["v"] for rows in _tied()}
    assert len(by_row_order) == 3


def test_r3_identical_copies_are_one_served_and_the_rest_superseded():
    out = pit_query([rec("k", "2023-01-01T00:00:00Z", {"v": "same"}, order=0), rec("k", "2023-01-01T00:00:00Z", {"v": "same"}, order=1), rec("j", "2023-01-01T00:00:00Z", {"v": "x"}, order=2)], AS_OF)
    assert len(out.records) == 2 and out.counts["superseded"] == 1 and out.counts["ambiguous_versions"] == 1
    assert out.counts["served"] + out.counts["superseded"] == 3                            # every row is accounted for


def test_a_tie_is_a_data_quality_flag_on_the_receipt_not_no_flags(make_session):
    s = make_session("2023-01-21T14:00:00Z")
    s.add_source(ListSource("d", [rec("k", "2023-01-01T00:00:00Z", {"v": "a"}, order=0), rec("k", "2023-01-01T00:00:00Z", {"v": "b"}, order=1), rec("j", "2023-01-02T00:00:00Z", {"v": "c"}, order=2)]))
    s.query("d")
    r = s.receipt().to_dict()
    assert "tied_versions" in r["quality"]["flags"] and r["quality"]["tied_versions"] == 1 and r["blocked"]["superseded"] == 1
    clean = make_session("2023-01-21T14:00:00Z")
    clean.add_source(ListSource("d", [rec("k", "2023-01-01T00:00:00Z", {"v": "a"}), rec("j", "2023-01-02T00:00:00Z", {"v": "c"}, order=1)]))
    clean.query("d")
    q = clean.receipt().to_dict()["quality"]
    assert "tied_versions" not in q and "tied_versions" not in q["flags"]                  # no tie: nothing added, so summaries and ledger hashes of clean data are what they were


# ------------------------------------------------------------------ filters, lookback, limit
def test_filters_lookback_and_limit():
    rs = [rec(f"r{i}", f"2023-01-{i:02d}T00:00:00Z", {"sym": "A" if i % 2 else "B"}, event_time=f"2023-01-{i:02d}T00:00:00Z") for i in range(1, 21)]
    out = pit_query(rs, AS_OF, filters={"sym": "A"})
    assert all(r.payload["sym"] == "A" for r in out.records) and len(out.records) == 10
    assert len(pit_query(rs, AS_OF, filters={"sym": ["A", "B"]}).records) == 20
    assert ids(pit_query(rs, AS_OF, lookback=timedelta(days=3))) == ["r19", "r20"]          # window starts Jan 18 14:00; Jan 18 00:00 is outside it
    assert ids(pit_query(rs, AS_OF, limit=2)) == ["r19", "r20"]
    assert pit_query(rs, AS_OF, limit=0).records == ()
    assert pit_query(rs, AS_OF, filters={"nope": 1}).records == ()


def test_lookback_needs_an_event_time_and_counts_what_it_excludes():
    out = pit_query([rec("no_event", "2023-01-01T00:00:00Z")], AS_OF, lookback=timedelta(days=30))
    assert out.records == () and out.counts["outside_window"] == 1


def test_records_flagged_available_before_event_warn():
    r = rec("odd", "2023-01-01T00:00:00Z").with_flags("available_before_event")
    assert pit_query([r], AS_OF).warnings


# ------------------------------------------------------------------ the property that matters
@pytest.mark.parametrize("seed", range(25))
def test_property_nothing_served_is_from_the_future_and_every_served_version_is_the_latest_visible(seed):
    rng = random.Random(seed)
    base = T("2023-01-01T00:00:00Z")
    rs = [rec(f"id{rng.randrange(12)}", None if rng.random() < 0.1 else (base + timedelta(hours=rng.randrange(0, 24 * 40))).strftime("%Y-%m-%dT%H:%M:%SZ"),
              {"n": n}, order=n) for n in range(80)]
    t = base + timedelta(hours=rng.randrange(0, 24 * 40))
    for allow in (False, True):
        out = pit_query(rs, t, allow_untimestamped=allow)
        for r in out.records:
            if r.available_at is not None:
                assert r.available_at <= t                                                         # R1
                later_visible = [o for o in rs if o.record_id == r.record_id and o.available_at is not None and r.available_at < o.available_at <= t]
                assert not later_visible                                                           # R3
            else:
                assert allow                                                                       # R2
        assert len({r.record_id for r in out.records}) == len(out.records)                         # one version per id
