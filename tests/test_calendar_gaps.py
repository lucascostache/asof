"""Decision 3: the gap check on a market calendar. Nights, weekends and exchange holidays are not gaps; a quiet stretch inside a day, or a trading day with nothing, is.
Without a calendar nothing changes (the summaries, and so the ledger hashes, are exactly as before)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from asof import Session, quality as dq
from asof.calendars import XNYS_ONE_OFF, is_session, sessions_between, xnys_holidays
from asof.errors import SourceError
from asof.records import Record
from asof.sources import CSV

UTC = timezone.utc
NOW = datetime(2026, 1, 1, tzinfo=UTC)

# The NYSE's published full-day closures (holiday schedules for these years), typed in by hand, not computed.
PUBLISHED = {
    2021: ["2021-01-01", "2021-01-18", "2021-02-15", "2021-04-02", "2021-05-31", "2021-07-05", "2021-09-06", "2021-11-25", "2021-12-24"],
    2022: ["2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20", "2022-07-04", "2022-09-05", "2022-11-24", "2022-12-26"],
    2023: ["2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29", "2023-06-19", "2023-07-04", "2023-09-04", "2023-11-23", "2023-12-25"],
    2024: ["2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25"],
    2025: ["2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25"],
    2026: ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"],
}


@pytest.mark.parametrize("year", sorted(PUBLISHED))
def test_the_computed_holidays_are_the_published_ones(year):
    assert sorted(d.isoformat() for d in xnys_holidays(year)) == PUBLISHED[year]


def test_sessions_weekends_one_off_closures_and_the_covered_range():
    assert is_session("XNYS", date(2023, 1, 3)) is True and is_session("XNYS", date(2023, 1, 7)) is False          # a Tuesday; a Saturday
    assert is_session("XNYS", date(2023, 1, 2)) is False                                                            # New Year's Day observed on Monday
    assert all(is_session("XNYS", d) is False for d in XNYS_ONE_OFF) and date(2025, 1, 9) in XNYS_ONE_OFF
    assert is_session("XNYS", date(1960, 3, 1)) is None and is_session("XNYS", date(2100, 3, 1)) is None            # outside the calendar: unknown, never "open"
    assert is_session("weekdays", date(2023, 1, 2)) is True                                                         # no holidays on that one
    assert sessions_between("XNYS", date(2023, 1, 6), date(2023, 1, 9)) == []                                       # Friday to Monday: nothing skipped
    assert sessions_between("XNYS", date(2022, 12, 30), date(2023, 1, 4)) == [date(2023, 1, 3)]                     # the holiday Monday is not a session


def news(days, hours=range(12, 21), skip=()):
    """One record an hour, 12:00 to 20:00 UTC, on each of ``days`` (dates), except the (date, hour) pairs in ``skip``."""
    out = []
    for d in days:
        for h in hours:
            if (d, h) in skip:
                continue
            t = datetime(d.year, d.month, d.day, h, tzinfo=UTC)
            out.append(Record("n", f"n{len(out)}", t, t + timedelta(minutes=2), {"i": len(out)}, order=len(out)))
    return out


def business_days(a, b, cal="XNYS"):
    d, out = a, []
    while d <= b:
        if is_session(cal, d):
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = business_days(date(2022, 12, 1), date(2023, 2, 28))                                                          # includes Christmas, New Year's, MLK and Presidents' Day


def test_without_a_calendar_every_night_is_a_gap_and_with_one_none_is():
    recs = news(DAYS)
    plain = dq.analyse_source(recs, now=NOW)
    assert plain["gaps"]["n_gaps"] == len(DAYS) - 1 and "gaps" in plain["flags"]                                    # each night and weekend
    assert set(plain["gaps"]) == {"median_step_seconds", "n_gaps", "largest_gap_seconds", "largest_gap_after"}       # no new keys: old summaries and hashes are unchanged
    cal = dq.analyse_source(recs, now=NOW, calendar="XNYS")
    g = cal["gaps"]
    assert g["n_gaps"] == 0 and "gaps" not in cal["flags"] and g["calendar"] == "XNYS" and g["median_step_seconds"] == 3600 and g["median_sessions_skipped"] == 0


def test_a_trading_day_with_nothing_is_a_gap_and_a_holiday_with_nothing_is_not():
    missing = date(2023, 1, 25)
    recs = news([d for d in DAYS if d != missing])
    g = dq.analyse_source(recs, now=NOW, calendar="XNYS")["gaps"]
    assert g["n_gaps"] == 1 and g["n_gaps_missed_sessions"] == 1 and g["n_sessions_missed"] == 1 and g["n_gaps_within_day"] == 0
    assert g["largest_gap_after"] == "2023-01-24T20:00:00Z"


def test_a_long_quiet_stretch_inside_a_day_is_a_gap():
    d = date(2023, 1, 25)
    recs = news(DAYS, hours=range(8, 21), skip={(d, h) for h in range(9, 20)})                                         # 08:00, then nothing until 20:00: 12 hours
    g = dq.analyse_source(recs, now=NOW, calendar="XNYS")["gaps"]
    assert g["n_gaps"] == 1 and g["n_gaps_within_day"] == 1 and g["largest_gap_seconds"] == 12 * 3600 and g["largest_gap_after"] == "2023-01-25T08:00:00Z"


def test_dates_without_a_time_are_read_as_dates_not_as_the_evening_before_in_new_york():
    """Found while building this: dates read as midnight UTC fell on the previous evening in New York, so every Friday became a Thursday and every weekend a missed session."""
    recs = [Record("d", f"d{i}", datetime(d.year, d.month, d.day, tzinfo=UTC), datetime(d.year, d.month, d.day, 22, tzinfo=UTC), {"i": i}, order=i) for i, d in enumerate(DAYS)]
    g = dq.analyse_source(recs, now=NOW, calendar="XNYS")["gaps"]
    assert g["n_gaps"] == 0 and g["median_sessions_skipped"] == 0
    g2 = dq.analyse_source([r for r in recs if r.event_time.date() != date(2023, 2, 3)], now=NOW, calendar="XNYS")["gaps"]         # take out one Friday
    assert g2["n_gaps"] == 1 and g2["n_sessions_missed"] == 1


def test_sparse_series_are_measured_against_their_own_spacing():
    quarter_ends = [date(2022, 3, 31), date(2022, 6, 30), date(2022, 9, 30), date(2022, 12, 30), date(2023, 3, 31), date(2023, 6, 30)]
    recs = [Record("q", f"q{i}", datetime(d.year, d.month, d.day, tzinfo=UTC), datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(days=30), {"i": i}, order=i)
            for i, d in enumerate(quarter_ends)]
    g = dq.analyse_source(recs, now=NOW, calendar="XNYS")["gaps"]
    assert g["n_gaps"] == 0 and 60 <= g["median_sessions_skipped"] <= 64                                               # about 62 sessions between quarter ends: not a gap


def test_a_date_outside_the_calendar_makes_the_answer_unknown_not_zero():
    recs = [Record("o", f"o{i}", datetime(1960, 1, 4 + i, 15, tzinfo=UTC), datetime(1960, 1, 4 + i, 16, tzinfo=UTC), {"i": i}, order=i) for i in range(5)]
    g = dq.analyse_source(recs, now=NOW, calendar="XNYS")["gaps"]
    assert g["n_gaps"] is None and "gaps" not in dq.analyse_source(recs, now=NOW, calendar="XNYS")["flags"]


def _csv(tmp_path, recs):
    p = tmp_path / "news.csv"
    p.write_text("id,t,a\n" + "".join(f"{r.record_id},{r.event_time.isoformat().replace('+00:00', 'Z')},{r.available_at.isoformat().replace('+00:00', 'Z')}\n" for r in recs))
    return p


def test_a_monday_morning_query_is_not_stale_because_of_the_weekend(tmp_path):
    p = _csv(tmp_path, news(DAYS))
    monday = datetime(2023, 1, 30, 11, 0, tzinfo=UTC)                                                                 # 06:00 New York, before the day's first story (12:00 UTC)
    results = {}
    for cal in (None, "XNYS"):
        with Session(monday, ledger_path=tmp_path / f"{cal}.db") as s:
            s.add_source(CSV(p, name="news", event_time="t", available_at="a", record_id="id", assume_tz="UTC", calendar=cal))
            rows = s.query("news", lookback="10d")
            results[cal] = (s.receipt().quality, [r.record_id for r in rows])
    plain, cal = results[None][0], results["XNYS"][0]
    assert plain["freshness"]["stale"] is True and "stale" in plain["flags"] and "gaps" in plain["flags"]             # the weekend, read as missing data
    assert cal["freshness"]["stale"] is False and "stale" not in cal["flags"] and "gaps" not in cal["flags"]
    assert cal["freshness"]["calendar"] == "XNYS" and cal["freshness"]["sessions_since_newest"] == 0
    assert results[None][1] == results["XNYS"][1]                                                                      # the calendar changes nothing that is served


def test_a_query_after_a_missed_trading_day_is_stale(tmp_path):
    p = _csv(tmp_path, news([d for d in DAYS if d <= date(2023, 1, 26)]))                                              # nothing after Thursday 26 January
    with Session(datetime(2023, 1, 31, 11, 0, tzinfo=UTC), ledger_path=tmp_path / "l.db") as s:                       # Tuesday morning: Friday and Monday passed with nothing
        s.add_source(CSV(p, name="news", event_time="t", available_at="a", record_id="id", assume_tz="UTC", calendar="XNYS"))
        s.query("news", lookback="10d")
        f = s.receipt().quality["freshness"]
    assert f["stale"] is True and f["sessions_since_newest"] == 2


def test_an_unknown_calendar_is_refused_when_the_source_is_made(tmp_path):
    with pytest.raises(SourceError, match="unknown calendar 'NYSE'"):
        CSV(tmp_path / "x.csv", event_time="t", available_at="a", calendar="NYSE")


def test_the_mcp_config_accepts_a_calendar(tmp_path):
    pytest.importorskip("mcp")
    from asof.mcp_server import session_from_config
    _csv(tmp_path, news(DAYS[:5]))
    cfg = {"as_of": "2023-03-01T00:00:00Z", "ledger_path": "l.db",
           "sources": [{"type": "csv", "name": "news", "path": "news.csv", "event_time": "t", "available_at": "a", "record_id": "id", "assume_tz": "UTC", "calendar": "XNYS"}]}
    s, _ = session_from_config(cfg, tmp_path)
    try:
        q = s.quality()["news"]
        assert q["gaps"]["calendar"] == "XNYS" and q["gaps"]["n_gaps"] == 0
    finally:
        s.close()


def test_a_calendar_summary_holds_no_float_so_it_hashes_the_same_in_every_language():
    g = dq.analyse_source(news(DAYS), now=NOW, calendar="XNYS")
    assert not any(isinstance(v, float) for v in g["gaps"].values())
    json.dumps(g, allow_nan=False)
