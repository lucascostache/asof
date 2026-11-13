"""Compare what the REAL checks found with what the manifest says was planted. Nothing here is tuned to the fixture: a planted issue that no check flags is reported NOT DETECTED and left that way."""
from __future__ import annotations

import csv
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asof import Session
from asof.ledger import Ledger
from asof.quality import SEVERITY

from .common import FIX, MANIFEST, OUT, pinned_clock, run_checks, sources

UTC = timezone.utc


START = datetime(2022, 1, 4, 14, 0, tzinfo=UTC)                                                  # the start of the backtest: 09:00 New York on its second trading day


def survivors_only(root: Path | None = None) -> list[str]:
    """ "Who is trading on the last day": the list a careless backtest uses for every date. This IS the planted survivorship problem, in the form it reaches a backtest."""
    with open((root or FIX) / "bars.csv") as f:
        rows = list(csv.DictReader(f))
    return sorted({r["symbol"] for r in rows if r["open_time"] == MANIFEST["period"][1]})


def survivorship_check(universe: str, root: Path | None = None, at: datetime = START) -> dict[str, Any]:
    """Ask a session for the members of a universe at ``at`` with the REAL check on (``Universe(listed_in="bars")``); return the receipt's flags and what the check found.
    ``universe`` is ``"as_of"`` (the fixture's change log, delistings included) or ``"survivors_only"`` (the list above, every name added at the start, nobody ever removed)."""
    from asof.sources import Universe
    srcs = {s.name: s for s in sources(root=root)}
    uni = srcs["universe"] if universe == "as_of" else Universe([{"symbol": s, "action": "add", "effective": "2021-12-15", "announced": "2021-12-01"} for s in survivors_only(root)],
                                                               name="universe", assume_tz="UTC", listed_in="bars")
    with pinned_clock(at) as clock, tempfile.TemporaryDirectory() as d:
        s = Session(at, ledger=Ledger(Path(d) / "l.db", now=clock), session_id=f"survivorship-{universe}")
        s.add_source(srcs["bars"])
        s.add_source(uni)
        members = sorted(s.members("universe"))
        q = s.receipt().quality
        s.close()
    listed = q["membership"]["listed"]
    return {"universe": universe, "at": at.isoformat().replace("+00:00", "Z"), "n_members": len(members), "flags": q["flags"], "flagged": "survivorship" in q["flags"],
            "n_listed": listed["n_listed"], "evidence_date": listed["evidence_date"], "listed_not_members": listed["listed_not_members"]}


def survivorship_measurement() -> dict[str, Any]:
    """The same problem measured without the flag: the as-of universe at the start compared with the survivors-only list (Session.members)."""
    naive = survivors_only()
    as_of = survivorship_check("as_of")
    with pinned_clock(START) as clock, tempfile.TemporaryDirectory() as d:
        s = Session(START, ledger=Ledger(Path(d) / "l.db", now=clock), session_id="survivorship")
        srcs = {x.name: x for x in sources()}
        s.add_source(srcs["bars"])
        s.add_source(srcs["universe"])
        members = sorted(s.members("universe"))
        s.close()
    missing = sorted(set(members) - set(naive))
    return {"naive_universe_size": len(naive), "as_of_universe_size_at_start": as_of["n_members"], "missing_from_naive": missing, "planted": sorted(MANIFEST["planted"]["survivorship"]["delisted"]),
            "matches_planted": missing == sorted(MANIFEST["planted"]["survivorship"]["delisted"])}


def _issue(q: dict[str, Any], flag: str) -> dict[str, Any] | None:
    return next((i for i in q.get("issues", []) if i["flag"] == flag), None)


