"""Data-quality summaries: what can be said, from the records alone, about how trustworthy a source's timestamps and rows look.

Two summaries, both plain JSON made of integers, strings and ``None`` (no floats, so they hash the same in every language):

* :func:`analyse_source` runs once when a source is loaded and is written into the ``source_loaded`` event as ``quality``. It looks at the **whole** source, including records that
  become available after any session's clock, so it is an **operator** view: like the blocked counts, it must not be shown to the agent.
* :func:`query_quality` runs for every query and is written into the ``query`` event as ``quality``. It uses only the record versions that were **visible at T**
  (``available_at <= T``) and the records served, so it adds no information about the future.

What this is **not**: it cannot verify a timestamp (see ``docs/pilot/LIMITATIONS.md``). It looks for patterns that real data does not have when its clocks are honest, and says
so, with the exact thresholds used, so nobody has to trust an unexplained score. A heuristic that cannot run says ``None`` (unknown), never ``0``.

Thresholds (also written into each summary as ``thresholds``):

* a **gap** is a step between consecutive distinct ``event_time`` values larger than ``GAP_FACTOR`` times the median step. For a source with a market ``calendar`` (see
  :mod:`asof.calendars`), nights, weekends and the exchange's holidays are left out: a step within one local date is compared with the median such step, and a step across dates
  counts the trading sessions it skips (a gap when that is more than ``GAP_FACTOR`` times the median number skipped);
* a **burst** is one hour of ``available_at`` holding at least ``BURST_MIN_RECORDS`` records whose ``event_time`` spans at least ``BURST_MIN_SPAN_DAYS`` days: a long history that
  became "available" all at once, the signature of a backfill stamped with the load time;
* served data is **stale** when the newest served ``event_time`` is more than ``STALE_FACTOR`` median steps older than T.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from statistics import median_low
from typing import Any, Iterable, Mapping, Sequence

from . import calendars
from .records import Record

QUALITY_VERSION = 1
GAP_FACTOR = 5
STALE_FACTOR = 5
BURST_MIN_RECORDS = 20
BURST_MIN_SPAN_DAYS = 30
LATE_LAG_FACTOR = 10                # a record is "late" when it became available more than this many times the median lag after its event (and more than LATE_LAG_MIN_SECONDS after it)
LATE_LAG_MIN_SECONDS = 86400
EXAMPLES = 5                        # how many affected record ids (as hashes) an issue lists
THRESHOLDS = {"gap_factor": GAP_FACTOR, "stale_factor": STALE_FACTOR, "burst_min_records": BURST_MIN_RECORDS, "burst_min_span_days": BURST_MIN_SPAN_DAYS,
              "late_lag_factor": LATE_LAG_FACTOR, "late_lag_min_seconds": LATE_LAG_MIN_SECONDS}
# How serious each flag is. error: the timestamps cannot be honest. warning: a pattern honest data does not usually show; worth a look before relying on the source. info: something the source did that you should know.
SEVERITY = {"unusable_timestamps": "error", "available_before_event": "error", "future_dated": "error", "missing_available_at": "warning", "missing_event_time": "warning", "late_availability": "warning",
            "bulk_backfill": "warning", "restatements": "warning", "duplicate_rows": "warning", "gaps": "warning", "no_record_identity": "warning", "too_few_points_for_gaps": "info",
            "partial_bars_withheld": "info", "availability_moved_to_close": "info",
            "survivorship": "warning"}                      # a query flag (a universe with listed_in=...): names trading at T that the universe leaves out
# "counted" issues are an exact count of something observable in the records; "heuristic" ones rest on a threshold (see THRESHOLDS) and are therefore never proof.
BASIS = {"late_availability": "heuristic", "bulk_backfill": "heuristic", "gaps": "heuristic", "too_few_points_for_gaps": "heuristic"}
UTC = timezone.utc


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _hour(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:00:00Z")


def _secs(delta: Any) -> int:
    return int(delta.total_seconds())


def _spacing(times: Iterable[datetime]) -> tuple[int | None, list[tuple[int, datetime]]]:
    """(median step in seconds, every step as (seconds, start)) over the distinct event times; ``None`` when there are fewer than three distinct times."""
    ts = sorted(set(times))
    if len(ts) < 3:
        return None, []
    steps = [(_secs(b - a), a) for a, b in zip(ts, ts[1:], strict=False)]
    med = median_low([s for s, _ in steps])
    return (med if med > 0 else None), steps


def _gaps(median: int | None, steps: list[tuple[int, datetime]]) -> dict[str, Any]:
    if median is None:
        return {"median_step_seconds": None, "n_gaps": None, "largest_gap_seconds": None, "largest_gap_after": None}
    big = [(s, a) for s, a in steps if s > GAP_FACTOR * median]
    top = max(big, default=None)
    return {"median_step_seconds": median, "n_gaps": len(big), "largest_gap_seconds": top[0] if top else None, "largest_gap_after": _iso(top[1]) if top else None}


# ------------------------------------------------------------------ gaps on a market calendar
# With a calendar (asof.calendars), a step between two consecutive event times is one of two kinds:
#   * WITHIN one local date: measured in seconds, and a gap when longer than GAP_FACTOR times the median of such steps;
#   * ACROSS dates: measured in the trading sessions it skips (session dates strictly between the two, which therefore hold no record), and a gap when it skips more than
#     GAP_FACTOR times the median number skipped (so for data that has something every session, one missing session is a gap).
# Nights, weekends and holidays are never counted: a step from Friday evening to Monday morning skips no session. A record ON a closed day is not a problem.

def _dater(times: Iterable[datetime], cal: str) -> Any:
    """How to read the date of an event time. A series whose every time is exactly midnight UTC holds DATES (a CSV column "2023-01-06" read in UTC), not instants: its date is
    the UTC date. Converted to New York it would land on the evening before, and every Friday would look like a Thursday."""
    ts = list(times)
    if ts and all(t.astimezone(UTC).time() == datetime.min.time() for t in ts):
        return lambda t: t.astimezone(UTC).date()
    return lambda t: calendars.local_date(cal, t)


def _calendar_steps(times: Iterable[datetime], cal: str, dater: Any = None) -> tuple[list[tuple[int, datetime]], list[tuple[int, datetime, int]], bool]:
    """(steps within a date as (seconds, start), steps across dates as (sessions skipped, start, seconds), whether some date was outside the calendar)."""
    ts = sorted(set(times))
    day = dater or _dater(ts, cal)
    within: list[tuple[int, datetime]] = []
    across: list[tuple[int, datetime, int]] = []
    outside = False
    if any(calendars.is_session(cal, day(t)) is None for t in ts):                    # a date the calendar does not cover: the answer is unknown, never 0
        return [], [], True
    for a, b in zip(ts, ts[1:], strict=False):
        da, db = day(a), day(b)
        if da == db:
            within.append((_secs(b - a), a))
            continue
        empty = calendars.sessions_between(cal, da, db)
        if empty is None:
            outside = True
            continue
        across.append((len(empty), a, _secs(b - a)))
    return within, across, outside


def _calendar_medians(within: list[tuple[int, datetime]], across: list[tuple[int, datetime, int]]) -> tuple[int | None, int | None]:
    m_within = median_low([s for s, _ in within]) if len(within) >= 2 else None
    m_across = median_low([k for k, _, _ in across]) if across else None
    return (m_within if m_within else None), m_across


def _calendar_gaps(times: Sequence[datetime], cal: str) -> dict[str, Any]:
    within, across, outside = _calendar_steps(times, cal)
    m_within, m_across = _calendar_medians(within, across)
    out: dict[str, Any] = {"median_step_seconds": m_within, "n_gaps": None, "largest_gap_seconds": None, "largest_gap_after": None, "calendar": cal,
                           "median_sessions_skipped": m_across, "n_gaps_within_day": None, "n_gaps_missed_sessions": None, "n_sessions_missed": None}
    if outside or len(set(times)) < 3 or (m_within is None and m_across is None):
        return out                                                                   # unknown, never 0
    big_within = [(s, a) for s, a in within if m_within is not None and s > GAP_FACTOR * m_within]
    big_across = [(k, a, s) for k, a, s in across if m_across is not None and k > GAP_FACTOR * m_across]
    top = max([(s, a) for s, a in big_within] + [(s, a) for _, a, s in big_across], default=None)
    out.update(n_gaps=len(big_within) + len(big_across), largest_gap_seconds=top[0] if top else None, largest_gap_after=_iso(top[1]) if top else None,
               n_gaps_within_day=len(big_within), n_gaps_missed_sessions=len(big_across), n_sessions_missed=sum(k for k, _, _ in big_across))
    return out


def _burst_hours(records: Sequence[Record]) -> dict[str, tuple[int, int]]:
    """{available_at hour: (n records, event_time span in days)} for the hours that count as a bulk backfill."""
    by: dict[str, list[Record]] = {}
    for r in records:
        if r.available_at is not None and r.event_time is not None:
            by.setdefault(_hour(r.available_at), []).append(r)
    out: dict[str, tuple[int, int]] = {}
    for h, rs in by.items():
        if len(rs) >= BURST_MIN_RECORDS:
            span = _secs(max(r.event_time for r in rs) - min(r.event_time for r in rs)) // 86400          # type: ignore[type-var,operator]
            if span >= BURST_MIN_SPAN_DAYS:
                out[h] = (len(rs), span)
    return out


def _late(records: Sequence[Record]) -> list[Record]:
    """Records that became available much later than the event they describe: more than ``LATE_LAG_FACTOR`` times the median lag, and more than ``LATE_LAG_MIN_SECONDS``. A backfill, or a vendor that restated history."""
    lagged = [(r, _secs(r.available_at - r.event_time)) for r in records if r.available_at is not None and r.event_time is not None and r.available_at >= r.event_time]   # type: ignore[operator]
    if not lagged:
        return []
    med = median_low(sorted(s for _, s in lagged))
    limit = max(LATE_LAG_MIN_SECONDS, LATE_LAG_FACTOR * med)
    return [r for r, s in lagged if s > limit]


def _issues(records: Sequence[Record], q: dict[str, Any], late: list[Record], bursts: dict[str, tuple[int, int]], now: datetime, kind: str) -> list[dict[str, Any]]:
    """One entry per flag raised, with how serious it is, how many records it touches, whether the count is exact or a heuristic, and up to ``EXAMPLES`` of the affected record ids **as hashes**
    (the same hash a ``hashes_only`` ledger gives ``record_id_sha256`` on the served record, so a platform can link an issue to its rows without a raw id ever being in the summary)."""
    from .privacy import digest
    ahead = [r for r in records if r.event_time is not None and r.available_at is not None and r.available_at < r.event_time] if kind == SERIES else []
    seen: set[Any] = set()
    dups: list[Record] = []
    for r in records:
        k = (r.record_id, _iso(r.available_at), r.payload_sha256) if q["identity"] == "record_id" else (r.payload_sha256,)
        if k in seen:
            dups.append(r)
        seen.add(k)
    by_id: dict[str, list[Record]] = {}
    for r in records:
        by_id.setdefault(r.record_id, []).append(r)
    restated = [v for vs in by_id.values() if q["identity"] == "record_id" and len({x.payload_sha256 for x in vs}) > 1 for v in sorted(vs, key=lambda x: x.order)[1:]]
    in_burst = [r for r in records if r.available_at is not None and r.event_time is not None and _hour(r.available_at) in bursts]
    rows: dict[str, list[Record]] = {
        "late_availability": late, "available_before_event": ahead, "duplicate_rows": dups, "restatements": restated, "bulk_backfill": in_burst,
        "missing_available_at": [r for r in records if r.available_at is None], "missing_event_time": [r for r in records if r.event_time is None],
        "future_dated": [r for r in records if (r.available_at is not None and r.available_at > now) or (r.event_time is not None and r.event_time > now)],
    }
    counts = {"gaps": q["gaps"]["n_gaps"], "partial_bars_withheld": q.get("notes", {}).get("partial_bars_withheld"), "availability_moved_to_close": q.get("notes", {}).get("availability_moved_to_close")}
    out = []
    for flag in q["flags"]:
        hit = rows.get(flag)
        n = len(hit) if hit is not None else counts.get(flag)
        out.append({"flag": flag, "severity": SEVERITY.get(flag, "warning"), "basis": BASIS.get(flag, "counted"), "n": n,
                    "example_record_ids_sha256": [digest(r.record_id) for r in sorted(hit, key=lambda x: x.order)[:EXAMPLES]] if hit else []})
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda i: (order.get(i["severity"], 3), i["flag"]))


def _identity(records: Sequence[Record]) -> str:
    """``row_index`` when every record id is just its position in the source (``row:<n>``): the source gave no key, so versions of one record cannot be told from different records."""
    return "row_index" if records and all(r.record_id == f"row:{r.order}" for r in records) else "record_id"


def _lag(records: Iterable[Record]) -> dict[str, int | None]:
    lags = sorted(_secs(r.available_at - r.event_time) for r in records if r.available_at is not None and r.event_time is not None and r.available_at >= r.event_time)   # type: ignore[operator]
    return {"min": lags[0] if lags else None, "median": median_low(lags) if lags else None, "max": lags[-1] if lags else None}


SERIES = "series"
CHANGE_LOG = "change_log"


def analyse_source(records: Sequence[Record], *, now: datetime, kind: str = SERIES, notes: Mapping[str, int] | None = None, calendar: str | None = None) -> dict[str, Any]:
    """The per-source summary (see the module docstring). ``now`` is the wall clock at load time, used only for ``future_dated``.
    ``calendar`` (``"XNYS"`` or ``"weekdays"``, see :mod:`asof.calendars`) makes the gap check leave nights, weekends and the exchange's holidays out; without one, time runs around the clock.

    ``kind`` is ``"series"`` (prices, filings, readings: one record per observation) or ``"change_log"`` (a universe: one record per change, **announced before it takes effect** and arriving at irregular
    intervals). For a change log, availability before the event is normal, so it is reported as ``announced_ahead`` and not as suspicious, and gaps are not measured (``null``)."""
    n = len(records)
    cal = calendars.check(calendar)
    flags = Counter(f for r in records for f in r.flags)
    ev = [r.event_time for r in records if r.event_time is not None]
    av = [r.available_at for r in records if r.available_at is not None]
    identity = _identity(records)
    ahead = sum(1 for r in records if r.event_time is not None and r.available_at is not None and r.available_at < r.event_time)
    before = 0 if kind == CHANGE_LOG else ahead
    future = sum(1 for r in records if (r.available_at is not None and r.available_at > now) or (r.event_time is not None and r.event_time > now))
    median, steps = (None, []) if kind == CHANGE_LOG else _spacing(ev)
    bursts = _burst_hours(records)
    late = [] if kind == CHANGE_LOG else _late(records)
    largest = max(bursts.items(), key=lambda kv: (kv[1][0], kv[0]), default=None)
    if identity == "record_id":
        by_id: dict[str, list[Record]] = {}
        for r in records:
            by_id.setdefault(r.record_id, []).append(r)
        restated = {rid: vs for rid, vs in by_id.items() if len({v.payload_sha256 for v in vs}) > 1}
        restatements: dict[str, Any] = {"detectable": True, "n_ids_restated": len(restated), "n_extra_versions": sum(len(vs) - 1 for vs in restated.values())}
        dup_keys = [(r.record_id, _iso(r.available_at), r.payload_sha256) for r in records]
        dup_basis = "record_id+available_at+payload"
    else:
        restatements = {"detectable": False, "n_ids_restated": None, "n_extra_versions": None}
        dup_keys = [(r.payload_sha256,) for r in records]                       # type: ignore[misc]
        dup_basis = "payload"
    duplicates = {"basis": dup_basis, "n_duplicate_rows": n - len(set(dup_keys))}
    out: dict[str, Any] = {
        "v": QUALITY_VERSION, "evaluated_at": _iso(now), "n_records": n, "kind": kind, "identity": identity,
        "event_time": {"n_present": len(ev), "n_missing": n - len(ev), "min": _iso(min(ev)) if ev else None, "max": _iso(max(ev)) if ev else None,
                       "n_naive": flags.get("naive_event_time", 0), "n_unparseable": flags.get("unparseable_event_time", 0)},
        "available_at": {"n_present": len(av), "n_missing": n - len(av), "min": _iso(min(av)) if av else None, "max": _iso(max(av)) if av else None,
                         "n_naive": flags.get("naive_available_at", 0), "n_unparseable": flags.get("unparseable_available_at", 0)},
        "timing": {"available_before_event": before, "announced_ahead": ahead if kind == CHANGE_LOG else None, "future_dated": future, "lag_seconds": _lag(records),
                   "late_availability": None if kind == CHANGE_LOG else len(late)},          # a change log arrives at irregular intervals by nature: lateness is not measured for it
        "bursts": {"n_bursts": len(bursts), "n_records_in_bursts": sum(c for c, _ in bursts.values()),
                   "largest": None if largest is None else {"available_at_hour": largest[0], "n_records": largest[1][0], "event_span_days": largest[1][1]}},
        "restatements": restatements, "duplicates": duplicates, "thresholds": dict(THRESHOLDS),
        "gaps": _gaps(median, steps) if kind == CHANGE_LOG or cal is None else _calendar_gaps(ev, cal),
    }
    if notes:
        out["notes"] = {str(k): int(v) for k, v in sorted(notes.items())}               # what the source itself did to the data before AsOf saw it (for bars: partial bars withheld, availability moved to the close)
    out["flags"] = _source_flags(out)
    out["issues"] = _issues(records, out, late, bursts, now, kind)
    sev = Counter(i["severity"] for i in out["issues"])
    out["by_severity"] = {"error": sev.get("error", 0), "warning": sev.get("warning", 0), "info": sev.get("info", 0)}          # counts of issues by severity: there is no single score
    return out


def _source_flags(q: dict[str, Any]) -> list[str]:
    f: list[str] = []
    if q.get("notes", {}).get("partial_bars_withheld"):
        f.append("partial_bars_withheld")
    if q.get("notes", {}).get("availability_moved_to_close"):
        f.append("availability_moved_to_close")
    if q["event_time"]["n_missing"]:
        f.append("missing_event_time")
    if q["available_at"]["n_missing"]:
        f.append("missing_available_at")
    if q["event_time"]["n_naive"] or q["available_at"]["n_naive"] or q["event_time"]["n_unparseable"] or q["available_at"]["n_unparseable"]:
        f.append("unusable_timestamps")
    if q["timing"]["available_before_event"]:
        f.append("available_before_event")
    if q["timing"].get("late_availability"):
        f.append("late_availability")
    if q["timing"]["future_dated"]:
        f.append("future_dated")
    if q["bursts"]["n_bursts"]:
        f.append("bulk_backfill")
    if q["restatements"]["n_ids_restated"]:
        f.append("restatements")
    if q["identity"] == "row_index":
        f.append("no_record_identity")
    if q["duplicates"]["n_duplicate_rows"]:
        f.append("duplicate_rows")
    if q["gaps"]["n_gaps"]:
        f.append("gaps")
    if q["kind"] == SERIES and q["gaps"]["n_gaps"] is None and q["event_time"]["n_present"] and q["event_time"]["n_present"] < 3:
        f.append("too_few_points_for_gaps")
    return f


def query_quality(visible: Sequence[Record], served: Sequence[Record], as_of: datetime, *, kind: str = SERIES, calendar: str | None = None) -> dict[str, Any]:
    """The per-receipt summary. ``visible`` is every record version with ``available_at <= as_of`` (what the session could see), ``served`` what this query returned.
    ``kind`` is as for :func:`analyse_source`: for a ``change_log`` nothing is said about staleness or gaps (``null``) and availability before the event is not counted as suspicious.
    ``calendar`` is as for :func:`analyse_source`: gaps between served records and staleness then leave nights, weekends and holidays out (both measured against what was visible at T)."""
    n = len(served)
    ev = [r.event_time for r in served if r.event_time is not None]
    cal = None if kind == CHANGE_LOG else calendars.check(calendar)
    visible_ev = [r.event_time for r in visible if r.event_time is not None]
    median = None if kind == CHANGE_LOG or cal else _spacing(visible_ev)[0]      # type: ignore[arg-type]
    bursts = _burst_hours(visible)
    versions: dict[str, set[str]] = {}
    for r in visible:
        versions.setdefault(r.record_id, set()).add(r.payload_sha256)
    newest = max(ev) if ev else None
    age = _secs(as_of - newest) if newest is not None else None
    stale = None if (age is None or median is None) else age > STALE_FACTOR * median
    served_median_gaps = None
    if median is not None and len(set(ev)) >= 2:
        sorted_ev = sorted(set(ev))
        served_median_gaps = sum(1 for a, b in zip(sorted_ev, sorted_ev[1:], strict=False) if _secs(b - a) > GAP_FACTOR * median)
    cal_fresh: dict[str, Any] = {}
    if cal:
        day = _dater(visible_ev, cal)                                                  # type: ignore[arg-type]
        within, across, outside = _calendar_steps(visible_ev, cal, day)                # type: ignore[arg-type]
        m_within, m_across = (None, None) if outside or len(set(visible_ev)) < 3 else _calendar_medians(within, across)
        median = m_within
        if (m_within is not None or m_across is not None) and len(set(ev)) >= 2:
            s_within, s_across, s_outside = _calendar_steps(ev, cal, day)
            served_median_gaps = None if s_outside else (sum(1 for s, _ in s_within if m_within is not None and s > GAP_FACTOR * m_within)
                                                         + sum(1 for k, _, _ in s_across if m_across is not None and k > GAP_FACTOR * m_across))
        missed = None
        if newest is not None:
            dn, dt = day(newest), calendars.local_date(cal, as_of)
            between = calendars.sessions_between(cal, dn, dt) if dn < dt else []
            missed = None if between is None else len(between)                     # sessions that passed with nothing newer to serve
            if missed is None:
                stale = None
            elif dn == dt:
                stale = None if m_within is None else age > STALE_FACTOR * m_within  # type: ignore[operator]
            else:
                stale = None if m_across is None else missed > STALE_FACTOR * m_across
        cal_fresh = {"calendar": cal, "sessions_since_newest": missed, "median_sessions_skipped": m_across}
    out: dict[str, Any] = {
        "v": QUALITY_VERSION, "kind": kind, "n_visible_versions": len(visible), "served": n,
        "served_with_event_time": len(ev), "served_with_available_at": sum(1 for r in served if r.available_at is not None),
        "available_before_event": 0 if kind == CHANGE_LOG else sum(1 for r in served if r.event_time is not None and r.available_at is not None and r.available_at < r.event_time),
        "in_backfill_burst": sum(1 for r in served if r.available_at is not None and _hour(r.available_at) in bursts),
        "restated_ids": sum(1 for r in served if len(versions.get(r.record_id, ())) > 1),
        "duplicate_rows": n - len({r.payload_sha256 for r in served}),
        "lag_seconds": _lag(served),
        "freshness": {"newest_event_time": _iso(newest), "age_seconds": age, "median_step_seconds": median, "stale": stale, **cal_fresh},
        "gaps_between_served": served_median_gaps, "thresholds": dict(THRESHOLDS),
    }
    top: dict[str, tuple[Any, int]] = {}                 # record id -> (its latest visible available_at, how many visible versions share it)
    for r in visible:
        cur = top.get(r.record_id)
        if cur is None or r.available_at > cur[0]:       # type: ignore[operator]
            top[r.record_id] = (r.available_at, 1)
        elif r.available_at == cur[0]:
            top[r.record_id] = (cur[0], cur[1] + 1)
    tied = sum(1 for r in served if top.get(r.record_id, (None, 1))[1] > 1)
    if tied:                                             # written only when it happens, so the summaries (and ledger hashes) of data without ties are unchanged
        out["tied_versions"] = tied
    out["flags"] = _query_flags(out)
    return out


def _query_flags(q: dict[str, Any]) -> list[str]:
    f: list[str] = []
    if q["served"] and q["served_with_event_time"] < q["served"]:
        f.append("missing_event_time")
    if q["served"] and q["served_with_available_at"] < q["served"]:
        f.append("unknown_availability")
    if q["available_before_event"]:
        f.append("available_before_event")
    if q["in_backfill_burst"]:
        f.append("bulk_backfill")
    if q["restated_ids"]:
        f.append("restated")
    if q["duplicate_rows"]:
        f.append("duplicate_rows")
    if q.get("tied_versions"):
        f.append("tied_versions")                        # several versions of a served record became available at the same instant: which one is "the latest" is not given by the clocks
    if q["freshness"]["stale"]:
        f.append("stale")
    if q["gaps_between_served"]:
        f.append("gaps")
    if not q["served"]:
        f.append("nothing_served")
    return f


def survivorship(members: Sequence[str], listed_records: Sequence[Record], as_of: datetime, *, field: str, calendar: str | None = None) -> dict[str, Any]:
    """Names that were trading at ``as_of`` but are not ``members`` of a universe at ``as_of``: the survivorship check.

    "Trading at T" is read from ``listed_records`` (normally prices): a name is listed if it has a record whose ``event_time`` falls on the **latest date that has any record**,
    among the records visible at T (``available_at`` and ``event_time`` at or before T). Dates are local dates on ``calendar`` (UTC without one). Nothing after T is used.
    Returns the evidence date, the number listed, and the listed names missing from the universe (sorted) with their count."""
    seen = [r for r in listed_records if r.available_at is not None and r.event_time is not None and r.available_at <= as_of and r.event_time <= as_of and r.payload.get(field) is not None]
    if not seen:
        return {"evidence_date": None, "n_listed": 0, "n_listed_not_members": 0, "listed_not_members_sha256": None, "listed_not_members": []}
    cal = calendars.check(calendar)
    tz_date = _dater([r.event_time for r in seen], cal) if cal else (lambda t: t.astimezone(UTC).date())      # type: ignore[misc]
    last = max(tz_date(r.event_time) for r in seen)                                             # type: ignore[arg-type]
    listed = {str(r.payload[field]) for r in seen if tz_date(r.event_time) == last}             # type: ignore[arg-type]
    missing = sorted(listed - {str(m) for m in members})
    from .privacy import digest
    return {"evidence_date": last.isoformat(), "n_listed": len(listed), "n_listed_not_members": len(missing), "listed_not_members_sha256": digest(missing), "listed_not_members": missing}


def coverage(present: int, total: int) -> float | None:
    """A ratio for display. ``None`` for an empty source: no records is unknown coverage, not full and not zero."""
    return None if not total else present / total
