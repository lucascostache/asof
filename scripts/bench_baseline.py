#!/usr/bin/env python3
"""Baseline measurements: how long things take and how much memory they use, on this machine, today. Numbers for comparing later versions with, not claims.

    python scripts/bench_baseline.py --work /some/empty/folder            # everything (several minutes, about 1 GB of disk in --work)
    python scripts/bench_baseline.py --work DIR --only ledger-verify      # one measurement (ledger-verify needs ledger-build to have run in the same --work)
    python scripts/bench_baseline.py --work DIR --scale 0.01              # the same steps on 1% of the data (a quick check that the script works)

Each measurement runs in its own process, so its peak memory is its own. Measurements:

  ledger-build     write 1,000,000 events to a new ledger (in atomic batches of 1,000)
  ledger-verify    `Ledger.verify()` on that ledger: every hash recomputed, the anchor checked
  query-1m         the time-locked query over 1,000,000 record versions in memory (half of them after the clock), without a session
  session-1m       a Session over a 1,000,000-row source: loading it (hashing every row, the data-quality summary), then one query that serves 100 rows
  advance-10k      10,000 clock advances in one session, with a query after each (5 rows served each time), every step written to the ledger
  advance-10k-unlisted   the same with Session(max_listed_blocked=0): withheld records are counted but not named one by one (the default names up to 1,000 per query)
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
BASE = datetime(2020, 1, 1, tzinfo=UTC)
STEPS = ("ledger-build", "ledger-verify", "query-1m", "session-1m", "advance-10k", "advance-10k-unlisted")


def peak_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024      # bytes on macOS, kilobytes on Linux


def records(n: int):
    from asof import Record
    out = []
    for i in range(n):
        t = BASE + timedelta(seconds=i)
        out.append(Record("d", f"r{i % (n // 2 or 1)}", t, t, {"i": i, "symbol": "ACME-SYN" if i % 2 else "OTHER-SYN", "close": 100 + (i % 977) / 10}, order=i))      # every id has two versions
    return out


def ledger_build(work: Path, n: int) -> dict:
    from asof.ledger import EventSpec, Ledger
    path = work / "big.ledger.db"
    for f in work.glob("big.ledger*"):
        f.unlink()
    led = Ledger(path)
    T = BASE
    t0 = time.perf_counter()
    for start in range(0, n, 1000):
        led.append_many([EventSpec("bench", "record_served", T, T, {"source": "d", "record_id": f"r{i}", "record_payload": {"i": i, "symbol": "ACME-SYN", "close": 100 + (i % 977) / 10},
                                                                    "timing": "declared", "flags": []}, f"call{start}") for i in range(start, min(start + 1000, n))])
    led.close()
    secs = time.perf_counter() - t0
    return {"events": n, "seconds": round(secs, 2), "events_per_second": round(n / secs), "file_mb": round(path.stat().st_size / 1e6, 1), "peak_memory_mb": round(peak_mb(), 1)}


def ledger_verify(work: Path, n: int) -> dict:
    from asof.ledger import Ledger
    path = work / "big.ledger.db"
    led = Ledger(path)
    t0 = time.perf_counter()
    v = led.verify()
    secs = time.perf_counter() - t0
    led.close()
    assert v.ok and v.anchor == "ok", v.reason
    return {"events": v.n_events, "ok": v.ok, "anchor": v.anchor, "seconds": round(secs, 2), "events_per_second": round(v.n_events / secs), "peak_memory_mb": round(peak_mb(), 1)}


def query_1m(work: Path, n: int) -> dict:
    from asof.query import pit_query
    t0 = time.perf_counter()
    recs = records(n)
    build = time.perf_counter() - t0
    clock = BASE + timedelta(seconds=n // 2)
    t0 = time.perf_counter()
    res = pit_query(recs, clock)
    everything = time.perf_counter() - t0
    t0 = time.perf_counter()
    few = pit_query(recs, clock, filters={"symbol": "ACME-SYN"}, lookback=timedelta(hours=1), limit=100)
    filtered = time.perf_counter() - t0
    assert all(r.available_at <= clock for r in res.records) and res.counts["blocked_future"] == n - n // 2 - 1
    return {"record_versions": n, "build_records_seconds": round(build, 2), "query_seconds": round(everything, 2), "served": res.counts["served"], "blocked_future": res.counts["blocked_future"],
            "filtered_query_seconds": round(filtered, 2), "filtered_served": len(few.records), "peak_memory_mb": round(peak_mb(), 1)}


def session_1m(work: Path, n: int) -> dict:
    from asof import Session, sources
    csv = work / "big.csv"
    t0 = time.perf_counter()
    with open(csv, "w") as f:
        f.write("id,symbol,date,close,available_at\n")
        for i in range(n):
            t = (BASE + timedelta(seconds=i)).isoformat().replace("+00:00", "Z")
            f.write(f"r{i},{'ACME-SYN' if i % 2 else 'OTHER-SYN'},{t},{100 + (i % 977) / 10},{t}\n")
    write = time.perf_counter() - t0
    ledger = work / "session.ledger.db"
    for f in work.glob("session.ledger*"):
        f.unlink()
    with Session(BASE + timedelta(seconds=n // 2), ledger_path=ledger) as s:
        t0 = time.perf_counter()
        s.add_source(sources.CSV(csv, name="d", event_time="date", available_at="available_at", record_id="id"))
        load = time.perf_counter() - t0
        t0 = time.perf_counter()
        rows = s.query("d", symbol="ACME-SYN", limit=100)
        q = time.perf_counter() - t0
        blocked = s.receipt().blocked.get("future", 0)
        assert len(rows) == 100 and all(r.available_at <= s.as_of for r in rows) and s.verify_ledger().ok
    return {"rows": n, "csv_mb": round(csv.stat().st_size / 1e6, 1), "write_csv_seconds": round(write, 2), "load_source_seconds": round(load, 2), "query_seconds": round(q, 2), "served": len(rows),
            "blocked_future": blocked, "peak_memory_mb": round(peak_mb(), 1)}


def advance_10k(work: Path, n: int, **session_options) -> dict:
    from asof import Session
    steps = max(100, n // 100)
    recs = records(steps * 2)                                                    # two versions per id; one new second of data becomes visible per step
    from asof.sources.base import Source

    class Mem(Source):
        name = "d"

        def records(self):
            return iter(recs)
    ledger = work / "advance.ledger.db"
    for f in work.glob("advance.ledger*"):
        f.unlink()
    with Session(BASE, ledger_path=ledger, **session_options) as s:
        s.add_source(Mem())
        t0 = time.perf_counter()
        for i in range(1, steps + 1):
            s.advance_to(BASE + timedelta(seconds=i))
            rows = s.query("d", limit=5)
            assert all(r.available_at <= s.as_of for r in rows)
        secs = time.perf_counter() - t0
        events = len(s.events())
        ok = s.verify_ledger().ok
    assert ok
    return {"steps": steps, "source_record_versions": len(recs), "seconds": round(secs, 2), "milliseconds_per_step": round(1000 * secs / steps, 2), "ledger_events": events,
            "ledger_mb": round(ledger.stat().st_size / 1e6, 1), "peak_memory_mb": round(peak_mb(), 1)}


RUN = {"ledger-build": ledger_build, "ledger-verify": ledger_verify, "query-1m": query_1m, "session-1m": session_1m, "advance-10k": advance_10k,
       "advance-10k-unlisted": lambda work, n: advance_10k(work, n, max_listed_blocked=0)}


def machine() -> dict:
    def sysctl(name: str) -> str:
        try:
            return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=10).stdout.strip()
        except OSError:
            return ""
    mem = sysctl("hw.memsize")
    return {"cpu": sysctl("machdep.cpu.brand_string") or platform.processor(), "cores": os.cpu_count(), "memory_gb": round(int(mem) / 2**30) if mem.isdigit() else None,
            "os": f"{platform.system()} {platform.release()} ({platform.machine()})", "python": sys.version.split()[0], "sqlite": __import__("sqlite3").sqlite_version}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="a folder for the files this writes (created if missing; about 1 GB at full scale)")
    ap.add_argument("--only", choices=STEPS, help="run one measurement, in this process")
    ap.add_argument("--scale", type=float, default=1.0, help="fraction of the full size (default 1.0 = 1,000,000)")
    a = ap.parse_args()
    work = Path(a.work).expanduser()
    work.mkdir(parents=True, exist_ok=True)
    n = max(1000, int(1_000_000 * a.scale))
    if a.only:
        print(json.dumps(RUN[a.only](work, n)))
        return 0
    from asof import __version__
    out = {"asof_version": __version__, "date": datetime.now(UTC).strftime("%Y-%m-%d"), "scale": a.scale, "machine": machine(), "results": {}}
    for step in STEPS:
        r = subprocess.run([sys.executable, __file__, "--work", str(work), "--scale", str(a.scale), "--only", step], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"{step}: FAILED\n{r.stderr[-1500:]}", file=sys.stderr)
            return 1
        out["results"][step] = json.loads(r.stdout.strip().splitlines()[-1])
        print(f"{step}: {json.dumps(out['results'][step])}", flush=True)
    (work / "baseline.json").write_text(json.dumps(out, indent=2) + "\n")
    print(f"\nmachine: {json.dumps(out['machine'])}\nwrote {work / 'baseline.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
