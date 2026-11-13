"""OHLC bars with the time rules a backtest needs to be honest about, built in.

* **A bar is available when it closes, not when it opens.** ``available_at`` is the bar's close for its timeframe: ``open + 5 minutes`` for a ``5m`` bar, ``open + 1 hour`` for ``1h``, the session
  close for a ``1d`` bar when ``session_close`` is given (``"16:00"`` in ``session_tz``), or an explicit ``close_time`` column. A vendor's own ``available_at`` can only make a bar *later*:
  one earlier than the close is replaced by the close and the record is flagged ``availability_moved_to_close`` (it is counted in the data-quality notes).
* **A partial bar is never served by default.** The bar forming at T has ``available_at`` in the future, so the time lock withholds it like any other future record. Rows a source marks
  incomplete (``complete`` column false) are dropped and counted (``partial_bars_withheld``). With ``allow_partial=True`` they are served, flagged ``partial_bar``, and the receipt warns that partial
  bars were served; a partial row needs its own ``available_at`` (the time of the snapshot) and is withheld, counted, if it has none.
* **Higher timeframes are locked the same way.** Every row may carry its own ``timeframe`` (a ``timeframe`` column), so a daily or weekly bar is available only after its close even when it sits next to
  hourly bars: at 14:00 on Wednesday the daily bar for Wednesday and the weekly bar for this week are both still in the future.
* ``event_time`` is the bar's open. A bar whose high, low, open and close are inconsistent is kept and flagged ``inconsistent_ohlc``.

:func:`intrabar_ambiguous` answers a different question: when a stop loss and a take profit both lie inside one bar's range, the bar alone cannot say which was hit first.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from ..errors import SourceError
from ..records import Record
from .base import RowSource, UTC, _zone, json_safe, parse_source_time

_TF = re.compile(r"^(\d+)\s*(s|m|h|d|w)$", re.IGNORECASE)
_UNIT = {"s": timedelta(seconds=1), "m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1), "w": timedelta(weeks=1)}


def parse_timeframe(tf: str) -> tuple[int, str, timedelta]:
    """``"5m"`` -> ``(5, "m", timedelta(minutes=5))``. Units: s, m, h, d, w."""
    m = _TF.match(str(tf).strip())
    if not m or int(m.group(1)) <= 0:
        raise SourceError(f"timeframe {tf!r} is not like '1m', '5m', '1h', '1d' or '1w'")
    n, u = int(m.group(1)), m.group(2).lower()
    return n, u, n * _UNIT[u]


def intrabar_ambiguous(high: float, low: float, side: str, stop: float, target: float) -> bool:
    """True when both a stop loss and a take profit lie inside one bar's range, so the bar cannot say which was reached first.

    For a long position the stop is below the entry and the target above; for a short, the other way round. A stop on the wrong side of the target is refused (the caller has a mistake).
    A bar that touches only one level is not ambiguous; one that touches neither is not either."""
    s = str(side).lower()
    if s not in ("buy", "long", "sell", "short"):
        raise ValueError(f"side must be buy/long or sell/short, not {side!r}")
    if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (high, low, stop, target)):
        raise ValueError("high, low, stop and target must be finite numbers")
    if low > high:
        raise ValueError("a bar's low is above its high")
    long = s in ("buy", "long")
    if (long and not stop < target) or (not long and not stop > target):
        raise ValueError(f"for a {'long' if long else 'short'} the stop must be {'below' if long else 'above'} the target")
    hit_stop = low <= stop if long else high >= stop
    hit_target = high >= target if long else low <= target
    return bool(hit_stop and hit_target)


class Bars(RowSource):
    """OHLC bars (see the module docstring). Give ``rows`` (mappings) or ``path`` (a CSV with a header row).

    ``timeframe`` is the default for every row (``"1h"``); a ``timeframe_field`` column overrides it per row. Times with no zone need ``assume_tz`` (never assumed to be UTC)."""

    def __init__(self, rows: Iterable[Mapping[str, Any]] | None = None, *, path: str | Path | None = None, name: str, timeframe: str | None = "1h", timeframe_field: str | None = None,
                 open_time: str = "open_time", close_time: str | None = None, available_at: str | None = None, complete: str | None = None, allow_partial: bool = False,
                 session_close: str | None = None, session_tz: str | None = None, assume_tz: str | None = None, key: tuple[str, ...] | None = None,
                 calendar: str | None = None) -> None:
        if (rows is None) == (path is None):
            raise SourceError("Bars needs rows or a path, not both and not neither")
        if timeframe is None and timeframe_field is None:
            raise SourceError("give a timeframe or a timeframe_field column")
        if timeframe is not None:
            parse_timeframe(timeframe)
        if session_close is not None:
            if not re.match(r"^\d{1,2}:\d{2}$", session_close) or session_tz is None:
                raise SourceError("session_close is 'HH:MM' and needs session_tz (the zone that clock time is in)")
            _zone(session_tz)
        self._rows = None if rows is None else [dict(r) for r in rows]
        self.path = None if path is None else Path(path).expanduser()
        self.timeframe, self.timeframe_field = timeframe, timeframe_field
        self.close_time_field, self.vendor_available_field, self.complete_field = close_time, available_at, complete
        self.allow_partial = bool(allow_partial)
        self.session_close, self.session_tz = session_close, session_tz
        self.partial_withheld = 0
        self.moved_to_close = 0
        super().__init__(name=name, event_time=open_time, available_at=None, key=key, assume_tz=assume_tz, calendar=calendar)

    # -- the source's own account of what it did to the data, for the quality summary
    def quality_notes(self) -> dict[str, int]:
        return {"partial_bars_withheld": self.partial_withheld, "availability_moved_to_close": self.moved_to_close}

    def rows(self) -> Iterator[Mapping[str, Any]]:
        if self.path is not None:
            from .files import CSV
            yield from CSV(self.path, event_time=self.event_time_field, available_at=None, name=self.name, coerce="auto").rows()
        else:
            yield from (self._rows or [])

    def records(self) -> Iterator[Record]:
        self.partial_withheld = self.moved_to_close = 0
        for n, row in enumerate(self.rows()):
            rec = self.build(row, n)
            if rec is not None:
                yield rec

    def _close_of(self, open_dt: datetime, tf: str, row: Mapping[str, Any]) -> datetime:
        col = self.close_time_field
        if col is not None and row.get(col) not in (None, ""):
            c, problem = parse_source_time(row[col], assume_tz=self.assume_tz, end_of_day=False)
            if c is None:
                raise SourceError(f"bars {self.name!r}: close_time {row[col]!r} is {problem}")
            return c
        _, unit, delta = parse_timeframe(tf)
        if unit == "d" and self.session_close:
            z = _zone(self.session_tz)
            local = open_dt.astimezone(z)
            hh, mm = (int(x) for x in self.session_close.split(":"))
            day = local.date()
            close = datetime.combine(day, time(hh, mm), tzinfo=z)
            while close <= local:                                          # a bar opened after that clock time closes the next day
                day += timedelta(days=1)
                close = datetime.combine(day, time(hh, mm), tzinfo=z)
            return close.astimezone(UTC)
        return open_dt + delta

    def build(self, row: Mapping[str, Any], n: int) -> Record | None:     # type: ignore[override]
        if self.event_time_field not in row:
            raise SourceError(f"bars {self.name!r}: column {self.event_time_field!r} (the bar's open time) is not in the data; columns are {sorted(row)}")
        flags: list[str] = []
        tf = str(row.get(self.timeframe_field)) if self.timeframe_field and row.get(self.timeframe_field) not in (None, "") else self.timeframe
        if tf is None:
            raise SourceError(f"bars {self.name!r}: row {n} has no timeframe")
        parse_timeframe(tf)
        open_dt, problem = parse_source_time(row[str(self.event_time_field)], assume_tz=self.assume_tz, end_of_day=False)
        payload = {str(k): json_safe(v) for k, v in row.items()}
        payload["timeframe"] = tf
        if open_dt is None:
            flags.append(f"{problem}_event_time")
            return Record(source_id=self.name, record_id=self._record_id(row, n), event_time=None, available_at=None, payload=payload, flags=tuple(flags) + ("missing_available_at",), order=n)
        close = self._close_of(open_dt, tf, row)
        payload["bar_close"] = close.isoformat().replace("+00:00", "Z")
        complete = True if self.complete_field is None else str(row.get(self.complete_field)).strip().lower() not in ("false", "0", "no", "n", "", "none")
        vendor = None
        if self.vendor_available_field and row.get(self.vendor_available_field) not in (None, ""):
            vendor, vproblem = parse_source_time(row[self.vendor_available_field], assume_tz=self.assume_tz, end_of_day=False)
            if vendor is None:
                flags.append(f"{vproblem}_available_at")
        if not complete:
            if not self.allow_partial or vendor is None:
                self.partial_withheld += 1
                return None
            flags.append("partial_bar")
            avail = vendor                                                   # a snapshot: available when it was taken
        else:
            avail = close
            if vendor is not None and vendor < close:
                flags.append("availability_moved_to_close")
                self.moved_to_close += 1
            elif vendor is not None:
                avail = vendor                                               # later than the close: the vendor was slower, and that is the truth about when it was knowable
        hi, lo, op, cl = (row.get(k) for k in ("high", "low", "open", "close"))
        nums = [x for x in (hi, lo, op, cl) if isinstance(x, (int, float))]
        if len(nums) == 4 and (lo > hi or not (lo <= op <= hi) or not (lo <= cl <= hi)):                       # type: ignore[operator]
            flags.append("inconsistent_ohlc")
        return Record(source_id=self.name, record_id=self._record_id(row, n), event_time=open_dt, available_at=avail, payload=payload, flags=tuple(flags), order=n)
