"""A read-only SQL source."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from ..errors import SourceError
from .base import RowSource

_READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|truncate|grant|revoke|merge|call|exec)\b", re.IGNORECASE)


def check_read_only(sql: str) -> str:
    """Accept one SELECT/WITH statement and nothing else. This is a guard against accidents, not a security boundary: also give the
    connection read-only credentials."""
    stripped = sql.strip().rstrip(";").strip()
    if ";" in stripped:
        raise SourceError("only a single SQL statement is allowed")
    if not _READ_ONLY.match(stripped):
        raise SourceError("only SELECT queries are allowed in a read-only SQL source")
    # keywords inside string literals or identifiers would false-positive; strip quoted text first
    unquoted = re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", "", stripped)
    bad = _FORBIDDEN.search(unquoted)
    if bad:
        raise SourceError(f"the keyword {bad.group(0).upper()!r} is not allowed in a read-only SQL source")
    return stripped


class SQL(RowSource):
    """Rows from a SELECT query.

    ``database`` is a path to a SQLite file (opened read-only by the driver, with ``query_only`` on) or an open DB-API connection (a
    statement guard still applies; use read-only credentials). The query must return the time columns named by ``event_time`` / ``available_at``.
    Include **every** version of a restated row, not just the latest: the time lock picks the right one.
    """

    def __init__(self, database: str | Path | Any, query: str, *, event_time: str | None, available_at: str | None, name: str,
                 params: Sequence[Any] = (), record_id: str | None = None, key: Sequence[str] | None = None, assume_tz: str | None = None,
                 date_only_available_at: str = "end_of_day", version: str | None = None, recorded_at: str | None = None, calendar: str | None = None) -> None:
        self.query = check_read_only(query)
        self.params = tuple(params)
        self.database = database
        super().__init__(name=name, event_time=event_time, available_at=available_at, record_id=record_id, key=key, assume_tz=assume_tz,
                         date_only_available_at=date_only_available_at, version=version, recorded_at=recorded_at, calendar=calendar)

    def rows(self) -> Iterator[Mapping[str, Any]]:
        own = isinstance(self.database, (str, Path))
        try:
            conn: Any
            if own:
                path = Path(self.database).expanduser()
                if not path.exists():
                    raise SourceError(f"database {path} does not exist")
                conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
                conn.execute("PRAGMA query_only = ON")
            else:
                conn = self.database
            cur = conn.cursor()
            try:
                cur.execute(self.query, self.params)
                cols = [d[0] for d in cur.description or []]
                for row in cur:
                    yield dict(zip(cols, row, strict=True))
            finally:
                cur.close()
                if own:
                    conn.close()
        except sqlite3.Error as e:
            raise SourceError(f"SQL source {self.name!r} failed: {e}") from e
