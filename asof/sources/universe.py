"""Universe-as-of: who was in an index or a watch list on a given date, as it was *knowable* then.

Index membership is usually published as a table of the current members, or as intervals ``(symbol, start, end)``. Both leak the future: the current list leaves out everything that
was dropped since (survivorship bias), and an interval's ``end`` was not known on its first day. A universe is therefore stored as a log of **changes**, one row per add or remove,
with the two clocks every AsOf record has:

* ``effective``: the date the change took effect (the record's ``event_time``);
* ``announced``: when it became public (the record's ``available_at``), which is what the time lock uses.

At time T the members are what the changes **announced by T** and **effective by T** add up to. A change announced by T but effective later is *pending* (not yet a member, and not yet
gone); a change effective by T but announced after T is **blocked** like any other future record, so a retroactive correction never reaches an earlier session. Ask for the members with
``Session.members(name)``, or restrict another source to them with ``Session.query(source, universe=(name, "symbol"))``.

Dates with no time zone are **not** assumed to be UTC (the rule for every source): pass ``assume_tz="America/New_York"`` (or ``"UTC"``) or the changes are withheld and the receipt's data-quality
flags say why (``unusable_timestamps``). A date without a time of day is the start of that day for ``effective`` and the **end** of that day for ``announced``.

Rows::

    symbol,action,effective,announced
    AAA,add,2020-01-02,2019-12-20
    BBB,add,2020-01-02,2019-12-20
    BBB,remove,2022-06-30,2022-06-10
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ..errors import SourceError
from ..records import Record
from .base import RowSource

ACTIONS = ("add", "remove")


class Universe(RowSource):
    """A membership change log (see the module docstring). Give ``rows`` (a list of mappings) or ``path`` (a CSV file with a header row).

    A change is normally **announced before it takes effect**, so ``available_at`` earlier than ``event_time`` is expected here and is not flagged ``available_before_event`` (the receipt
    counts it as ``announced_ahead``); and a change log arrives at irregular intervals, so gaps and staleness are not measured for it (``quality_kind = "change_log"``)."""

    quality_kind = "change_log"

    def __init__(self, rows: Iterable[Mapping[str, Any]] | None = None, *, path: str | Path | None = None, name: str, member: str = "symbol", action: str = "action",
                 effective: str = "effective", announced: str = "announced", assume_tz: str | None = None, date_only_available_at: str = "end_of_day",
                 listed_in: str | None = None, listed_field: str | None = None) -> None:
        """``listed_in`` names another source of the session that holds a record for every name trading on every session, normally prices (``listed_field`` is its column with the
        name; default: the same as ``member``). A query on this universe then checks for **survivorship**: names with a record on the latest trading date visible at T in that
        source, which this universe does not list at T. Any such name raises the ``survivorship`` flag on the query's receipt, with their number (and, in privacy mode ``full``,
        the names). Only records visible at T are used, so the check adds no information about the future."""
        if (rows is None) == (path is None):
            raise SourceError("a Universe needs rows or a path, not both and not neither")
        self.listed_in, self.listed_field = listed_in, listed_field
        self._rows = None if rows is None else [dict(r) for r in rows]
        self.path = None if path is None else Path(path).expanduser()
        self.member_field, self.action_field = member, action
        super().__init__(name=name, event_time=effective, available_at=announced, key=(member, action, effective), assume_tz=assume_tz,
                         date_only_available_at=date_only_available_at)

    def build(self, row: Mapping[str, Any], n: int) -> Record:
        rec = super().build(row, n)
        return replace(rec, flags=tuple(f for f in rec.flags if f != "available_before_event"))        # announced ahead of the effective date: the normal case for an index change

    def rows(self) -> Iterator[Mapping[str, Any]]:
        if self.path is not None:
            from .files import CSV
            src: Iterable[Mapping[str, Any]] = CSV(self.path, event_time=self.event_time_field, available_at=self.available_at_field, name=self.name, coerce="none").rows()
        else:
            src = self._rows or []
        for n, row in enumerate(src):
            for col in (self.member_field, self.action_field):
                if col not in row:
                    raise SourceError(f"universe {self.name!r}: column {col!r} is not in the data; columns are {sorted(row)}")
            act = str(row[self.action_field]).strip().lower()
            if act not in ACTIONS:
                raise SourceError(f"universe {self.name!r}: row {n} has action {row[self.action_field]!r}; it must be 'add' or 'remove'")
            who = row[self.member_field]
            if who is None or not str(who).strip():
                raise SourceError(f"universe {self.name!r}: row {n} has no member in column {self.member_field!r}")
            yield {**row, self.action_field: act, self.member_field: str(who).strip()}


@dataclass(frozen=True)
class Membership:
    members: tuple[str, ...]
    pending_adds: int = 0               # announced by T, effective after T
    pending_removes: int = 0
    duplicate_adds: int = 0             # an add for someone already a member
    orphan_removes: int = 0             # a remove for someone who was never added (or already removed)


def members_as_of(records: Sequence[Record], as_of: datetime, *, member: str = "symbol", action: str = "action") -> Membership:
    """Replay change records that are already time-locked (``available_at <= as_of``) up to their effective date. Pure: no clock, no I/O."""
    live: set[str] = set()
    pending_adds = pending_removes = dup = orphan = 0
    for r in sorted((r for r in records if r.event_time is not None), key=lambda r: (r.event_time, r.available_at or r.event_time, r.order)):
        who, act = str(r.payload.get(member)), str(r.payload.get(action))
        if r.event_time > as_of:                                      # type: ignore[operator]
            if act == "add":
                pending_adds += 1
            else:
                pending_removes += 1
            continue
        if act == "add":
            if who in live:
                dup += 1
            live.add(who)
        else:
            if who not in live:
                orphan += 1
            live.discard(who)
    return Membership(tuple(sorted(live)), pending_adds, pending_removes, dup, orphan)
