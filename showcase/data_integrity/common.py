"""Shared pieces: load the fixture through the REAL AsOf sources, run the REAL checks (a Session loading each source runs asof.quality.analyse_source), with a pinned clock so output is identical every run."""
from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import asof.session as _session
from asof import Session
from asof.ledger import Ledger
from asof.sources import Bars, CSV, Universe

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "data_integrity"
OUT = ROOT / "out"
WEB = ROOT / "web"
UTC = timezone.utc
NY = "America/New_York"
MANIFEST = json.loads((FIX / "manifest.json").read_text())
CLOCKS = {k: datetime.fromisoformat(v.replace("Z", "+00:00")) for k, v in MANIFEST["check_clocks"].items()}


def sources(day: str = "day1", root: Path | None = None) -> list[Any]:
    """The fixture's sources, built exactly as a user would build them from files. ``day30`` swaps in the later news file and adds the new ratings series.
    ``root`` reads the same five files from another folder (the clean control dataset, see control.py); the way each source is declared does not change."""
    FIX = root or globals()["FIX"]
    d30 = FIX / "day30"
    news_path = d30 / "news.csv" if day == "day30" else FIX / "news.csv"
    # Every series is US market data, so every series is declared on the New York Stock Exchange calendar (nights, weekends and its holidays are not gaps). The universe names the
    # prices as its evidence of who was trading, for the survivorship check. Both are declarations about the data, made the same way for the fixture and the clean control.
    cal = "XNYS"
    out: list[Any] = [
        Bars(path=FIX / "bars.csv", name="bars", timeframe="1d", session_close="16:00", session_tz=NY, assume_tz=NY, available_at="vendor_available_at", complete="complete", key=("symbol", "open_time"),
             calendar=cal),
        CSV(FIX / "fundamentals.csv", name="fundamentals", event_time="period_end", available_at="published_at", key=("symbol", "period_end"), assume_tz="UTC", calendar=cal),
        CSV(news_path, name="news", event_time="event_time", available_at="available_at", record_id="news_id", assume_tz="UTC", calendar=cal),
        CSV(FIX / "estimates.csv", name="estimates", event_time="period_end", available_at="available_at", key=("symbol", "period_end"), assume_tz="UTC", calendar=cal),
        Universe(path=FIX / "universe.csv", name="universe", assume_tz="UTC", listed_in="bars"),
    ]
    if day == "day30":
        out.append(CSV(d30 / "analyst_ratings.csv", name="analyst_ratings", event_time="rating_date", available_at="available_at", key=("symbol", "rating_date"), assume_tz="UTC",
                       calendar=cal))
    return out


@contextmanager
def pinned_clock(start: datetime) -> Iterator[Any]:
    """Pin the wall clock (the ledger's recorded_at and the quality summary's evaluated_at) and the random ids, then restore everything. Returns the ledger clock."""
    import uuid
    real_wall, real_uuid = _session._wall, uuid.uuid4
    n, ticks = [0], [0]

    def fake_uuid() -> Any:
        n[0] += 1
        return uuid.UUID(int=n[0])

    def clock() -> datetime:
        ticks[0] += 1
        return start + timedelta(seconds=ticks[0])
    _session._wall, uuid.uuid4 = clock, fake_uuid                          # type: ignore[assignment]
    try:
        yield clock
    finally:
        _session._wall, uuid.uuid4 = real_wall, real_uuid                  # type: ignore[assignment]


def run_checks(day: str = "day1", root: Path | None = None) -> dict[str, Any]:
    """Load every source into a Session whose clock is the check run's time, run each source's quality check, verify the ledger chain. Returns the raw reports (no tuning, no filtering)."""
    when = CLOCKS[day]
    with pinned_clock(when) as clock, tempfile.TemporaryDirectory() as d:
        s = Session(when, ledger=Ledger(Path(d) / "ledger.db", now=clock), session_id=f"data-integrity-{day}")
        for src in sources(day, root):
            s.add_source(src)
        quality = s.quality()
        v = s.verify_ledger(anchor="skip")
        chain = {"ok": v.ok, "status": "intact" if v.ok else "broken", "n_events": v.n_events, "head_hash": v.head_hash, "first_bad_seq": v.first_bad_seq, "reason": v.reason, "anchor": v.anchor}
        s.close()
    return {"check_run_at": when.isoformat().replace("+00:00", "Z"), "sources": quality, "chain": chain}
