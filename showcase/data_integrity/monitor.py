"""MONITORING PREVIEW (monitoring is not built; this is honest about that): the same checks run by hand on two snapshots of the fixture, day 1 and day 30, and the DIFFERENCE shown as an alert list.

This is two manual check runs compared after the fact. Nothing watches anything, nothing runs on a schedule, and no alert was sent anywhere.

Rules (they belong to this preview, not to the checks; the checks are not changed):
  * a flag that is on a source on day 30 but was not on day 1                              -> an alert, at the flag's own severity (asof.quality.SEVERITY)
  * a source that did not exist on day 1 and has flags on day 30                            -> an alert per flag
  * the median delay between event and availability of the NEW rows is at least FEED_FACTOR times the day-1 median and at least FEED_MIN_SECONDS -> a "feed delay" alert
  * the ledger chain of each run verifies                                                    -> a green line
"""
from __future__ import annotations

from statistics import median
from typing import Any

from asof.quality import SEVERITY

from .common import sources

FEED_FACTOR = 3
FEED_MIN_SECONDS = 300
LABEL = "Simulated: two manual check runs, not live monitoring."


def _lags(day: str, source: str) -> dict[str, float]:
    src = next(s for s in sources(day) if s.name == source)
    return {r.record_id: (r.available_at - r.event_time).total_seconds() for r in src.records() if r.available_at is not None and r.event_time is not None}


def human(sec: float) -> str:
    return f"{sec / 3600:.1f} h" if sec >= 7200 else f"{sec / 60:.0f} min" if sec >= 90 else f"{sec:.0f} s"


def diff(d1: dict[str, Any], d30: dict[str, Any]) -> list[dict[str, Any]]:
    """The alert list: dicts with level (error | warning | info | ok), source and text. Order: new problems by severity, then the chain line, which is last (newest at the bottom)."""
    out: list[dict[str, Any]] = []
    s1, s30 = d1["sources"], d30["sources"]
    for name, q in sorted(s30.items()):
        if name not in s1:
            for flag in q["flags"]:
                iss = next((i for i in q["issues"] if i["flag"] == flag), None)
                n = (iss or {}).get("n") or q["n_records"]
                out.append({"level": SEVERITY.get(flag, "info"), "source": name, "text": f"new source {name}: {flag.replace('_', ' ')} ({n:,} rows)", "flag": flag, "n": n})
            continue
        for flag in sorted(set(q["flags"]) - set(s1[name]["flags"])):
            iss = next((i for i in q["issues"] if i["flag"] == flag), None)
            out.append({"level": SEVERITY.get(flag, "info"), "source": name, "text": f"{name}: new flag {flag.replace('_', ' ')}", "flag": flag, "n": (iss or {}).get("n")})
        if q["n_records"] != s1[name]["n_records"] and name == "news":
            old, new = _lags("day1", name), _lags("day30", name)
            fresh = [v for k, v in new.items() if k not in old]
            if fresh and old:
                a, b = median(old.values()), median(fresh)
                if b >= FEED_FACTOR * a and b >= FEED_MIN_SECONDS:
                    out.append({"level": "warning", "source": name, "text": f"{name}: feed delay {human(a)} → {human(b)} ({len(fresh)} new rows)", "flag": "feed_delay", "n": len(fresh)})
    rank = {"error": 0, "warning": 1, "info": 2}
    out.sort(key=lambda a: (rank.get(a["level"], 3), a["source"], a["text"]))
    c30 = d30["chain"]
    out.append({"level": "ok" if c30["ok"] else "error", "source": "ledger", "text": f"chain verified · {c30['n_events']:,} events" if c30["ok"] else f"chain broken at event {c30['first_bad_seq']}", "flag": "chain", "n": c30["n_events"]})
    return out
