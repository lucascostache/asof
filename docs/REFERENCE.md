# AsOf reference

*This is the full reference: every rule, source, command and caveat, with the test that checks each. For a three-line summary and a five-minute start, read the [README](../README.md). Commands on this page are run from a checkout of the repository.*

**Show an AI agent only what was knowable at time T.** AsOf is a small, dependency-free Python library (plus an optional MCP server) that

* serves data records only if they had become knowable by a chosen time `T` (the *time lock*),
* routes an agent's tools through a proxy that drops anything dated after `T`,
* sends model calls through one gateway and records them,
* writes every query, tool call, model call, decision, block and clock change to a **tamper-evident local ledger** with an anchor against truncation, and
* returns a **receipt** of exactly what each call could and could not see.

It is the open adoption layer, and it is honest about what it cannot do: read [What AsOf does NOT prove](#what-asof-does-not-prove) before relying on it.
Apache-2.0. Python 3.10 to 3.14. No required dependencies. The record and ledger formats are published in [SPEC.md](../SPEC.md).

```bash
pip install -e .                       # from a checkout. Extras: '.[mcp]' (MCP server), '.[parquet]', '.[server]' (TLS roots for sync/SEC)
python examples/quickstart.py
```

## Quickstart 1: Python (5 minutes)

```python
from asof import ModelResponse, Session, sources

s = Session(as_of="2023-01-21T14:00:00Z", ledger_path="my_ledger.db")      # naive datetimes are rejected
s.add_source(sources.CSV("prices.csv", event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))

rows = s.query("prices", symbol="ACME-SYN", lookback="30d")                    # time-locked
print(s.receipt())                                                         # what it could see, what was blocked

s.advance_to("2023-01-24T14:00:00Z")                                       # forward only
s.add_tool("news_search", my_search_fn, timestamp_field="published_at")
news = s.tool("news_search", query="ACME-SYN guidance")                        # future items dropped
reply = s.model("anthropic:<model>", messages=[{"role": "user", "content": "..."}])   # logged
s.decision("hold", reason="no signal")                                     # an agent decision, stamped with the clock time
assert s.verify_ledger().ok                                                # recompute the hash chain, check the tail anchor
```

`examples/quickstart.py` is a complete, runnable version. Its real output (a test checks these lines):

```
closes the agent can see on Jan 21: [148.0, 150.0]
Receipt 0f095e58b822 (query: prices) as of 2023-01-21T14:00:00Z
  served 2 (2 with declared timing, 0 unknown)
  blocked: future=1
  ledger event 7, hash 2c756ed64250b278...

closes the agent can see on Jan 24: [148.0, 150.0, 155.0]

news: ['ACME-SYN raises guidance']
Receipt 450d44cf9259 (model: demo:model) as of 2023-01-24T14:00:00Z
  model demo:model: declared cutoff unknown, contamination score unknown, memory label 'unknown'
  warning: the model's training cutoff is unknown: it may remember events after T
  ledger event 17, hash afccb3d287113ee7...

ledger verifies: True (17 events)
```

## Quickstart 2: a CSV, Claude and MCP (5 minutes)

AsOf can sit between Claude Desktop (or any MCP client) and your data. The server owns the clock; the model can only read data as of that clock.

**1. Install and create a demo.**

```bash
pip install "strathora-asof[mcp] @ git+<REPOSITORY URL>"     # the package is `strathora-asof`; do not `pip install asof`, that name on PyPI is an unrelated project
strathora init --demo ~/asof-demo
```

This creates the ledger, writes `~/asof-demo/prices.csv` (six ACME-SYN closes, 18 to 25 January 2023) and `~/asof-demo/asof.config.json` (clock fixed at **2023-01-21T14:00:00Z**), and prints the config below with your real paths.

**2. Add AsOf to your MCP client.** For Claude Desktop that is `claude_desktop_config.json` (on macOS under `~/Library/Application Support/Claude/`); other clients have an equivalent. Use the snippet `strathora init` printed; it has this shape:

<!-- claude-desktop-config:start -->
```json
{
  "mcpServers": {
    "asof": {
      "command": "/ABSOLUTE/PATH/TO/python",
      "args": ["-m", "asof", "serve-mcp", "--config", "/ABSOLUTE/PATH/TO/asof-demo/asof.config.json"]
    }
  }
}
```
<!-- claude-desktop-config:end -->

Restart the client. (Without an MCP client you can run the server directly: `strathora serve-mcp --config ~/asof-demo/asof.config.json`, which speaks MCP on stdio.)

**3. Ask.** For example: *"Use the asof tools to query the `prices` source and tell me the latest ACME-SYN close you can see."* You should see the closes **147.0, 148.0 and 150.0** and nothing from 23 January on: those were not knowable at the clock time. Asking the model to advance the clock is refused (`allow_model_clock` is false; the operator owns the clock).

**4. Check the evidence.**

```bash
strathora verify                # recompute the hash chain and check the tail anchor: "OK: N events, hash chain intact, tail anchor: ok"
strathora head                  # print {seq, hash, recorded_at}: store it somewhere you trust (see "What AsOf does NOT prove")
strathora receipt <receipt_id>  # the full receipt (with blocked counts) for the id the query returned
```

The MCP tools are `set_clock`, `advance_clock`, `query`, `tool_call`, `model_call` (off unless `enable_model_call`), `get_receipt`, `verify_ledger` and `ledger_head`. Everything goes through the same Session as the
Python API, so R1 to R8 are enforced by the same code. The model's view of a receipt omits the blocked counts (they would reveal that something exists after T) and the call's position in the ledger; for the same reason `verify_ledger` and `ledger_head` tell the model
whether the ledger is intact and the hash of its newest event, but not how many events it holds (a call that withheld something writes one more event). With `receipt_detail: "full"` all three give the operator's view. Config file keys (unknown keys are errors):
`as_of`, `max_as_of` (a ceiling the clock can never pass), `allow_model_clock`, `privacy`, `ledger_path`, `allow_untimestamped`, `receipt_detail` (`agent` or `full`), `enable_model_call`, `allowed_models`, `max_rows`, `max_listed_blocked` (how many withheld records a `block` event names, default 1,000; the count is always exact: `docs/BASELINE.md`), `sources`,
`tools` and `providers` (the last two name Python functions as `"package.module:function"`: the config is as trusted as the code it names). The model cannot set `api_key`, `base_url` or `timeout` on `model_call`,
and it can call only the providers in `providers` and the models in `allowed_models` (exact ids such as `"anthropic:<model>"`, or `"local:*"` for a whole provider): no built-in provider is reachable unless it is listed.
A refused attempt is a `model_error` event whose `gateway.rule` is `operator_parameter` or `model_not_allowed`.

## Demo (3 minutes, synthetic data)

```bash
python demo/run_demo.py                 # the whole story, offline; the model is a scripted stand-in
python demo/run_demo.py --no-network    # the same, with a hard guarantee: no model call, no network of any kind (safe to run live)
python demo/run_demo.py --pause         # wait for Enter between steps, to talk over it
```

**Everything in it is synthetic**: a fictional company ("Acme Synthetic Corp", ticker `ACME-SYN`), invented prices and headlines labelled `[SYNTHETIC]`, and a scripted stand-in instead of a model. It tells one story in six steps:
(1) a session whose clock is the evening before a (fictional) earnings day; (2) the agent asks for prices and news, and the earnings article, dated the next day, exists in the source; (3) the receipt says what was served and that
**1 item was blocked**, plus the model-memory label (`unknown`: no cutoff or contamination score is registered, and the demo invents none); (4) the clock moves past the release and the article is served, with nothing
blocked; (5) a copy of the ledger is edited with `sqlite3` (one value, one event) and `strathora verify` **fails at exactly that event**; (6) optionally, the session is synced to a local Strathora, where it opens as a run with its
receipts. Step 6 runs only if `ASOF_SYNC_API_KEY` is in your environment (create a key under Strathora's Settings, Connections, AsOf clients); the key is read from the environment and never stored. Without it, or with
`--no-network`, the step is skipped and says so. The demo keeps its own ledger (`~/.asof-demo/ledger.db`, or `--ledger`) so running it again continues one chain and the same key keeps working.

Expected output of `python demo/run_demo.py --no-network` on a fresh ledger (session ids and hashes differ on every run, and later runs continue the same ledger, so event numbers shift):

<!-- demo-output:start -->
```text
==============================================================================
AsOf demo.  EVERYTHING BELOW IS SYNTHETIC: a fictional company, invented prices and headlines.
No real data, no real model, no real customer. The point is the mechanism, not the numbers.
==============================================================================

------------------------------------------------------------------------------
STEP 1/6  Start a time-locked session: as of 2023-02-01T23:00:00Z (the evening before earnings day)
------------------------------------------------------------------------------
  session <session-id>, privacy hashes_only (the ledger keeps hashes and counts, not the data)
  the clock only moves forward. Fictional earnings: published 2023-02-02T21:05:00Z, i.e. AFTER this session's clock.

------------------------------------------------------------------------------
STEP 2/6  The agent asks for prices and for news
------------------------------------------------------------------------------
  prices the agent sees (5): 01-26 101.2, 01-27 102.05, 01-30 100.8, 01-31 101.6, 02-01 102.4
  news the agent sees (3):
    2023-01-24T14:00Z  [SYNTHETIC] Acme Synthetic Corp opens a new regional office
    2023-01-30T13:30Z  [SYNTHETIC] Analysts expect a steady quarter from Acme Synthetic
    2023-01-31T15:00Z  [SYNTHETIC] Acme Synthetic will report fourth-quarter results on Thursday
  The earnings article exists in the news source. The agent never receives it.

------------------------------------------------------------------------------
STEP 3/6  The receipt: what was served, what was blocked (for the operator, not the agent)
------------------------------------------------------------------------------
  prices query
    served   5  (5 with declared timing, 0 unknown)
    blocked  1  (future=1)
    ledger   event 11, hash <hash>...
  news tool call
    served   3  (3 with declared timing, 0 unknown)
    blocked  1  (future=1)
    ledger   event 16, hash <hash>...

  -> 1 item dated after the clock was BLOCKED, and the block is a ledger event.
  What the agent may be shown (blocked counts left out: even a count says 'something exists later'):
    {"target": "news_search", "served": 3, "served_timing": {"declared": 3, "unknown": 0}}

  model: standin:scripted  (a scripted stand-in: no model is called)
    model memory label: unknown   declared cutoff: unknown   contamination score: none registered
    AsOf does not control what a model remembers. 'unknown' stays unknown until someone measures it; it is never upgraded to 'clean'.

------------------------------------------------------------------------------
STEP 4/6  Move the clock past the earnings release (2023-02-03T00:00:00Z): the article is now served
------------------------------------------------------------------------------
  news tool call, after the clock moved
    served   4  (4 with declared timing, 0 unknown)
    blocked  0
    ledger   event 24, hash <hash>...
  the agent now sees 4 headlines, including: [SYNTHETIC] Acme Synthetic beats fourth-quarter estimates and raises its outlook

  ledger: OK: 25 events, hash chain intact, tail anchor ok  (<ledger>)

------------------------------------------------------------------------------
STEP 5/6  Tamper with a copy of the ledger, then verify it
------------------------------------------------------------------------------
  We edit one value of one event in the copy: event 12 is the block that recorded the hidden article. We change its count from 1 to 0, as if nothing was blocked.
    $ sqlite3 tampered_ledger.db
    sqlite> DROP TRIGGER events_no_update;     -- the ledger refuses UPDATE; whoever has the file can remove that guard...
    sqlite> UPDATE events SET payload = replace(payload, '"count":1', '"count":0') WHERE seq = 12;
  ...which is why the hash chain exists. Now:
    $ strathora verify --ledger tampered_ledger.db
    FAIL: payload does not match its recorded hash (the payload was altered) (at event 12)
    (exit code 1)
  -> FAIL, at exactly the event we changed (#12). The untouched ledger still says OK.

------------------------------------------------------------------------------
STEP 6/6  Sync to Strathora (opt-in) and open the run and its receipt there
------------------------------------------------------------------------------
  skipped: --no-network. Nothing left this process.

------------------------------------------------------------------------------
DONE  session <session-id>.  All data was synthetic.  Files: <out>
What you saw: a lock that withholds what was not yet public, a receipt that says what it withheld, a clock that only moves forward, and a ledger that shows an edit.
What it does not prove: that the source's timestamps are true, that a model forgot anything, or that nothing was left unsent. See docs/pilot/LIMITATIONS.md.
```
<!-- demo-output:end -->

`tests/test_demo.py` runs the demo on every test run: offline, with every network call made to raise, checking these counts, the failure at the edited event, that keys in the environment are ignored, and that a second run
continues the ledger. This block is compared with the demo's real output.

## The rules, and where each is enforced and tested

| | Rule | Code | Tests |
|---|---|---|---|
| R1 | A record is served only if `available_at <= T`. **The boundary is inclusive**: equal is served, one microsecond later is not. | `query.py` | `test_query.py` |
| R2 | A record with no usable `available_at` is **not served** unless the session sets `allow_untimestamped=True`; then it is labelled `unknown`. A zone-less timestamp counts as unusable: it is never assumed to be UTC. | `query.py`, `sources/base.py` | `test_query.py`, `test_sources.py` |
| R3 | Restatements: of several versions sharing a `record_id`, the **latest version available at T** is served, never a later correction. Filters apply *after* choosing the version. | `query.py` | `test_query.py` (incl. a 25-seed property test) |
| R4 | Every tool result must carry a timestamp. Dated after T: dropped and logged (`blocked_future`). Undated: handled as R2. | `tools.py` | `test_tools.py` |
| R5 | The clock only moves forward (and cannot pass an optional ceiling). Rewinding raises and is logged; every change is logged. | `clock.py`, `session.py` | `test_clock.py`, `test_clocks.py` |
| R6 | Every query, tool call, model call, decision, block and clock change is a ledger event in an append-only hash chain. | `ledger.py`, `session.py` | `test_ledger.py`, `test_clocks.py` |
| R7 | API keys never reach the ledger, a receipt, a log or a sync payload. In `hashes_only` mode raw payloads, prompts, responses, system prompts and all model parameter values are hashes. | `redaction.py`, `privacy.py`, `ledger.py`, `sync.py` | `test_privacy.py`, `test_sync.py`, `test_campaign_secrets.py` |
| R8 | A model's memory is never claimed clean. Receipts show the declared cutoff **and who declared it** (`user` / `provider`) or `unknown`; the label is `unknown` or at most `observed`. | `contam.py` | `test_contam.py` |

## The ledger: three clocks, a hash chain, a tail anchor

Every event has three times with exact meanings (full detail and the hash recipe in [SPEC.md](../SPEC.md)):

| | `event_time` | `available_at` | `recorded_at` |
|---|---|---|---|
| meaning | when the underlying thing happened | when it became knowable (never wall-clock) | wall-clock UTC when the ledger wrote it; **set only by the ledger** |
| a served record | the record's own `event_time` | the record's own `available_at` | wall clock |
| an agent action (query, tool call, model call, decision, block, clock change) | the session clock T | T | wall clock |

Each event stores the hash of the one before it (and its own position), so changing, removing, reordering or splicing events is detected by `verify_ledger()` / `strathora verify`, which names the first bad event.
After every write the ledger exports `{seq, hash, recorded_at}` to `head.json` beside it; `verify` **fails** if the ledger is shorter than that anchor, if the anchored event's hash differs, or if the anchor is missing
from a non-empty ledger. Schema v2 is not mixed with older ledgers: an old file is refused (it stays verifiable with the release that wrote it); `strathora init --archive-legacy` moves it aside.
Events are written in atomic batches, safely from many threads and processes (tested with 50 concurrent writes).
Storage: the ledger is a SQLite file. It switches to write-ahead logging (WAL) at its first write, so readers never block the writer, and `secure_delete` is on. **Opening a ledger to read or verify it never changes the file.**
While a ledger is open, committed events may sit in a `-wal` file beside it: a plain `cp` of an open ledger can miss them, so use `Ledger.backup(dest)` (SQLite's online backup) for a copy of a live ledger. `close()` folds the log
back and leaves one self-contained file, which a plain copy preserves. For one chain per session use `Ledger.for_session(directory, session_id)`: each session then has its own chain, its own `chain_id` and its own anchor.
The command is `strathora`. It is also installed under its older name, `asof`: the two run the same program.

## Sources

| Source | Notes |
|---|---|
| `CSV(path, event_time=, available_at=, key=/record_id=, assume_tz=)` | Plain integers and decimals become numbers; everything else (including `007`) stays text. Without `key`/`record_id`, ids are row numbers and restatements cannot be detected. `version=` and `recorded_at=` name columns holding the row's own version number and when it was recorded: they settle an exact tie on `available_at` (highest version, then latest `recorded_at`); without them a tie is settled by the payload hash. `JSONL`, `Parquet` and `SQL` take the same two options. |
| `JSONL`, `Parquet` | A malformed JSONL line stops the load with its line number. Parquet needs `pyarrow` (the `[parquet]` extra: `strathora-asof[parquet]`). |
| `SQL(db, "SELECT ...", name=, ...)` | A single `SELECT`/`WITH` only; SQLite files open read-only. The statement guard prevents accidents; it is not a security boundary, so also use read-only credentials. Return **every** version of a restated row. |
| `MacroCalendar(path)` | One row per release: `series`, `period`, `release_time`, plus any columns. A revision is another row with a later `release_time`. |
| `SECFilings(user_agent, ciks=[...])` | Filing **metadata** from EDGAR with `acceptanceDateTime` as `available_at` (below). |
| `Universe(rows or path=, name=, member=, action=, effective=, announced=, assume_tz=)` | Index or watch-list membership as a log of `add` / `remove` changes with an effective date and an announcement date. `session.members("sp500")` is who was a member at T, as knowable then; `session.query("prices", universe=("sp500", "symbol"))` keeps only those (below). |

Timestamps (`sources/base.py`): missing, unparseable or zone-less `available_at` is withheld and flagged; `assume_tz="America/New_York"` opts in to a zone, and an ambiguous or non-existent local time resolves to the
**later** instant. A date-only `available_at` means the **end** of that day. `available_at` before `event_time` is flagged. A source is loaded once per session; its fingerprint is in the ledger.

### Data quality and universes (SPEC 1.2)

Every source is summarised when it loads, and every query's receipt carries a summary of what it served (`session.quality("prices")`, `session.receipt().quality`; SPEC section 4.5). It reports, with the thresholds used:
timestamp coverage (event time, availability, zone-less or unparseable values), suspicious timing (available before the event, dated after the wall clock at load, a long history that became "available" in one hour),
restatements (when the source has record ids; **unknown**, not zero, when it does not), duplicate rows, gaps in the history, and staleness of what was served at T.

```python
print(s.receipt().summary())
#   data quality: timestamps 100% event_time / 100% available_at; newest served record 86400 s before T
#   quality flags: none   source: bulk_backfill
```

* **These are heuristics, not verification.** They look for patterns honest clocks do not produce; a source can stamp every record consistently and still lie. See `docs/pilot/LIMITATIONS.md`.
* The per-source summary spans the **whole** source, including records after T, so it is for the operator and is left out of the agent view. The per-query summary uses only what was visible at T (a test checks that adding
  future records changes none of it).
* A ledger written before this existed reports "not recorded", never "clean".
* **Universe-as-of.** A universe stores changes, not a current list or end dates, so the members at T are what had been *announced and had taken effect* by T. A change reported late never reaches an earlier session;
  a dropped name is still a member before its removal; the receipt of a restricted query says how many records the restriction removed (`universe.excluded`), which is the survivorship effect on that query.
* **Market calendars for gaps.** A source made with `calendar="XNYS"` (the New York Stock Exchange: weekdays in New York, minus its holidays and the one-off closures since 2001) or `calendar="weekdays"`
  is checked for gaps on that calendar: nights, weekends and holidays are not gaps. A step inside one day is a gap when it is longer than 5 times the median step inside a day; a step across days is a gap
  when it skips more trading days than 5 times the median number skipped (so for data that has something every trading day, one empty trading day is a gap). Staleness at T is measured the same way, so a
  Monday-morning query is not stale because of the weekend. A series whose every time is midnight UTC is read as dates. Without a calendar, time runs around the clock and nothing changes. The calendar
  changes nothing that is served. In an MCP config: `"calendar": "XNYS"` on a source.
* **The survivorship flag.** `Universe(..., listed_in="prices")` names a source with a record for every name on every trading day. Every query on the universe then compares its members at T with the names
  that have a record on the latest date visible at T in that source; names trading but not members raise the `survivorship` flag (warning) on the receipt, with their number and, in privacy mode `full`,
  the names. It uses only what was visible at T. A survivors-only list (today's names used for every date) is caught at every T before a delisting; after the last delisting it is correct, and nothing is flagged.

### Bars, evidence links and execution (SPEC 1.2)

* **Bars** (`sources.Bars`): a bar is available when it **closes** (`open + timeframe`, or the session close for a daily bar, or an explicit `close_time`), never when it opens. A vendor stamp earlier than the close is replaced by the close
  and counted (`availability_moved_to_close`). The bar still forming at T is withheld like any future record; rows marked incomplete are dropped and counted, and served only with `allow_partial=True`, labelled `partial_bar` with a warning on the receipt.
  Timeframes are per row, so a daily or weekly bar is locked until its own close even beside hourly bars. `intrabar_ambiguous(high, low, side, stop, target)` says when a stop loss and a take profit both lie inside one bar.
* **Evidence links**: `session.decision("buy")` records, in the decision event itself, the ids of the served records and the calls it rests on (`links`): by default everything since the previous decision (*available to* it, not proof of
  what the agent used), or exactly what you name with `based_on=[receipt, call_id]`. A viewer draws the evidence graph from these ids, never from the order of events.
* **Execution**: `session.order(id, symbol=, side=, size=, intended_price=, expected_price=, expected_source="model"|"backtest", stop_loss=, take_profit=, decision=)`, `session.fill(id, price=, size=, fees=, filled_at=, reference_price=, bar=)`
  and `session.mark(symbol, size=, price=)`. A fill cannot be from the future, before its order, or beyond the order's size; `fees=None` means "not recorded" and is never read as free. In `hashes_only` mode the symbol, prices, sizes and fees
  are hashed; the ids, the links and the intrabar flag stay readable. A receiver (Strathora) attributes the gap between expected and actual execution into costs, decay, timing, slippage and unexplained, which add up to the gap exactly.

### SEC filings

`available_at` is EDGAR's `acceptanceDateTime` converted to UTC, and a filing is **never served before it** (inclusive at the instant). An amendment (`10-K/A`) shares an identity with its original: the original is served
until the amendment is accepted. **How the time is read:** EDGAR's JSON writes `"2023-11-03T09:08:27.000Z"`. By default AsOf drops the `Z` and reads the digits as **New York** time (4 to 5 hours later than UTC: it
**errs late**, so if it is wrong data is withheld, not leaked); `acceptance_tz="UTC"` trusts the `Z`. **The author has not confirmed which is right against live EDGAR.** Run
`python scripts/smoke_edgar.py --write-fixture tests/fixtures/edgar_known_filings.json` with your own User-Agent: it compares the JSON value with the "Accepted" time on EDGAR's filing-index page for real
filings and says which reading agrees. Until real filings are in that fixture, `tests/test_sec.py::test_conversion_against_real_filings...` is **skipped**, and the real-world conversion is **unverified**.
Fair access: requests are spaced at least 0.125 s apart (a value under 0.1 s is refused: the SEC's ceiling is 10 per second), responses are cached under `~/.asof/cache/sec` by the sha256 of the URL with the body's
sha256 checked on every read, and you must supply **your own** User-Agent with **your own** contact (`user_agent=` or `ASOF_SEC_USER_AGENT`); the package contains no address and no default.

## Tools, models, contamination

* **Tools** (`s.add_tool("news_search", fn, timestamp_field="published_at", pass_as_of=True)`): a list of dicts is a list of items; each needs the timestamp field. Anything else (free text, a number) is one undated item.
  With `pass_as_of=True` the proxy sets `as_of` itself, overriding anything the caller (or a model) supplied.
* **Models** (`s.model("anthropic:...")`, `"openai:..."`, `"meta:..."`, `"local:..."` for any OpenAI-compatible server, or `s.register_provider(...)`): standard library only; keys from `api_key=` or `ANTHROPIC_API_KEY` /
  `OPENAI_API_KEY` / `MODEL_API_KEY` (Meta) / `ASOF_LOCAL_API_KEY`, sent to the provider and nowhere else. Another OpenAI-compatible provider is configuration only:
  `gw.register_openai_compatible("acme", ProviderSpec("https://...", "ACME_KEY"))`. **Meta is UNVERIFIED**: its endpoint (`https://api.meta.ai/v1`, overridable with `META_API_BASE_URL`) and model names come from configuration and have
  not been called live by this project; every receipt for it says `provider_verified: false`. Anthropic was run live once, with one model (`docs/LIVE_TEST.md`); OpenAI and the other providers have not been called live with a key, and the tests use a local mock server.
  Receipts still say `provider_verified: false` for every built-in provider.
* **Gateway rules, on every model call** (`ModelGateway(GatewayPolicy(...))`, SPEC 4.8). The call is stamped with the session clock T. **Provider-side tools** (web search, fetch, code execution, remote MCP, file search, or a model that
  searches by itself) are **refused by default**, before anything is sent: they run outside the tool proxy and can bring in information from after T. If an operator allows them (`allow_server_tools=True`) every such call is **FLAGGED**, a provider that
  says it ran one is flagged even if none was requested, and `session.clean` is false for the rest of the run. Models whose name contains `contributor` (they train on what they are sent) are refused unless `allow_contributor_models=True`
  (then flagged); a refused attempt is itself a ledger event. Requests that lean on **provider-side state** (`previous_response_id`, a stored thread, a cached context, `store=True`) are refused: the full conversation is sent every time, so the receipt
  reflects everything the model saw. A pinned version (`pins={"openai:gpt-x": "gpt-x-2025"}`) that the provider does not report is flagged. Tool calls a model asks for are run with `s.run_tool_calls(resp)`, which sends each through the tool proxy.
  In a multi-turn tool loop, append `resp.raw_message` (the provider's assistant turn, unchanged, thinking blocks included) to `messages`, not a turn rebuilt from `resp.text`. **Streaming is not supported:** `stream=True` is refused before
  anything is sent, because the gateway records one whole response per call. **The gateway does not retry:** a rate limit, an overload or a dropped connection is one gateway error (with the provider's "retry after" when it gives one)
  and one ledger event; whether to call again is the caller's decision, and a second call is a second ledger event.
  `s.model_receipt()` returns the flat receipt: run id, clock T, provider, model, version, reasoning effort, settings, prompt and output hashes, tokens (in/out/cached/reasoning), provider tools used, items served and blocked so far, labels,
  a data-quality summary, and the previous and own hash. Detection is by name and shape from what providers document: a provider that runs a tool without saying so cannot be caught here.
* **Decision gate** (`s.gate(Intent(...), asof.gate.Policy(...))`, SPEC 4.9): a pure function from an intent, a policy and the history so far to **APPROVE / HOLD / BLOCK** with every reason. BLOCK: symbol not allowed, size, position or
  notional over a limit, model or version not allowed, a duplicate intent id. HOLD: the kill switch (one flag holds everything), data too old, the rate limit used up, or a size at or over the human-approval threshold. A check that cannot be
  made (a notional limit with no price, a freshness limit with no data time) is **unknown and holds**: it never passes. In a session the gate is judged on the session clock and writes an `intent` event (what, from which model and version,
  resting on which data) and a `gate_decision` event as one call, so **data seen -> model -> intent -> gate -> order -> fill** can be walked by ids; an order may be linked only to an APPROVED intent (`s.order(..., intent=result.call_id)`).
  **It never sends an order**: it records a decision.
* **Agent memory** (`asof.Memory`, an R&D feature kept small): every memory has an `available_at`, the moment it was learned, and a recall at T returns only memories learned at or before T (inclusive). It is an ordinary
  source, so `s.add_source(memory)` and `s.recall("memory", "acme guidance")` are time-locked and logged like any query, the receipt counts the memories withheld, and a note rewritten under the same `key` replaces the old one
  only from when the rewrite was learned. SQLite from the standard library, append-only, keyword ranking (no embeddings, no network). It controls what this store returns; it cannot make a model forget.
* **Secrets** (`asof.redaction`). Every ledger event has credential-shaped strings removed (provider keys, bearer and basic headers, tokens, JWTs, private-key blocks, the password in a URL, `password=...`),
  and values under credential-like keys. A value with no recognisable shape can only be removed by knowing it, so the gateway **registers every key it sends**, the sync client registers its own, and
  `asof.register_secret(value)` registers yours (a tool's API key, a database password): a registered secret is removed wherever it appears whole, and from error text also when a server repeats it
  URL-encoded, in base64, in hex, broken over lines, or "masked" with only the middle replaced. Not recognised: any other transformation of a secret, and a piece shorter than 8 characters that is not next to a mask.
  Over MCP, the text of an exception from your own tool or provider is not passed to the calling model at all (it is told that the call failed; the cleaned message is in the ledger).
  `scripts/secret_search.py --env NAME PATHS...` searches files (and, with `--git`, a repository's whole history) for a secret and every 8- and 10-character piece of it, without printing it.
* **Contamination** (`asof.contam`) ships **empty**: no model names, cutoffs or scores are bundled. Register what you have verified: `contam.register("openai:name", declared_cutoff="...Z", declared_by="provider")`.
  The receipt then shows the cutoff and who declared it, a warning when it is after T, and the label `unknown` (or `observed` if you also registered a score): never `clean`.

## Presets

Plain frozen config objects; each states every rule it sets and refuses what would break them. `Preset.build(as_of, sources=[...], tools=[ToolDef(...)], ledger_path=...)` returns a Session; the only setting you may
choose is `privacy`. Every preset: forward-only clock, `allow_untimestamped` False, every call a ledger event.

**`time_locked_research()`**: Research with SEC filings, your own data, and a proxied news tool, all locked to one time.

* allow_untimestamped is False and cannot be changed: anything without a usable timestamp is withheld.
* The clock only moves forward.
* Sources: an SECFilings source is required; your own sources (CSV, JSONL, Parquet, SQL, MacroCalendar) are allowed alongside it.
* At least one tool (the news tool) is required, and it is reached only through the time-locked proxy: items dated after the clock are dropped.
* privacy is yours to choose ('full' or 'hashes_only'); credentials are never recorded in either mode.

**`backtest_safe_agent()`**: A backtesting agent that can use only your own sources and must record a decision before time moves on.

* allow_untimestamped is False and cannot be changed.
* The clock only moves forward.
* Sources: your own local sources only (CSV, JSONL, Parquet, SQL, MacroCalendar). SECFilings, which uses the network, is refused.
* Tools: none. Nothing outside your sources can reach the agent through this preset.
* decision_gate is on and cannot be changed: after any query, tool or model call the clock refuses to advance until the agent has logged a decision with Session.decision(); every decision is a ledger event stamped with the clock time.
* privacy is yours to choose.

**`earnings_analysis(earnings_date)`**: Analyse a company before its earnings on 2023-11-02T21:00:00Z: SEC filings and the macro calendar only, never at or after that instant.  (shown for `earnings_analysis("2023-11-02T21:00:00Z")`)

* allow_untimestamped is False and cannot be changed.
* The clock only moves forward.
* as_of must be strictly before the earnings date (2023-11-02T21:00:00+00:00), and the clock has a ceiling one microsecond before it: it can never reach it.
* Sources: an SECFilings source and a MacroCalendar source are both required; other sources are allowed.
* Filings and macro releases appear only once their acceptance or release time has passed (inclusive of that instant).
* privacy is yours to choose.

## Command line

`strathora init [--ledger PATH] [--archive-legacy] [--demo DIR]` · `strathora verify [--ledger PATH] [--head HEAD] [--no-anchor] [--json]` (exit 1 on failure) · `strathora head` ·
`strathora receipt <id> [--json] [--agent]` (rebuilt from the ledger; identical to the in-process receipt; `--agent` prints the agent's view as JSON, with no blocked counts) · `strathora report --html FILE [--ledger PATH] [--session ID] [--synthetic] [--title T] [--max-receipts N] [--external]` (one self-contained HTML
file with the widgets inlined, made from the ledger alone) · `strathora serve-mcp --config FILE` · `strathora sync` · `strathora --version`.
Exit codes: 0 done; 1 the check failed (`verify` found a problem, no such receipt, sync refused); 2 the command could not run (the ledger is missing, unreadable or of another version; a wrong argument). `verify`, `head`, `receipt` and `report` open a ledger that cannot be written (read-only media, an archived copy) read-only, and change nothing.

**Widgets** (`asof.widgets`, see `asof/widgets/README.md`): dependency-free web components (`<asof-receipt>`, `<asof-clock-timeline>`, `<asof-quality-panel>`, `<asof-chain-status>`, `<asof-rolling-sharpe>`, `<asof-leak-overlay>`,
`<asof-gap-breakdown>`), `session.show("receipt")` in Jupyter, a plugin API (`widgets.register`), deterministic output and snapshot tests. `python scripts/widgets_demo.py` writes a local demo page from synthetic fixtures.

## Sync (optional, off by default)

`asof.sync` pushes ledger events to an HTTPS endpoint **only if you enable it** (`SyncConfig(enabled=True, ...)` or `ASOF_SYNC_ENABLED=1`, `ASOF_SYNC_ENDPOINT`, `ASOF_SYNC_API_KEY`) **and call `push()`**: a session never
syncs by itself. Every event goes as an envelope sufficient to verify the whole chain; the payload goes only for `hashes_only` sessions (already hashes), or for `full` sessions if you also set `send_payloads=True`.
The key is sent only in the `Authorization` header. The wire format (`asof.sync/2`; `asof.sync/1` from 0.2.0 is still accepted by the receiver) is in [SPEC.md](../SPEC.md). **No deployed Strathora endpoint exists yet.** A receiver exists in the Strathora engine (a separate, private codebase); the client is tested against local mock servers
(http and https with a private CA) and against that receiver running locally in the engine's own tests. **It is not deployed anywhere and has not been used against a real service.** One key can own several ledgers (`chain_label` names this one).

## What AsOf does NOT prove

1. **Closed-model memory.** A model trained on data past T may "know" the future whatever the data layer serves. AsOf cannot make a model forget or measure what it remembers. The strongest label it will ever print is
   `observed`, and only if you registered a contamination score; a declared cutoff is a statement by whoever registered it (the receipt says who), not a measurement.
2. **The quality of user-supplied timestamps.** If a source says a record was available on 1 January when it was published on 1 February, AsOf serves it on 1 January. `available_at` is only as honest as its owner; the
   same goes for tools. AsOf enforces timestamps and records what it enforced; it does not verify them.
3. **Truncation, without a trusted head anchor.** A chain cut off at the end is still self-consistent. `head.json` beside the ledger catches a cut, but anyone who can rewrite the ledger can rewrite that file too.
   **The anchor is only as trustworthy as where it is stored**: copy `strathora head` to git, email or another system, and verify with `strathora verify --head`.

Also: it controls only what flows through it (an agent with the open web or another tool is outside the lock); it does not read article text (a pre-T item that *mentions* later events passes); the ledger is
tamper-evident, not tamper-proof, and unsigned (it shows what was asked and served, not who asked); receipts and blocked counts leak that something exists after T, so keep them away from the agent;
`allow_untimestamped=True` weakens the lock by design; `SECFilings` lists filings and does not fetch or parse their text or numbers.

## Python versions and CI

`requires-python >= 3.10`. The whole suite, plus `ruff` and `mypy`, was run on **3.10.22, 3.11.18, 3.12.16, 3.13.13 and 3.14.6** with `scripts/test_matrix.sh` (the same steps as `.github/workflows/ci.yml`), on macOS only.
`.github/workflows/ci.yml` runs those steps with Python 3.10 to 3.14 on `ubuntu-latest` on every push and pull request, and on `macos-latest` on pushes to `main`, when started by hand, and weekly (macOS minutes cost ten times as much on a private repository; the file says how to restore macOS on every push). A third job runs `scripts/secret_scan.py --allowlist scripts/secret_scan_allowlist.txt` over the whole history on every push and pull request, and fails on any finding or commit identity the allowlist does not name. The first GitHub Actions run, on 2026-11-13, was the first time the suite ran on Linux; it found three things, all fixed: the MCP server lost `session_end` (or printed a traceback) when stopped during start-up, a test read a process list that Linux's `ps` cuts short, and two tests assumed this machine's exact Python patch version or speed. After the fixes all 10 jobs passed. **Windows has not been run.** Python 3.10 passed but reached end of life in October 2026; drop it from `requires-python` and the CI matrix when you no longer need it.

## PIT-Fin pilot harness (does a model remember the future?)

`asof.pitfin` asks each question four ways: **A** as written, **B** disguised (the company becomes "Company A", every date shifts by a fixed per-question number of days), **C** with "use only information that was public before T",
and **D** a control about an event after every tested model's cutoff. It scores each model's **memory gap** (A minus B), **instruction failure** (C answers that are correct anyway, and the drop from A to C),
**future-mention rate** (answers naming a date after T or a fact only the later event reveals) and **skill baseline** (D). Every call goes through the gateway (stamped with T, provider-side search refused, a receipt kept) on one
hash-chained ledger. Questions are a JSON list (`examples/pitfin/sample_questions.json`: five **synthetic** ones; the real set is yours; public sources only: EDGAR, prices, earnings dates). Grading is exact by default.

    python scripts/pitfin_run.py --questions examples/pitfin/sample_questions.json     # offline stand-ins: SYNTHETIC, no network, no key
    python scripts/pitfin_run.py --questions my_50.json --models anthropic:M,openai:M,meta:M --repeats 5 --live     # real calls: billable, keys from the environment

Without `--live` only the four offline stand-ins run (they read the answer key and are **not** language models: they show how each score moves). `scripts/leak_demo.py` shows the web-search leak: the same question
(a) straight to a provider with search on, (b) through the gateway (refused; then answered without search, with its receipt; then, if an operator allows search, the same leak, recorded and flagged). Offline it uses a local mock provider.

## Smoke tests you run with your own keys

`scripts/smoke_anthropic.py` and `scripts/smoke_openai.py` make **one** real call each (`--model` is required: AsOf does not choose a model for you), print the receipt, verify the ledger and check the key is not in
the ledger file. `scripts/smoke_edgar.py` fetches real filings with your User-Agent and prints the raw acceptance times, AsOf's conversions and EDGAR's own "Accepted" time. They are tested here against local mock
servers with dummy keys, and exit non-zero on any failure; **the author has not run them against the live services.**
`scripts/live_test.py --out DIR` goes further, in one session for both providers: one call each, a real provider error with the key in use, a wrong key (providers echo part of a rejected key), and the MCP server's
tool definitions sent as the provider's tools, to see whether they are accepted as they are. It writes `report.json` and checks that no key, or part of one, is in the ledger, the report or its own output.

`scripts/verify_all.sh` runs everything that needs no key (the test matrix, `scripts/tamper_demo.sh`, the offline demo) and then prints the exact commands for the checks that do. `scripts/tamper_demo.sh` alters one byte of a copy
of a ledger, shows `strathora verify` fail at the event it sits in, restores the byte, and proves your ledger was never written. `docs/CLAUDE_DESKTOP.md` is the step-by-step MCP setup with three test prompts (unverified in Claude Desktop itself).

## Not in this package

Full checks, Verumark reports, private holdouts, Proving Ground, Approvals/Warden management and cross-customer intelligence belong to the Strathora platform. There is no Strathora client here;
`contam.set_score_provider(fn)` is the hook for external scores.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
scripts/test_matrix.sh          # every supported Python (uses uv)
```

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
