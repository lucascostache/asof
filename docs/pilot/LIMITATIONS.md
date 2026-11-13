# Limitations: what AsOf and the pilot do not prove

Read this before you rely on a receipt, a report or a number from the pilot. It is short on purpose.

## 1. A closed model's memory is never "Verified"

AsOf controls what data **flows through it**. It cannot see inside a model. A model trained on text that runs past your as-of time may "remember" the future whatever the data layer serves.
* A receipt shows the model's **declared cutoff** and **who declared it** (`user` or `provider`). That is a statement, not a measurement, and AsOf ships **no** cutoff dates because a wrong one would silently turn into a leak.
* A **contamination score** appears only if someone measured and registered one.
* The memory label is `unknown`, or at most `observed` (a score exists). There is no `clean` and no `verified`, by design.
* Time-locked data plus a model that already knows the outcome is **not** a clean backtest. Treat model memory as Unknown unless you have measured it for the period in question.

## 2. Timestamps are supplied by whoever owns the data

`available_at` is declared by the data's owner. AsOf **enforces** it; it cannot **verify** it. If a source stamps a restated figure with its original date, or a vendor's "published" time is really the time it was scraped, the lock will trust it. Common traps: restatements, revisions, time zones, and filings whose acceptance time is read as the wrong zone (for SEC data AsOf reads the digits as New York time and errs late; settle it against EDGAR's own pages with `scripts/smoke_edgar.py`). Records with no `available_at` are withheld, never assumed old.

**The data-quality summary in a receipt (AsOf 0.4.0 and later) is not a check of the timestamps.** It looks for patterns that honest clocks do not produce (a long history available in one hour, availability before the event, gaps, staleness) using thresholds written into every summary, and those thresholds are judgement, not measurement. A source whose every record is consistently mis-stamped passes all of it. A check that cannot run says "unknown" (for example restatements, when a source has no record ids); an older ledger says "not recorded". The per-source summary looks at records after T, so it is for you and not for the agent. Index membership is only as good as the announcement dates in the change log you supply.

## 3. The head anchor must be stored somewhere you trust

A hash chain shows that events were not **changed** inside the chain. It cannot show that the **end** was not cut off, or that the whole file was not rebuilt. Truncation is detectable only against a copy of the **head** (`{seq, hash, recorded_at}`) kept somewhere the writer cannot reach: another system, an email to yourself, version control. If the head is stored only next to the ledger, tampering that removes both goes unnoticed.
Strathora stores the head it last received for each client and serves it (`GET /api/asof/head/<client id>`). That is "elsewhere" only to the extent you trust Strathora and the client to be independent of each other, and only up to the last sync; events written since are not covered.

## 4. Only what flows through AsOf is locked

Anything your agent can reach **another way**, its own memory, other tools, the open web, a cache, a database it queries directly, is outside the lock. A receipt describes the calls made through AsOf and nothing else.

## 5. A chain is not truth, and not authorship

An intact chain means the recorded events are unchanged since they were written. It does not mean they describe what the agent really did, that nothing was left unrecorded, or who wrote them: the ledger is unsigned. Anyone who controls the writer can write a false but internally consistent ledger **before** the first anchor exists elsewhere.

## 6. Sending to Strathora

* **`hashes_only` is not confidentiality for guessable values**, and the names you choose (sources, tools, models, decision labels) are readable. See `DATA_HANDLING.md`.
* **One key can own many ledger chains** (AsOf 0.3.0 and later; the clients and Strathora use a per-chain id). A new chain id is accepted as a new chain, not a fork; two different events at the same position in one chain are rejected. **A chain id is a claim by the client:** nothing stops a client from starting a new chain to hide events it dropped, and each chain is checked on its own. If "how many ledgers should this customer have" matters, agree it in the pilot plan and read the chain list. Clients on the older format (AsOf 0.2.0) are accepted as one chain called `default`; **AsOf 0.3.0 reads and verifies a 0.2.0 ledger but will not append to it**.
* A client that sends **envelopes only** (a `full` session without payloads) proves its chain, but counts that live in a payload read "not synced"; nothing is estimated.
* Login, workspaces and encryption at rest exist now (see `DATA_HANDLING.md`). Still **not built:** single sign-on, two-factor authentication, password reset by email (an operator sets passwords), automatic retention, a log of who read what, and deletion of a generated report.
* **Encryption at rest does not protect from the operator or from a running engine:** the process holds the database and the keys. Hashes, sequence numbers, timestamps and event types stay readable. Deleted data is scrubbed from the database file after a purge, but can linger, sealed, in older backups, file-system snapshots and the disk's own free space.
* **A lost master key means the sealed data is unreadable for good** (chains report `locked`).
* **The rate limiter is in the engine process's memory.** Run one engine process; with several the limits multiply, and a restart resets them. A shared limiter can be plugged in but has not been written.
* **Backups are point-in-time.** Restoring an older one rewinds each chain; clients resend what came after it. Events a client has deleted from its own ledger cannot be recovered from the receiver.
* Strathora's check of your chain covers what it **received**: events you never sent are invisible to it.

## 7. Verumark reports built from an AsOf session

* A report is built **per decision**. Records and tool items served **after the session's last decision** belong to no decision and are not in it.
* For a `hashes_only` client the input values are hashes, so a check that needs the value itself cannot run and says Unknown.
* Where a check cannot run, or a timestamp is missing, the claim is labelled **Unknown**, never counted as clean.
* The model-memory check stays Unknown without probe results (item 1).

## 7b. Execution, evidence links and bars (AsOf 0.4.0 and later)

* **AsOf cannot see an exchange.** Orders, fills, fees and marks are what **your code tells it**. A report can say the numbers add up and are linked by the ids you recorded; it cannot say they are what really happened.
* **The attribution of an execution gap rests on prices you state.** The gap against the model's or backtest's expected fills is split into costs, decay, timing, slippage and unexplained, and the parts add up to the gap exactly (unexplained is the remainder).
  Decay, timing and slippage use your expected, arrival and reference prices, so they are **Observed**, not Verified. Costs are Verified only when every fill recorded a fee. A price you did not record leaves its effect in *unexplained*, shown with its parts.
  Orders with no fill, and orders with no expected price, are listed and left out of the gap; **the opportunity cost of an order that never filled is not attributed.**
* **An exit order inside one bar is unknown.** When a stop loss and a take profit both lie inside a bar's range, the bar cannot say which was hit first. AsOf records the flag when you pass the bar, otherwise it records "not assessed", and the report labels the exit **Observed**.
* **Links say what was available, not what was used.** By default a decision links everything served since the previous decision. Only `based_on=` is the agent's own statement of what it rests on, and AsOf cannot check that statement either.
* **Bars are only as good as their close.** A bar is available at the close you give it (its timeframe, a session close, or a `close_time` column). A weekly bar needs a `close_time` column or it is taken to close a week after its open;
  a futures session that opens the evening before needs `session_close` set correctly. AsOf cannot know an exchange's calendar, holidays or half days.
* **In `hashes_only` mode** every price, size and fee is a hash, so a report built from such a client shows the orders, the fills and the links, and **no** execution numbers ("not synced", never zero).

## 8. What has not been verified, as of this build

Be told rather than assume. Not verified by the author: the **nginx and Caddy configurations** in the service's deployment notes (neither program was available; only a stand-in TLS proxy was tested) and any real certificate, firewall or host log; **Redis or any shared rate limiter** (not written); the **web app's database backup and restore** beyond one `.backup` of a test database; **restoring onto another machine**; a backup taken under sustained heavy writes; **cross-language hashing against any language other than Node** (the test vectors in `spec/vectors` are matched by the Python code and by a Node verifier, and by a second, independent Python verifier, nothing else); **numbers above 2 to the power 53** in payloads (refused on write, read as doubles); the **encryption design has not had an outside review**; Claude Desktop beyond one run of `docs/CLAUDE_DESKTOP_CHECK.md` in its Code tab on 2026-11-13 (it passed; the Chat tab and the longer `docs/CLAUDE_DESKTOP.md` tests were not run); live calls to OpenAI (not yet tested with a key; Anthropic was run live once with one model, see `docs/LIVE_TEST.md`; `scripts/smoke_*.py` themselves are tested only against local mock servers); live EDGAR (the by-hand check in `scripts/smoke_edgar.py` is yours to run); the GitHub Actions workflow as it stands (its test jobs passed on GitHub on Linux and macOS on 2026-11-13; later changes to it, such as the secret-scan job, had not run there when this was written); install timing steps 4 to 7 with a customer's real data; and the pilot success measures, which have no results yet.

## 8b. Where it has been installed, and where the ledger file can live

Run on 2026-11-12 (`scripts/clean_install_check.sh`, `tests/test_campaign_paths.py`):

* **Python 3.10, 3.11, 3.12, 3.13 and 3.14, on one macOS machine (Apple silicon).** On each: a new virtual environment, the one-line install from git with and without the MCP extra, and the README's Install and Quickstart typed exactly as written, with the output compared to the README's. **Linux and Windows have not been run.** The install was from a local git address, not from a hosted repository.
* **A ledger path with spaces, accents, non-Latin letters, an emoji, quotes or twelve missing parent folders works**: the folders are made, and the anchor sits beside the ledger.
* **A ledger that cannot be written can still be checked.** `strathora verify`, `head`, `receipt` and `report` open a read-only ledger (or one in a read-only folder) without changing a byte. Until this run they failed with "attempt to write a readonly database". Writing to such a ledger, or creating one in a folder that cannot be written, is refused with one line.
* **When the disk fills up, the write that does not fit is refused whole** ("database or disk is full"), a query whose record cannot be written serves nothing, and the ledger still verifies; when space returns it carries on. Tested with SQLite's own size limit and once by hand on a real 3 MB volume. Two things to know: if only the anchor file cannot be written, the event is in the ledger and the anchor is one event behind until the next write (`verify` says so); and a ledger closed on a full disk may be left as `ledger.db` plus a `ledger.db-wal` file, so copy both, or use `Ledger.backup`.
* **Not tried:** a network drive, a synced folder (iCloud, Dropbox), a case-insensitive clash of names, paths longer than the operating system allows, and a disk that fails in the middle of a write for a reason other than being full.

## 9. What would change this page

A measured contamination score for a model and period (item 1), a verified timestamp source (item 2), an independent store for heads (item 3). If you find a way AsOf lets the future through that this page does not list, that is a bug: tell us.
