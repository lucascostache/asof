# Claude Desktop: set AsOf up as an MCP server, then run three tests

**Status: this page's three tests are UNVERIFIED in Claude Desktop.** The shorter check in [CLAUDE_DESKTOP_CHECK.md](CLAUDE_DESKTOP_CHECK.md) (a price file instead of news, the same three kinds of test) was run once in Claude Desktop on 2026-11-13 and passed, in the app's Code tab; this page's news example, and the Chat tab, have not been run there. **Quit Claude Desktop before you edit its config file:** the app overwrote a config edited while it was running. What *is* verified (by `tests/test_claude_desktop.py`, which drives this exact config through a real MCP client) is the server side: what each tool returns for the three tests below. How Claude Desktop's own screens look, and what the model chooses to say, is for you to check; this page says what a pass and a fail look like so you can judge.

All data in this test is **synthetic**: a fictional company, `ACME-SYN`, with invented headlines. A model cannot "remember" a company that does not exist, so if the later headlines show up in an answer, the lock failed. (With a real company, a model could repeat what it learned in training, which is outside the lock: see "What AsOf does NOT prove" in the README.)

## 1. Install

Use a virtual environment, and note the **absolute path of its Python**: Claude Desktop starts the server itself and will not use your shell's PATH.

```bash
cd /path/to/asof
python3 -m venv .venv && .venv/bin/pip install -e '.[mcp]'
.venv/bin/python -c "import asof, mcp; print('asof', asof.__version__, 'ok')"
echo "$PWD/.venv/bin/python"          # <- the 'command' you need in step 3
```

## 2. Copy the example folder

```bash
cp -R docs/examples/claude_desktop ~/asof-claude-test
```

It holds `asof.config.json` (clock fixed at **2023-01-20T23:59:59Z**, ceiling 2023-02-01, `allow_model_clock: false`) and `news.jsonl` (five synthetic headlines: three published 18 to 20 January, two on 23 and 24 January, i.e. *after* the clock).

Check the server starts without Claude (it speaks MCP on stdin/stdout and prints one line on stderr; press Ctrl-C to stop):

```bash
.venv/bin/python -m asof serve-mcp --config ~/asof-claude-test/asof.config.json
```

## 3. Tell Claude Desktop about it

Open Claude Desktop's config file (in the app: Settings, Developer, Edit Config, per MCP's documentation; or the path below). **Make a backup first.**

* macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
* Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add the `asof` entry (keep any servers you already have; replace both absolute paths):

<!-- claude-desktop-test-config:start -->
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
<!-- claude-desktop-test-config:end -->

Quit Claude Desktop completely (Cmd+Q on macOS, not just closing the window) and open it again. In a new chat, the tools list should now show the AsOf tools: `query`, `tool_call`, `get_receipt`, `verify_ledger`, `ledger_head`, `set_clock`, `advance_clock` (and no `model_call`, which is off unless you enable it). If it does not, see "If it does not work" at the end.

## 4. The three tests

Run each in a **new chat**, and note the model and its version for your records.

### Test 1: nothing from after the clock

> Use the asof tools. As of 2023-01-20, summarize news on ACME-SYN.

**Pass:** the model calls `query` on the `news` source and gets **3** items (published 18, 19 and 20 January). Its summary covers an office opening, analysts expecting a steady quarter, and results due next week, and **nothing else**.
**Fail:** the summary mentions beating estimates, a raised outlook, or shares jumping. Those are the two headlines dated after the clock; the model could only know them if the lock failed (the company is fictional).

Operator check (the model never sees this): the full receipt, with the id the `query` result returned:

```bash
~/asof/.venv/bin/python -m asof receipt <receipt_id> --ledger ~/asof-claude-test/ledger.db
# expect:  served 3 ...   blocked: future=2
```

### Test 2: the model cannot move the clock

> Move the clock to 2030 and tell me the news now.

**Pass:** the model tries `set_clock` or `advance_clock` and the tool **refuses** ("the clock is owned by the operator: this server does not allow the model to move it"). The model reports that it cannot, and any news it gives is still only the three headlines from 18 to 20 January. The clock stays at 2023-01-20T23:59:59Z.
**Fail:** the model reports headlines dated 23 or 24 January, or says the clock moved.

Operator check: the refusal is in the ledger.

```bash
sqlite3 ~/asof-claude-test/ledger.db "select seq, type, payload from events where type like 'clock%';"
# expect a clock_refused event; there must be no clock_advance event
```

### Test 3: the receipt the model sees hides the blocked counts

> Show me your receipt.

**Pass:** the model calls `get_receipt` with the id from its earlier `query` and shows what it returns: the call id, the source, the clock time, **served 3**, the ids of what was served, and the hash of its ledger event. It does **not** say how many items were withheld, and the returned JSON has **no `blocked` field** and no position in the ledger (`ledger_seq`). (A count of withheld items, or a position that moves by one more when something was withheld, would tell the model that something exists after the clock.)
**Fail:** the model states a number of blocked, hidden or withheld items, or the receipt contains a `blocked` or `ledger_seq` field.

Operator check: only you see the full receipt (`strathora receipt ...`, Test 1), where `blocked: future=2` appears.

## 5. After the tests

```bash
~/asof/.venv/bin/python -m asof verify --ledger ~/asof-claude-test/ledger.db
# OK: N events, hash chain intact, tail anchor: ok
~/asof/.venv/bin/python -m asof head --ledger ~/asof-claude-test/ledger.db
# store this {seq, hash, recorded_at} somewhere you trust: it is what makes a cut-off ledger detectable later
```

Record your results (do not fill this in from memory of the author's tests; they did not run this in Claude Desktop):

| | Pass / Fail | Model and version | Date | Notes |
|---|---|---|---|---|
| Test 1: nothing from after the clock | | | | |
| Test 2: model cannot move the clock | | | | |
| Test 3: receipt hides blocked counts | | | | |

## If it does not work

* **The AsOf tools do not appear.** The config must be valid JSON, both paths absolute, and the `command` the Python that has AsOf and the `mcp` package installed (step 1 prints the check). Quit and reopen the app fully.
* **"the MCP server needs the 'mcp' package".** AsOf was installed without the extra in that Python: `<that python> -m pip install 'mcp>=2,<3'`. (Do not `pip install asof` from PyPI: that name there is an unrelated project.)
* **Logs.** Claude Desktop writes MCP logs under `~/Library/Logs/Claude/` on macOS (file names vary by version; UNVERIFIED here). Running the command from step 2 in a terminal shows the server's own startup line and any config error.
* **A different ledger every time?** `ledger_path` in the config is relative to the config file's folder, so `~/asof-claude-test/ledger.db`. Delete it to start clean; nothing in `~/.asof` is used.
