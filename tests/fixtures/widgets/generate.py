"""Regenerate the widget fixtures from REAL AsOf output on ONE shared synthetic source (a fictional strategy's daily returns; no real data, no keys, no network).

    python tests/fixtures/widgets/generate.py

Every widget fixture is derived from the same rows (504 business days plus one batch of 40 loaded twice), so the counts agree across widgets by construction (and tests/test_widget_consistency.py checks that they do).
The planted structure is realistic in COUNT: about 28 rows are pending at ``AS_OF`` (leakage) and 40 are repeated copies (data issues), measured over a one-year window, so no single row drives a large share of the gap:

* a real Session reads the rows as of ``AS_OF``: receipt.json (served / blocked-by-the-clock counts), chain.json, quality.json (the real data-quality check on the same rows);
* timeline.json is the three clocks of those same rows (one event per row) with ``as_of`` = ``AS_OF``;
* rolling-sharpe.json and leak-overlay.json are computed from the same rows on the same grid of points (one per business day of the data, at the day's close), with ``WINDOW`` = 63 (one quarter of trading days; configurable everywhere else);
* attribution.json (gap breakdown) decomposes the RETURN gap (annualised return, percentage points), because returns add and a Sharpe ratio does not. Its Look-ahead leakage part is the locked-minus-unlocked
  annualised return at ``AS_OF`` over the same window as the leak overlay, its Data issues part is the locked return with every row the quality report flags minus the locked return without them, and the gap
  is the sum of its parts, so they add up to it exactly. Costs, Signal decay, Timing, Slippage and Unexplained are hand-set numbers in the engine's attribution shape (this repository has no execution engine
  to measure them). The Sharpe gap is one headline number (return gap divided by the annualised volatility of the window), never split into parts.

The returns are a fixed formula (a small integer generator, no ``random``), so the committed JSON files are what the snapshot tests read: regenerate only on purpose.
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from asof import Session
from asof.ledger import Ledger
from asof.privacy import digest
from asof.records import Record
from asof.sources.base import Source
from asof.widgets import compute

OUT = Path(__file__).parent
UTC = timezone.utc

N_ROWS = 504                                     # two years of business days (before the reloaded ones are added)
N_DUPLICATES = 40                                # PLANTED bad rows: one batch of 40 consecutive rows loaded twice. The quality check counts them; the data-issues part is measured from them (many rows, so no single one drives it)
WINDOW = 63                                      # the rolling Sharpe / leak overlay window: one quarter of trading days
GRID_CLOSE = "T23:00:00Z"                        # the time of day each chart point is taken at: after that day's on-time row is out (21:00 plus a few seconds)
GAP_WINDOW = 252                               # the gap breakdown is measured over the last YEAR of rows, so its measured parts rest on tens of rows each, not on one or two
AS_OF = "2022-10-24T00:00:00Z"
D0 = datetime(2021, 1, 4, tzinfo=UTC)            # a Monday
LATE_EVERY = 3                                   # PLANTED look-ahead: every third row is published LATE_DAYS after its event (a slow feed). At AS_OF about 28 such rows have happened but are not out yet:
LATE_DAYS = 120                                  # a run that ignores publication delay uses all of them early. The quality check flags the late rows (late_availability)
LATE_BUMP = 0.0015                               # PLANTED: the late rows are slightly better days (the slow feed carries the informative ones), so the look-ahead run looks better. Small per row; the size comes from the count
CALENDAR = "one return per business day, Mon–Fri"            # what one row is. Rows are ONE PER BUSINESS DAY (weekends skipped), so annualising with 252 periods a year is right; the widgets say so
FIXTURE_SECRET = "fixture-client-secret-not-a-real-key"                      # keys the row ids in quality.json; a made-up value, so the committed ids are reproducible and tie to no client
FIXED_START = datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC)                      # the pinned wall clock: the fixtures never contain the day they were made. After every date in the data, so nothing is "future dated"
HAND_SET = [("costs", "Costs", -0.60, "VERIFIED"), ("decay", "Signal decay", -0.90, "OBSERVED"), ("timing", "Timing", -0.70, "OBSERVED"), ("slippage", "Slippage", -0.80, "OBSERVED")]       # percentage points of annualised return
UNEXPLAINED = -0.30                              # hand-set, like the four above: in this fixture the gap is BUILT from its parts, so they add up to it exactly (real attributions compute the gap first)
PPY = 252                                        # periods per year used to annualise: right because every row is a BUSINESS day (see CALENDAR and build_rows)


def iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_rows() -> list[dict]:
    """The one shared source. One return per BUSINESS day (Mon-Fri) from a small integer generator; every ``LATE_EVERY``-th row is published ``LATE_DAYS`` days late. A few seconds of per-row stagger make every
    ``available_at`` distinct, so the number of distinct rows equals the number of moments something became knowable."""
    x, rows = 20240102, []
    for i in range(N_ROWS):
        x = (x * 1103515245 + 12345) % 2**31
        r = (x / 2**31 - 0.5) * 0.024 + 0.0002
        late = i % LATE_EVERY == 0
        if late:
            r += LATE_BUMP
        r = round(r, 6)
        ev = D0 + timedelta(days=i + 2 * (i // 5))                                 # D0 is a Monday: row i is the i-th business day (Mon-Fri), the two weekend days skipped after every fifth row
        av = ev + timedelta(hours=21, seconds=i) + (timedelta(days=LATE_DAYS) if late else timedelta(0))
        rows.append({"record_id": f"r{i}", "event_time": iso(ev), "available_at": iso(av), "ret": r})
    return with_duplicates(rows)


def with_duplicates(rows: list[dict]) -> list[dict]:
    """PLANTED bad rows: one batch of ``N_DUPLICATES`` consecutive rows is loaded twice (same record id, same values, same ``available_at``), as a re-run ingestion job would do. The quality check counts them as
    ``duplicate_rows`` and a run that uses both copies counts those days twice. The batch is the stretch of that length with the lowest total return among the rows the locked run uses at ``AS_OF`` (and recent
    enough that every copy stays inside the one-year window), so the data-issues part has a visible size and the same sign as the others. A copy is placed right after its original."""
    seen = [r for r in rows if r["available_at"] <= AS_OF][-(GAP_WINDOW - N_DUPLICATES):]
    start = min(range(len(seen) - N_DUPLICATES + 1), key=lambda k: (sum(r["ret"] for r in seen[k:k + N_DUPLICATES]), k))
    chosen = {r["record_id"] for r in seen[start:start + N_DUPLICATES]}
    out: list[dict] = []
    for r in rows:
        out.append(r)
        if r["record_id"] in chosen:
            out.append(dict(r))
    return out


class Returns(Source):
    name = "returns"

    def records(self):
        z = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))               # noqa: E731
        return [Record("returns", r["record_id"], z(r["event_time"]), z(r["available_at"]), {"day": r["event_time"][:10], "ret": r["ret"]}, order=i) for i, r in enumerate(build_rows())]


def pin_ids() -> None:
    """Make every uuid in this process a counter, so event ids, call ids and therefore hashes are the same on every run."""
    import uuid
    n = [0]

    def fake_uuid4():
        n[0] += 1
        return uuid.UUID(int=n[0])
    uuid.uuid4 = fake_uuid4                                                      # type: ignore[assignment]
    import asof.session as _s
    sys.version = "3.13.0 (pinned for the fixtures)"                             # session_start records the Python and AsOf versions, so without this the hashes change with every interpreter or release
    _s.__version__ = "0.0.0-fixture"


def attribution(rows: list[dict], quality: dict, grid: list[str]) -> dict:
    """The gap breakdown, in Sharpe units, with its measured parts read off the same rows and the same grid as the leak overlay (see the module docstring)."""
    # The rows the quality check counts as duplicate_rows. Its report lists only a few example ids (as hashes), so the full set is found the way the check defines it (the same record id, availability and
    # values as an earlier row) and then held against the report: the count must be the report's count and every example must be in the set.
    seen: set[tuple] = set()
    flagged: set[str] = set()
    for r in rows:
        k = (r["record_id"], r["available_at"], r["ret"])
        if k in seen:
            flagged.add(r["record_id"])
        seen.add(k)
    issue = [i for i in quality["issues"] if i["flag"].endswith("duplicate_rows")]
    assert len(issue) == 1 and issue[0]["n"] == len(flagged) > 0 and set(issue[0]["example_record_ids_sha256"]) <= {digest(i) for i in flagged}, "the rows found here must be the rows the quality report counts"
    t = [g for g in grid if g <= AS_OF][-1]                                       # the last moment at or before AS_OF
    m = compute.measure_gap_effects(rows, at=t, window=GAP_WINDOW, flagged_ids=sorted(flagged), periods_per_year=PPY)
    parts = [{"id": i, "name": n, "value": v, "label": lb} for i, n, v, lb in HAND_SET]
    parts += [{"id": "leakage", "name": "Look-ahead leakage", "value": m["leakage_pp"], "label": "OBSERVED"}, {"id": "data_issues", "name": "Data issues", "value": m["data_issues_pp"], "label": "OBSERVED"},
              {"id": "unexplained", "name": "Unexplained", "value": UNEXPLAINED, "label": "UNKNOWN"}]
    gap = float(sum(Decimal(str(p["value"])) for p in parts))                       # the parts in exact decimal arithmetic: the gap is their sum
    vol_pct = round(m["volatility"] * 100, 2)                                     # the volatility as the widget prints it
    sharpe_gap = round(round(gap, 2) / vol_pct, 2)                                # from the two figures AS SHOWN (2 decimals each), so anyone can check the tile by hand: -18.24 / 11.02 = -1.66
    return {"mode": "execution_gap", "unit": "pp", "metric": "annualised return", "as_of": AS_OF, "gap": gap, "components": parts, "reconciles": True,
            "annualisation": {"periods_per_year": PPY, "calendar": CALENDAR},
            "headline": {"sharpe_gap": sharpe_gap, "volatility_pct": vol_pct,
                         "method": f"return gap ÷ annualised volatility of the window used = {gap:.2f} ÷ {vol_pct:.2f}% = {sharpe_gap:.2f}; shown as one number, not split into parts"},
            "row_counts": {"leakage": len(m["leakage_rows"]), "data_issues": len(m["data_issue_rows"]), "both": len(set(m["leakage_rows"]) & set(m["data_issue_rows"]))},
            "basis": {"leakage": "annualised return of the locked window minus the unlocked window at the point", "data_issues": "annualised return of the locked window minus the same without the repeated copies of the rows the quality report flags as duplicates",
                      "window": GAP_WINDOW, "point": t, "flagged_ids": sorted(flagged), "leakage_rows": m["leakage_rows"], "data_issue_rows": m["data_issue_rows"], "locked_return_pp": m["locked_return_pp"],
                      "unlocked_return_pp": m["unlocked_return_pp"], "clean_return_pp": m["clean_return_pp"]}}


def main() -> None:
    pin_ids()
    ticks = [0]

    def clock() -> datetime:
        ticks[0] += 1
        return FIXED_START + timedelta(seconds=ticks[0])

    import asof.session as _session
    _session._wall = clock                                                       # the data-quality summary stamps "evaluated_at" from this: pin it too

    rows = build_rows()
    with tempfile.TemporaryDirectory() as d:
        s = Session(AS_OF, ledger=Ledger(Path(d) / "ledger.db", now=clock), session_id="fixture-session", model="demo:m")
        s.add_source(Returns())
        s.register_provider("demo", lambda req: __import__("asof").ModelResponse("ok", "demo-1", "stop", {"prompt_tokens": 5, "completion_tokens": 2}))
        s.query("returns")                                                       # every row available at AS_OF; the receipt counts what the clock withheld
        s.model("demo:m", messages=[{"role": "user", "content": "hello"}])
        s.decision("hold", why="fixture")
        receipt = json.loads(json.dumps(s.receipt(last=False)[0].to_dict(), default=str))
        quality = json.loads(json.dumps(s.show("quality-panel", secret=FIXTURE_SECRET).data, default=str))
        raw_quality = json.loads(json.dumps(__import__("asof.widgets.report", fromlist=["_merge"])._merge(s.quality()), default=str))     # the report as quality.py gives it (plain hashes): to find the rows it flags
        chain = json.loads(json.dumps(s.show("chain-status").data, default=str))
        s.close()
    grid = sorted({r["event_time"][:10] + GRID_CLOSE for r in rows})              # ONE POINT PER BUSINESS DAY of the data, at that day's close: the charts' dates are business days, first to last, and nothing else
    timeline = {"as_of": AS_OF, "events": [{"seq": i + 1, "type": "row", "event_time": r["event_time"], "available_at": r["available_at"], "recorded_at": receipt["recorded_at"]} for i, r in enumerate(rows)]}
    write = lambda name, obj: (OUT / f"{name}.json").write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n")      # noqa: E731
    write("receipt", receipt)
    write("timeline", timeline)
    write("quality", quality)
    write("chain", chain)
    write("returns", rows)
    write("rolling-sharpe", compute.rolling_sharpe(rows, window=WINDOW, at=grid, synthetic=True, calendar=CALENDAR))
    write("leak-overlay", compute.leak_overlay(rows, window=WINDOW, at=grid, synthetic=True, calendar=CALENDAR))
    write("attribution", attribution(rows, raw_quality, grid))
    print("wrote receipt, timeline, quality, chain, returns, rolling-sharpe, leak-overlay, attribution")


if __name__ == "__main__":
    sys.exit(main())
