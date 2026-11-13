"""The Source interface and the shared record-building rules.

How a source turns a row into a :class:`~asof.records.Record` decides what can leak, so it is defined once, here:

* ``available_at`` missing, unparseable or without a time zone  ->  the record is built with ``available_at=None`` and a flag.
  It is then withheld by default (R2). A naive timestamp is never assumed to be UTC.
* To read zone-less timestamps on purpose, pass ``assume_tz="America/New_York"`` (or ``"UTC"``).
* A date with no time of day is ``available_at`` at the **end** of that day (conservative), and ``event_time`` at the start.
* ``available_at`` earlier than ``event_time`` is impossible for real data; the record is flagged ``available_before_event``.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ..errors import SourceError
from ..records import Record

UTC = timezone.utc


def _zone(name: str | None):
    if name is None:
        return None
    if name.upper() in ("UTC", "Z"):
        return UTC
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as e:  # noqa: BLE001 - ZoneInfoNotFoundError, or no tz database on this platform
        raise SourceError(f"unknown time zone {name!r} ({e}); install the 'tzdata' package if your platform has no tz database") from e


def parse_source_time(value: Any, *, assume_tz: str | None, end_of_day: bool) -> tuple[datetime | None, str | None]:
    """Return ``(instant_in_utc_or_None, problem_or_None)``. Never guesses a zone."""
    if value is None or (isinstance(value, float) and math.isnan(value)) or (isinstance(value, str) and not value.strip()):
        return None, "missing"
    zone = _zone(assume_tz)
    dt: datetime | None = None
    date_only = False
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt, date_only = datetime.combine(value, time(0)), True
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            date_only = len(text) <= 10 and "T" not in text and " " not in text
        except ValueError:
            return None, "unparseable"
    else:
        return None, "unparseable"
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        if zone is None:
            return None, "naive"
        # A local wall-clock time can be ambiguous (clocks go back) or non-existent (clocks go forward). Resolve to the LATER instant: for
        # availability, being late withholds data, being early leaks it.
        dt = max((dt.replace(tzinfo=zone, fold=0), dt.replace(tzinfo=zone, fold=1)), key=lambda d: d.astimezone(UTC))
    if date_only and end_of_day:
        dt = datetime.combine(dt.date(), time(0), tzinfo=dt.tzinfo) + timedelta(days=1) - timedelta(microseconds=1)
    return dt.astimezone(UTC), None


def json_safe(value: Any) -> Any:
    """Make a cell value canonical-JSON safe: NaN/inf become None."""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


class Source(ABC):
    """Anything that can enumerate records, including ones from the future. The time lock is applied later, in :mod:`asof.query`."""

    name: str
    calendar: str | None = None          # a market calendar for the data-quality gap check only (asof.calendars); None: the source runs around the clock

    @abstractmethod
    def records(self) -> Iterable[Record]:
        """Every record version the source holds. Must not filter by time: filtering is the query layer's job."""


