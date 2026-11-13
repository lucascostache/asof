"""Generate the SYNTHETIC data-integrity fixture: 50 fictional tickers (SYN01..SYN50), about two years of daily bars, quarterly EPS, a news-timestamp table, a universe change log and a
consensus-estimates series, with PLANTED data problems documented in manifest.json. Fixed seed, fixed dates, no network, no real data: the files are identical on every run.

    python showcase/fixtures/data_integrity/generate.py

Only the standard library is used, and only + - * / and rng.random() (no exp/log/gauss), so the numbers do not depend on the platform's maths library.
The "day 30" snapshot (day30/) is the same dataset a month later: 30 more days of news that arrive slowly, and a new backfilled series. It exists for the monitoring preview.
"""
from __future__ import annotations

import csv
import json
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

OUT = Path(__file__).parent
SEED = 20240102
UTC = timezone.utc
NY = ZoneInfo("America/New_York")
N_TICKERS = 50
FIRST, LAST = date(2022, 1, 3), date(2023, 12, 29)
SYMBOLS = [f"SYN{i + 1:02d}" for i in range(N_TICKERS)]

DELISTED = [3, 11, 19, 27, 35, 43]                       # indices of the 6 tickers that are delisted mid-period
OPEN_STAMPED = list(range(10))                           # the vendor feed for these 10 tickers stamps each daily bar "available" at the OPEN (the bar-close problem)
N_RESTATED = 12
BACKFILL_AT = datetime(2023, 11, 20, 10, 15, tzinfo=UTC)  # the whole consensus-estimates series was loaded in one go at this time, but is dated 2022-2023
NEWS_ROWS = 3000
NEWS_MISSING = 300                                       # exactly 10% of news rows have no available_at
DAY1_CHECK = datetime(2023, 12, 30, 9, 0, tzinfo=UTC)    # the fixed clock of the "day 1" check run
DAY30_CHECK = datetime(2024, 1, 29, 9, 0, tzinfo=UTC)    # ... and of the "day 30" run
DAY30_RATINGS_ROWS = 3200
DAY30_RATINGS_AT = datetime(2024, 1, 15, 10, 20, tzinfo=UTC)


def z(rng: random.Random) -> float:
    """About N(0,1): the sum of 12 uniforms minus 6. Exactly reproducible (only additions)."""
    return sum(rng.random() for _ in range(12)) - 6.0


