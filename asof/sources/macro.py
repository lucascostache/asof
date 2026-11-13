"""MacroCalendar: economic releases with their real release times, from a static file you maintain."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..errors import SourceError
from .base import UTC
from .files import CSV, JSONL

_PERIOD = re.compile(r"^(\d{4})(?:-(\d{2})|[-]?Q([1-4]))?$", re.IGNORECASE)
REQUIRED = ("series", "period", "release_time")


def parse_period(value: Any) -> datetime:
    """The start of a reference period: ``2023-01-31`` -> that day, ``2023-01`` -> Jan 1, ``2023Q2`` -> Apr 1, ``2023`` -> Jan 1 (all UTC midnight)."""
    s = str(value).strip()
    m = _PERIOD.match(s)
    if m:
        year, month, quarter = int(m.group(1)), m.group(2), m.group(3)
        mo = int(month) if month else (int(quarter) - 1) * 3 + 1 if quarter else 1
        return datetime(year, mo, 1, tzinfo=UTC)
    try:
        d = datetime.fromisoformat(s)
    except ValueError as e:
        raise SourceError(f"cannot read reference period {value!r}; use YYYY, YYYY-MM, YYYYQn or YYYY-MM-DD") from e
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC)


class MacroCalendar(CSV):
    """A file with one row per *release* of a figure.

    Required columns: ``series`` (e.g. ``CPI``), ``period`` (the reference period the figure describes) and ``release_time`` (when it was published).
    Any other columns (``value``, ``unit``, ...) ride along. A revision is another row with the same ``series`` and ``period`` and a later
    ``release_time``; the time lock then serves the revision that existed at T and never a later one (R3).

    ``release_time`` must carry a time zone, or you pass ``release_tz="America/New_York"`` to say how zone-less times are to be read.
    Format: CSV by default; ``fmt="jsonl"`` for JSON lines.
    """

    def __init__(self, path: str | Path, *, name: str = "macro", release_tz: str | None = None, fmt: str = "csv") -> None:
        if fmt not in ("csv", "jsonl"):
            raise SourceError("fmt must be 'csv' or 'jsonl'")
        super().__init__(path, event_time="period_start", available_at="release_time", name=name, record_id="record_id", assume_tz=release_tz)
        self.fmt = fmt

    def rows(self) -> Iterator[Mapping[str, Any]]:
        base = CSV.rows(self) if self.fmt == "csv" else JSONL.rows(self)  # type: ignore[arg-type]
        for n, row in enumerate(base):
            missing = [c for c in REQUIRED if c not in row or row[c] in (None, "")]
            if missing:
                raise SourceError(f"{self.path}: row {n + 1} is missing {missing}; every release needs series, period and release_time")
            out = dict(row)
            out["record_id"] = f"{row['series']}:{row['period']}"
            out["period_start"] = parse_period(row["period"]).isoformat().replace("+00:00", "Z")
            yield out