class RowSource(Source):
    """A source whose records come from rows (mappings). Subclasses implement :meth:`rows`."""

    def __init__(self, *, name: str, event_time: str | None, available_at: str | None, record_id: str | None = None,
                 key: Sequence[str] | None = None, assume_tz: str | None = None, date_only_available_at: str = "end_of_day",
                 version: str | None = None, recorded_at: str | None = None, calendar: str | None = None) -> None:
        """``version`` names a column holding the source's own version number of a row (a whole number); ``recorded_at`` names a column holding when the source recorded that version.
        They matter only when several versions of one record share the same ``available_at``: the highest version is served, then the latest ``recorded_at`` (see :mod:`asof.query`).
        Neither is ever used to decide WHETHER a row is visible: that is ``available_at`` alone.
        ``calendar`` (``"XNYS"``, ``"weekdays"`` or ``None``) tells the data-quality check when the market is closed, so nights, weekends and holidays are not reported as gaps.
        It changes nothing that is served."""
        from .. import calendars
        try:
            self.calendar = calendars.check(calendar)
        except ValueError as e:
            raise SourceError(str(e)) from None
        if date_only_available_at not in ("end_of_day", "start_of_day"):
            raise SourceError("date_only_available_at must be 'end_of_day' or 'start_of_day'")
        if record_id and key:
            raise SourceError("give record_id or key, not both")
        self.name = name
        self.event_time_field, self.available_at_field = event_time, available_at
        self.record_id_field, self.key_fields = record_id, tuple(key or ())
        self.assume_tz = assume_tz
        self.version_field, self.recorded_at_field = version, recorded_at
        self.end_of_day = date_only_available_at == "end_of_day"
        _zone(assume_tz)                                          # fail at construction, not on first query

    @abstractmethod
    def rows(self) -> Iterator[Mapping[str, Any]]: ...

    def records(self) -> Iterator[Record]:
        for n, row in enumerate(self.rows()):
            yield self.build(row, n)

    def build(self, row: Mapping[str, Any], n: int) -> Record:
        payload = {str(k): json_safe(v) for k, v in row.items()}
        flags: list[str] = []
        times: dict[str, datetime | None] = {}
        for label, field, eod in (("event_time", self.event_time_field, False), ("available_at", self.available_at_field, self.end_of_day)):
            if field is None:
                times[label] = None
                flags.append(f"missing_{label}")
                continue
            if field not in row:
                raise SourceError(f"source {self.name!r}: column {field!r} (for {label}) is not in the data; columns are {sorted(row)}")
            t, problem = parse_source_time(row[field], assume_tz=self.assume_tz, end_of_day=eod)
            times[label] = t
            if problem:
                flags.append(f"{problem}_{label}")
        ev, av = times["event_time"], times["available_at"]
        if ev is not None and av is not None and av < ev:
            flags.append("available_before_event")
        version, stamp = self._tie_fields(row, flags)
        return Record(source_id=self.name, record_id=self._record_id(row, n), event_time=ev, available_at=av, payload=payload,
                      flags=tuple(flags), order=n, version=version, recorded_at=stamp)

    def _tie_fields(self, row: Mapping[str, Any], flags: list[str]) -> tuple[int | None, datetime | None]:
        """The row's own version number and recording time, when the source names columns for them. A cell that cannot be read is flagged and left out: it is never guessed."""
        version: int | None = None
        stamp: datetime | None = None
        if self.version_field is not None:
            if self.version_field not in row:
                raise SourceError(f"source {self.name!r}: column {self.version_field!r} (for version) is not in the data; columns are {sorted(row)}")
            raw = row[self.version_field]
            if raw is None or (isinstance(raw, float) and math.isnan(raw)) or (isinstance(raw, str) and not raw.strip()):
                flags.append("missing_version")
            elif isinstance(raw, int) and not isinstance(raw, bool):
                version = raw
            elif isinstance(raw, float) and raw.is_integer():
                version = int(raw)
            elif isinstance(raw, str) and raw.strip().lstrip("+-").isdigit():
                version = int(raw.strip())
            else:
                flags.append("unparseable_version")
        if self.recorded_at_field is not None:
            if self.recorded_at_field not in row:
                raise SourceError(f"source {self.name!r}: column {self.recorded_at_field!r} (for recorded_at) is not in the data; columns are {sorted(row)}")
            stamp, problem = parse_source_time(row[self.recorded_at_field], assume_tz=self.assume_tz, end_of_day=False)
            if problem:
                flags.append(f"{problem}_recorded_at")
        return version, stamp

    def _record_id(self, row: Mapping[str, Any], n: int) -> str:
        if self.record_id_field:
            if self.record_id_field not in row:
                raise SourceError(f"source {self.name!r}: record_id column {self.record_id_field!r} is not in the data")
            return str(row[self.record_id_field])
        if self.key_fields:
            missing = [k for k in self.key_fields if k not in row]
            if missing:
                raise SourceError(f"source {self.name!r}: key column(s) {missing} are not in the data")
            return "|".join(str(row[k]) for k in self.key_fields)
        return f"row:{n}"                                         # no identity given: restatements cannot be detected (documented)
