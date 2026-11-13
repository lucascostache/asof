"""Pre-launch campaign, area A: the time lock itself.

1. property-based tests (Hypothesis): random records, times, versions and clocks; the invariant is that nothing served was available after T
2. boundaries: the microsecond either side of T, time zones and DST, naive times, leap day, year ends, 1970 and 2100
3. versions and restatements: ties, and 100 shuffles of the input giving one answer
4. the forward-only clock, including two threads
5. a session stepped through 500 times against a fresh query at each time

The number of generated cases per property is ``ASOF_PROPERTY_EXAMPLES`` (default 200, so the ordinary suite stays quick); the campaign ran it at 10000.
"""
from __future__ import annotations

import os
import random
import threading
from datetime import datetime, timedelta, timezone

import pytest

from asof import Ledger, Record, Session, sources
from asof.clock import parse_time
from asof.errors import ClockError, NaiveTimeError
from asof.query import pit_query

from conftest import ListSource, T, rec

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings          # noqa: E402
from hypothesis import strategies as st                      # noqa: E402

UTC = timezone.utc
N = int(os.environ.get("ASOF_PROPERTY_EXAMPLES", "200"))
FAST = settings(max_examples=N, deadline=None, database=None, suppress_health_check=list(HealthCheck))
SLOW = settings(max_examples=max(20, N // 20), deadline=None, database=None, suppress_health_check=list(HealthCheck))      # properties that open a ledger per case
US = timedelta(microseconds=1)
BASE = datetime(2023, 1, 21, 14, 0, 0, tzinfo=UTC)

# ------------------------------------------------------------------ strategies
# Times come from two pools: a dense one (single microseconds around BASE, so exact ties and the T boundary are hit all the time) and a wide one (about +-400 days).
_dense = st.integers(-6, 6).map(lambda n: BASE + n * US)
_wide = st.integers(-400 * 86400 * 10**6, 400 * 86400 * 10**6).map(lambda us: BASE + timedelta(microseconds=us))
_offsets = st.sampled_from([0, 330, -480, 60, 765, -210, 840, -720])                                   # minutes: the same instant written in different zones
instants = st.builds(lambda dt, off: dt.astimezone(timezone(timedelta(minutes=off))), st.one_of(_dense, _dense, _wide), _offsets)


@st.composite
def record_lists(draw, max_size=40):
    n = draw(st.integers(0, max_size))
    out = []
    for i in range(n):
        out.append(Record("s", draw(st.sampled_from("abcdef")), draw(st.one_of(st.none(), instants)), draw(st.one_of(st.none(), instants, instants, instants, instants)),
                          {"v": draw(st.integers(0, 3)), "k": draw(st.sampled_from(["x", "y"]))}, order=i,
                          version=draw(st.one_of(st.none(), st.none(), st.integers(0, 3))), recorded_at=draw(st.one_of(st.none(), st.none(), instants))))
    return out


def oracle(records, as_of, allow_untimestamped=False):
    """What must be served with no filters: written from the rules, not from the implementation. One version per record id: the latest available at T; an exact tie goes to the highest
    explicit version, then the latest recorded_at, then the larger payload hash."""
    by_id: dict[str, list[Record]] = {}
    for r in records:
        by_id.setdefault(r.record_id, []).append(r)
    out = {}
    for rid, versions in by_id.items():
        seen = [v for v in versions if v.available_at is not None and v.available_at <= as_of]
        pool = seen if seen else ([v for v in versions if v.available_at is None] if allow_untimestamped else [])
        if not pool:
            continue
        if seen:
            top = max(v.available_at for v in seen)
            pool = [v for v in seen if v.available_at == top]
        with_version = [v for v in pool if v.version is not None]
        if with_version:
            best = max(v.version for v in with_version)
            pool = [v for v in with_version if v.version == best]
        with_stamp = [v for v in pool if v.recorded_at is not None]
        if with_stamp:
            last = max(v.recorded_at for v in with_stamp)
            pool = [v for v in with_stamp if v.recorded_at == last]
        out[rid] = max(v.payload_sha256 for v in pool)
    return out


def served_map(result_records):
    return {r.record_id: r.payload_sha256 for r in result_records}


# ------------------------------------------------------------------ 1. properties
@FAST
@given(record_lists(), instants, st.booleans(), st.one_of(st.none(), st.sampled_from(["x", "y"])), st.one_of(st.none(), st.integers(0, 900 * 86400)), st.one_of(st.none(), st.integers(0, 50)))
def test_property_nothing_served_was_available_after_T(records, as_of, allow, k, lookback_s, limit):
    """THE invariant, with every option that could weaken it switched on at random (untimestamped rows allowed, a filter, a lookback, a limit)."""
    res = pit_query(records, as_of, filters={"k": k} if k else None, lookback=None if lookback_s is None else timedelta(seconds=lookback_s), limit=limit, allow_untimestamped=allow)
    for r in res.records:
        assert r.available_at is None or r.available_at <= as_of, (r.record_id, r.available_at, as_of)
        if r.available_at is None:
            assert allow and r.record_id in res.served_unknown_ids                                    # an undated row only with the opt-in, and labelled
        if lookback_s is not None:
            assert r.event_time is not None and as_of - timedelta(seconds=lookback_s) <= r.event_time <= as_of
        if k:
            assert r.payload["k"] == k
    ids = [r.record_id for r in res.records]
    assert len(ids) == len(set(ids))                                                                   # one version per record id
    assert limit is None or len(ids) <= limit
    assert res.counts["served"] == len(ids)


@FAST
@given(record_lists(), instants, st.booleans(), st.randoms(use_true_random=False))
def test_property_the_served_version_is_the_one_the_rules_name_and_row_order_does_not_matter(records, as_of, allow, rnd):
    res = pit_query(records, as_of, allow_untimestamped=allow)
    assert served_map(res.records) == oracle(records, as_of, allow)
    assert res.counts["blocked_future"] == sum(1 for r in records if r.available_at is not None and r.available_at > as_of)      # exact, with no filter
    shuffled = list(records)
    rnd.shuffle(shuffled)
    again = pit_query(shuffled, as_of, allow_untimestamped=allow)
    same = lambda rs: [(r.record_id, r.payload_sha256, r.event_time, r.available_at, r.version, r.recorded_at, r.flags) for r in rs]      # noqa: E731 - everything about a row except its position
    assert same(again.records) == same(res.records)                                                                             # same rows, same order
    assert again.counts == res.counts


def test_two_versions_with_the_same_payload_but_different_event_times_are_not_chosen_by_row_order():
    """Found by the 10,000-case run (the 200-case run missed it): versions tied on availability, version, recorded_at AND payload hash, but with different event times, were picked by
    position. The row then sorted differently in the result, and fell in or out of a lookback window, depending on the order of the source's rows."""
    t = T("2023-01-20T00:00:00Z")
    a = Record("s", "a", None, t, {"v": 0})
    b = Record("s", "a", T("2023-01-19T00:00:00Z"), t, {"v": 0})
    c = Record("s", "a", T("2023-01-10T00:00:00Z"), t, {"v": 0}, flags=("x",))
    assert a.payload_sha256 == b.payload_sha256 == c.payload_sha256
    import itertools
    picks = {(r.event_time, r.flags) for order in itertools.permutations([a, b, c]) for r in pit_query(list(order), T(AS_OF)).records}
    assert picks == {(T("2023-01-19T00:00:00Z"), ())}                                                                           # one answer for all six orders
    inside = {len(pit_query(list(order), T(AS_OF), lookback=timedelta(days=5)).records) for order in itertools.permutations([a, b, c])}
    assert inside == {1}                                                                                                        # and one answer to "is it inside the window"


@FAST
@given(record_lists(), instants, instants)
def test_property_moving_T_forward_never_takes_a_record_id_away_and_never_serves_an_older_version(records, t1, t2):
    lo, hi = sorted((t1, t2))
    early, late = pit_query(records, lo), pit_query(records, hi)
    e, l = {r.record_id: r for r in early.records}, {r.record_id: r for r in late.records}
    assert set(e) <= set(l)
    for rid, r in e.items():
        assert l[rid].available_at >= r.available_at
    assert late.counts["blocked_future"] <= early.counts["blocked_future"]


@SLOW
@given(record_lists(max_size=25), instants, st.integers(0, 10**9), st.booleans(), st.sampled_from(["full", "hashes_only"]))
def test_property_a_session_serves_and_records_only_what_was_available_at_T(tmp_path_factory, records, as_of, step_us, allow, privacy):
    """The same invariant through the whole Session: the rows returned, the ledger's record_served events (each keeps its own available_at) and the receipt, before and after a clock move."""
    path = tmp_path_factory.mktemp("p") / "ledger.db"
    with Session(as_of, ledger_path=path, allow_untimestamped=allow, privacy=privacy) as s:
        s.add_source(ListSource("s", records))
        for clock in (as_of, as_of + timedelta(microseconds=step_us)):
            s.advance_to(clock)
            rows = s.query("s")
            assert served_map(rows) == oracle(records, clock.astimezone(UTC), allow)
            assert all(r.available_at is None or r.available_at <= clock for r in rows)
            r = s.receipt()
            assert r.served == len(rows) and parse_time(r.as_of) == clock
            assert r.blocked.get("future", 0) == sum(1 for x in records if x.available_at is not None and x.available_at > clock)
        for e in s.events("record_served"):
            assert e.available_at is None or parse_time(e.available_at) <= s.as_of
        assert s.verify_ledger().ok


# ------------------------------------------------------------------ 2. boundaries
AS_OF = "2023-01-21T14:00:00Z"


@pytest.mark.parametrize("available_at, served", [("2023-01-21T14:00:00Z", True), ("2023-01-21T14:00:00.000001Z", False), ("2023-01-21T13:59:59.999999Z", True),
                                                  ("2023-01-21T14:00:00+00:00", True), ("2023-01-21T19:30:00+05:30", True), ("2023-01-21T19:30:00.000001+05:30", False),
                                                  ("2023-01-21T06:00:00-08:00", True), ("2023-01-21T06:00:00.000001-08:00", False), ("2023-01-22T03:59:59.999999+14:00", True),
                                                  ("2023-01-21T02:00:00.000001-12:00", False)])
def test_the_boundary_is_inclusive_to_the_microsecond_in_any_zone(available_at, served, make_session, tmp_path):
    assert [r.record_id for r in pit_query([rec("a", available_at)], T(AS_OF)).records] == (["a"] if served else [])
    p = tmp_path / "d.csv"                                                                             # and the same through a file source and a session
    p.write_text(f"id,available_at\na,{available_at}\n")
    s = make_session(AS_OF)
    s.add_source(sources.CSV(p, event_time=None, available_at="available_at", key=("id",)))
    assert len(s.query("d")) == (1 if served else 0)
    assert s.receipt().blocked.get("future", 0) == (0 if served else 1)


@pytest.mark.parametrize("zone, local, instant_utc", [
    ("America/New_York", "2023-11-05T01:30:00", "2023-11-05T06:30:00Z"),      # clocks go back: 01:30 happens twice (05:30Z and 06:30Z). The LATER one is used
    ("America/New_York", "2023-03-12T02:30:00", "2023-03-12T07:30:00Z"),      # clocks go forward: 02:30 does not exist. Read with the offset that gives the later instant
    ("Europe/Berlin", "2023-10-29T02:30:00", "2023-10-29T01:30:00Z"),         # EU autumn: 02:30 twice (00:30Z and 01:30Z)
    ("Europe/Berlin", "2023-03-26T02:30:00", "2023-03-26T01:30:00Z"),         # EU spring: 02:30 does not exist
    ("America/New_York", "2023-07-01T12:00:00", "2023-07-01T16:00:00Z"),      # an ordinary summer time, for contrast
    ("Europe/Berlin", "2023-01-10T12:00:00", "2023-01-10T11:00:00Z"),         # and an ordinary winter time
])
def test_a_zone_less_time_read_in_a_named_zone_resolves_dst_to_the_later_instant_so_it_can_only_be_late(zone, local, instant_utc, tmp_path):
    p = tmp_path / "d.csv"
    p.write_text(f"id,available_at\na,{local}\n")
    r = list(sources.CSV(p, event_time=None, available_at="available_at", key=("id",), assume_tz=zone).records())[0]
    assert r.available_at == T(instant_utc), (r.available_at, instant_utc)
    for clock, n in ((T(instant_utc) - US, 0), (T(instant_utc), 1)):                                   # blocked one microsecond before that instant, served at it
        assert len(pit_query([r], clock).records) == n


def test_naive_times_are_refused_or_withheld_never_assumed(make_session, tmp_path):
    for bad in ("2023-01-21T14:00:00", "2023-01-21", datetime(2023, 1, 21, 14)):
        with pytest.raises(NaiveTimeError):
            Session(bad, ledger_path=tmp_path / "x.db")                                                # the session clock
    s = make_session(AS_OF)
    with pytest.raises(NaiveTimeError):
        s.advance_to("2023-01-22T00:00:00")                                                            # a clock move
    assert s.as_of == T(AS_OF)
    with pytest.raises(ValueError, match="timezone-aware"):
        pit_query([], datetime(2023, 1, 21, 14))                                                       # the query function itself
    for field in ("event_time", "available_at", "recorded_at"):
        with pytest.raises(ValueError, match="timezone-aware"):
            Record("s", "a", **{"event_time": None, "available_at": None, field: datetime(2023, 1, 1)}, payload={})      # a record
    p = tmp_path / "d.csv"
    p.write_text("id,available_at\na,2023-01-01T00:00:00\nb,2023-01-01T00:00:00Z\n")                  # a zone-less cell in a file, with no assume_tz
    s.add_source(sources.CSV(p, event_time=None, available_at="available_at", key=("id",)))
    assert [r.record_id for r in s.query("d")] == ["b"]
    assert s.receipt().blocked["untimestamped"] == 1                                                   # withheld and counted, not read as UTC
    naive = [r for r in list(sources.CSV(p, event_time=None, available_at="available_at", key=("id",)).records()) if r.record_id == "a"][0]
    assert naive.available_at is None and "naive_available_at" in naive.flags


@pytest.mark.parametrize("cell, clock, served", [
    ("2024-02-29T12:00:00Z", "2024-02-29T12:00:00Z", True),                   # leap day
    ("2024-02-29T12:00:00Z", "2024-02-29T11:59:59.999999Z", False),
    ("2024-02-29", "2024-02-29T23:59:59.999998Z", False),                     # a date with no time means the END of that day
    ("2024-02-29", "2024-02-29T23:59:59.999999Z", True),
    ("2023-02-29T12:00:00Z", "2030-01-01T00:00:00Z", False),                  # not a real date: unparseable, so withheld for ever
    ("2016-12-31T23:59:60Z", "2030-01-01T00:00:00Z", False),                  # a leap second: Python cannot represent it, so it is withheld, not rounded
    ("2023-12-31T23:59:59.999999Z", "2023-12-31T23:59:59.999999Z", True),     # year end
    ("2024-01-01T00:00:00Z", "2023-12-31T23:59:59.999999Z", False),
    ("1970-01-01T00:00:00Z", "1970-01-01T00:00:00Z", True),                   # the epoch
    ("1970-01-01T00:00:00Z", "1969-12-31T23:59:59.999999Z", False),
    ("1969-07-20T20:17:00Z", "1970-01-01T00:00:00Z", True),                   # before the epoch
    ("2100-01-01T00:00:00Z", "2099-12-31T23:59:59.999999Z", False),           # far future
    ("2100-01-01T00:00:00Z", "2100-01-01T00:00:00Z", True),
    ("2100-02-29T00:00:00Z", "2101-01-01T00:00:00Z", False),                  # 2100 is not a leap year
    ("9999-12-31T23:59:59.999999Z", "9999-12-31T23:59:59.999999Z", True),     # the last instant Python has
    ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z", True),                   # and the first
])
def test_calendar_edges(cell, clock, served, tmp_path):
    p = tmp_path / "d.csv"
    p.write_text(f"id,available_at\na,{cell}\n")
    with Session(clock, ledger_path=tmp_path / "l.db") as s:
        s.add_source(sources.CSV(p, event_time=None, available_at="available_at", key=("id",), assume_tz="UTC"))
        assert len(s.query("d")) == (1 if served else 0)
        assert s.verify_ledger().ok


def test_a_lookback_at_the_edge_of_the_calendar_is_a_clean_answer_not_a_crash(tmp_path):
    """`as_of - lookback` leaves the range of dates Python has when the clock is in year 1 (or the lookback is enormous). The window then simply starts at the first instant there is."""
    with Session("0001-01-02T00:00:00Z", ledger_path=tmp_path / "l.db") as s:
        s.add_source(ListSource("d", [rec("a", "0001-01-01T00:00:00Z", event_time="0001-01-01T00:00:00Z")]))
        assert [r.record_id for r in s.query("d", lookback="30d")] == ["a"]
    with Session(AS_OF, ledger_path=tmp_path / "m.db") as s:
        s.add_source(ListSource("d", [rec("a", "2023-01-01T00:00:00Z", event_time="2023-01-01T00:00:00Z")]))
        assert [r.record_id for r in s.query("d", lookback=timedelta(days=900000))] == ["a"]           # longer than the calendar: everything up to T
        for absurd in ("999999999999999d", "1e400d"):
            with pytest.raises(ValueError):
                s.query("d", lookback=absurd)                                                          # a number too large to be a duration is a plain usage error
        assert s.verify_ledger().ok


# ------------------------------------------------------------------ 3. versions and restatements
def _versions():
    """One record id, eight versions: three restatements over time, then a five-way exact tie on the last availability (two with explicit versions, one with only a recorded_at, two bare)."""
    t = "2023-01-20T00:00:00Z"
    return [Record("s", "a", None, T("2023-01-10T00:00:00Z"), {"v": "first"}), Record("s", "a", None, T("2023-01-15T00:00:00Z"), {"v": "second"}, version=9),
            Record("s", "a", None, T(t), {"v": "tie-version-1"}, version=1), Record("s", "a", None, T(t), {"v": "tie-version-2"}, version=2, recorded_at=T("2023-01-01T00:00:00Z")),
            Record("s", "a", None, T(t), {"v": "tie-recorded"}, recorded_at=T("2023-01-19T00:00:00Z")), Record("s", "a", None, T(t), {"v": "tie-bare-1"}), Record("s", "a", None, T(t), {"v": "tie-bare-2"}),
            Record("s", "a", None, T("2023-02-01T00:00:00Z"), {"v": "later-correction"}, version=99)]


def test_restatements_and_ties_give_one_answer_across_100_shuffles_of_the_input():
    rows = _versions()
    expect = {"2023-01-09T00:00:00Z": None, "2023-01-10T00:00:00Z": "first", "2023-01-19T23:59:59.999999Z": "second",       # the highest VERSION NUMBER does not win: the latest available does
              "2023-01-20T00:00:00Z": "tie-version-2", "2023-01-31T00:00:00Z": "tie-version-2", "2023-02-01T00:00:00Z": "later-correction"}
    rnd = random.Random(20261111)
    for n in range(100):
        shuffled = [Record(r.source_id, r.record_id, r.event_time, r.available_at, r.payload, order=i, version=r.version, recorded_at=r.recorded_at) for i, r in enumerate(rnd.sample(rows, len(rows)))]
        for clock, want in expect.items():
            got = pit_query(shuffled, T(clock)).records
            assert [r.payload["v"] for r in got] == ([want] if want else []), (n, clock)
    tie = pit_query(rows, T("2023-01-20T00:00:00Z"))
    assert tie.counts["ambiguous_versions"] == 1 and tie.counts["superseded"] == 6 and any("identical availability" in w for w in tie.warnings)


def test_the_tie_ladder_explicit_version_then_recorded_at_then_payload_hash():
    t = T("2023-01-20T00:00:00Z")
    bare = [Record("s", "a", None, t, {"v": i}) for i in range(5)]
    by_hash = max(bare, key=lambda r: r.payload_sha256)
    assert pit_query(bare, t).records[0].payload == by_hash.payload and pit_query(bare[::-1], t).records[0].payload == by_hash.payload      # nothing declared: the content decides, either way round
    stamped = bare + [Record("s", "a", None, t, {"v": "older stamp"}, recorded_at=T("2023-01-01T00:00:00Z")), Record("s", "a", None, t, {"v": "newer stamp"}, recorded_at=T("2023-01-02T00:00:00Z"))]
    assert pit_query(stamped, t).records[0].payload["v"] == "newer stamp"                                                                  # any recorded_at beats none; the later one wins
    versioned = stamped + [Record("s", "a", None, t, {"v": "v0"}, version=0), Record("s", "a", None, t, {"v": "v1"}, version=1, recorded_at=T("2000-01-01T00:00:00Z"))]
    assert pit_query(versioned, t).records[0].payload["v"] == "v1"                                                                         # any explicit version beats any recorded_at
    same = versioned + [Record("s", "a", None, t, {"v": "v1 again, stamped later"}, version=1, recorded_at=T("2023-01-05T00:00:00Z"))]
    assert pit_query(same, t).records[0].payload["v"] == "v1 again, stamped later"                                                         # same version: recorded_at settles it


def test_a_file_source_can_declare_version_and_recorded_at_columns_so_a_tie_is_settled_by_the_data_not_by_a_hash(tmp_path, make_session):
    """Found in the campaign: the tie ladder existed, but no built-in source could feed it (only hand-built Records carried a version), so a tie in a CSV always fell through to the hash."""
    rows = ["a,2023-01-20T00:00:00Z,1,2023-01-19T00:00:00Z,draft", "a,2023-01-20T00:00:00Z,3,2023-01-19T02:00:00Z,final", "a,2023-01-20T00:00:00Z,2,2023-01-19T05:00:00Z,middle",
            "b,2023-01-20T00:00:00Z,,2023-01-19T00:00:00Z,b-early", "b,2023-01-20T00:00:00Z,,2023-01-19T09:00:00Z,b-late", "c,2023-01-20T00:00:00Z,not-a-number,,c-odd"]
    rnd = random.Random(7)
    for n in range(100):
        p = tmp_path / f"d{n}.csv"
        p.write_text("id,available_at,ver,loaded_at,note\n" + "\n".join(rnd.sample(rows, len(rows))) + "\n")
        src = sources.CSV(p, name="d", event_time=None, available_at="available_at", record_id="id", version="ver", recorded_at="loaded_at")
        got = {r.record_id: r.payload["note"] for r in pit_query(list(src.records()), T(AS_OF)).records}
        assert got == {"a": "final", "b": "b-late", "c": "c-odd"}, n
    recs = {r.payload["note"]: r for r in src.records()}
    assert recs["final"].version == 3 and recs["final"].recorded_at == T("2023-01-19T02:00:00Z") and recs["b-late"].version is None
    assert recs["c-odd"].version is None and "unparseable_version" in recs["c-odd"].flags and "missing_recorded_at" in recs["c-odd"].flags      # a bad cell is flagged, never guessed
    s = make_session(AS_OF)
    s.add_source(src)
    assert {r.payload["note"] for r in s.query("d")} == {"final", "b-late", "c-odd"}
    with pytest.raises(Exception, match="not in the data"):
        list(sources.CSV(p, name="e", event_time=None, available_at="available_at", record_id="id", version="nope").records())


# ------------------------------------------------------------------ 4. the forward-only clock
def test_every_way_of_moving_the_clock_back_or_past_its_ceiling_is_refused_and_logged(make_session):
    s = make_session(AS_OF, max_as_of="2023-02-01T00:00:00Z")
    start = s.as_of
    attempts = [(start - US, "rewind"), (start - timedelta(days=3650), "rewind"), ("1970-01-01T00:00:00Z", "rewind"), ("2023-01-21T15:59:59.999999+02:00", "rewind"),      # the same rewind, written in another zone
                (T("2023-02-01T00:00:00Z") + US, "ceiling"), ("2100-01-01T00:00:00Z", "ceiling"), ("9999-12-31T23:59:59.999999Z", "ceiling")]
    for to, _ in attempts:
        with pytest.raises(ClockError):
            s.advance_to(to)
        assert s.as_of == start                                                                         # the clock did not move
    assert s.advance_to(start) == start and s.advance_to("2023-01-21T16:00:00+02:00") == start         # to the SAME instant (in any zone): allowed, a no-op, nothing logged
    assert not s.events("clock_advance")
    assert len(s.events("clock_rewind_rejected")) == 4 and len(s.events("clock_ceiling_rejected")) == 3
    for e, (to, _) in zip(s.events("clock_rewind_rejected") + s.events("clock_ceiling_rejected"), attempts, strict=True):
        assert parse_time(e.data()["attempted"]) == parse_time(to) and parse_time(e.data()["current"]) == start      # each refusal names what was tried
    assert s.advance_to("2023-02-01T00:00:00Z") == T("2023-02-01T00:00:00Z")                           # the ceiling itself is reachable (inclusive)
    with pytest.raises(ClockError):
        s.advance_to(start)                                                                             # and now the old time is a rewind
    for junk in ("yesterday", "", "2023-13-01T00:00:00Z", None, 1674309600, 1674309600.5, ["2023-01-22T00:00:00Z"]):
        with pytest.raises((ValueError, TypeError)):
            s.advance_to(junk)                                                                          # not a time at all: refused before the clock is touched
    assert s.as_of == T("2023-02-01T00:00:00Z") and s.verify_ledger().ok


def test_two_threads_advancing_the_clock_never_move_it_backwards(make_session):
    s = make_session(AS_OF)
    s.add_source(ListSource("d", [rec(f"r{i}", (BASE + timedelta(seconds=i)).isoformat()) for i in range(0, 400, 7)]))
    targets = [BASE + timedelta(seconds=i) for i in range(1, 401)]
    rnd = random.Random(5)
    plans = [rnd.sample(targets, len(targets)), rnd.sample(targets, len(targets))]                     # each thread asks for all 400 times in its own random order
    seen: list[tuple[datetime, int]] = []
    errors: list[BaseException] = []
    gate = threading.Barrier(2)

    def run(plan):
        try:
            gate.wait()
            for to in plan:
                try:
                    s.advance_to(to)
                except ClockError:
                    pass
                rows = s.query("d")                                                                    # a query between moves, as an agent would
                now = s.as_of
                assert all(r.available_at <= now for r in rows)
                seen.append((now, len(rows)))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=run, args=(p,)) for p in plans]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, errors[:1]
    assert s.as_of == targets[-1]                                                                       # both asked for the last time at some point: it is where the clock ends
    moves = [parse_time(e.data()["to"]) for e in s.events("clock_advance")]
    assert moves == sorted(moves) and len(set(moves)) == len(moves)                                    # the logged moves only ever go forward
    refused = len(s.events("clock_rewind_rejected"))
    assert len(moves) + refused <= 800 and refused > 0                                                  # (a request for the current instant is a no-op and is in neither)
    assert len(s.events("query")) == 800 and s.verify_ledger().ok


