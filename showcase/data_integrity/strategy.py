"""A deliberately simple SYNTHETIC strategy, run twice on the fixture.

    signal  = rank-sum of 5-day momentum and the latest EPS change; hold the top 8 names, equal weight, for the day (decision at 10:00 New York, P&L = that day's close-to-close return)

    UNLOCKED (naive data)   the vendor's bar timestamps taken at face value (10 tickers stamp each bar at the OPEN, so today's close is visible at 10:00), the latest EPS row by period end (restated values,
                            known as if on the last day of the quarter), and today's survivors as the universe.
    LOCKED                  the same code on data read through a Session whose clock is 10:00: bars only once closed, EPS only as published and not yet restated, the universe as it was at T.

Nothing is tuned: the leak is whatever these three naive reads are worth on this fixture.
"""
from __future__ import annotations

import csv
import math
import tempfile
from collections import defaultdict
from datetime import date, datetime, time, timezone
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any
from zoneinfo import ZoneInfo

from asof import Session
from asof.ledger import Ledger

from .common import FIX, MANIFEST, pinned_clock, sources

UTC, NY = timezone.utc, ZoneInfo("America/New_York")
WARMUP_DAYS, TOP_N, WINDOW = 60, 8, 60

Bars = dict[str, list[tuple[date, float]]]          # symbol -> [(bar date, close)] visible at the decision time, oldest first
Eps = dict[str, list[float]]                        # symbol -> EPS by period, oldest first


def _read(name: str) -> list[dict[str, str]]:
    with open(FIX / name) as f:
        return list(csv.DictReader(f))


def _decision_time(d: date) -> datetime:
    return datetime.combine(d, time(10, 0), tzinfo=NY)


def _score(bars: Bars, eps: Eps, universe: set[str]) -> list[str]:
    """The strategy itself: the same function for both runs. Returns the symbols to hold."""
    mom: dict[str, float] = {}
    chg: dict[str, float] = {}
    for s in universe:
        b = bars.get(s, [])
        if len(b) >= 6 and b[-6][1] > 0:
            mom[s] = b[-1][1] / b[-6][1] - 1
        e = eps.get(s, [])
        if len(e) >= 2 and e[-2] != 0:
            chg[s] = (e[-1] - e[-2]) / abs(e[-2])
    names = sorted(set(mom) & set(chg))
    if len(names) < TOP_N:
        return []
    rank = defaultdict(float)
    for table in (mom, chg):
        for r, s in enumerate(sorted(names, key=lambda x: (table[x], x))):
            rank[s] += r
    return sorted(names, key=lambda s: (-rank[s], s))[:TOP_N]


def _closes() -> tuple[list[date], dict[str, dict[date, float]]]:
    by: dict[str, dict[date, float]] = defaultdict(dict)
    days: set[date] = set()
    for r in _read("bars.csv"):
        d = date.fromisoformat(r["open_time"])
        by[r["symbol"]][d] = float(r["close"])
        days.add(d)
    return sorted(days), by


def _day_return(closes: dict[str, dict[date, float]], days: list[date], i: int, held: list[str]) -> float | None:
    """The realised close-to-close return of the day for the held names (a name with no bar that day contributes nothing). Both runs are paid from the same true prices."""
    rs = []
    for s in held:
        c, p = closes[s].get(days[i]), closes[s].get(days[i - 1])
        if c is not None and p is not None and p > 0:
            rs.append(c / p - 1)
    return fmean(rs) if rs else None


def run_unlocked() -> dict[date, float | None]:
    days, closes = _closes()
    vendor = {(r["symbol"], r["open_time"]): r["vendor_available_at"] for r in _read("bars.csv")}
    survivors = {s for s in closes if days[-1] in closes[s]}
    fund = defaultdict(dict)                                       # symbol -> period_end -> latest EPS row (restated beats original)
    for r in _read("fundamentals.csv"):
        fund[r["symbol"]][r["period_end"]] = float(r["eps"]) if r["version"] == "restated" or r["period_end"] not in fund[r["symbol"]] else fund[r["symbol"]][r["period_end"]]
    pnl: dict[date, float | None] = {}
    for i, d in enumerate(days):
        if i < WARMUP_DAYS:
            continue
        t = _decision_time(d)
        bars: Bars = {}
        for s in survivors:
            vis = [(x, closes[s][x]) for x in days[max(0, i - 12): i + 1] if x in closes[s] and datetime.fromisoformat(vendor[(s, x.isoformat())].replace("Z", "+00:00")) <= t]
            bars[s] = vis
        eps: Eps = {s: [v for p, v in sorted(fund[s].items()) if date.fromisoformat(p) <= d] for s in survivors}      # by period end: as if known on the last day of the quarter
        pnl[d] = _day_return(closes, days, i, _score(bars, eps, survivors))
    return pnl


