"""The point-in-time query: rules R1, R2 and R3.

Order of operations (it matters):

1. Split every record version by time. ``available_at > T`` is blocked (R1); ``available_at is None`` is withheld unless the session allows
   untimestamped data (R2).
2. For each ``record_id``, keep only the **latest version visible at T** (R3). A later correction is never served, even if the earlier version
   is the one that matches your filter.
3. Only then apply the filters, to the version chosen in step 2. (Filtering first could serve a superseded version that happens to match.)
4. Apply the lookback window on ``event_time`` and the limit.

Boundary (R1): a record with ``available_at == T`` **is** served. One microsecond later is not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from .clock import UTC
from .records import Record

_MIN = datetime.min.replace(tzinfo=UTC)
MAX_LISTED = 1000          # how many blocked records are listed individually; the count is always exact


def matches(payload: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    """Equality on payload fields; a list, tuple or set means 'any of'. A field the payload lacks never matches."""
    for k, want in filters.items():
        if k not in payload:
            return False
        have = payload[k]
        if isinstance(want, (list, tuple, set, frozenset)):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


def _tie_key(r: Record) -> tuple[Any, ...]:
    """Which of several versions with the SAME ``available_at`` wins: the highest explicit ``version``; without one, the latest ``recorded_at``; without either, the larger payload hash.
    The last step is arbitrary but depends only on the content, so shuffling the source's rows cannot change what is served. Two versions can share a payload hash and still differ in
    their event time or flags (a hand-built source can do that), so those are part of the key too: versions that are equal in ALL of it are the same answer whichever is taken."""
    return (r.version is not None, r.version if r.version is not None else 0, r.recorded_at is not None, r.recorded_at or _MIN, r.payload_sha256,
            r.event_time is not None, r.event_time or _MIN, r.flags)


@dataclass(frozen=True)
class QueryResult:
    records: tuple[Record, ...]
    as_of: datetime
    counts: Mapping[str, int]
    warnings: tuple[str, ...] = ()
    served_unknown_ids: tuple[str, ...] = field(default=())
    blocked_future: tuple[tuple[str, str], ...] = field(default=())          # (record_id, payload_sha256), capped at MAX_LISTED
    blocked_untimestamped: tuple[tuple[str, str], ...] = field(default=())


def pit_query(records: Iterable[Record], as_of: datetime, *, filters: Mapping[str, Any] | None = None, lookback: timedelta | None = None,
              limit: int | None = None, allow_untimestamped: bool = False, max_listed: int = MAX_LISTED) -> QueryResult:
    """Apply R1-R3 to ``records`` at ``as_of`` (timezone-aware). ``records`` is every version the source holds, future ones included.
    ``max_listed`` is how many blocked records are named individually in the result (the counts are always exact)."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if max_listed < 0:
        raise ValueError("max_listed must not be negative")
    MAX_LISTED_ = max_listed
    filters = dict(filters or {})
    visible: dict[str, list[Record]] = {}
    untimed: dict[str, list[Record]] = {}
    blocked_future = blocked_untimestamped = 0
    bf_ids: list[tuple[str, str]] = []
    bu_ids: list[tuple[str, str]] = []
    for r in records:
        hit = matches(r.payload, filters)                 # for the receipt's counts only: what the block was worth to this query
        if r.available_at is None:
            untimed.setdefault(r.record_id, []).append(r)
            if hit and not allow_untimestamped:
                blocked_untimestamped += 1
                if len(bu_ids) < MAX_LISTED_:
                    bu_ids.append((r.record_id, r.payload_sha256))
        elif r.available_at > as_of:                      # R1: inclusive boundary, so equal is served
            if hit:
                blocked_future += 1
                if len(bf_ids) < MAX_LISTED_:
                    bf_ids.append((r.record_id, r.payload_sha256))
        else:
            visible.setdefault(r.record_id, []).append(r)

    chosen: list[tuple[Record, bool]] = []                # (record, is_unknown_timing)
    superseded = 0
    ambiguous: list[str] = []
    for rid, versions in visible.items():
        top = max(v.available_at for v in versions)       # type: ignore[type-var]
        latest = [v for v in versions if v.available_at == top]
        if len(latest) > 1:
            ambiguous.append(rid)
        pick = max(latest, key=_tie_key)              # exact tie: the explicit version, then recorded_at, then the payload hash. Never the row's position in the source
        superseded += len(versions) - 1                   # every visible version that was not served, the tied ones included
        chosen.append((pick, False))
    if allow_untimestamped:                               # R2: only when the session opted in; a timestamped version always wins
        for rid, versions in untimed.items():
            if rid in visible:
                continue
            if len(versions) > 1:
                ambiguous.append(rid)
            chosen.append((max(versions, key=_tie_key), True))

    matched = [(r, unk) for r, unk in chosen if matches(r.payload, filters)]
    outside = 0
    if lookback is not None:
        try:
            lo = as_of - lookback
        except OverflowError:                             # the window would start before the first date there is (a clock in year 1, or an enormous lookback): it starts at the beginning
            lo = _MIN
        kept = []
        for r, unk in matched:
            if r.event_time is None or not (lo <= r.event_time <= as_of):
                outside += 1
            else:
                kept.append((r, unk))
        matched = kept
    matched.sort(key=lambda p: (p[0].event_time is None, p[0].event_time or _MIN, p[0].available_at or _MIN, p[0].record_id))
    if limit is not None:
        if limit < 0:
            raise ValueError("limit must not be negative")
        matched = matched[-limit:] if limit else []       # the most recent `limit` rows, still in time order

    served = [r for r, _ in matched]
    unknown = tuple(r.record_id for r, unk in matched if unk)
    warnings: list[str] = []
    n_partial = sum("partial_bar" in r.flags for r in served)
    if n_partial:
        warnings.append(f"{n_partial} served record(s) are partial bars (the session allowed them): their values were not final at the time they were stamped")
    n_before = sum("available_before_event" in r.flags for r in served)
    if n_before:
        warnings.append(f"{n_before} served record(s) are flagged available_before_event: their declared availability is earlier than the event itself, so the source's timestamps are suspect")
    if ambiguous:
        warnings.append(f"{len(ambiguous)} record id(s) had several versions with identical availability; the highest explicit version (or the latest recorded_at) was served, "
                        "and where neither is given the choice is made by payload hash, never by row order")
    counts = {"served": len(served), "served_unknown_timing": len(unknown), "blocked_future": int(blocked_future),
              "blocked_untimestamped": int(blocked_untimestamped), "superseded": superseded, "outside_window": outside, "ambiguous_versions": len(ambiguous)}
    return QueryResult(records=tuple(served), as_of=as_of, counts=counts, warnings=tuple(warnings), served_unknown_ids=unknown,
                       blocked_future=tuple(bf_ids), blocked_untimestamped=tuple(bu_ids))
