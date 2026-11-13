"""Records: the unit every source returns.

A record carries two times and they mean different things:

* ``event_time``: when the thing happened (a trade, a quarter ending, a price print).
* ``available_at``: when it became *knowable* to someone outside the system. Time-locking uses only this one.

Both are declared by the data's owner. AsOf cannot verify them; it enforces them.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Mapping

from . import jcs
from .clock import UTC


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        if o.tzinfo is None:
            return {"__naive_datetime__": o.isoformat()}          # kept distinguishable; never silently treated as UTC
        return o.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=repr)
    if isinstance(o, bytes):
        return {"__bytes_sha256__": hashlib.sha256(o).hexdigest()}
    if hasattr(o, "item") and callable(o.item):                   # numpy / pandas scalars without importing them
        return o.item()
    raise TypeError(f"cannot serialise {type(o).__name__} to canonical JSON")


def _normalise(o: Any) -> Any:
    """Turn what a payload may hold (datetimes, dates, sets, bytes, numpy scalars, tuples, int keys) into plain JSON values, with the same rules as before."""
    if o is None or isinstance(o, (bool, int, float, str)):
        return o
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if not isinstance(k, str):
                if isinstance(k, bool) or not isinstance(k, int):
                    raise TypeError(f"cannot use a {type(k).__name__} as a JSON object key")
                k = str(k)
            out[k] = _normalise(v)
        return out
    if isinstance(o, (list, tuple)):
        return [_normalise(v) for v in o]
    return _normalise(_default(o))


def canonical_json(obj: Any) -> str:
    """The canonical form of ledger schema 3 (SPEC 1.1): RFC 8785 (JCS), after datetimes become UTC ISO-8601, sets sorted arrays and bytes ``{"__bytes_sha256__": ...}``.
    Same value, same bytes, in any language that implements RFC 8785 (see ``asof.jcs``)."""
    return jcs.canonicalize(_normalise(obj))


def legacy_canonical_json(obj: Any) -> str:
    """The v1.0 form (ledger schema 2): ``json.dumps`` with sorted keys and no whitespace. Floats are written as Python writes them, so another language cannot reproduce it
    in general. Kept to read and check schema-2 material; nothing writes it any more."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default, allow_nan=False)


def sha256_hex(text: str | bytes) -> str:
    return hashlib.sha256(text.encode("utf-8") if isinstance(text, str) else text).hexdigest()


def payload_sha256(payload: Any) -> str:
    return sha256_hex(canonical_json(payload))


@dataclass(frozen=True)
class Record:
    """One version of one record. Several versions may share ``record_id`` (restatements); they differ in ``available_at``."""

    source_id: str
    record_id: str
    event_time: datetime | None
    available_at: datetime | None
    payload: Mapping[str, Any]
    payload_sha256: str = ""
    flags: tuple[str, ...] = field(default=())
    order: int = 0                       # position in the source. NOT used to choose between versions (see query._tie_key); kept for listing rows in a stable order
    version: int | None = None           # the source's own version number of this record, when it has one: the higher one wins an exact tie on available_at
    recorded_at: datetime | None = None  # when the source recorded this version, when it says so: the later one wins a tie that ``version`` does not settle

    def __post_init__(self) -> None:
        if not self.payload_sha256:
            object.__setattr__(self, "payload_sha256", payload_sha256(self.payload))
        for name in ("event_time", "available_at", "recorded_at"):
            v = getattr(self, name)
            if v is not None and (v.tzinfo is None or v.utcoffset() is None):
                raise ValueError(f"Record.{name} must be timezone-aware or None")

    def with_flags(self, *flags: str) -> "Record":
        return replace(self, flags=tuple(dict.fromkeys((*self.flags, *flags))))

    def verify(self) -> bool:
        """Does ``payload_sha256`` still match the payload?"""
        return self.payload_sha256 == payload_sha256(self.payload)

    def to_dict(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "record_id": self.record_id,
                "event_time": _iso(self.event_time), "available_at": _iso(self.available_at),
                "payload": dict(self.payload), "payload_sha256": self.payload_sha256, "flags": list(self.flags)}


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
