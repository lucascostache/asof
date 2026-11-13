"""Decision 4: a survivorship FLAG. A universe declared with ``listed_in="prices"`` checks, at every query, whether names that were trading at T (a price on the latest trading date
visible at T) are missing from the universe at T. Only data visible at T is used."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from asof import Session, quality as dq
from asof.errors import SourceError
from asof.records import Record
from asof.sources import Universe

from conftest import ListSource

UTC = timezone.utc
T = datetime(2023, 3, 1, 15, 0, tzinfo=UTC)


def prices(names_by_day: dict[date, list[str]]) -> list[Record]:
    out = []
    for d, names in sorted(names_by_day.items()):
        for n in names:
            t = datetime(d.year, d.month, d.day, tzinfo=UTC)
            out.append(Record("prices", f"{n}|{d}", t, t + timedelta(hours=21), {"symbol": n, "close": 1}, order=len(out)))
    return out


def days(a: date, n: int) -> list[date]:
    out, d = [], a
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = days(date(2023, 2, 1), 19)                                                     # 1 to 27 February, then T is 1 March 15:00 (prices of 28 Feb not yet available)
ALL = ["AAA", "BBB", "CCC", "DDD"]


def session(tmp_path, universe_rows, price_records, *, privacy="full", at=T, listed_in="prices"):
    s = Session(at, ledger_path=tmp_path / f"{len(list(tmp_path.iterdir()))}.db", privacy=privacy)
    s.add_source(ListSource("prices", price_records))
    s.add_source(Universe(universe_rows, name="uni", assume_tz="UTC", listed_in=listed_in))
    return s


def add(names, when="2023-01-01"):
    return [{"symbol": n, "action": "add", "effective": when, "announced": when} for n in names]


def test_a_universe_missing_a_name_that_is_trading_raises_the_flag_with_the_names(tmp_path):
    with session(tmp_path, add(["AAA", "BBB"]), prices({d: ALL for d in DAYS})) as s:
        assert sorted(s.members("uni")) == ["AAA", "BBB"]
        q = s.receipt().quality
    assert "survivorship" in q["flags"] and dq.SEVERITY["survivorship"] == "warning"
    listed = q["membership"]["listed"]
    assert listed["source"] == "prices" and listed["evidence_date"] == "2023-02-27" and listed["n_listed"] == 4
    assert listed["n_listed_not_members"] == 2 and listed["listed_not_members"] == ["CCC", "DDD"]


def test_a_complete_universe_raises_nothing(tmp_path):
    with session(tmp_path, add(ALL), prices({d: ALL for d in DAYS})) as s:
        s.members("uni")
        q = s.receipt().quality
    assert "survivorship" not in q["flags"] and q["membership"]["listed"]["n_listed_not_members"] == 0


def test_a_name_that_stopped_trading_before_T_is_not_listed_and_is_not_flagged(tmp_path):
    book = {d: (ALL if d < date(2023, 2, 15) else ["AAA", "BBB", "CCC"]) for d in DAYS}                     # DDD delisted mid-February
    uni = add(ALL) + [{"symbol": "DDD", "action": "remove", "effective": "2023-02-15", "announced": "2023-02-01"}]
    with session(tmp_path, uni, prices(book)) as s:
        assert sorted(s.members("uni")) == ["AAA", "BBB", "CCC"]
        assert "survivorship" not in s.receipt().quality["flags"]


def test_a_survivors_only_list_is_caught_before_the_delisting_and_is_right_after_it(tmp_path):
    book = {d: (ALL if d < date(2023, 2, 15) else ["AAA", "BBB", "CCC"]) for d in DAYS}
    survivors = add(["AAA", "BBB", "CCC"])                                                                 # the list "who trades today", used for every date
    with session(tmp_path, survivors, prices(book), at=datetime(2023, 2, 10, 15, 0, tzinfo=UTC)) as s:
        s.members("uni")
        assert s.receipt().quality["membership"]["listed"]["listed_not_members"] == ["DDD"]
    with session(tmp_path, survivors, prices(book)) as s:
        s.members("uni")
        assert "survivorship" not in s.receipt().quality["flags"]                                          # after DDD's last price, the list is correct at T


def test_nothing_after_T_is_used_a_listing_that_starts_later_is_not_evidence(tmp_path):
    later = {d: ALL + ["EEE"] for d in DAYS} | {date(2023, 3, 2): ALL + ["EEE", "FFF"]}                      # FFF appears only after T
    with session(tmp_path, add(ALL + ["EEE"]), prices(later)) as s:
        s.members("uni")
        q = s.receipt().quality
    assert "survivorship" not in q["flags"] and q["membership"]["listed"]["evidence_date"] == "2023-02-27"


def test_in_privacy_mode_hashes_only_the_names_are_not_written_only_their_count_and_hash(tmp_path):
    with session(tmp_path, add(["AAA"]), prices({d: ALL for d in DAYS}), privacy="hashes_only") as s:
        s.members("uni")
        listed = s.receipt().quality["membership"]["listed"]
        raw = s.ledger.path.read_bytes()
    assert "listed_not_members" not in listed and listed["n_listed_not_members"] == 3 and len(listed["listed_not_members_sha256"]) == 64
    assert b"BBB" not in raw and b"CCC" not in raw


def test_a_listed_in_source_that_is_not_in_the_session_is_refused(tmp_path):
    with session(tmp_path, add(ALL), prices({d: ALL for d in DAYS}), listed_in="quotes") as s:
        with pytest.raises(SourceError, match="listed_in='quotes' is not a source in this session"):
            s.members("uni")


def test_without_listed_in_the_universe_query_is_exactly_as_before(tmp_path):
    with session(tmp_path, add(["AAA"]), prices({d: ALL for d in DAYS}), listed_in=None) as s:
        s.members("uni")
        q = s.receipt().quality
    assert "listed" not in q["membership"] and "survivorship" not in q["flags"]


def test_the_agent_never_sees_the_check(tmp_path):
    with session(tmp_path, add(["AAA"]), prices({d: ALL for d in DAYS})) as s:
        s.members("uni")
        view = s.receipt().agent_view()
    assert "quality" not in view and "BBB" not in str(view)
