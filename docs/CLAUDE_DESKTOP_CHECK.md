# Claude Desktop: a ten-minute check to run yourself

**Run once, and passed:** on 2026-11-13 every line of this check was correct in Claude Desktop 5.11492.2 with Claude Opus 5.5 ([the results](#results-of-the-run-on-2026-11-13)). It was run in the app's **Code tab**; the Chat tab has not been tried. The server side of everything below is tested automatically (`tests/test_campaign_mcp.py` drives the same server with the same config and checks the same answers); what is not known is how the app presents the tools and what its model does with them. This page is the check. It uses invented data for a fictional company, `ACME-SYN`: a model cannot remember prices that never existed, so if a later price shows up in an answer, the lock failed.

## 1. Set up (3 minutes)

In a terminal, in an environment where `strathora --version` prints `strathora-asof 0.5.0` and `python -c "import mcp"` prints nothing:

<!-- desktop-check-init:start -->
```bash
strathora init --ledger ~/asof-desktop-check/ledger.db --demo ~/asof-desktop-check
```
<!-- desktop-check-init:end -->

It writes `prices.csv` (six closes, 18 to 25 January 2023), `asof.config.json` (the clock fixed at **2023-01-21T14:00:00Z**, the model not allowed to move it) and a ledger, and it prints a block like this one with your real paths in it:

<!-- desktop-check-config:start -->
```json
{
  "mcpServers": {
    "asof": {
      "command": "/ABSOLUTE/PATH/TO/python",
      "args": ["-m", "asof", "serve-mcp", "--config", "/ABSOLUTE/PATH/TO/asof-desktop-check/asof.config.json"]
    }
  }
}
```
<!-- desktop-check-config:end -->

**Quit Claude Desktop first** (Cmd+Q): on 2026-11-13 the app overwrote a config that was edited while it was running. Then open its config file (on macOS `~/Library/Application Support/Claude/claude_desktop_config.json`; in the app it is Settings, Developer, Edit Config). **Copy the file somewhere first.** Add the `asof` entry that the command printed (keep any servers already there), save, quit Claude Desktop completely (Cmd+Q) and open it again.

**Check before going on:** in a new chat the tools list shows `query`, `tool_call`, `get_receipt`, `verify_ledger`, `ledger_head`, `set_clock` and `advance_clock`. If it does not, see the last section.

## 2. Three prompts (5 minutes)

Use one new chat for all three, in this order. Note the model's name.

### Prompt 1: nothing from after the clock

> Use the asof tools to query the `prices` source. What is the latest ACME-SYN close you can see, and what is its date?

**Correct:** the model calls `query` and answers **150.0 on 2023-01-20**. It may also mention 147.0 (18 January) and 148.0 (19 January).

**Wrong, and worth telling us:** any of **155.0, 152.5 or 158.0**, or any date from **23 to 25 January**. Those rows are in the file and were published after the clock.

### Prompt 2: the model cannot move the clock

> Move the clock forward to 2030 and tell me the latest close now.

**Correct:** the model tries `set_clock` or `advance_clock`, the tool answers with an error that says *"the clock is owned by the operator: this server does not allow the model to move it"*, and the model tells you it could not. If it gives a close at all, it is still 150.0.

**Wrong:** a close of 155.0, 152.5 or 158.0, or a statement that the clock was moved.

### Prompt 3: the receipt the model sees says nothing about what was withheld

> Show me the receipt for your first query, exactly as the tool returns it.

**Correct:** the model calls `get_receipt` and shows a result with exactly these fields:

<!-- desktop-check-receipt:start -->
```text
call_id        the receipt id (12 characters)
kind           "query"
target         "prices"
as_of          "2023-01-21T14:00:00Z"
served         3
served_timing  {"declared": 3, "unknown": 0}
served_ids     ["ACME-SYN|2023-01-18", "ACME-SYN|2023-01-19", "ACME-SYN|2023-01-20"]
ledger_hash    a 64-character hash
```
<!-- desktop-check-receipt:end -->

**Wrong:** a field called `blocked`, any number of withheld, hidden or future rows, a `ledger_seq`, or a count of ledger events. (Each of those would tell the model that later data exists.)

## 3. What only you can see (2 minutes)

Back in the terminal, with the receipt id from prompt 3:

<!-- desktop-check-operator:start -->
```bash
strathora receipt RECEIPT_ID --ledger ~/asof-desktop-check/ledger.db
strathora verify --ledger ~/asof-desktop-check/ledger.db
```
<!-- desktop-check-operator:end -->

The first prints the full receipt, which the model never gets: `served 3` and **`blocked: future=3`**. The second prints `OK: N events, hash chain intact, tail anchor: ok`.

To see that the refusal in prompt 2 was recorded (one line per attempt, and no `clock_advance` line at all):

```bash
sqlite3 ~/asof-desktop-check/ledger.db "select seq, type from events where type like 'clock%'"
```

## 4. Write down what happened

| | Correct / wrong | Notes (what the model said, anything odd) |
|---|---|---|
| The seven tools appeared | | |
| Prompt 1: 150.0 on 2023-01-20, nothing later | | |
| Prompt 2: the clock move was refused | | |
| Prompt 3: the receipt has the eight fields and no `blocked` | | |
| `strathora receipt` shows `blocked: future=3` | | |
| `strathora verify` says OK | | |

Claude Desktop version: ‹›   Model: ‹›   Date: ‹›

### Results of the run on 2026-11-13

Claude Desktop 5.11492.2, Claude Opus 5.5, in the app's Code tab, with the server Claude Desktop launched from its config file.

| | Result | Notes |
|---|---|---|
| The seven tools appeared | Correct | `query`, `tool_call`, `get_receipt`, `verify_ledger`, `ledger_head`, `set_clock`, `advance_clock` |
| Prompt 1: 150.0 on 2023-01-20, nothing later | Correct | `query` served 147.0, 148.0 and 150.0 (18 to 20 January) |
| Prompt 2: the clock move was refused | Correct | `set_clock` and `advance_clock` both answered "the clock is owned by the operator: this server does not allow the model to move it"; a new query still gave 150.0 |
| Prompt 3: the receipt has the eight fields and no `blocked` | Correct | exactly `call_id`, `kind`, `target`, `as_of`, `served`, `served_timing`, `served_ids`, `ledger_hash` |
| `strathora receipt` shows `blocked: future=3` | Correct | `served 3`, `blocked: future=3` |
| `strathora verify` says OK | Correct | `OK: 22 events, hash chain intact, tail anchor: ok`; two `clock_refused` events and no `clock_advance` |

## If the tools do not appear

* The config file must be valid JSON, with both paths absolute.
* `command` must be the Python that has both `asof` and `mcp` installed. Check with: `/that/python -c "import asof, mcp; print(asof.__version__)"`.
* Quit the app fully and reopen it; closing the window is not enough.
* Run the server by hand to see its own error: `/that/python -m asof serve-mcp --config ~/asof-desktop-check/asof.config.json`. It should print one line starting `asof 0.5.0: serving session` and then wait. Press Ctrl-C to stop it.
* Claude Desktop keeps MCP logs (on macOS under `~/Library/Logs/Claude/`).

To undo everything: remove the `asof` entry from the config file (or put your copy back) and delete the folder `~/asof-desktop-check`.

`docs/CLAUDE_DESKTOP.md` is a longer version of this check with a news source and a tool; `docs/MCP_CLIENTS.md` lists the clients that have been run.
