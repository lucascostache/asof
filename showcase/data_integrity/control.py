"""A CLEAN CONTROL for the data-integrity fixture: the same SYNTHETIC tables with every planted problem taken out, run through the same checks with the same source declarations.
Anything the checks flag on it is a false positive, and is reported as one. Nothing is tuned: the checks and their thresholds are the library's defaults.

    python -m showcase.data_integrity.control

What "clean" means here, per planted problem (the control is derived from the committed fixture files, so it is exactly as reproducible as they are):

    backfill        estimates.csv   each estimate becomes available 14 to 23 days after its period ends (not all in one hour in November 2023)
    restatement     fundamentals    the 12 "restated" rows are dropped: one value per symbol and period
    bar_close       bars.csv        the 10 tickers stamped at the open are stamped at 16:20 New York like the other 40
    timestamp_gaps  news.csv        the 300 rows with no available_at get one (event time + 120 s)
    survivorship    bars, universe  the 6 delisted tickers are left out altogether: 44 tickers, none removed
"""
from __future__ import annotations

import csv
import json
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .common import FIX, MANIFEST, OUT, run_checks

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def _read(name: str) -> tuple[list[str], list[dict[str, str]]]:
    with open(FIX / name, newline="") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), list(r)


def _write(path: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_clean_control(out: Path) -> dict[str, int]:
    """Write the five tables without the planted problems into ``out``. Returns the row count of each."""
    out.mkdir(parents=True, exist_ok=True)
    gone = set(MANIFEST["planted"]["survivorship"]["delisted"])
    n: dict[str, int] = {}

    h, rows = _read("bars.csv")
    rows = [r for r in rows if r["symbol"] not in gone]
    for r in rows:
        r["vendor_available_at"] = _iso(datetime.combine(date.fromisoformat(r["open_time"]), time(16, 20), tzinfo=NY))
    _write(out / "bars.csv", h, rows)
    n["bars.csv"] = len(rows)

    h, rows = _read("fundamentals.csv")
    rows = [r for r in rows if r["version"] == "original" and r["symbol"] not in gone]
    _write(out / "fundamentals.csv", h, rows)
    n["fundamentals.csv"] = len(rows)

    h, rows = _read("news.csv")
    for r in rows:
        if not r["available_at"]:
            r["available_at"] = _iso(datetime.fromisoformat(r["event_time"].replace("Z", "+00:00")) + timedelta(seconds=120))
    _write(out / "news.csv", h, rows)
    n["news.csv"] = len(rows)

    h, rows = _read("estimates.csv")
    rows = [r for r in rows if r["symbol"] not in gone]
    for k, r in enumerate(rows):
        r["available_at"] = _iso(datetime.combine(date.fromisoformat(r["period_end"]) + timedelta(days=14 + k % 10), time(12, 0), tzinfo=UTC) + timedelta(seconds=k))
    _write(out / "estimates.csv", h, rows)
    n["estimates.csv"] = len(rows)

    h, rows = _read("universe.csv")
    rows = [r for r in rows if r["symbol"] not in gone]
    _write(out / "universe.csv", h, rows)
    n["universe.csv"] = len(rows)
    return n


def false_positives(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Every flag raised on the control, of warning severity or worse and of info severity alike: none is hidden."""
    out = []
    for name, q in sorted(report["sources"].items()):
        for flag in q["flags"]:
            iss = next((i for i in q.get("issues", []) if i["flag"] == flag), {})
            out.append({"source": name, "flag": flag, "severity": iss.get("severity"), "basis": iss.get("basis"), "n": iss.get("n"), "of": q.get("n_records")})
    return out


def run() -> dict[str, Any]:
    from .detect import survivorship_check
    with tempfile.TemporaryDirectory() as d:
        counts = write_clean_control(Path(d))
        report = run_checks("day1", root=Path(d))
        surv = survivorship_check("as_of", root=Path(d))                                              # the control's universe, queried at the start with the survivorship check on
    fp = false_positives(report)
    if surv["flagged"]:
        fp.append({"source": "universe", "flag": "survivorship", "severity": "warning", "basis": "counted", "n": len(surv["listed_not_members"]), "of": surv["n_listed"]})
    return {"synthetic": True, "tables": counts, "flags_per_source": {k: v["flags"] for k, v in sorted(report["sources"].items())}, "false_positives": fp,
            "planted_flags_raised_on_control": sorted({(f["source"], f["flag"]) for f in fp} & ({(p["source"], p["expect_flag"]) for p in MANIFEST["planted"].values() if "expect_flag" in p}
                                                                                                | {("universe", "survivorship")})),
            "survivorship_at_start": {k: surv[k] for k in ("at", "n_members", "n_listed", "flagged", "listed_not_members")},
            "gaps_detail": {k: v["gaps"] for k, v in sorted(report["sources"].items()) if "gaps" in v["flags"]}, "chain_ok": report["chain"]["ok"]}


def section(res: dict[str, Any]) -> str:
    """The README's account of the control run, made from the result so that it cannot drift from it."""
    t = res["tables"]
    rows = "\n".join(f"| `{name}` | {t.get(name + '.csv', 0)} | {', '.join(f'`{x}`' for x in flags) or 'none'} |" for name, flags in res["flags_per_source"].items())
    fps = "\n".join(f"- `{f['source']}`: `{f['flag']}` ({f['severity']}, n={f['n']} of {f['of']} rows, {f['basis'] or 'counted'})." for f in res["false_positives"]) or "- none"
    g = res["gaps_detail"].get("news")
    why = (f"\n\nThe `gaps` flag on `news` is a false positive. The source is declared on the New York Stock Exchange calendar, so nights, weekends and exchange holidays are not counted "
           f"(before the calendar existed this flag counted 530 gaps, 516 of them nights and weekends). What is left: {g['n_gaps_within_day']} quiet stretches inside one day, each longer than "
           f"five times the median step within a day ({g['median_step_seconds']} s, so longer than {5 * g['median_step_seconds'] / 3600:.1f} hours), and {g['n_gaps_missed_sessions']} trading "
           f"day(s) with no story at all. The news table holds about six stories a day at random times, so both happen by chance; nothing was removed from it. The rule was not changed "
           f"to hide this: on a sparse feed, read the count, not the flag.") if g and g.get("calendar") else ""
    s = res.get("survivorship_at_start")
    surv = (f"\n\nThe survivorship check on the control's universe at {s['at']} ({s['n_members']} names; the prices show {s['n_listed']} trading): "
            f"{'no flag' if not s['flagged'] else 'FLAGGED, a false positive'}.") if s else ""
    return f"""## Clean control: what the checks say when nothing is planted

The same tables with every planted problem taken out (`python -m showcase.data_integrity.control`; how each one is removed is at the top of `data_integrity/control.py`), the same source declarations, the same checks and default thresholds.

| source | rows | flags |
|---|---|---|
{rows}

Planted-issue flags raised on the control: **{len(res['planted_flags_raised_on_control'])}**. False positives: **{len(res['false_positives'])}**.

{fps}{why}{surv}

This is one control dataset made from one fixture: it shows that these checks do not fire on this clean data, not a false-positive rate for data in general."""


def main() -> dict[str, Any]:
    res = run()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "control.json").write_text(json.dumps(res, indent=1, sort_keys=True, default=str) + "\n")
    print("clean control (SYNTHETIC; the planted problems removed):")
    for name, flags in res["flags_per_source"].items():
        print(f"  {name:13s} {res['tables'].get(name + '.csv', 0):6d} rows   flags: {', '.join(flags) or 'none'}")
    print(f"false positives: {len(res['false_positives'])}")
    for f in res["false_positives"]:
        print(f"  {f['source']}: {f['flag']} ({f['severity']}, n={f['n']} of {f['of']}, {f['basis'] or 'counted'})")
    return res


if __name__ == "__main__":
    main()
