"""The session clock.

A clock holds one instant, ``as_of`` (timezone-aware, normalised to UTC). It can only move forward (rule R5).
Every change is reported through ``on_change`` so the session can log it (rule R6). A request to move backwards is
refused and reported through ``on_reject``; the clock does not move.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Union

from .errors import ClockError, NaiveTimeError

UTC = timezone.utc
TimeLike = Union[str, datetime]

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.IGNORECASE)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_time(value: TimeLike, *, what: str = "time") -> datetime:
    """Parse a timezone-aware instant and return it in UTC.

    Accepts an aware ``datetime`` or an ISO-8601 string (``Z`` or an explicit offset). A naive value is rejected:
    without a zone it names no single instant, and guessing would silently shift data across the time lock.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as e:
            raise ValueError(f"{what} {value!r} is not an ISO-8601 timestamp") from e
    else:
        raise TypeError(f"{what} must be a datetime or an ISO-8601 string, not {type(value).__name__}")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise NaiveTimeError(f"{what} {value!r} has no time zone; give an explicit offset such as 'Z' or '+00:00'")
    return dt.astimezone(UTC)


def parse_duration(value: Union[str, timedelta]) -> timedelta:
    """``"30d"``, ``"12h"``, ``"90m"``, ``"2w"``, ``"45s"`` or a ``timedelta``. Months and years are refused: they have no fixed length."""
    if isinstance(value, timedelta):
        if value < timedelta(0):
            raise ValueError("a lookback must not be negative")
        return value
    m = _DURATION.match(value) if isinstance(value, str) else None
    if not m:
        raise ValueError(f"cannot read duration {value!r}; use a number and one of s, m, h, d, w (for example '30d')")
    try:
        return timedelta(seconds=float(m.group(1)) * _UNITS[m.group(2).lower()])
    except OverflowError:
        raise ValueError(f"duration {value!r} is too large (the longest there is, is about 2.7 million years)") from None


class Clock:
    """A forward-only instant, with an optional ceiling the clock can never pass.

    ``on_change(old, new)`` is called after every move. ``on_reject(current, attempted, reason)`` is called when a move is refused, with ``reason``
    ``"rewind"`` (earlier than now) or ``"ceiling"`` (later than ``max_as_of``); the clock does not move in either case.
    """

    def __init__(self, as_of: TimeLike, *, max_as_of: TimeLike | None = None, on_change: Callable[[datetime, datetime], None] | None = None,
                 on_reject: Callable[[datetime, datetime, str], None] | None = None) -> None:
        self._now = parse_time(as_of, what="as_of")
        self._start = self._now
        self._max = parse_time(max_as_of, what="max_as_of") if max_as_of is not None else None
        if self._max is not None and self._now > self._max:
            raise ClockError(f"as_of {self._now.isoformat()} is after the ceiling max_as_of {self._max.isoformat()}")
        self._on_change = on_change
        self._on_reject = on_reject

    @property
    def now(self) -> datetime:
        return self._now

    @property
    def start(self) -> datetime:
        """The instant the clock was created at."""
        return self._start

    @property
    def ceiling(self) -> datetime | None:
        """The latest instant the clock may ever reach (inclusive), or ``None``."""
        return self._max

    def advance_to(self, to: TimeLike) -> datetime:
        """Move to ``to``. Equal is a no-op (nothing logged); earlier or past the ceiling raises :class:`ClockError` and the clock stays put."""
        target = parse_time(to, what="advance_to")
        if target == self._now:
            return self._now
        if target < self._now:
            if self._on_reject:
                self._on_reject(self._now, target, "rewind")
            raise ClockError(f"the clock cannot move backwards ({self._now.isoformat()} -> {target.isoformat()}); "
                             "start a new Session to look at an earlier time")
        if self._max is not None and target > self._max:
            if self._on_reject:
                self._on_reject(self._now, target, "ceiling")
            raise ClockError(f"the clock cannot pass its ceiling max_as_of={self._max.isoformat()} (asked for {target.isoformat()})")
        old, self._now = self._now, target
        if self._on_change:
            self._on_change(old, target)
        return self._now
