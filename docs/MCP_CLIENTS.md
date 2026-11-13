# MCP clients: what has been tested, and how they differ

AsOf's MCP server (`strathora serve-mcp --config FILE`) speaks MCP over **stdio**: the client starts it as a local process. There is no network transport, no login and one session per server process.

## What was actually run

| Client | Version | Date | What was run | Result |
|---|---|---|---|---|
| MCP Python SDK client | `mcp` 2.3.0 | every test run | `tests/test_mcp.py`, `tests/test_docs.py`, `tests/test_claude_desktop.py`: every tool, over stdio and in process | passes |
| **MCP Inspector** (command-line mode; TypeScript SDK 1.29.0) | 0.21.2 | 2026-11-11 | `tools/list`, then `tools/call` for `query` (plain, and with an object argument and a limit), `advance_clock`, `set_clock`, `get_receipt`, `verify_ledger`, an unknown source | every call behaved as documented; differences below |
| **MCP Inspector**, the full matrix (`tests/test_campaign_mcp.py`, run when the Inspector is on the machine) | 0.21.2 | 2026-11-12 | all eight tools used as intended (`model_call` included); 16 documented errors (unknown source and tool, bad durations and limits, the three clock refusals, receipts of other sessions, wrong tool arguments, a key or a stream in `model_call`, models the operator did not list); 9 calls the schema does not allow (missing and mistyped arguments, a tool that does not exist) | every tool answered as documented; every error came back as an error with the documented message and no traceback; the ledger verified afterwards |
| **opencode** (terminal coding agent) | 1.18.18 | 2026-11-11 | the server added to a project `opencode.json`; `opencode mcp list` | `asof connected`. **No model was run**, so no tool was called through it. A run with four of opencode's free models was attempted the same day, on the synthetic example only: the model provider refused each one before any tool call ("Free usage limit reached") |
| MCP Python SDK client **driven by a real model** (`scripts/live_agent_loop.py`) | `mcp` 2.3.0, `claude-haiku-5-5` | 2026-11-12 | a seven-turn loop: the model chose and called `query`, `tool_call`, `advance_clock`, `get_receipt` and `verify_ledger` through the client, on synthetic data that held an instruction aimed at it | every check passed; the clock was not moved and nothing from after it was shown (`docs/LIVE_TEST.md`) |
| Claude Desktop | n/a | n/a | not run by the author | **unverified**: `docs/CLAUDE_DESKTOP_CHECK.md` is a checklist to run it yourself |

All of it on one machine (macOS, Apple silicon), against synthetic data (a fictional company, `ACME-SYN`), with AsOf installed from git into a fresh virtual environment.

**Not tested: any MCP client application with a model in the loop.** A real model has driven the server's tools, but through a script that plays the client's part; how a client application (Claude Desktop, a coding agent) presents the tools to its own model, and what that model then does, has not been observed. Whether a provider accepts the tool schemas as they are has been checked for one provider only (see difference 5).

## What the Inspector saw (the three tests from `docs/CLAUDE_DESKTOP.md`, without a model)

| Test | Call | Result |
|---|---|---|
| 1. Nothing from after the clock | `query` `source=news` | `served: 3`, the headlines of 18, 19 and 20 January, clock `2023-01-20T23:59:59Z`. The operator's `strathora receipt <id>` shows `blocked: future=2` |
| 2. The model cannot move the clock | `advance_clock` `by=1d`, `set_clock` `as_of=2030-01-01T00:00:00Z` | both refused: `isError: true`, "the clock is owned by the operator: this server does not allow the model to move it". Two `clock_refused` events in the ledger, no clock change |
| 3. The agent's receipt hides blocked counts | `get_receipt` | see difference 2: the Inspector's command-line mode cannot ask for an earlier call's receipt |

Afterwards `strathora verify` on that ledger: `OK: 40 events, hash chain intact, tail anchor: ok`.

## Differences between clients

