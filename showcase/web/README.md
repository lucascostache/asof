# Data-integrity showcase (SYNTHETIC)

Everything here is made from a **synthetic** fixture: fictional tickers (`SYN01`..`SYN50`), made-up numbers, no real data, no keys, no network. Every picture carries the "synthetic" tag.
The checks are the real AsOf code (`asof.quality`, `asof.sources.Bars`, `Universe`, `Session`), run unchanged.

## Regenerate (one command)

```
python showcase/build.py
```

It regenerates the fixture, runs the checks, runs the strategy twice (about 2 minutes: the locked run makes about 1,400 time-locked queries), and rewrites everything in `showcase/out/` and `showcase/web/`.
The PNGs need `pip install playwright && playwright install chromium`; without it the HTML snippets and JSON are still written. Fixture, JSON and HTML are byte-identical on every run
with the same Python version (fixed seed, fixed clocks). A ledger's first event records the Python version, so the chain's head hash, and the files that show it, differ on another version; the check results do not (compared on 3.11 and 3.13). PNG bytes can differ slightly between machines because fonts and anti-aliasing do.

## The fixture (`showcase/fixtures/data_integrity/`)

50 fictional tickers, 2022-01-03 to 2023-12-29: `bars.csv` (daily bars, 24,522 rows), `fundamentals.csv` (quarterly EPS), `news.csv` (timestamps only, 3,000 rows),
`estimates.csv` (a consensus series), `universe.csv` (membership change log). `day30/` is the same data a month later. `manifest.json` is the ground truth of what was planted.

## What was planted, and what the real checks found (day 1)

| planted issue | found by | detected? | severity | planted | found |
|---|---|---|---|---|---|
| backfill | `quality.py:bulk_backfill` | **DETECTED** | warning | 400 | 400 |
| restatement | `quality.py:restatements` | **DETECTED** | warning | 12 | 12 |
| bar_close | `bars.py + quality.py:availability_moved_to_close` | **DETECTED** | info | 4837 | 4837 |
| timestamp_gaps | `quality.py:missing_available_at` | **DETECTED** | warning | 300 | 300 |
| survivorship | `quality.py:survivorship (a survivors-only universe queried at the start)` | **DETECTED** | warning | 6 | 6 |
| survivorship | `Session.members (universe as of T) vs a survivors-only list` | **MEASURED** | - | 6 | 6 |

- **Survivorship is not flagged by any data-quality check.** It shows only when a survivors-only list is compared with the universe as it was: 50 names then, 44 in a list of today's survivors; the 6 missing are exactly the delisted ones. That is a measurement, not a flag.
- `severity` is `asof.quality.SEVERITY`; `bar_close` is `info` because the library repairs it (the bar is made available at its close) and counts the repair.

Flags the checks raised that were **not** planted (reported, not explained away):

- `news`: `gaps` (warning, n=56, heuristic) is not in the manifest.

Nothing was tuned to the fixture. A planted issue that no check flags is shown as NOT DETECTED and left that way.

## Clean control: what the checks say when nothing is planted

The same tables with every planted problem taken out (`python -m showcase.data_integrity.control`; how each one is removed is at the top of `data_integrity/control.py`), the same source declarations, the same checks and default thresholds.

| source | rows | flags |
|---|---|---|
| `bars` | 22880 | none |
| `estimates` | 352 | none |
| `fundamentals` | 352 | none |
| `news` | 3000 | `gaps` |
| `universe` | 44 | none |

Planted-issue flags raised on the control: **0**. False positives: **1**.

- `news`: `gaps` (warning, n=56 of 3000 rows, heuristic).

The `gaps` flag on `news` is a false positive. The source is declared on the New York Stock Exchange calendar, so nights, weekends and exchange holidays are not counted (before the calendar existed this flag counted 530 gaps, 516 of them nights and weekends). What is left: 53 quiet stretches inside one day, each longer than five times the median step within a day (4318 s, so longer than 6.0 hours), and 3 trading day(s) with no story at all. The news table holds about six stories a day at random times, so both happen by chance; nothing was removed from it. The rule was not changed to hide this: on a sparse feed, read the count, not the flag.

The survivorship check on the control's universe at 2022-01-04T14:00:00Z (44 names; the prices show 44 trading): no flag.

This is one control dataset made from one fixture: it shows that these checks do not fire on this clean data, not a false-positive rate for data in general.

## Leak overlay: the same strategy on naive and on locked data

Strategy (synthetic, deliberately simple): rank-sum of 5-day momentum and the latest EPS change, hold the top 8 names equal-weight for the day, decision at 10:00 New York.
**Unlocked** reads the vendor's timestamps at face value (10 tickers stamp each bar at the open), the latest EPS by period end (restated values, as if known at quarter end), and today's survivors. **Locked** runs the same code on a time-locked `Session`.

| | locked (as of) | unlocked (naive) |
|---|---|---|
| final equity (growth of 1) | 1.01 | 2.1405 |

The shaded area is labelled "difference attributable to look-ahead (observed)": it is a measured difference between two runs, not proof of which of the three reads caused how much of it.

## Monitoring preview

**Simulated: two manual check runs, not live monitoring.** Monitoring is not built. Two manual runs of the same checks, on day 1 and on day 30 (30 more days of news that arrive slowly, plus a new backfilled series), and the difference:

- warning: new source analyst_ratings: bulk backfill (3,200 rows)
- warning: news: feed delay 2 min → 46 min (120 new rows)
- ok: chain verified · 13 events

## Files

`web/data-quality.*`, `web/leak-overlay.*` (equity), `web/leak-overlay-sharpe.*`, `web/leak-overlay-hitrate.*`, `web/monitoring-preview.*` (`.html` = standalone, widgets inlined; `.png` = 2x, dark, compact).
`out/data_integrity_report.json` is the raw output of the checks (day 1 and day 30); `out/detection.{json,txt}` the table above; `out/strategy_summary.json` and `out/leak_overlay_*.json` the strategy runs.