# ------------------------------------------------------------------ 5. stepping through time against a fresh query each time
def test_one_session_stepped_through_500_random_times_equals_a_fresh_as_of_query_at_each(tmp_path):
    rnd = random.Random(20261111)
    span = 200 * 86400
    data = []
    for i in range(300):
        rid = f"r{rnd.randrange(60)}"
        avail = BASE + timedelta(seconds=rnd.randrange(span), microseconds=rnd.randrange(3))
        data.append(Record("d", rid, avail - timedelta(hours=rnd.randrange(72)), avail if rnd.random() > 0.05 else None, {"v": i, "k": rnd.choice("xy")}, order=i,
                           version=rnd.choice([None, None, 1, 2]), recorded_at=None))
    data += [Record("d", "tied", None, BASE + timedelta(days=50), {"v": f"t{i}"}, order=1000 + i) for i in range(4)]
    times = sorted(BASE + timedelta(seconds=rnd.randrange(span), microseconds=rnd.randrange(3)) for _ in range(500))
    times[100] = times[99]                                                                              # a step that does not move the clock at all
    mismatches = 0
    with Session(BASE, ledger_path=tmp_path / "long.db") as long:
        long.add_source(ListSource("d", data))
        for n, clock in enumerate(times):
            long.advance_to(clock)
            kw = {} if n % 3 else {"k": "x", "lookback": "30d", "limit": 25}                           # every third step with a filter, a window and a limit
            stepped = [(r.record_id, r.payload_sha256) for r in long.query("d", **kw)]
            with Session(clock, ledger_path=tmp_path / "fresh" / f"{n}.db") as fresh:                  # a brand-new session at that time: its own load of the source, its own ledger
                fresh.add_source(ListSource("d", list(reversed(data))))                               # (handed the rows in the opposite order, too)
                new = [(r.record_id, r.payload_sha256) for r in fresh.query("d", **kw)]
                counts = (fresh.receipt().served, dict(fresh.receipt().blocked))
            mismatches += stepped != new or (long.receipt().served, dict(long.receipt().blocked)) != counts
        assert long.verify_ledger().ok
    assert mismatches == 0
    assert len(Ledger(tmp_path / "long.db").events(type="clock_advance")) == len(set(times)) - (1 if times[0] == BASE else 0)
