# Baseline: how long things take, and how much memory and disk they use

**One machine, one run, synthetic data. This is a baseline to compare later versions with, not a benchmark:** nothing was repeated to get an average, nothing was tuned, and a different machine will give different numbers. The numbers below are copied from `docs/baseline.json`, which the script wrote (a test checks that they agree).

| | |
|---|---|
| Date | 2026-11-12 |
| Version | strathora-asof 0.5.0 |
| Machine | Apple M2, 8 cores, 16 GB memory, macOS (Darwin 24.5.0, arm64) |
| Python, SQLite | 3.13.13, 3.53.0 |
| Command | `python scripts/bench_baseline.py --work DIR` (each step in its own process, so its peak memory is its own) |

## Results

| Step | What it does | Time | Peak memory | Disk |
|---|---|---|---|---|
| `ledger-build` | appends 1,000,000 events to a new ledger, in batches | 134.13 s (7,456 events a second) | 31.5 MB | 601.3 MB |
| `ledger-verify` | verifies that ledger: the whole hash chain and the anchor | 24.97 s (40,042 events a second) | 28.6 MB | - |
| `query-1m` | one time-locked query over 1,000,000 record versions held in memory (half of them after the clock) | 2.58 s (2.43 s with a filter that serves 100) | 776.7 MB | - |
| `session-1m` | a `Session` loads a 1,000,000-row CSV file (65.4 MB), then answers one query | load 75.71 s, query 5.64 s | 1,627.4 MB | - |
| `advance-10k` | a replay: 10,000 times, move the clock one step and run one query (20,000 record versions in the source) | 515.8 s (51.58 ms a step) | 753.8 MB | 824.1 MB |
| `advance-10k-unlisted` | the same replay with `max_listed_blocked=0` | 409.4 s (40.94 ms a step) | 219.6 MB | 62.8 MB |

An earlier run the same day, while the machine was doing other work, gave times within about 12% of these (for example 138.35 s, 24.53 s, 2.53 s, 76.4 s, 462.3 s and 399.63 s for the six steps), the same disk sizes, and lower peak memory in two steps (`session-1m` 1,387.8 MB, `advance-10k` 503.5 MB). Two runs are not enough to say more than that the numbers are of this size.

## What the numbers say

* **Verifying is cheap and flat in memory.** A million events verify in about 25 seconds in under 30 MB, because the ledger is read one row at a time.
* **Writing is the slow part of the ledger:** about 7,500 events a second, about 600 bytes an event on disk.
* **A `Session` keeps every record of a source in memory.** A million CSV rows took about 1.6 GB and over a minute to load. This is the practical size limit today: the library has no way to query a source without loading it whole. (Reading the rows straight into `pit_query` took about half the memory.)
* **A long replay over a source with much withheld data writes a large ledger.** Every query that withholds records writes a `block` event that names up to 1,000 of them (about 75 bytes each). In the replay above nearly every step withholds thousands of records, so the ledger grew to 824 MB for 79,997 events, about 10 KB an event.

## The option that came out of this: `max_listed_blocked`

`Session(..., max_listed_blocked=N)` (in an MCP config: `"max_listed_blocked": N`) sets how many withheld records a `block` event names. The default is 1,000, as before. The **count** of withheld records is always exact and always recorded; with a lower number the event names fewer of them and says `list_truncated: true`. Nothing that is served changes, and receipts show the same counts (tested in `tests/test_campaign_scale.py`).

In the replay above, `0` made the ledger 13 times smaller (62.8 MB against 824.1 MB), the peak memory about a third, and the run about a fifth faster. What is given up: the ledger then proves **how many** records were withheld at each query, not **which ones**. The default was left as it is; choosing a lower value is the operator's decision.

## Not measured

* Any other machine or operating system (Linux was not run), any other Python version.
* More than one run of each step, so there is no spread.
* Two processes writing to one ledger at this size, a ledger on a network disk, a nearly full disk.
* Sources other than a CSV file and records held in memory (SQL, Parquet, JSONL at this size).
* The MCP server and the model gateway: no load was put on either.

## Run it yourself

```bash
python scripts/bench_baseline.py --work /some/empty/folder               # everything: about 20 minutes and 1.5 GB of disk here
python scripts/bench_baseline.py --work /some/empty/folder --scale 0.01  # the same steps on 1% of the data, a few seconds
python scripts/bench_baseline.py --work /some/empty/folder --only ledger-verify
```

It writes `baseline.json` into the folder and prints each result as it finishes.
