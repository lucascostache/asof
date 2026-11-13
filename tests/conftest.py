from __future__ import annotations

from datetime import datetime, timezone

import pytest

from asof import Record, Session

UTC = timezone.utc


def T(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def rec(record_id: str, available_at: str | None, payload: dict | None = None, event_time: str | None = None, order: int = 0, source: str = "s") -> Record:
    return Record(source, record_id, T(event_time) if event_time else None, T(available_at) if available_at else None, payload or {"id": record_id}, order=order)


class ListSource:
    """A source backed by an in-memory list: the simplest way to hand exact records to a session."""
    def __init__(self, name: str, records: list[Record]):
        self.name, self._r = name, records

    def records(self):
        return iter(self._r)


@pytest.fixture
def ledger_path(tmp_path):
    return tmp_path / "ledger.db"


@pytest.fixture
def make_session(ledger_path):
    made = []

    def _make(as_of="2023-01-21T14:00:00Z", **kw):
        kw.setdefault("ledger_path", ledger_path)
        s = Session(as_of, **kw)
        made.append(s)
        return s

    yield _make
    for s in made:
        s.close()
