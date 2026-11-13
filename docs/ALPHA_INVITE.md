# AsOf alpha: getting started

Thank you for trying AsOf. This page gets you to a working setup in about 20 minutes, and says how to report what you find.

**What you are testing.** AsOf shows an AI agent only the data that was knowable at a time `T` you choose, so a backtest or a replay cannot see the future. Every query, tool call and model call goes into a tamper-evident ledger on your machine, and each one gets a receipt. It is v0.5 and early: names, file formats and behaviour can still change. Read [What it doesn't do](../README.md#what-it-doesnt-do) before you rely on it.

**What you need.** Python 3.10 or newer and git. Claude Desktop is optional and only used in step 3.

## 1. Install

Clone the repository (step 3 uses an example folder from it) and install it with the MCP extra:

```bash
git clone https://github.com/strathora/asof
cd asof
python3 -m venv .venv
.venv/bin/pip install -e '.[mcp]'
.venv/bin/strathora --version          # prints: strathora-asof 0.5.0
```

The package is called `strathora-asof`; you import it as `asof` and run it as `strathora`. It is **not on PyPI**: do not run `pip install asof`, which installs an unrelated project with the same name.

## 2. One example: ask for data as of a time

Inside the `asof` folder, create the demo files. This writes a ledger, a six-row `prices.csv` of invented prices for a fictional company, `ACME-SYN`, and a config file:

```bash
.venv/bin/strathora init --ledger asof-demo/ledger.db --demo asof-demo
```

Save this as `try_asof.py` and run `.venv/bin/python try_asof.py`:

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

You should see `knowable on 21 Jan: [147.0, 148.0, 150.0]`, then a receipt that says `served 3` and `blocked: future=3`, then `knowable on 24 Jan: [147.0, 148.0, 150.0, 155.0]`. Three prices were published after the clock, so they were withheld; moving the clock to 24 January releases one more.

Then check the ledger:

```bash
.venv/bin/strathora verify --ledger asof-demo/ledger.db
```

It prints `OK: ... events, hash chain intact, tail anchor: ok`.

## 3. Claude Desktop setup (optional)

This puts AsOf between Claude and a folder of synthetic news, with the clock fixed at 20 January 2023. Claude gets a `query` tool that can only see what was published by then, and it cannot move the clock.

**Checked once in Claude Desktop, in a slightly different form.** On 2026-11-13 the shorter check in [CLAUDE_DESKTOP_CHECK.md](CLAUDE_DESKTOP_CHECK.md) (a price file instead of this news example) passed in Claude Desktop's Code tab with Claude Opus 5.5. This news example, and the Chat tab, have not been run in the app yet. Report what you see either way.

1. Copy the example folder: `cp -R docs/examples/claude_desktop ~/asof-claude-test`
2. Get the absolute path of your Python: run `echo "$PWD/.venv/bin/python"` inside the `asof` folder.
3. **Quit Claude Desktop** (Cmd+Q on macOS): it can overwrite a config file edited while it runs. Open its config file; on macOS it is `~/Library/Application Support/Claude/claude_desktop_config.json`. **Back it up first.** Add this entry, keep any servers you already have, and replace both absolute paths:

   ```json
   {
     "mcpServers": {
       "asof": {
         "command": "/ABSOLUTE/PATH/TO/asof/.venv/bin/python",
         "args": ["-m", "asof", "serve-mcp", "--config", "/ABSOLUTE/PATH/TO/asof-claude-test/asof.config.json"]
       }
     }
   }
   ```

4. Open Claude Desktop again. In a new chat, ask: *"Use the asof tools. As of 2023-01-20, summarize news on ACME-SYN."*

**Pass:** the summary covers only three headlines: an office opening, analysts expecting a steady quarter, and results due next week. **Fail:** it mentions beating estimates, a raised outlook or shares jumping. Those headlines were published after the clock, and the company is fictional, so a model could only know them if the lock failed.

[CLAUDE_DESKTOP.md](CLAUDE_DESKTOP.md) has two more tests (the model tries to move the clock; the receipt the model sees), how to check each result yourself, and what to do if the tools don't appear.

## 4. Report what you find

Open an issue in this repository: on GitHub, use the **Issues** tab and then **New issue**. With the command-line tool, run `gh issue create --repo strathora/asof`. **Issues are public:** anyone can read them.

A useful issue has:

* **What you did:** the command, the code or the prompt, and the config if it matters.
* **What you expected, and what happened instead.** Paste the full error output, not a summary.
* **Versions:** the output of `.venv/bin/strathora --version` and `python3 --version`, your operating system, and for MCP problems the client and its version, and the model.
* **The ledger check:** the output of `strathora verify --ledger <your ledger>`, if a ledger is involved.

Reports that something worked are useful too, especially in Claude Desktop's Chat tab (only its Code tab has been tried), on Linux (the test suite runs there in CI, but nobody has used AsOf there by hand) and on Windows (never run).

**Don't paste these into an issue:**

* An API key, or a config or environment file that contains one.
* Your own data, or a ledger built from it. A ledger stores what was served.

Ledgers built from the demo or the Claude Desktop example hold only invented data and are fine to attach.
