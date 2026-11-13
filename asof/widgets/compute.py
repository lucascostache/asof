"""Pure computations behind the widgets. No clock, no I/O: the same rows in, the same numbers out.

``rolling_sharpe``: at each point in time, the Sharpe ratio of the last ``window`` returns **that were knowable then** (``available_at`` <= the point's time). A return that was only published
after the point is not used, however early its ``event_time``. A point with fewer than ``window`` such returns is ``None`` (unknown), never 0.

``leak_overlay``: the same metric computed twice, *locked* (as above) and *unlocked* (every return whose ``event_time`` is <= the point, as a backtest that ignores publication delay would do), and the
difference between them. That difference is **observed**: it is what changed between two runs, which is evidence of look-ahead, not a proof of cause.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import sqrt
from statistics import fmean, stdev
from typing import Any, Mapping, Sequence

from ..clock import parse_time

LABEL = "difference attributable to look-ahead (observed)"
RULE = "each point uses only rows with available_at <= that point's time"


def _t(v: Any, what: str) -> datetime:
    return parse_time(v, what=what)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _prep(rows: Sequence[Mapping[str, Any]], ret: str, event_time: str, available_at: str) -> list[tuple[datetime, datetime, float]]:
    out = []
    for i, r in enumerate(rows):
        for k in (ret, event_time, available_at):
            if k not in r:
                raise ValueError(f"row {i} has no {k!r}")
        v = r[ret]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"row {i}: {ret} must be a finite number")
        out.append((_t(r[event_time], f"row {i} {event_time}"), _t(r[available_at], f"row {i} {available_at}"), float(v)))
    return sorted(out, key=lambda x: (x[0], x[1]))


def _lag(prep: list[tuple[datetime, datetime, float]]) -> dict[str, int] | None:
    """How long after its event a row became available, over all rows: median and longest, in whole seconds. ``None`` when no row lags (then there is nothing to say)."""
    lags = sorted(int((a - e).total_seconds()) for e, a, _ in prep if a >= e)
    return {"median": lags[len(lags) // 2], "max": lags[-1]} if lags and lags[-1] > 0 else None


def _sharpe(vals: list[float], ppy: int) -> float | None:
    if len(vals) < 2:
        return None
    sd = stdev(vals)
    return None if sd == 0 else round(fmean(vals) / sd * sqrt(ppy), 6)


def _series(rows: list[tuple[datetime, datetime, float]], at: Sequence[datetime], window: int, ppy: int, locked: bool) -> list[dict[str, Any]]:
    pts = []
    for p in at:
        vis = [r for r in rows if (r[1] <= p if locked else r[0] <= p)]
        used = [r[2] for r in vis[-window:]]
        v = _sharpe(used, ppy) if len(vis) >= window else None
        pts.append({"t": _iso(p), "v": v, "n": len(used) if len(vis) >= window else len(vis)})
    return pts


def rolling_sharpe(rows: Sequence[Mapping[str, Any]], *, window: int = 20, at: Sequence[Any] | None = None, periods_per_year: int = 252, ret: str = "ret",
                   event_time: str = "event_time", available_at: str = "available_at", synthetic: bool = False, title: str | None = None, calendar: str | None = None) -> dict[str, Any]:
    """Rolling Sharpe as of each point (see the module docstring). ``at`` defaults to every distinct ``available_at``: the moments a new return became knowable.
    ``calendar`` is a plain-words statement of what one period is (e.g. "business days (Mon-Fri)"); it is shown next to ``periods_per_year`` because annualising with 252 only makes sense for business days."""
    if window < 2:
        raise ValueError("window must be at least 2")
    prep = _prep(rows, ret, event_time, available_at)
    pts_at = sorted({_t(x, "at") for x in at}) if at is not None else sorted({r[1] for r in prep})
    d: dict[str, Any] = {"title": title or f"Rolling Sharpe ({window}), as of", "window": window, "periods_per_year": periods_per_year, "rule": RULE, "points": _series(prep, pts_at, window, periods_per_year, True)}
    if _lag(prep):
        d["lag_seconds"] = _lag(prep)                          # stated on the widget: it is why a point can have fewer rows than days have passed
    if calendar:
        d["calendar"] = calendar
    if synthetic:
        d["synthetic"] = True
    return d


def leak_overlay(rows: Sequence[Mapping[str, Any]], *, window: int = 20, at: Sequence[Any] | None = None, periods_per_year: int = 252, ret: str = "ret", event_time: str = "event_time",
                 available_at: str = "available_at", synthetic: bool = False, title: str | None = None, calendar: str | None = None) -> dict[str, Any]:
    """The rolling Sharpe locked and unlocked, side by side, with the gap between them summarised. Points where either is unknown are not compared (and are counted as such)."""
    if window < 2:
        raise ValueError("window must be at least 2")
    prep = _prep(rows, ret, event_time, available_at)
    pts_at = sorted({_t(x, "at") for x in at}) if at is not None else sorted({r[1] for r in prep} | {r[0] for r in prep})
    locked, unlocked = _series(prep, pts_at, window, periods_per_year, True), _series(prep, pts_at, window, periods_per_year, False)
    gaps = [abs(u["v"] - l["v"]) for l, u in zip(locked, unlocked, strict=True) if l["v"] is not None and u["v"] is not None]
    d: dict[str, Any] = {"title": title or "Look-ahead leak overlay", "metric": f"rolling Sharpe ({window})", "label": LABEL, "window": window, "periods_per_year": periods_per_year, "locked": locked, "unlocked": unlocked,
                         "summary": {"n_points": len(pts_at), "n_compared": len(gaps), "max_abs_gap": round(max(gaps), 6) if gaps else None, "mean_abs_gap": round(fmean(gaps), 6) if gaps else None}}
    if _lag(prep):
        d["lag_seconds"] = _lag(prep)
    if calendar:
        d["calendar"] = calendar
    if synthetic:
        d["synthetic"] = True
    return d


def percent_shares(values: Sequence[float | None], total: float | None = None) -> list[int | None]:
    """Each value's share of ``total`` (default: their sum) in WHOLE percent, worked out once from the unrounded values and rounded so the shares add up to 100: round every share down, then give the missing
    points to the parts with the largest fractional remainders (the earlier part first on a tie). Exact decimal arithmetic, so the same input gives the same shares everywhere. Every view of a gap breakdown
    (internal and shared) reads these numbers, and the JavaScript fallback ``shares`` in asof-widgets.js is the same rule. ``None`` for every part when a value is missing or the total is 0: unknown, not 0%."""
    from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
    n = len(values)
    if n == 0:
        return []
    fine = all(isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf") for v in values)
    tot = sum(values) if total is None and fine else total                                  # type: ignore[arg-type]
    if not fine or not isinstance(tot, (int, float)) or isinstance(tot, bool) or tot == 0 or tot != tot:
        return [None] * n
    dt = Decimal(repr(float(tot)))
    raw = [Decimal(100) * Decimal(repr(float(v))) / dt for v in values]                      # type: ignore[arg-type]
    floors = [r.to_integral_value(rounding=ROUND_FLOOR) for r in raw]
    left = int(sum(raw, Decimal(0)).to_integral_value(rounding=ROUND_HALF_EVEN)) - int(sum(floors, Decimal(0)))      # a Decimal start, so the sum is a Decimal for the type checker too (same value)
    for i in sorted(range(n), key=lambda i: (-(raw[i] - floors[i]), i))[:max(left, 0)]:
        floors[i] += 1
    return [int(f) for f in floors]


def measure_gap_effects(rows: Sequence[Mapping[str, Any]], *, at: Any, window: int = 63, flagged_ids: Sequence[Any] = (), periods_per_year: int = 252, ret: str = "ret", event_time: str = "event_time",
                        available_at: str = "available_at", record_id: str = "record_id") -> dict[str, Any]:
    """The two MEASURED parts of a return gap at the point ``at``, from two disjoint sets of rows, in percentage points of annualised return (mean return x ``periods_per_year`` x 100).

    * **Look-ahead leakage**: the rows the unlocked run uses (the last ``window`` with ``event_time`` <= ``at``) that were not yet available (``available_at`` > ``at``). Effect = locked return minus unlocked return.
    * **Data issues**: wrong or revised values, taken from ``flagged_ids`` (the record ids a data-quality report flags). Among the rows the LOCKED run really uses (the last ``window`` with ``available_at`` <= ``at``),
      the flagged ones: when an id appears more than once its repeats are the issue rows (the first copy is kept); an id that appears once is itself. Effect = locked return minus the locked return without them.

    The sets cannot overlap: leakage rows are not available at ``at`` and data-issue rows are, so a flagged row that is not yet published counts as leakage and never again as a data issue. They are returned as row
    positions (index into ``rows``), so two copies of one record are told apart. ``None`` effects mean a window had no rows. Nothing here reads the clock."""
    prep = []
    for i, r in enumerate(rows):
        for k in (ret, event_time, available_at):
            if k not in r:
                raise ValueError(f"row {i} has no {k!r}")
        prep.append((_t(r[event_time], f"row {i} {event_time}"), _t(r[available_at], f"row {i} {available_at}"), float(r[ret]), r.get(record_id), i))
    prep.sort(key=lambda x: (x[0], x[1], x[4]))
    p, flagged = _t(at, "at"), set(flagged_ids)
    seen: set[Any] = set()
    repeats: set[int] = set()                                                          # row positions that are a repeat of an id already seen, or the only copy of a flagged id
    count = {rid: sum(1 for x in prep if x[3] == rid) for rid in flagged}
    for x in prep:
        if x[3] in flagged and (count[x[3]] == 1 or x[3] in seen):
            repeats.add(x[4])
        seen.add(x[3])
    locked = [x for x in prep if x[1] <= p][-window:]
    unlocked = [x for x in prep if x[0] <= p][-window:]
    tidy = [x for x in prep if x[1] <= p and x[4] not in repeats][-window:]
    leak_rows = sorted(x[4] for x in unlocked if x[1] > p)
    data_rows = sorted(x[4] for x in locked if x[4] in repeats)
    ann = lambda v: round(fmean(x[2] for x in v) * periods_per_year * 100, 6) if v else None      # noqa: E731
    lo, un, cl = ann(locked), ann(unlocked), ann(tidy)
    return {"point": _iso(p), "window": window, "periods_per_year": periods_per_year, "leakage_rows": leak_rows, "data_issue_rows": data_rows,
            "locked_return_pp": lo, "unlocked_return_pp": un, "clean_return_pp": cl,
            "leakage_pp": None if lo is None or un is None else round(lo - un, 4), "data_issues_pp": None if lo is None or cl is None else round(lo - cl, 4),
            "volatility": round(stdev([x[2] for x in locked]) * sqrt(periods_per_year), 6) if len(locked) > 1 else None}


def gap_breakdown(attribution: Mapping[str, Any], *, synthetic: bool = False, title: str | None = None) -> dict[str, Any]:
    """Normalise a Verumark attribution (``gap``, ``components[{id, name, value, label}]``, optional ``unit``) for the widget. The parts are in an ADDITIVE unit (a return, e.g. ``unit: "pp"`` = percentage points
    of annualised return): a ratio such as a Sharpe ratio does not split into parts that add up, so a Sharpe gap travels only as ``headline: {sharpe_gap, method}`` and is shown as one number. No value is recomputed: a component whose value is ``None`` stays "not computed".
    Whether the parts add up to the gap is checked here, not taken from the input: ``reconciles`` is ``True`` / ``False`` from the numbers (within 1e-9), or ``None`` when a part or the gap is missing."""
    if "components" not in attribution:
        raise ValueError("an attribution has components")
    comps = [{"id": c.get("id"), "name": c.get("name", c.get("id")), "value": c.get("value"), "label": c.get("label", "UNKNOWN")} for c in attribution["components"]]
    vals, gap = [c["value"] for c in comps], attribution.get("gap")
    numeric = isinstance(gap, (int, float)) and not isinstance(gap, bool) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals)
    for c, s in zip(comps, percent_shares(vals, gap), strict=True):                       # the percentages, once: every view reads these
        c["share"] = s
    d: dict[str, Any] = {"title": title or "Where the gap came from", "gap": gap, "components": comps, "mode": attribution.get("mode"), "reconciles": (abs(sum(vals) - gap) < 1e-9) if numeric else None}
    for k in ("unit", "metric"):
        if attribution.get(k):
            d[k] = attribution[k]
    for k in ("headline", "annualisation", "row_counts"):                    # the Sharpe gap as a headline (never split into parts) and how the return was annualised: passed through, not recomputed
        if isinstance(attribution.get(k), Mapping):
            d[k] = dict(attribution[k])
    if synthetic:
        d["synthetic"] = True
    return d
