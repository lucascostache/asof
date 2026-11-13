"""Time-stamped agent memory: every memory has an ``available_at``, the moment it was learned, and a recall at time T returns only memories learned at or before T.

An agent that carries notes from run to run is a leak waiting to happen in a backtest: a note written in 2024 must not be recalled by a session standing at 2023. This store is an ordinary AsOf :class:`Source`, so the
same rules apply to it as to prices and news: R1 (available_at <= T, inclusive), R3 (a note rewritten under the same ``key`` replaces the earlier one only from the moment the rewrite was learned), a blocked memory is
counted in the receipt, and every recall through a session is a logged, time-locked query.

Small on purpose: SQLite from the standard library, append-only (no update, no delete: a correction is a new version under the same key), keyword ranking (no embeddings, no network, deterministic). It does not make a
model forget what it was trained on; it controls only what this store hands back.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from math import log
from pathlib import Path
from typing import Iterable, Sequence

from .clock import TimeLike, parse_time
from .errors import AsOfError
from .ledger import iso
from .query import pit_query
from .records import Record
from .sources.base import Source

_WORD = re.compile(r"[a-z0-9]+")
_DDL = """
CREATE TABLE IF NOT EXISTS memories (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    key TEXT,
    text TEXT NOT NULL,
    tags TEXT NOT NULL,
    source TEXT,
    available_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS memories_no_update BEFORE UPDATE ON memories BEGIN SELECT RAISE(ABORT, 'memories are append-only: write a new version under the same key'); END;
CREATE TRIGGER IF NOT EXISTS memories_no_delete BEFORE DELETE ON memories BEGIN SELECT RAISE(ABORT, 'memories are append-only'); END;
"""


class MemoryWriteError(AsOfError, ValueError):
    """A memory could not be written (no time, naive time, empty text)."""


@dataclass(frozen=True)
class Recalled:
    memory_id: str
    text: str
    learned_at: datetime
    score: float
    tags: tuple[str, ...]
    key: str | None = None


@dataclass(frozen=True)
class RecallResult:
    items: tuple[Recalled, ...]
    withheld_future: int          # memories learned AFTER T. The existence of later memories is itself information: keep this count away from the agent.


def _tokens(s: str) -> list[str]:
    return _WORD.findall(s.lower())


def rank(records: Iterable[Record], query: str, *, limit: int = 5, tags: Sequence[str] | None = None) -> list[Recalled]:
    """Rank memory records (already time-locked) against ``query`` by keyword overlap, weighting rarer words more. Memories sharing no word with the query are not returned. Ties go to the more recently learned."""
    recs = [r for r in records if r.available_at is not None]
    if tags:
        want = set(tags)
        recs = [r for r in recs if want & set(r.payload.get("tags") or ())]
    q = set(_tokens(query))
    if not q or not recs:
        return []
    docs = [_tokens(str(r.payload.get("text", ""))) for r in recs]
    n = len(docs)
    df = {w: sum(1 for d in docs if w in d) for w in q}
    out = []
    for r, d in zip(recs, docs, strict=True):
        score = sum((d.count(w) / (len(d) or 1)) * (1.0 + log((n + 1) / (df[w] + 0.5))) for w in q if df[w])
        if score > 0:
            assert r.available_at is not None
            out.append(Recalled(str(r.payload.get("memory_id")), str(r.payload.get("text", "")), r.available_at, round(score, 6), tuple(r.payload.get("tags") or ()), r.payload.get("key")))
    out.sort(key=lambda x: (-x.score, -x.learned_at.timestamp(), x.memory_id))
    return out[: max(0, limit)]


class Memory(Source):
    """The store. ``path=None`` keeps it in memory; a path keeps it in a SQLite file (append-only)."""

    def __init__(self, name: str = "memory", path: str | Path | None = None) -> None:
        self.name = name
        self._lock = threading.RLock()
        target = ":memory:" if path is None else str(Path(path).expanduser())
        if path is not None:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
        self._db.executescript(_DDL)

    def remember(self, text: str, *, learned_at: TimeLike, tags: Sequence[str] = (), key: str | None = None, source: str | None = None) -> str:
        """Write a memory that became known at ``learned_at`` (a time with a zone: a naive time is refused, never assumed to be UTC). Returns its id. A second memory under the same ``key`` is a new
        version: a session standing before ``learned_at`` still sees the old one."""
        if not isinstance(text, str) or not text.strip():
            raise MemoryWriteError("a memory needs some text")
        try:
            at = parse_time(learned_at, what="learned_at")
        except (ValueError, TypeError) as e:
            raise MemoryWriteError(f"a memory needs the time it was learned, with a time zone: {e}") from e
        if isinstance(tags, str):
            tags = (tags,)
        mid = uuid.uuid4().hex[:16]
        with self._lock:
            self._db.execute("INSERT INTO memories(id, key, text, tags, source, available_at, recorded_at) VALUES (?,?,?,?,?,?,?)",
                             (mid, key, text, "\x1f".join(str(t) for t in tags), source, iso(at), iso(datetime.now(timezone.utc))))
        return mid

    def __len__(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def records(self) -> list[Record]:
        """Every memory version, including ones learned in the future (the time lock is applied by the query)."""
        with self._lock:
            rows = self._db.execute("SELECT seq, id, key, text, tags, source, available_at FROM memories ORDER BY seq").fetchall()
        out = []
        for seq, mid, key, text, tags, src, at in rows:
            t = parse_time(at, what="available_at")
            out.append(Record(self.name, key or mid, t, t, {"memory_id": mid, "key": key, "text": text, "tags": tags.split("\x1f") if tags else [], "source": src}, order=seq))
        return out

    def recall(self, query: str, *, as_of: TimeLike, limit: int = 5, tags: Sequence[str] | None = None) -> RecallResult:
        """Memories relevant to ``query`` that were learned at or before ``as_of`` (inclusive). Those learned later are not returned, only counted."""
        res = pit_query(self.records(), parse_time(as_of, what="as_of"))
        return RecallResult(tuple(rank(res.records, query, limit=limit, tags=tags)), int(res.counts.get("blocked_future", 0)))

    def close(self) -> None:
        with self._lock:
            self._db.close()