def table(report: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per planted issue: where it was planted, which check should see it, whether it did, how serious, and how the count compares with the planted count."""
    P, rows = MANIFEST["planted"], []
    planted_n = {"backfill": P["backfill"]["rows"], "restatement": P["restatement"]["n_restated"], "bar_close": P["bar_close"]["rows"], "timestamp_gaps": P["timestamp_gaps"]["rows"]}
    for key in ("backfill", "restatement", "bar_close", "timestamp_gaps"):
        p = P[key]
        q = report["sources"][p["source"]]
        flag = p["expect_flag"]
        hit = flag in q["flags"]
        iss = _issue(q, flag)
        if key == "restatement":
            found = q["restatements"]["n_ids_restated"]
        elif key == "bar_close":
            found = (q.get("notes") or {}).get("availability_moved_to_close")
        else:
            found = iss["n"] if iss else None
        rows.append({"planted": key, "table": p["table"], "check": f"quality.py:{flag}" if key != "bar_close" else "bars.py + quality.py:availability_moved_to_close", "detected": "DETECTED" if hit else "NOT DETECTED",
                     "severity": SEVERITY.get(flag) if hit else None, "basis": (iss or {}).get("basis"), "planted_count": planted_n[key], "found_count": found, "note": ""})
    # survivorship: the flag on a universe query (Universe(listed_in="bars")), on the survivors-only list at the start; and the same check on the real universe, where it must stay quiet
    surv = survivorship_measurement()
    planted = sorted(P["survivorship"]["delisted"])
    naive, real = survivorship_check("survivors_only"), survivorship_check("as_of")
    hit = naive["flagged"] and naive["listed_not_members"] == planted
    rows.append({"planted": "survivorship", "table": "universe.csv + bars.csv", "check": "quality.py:survivorship (a survivors-only universe queried at the start)",
                 "detected": "DETECTED" if hit else "NOT DETECTED", "severity": SEVERITY["survivorship"] if hit else None, "basis": "counted",
                 "planted_count": len(planted), "found_count": len(naive["listed_not_members"]),
                 "note": (f"at {naive['at']} the list has {naive['n_members']} names and the prices of {naive['evidence_date']} show {naive['n_listed']} trading; the "
                          f"{len(naive['listed_not_members'])} it leaves out are {'exactly the delisted ones' if hit else 'not the delisted ones'}. The fixture's own universe at the same time: "
                          f"{'no flag' if not real['flagged'] else 'FLAGGED (a false positive)'}. After the last delisting a survivors-only list is right, so the flag can only show it before then")})
    rows.append({"planted": "survivorship", "table": "universe.csv + bars.csv", "check": "Session.members (universe as of T) vs a survivors-only list", "detected": "MEASURED" if surv["matches_planted"] else "NOT DETECTED",
                 "severity": None, "basis": "counted", "planted_count": len(P["survivorship"]["delisted"]), "found_count": len(surv["missing_from_naive"]),
                 "note": f"as-of universe at the start has {surv['as_of_universe_size_at_start']} names, the survivors-only list {surv['naive_universe_size']}; the {len(surv['missing_from_naive'])} missing are exactly the delisted ones" if surv["matches_planted"] else "the difference does not match"})
    return rows


def unplanted(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Flags the checks raised that the manifest does not list: reported, not explained away."""
    expected = {(p["source"], p["expect_flag"]) for p in MANIFEST["planted"].values() if "expect_flag" in p}
    out = []
    for name, q in sorted(report["sources"].items()):
        for flag in q["flags"]:
            if (name, flag) not in expected:
                iss = _issue(q, flag)
                out.append({"source": name, "flag": flag, "severity": SEVERITY.get(flag), "n": (iss or {}).get("n"), "basis": (iss or {}).get("basis")})
    return out


def render_table(rows: list[dict[str, Any]], extra: list[dict[str, Any]]) -> str:
    head = ["planted issue", "found by", "detected?", "severity", "planted", "found"]
    body = [[r["planted"], r["check"], r["detected"], r["severity"] or "-", str(r["planted_count"]), "-" if r["found_count"] is None else str(r["found_count"])] for r in rows]
    w = [max(len(x[i]) for x in [head] + body) for i in range(len(head))]
    line = lambda c: "  ".join(x.ljust(w[i]) for i, x in enumerate(c))               # noqa: E731
    out = [line(head), line(["-" * n for n in w])] + [line(b) for b in body]
    for r in rows:
        if r["note"]:
            out.append(f"  note ({r['planted']}): {r['note']}")
    for u in extra:
        out.append(f"  unplanted flag: {u['source']}: {u['flag']} ({u['severity']}, n={u['n']}, {u['basis'] or 'counted'}) - not in the manifest")
    return "\n".join(out)


def main() -> dict[str, Any]:
    report = {"day1": run_checks("day1"), "day30": run_checks("day30")}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "data_integrity_report.json").write_text(json.dumps(report, indent=1, sort_keys=True, default=str) + "\n")             # the raw reports, untouched
    rows, extra = table(report["day1"]), unplanted(report["day1"])
    (OUT / "detection.json").write_text(json.dumps({"rows": rows, "unplanted_flags": extra, "survivorship": survivorship_measurement()}, indent=1, sort_keys=True) + "\n")
    text = render_table(rows, extra)
    (OUT / "detection.txt").write_text(text + "\n")
    print(text)
    return report


if __name__ == "__main__":
    main()
