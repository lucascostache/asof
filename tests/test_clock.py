from datetime import datetime, timedelta, timezone

import pytest

from asof import ClockError, NaiveTimeError, Session, parse_duration, parse_time


def test_naive_datetimes_and_strings_are_rejected(make_session):
    with pytest.raises(NaiveTimeError):
        parse_time(datetime(2023, 1, 21, 14, 0))
    with pytest.raises(NaiveTimeError):
        parse_time("2023-01-21T14:00:00")
    with pytest.raises(NaiveTimeError):
        make_session("2023-01-21 14:00")
    with pytest.raises(NaiveTimeError):
        make_session(datetime(2023, 1, 21))


def test_zones_are_normalised_to_utc():
    assert parse_time("2023-01-21T14:00:00Z") == parse_time("2023-01-21T09:00:00-05:00") == parse_time("2023-01-21T14:00:00+00:00")
    assert parse_time("2023-01-21T09:00:00-05:00").utcoffset() == timedelta(0)
    assert parse_time(datetime(2023, 1, 21, 14, tzinfo=timezone(timedelta(hours=2)))).hour == 12


def test_garbage_is_not_a_time():
    for bad in ("tomorrow", "", "2023-13-45T00:00:00Z"):
        with pytest.raises(ValueError):
            parse_time(bad)
    with pytest.raises(TypeError):
        parse_time(1674309600)


def test_durations():
    assert parse_duration("30d") == timedelta(days=30) and parse_duration("12h") == timedelta(hours=12) and parse_duration("2w") == timedelta(weeks=2)
    assert parse_duration("90m") == timedelta(minutes=90) and parse_duration(timedelta(hours=1)) == timedelta(hours=1)
    for bad in ("1mo", "1y", "d", "-5d", "30", "", None):
        with pytest.raises((ValueError, TypeError)):
            parse_duration(bad)  # type: ignore[arg-type]


def test_r5_the_clock_only_moves_forward_and_every_change_is_logged(make_session):
    s = make_session("2023-01-21T14:00:00Z")
    s.advance_to("2023-01-22T14:00:00Z")
    s.advance_to("2023-01-22T15:00:00-00:00")
    assert s.as_of == parse_time("2023-01-22T15:00:00Z")
    adv = [e.data() for e in s.events("clock_advance")]
    assert [(a["from"], a["to"]) for a in adv] == [("2023-01-21T14:00:00Z", "2023-01-22T14:00:00Z"), ("2023-01-22T14:00:00Z", "2023-01-22T15:00:00Z")]


def test_r5_rewinding_is_refused_logged_and_does_not_move_the_clock(make_session):
    s = make_session("2023-01-21T14:00:00Z")
    s.advance_to("2023-01-25T00:00:00Z")
    with pytest.raises(ClockError, match="backwards"):
        s.advance_to("2023-01-22T00:00:00Z")
    assert s.as_of == parse_time("2023-01-25T00:00:00Z")                      # still at the maximum: no way to rewind, look, then jump past it
    rej = s.events("clock_rewind_rejected")
    assert len(rej) == 1 and rej[0].data() == {"current": "2023-01-25T00:00:00Z", "attempted": "2023-01-22T00:00:00Z"}


def test_advancing_to_the_same_instant_is_a_silent_noop(make_session):
    s = make_session("2023-01-21T14:00:00Z")
    s.advance_to("2023-01-21T09:00:00-05:00")
    assert s.events("clock_advance") == []


def test_advance_rejects_naive_times(make_session):
    s = make_session()
    with pytest.raises(NaiveTimeError):
        s.advance_to("2023-02-01T00:00:00")
    assert s.events("clock_advance") == []


def test_a_closed_session_refuses_further_use(make_session):
    s = make_session()
    s.close()
    with pytest.raises(Exception, match="closed"):
        s.advance_to("2024-01-01T00:00:00Z")
