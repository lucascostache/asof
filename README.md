# AsOf

**Status: v0.5 (0.x), early.** It works on the author's machine and in its own tests, and one model provider has been called live; nobody outside has run it yet. Names, file formats and behaviour can still change between versions. Read [What it doesn't do](#what-it-doesnt-do) before relying on it.

<!-- summary:start -->
AsOf shows an AI agent only the data that was knowable at a time `T` you choose, so a backtest or a replay cannot see the future.
Every query, tool call and model call goes into a tamper-evident local ledger, and each one gets a receipt of what was served and what was withheld.
It is a Python library with no dependencies, plus an optional MCP server; it runs on your machine.
<!-- summary:end -->

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install "git+https://github.com/strathora/asof"
```

Python 3.10 or newer (this install and the quickstart below were run on 3.10, 3.11, 3.12, 3.13 and 3.14, on macOS only). The package is called `strathora-asof`; you import it as `asof` and run it as `strathora` (the older command name `asof` still works). It is **not on PyPI**: do not `pip install asof`, that name there is an unrelated project.

## Quickstart (5 minutes)

**1. Create a demo folder.** This writes a ledger, a six-row `prices.csv` and a config file into `asof-demo/`. The prices are invented sample numbers, not market data.

<!-- quickstart-init:start -->
```bash
strathora init --ledger asof-demo/ledger.db --demo asof-demo
```
<!-- quickstart-init:end -->

Each row has two times: the date the price is for, and `available_at`, the moment it became knowable. AsOf serves a row only once its `available_at` has passed.

**2. Ask for the data as of a time.** Save this as `try_asof.py` and run `python try_asof.py`:

<!-- quickstart-code:start -->
```python
from asof import Session, sources

s = Session(as_of="2023-01-21T14:00:00Z", ledger_path="asof-demo/ledger.db")      # the clock: 21 January 2023, 14:00 UTC
s.add_source(sources.CSV("asof-demo/prices.csv", event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))

rows = s.query("prices", symbol="ACME-SYN")                    # six closes are in the file; three were knowable at the clock
print("knowable on 21 Jan:", [r.payload["close"] for r in rows])
print(s.receipt().summary())                               # for you, not for the agent: what was served and what was withheld

s.advance_to("2023-01-24T14:00:00Z")                       # the clock only moves forward
print("knowable on 24 Jan:", [r.payload["close"] for r in s.query("prices", symbol="ACME-SYN")])
s.close()
```
<!-- quickstart-code:end -->

You should see this (the receipt id and the hash differ on every run):

<!-- quickstart-output:start -->
```text
knowable on 21 Jan: [147.0, 148.0, 150.0]
Receipt <id> (query: prices) as of 2023-01-21T14:00:00Z
  served 3 (3 with declared timing, 0 unknown)
  blocked: future=3
  data quality: timestamps 100% event_time / 100% available_at; newest served record 136800 s before T
  quality flags: none
  ledger event 8, hash <hash>...
knowable on 24 Jan: [147.0, 148.0, 150.0, 155.0]
```
<!-- quickstart-output:end -->

Three closes were served and three were withheld because they were published after the clock. Moving the clock to 24 January releases one more.

**3. Check the evidence.**

<!-- quickstart-verify:start -->
```bash
strathora verify --ledger asof-demo/ledger.db
```
<!-- quickstart-verify:end -->

It prints `OK: 16 events, hash chain intact, tail anchor: ok`. Edit an event in the ledger file and it prints `FAIL` with the number of that event.

**4. Optional: put it between an agent and your data, with no code.** Install the MCP extra with `pip install "strathora-asof[mcp] @ git+https://github.com/strathora/asof"`, then paste the block that step 1 printed into your MCP client's config. The agent gets a `query` tool that is locked to the clock in `asof-demo/asof.config.json`, and it cannot move that clock. [docs/MCP_CLIENTS.md](docs/MCP_CLIENTS.md) lists the clients this was run with and how they differ.

To wrap your own data, tools and model, follow [docs/pilot/INSTALL.md](docs/pilot/INSTALL.md) (about 15 minutes).

**Long replays: `max_listed_blocked`.** Each query that withholds records writes one `block` event naming up to 1,000 of them (about 75 bytes each), so a long replay over a large source makes a large ledger. `Session(..., max_listed_blocked=N)`, or `"max_listed_blocked": N` in an MCP config, names at most `N`; `0` names none. The count of withheld records is always exact and always recorded, and nothing that is served changes. What a lower number gives up: the ledger then proves how many records were withheld, not which ones. The default stays 1,000.

## What it doesn't do

* **It only locks what goes through it.** If the agent also has web search, another MCP server, a database connection or any other tool of its own, those bypass the time lock completely. So do tools that run at the model provider (web search, code execution): the gateway refuses them by default, and flags the run if you allow them, but it cannot see a provider that uses one without saying so.
* **It cannot make a model forget.** A model trained on data from after `T` may already know what happened, whatever AsOf serves. AsOf does not measure this. A receipt never says a model's memory is clean: the label is `unknown`, or `observed` if you supply a measurement yourself.
* **It trusts your timestamps.** If a source says a record was available on 1 January when it was really published on 1 February, AsOf serves it on 1 January. It enforces `available_at`; it does not check that it is true. It also does not read text: an old article that mentions a later event passes.
* **The ledger shows tampering; it does not prevent it.** Anyone who can rewrite the ledger file can also rewrite the anchor beside it. Store the output of `strathora head` somewhere else if you need to detect a ledger that was cut short. The ledger is not signed: it shows what was asked and served, not who asked.
* **It is not a hosted service.** The MCP server runs as a local process for one user. There is no network transport, no login and no separation between users.
* **It shares its import name with an unrelated package.** You install `strathora-asof` and import `asof`. A different project on PyPI is also called `asof`. If both are installed in the same environment their files can overwrite each other, and `import asof` may load the wrong one. Use a separate virtual environment, and check with `strathora --version`.
* **It has been run against one live service, with one model.** On 2026-11-11 and 2026-11-12 the Anthropic adapter was run against the live API: single calls and their receipts, real provider errors, a seven-turn agent loop that used the MCP server's tools (with an instruction to move the clock planted in the data it was served; the clock did not move), and a provider-side tool allowed on purpose, which flagged the run ([docs/LIVE_TEST.md](docs/LIVE_TEST.md)). **OpenAI: not yet tested** with a key. The SEC filings source is tested against local mock servers only. One MCP client application has been run with a model: on 2026-11-13 the check in [docs/CLAUDE_DESKTOP_CHECK.md](docs/CLAUDE_DESKTOP_CHECK.md) passed in Claude Desktop (version 5.11492.2, its Code tab, Claude Opus 5.5); its Chat tab, and other client applications, have not been tried. Streaming is not supported, and the gateway does not retry. For everything else, the first real use will be the first real test.
* **It loads each source into memory, whole.** A session with a 1,000,000-row CSV file took over a minute to load and about 1.6 GB of memory on one laptop; a replay of 10,000 clock steps wrote an 824 MB ledger until the lists of withheld records were switched off (`max_listed_blocked`, below). The numbers, from one run on one machine, are in [docs/BASELINE.md](docs/BASELINE.md). The test suite runs on Linux and macOS in CI; people have used it by hand only on macOS, and Windows is untested.

A longer list, with what each limit means in practice: [docs/pilot/LIMITATIONS.md](docs/pilot/LIMITATIONS.md).

## More

* [docs/REFERENCE.md](docs/REFERENCE.md): every rule and the test that checks it, sources, tools, models, the decision gate, presets, the command line, widgets, sync.
* [SPEC.md](SPEC.md): the record, ledger and receipt formats.
* `python demo/run_demo.py --no-network` (from a checkout): a three-minute story on synthetic data, including an edited ledger that fails verification.
* `scripts/clean_install_check.sh` (from a checkout): installs from git into a fresh environment and runs this quickstart's checks.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). The core installs nothing else; what each optional extra installs, with its licence, is in [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md).
