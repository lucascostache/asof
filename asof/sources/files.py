"""File sources: CSV, JSONL, Parquet."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from ..errors import SourceError
from .base import RowSource

_INT = re.compile(r"^[+-]?(0|[1-9]\d*)$")
_FLOAT = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


def _coerce(cell: str) -> Any:
    """CSV cells are text. Convert plain integers and decimals; keep everything else (including '007', dates, ids) as text."""
    s = cell.strip()
    if s == "":
        return None
    if _INT.match(s) and len(s) < 16:
        return int(s)
    if _FLOAT.match(s) and not (s.lstrip("+-").startswith("0") and len(s.lstrip("+-")) > 1 and s.lstrip("+-")[1].isdigit()):
        try:
            return float(s)
        except ValueError:
            return cell
    return cell


class CSV(RowSource):
    """A CSV file with a header row.

    ``event_time`` and ``available_at`` name the columns holding the two times. Identity for restatements: ``record_id="col"`` or
    ``key=("symbol", "date")``. ``coerce="auto"`` turns plain numbers into numbers (``"none"`` keeps every cell as text).
    """

    def __init__(self, path: str | Path, *, event_time: str | None, available_at: str | None, name: str | None = None, record_id: str | None = None,
                 key: Sequence[str] | None = None, assume_tz: str | None = None, date_only_available_at: str = "end_of_day",
                 coerce: str = "auto", delimiter: str = ",", encoding: str = "utf-8-sig", version: str | None = None, recorded_at: str | None = None, calendar: str | None = None) -> None:
        self.path = Path(path).expanduser()
        if coerce not in ("auto", "none"):
            raise SourceError("coerce must be 'auto' or 'none'")
        self.coerce, self.delimiter, self.encoding = coerce, delimiter, encoding
        super().__init__(name=name or self.path.stem, event_time=event_time, available_at=available_at, record_id=record_id, key=key,
                         assume_tz=assume_tz, date_only_available_at=date_only_available_at, version=version, recorded_at=recorded_at, calendar=calendar)

    def rows(self) -> Iterator[Mapping[str, Any]]:
        try:
            f = open(self.path, newline="", encoding=self.encoding)
        except OSError as e:
            raise SourceError(f"cannot open {self.path}: {e}") from e
        with f:
            reader = csv.DictReader(f, delimiter=self.delimiter)
            if reader.fieldnames is None:
                raise SourceError(f"{self.path} is empty")
            for line in reader:
                if None in line:
                    raise SourceError(f"{self.path}: line {reader.line_num} has more cells than the header")
                yield {k: (_coerce(v) if self.coerce == "auto" and v is not None else v) for k, v in line.items()}


class JSONL(RowSource):
    """One JSON object per line. A line that is not valid JSON stops the load with its line number: nothing is skipped silently."""

    def __init__(self, path: str | Path, *, event_time: str | None, available_at: str | None, name: str | None = None, record_id: str | None = None,
                 key: Sequence[str] | None = None, assume_tz: str | None = None, date_only_available_at: str = "end_of_day",
                 version: str | None = None, recorded_at: str | None = None, calendar: str | None = None) -> None:
        self.path = Path(path).expanduser()
        super().__init__(name=name or self.path.stem, event_time=event_time, available_at=available_at, record_id=record_id, key=key,
                         assume_tz=assume_tz, date_only_available_at=date_only_available_at, version=version, recorded_at=recorded_at, calendar=calendar)

    def rows(self) -> Iterator[Mapping[str, Any]]:
        try:
            f = open(self.path, encoding="utf-8")
        except OSError as e:
            raise SourceError(f"cannot open {self.path}: {e}") from e
        with f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    raise SourceError(f"{self.path}: line {n} is not valid JSON ({e.msg})") from e
                if not isinstance(obj, dict):
                    raise SourceError(f"{self.path}: line {n} is not a JSON object")
                yield obj


class Parquet(RowSource):
    """A Parquet file (needs ``pyarrow``: ``pip install 'pyarrow>=14'``, or AsOf's ``[parquet]`` extra). Timestamp columns that carry no time zone are flagged, not assumed UTC."""

    def __init__(self, path: str | Path, *, event_time: str | None, available_at: str | None, name: str | None = None, record_id: str | None = None,
                 key: Sequence[str] | None = None, assume_tz: str | None = None, date_only_available_at: str = "end_of_day",
                 version: str | None = None, recorded_at: str | None = None, calendar: str | None = None) -> None:
        self.path = Path(path).expanduser()
        super().__init__(name=name or self.path.stem, event_time=event_time, available_at=available_at, record_id=record_id, key=key,
                         assume_tz=assume_tz, date_only_available_at=date_only_available_at, version=version, recorded_at=recorded_at, calendar=calendar)

    def rows(self) -> Iterator[Mapping[str, Any]]:
        try:
            import pyarrow.parquet as pq
        except ImportError as e:
            raise SourceError("reading Parquet needs pyarrow, which is not installed in this Python: pip install 'pyarrow>=14'") from e
        try:
            table = pq.read_table(self.path)
        except (OSError, ValueError) as e:
            raise SourceError(f"cannot read {self.path}: {e}") from e
        yield from table.to_pylist()