1. **The config has a different shape in each client; the command is the same.** Claude Desktop and the Inspector (`--config FILE --server asof`) read the same block, the one `strathora init --demo` prints:

   ```json
   {"mcpServers": {"asof": {"command": "/ABSOLUTE/PATH/TO/python", "args": ["-m", "asof", "serve-mcp", "--config", "/ABSOLUTE/PATH/TO/asof.config.json"]}}}
   ```

   opencode reads `opencode.json`, where the command and its arguments are **one list** and the entry needs a type:

   ```json
   {"mcp": {"asof": {"type": "local", "enabled": true, "command": ["/ABSOLUTE/PATH/TO/python", "-m", "asof", "serve-mcp", "--config", "/ABSOLUTE/PATH/TO/asof.config.json"]}}}
   ```

   In every client use **absolute paths**, and the Python that has both `asof` and `mcp` installed: a client does not start the server in your folder or with your shell's PATH.

2. **One server process is one session, and `get_receipt` only answers for the current session.** A client that keeps the server running (a desktop app, a coding agent) has one session for as long as it runs, so the model can ask for the receipt of its own earlier call. A client that starts the server for each request (the Inspector's command-line mode) gets a **new session every time**: `get_receipt` with an id from an earlier request is refused ("no receipt '...' in this session"). This is deliberate (a session cannot read another session's receipts). The operator is not affected: `strathora receipt <id> --ledger FILE` reads any session's receipt.

3. **Every server start is written to the ledger, even if no tool is called.** Listing tools, or `opencode mcp list`, starts and stops the server: the ledger gains `session_start`, `source_added`, `source_loaded` and `session_end` each time (4 events here). Eight Inspector requests left eight sessions. This is correct, but expect more sessions in the ledger than conversations.

4. **Results arrive twice, identically; errors arrive as text.** A successful call returns the same object as structured content and as JSON text, so a client that reads only one of them sees the same data. A refused or failed call returns `isError: true` and one text item that begins `Error executing tool <name>: ` followed by AsOf's message; there is no structured content on an error.

5. **Optional arguments are advertised as "this type or null".** `query`'s `filters`, `lookback` and `limit`, `tool_call`'s `arguments`, and `advance_clock`'s `to` and `by` appear in the tool schema as `anyOf: [{type}, {type: "null"}]`. The Inspector and the Python client accept this. Some model providers' function-calling is stricter about schemas than the MCP clients are. Anthropic's API accepted all seven definitions as they are and its model made a well-formed `query` call (`docs/LIVE_TEST.md`; sent straight to the API, not through an MCP client). **This has not been tested with OpenAI or any other provider**, so there it is the first thing to check if a client connects but a model's tool call is rejected.

6. **Paths inside `asof.config.json` do not depend on the client.** Relative paths in the config are read against the config file's own folder, never the client's working directory: the Inspector ran from a different folder and the ledger still appeared beside the config. (`strathora init --demo` writes the ledger path as an absolute path.)

7. **The server prints one line on stderr when it starts** (`asof 0.5.0: serving session ..., clock ..., ceiling ..., model may move clock: False`). stdout carries only the protocol. Clients show stderr in their logs; it is not an error.

8. **A client that stops the server with a signal.** Closing the server's input is the ordinary way to stop it, and the session then ends with a `session_end` event. opencode, when it was itself stopped, ended the server with `SIGTERM` instead, and the session had no `session_end`. The server now treats `SIGTERM`, Ctrl-C (`SIGINT`) and a closed terminal (`SIGHUP`) as ordinary stops and writes the event (tested: 100 starts and stops, 25 each way, 100 closed sessions). A hard kill (`SIGKILL`, a crash, power loss) cannot be recorded: the chain still verifies, and that session simply has no end event.

9. **Two clients, one config.** Two clients pointed at the same config file start two servers on the same ledger. Nothing refuses that, and nothing breaks: the ledger lets one writer in at a time, so there is one valid chain holding two sessions, each with its own clock, and neither can read the other's receipts. If you want one ledger per client, give each its own config with its own `ledger_path`.

## Repeat it

```bash
# MCP Inspector, no model. Put the "mcpServers" block above into mcp.json first.
npx @modelcontextprotocol/inspector --cli --config mcp.json --server asof --method tools/list
npx @modelcontextprotocol/inspector --cli --config mcp.json --server asof --method tools/call --tool-name query --tool-arg source=news
npx @modelcontextprotocol/inspector --cli --config mcp.json --server asof --method tools/call --tool-name advance_clock --tool-arg by=1d

# opencode: put the "mcp" block above into opencode.json in an empty folder, then
opencode mcp list
```
