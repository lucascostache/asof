# Pilot install: about 15 minutes, one data source, one tool, one model

**We do this with you on a call.** Bring a terminal and the three things in section 1; we will share a screen and go through it together. You can also do it alone from this page.
Nothing here sends your data anywhere. Sending is a separate, optional step at the end (section 8) that you switch on yourself.

## 1. What to have ready (send us the first two before the call if you can)

1. **One data source as a file or table** with, for each record, two times: when it **happened** (`event_time`) and when it **became knowable** to anyone (`available_at`). A CSV, Parquet file or SQL query is fine.
   If you have no `available_at`, tell us: AsOf withholds records without one (they are counted as `blocked_untimestamped`) rather than guess.
2. **One tool your agent already calls** (a news search, a filings lookup, a feature service): the function and the name of the timestamp field in what it returns.
3. **One model** your agent uses (`anthropic:<name>`, `openai:<name>` or `local:<name>` for any OpenAI-compatible server) and its API key **in an environment variable** in your own terminal. Never paste a key into a file, a chat or an email.

Python 3.10 or newer. Nothing else: the core has no dependencies.

## 2. Install (2 minutes)

The package is called `strathora-asof`: you import it as `asof` and run it as `strathora` (the older command name `asof` still works). It is not published on PyPI. **Do not run `pip install asof`:** that name on PyPI belongs to an unrelated project. We send you a wheel file, or access to the private repository.

```bash
python3 -m venv .venv
.venv/bin/pip install strathora_asof-0.5.0-py3-none-any.whl   # the file we sent you
# or, with access to the repository, one line:
# .venv/bin/pip install "git+<the repository address we give you>"
.venv/bin/strathora --version                                 # strathora-asof 0.5.0
```

*Verified by the author:* a wheel built from this repository installed into a clean virtual environment offline and ran the self-test below in about 7 seconds. The `[mcp]` extra (section 9) downloads one more package and needs network access.

## 3. Self-test on synthetic data (1 minute)

Use the template we send with this page, `wrap_agent.py` (it lives at `docs/pilot/examples/wrap_agent.py` in the repository):

```bash
.venv/bin/python wrap_agent.py --selftest
```

You should see three receipts (a query that served 2 and blocked 1, a tool call that served 1 and blocked 1, a model call whose memory label is `unknown`) and a last line like `ledger ...: OK: 14 events, anchor ok`. It uses invented data and a scripted stand-in for the model: no key, no network.

## 4. Wrap your data source (3 minutes) — "EDIT 1" in the template

Point `DATA_FILE` at your file and name your two time columns in `SOURCE`. Two rules matter:

* Every time needs a **time zone** (`2024-03-01T14:00:00Z`). The **session clock** without one is refused with an error. A **record's time** without one is never assumed to be UTC: the record is treated as having no usable time, so it is **withheld and counted** as `blocked_untimestamped`. If your file really is in one zone, say so with `assume_tz="UTC"` (or e.g. `"America/New_York"`) and the times are read in that zone.
* A record is served only if `available_at <= the session clock`. Records dated later, and records with no `available_at`, are withheld and counted.

## 5. Wrap one tool (2 minutes) — "EDIT 2"

Replace `news_search` with a call to your real tool and set `timestamp_field` to the field that holds each item's publication time. Items dated after the clock are dropped before your agent sees them.

## 6. Wrap one model (2 minutes) — "EDIT 3"

Set `MODEL = "anthropic:<the model you use>"` (or `openai:`/`local:`). The key is read from `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, sent to the provider and nowhere else, and never written to the ledger. AsOf does not choose a model for you and ships no model cutoff dates: if you know a model's cutoff and who stated it, you can register it; otherwise the receipt says `unknown`.

## 7. Run it and read the receipt (2 minutes)

```bash
export ANTHROPIC_API_KEY=...        # in your own terminal
.venv/bin/python wrap_agent.py --as-of 2024-03-01T14:00:00Z --ledger ./pilot-ledger.db
.venv/bin/strathora receipt <receipt id from the output> --ledger ./pilot-ledger.db
.venv/bin/strathora verify --ledger ./pilot-ledger.db        # OK: N events, hash chain intact, tail anchor: ok
.venv/bin/strathora head --ledger ./pilot-ledger.db          # store this somewhere you trust (see LIMITATIONS.md)
```

The template runs in `hashes_only` mode by default: the ledger keeps hashes, counts and times, not your records, prompts or responses.

## 8. Optional, later: send the ledger to us (you switch it on)

Only after we have agreed on what is sent (read `DATA_HANDLING.md`). We give you an API key and an address; you set three variables in your own terminal:

```bash
export ASOF_SYNC_ENABLED=1
export ASOF_SYNC_ENDPOINT=<the address we give you>
export ASOF_SYNC_API_KEY=<the key we give you>
.venv/bin/strathora sync --ledger ./pilot-ledger.db       # or leave ASOF_SYNC_ENABLED=1 on when you run wrap_agent.py
```

One key can own **several ledgers**, for example one per machine or strategy: each ledger file has its own chain id, and we keep each chain's head separately. Name a ledger with `ASOF_SYNC_CHAIN_LABEL=desk-1` (optional; we can read the label). A second ledger is not an error. What is refused (a "fork") is a *different event at the same position in the same ledger*.
A ledger written by AsOf 0.2.0 can still be read, verified and synced, but 0.3.0 will not add to it: start a new ledger (`strathora init` with a new path) and keep the old one.

## 9. Alternative: through MCP (no code changes)

If your agent runs in an MCP client (for example Claude Desktop), AsOf can sit between it and your data instead: add the MCP package to the same environment (`.venv/bin/pip install 'mcp>=2,<3'`), write a small config file, and add the server to the client. Step by step, with three test prompts: `docs/CLAUDE_DESKTOP.md` (not yet verified in Claude Desktop itself).

## 10. If something goes wrong

| You see | Why | Fix |
|---|---|---|
| `--as-of` is refused: "has no time zone" | the clock needs a zone | write it with `Z` or an offset |
| everything is `blocked_untimestamped` | the `available_at` column is empty or misnamed, **or its times have no zone** | name the right column; add `assume_tz="UTC"` (or your zone) if the times are zone-less; if you truly have no availability time, tell us |
| `served 0` | the clock is earlier than your oldest record | check `--as-of` and the time zone |
| `no API key for anthropic` | the variable is not set in this terminal | `export ANTHROPIC_API_KEY=...` |
| sync says `fork` | in this ledger's chain, we already hold a different event at the same position (a rebuilt or edited ledger, or one restored from an older copy under the same chain id) | tell us: do not delete anything yet. A *different ledger* under the same key is fine in 0.3.0 and later (it is its own chain; name it with `chain_label`) |
| sync says `401` or `403` | wrong or revoked key | ask us to check the key |

## Time budget

| Step | Minutes |
|---|---|
| 2 install | 2 |
| 3 self-test | 1 |
| 4 data source | 3 |
| 5 tool | 2 |
| 6 model | 2 |
| 7 run and read | 2 |
| buffer | 3 |
| **Total** | **15** |

Only steps 2 and 3 have been timed (about 7 seconds together, on a machine with Python already installed). The rest are **estimates**; they have not yet been done by anyone outside the author's machine, so treat the 15 minutes as a target until a first pilot confirms it.