def run_locked() -> tuple[dict[date, float | None], dict[str, Any]]:
    """The same strategy on a Session. Every read is time-locked; the receipts say what was withheld."""
    days, closes = _closes()
    start = _decision_time(days[WARMUP_DAYS])
    pnl: dict[date, float | None] = {}
    with pinned_clock(start) as clock, tempfile.TemporaryDirectory() as d:
        s = Session(start, ledger=Ledger(Path(d) / "ledger.db", now=clock), session_id="locked-run", privacy="hashes_only")
        for src in sources():
            s.add_source(src)
        for i, day in enumerate(days):
            if i < WARMUP_DAYS:
                continue
            s.advance_to(_decision_time(day)) if s.as_of != _decision_time(day).astimezone(UTC) else None
            recs = s.query("bars", lookback="10d", universe=("universe", "symbol"))
            assert all(r.available_at is not None and r.available_at <= s.as_of for r in recs), "the time lock served a record from the future"
            members = {r.payload["symbol"] for r in recs}
            bars: Bars = defaultdict(list)
            for r in sorted(recs, key=lambda r: r.event_time):
                bars[r.payload["symbol"]].append((r.event_time.astimezone(NY).date(), float(r.payload["close"])))
            eps: Eps = defaultdict(list)
            for r in sorted(s.query("fundamentals", lookback="420d"), key=lambda r: (r.payload["symbol"], r.payload["period_end"])):
                eps[r.payload["symbol"]].append(float(r.payload["eps"]))
            pnl[day] = _day_return(closes, days, i, _score(bars, eps, members))
        blocked = {"query_calls": len([e for e in s.ledger.events(session_id=s.id) if e.type == "query"]), "chain_ok": s.verify_ledger(anchor="skip").ok}
        s.close()
    return pnl, blocked


def metrics(pnl: dict[date, float | None]) -> dict[str, list[tuple[date, float | None]]]:
    """Equity (growth of 1), rolling Sharpe and rolling hit rate (%) over WINDOW days, one point per day."""
    eq, out_eq, out_sh, out_hr, hist = 1.0, [], [], [], []
    for d in sorted(pnl):
        r = pnl[d]
        if r is not None:
            eq *= 1 + r
            hist.append(r)
        out_eq.append((d, round(eq, 4)))
        w = hist[-WINDOW:]
        if len(hist) >= WINDOW and pstdev(w) > 0:
            out_sh.append((d, round(fmean(w) / pstdev(w) * math.sqrt(252), 3)))
        else:
            out_sh.append((d, None))
        out_hr.append((d, round(100 * sum(1 for x in w if x > 0) / len(w), 1) if len(hist) >= WINDOW else None))
    return {"equity": out_eq, "sharpe": out_sh, "hit_rate": out_hr}


def overlay(name: str, title: str, locked: list[tuple[date, float | None]], unlocked: list[tuple[date, float | None]]) -> dict[str, Any]:
    """The leak-overlay widget's data shape, from two real runs (the library's own compute.leak_overlay derives both series from one set of returns; here they are two separate runs)."""
    iso = lambda d: datetime.combine(d, time(21, 0), tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")             # noqa: E731
    lk = [{"t": iso(d), "v": v} for d, v in locked]
    un = [{"t": iso(d), "v": v} for d, v in unlocked]
    gaps = [abs(u["v"] - l["v"]) for l, u in zip(lk, un, strict=True) if l["v"] is not None and u["v"] is not None]
    return {"title": title, "metric": name, "label": "difference attributable to look-ahead (observed)", "locked": lk, "unlocked": un, "synthetic": True,
            "summary": {"n_points": len(lk), "n_compared": len(gaps), "max_abs_gap": round(max(gaps), 6) if gaps else None, "mean_abs_gap": round(fmean(gaps), 6) if gaps else None}}


def run() -> dict[str, Any]:
    un = run_unlocked()
    lo, info = run_locked()
    mu, ml = metrics(un), metrics(lo)
    return {"overlays": {
        "equity": overlay("equity (growth of 1)", "Equity: locked vs unlocked", ml["equity"], mu["equity"]),
        "sharpe": overlay(f"rolling Sharpe ({WINDOW})", "Sharpe: locked vs unlocked", ml["sharpe"], mu["sharpe"]),
        "hit_rate": overlay(f"rolling hit rate % ({WINDOW})", "Hit rate: locked vs unlocked", ml["hit_rate"], mu["hit_rate"])},
        "final": {"locked_equity": ml["equity"][-1][1], "unlocked_equity": mu["equity"][-1][1], "days": len(ml["equity"]), "locked_run": info},
        "rules": {"decision_time": "10:00 America/New_York", "hold": TOP_N, "window": WINDOW, "warmup_days": WARMUP_DAYS, "manifest_seed": MANIFEST["seed"]}}