def business_days(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ny_stamp(d: date, hh: int, mm: int) -> str:
    return iso(datetime.combine(d, time(hh, mm), tzinfo=NY))


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def main() -> None:
    rng = random.Random(SEED)
    days = business_days(FIRST, LAST)
    idx_of = {d: i for i, d in enumerate(days)}

    # ---- delisting dates: spread over the middle of the period
    delist_day = {t: days[int(len(days) * (0.30 + 0.09 * k))] for k, t in enumerate(DELISTED)}

    # ---- fundamentals: quarterly EPS, published ~5 weeks after the period ends
    periods = [date(2021, 12, 31), date(2022, 3, 31), date(2022, 6, 30), date(2022, 9, 30), date(2022, 12, 31), date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30)]
    base = [round(0.6 + 2.4 * rng.random(), 2) for _ in range(N_TICKERS)]
    eps: dict[tuple[int, int], float] = {}
    surprise: dict[tuple[int, int], float] = {}
    published: dict[tuple[int, int], datetime] = {}
    for t in range(N_TICKERS):
        for q, pe in enumerate(periods):
            zq = z(rng)
            surprise[(t, q)] = zq
            eps[(t, q)] = round(base[t] * (1 + 0.12 * zq), 2)
            lag_days = 30 + int(rng.random() * 11)
            published[(t, q)] = datetime.combine(pe + timedelta(days=lag_days), time(21, 0), tzinfo=UTC)
    fund_rows: list[list] = []
    for (t, q), v in sorted(eps.items()):
        fund_rows.append([SYMBOLS[t], periods[q].isoformat(), v, iso(published[(t, q)]), "original"])
    # restatement: the same (symbol, period) published again, later, with a different EPS
    pool = [(t, q) for t in range(N_TICKERS) for q in range(len(periods) - 1) if t not in DELISTED]
    rng.shuffle(pool)
    restated: list[tuple[int, int]] = sorted(pool[:N_RESTATED])
    restated_info = []
    for (t, q) in restated:
        delta = (0.08 + 0.07 * rng.random()) * (1 if rng.random() < 0.5 else -1)
        new = round(eps[(t, q)] * (1 + delta), 2)
        if new == eps[(t, q)]:
            new = round(new + 0.05, 2)
        at = published[(t, q)] + timedelta(days=90)
        fund_rows.append([SYMBOLS[t], periods[q].isoformat(), new, iso(at), "restated"])
        restated_info.append({"symbol": SYMBOLS[t], "period_end": periods[q].isoformat(), "first_eps": eps[(t, q)], "restated_eps": new, "restated_available_at": iso(at)})
    fund_rows.sort(key=lambda r: (r[0], r[1], r[3]))
    write_csv(OUT / "fundamentals.csv", ["symbol", "period_end", "eps", "published_at", "version"], fund_rows)

    # ---- bars: AR(1) daily returns; the market reacts to EPS on the day it is published (so a strategy that knows EPS early has an edge it never had)
    react: dict[tuple[int, date], float] = {}
    for (t, q), pub in published.items():
        d = pub.date()
        while d.weekday() >= 5 or d not in idx_of:
            d += timedelta(days=1)
            if d > LAST:
                break
        if d <= LAST:
            react[(t, d)] = 0.035 * surprise[(t, q)]
    bar_rows: list[list] = []
    last_bar: dict[int, date] = {}
    for t in range(N_TICKERS):
        px, prev_r = 20 + 180 * rng.random(), 0.0
        sigma = 0.010 + 0.008 * rng.random()
        end = delist_day.get(t, LAST)
        end_i = idx_of[end]
        for d in days:
            i = idx_of[d]
            if i > end_i:
                break
            r = 0.05 * prev_r + sigma * z(rng) / 1.0 + 0.0002
            if t in delist_day and end_i - 25 <= i < end_i - 5:
                r += 0.015                                       # the run-up before the collapse
            elif t in delist_day and i >= end_i - 5:
                r -= 0.12                                        # ... and the collapse before delisting
            r += react.get((t, d), 0.0)
            prev_r = r
            o = px
            c = max(1.0, px * (1 + r))
            hi = max(o, c) * (1 + 0.004 * rng.random())
            lo = min(o, c) * (1 - 0.004 * rng.random())
            px = c
            vendor = ny_stamp(d, 9, 30) if t in OPEN_STAMPED else ny_stamp(d, 16, 20)
            bar_rows.append([SYMBOLS[t], d.isoformat(), round(o, 2), round(hi, 2), round(lo, 2), round(c, 2), int(100000 + 900000 * rng.random()), "true", vendor])
        last_bar[t] = end
    write_csv(OUT / "bars.csv", ["symbol", "open_time", "open", "high", "low", "close", "volume", "complete", "vendor_available_at"], bar_rows)

    # ---- universe: a change log (effective / announced), the way an index provider publishes it
    uni_rows = [[s, "add", "2021-12-15", "2021-12-01"] for s in SYMBOLS]
    for t in DELISTED:
        eff = last_bar[t]
        uni_rows.append([SYMBOLS[t], "remove", eff.isoformat(), (eff - timedelta(days=10)).isoformat()])
    write_csv(OUT / "universe.csv", ["symbol", "action", "effective", "announced"], uni_rows)

    # ---- news: timestamps only (no text). ~2 minutes from event to availability on day 1. Exactly 10% of rows have no available_at.
    def news_rows(n: int, start: int, d0: date, d1: date, lag_lo: int, lag_span: int, missing: int) -> list[list]:
        r2 = random.Random(SEED + start)
        span_days = [d for d in business_days(d0, d1)]
        rows = []
        for k in range(n):
            d = span_days[int(r2.random() * len(span_days))]
            ev = datetime.combine(d, time(8, 0), tzinfo=UTC) + timedelta(seconds=int(r2.random() * 12 * 3600))
            lag = lag_lo + int(r2.random() * lag_span)
            rows.append([f"N{start + k:06d}", SYMBOLS[int(r2.random() * N_TICKERS)], iso(ev), iso(ev + timedelta(seconds=lag))])
        gone = set(r2.sample(range(n), missing)) if missing else set()
        for k in gone:
            rows[k][3] = ""
        rows.sort(key=lambda r: (r[2], r[0]))
        return rows

    news1 = news_rows(NEWS_ROWS, 0, FIRST, LAST, 45, 150, NEWS_MISSING)                     # lag 45..195 s: median about 2 minutes
    write_csv(OUT / "news.csv", ["news_id", "symbol", "event_time", "available_at"], news1)

    # ---- consensus estimates: BACKFILLED. Dated 2022-2023, but every row became available in one hour on 2023-11-20.
    est_rows = []
    for t in range(N_TICKERS):
        for q, pe in enumerate(periods):
            est_rows.append([SYMBOLS[t], pe.isoformat(), round(eps[(t, q)] * (1 + 0.04 * z(rng)), 2), iso(BACKFILL_AT + timedelta(seconds=(t * 8 + q) * 5))])
    write_csv(OUT / "estimates.csv", ["symbol", "period_end", "consensus_eps", "available_at"], est_rows)

    # ---- day 30: the same dataset a month later. 30 more days of news, now arriving ~45 minutes late; and a new series backfilled in one hour.
    new_news = news_rows(120, NEWS_ROWS, date(2023, 12, 30), date(2024, 1, 28), 2400, 600, 0)       # lag 40..50 min: median about 45 minutes
    write_csv(OUT / "day30" / "news.csv", ["news_id", "symbol", "event_time", "available_at"], news1 + new_news)
    rating_rows = []
    d_list = business_days(FIRST, LAST)
    k = 0
    for d in d_list:
        for t in range(N_TICKERS):
            if k >= DAY30_RATINGS_ROWS:
                break
            if (idx_of[d] + t) % 8 == 0:
                rating_rows.append([SYMBOLS[t], d.isoformat(), 1 + (idx_of[d] * 7 + t * 3) % 5, iso(DAY30_RATINGS_AT + timedelta(seconds=k))])
                k += 1
    write_csv(OUT / "day30" / "analyst_ratings.csv", ["symbol", "rating_date", "rating", "available_at"], rating_rows[:DAY30_RATINGS_ROWS])

    # ---- ground truth
    n_open = sum(1 for r in bar_rows if r[0] in {SYMBOLS[t] for t in OPEN_STAMPED})
    manifest = {
        "about": "SYNTHETIC fixture: fictional tickers and made-up numbers. Fixed seed; regenerate with `python showcase/fixtures/data_integrity/generate.py`. Nothing here is real data.",
        "seed": SEED, "period": [FIRST.isoformat(), LAST.isoformat()], "n_tickers": N_TICKERS, "tables": {"bars.csv": len(bar_rows), "fundamentals.csv": len(fund_rows), "news.csv": len(news1), "estimates.csv": len(est_rows),
                                                                                                          "universe.csv": len(uni_rows), "day30/news.csv": len(news1) + len(new_news), "day30/analyst_ratings.csv": len(rating_rows[:DAY30_RATINGS_ROWS])},
        "check_clocks": {"day1": iso(DAY1_CHECK), "day30": iso(DAY30_CHECK)},
        "planted": {
            "backfill": {"table": "estimates.csv", "what": "a consensus-estimates series added later but dated in the past", "rows": len(est_rows), "all_available_at_within": [iso(BACKFILL_AT), iso(BACKFILL_AT + timedelta(seconds=(N_TICKERS * 8 + 8) * 5))],
                         "event_times": [periods[0].isoformat(), periods[-1].isoformat()], "expect_flag": "bulk_backfill", "source": "estimates"},
            "restatement": {"table": "fundamentals.csv", "what": "EPS values revised after first publication (same symbol and period, a second row 90 days later)", "n_restated": len(restated_info), "restated": restated_info,
                            "expect_flag": "restatements", "source": "fundamentals"},
            "survivorship": {"table": "universe.csv + bars.csv", "what": "6 tickers delisted mid-period: their bars stop and they are removed from the universe, so a list of today's survivors leaves them out",
                             "delisted": {SYMBOLS[t]: last_bar[t].isoformat() for t in DELISTED}, "expect": "a naive (survivors-only) universe has 44 names; the as-of universe at the start has 50", "source": "universe"},
            "bar_close": {"table": "bars.csv", "what": "the vendor feed for 10 tickers stamps each daily bar as available at the OPEN (09:30 New York), before the bar has closed; a signal that reads it trades on information that does not exist yet",
                          "tickers": [SYMBOLS[t] for t in OPEN_STAMPED], "rows": n_open, "expect_flag": "availability_moved_to_close", "source": "bars"},
            "timestamp_gaps": {"table": "news.csv", "what": "10% of news rows have no available_at", "rows": NEWS_MISSING, "of": len(news1), "expect_flag": "missing_available_at", "source": "news"},
        },
        "day30_changes": {"slower_feed": {"table": "day30/news.csv", "what": "30 days of new news rows (120) whose lag from event to availability is about 45 minutes instead of about 2", "new_rows": len(new_news)},
                          "new_backfill": {"table": "day30/analyst_ratings.csv", "what": "a new series of 3,200 rows dated across 2022-2023 that all became available within one hour on 2024-01-15", "rows": DAY30_RATINGS_ROWS}},
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print(f"wrote {sum(manifest['tables'].values())} rows in {len(manifest['tables'])} tables to {OUT}")


if __name__ == "__main__":
    main()
