"""Market calendars for the data-quality checks: which local dates are trading sessions, so that a gap check can leave nights, weekends and exchange holidays out.

A calendar is used ONLY by :mod:`asof.quality` (gaps and staleness). It never decides whether a record is visible: that is ``available_at`` alone.

Two calendars:

* ``"XNYS"``: the New York Stock Exchange. Sessions are weekdays in ``America/New_York``, except the exchange's full-day closures: New Year's Day, Martin Luther King Jr. Day
  (from 1998), Washington's Birthday, Good Friday, Memorial Day, Juneteenth (from 2022), Independence Day, Labor Day, Thanksgiving and Christmas, moved to the Friday before
  when they fall on a Saturday (except New Year's Day, which is then not made up) and to the Monday after when they fall on a Sunday; and the unscheduled closures listed in
  ``XNYS_ONE_OFF``. Early closes (half days) are sessions. Supported for 1971 to 2099; a date outside that range is not called a session or a closure (``None``).
* ``"weekdays"``: Monday to Friday in UTC, no holidays.

What this does not know: an unscheduled closure after the list below was written, and rule changes after 2026. Both would show up as a missed session, never as a hidden one.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

NAMES = ("XNYS", "weekdays")
# Unscheduled full-day closures of the NYSE since 2001.
XNYS_ONE_OFF = frozenset({date(2001, 9, 11), date(2001, 9, 12), date(2001, 9, 13), date(2001, 9, 14),          # the September 11 attacks
                          date(2004, 6, 11),                                                                     # national day of mourning, President Reagan
                          date(2007, 1, 2),                                                                      # national day of mourning, President Ford
                          date(2012, 10, 29), date(2012, 10, 30),                                                # Hurricane Sandy
                          date(2018, 12, 5),                                                                     # national day of mourning, President G. H. W. Bush
                          date(2025, 1, 9)})                                                                     # national day of mourning, President Carter
FIRST_YEAR, LAST_YEAR = 1971, 2099


def check(name: str | None) -> str | None:
    if name is not None and name not in NAMES:
        raise ValueError(f"unknown calendar {name!r}; use one of {', '.join(NAMES)} (or none, for a source that runs around the clock)")
    return name


def zone(name: str) -> ZoneInfo:
    return ZoneInfo("America/New_York" if name == "XNYS" else "UTC")


def local_date(name: str, t: datetime) -> date:
    return t.astimezone(zone(name)).date()


def _easter(y: int) -> date:
    """Gregorian Easter Sunday (the anonymous algorithm in Meeus, 'Astronomical Algorithms')."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    return date(y, month, (h + l_ - 7 * m + 114) % 31 + 1)


def _nth_weekday(y: int, month: int, weekday: int, n: int) -> date:
    d = date(y, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y: int, month: int, weekday: int) -> date:
    d = date(y + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


@lru_cache(maxsize=None)
def xnys_holidays(y: int) -> frozenset[date]:
    """The NYSE's scheduled full-day closures in year ``y``."""
    if not FIRST_YEAR <= y <= LAST_YEAR:
        raise ValueError(f"the XNYS calendar covers {FIRST_YEAR} to {LAST_YEAR}, not {y}")
    days = set()
    ny = date(y, 1, 1)
    if ny.weekday() != 5:                                   # on a Saturday it is not made up (the exchange does not close on 31 December)
        days.add(_observed(ny))
    if y >= 1998:
        days.add(_nth_weekday(y, 1, 0, 3))                  # Martin Luther King Jr. Day: third Monday of January
    days.add(_nth_weekday(y, 2, 0, 3))                      # Washington's Birthday: third Monday of February
    days.add(_easter(y) - timedelta(days=2))                # Good Friday
    days.add(_last_weekday(y, 5, 0))                        # Memorial Day: last Monday of May
    if y >= 2022:
        days.add(_observed(date(y, 6, 19)))                 # Juneteenth
    days.add(_observed(date(y, 7, 4)))                      # Independence Day
    days.add(_nth_weekday(y, 9, 0, 1))                      # Labor Day: first Monday of September
    days.add(_nth_weekday(y, 11, 3, 4))                     # Thanksgiving: fourth Thursday of November
    days.add(_observed(date(y, 12, 25)))                    # Christmas
    return frozenset(d for d in days if d.year == y)


def is_session(name: str, d: date) -> bool | None:
    """Whether the exchange trades on local date ``d``; ``None`` for a date the calendar does not cover."""
    if d.weekday() >= 5:
        return False
    if name == "weekdays":
        return True
    if not FIRST_YEAR <= d.year <= LAST_YEAR:
        return None
    return d not in xnys_holidays(d.year) and d not in XNYS_ONE_OFF


def sessions_between(name: str, a: date, b: date) -> list[date] | None:
    """The session dates strictly between ``a`` and ``b``; ``None`` if any date in between is outside the calendar."""
    out, d = [], a + timedelta(days=1)
    while d < b:
        s = is_session(name, d)
        if s is None:
            return None
        if s:
            out.append(d)
        d += timedelta(days=1)
    return out
