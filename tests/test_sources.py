import json
import sqlite3

import pytest

from asof import Session, SourceError, sources

from conftest import T


def csv_file(tmp_path, text, name="prices.csv"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_csv_builds_records_with_both_times_a_hash_and_coerced_numbers(tmp_path):
    p = csv_file(tmp_path, "symbol,date,close,code,available_at\nACME-SYN,2023-01-20,150.5,007,2023-01-20T21:00:00Z\n")
    [r] = sources.CSV(p, event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC").records()
    assert r.source_id == "prices" and r.record_id == "ACME-SYN|2023-01-20" and r.available_at == T("2023-01-20T21:00:00Z")
    assert r.payload["close"] == 150.5 and r.payload["code"] == "007"                    # '007' stays text: it is an identifier, not a number
    assert r.verify() and len(r.payload_sha256) == 64 and r.event_time == T("2023-01-20T00:00:00Z")


def test_a_naive_available_at_is_never_assumed_to_be_utc(tmp_path):
    p = csv_file(tmp_path, "id,t,a\n1,2023-01-20T10:00:00Z,2023-01-20T10:00:00\n")
    [r] = sources.CSV(p, event_time="t", available_at="a", record_id="id").records()
    assert r.available_at is None and "naive_available_at" in r.flags
    s = Session("2030-01-01T00:00:00Z", ledger_path=tmp_path / "l.db")
    s.add_source(sources.CSV(p, event_time="t", available_at="a", record_id="id"))
    assert s.query("prices" if False else p.stem) == []                                    # withheld, however far T is
    assert s.events("source_loaded")[0].data()["flags"] == {"naive_available_at": 1}      # and the problem is visible in the ledger
    s.close()


def test_assume_tz_is_an_explicit_opt_in_and_uses_that_zone(tmp_path):
    p = csv_file(tmp_path, "id,t,a\n1,2023-01-20T09:00:00,2023-01-20T09:00:00\n")
    [r] = sources.CSV(p, event_time="t", available_at="a", record_id="id", assume_tz="America/New_York").records()
    assert r.available_at == T("2023-01-20T14:00:00Z")


def test_a_date_only_available_at_is_the_end_of_that_day_so_it_cannot_leak_hours(tmp_path):
    p = csv_file(tmp_path, "id,d,a\n1,2023-01-20,2023-01-20\n")
    [r] = sources.CSV(p, event_time="d", available_at="a", record_id="id", assume_tz="UTC").records()
    assert r.available_at > T("2023-01-20T23:59:59Z") and r.available_at < T("2023-01-21T00:00:00Z") and r.event_time == T("2023-01-20T00:00:00Z")
    [r2] = sources.CSV(p, event_time="d", available_at="a", record_id="id", assume_tz="UTC", date_only_available_at="start_of_day").records()
    assert r2.available_at == T("2023-01-20T00:00:00Z")


def test_missing_and_unparseable_times_are_flagged_and_withheld(tmp_path):
    p = csv_file(tmp_path, "id,t,a\n1,2023-01-20T00:00:00Z,\n2,2023-01-20T00:00:00Z,not-a-date\n3,,2023-01-20T00:00:00Z\n")
    r1, r2, r3 = sources.CSV(p, event_time="t", available_at="a", record_id="id").records()
    assert r1.available_at is None and "missing_available_at" in r1.flags
    assert r2.available_at is None and "unparseable_available_at" in r2.flags
    assert r3.event_time is None and r3.available_at is not None


def test_a_missing_time_column_is_an_error_not_a_silent_untimestamped_file(tmp_path):
    p = csv_file(tmp_path, "id,t\n1,2023-01-20T00:00:00Z\n")
    with pytest.raises(SourceError, match="column 'a'"):
        list(sources.CSV(p, event_time="t", available_at="a", record_id="id").records())
    with pytest.raises(SourceError, match="cannot open"):
        list(sources.CSV(tmp_path / "nope.csv", event_time="t", available_at="a").records())


def test_available_before_event_is_flagged(tmp_path):
    p = csv_file(tmp_path, "id,e,a\n1,2023-01-20T12:00:00Z,2023-01-19T12:00:00Z\n")
    [r] = sources.CSV(p, event_time="e", available_at="a", record_id="id").records()
    assert "available_before_event" in r.flags


def test_default_record_ids_are_row_numbers_so_restatements_need_a_key(tmp_path):
    p = csv_file(tmp_path, "id,t,a\nx,2023-01-20T00:00:00Z,2023-01-20T00:00:00Z\nx,2023-01-20T00:00:00Z,2023-01-22T00:00:00Z\n")
    assert [r.record_id for r in sources.CSV(p, event_time="t", available_at="a").records()] == ["row:0", "row:1"]
    assert [r.record_id for r in sources.CSV(p, event_time="t", available_at="a", record_id="id").records()] == ["x", "x"]
    with pytest.raises(SourceError, match="not both"):
        sources.CSV(p, event_time="t", available_at="a", record_id="id", key=("id",))


def test_jsonl_and_its_errors(tmp_path):
    p = tmp_path / "news.jsonl"
    p.write_text(json.dumps({"id": "a", "t": "2023-01-01T00:00:00Z", "title": "x"}) + "\n\n" + json.dumps({"id": "b", "t": "2023-01-02T00:00:00Z"}) + "\n")
    assert [r.record_id for r in sources.JSONL(p, event_time="t", available_at="t", record_id="id").records()] == ["a", "b"]
    p.write_text('{"id": "a"}\n{broken\n')
    with pytest.raises(SourceError, match="line 2"):
        list(sources.JSONL(p, event_time=None, available_at=None, record_id="id").records())
    p.write_text("[1,2]\n")
    with pytest.raises(SourceError, match="not a JSON object"):
        list(sources.JSONL(p, event_time=None, available_at=None).records())


def test_parquet_keeps_time_zone_awareness(tmp_path):
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq
    from datetime import datetime, timezone
    table = pa.table({"id": ["a", "b"], "t": pa.array([datetime(2023, 1, 1, tzinfo=timezone.utc), datetime(2023, 1, 2, tzinfo=timezone.utc)], pa.timestamp("us", tz="UTC")),
                      "naive": pa.array([datetime(2023, 1, 1), datetime(2023, 1, 2)], pa.timestamp("us")), "x": [1.5, None]})
    p = tmp_path / "d.parquet"
    pq.write_table(table, p)
    good = list(sources.Parquet(p, event_time="t", available_at="t", record_id="id").records())
    assert good[0].available_at == T("2023-01-01T00:00:00Z") and good[1].payload["x"] is None
    naive = list(sources.Parquet(p, event_time="t", available_at="naive", record_id="id").records())
    assert all(r.available_at is None and "naive_available_at" in r.flags for r in naive)       # a zone-less Parquet column is not trusted either


# ------------------------------------------------------------------ SQL
@pytest.fixture
def db(tmp_path):
    path = tmp_path / "x.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE px (sym TEXT, d TEXT, close REAL, avail TEXT)")
    c.executemany("INSERT INTO px VALUES (?,?,?,?)", [("ACME-SYN", "2023-01-20", 150.0, "2023-01-20T21:00:00Z"), ("ACME-SYN", "2023-01-20", 149.0, "2023-01-25T21:00:00Z")])
    c.commit()
    c.close()
    return path


def test_sql_reads_every_version_and_the_lock_picks_one(db, tmp_path):
    s = Session("2023-01-21T00:00:00Z", ledger_path=tmp_path / "l.db")
    s.add_source(sources.SQL(db, "SELECT * FROM px", event_time="d", available_at="avail", name="px", key=("sym", "d"), assume_tz="UTC"))
    assert [r.payload["close"] for r in s.query("px")] == [150.0]
    s.advance_to("2023-02-01T00:00:00Z")
    assert [r.payload["close"] for r in s.query("px")] == [149.0]
    s.close()


@pytest.mark.parametrize("sql", ["DROP TABLE px", "DELETE FROM px", "INSERT INTO px VALUES ('a','b',1,'c')", "UPDATE px SET close=0", "SELECT 1; DROP TABLE px",
                                 "WITH x AS (SELECT 1) DELETE FROM px", "PRAGMA writable_schema=1", "ATTACH DATABASE 'z' AS z", "  \n create table t(x)"])
def test_sql_refuses_anything_but_a_single_select(sql, db):
    with pytest.raises(SourceError):
        sources.SQL(db, sql, event_time="d", available_at="avail", name="x")


def test_sql_allows_keywords_inside_string_literals_and_leaves_the_database_untouched(db):
    src = sources.SQL(db, "SELECT sym, 'please DELETE nothing' AS note, d, avail FROM px", event_time="d", available_at="avail", name="x", assume_tz="UTC")
    assert len(list(src.records())) == 2
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM px").fetchone()[0] == 2


def test_sql_missing_database_and_bad_query_are_source_errors(tmp_path, db):
    with pytest.raises(SourceError, match="does not exist"):
        list(sources.SQL(tmp_path / "none.db", "SELECT 1", event_time=None, available_at=None, name="x").records())
    with pytest.raises(SourceError, match="failed"):
        list(sources.SQL(db, "SELECT * FROM nope", event_time=None, available_at=None, name="x").records())


# ------------------------------------------------------------------ MacroCalendar
def test_macro_calendar_vintages_serve_the_revision_that_existed_at_T(tmp_path):
    p = csv_file(tmp_path, "series,period,value,release_time\nCPI,2023-01,100.0,2023-02-14T13:30:00Z\nCPI,2023-01,100.4,2023-03-14T12:30:00Z\nCPI,2023-02,101.0,2023-03-14T12:30:00Z\n", "macro.csv")
    s = Session("2023-03-01T00:00:00Z", ledger_path=tmp_path / "l.db")
    s.add_source(sources.MacroCalendar(p))
    assert [(r.payload["period"], r.payload["value"]) for r in s.query("macro")] == [("2023-01", 100.0)]
    s.advance_to("2023-03-14T12:30:00Z")                                                           # inclusive boundary
    assert sorted((r.payload["period"], r.payload["value"]) for r in s.query("macro")) == [("2023-01", 100.4), ("2023-02", 101.0)]
    assert s.query("macro", series="CPI")[0].event_time == T("2023-01-01T00:00:00Z")
    s.close()


def test_macro_calendar_demands_a_zone_and_the_required_columns(tmp_path):
    naive = csv_file(tmp_path, "series,period,value,release_time\nCPI,2023-01,1,2023-02-14 08:30:00\n", "m1.csv")
    assert list(sources.MacroCalendar(naive).records())[0].available_at is None
    assert list(sources.MacroCalendar(naive, release_tz="America/New_York").records())[0].available_at == T("2023-02-14T13:30:00Z")
    bad = csv_file(tmp_path, "series,period,value\nCPI,2023-01,1\n", "m2.csv")
    with pytest.raises(SourceError, match="release_time"):
        list(sources.MacroCalendar(bad).records())
