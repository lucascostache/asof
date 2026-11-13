"""The ledger: an append-only, hash-chained event log in a local SQLite file (schema version 2).

Three clocks, with exact meanings (the format is specified in SPEC.md):

``event_time``    when the underlying thing happened. For a served record: the record's own ``event_time``. For an agent action (query, tool
                  call, model call, decision, block, clock change): the **session clock** T at that moment. ``NULL`` when unknown.
``available_at``  when it became knowable. For a served record: the record's own ``available_at``. For an agent action: T (the agent acts at T).
                  Never wall-clock time. ``NULL`` when unknown.
``recorded_at``   the wall-clock UTC time the ledger wrote the event. **Set only by the ledger**: callers cannot pass it.

Tamper evidence: each event stores the hash of the one before it, so changing, removing or reordering any earlier event breaks every hash after it;
:meth:`Ledger.verify` finds the first break. SQLite triggers refuse UPDATE and DELETE through normal use.

Tail anchor: after every write the ledger exports ``{seq, hash, recorded_at}`` to a ``head.json`` beside it. A chain cut off at the end is still
self-consistent, so :meth:`Ledger.verify` compares the tail with that file and **fails** if the ledger is shorter than the anchor, if the anchored event
has a different hash, or if the anchor file is missing from a non-empty ledger. **The anchor is only as trustworthy as where it is stored**: a
file next to the ledger can be rewritten together with it. Copy ``strathora head`` output somewhere you trust (git, email, another system) and pass it to
``verify(expected_head=...)``.

What this does not give you: it is tamper-*evident*, not tamper-*proof* (anyone who can write both files can forge a consistent history), it is local and
unsigned (it shows what was asked and served, not who asked), and it proves nothing about the truth of the timestamps recorded in it.

Versions: this is schema version 3 (SPEC 1.1). A schema 2 ledger (AsOf 0.2.0) is still read, verified and synced under its own hash domain, but is never extended: versions are not mixed in one chain.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .clock import UTC
from .errors import LedgerError, LedgerVersionError
from . import jcs
from .records import canonical_json, sha256_hex
from .redaction import REDACTED, redact, scrub                # noqa: F401  (re-exported: `from asof.ledger import redact` is the long-standing spelling)

GENESIS = "0" * 64
_WAL_PATIENCE = 10.0                                     # seconds a write waits for the switch into WAL before going ahead in the current journal mode (see Ledger._enable_wal)
SCHEMA_VERSION = 3                                      # what this release WRITES (SPEC 1.1: RFC 8785 canonical JSON)
SUPPORTED_SCHEMAS = (2, 3)                               # what it can open, verify and sync; 2 is SPEC 1.0 (Python-formatted canonical JSON), read-only here
DOMAINS = {2: "asof-ledger/2", 3: "asof-ledger/3"}
HASH_DOMAIN = DOMAINS[SCHEMA_VERSION]
HEAD_FILE_VERSION = 1

def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class LedgerEvent:
    seq: int
    id: str
    session_id: str
    type: str
    call_id: str | None
    event_time: str | None
    available_at: str | None
    recorded_at: str
    payload: str
    payload_sha256: str
    prev_hash: str
    hash: str

    def data(self) -> Any:
        return json.loads(self.payload)


@dataclass(frozen=True)
class EventSpec:
    """What a caller supplies for one event. Note there is no ``recorded_at``: only the ledger sets that."""
    session_id: str
    type: str
    event_time: datetime | None
    available_at: datetime | None
    payload: dict[str, Any]
    call_id: str | None = None


@dataclass(frozen=True)
class Head:
    seq: int
    hash: str
    recorded_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"version": HEAD_FILE_VERSION, "seq": self.seq, "hash": self.hash, "recorded_at": self.recorded_at}


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    n_events: int
    head_hash: str
    first_bad_seq: int | None = None
    reason: str | None = None
    anchor: str = "skipped"                    # "ok" | "behind" | "skipped" | "failed"
    warnings: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.ok


def _event_hash(seq: int, prev: str, id_: str, session_id: str, type_: str, call_id: str | None, event_time: str | None, available_at: str | None,
                recorded_at: str, payload_sha: str, domain: str = HASH_DOMAIN) -> str:
    """The hash binds the position (``seq``), the link (``prev``) and every other field, including all three clocks. The list holds only strings, integers and null, so
    its canonical form is the same under SPEC 1.0 and 1.1; the ``domain`` (``asof-ledger/2`` or ``/3``) is what tells the versions apart."""
    return sha256_hex(jcs.canonicalize([domain, seq, prev, id_, session_id, type_, call_id, event_time, available_at, recorded_at, payload_sha]))


_COLS = "seq,id,session_id,type,call_id,event_time,available_at,recorded_at,payload,payload_sha256,prev_hash,hash"

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    type TEXT NOT NULL,
    call_id TEXT,
    event_time TEXT,
    available_at TEXT,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS events_call ON events(call_id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'the ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'the ledger is append-only'); END;
"""


def default_head_path(ledger_path: Path) -> Path:
    """``<dir>/head.json`` for a ledger called ``ledger.db`` (so ``~/.asof/head.json`` by default), else ``<dir>/<stem>.head.json``."""
    return ledger_path.with_name("head.json" if ledger_path.name == "ledger.db" else f"{ledger_path.stem}.head.json")


def _read_head_file(path: Path) -> Head | None:
    try:
        d = json.loads(path.read_text())
        return Head(int(d["seq"]), str(d["hash"]), d.get("recorded_at"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def parse_head(value: Any) -> Head:
    """Accept a ``Head``, a ``(seq, hash)`` pair, a ``{"seq", "hash"}`` dict, a JSON string, or a path to a head.json."""
    if isinstance(value, Head):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return Head(int(value[0]), str(value[1]), None)
    if isinstance(value, dict):
        return Head(int(value["seq"]), str(value["hash"]), value.get("recorded_at"))
    if isinstance(value, (str, Path)):
        text = str(value)
        if text.lstrip().startswith("{"):
            return parse_head(json.loads(text))
        return parse_head(json.loads(Path(text).expanduser().read_text()))
    raise ValueError(f"cannot read a ledger head from {type(value).__name__}")


def open_for_reading(path: str | Path, **kw: Any) -> "Ledger":
    """Open an existing ledger to read or verify it. When the file or its folder cannot be written (read-only media, an archived copy, another user's file) it is opened
    read-only, so evidence can always be checked where it lies; otherwise it is opened as usual."""
    p = Path(path).expanduser()
    writable = os.access(p, os.W_OK) and os.access(p.parent, os.W_OK)
    return Ledger(p, read_only=not writable, **kw)


class Ledger:
    """Append-only event log. Safe to share between threads and between processes appending to the same file."""

    def __init__(self, path: str | Path = "~/.asof/ledger.db", *, head_path: str | Path | None = None, now: Callable[[], datetime] | None = None,
                 read_only: bool = False) -> None:
        """``read_only=True`` opens an existing ledger for reading and verifying only: nothing is created or written (not the file, not its folder, not the anchor), so it works on a
        ledger kept on read-only media or with read-only permissions. Appending to it raises :class:`LedgerError`."""
        self.path = Path(path).expanduser()
        self.head_path = Path(head_path).expanduser() if head_path else default_head_path(self.path)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self.read_only = bool(read_only)
        try:
            if self.read_only:
                if not self.path.is_file():
                    raise OSError("no such file")
                new = False
                self._db = sqlite3.connect("file:" + urllib.parse.quote(str(self.path.resolve())) + "?mode=ro", uri=True, check_same_thread=False, isolation_level=None, timeout=30)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                new = not self.path.exists()
                self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
            if new:
                os.chmod(self.path, 0o600)                        # the ledger can hold prompts and tool arguments: keep it private
            found = self._check_version()                         # first: a ledger this version refuses must be left byte-for-byte untouched, and WAL mode is written into the file header
            self._wal = False                                      # WAL is switched on by the first WRITE, never by opening: verifying or reading a ledger must not change the file
            if not self.read_only:
                self._db.execute("PRAGMA secure_delete=ON")       # freed pages are overwritten with zeros, so nothing removed from the file lingers in free space (a per-connection setting: it writes nothing)
                self._db.executescript(_DDL)
                self._db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif not self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone():
                raise OSError("the file is not an AsOf ledger (it has no events table)")
            row = self._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            self.schema_version = int(row[0]) if row else (found or SCHEMA_VERSION)
            self.hash_domain = DOMAINS[self.schema_version]
        except (OSError, sqlite3.Error) as e:
            raise LedgerError(f"cannot open ledger at {self.path}: {e}") from e
        self._swept = False
        if new and not self.head_path.exists():
            self._write_head(self.head())                          # anchor a NEW ledger at once (empty), so deleting everything later is visible. An existing ledger whose anchor is
                                                                   # missing is left without one: recreating it here would make `verify` say "anchor ok" about an anchor someone removed

    def _check_version(self) -> int | None:
        has_events = self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone()
        has_meta = self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        found = None
        if has_meta:
            row = self._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            found = int(row[0]) if row else None
        if has_events and found not in SUPPORTED_SCHEMAS:
            label = "1" if found in (None, 1) else str(found)
            raise LedgerVersionError(
                f"{self.path} is a schema-v{label} ledger; this version of AsOf writes schema v{SCHEMA_VERSION}, reads {SUPPORTED_SCHEMAS} and will not mix versions in one chain. "
                f"Keep the old file (it stays verifiable with the AsOf release that wrote it) and start a new one: `strathora init --archive-legacy`.")
        return found

    @property
    def chain_id(self) -> str:
        """The identity of this ledger as a chain (SPEC 1.1 section 7.2): a random 128-bit hex string made when the file is created (or, for an older file, the first time it is asked for)
        and kept in ``meta``. It is not part of any hash; it tells a receiver which chain a batch extends."""
        with self._lock:
            if self.read_only:
                row = self._db.execute("SELECT value FROM meta WHERE key='chain_id'").fetchone()
                if row is None:
                    raise LedgerError(f"{self.path} has no chain id yet and is open read-only, so none can be made")
                return str(row[0])
            self._db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('chain_id', ?)", (uuid.uuid4().hex,))
            return str(self._db.execute("SELECT value FROM meta WHERE key='chain_id'").fetchone()[0])

    def close(self) -> None:
        """Close the ledger. While it was open, committed events may have sat in the ``-wal`` file; closing folds them into the ledger file and returns it to a single-file mode, so a closed ledger
        is one self-contained file that a plain copy preserves. (Copying a ledger that is still open is not safe: use :meth:`backup`.) If another process still has it open, this is skipped."""
        with self._lock:
            if getattr(self, "_wal", False):                           # a ledger that was only read is left exactly as it was found
                try:
                    self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    self._db.execute("PRAGMA journal_mode=DELETE")
                except sqlite3.Error:
                    pass                                              # busy (another process is writing): its own close will do it
            self._db.close()

    def _enable_wal(self) -> None:
        """Write-ahead logging, from the first write on: readers do not block the writer. One writer at a time is still enforced (``BEGIN IMMEDIATE``), so the chain cannot fork.

        Switching a database into WAL takes an exclusive lock that the busy timeout does not wait for, so several processes making their first write at the same moment can collide ("database is locked").
        That is retried with a short backoff; if it still cannot be done within ``_WAL_PATIENCE`` seconds the write simply goes ahead in the current journal mode and the switch is tried again on the next write.
        WAL is an optimisation: nothing about the chain's correctness depends on it."""
        if self._wal:
            return
        deadline = time.monotonic() + _WAL_PATIENCE
        delay = 0.005
        while True:
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
                self._wal = True
                return
            except sqlite3.OperationalError:
                if time.monotonic() >= deadline:
                    return
                time.sleep(delay)
                delay = min(delay * 2, 0.1)

    def backup(self, dest: str | Path) -> Path:
        """A consistent single-file copy of the ledger as it is now, safe to make while it is open and being written (SQLite's online backup, not a file copy). Does not copy ``head.json``;
        copy that too (or use ``strathora head``) if the copy is to be verified against its tail anchor."""
        out = Path(dest).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._lock:
            target = sqlite3.connect(str(out))
            try:
                self._db.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")          # a copy is one file, not a database with a sidecar
            finally:
                target.close()
        os.chmod(out, 0o600)
        return out

    @classmethod
    def for_session(cls, directory: str | Path, session_id: str, **kw: Any) -> "Ledger":
        """One chain per session: ``<directory>/<session_id>.ledger.db``, with its own ``chain_id`` and its own tail anchor (``<session_id>.ledger.head.json``). Two sessions never share a chain,
        so one session's ledger can be synced, verified, handed over or purged without touching another's."""
        sid = str(session_id)
        if not sid or any(ch in sid for ch in "/\\\0") or sid in (".", ".."):
            raise LedgerError(f"{session_id!r} cannot be used as a file name; use letters, digits, '-' and '_'")
        return cls(Path(directory).expanduser() / f"{sid}.ledger.db", **kw)

    # ---------------------------------------------------------------- writing
    def append(self, session_id: str, type: str, *, event_time: datetime | None, available_at: datetime | None, payload: dict[str, Any],
               call_id: str | None = None) -> LedgerEvent:
        """Record one event. ``recorded_at`` is taken here, from the wall clock; it cannot be passed in."""
        return self.append_many([EventSpec(session_id, type, event_time, available_at, payload, call_id)])[0]

    def append_many(self, specs: Sequence[EventSpec]) -> list[LedgerEvent]:
        """Record several events in one transaction, in order, as one atomic write (all of them or none). All share one ``recorded_at``."""
        if not specs:
            return []
        if self.read_only:
            raise LedgerError(f"{self.path} is open read-only: nothing can be appended")
        if self.schema_version != SCHEMA_VERSION:
            raise LedgerVersionError(
                f"{self.path} is a schema-v{self.schema_version} ledger (SPEC 1.0). This version of AsOf can verify and sync it but writes only schema v{SCHEMA_VERSION} "
                f"and will not mix versions in one chain: start a new ledger (`strathora init --archive-legacy`) and keep this file for verification.")
        wall = iso(self._now())
        assert wall is not None
        prepared = []
        for sp in specs:
            body = canonical_json(redact(sp.payload))
            prepared.append((sp, body, sha256_hex(body), iso(sp.event_time), iso(sp.available_at)))
        out: list[LedgerEvent] = []
        with self._lock:
            try:
                self._enable_wal()
                self._db.execute("BEGIN IMMEDIATE")              # one writer at a time across processes, so the chain cannot fork
                row = self._db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
                prev, seq = (row[1], row[0]) if row else (GENESIS, 0)
                for sp, body, psha, ev_t, av_t in prepared:
                    id_ = uuid.uuid4().hex
                    seq += 1                                            # assigned here, inside the write lock, so it can be hashed
                    h = _event_hash(seq, prev, id_, sp.session_id, sp.type, sp.call_id, ev_t, av_t, wall, psha)
                    self._db.execute(
                        "INSERT INTO events(seq, id, session_id, type, call_id, event_time, available_at, recorded_at, payload, payload_sha256, prev_hash, hash) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (seq, id_, sp.session_id, sp.type, sp.call_id, ev_t, av_t, wall, body, psha, prev, h))
                    out.append(LedgerEvent(seq, id_, sp.session_id, sp.type, sp.call_id, ev_t, av_t, wall, body, psha, prev, h))
                    prev = h
                self._db.execute("COMMIT")
            except sqlite3.Error as e:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise LedgerError(f"could not append to the ledger: {e}") from e
            self._write_head(Head(out[-1].seq, out[-1].hash, out[-1].recorded_at))
        return out

    def _write_head(self, head: Head) -> None:
        """Export the tail to head.json (atomically). It never moves backwards, so a stale writer cannot hide a newer anchor."""
        if self.read_only:
            raise LedgerError(f"{self.path} is open read-only: its anchor cannot be written")
        try:
            existing = _read_head_file(self.head_path)
            if existing is not None and existing.seq > head.seq:
                return
            tmp = self.head_path.with_name(self.head_path.name + f".{os.getpid()}.{uuid.uuid4().hex[:10]}.tmp")      # unique per write: threads and processes cannot collide
            tmp.write_text(json.dumps(head.to_dict(), sort_keys=True, indent=2) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.head_path)
        except OSError as e:
            raise LedgerError(f"could not write the head anchor {self.head_path}: {e}") from e
        if not self._swept:
            self._swept = True
            self._sweep_stale_tmp()

    def write_anchor(self) -> Head:
        """Export the ledger's current tail to head.json now, and return it. Writing an event does this by itself; this is for the one case where it is wanted without a write:
        an existing ledger whose anchor file was lost (``strathora init`` calls it). It states the ledger as it is NOW, so it cannot vouch for anything removed before."""
        h = self.head()
        self._write_head(h)
        return h

    def _sweep_stale_tmp(self) -> None:
        """Remove ``head.json.<pid>.<random>.tmp`` files left by a writer that was killed between writing its temporary file and renaming it. Only files of processes that no longer
        exist (or, where that cannot be asked, files more than five minutes old) are removed, so a writer that is alive and mid-write is never disturbed. Once per Ledger object."""
        prefix = self.head_path.name + "."
        try:
            names = [n for n in os.listdir(self.head_path.parent) if n.startswith(prefix) and n.endswith(".tmp")]
        except OSError:
            return
        for name in names:
            first = name[len(prefix):].split(".")[0]
            pid = int(first) if first.isdigit() else None
            if pid == os.getpid():
                continue
            f = self.head_path.parent / name
            try:
                stale = time.time() - f.stat().st_mtime > 300
                if not stale and pid is not None and os.name == "posix":
                    try:
                        os.kill(pid, 0)                              # signal 0: only asks whether the process exists (POSIX)
                    except ProcessLookupError:
                        stale = True
                    except OSError:
                        pass                                         # it exists but is not ours to signal: leave its file alone
                if stale:
                    f.unlink()
            except OSError:
                pass

    # ---------------------------------------------------------------- reading
    def events(self, session_id: str | None = None, type: str | None = None, call_id: str | None = None) -> list[LedgerEvent]:
        q, args, cond = f"SELECT {_COLS} FROM events", [], []
        for col, val in (("session_id", session_id), ("type", type), ("call_id", call_id)):
            if val is not None:
                cond.append(f"{col} = ?")
                args.append(val)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        with self._lock:
            return [LedgerEvent(*r) for r in self._db.execute(q + " ORDER BY seq", args).fetchall()]

    def head(self) -> Head:
        """The newest event in the database (``seq`` 0 and the genesis hash if empty)."""
        with self._lock:
            row = self._db.execute("SELECT seq, hash, recorded_at FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return Head(row[0], row[1], row[2]) if row else Head(0, GENESIS, None)

    def anchor(self) -> Head | None:
        """What head.json says the tail should be, or ``None`` if the file is missing or unreadable."""
        return _read_head_file(self.head_path)

    def __len__(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    # ---------------------------------------------------------------- verifying
    def verify(self, expected_head: Any = None, *, anchor: str = "require") -> VerifyResult:
        """Recompute the whole chain from the first event, then check the tail anchor.

        Chain: sequence numbers have no gaps, every payload matches its hash, every event's hash matches *all* of its fields (including all three clocks)
        and its predecessor. Anchor (``anchor="require"``, the default): head.json must exist (unless the ledger has never been written to), the ledger must
        not be shorter than it, and the anchored event must have the recorded hash. ``anchor="skip"`` checks the chain only, and says so in the result.
        ``expected_head`` is a head you stored elsewhere (a ``Head``, ``(seq, hash)``, a dict, JSON text or a path); it is checked the same way and wins
        over head.json.
        """
        if anchor not in ("require", "skip"):
            raise ValueError("anchor must be 'require' or 'skip'")
        prev, n, expected_seq = GENESIS, 0, 1
        supplied = parse_head(expected_head) if expected_head is not None else None
        a = supplied if supplied is not None else (None if anchor == "skip" else self.anchor())
        seen: dict[int, str] = {}                                                            # only the anchored event's hash is kept: the events are read one at a time, not all at once
        with self._lock:
            try:
                meta = self._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                domain = DOMAINS.get(int(meta[0]) if meta else 0, HASH_DOMAIN)               # the version field decides how an existing ledger is verified
                cur = self._db.execute(f"SELECT {_COLS} FROM events ORDER BY seq")
                while True:
                    try:
                        r = cur.fetchone()
                        if r is None:
                            break
                        ev = LedgerEvent(*r)
                        bad = None
                        if ev.seq != expected_seq:
                            bad = f"sequence gap: expected event {expected_seq}, found {ev.seq} (an event was removed or reordered)"
                        elif not all(isinstance(x, str) for x in (ev.id, ev.session_id, ev.type, ev.recorded_at, ev.payload, ev.payload_sha256, ev.prev_hash, ev.hash)):
                            bad = "a field does not hold text (the file was altered or is damaged)"
                        elif sha256_hex(ev.payload) != ev.payload_sha256:
                            bad = "payload does not match its recorded hash (the payload was altered)"
                        elif ev.prev_hash != prev:
                            bad = "prev_hash does not match the previous event (the chain was cut or reordered)"
                        elif _event_hash(ev.seq, ev.prev_hash, ev.id, ev.session_id, ev.type, ev.call_id, ev.event_time, ev.available_at, ev.recorded_at, ev.payload_sha256, domain) != ev.hash:
                            bad = "event hash does not match its contents (a field was altered)"
                    except (sqlite3.Error, UnicodeError, ValueError, TypeError) as e:      # a row that cannot even be read: the file itself is damaged at (or just after) this event
                        return VerifyResult(False, n, prev, expected_seq, f"event {expected_seq} cannot be read: the ledger file is damaged ({type(e).__name__}: {e})", "failed")
                    n += 1
                    if bad:
                        return VerifyResult(False, n, prev, ev.seq, bad, "failed")
                    prev, expected_seq = ev.hash, ev.seq + 1
                    if a is not None and ev.seq == a.seq:
                        seen[ev.seq] = ev.hash
            except (sqlite3.Error, UnicodeError, ValueError) as e:
                return VerifyResult(False, n, prev, None, f"the ledger file is damaged and cannot be read as a ledger ({type(e).__name__}: {e})", "failed")
        tail = Head(n, prev, None)
        if supplied is not None:
            return self._check_anchor(supplied, seen, n, prev, "the head you supplied")
        if anchor == "skip":
            return VerifyResult(True, n, prev, anchor="skipped", warnings=("the tail anchor was not checked: a chain cut off at the end would still verify",))
        if a is None:
            if n == 0 and not self.head_path.exists():
                return VerifyResult(True, n, prev, anchor="ok")
            why = "is missing" if not self.head_path.exists() else "cannot be read"
            return VerifyResult(False, n, tail.hash, None, f"the tail anchor {self.head_path} {why}: truncation of the end of the ledger cannot be ruled out. "
                                f"Restore it from where you stored it, or verify against a head you saved (expected_head=..., `strathora verify --head`), "
                                f"or skip the anchor explicitly (anchor='skip', `strathora verify --no-anchor`).", "failed")
        return self._check_anchor(a, seen, n, prev, f"the anchor {self.head_path.name}")

    def _check_anchor(self, a: Head, seen: dict[int, str], n: int, prev: str, source: str) -> VerifyResult:
        if a.seq == 0:
            ok_note = ()
            return VerifyResult(True, n, prev, anchor="ok" if n == 0 else "behind",
                                warnings=ok_note if n == 0 else (f"{source} records an empty ledger but the ledger has {n} events",))
        if a.seq > n:
            return VerifyResult(False, n, prev, a.seq, f"the ledger was truncated: {source} records event {a.seq} but the ledger ends at event {n}", "failed")
        if seen.get(a.seq) != a.hash:
            return VerifyResult(False, n, prev, a.seq, f"event {a.seq} exists but its hash differs from {source}: the chain was rewritten", "failed")
        if a.seq < n:
            return VerifyResult(True, n, prev, anchor="behind",
                                warnings=(f"{source} is {n - a.seq} event(s) behind the ledger (normal after a crash between a write and its export, or when another "
                                          f"writer is active); events after it are covered by the chain but not by the anchor",))
        return VerifyResult(True, n, prev, anchor="ok")

    def __iter__(self) -> Iterator[LedgerEvent]:
        return iter(self.events())


def archive_legacy(path: str | Path) -> Path:
    """Move an old-version ledger (and its head file) aside so a fresh v2 ledger can start. The old file is kept intact, never deleted or converted."""
    p = Path(path).expanduser()
    if not p.exists():
        raise LedgerError(f"{p} does not exist")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = p.with_name(f"{p.stem}.legacy-{stamp}{p.suffix}")
    os.replace(p, dest)
    head = default_head_path(p)
    if head.exists():
        os.replace(head, dest.with_name(dest.stem + ".head.json"))
    return dest
