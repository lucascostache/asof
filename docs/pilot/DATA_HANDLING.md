# Data handling, in plain English

This describes what the **pilot build** does today. Where something is not built, it says so. It is a description of software, not a legal agreement or a certification.

## The short version

1. **AsOf runs on your machine.** Your data, your tools, your model calls and your keys stay there. The ledger is a file on your disk.
2. **Nothing is sent unless you switch sending on.** It is off by default and needs three settings you make yourself.
3. **With `hashes_only` (what the pilot template uses; AsOf's own library default is `full`, so check), no raw record, prompt, response, tool argument or error text is sent.** What is sent is hashes, counts, times and the names you chose (see below).
4. **We reject, and never store, anything raw from a `hashes_only` client, and any credential from any client.**
5. **You can stop it and delete it:** revoke the key (sending stops at once), then a workspace owner deletes the synced data. Some things are not removed by that; they are listed under "Deletion".

## What runs on your machine

The AsOf library, your data files, your tool functions, the calls to your model provider (with your key, which goes to the provider and nowhere else), and the ledger: a SQLite file with a hash chain. If you never turn sync on, none of it leaves.

## What is sent, and only if you turn sync on

You turn it on with `ASOF_SYNC_ENABLED=1`, an address and a key we give you. Then, for every ledger event, AsOf sends an **envelope**: a sequence number, ids, the session id, the event type, the call id, three times (`event_time`, `available_at`, `recorded_at`), and three hashes (of the payload, of the previous event, of this event). That is enough for us to check that the chain is intact **without reading any content**.

Whether the **payload** goes too depends on the session's mode:

| Mode | Payload sent? | What is in it |
|---|---|---|
| `hashes_only` (the pilot template's setting) | yes, but already reduced | hashes of everything raw, plus counts, times, and the names listed below |
| `full` | **no**, unless you also set `send_payloads=True` (a second, separate switch) | the real values. Our server accepts them only from a client we set up as `allow_payloads` |

**In `hashes_only` a payload carries only:** counts of what was served and blocked; times; token-usage counts; hashes of record ids, records, tool arguments, prompts, responses, parameters and error text; the **names you chose** (source names, tool names, the model id and provider, decision action labels such as `hold`, and the optional label you give a ledger chain, such as a machine or strategy name); the model's declared cutoff and who declared it, if you registered one; **data-quality summaries** (AsOf 0.4.0 and later): counts of records with and without each timestamp, of suspicious timing, restatements, duplicates and gaps, the earliest and latest times in a source, lag and gap sizes in seconds, flag names, and for a universe query the number of members and a hash of the member list, never a record value; **evidence links** (the ids of the events and calls a decision rests on); **execution** events (AsOf 0.4.0 and later): the order and fill **ids**, which decision an order implements, whether the expected price came from the model or a backtest, and a yes, no or unknown flag for whether a stop and a target both lay inside one bar, while the symbol, prices, sizes, fees, levels and position marks are hashed; **model-gateway metadata** (AsOf 0.5.0 and later): the session clock at each model call, the reasoning setting, token counts, the names of any provider-side tools that were requested or that the provider said it ran, which gateway rule refused a call, and flags; never a prompt, a response or a parameter value; **decision-gate** events (AsOf 0.5.0 and later): the intent id, the agent id, the model id and version, the time of the data the intent rests on, the gate's decision (APPROVE, HOLD or BLOCK) and, for each check, its name and outcome, while the symbol, size, price and the readable details are hashed; and short warning sentences AsOf wrote itself. **Read your own names before you send:** order, fill, intent and agent ids are ids you chose and are readable; a source called `acme-merger-target-list` is readable by us. Use neutral names for the pilot.

## What a hash is, and is not

A hash is a one-way fingerprint. It proves two copies are identical and that something did not change. It is **not encryption and not anonymisation**: if the original could only be one of a few possible values (a price from a small list, a yes/no, a name from a short list), anyone who can try the possibilities can find which one matches. Do not treat a hash of a guessable value as secret.

## What we store, and how

* Each client's events, **append-only** (the database refuses edits and deletes), in one hash chain per ledger (see "More than one ledger per customer"), plus the latest head of each chain, a log of every batch we accepted or rejected, and who made reports from it.
* Your API key is stored **only as a hash**, shown once when created. We cannot show it again.
* Every accepted or rejected batch, and every key creation, rotation, revocation and deletion, becomes an event in the platform ledger: ids, counts and reasons, never content and never a key.
* **Where:** ‹fill in for the pilot: our infrastructure or yours, region, who has access›. The data sits in SQLite files in the engine's data folder; the sensitive columns are encrypted (next section).
* **In transit:** the AsOf client refuses a non-`https` address (except `localhost` for testing).
* **Limits per request:** 1,000 events and 5 MB; 120 requests a minute per key (counted in one engine process's memory); at most 100 chains per client.
* **Over HTTPS:** the deployment notes, and what was and was not tested, are kept with the service (they are not in this repository; ask us for them). Our access logs must not hold request bodies, keys, cookies or query strings; the notes say how, and that has been tested only against a stand-in proxy, not against nginx or Caddy.

## More than one ledger per customer

One API key can own many **chains**, for example one per machine or strategy. Each chain has its own id (a random id the AsOf ledger creates; AsOf 0.3.0 and later send it, with an optional label you choose), its own head and its own hash chain. A **new chain id is not a fork** and starts at event 1. A real fork (two different events claiming the same position in the **same** chain) is still rejected. Clients on the older format (`asof.sync/1`, AsOf 0.2.0) are accepted and are stored as one chain called `default`. The interface lists each client's chains with their heads and status.

## Encryption at rest

* **What is encrypted:** each event's payload, each client's name and each chain's label. AES-256-GCM, a fresh random nonce per value, and each value is bound to its workspace, table, column and row, so a ciphertext copied to another row or workspace does not open.
* **What is not, on purpose:** hashes, sequence numbers, timestamps, event types and ids, and counts. That is what lets a chain be checked without a key. So someone with the database file alone learns when events happened, how many, and their hashes; not what they say.
* **Keys:** every workspace has its own key, derived from a master key and the workspace's epoch. The master key list is read from the engine's environment (`STRATHORA_MASTER_KEYS`). It is never stored in the database, never logged and never returned by the API; error messages do not contain it. **Whoever holds the database and the master key can read everything**, and the engine process holds both while it runs. This does not defend against someone with access to the running engine.
* **If the key is missing,** data is reported as `locked`, not as intact and not as tampered. Without any master key configured the engine refuses to store AsOf data.
* **Rotation** is documented and tested; the procedure is kept with the service (not in this repository). Old backups stay readable with the old key.
* **Not encrypted:** the Verumark runs and platform-ledger events made from a session (source names, decision labels, times and hashes; no raw value for a `hashes_only` client), the web app's database apart from passwords being stored only as scrypt hashes, and backups beyond what is already sealed in them.
* **Tested:** a search of the database files for known payload strings, client names and chain labels finds none (with a control that finds them when encryption is off); and no key appears in logs or responses. See the engine's `tests/test_sealing.py`.

## Who can sign in, and who sees what

* **Login:** email and password (stored as a scrypt hash; 12 characters or more); there is no public sign-up, and the operator creates accounts. A session is a random token in an `HttpOnly`, `SameSite=Lax` cookie (`Secure` and `__Host-` prefixed on HTTPS); the database holds only the token's hash. Sessions end after 12 hours idle or 7 days, when the account is disabled or removed, or on sign-out or password change. Five wrong passwords lock the account for 15 minutes. Cross-site writes are refused.
* **Every page and API route requires a signed-in session**, except the login page and call, the engine health check, the key-authenticated `POST /api/asof/sync`, and a client reading its own head with its key. Route lists are enumerated from the code in the tests, so a new route is covered.
* **Workspaces:** a person sees only their workspace's clients, chains, events, reports and runs; another workspace's ids answer 403. Customer workspaces reach the AsOf area and their own Verumark runs only; the rest of the platform belongs to the operator's home workspace.
* **Roles:** `owner`, `member`, `read-only`. Read-only people only read. Creating, rotating and revoking keys, inviting members, and **deleting synced data are owner-only**. Generating a report needs `member` or `owner`.
* The web app tells the engine who is calling with a signed token that lasts 60 seconds, and the engine checks the role and workspace again itself.
* **Not built:** single sign-on, two-factor authentication, password reset by email, an audit log of reads, and the ability for us to be locked out of seeing your data (the operator's database and keys can read it). Do not read these notes as a security certification.

## What we never see

* Your model provider keys, and anything you send to your provider.
* Your agent's code, your data files, and your tools' real arguments and results (in `hashes_only`, only their hashes).
* Anything at all while sync is off.
* Credentials: a payload containing a key-shaped value (`sk-...`, `Bearer ...`, `AKIA...`, `ghp_...`) or a field called api key, authorization, password, secret, access token or similar that holds a value, is **rejected whole**, from every client.

## Retention

Synced events are kept **until they are deleted**. There is **no automatic expiry** in this build (not built). Agree a period together and put it in the pilot plan: ‹period›.

## Deletion

1. A workspace **owner**, in Settings, Connections, AsOf clients: **Revoke** the key (the client can no longer send), then **Delete synced data**, typing the client's name to confirm. Nobody else can do this, and the engine enforces it as well as the screen.
2. That removes **all of the client's chains**: their events, heads, the batch log and the record of reports made from them. It leaves one ledger event saying how many items were deleted and the last head hash of each chain.
3. **Not deleted by that:**
   * the platform ledger's earlier events about the client (ids, counts, reasons, no content);
   * any **Verumark report you generated** from a synced session. A report's decision log is written into the append-only platform ledger and the build has no run-deletion function. **Do not generate reports from a session you may later need erased**, or ask us to rebuild the pilot instance;
   * **backups** made before the deletion: they still hold the data (sealed). A deletion does not reach into them. Delete or expire old backups yourselves;
   * **leftover bytes on the disk beneath the file.** After a deletion the engine rebuilds the database file (SQLite `VACUUM`, freed pages overwritten, the write-ahead log truncated), so the deleted sealed values are no longer in the file or its log: this is tested by searching the files for them. If another process was reading at that moment the log cannot be truncated yet; the deletion then reports `file_scrubbed: false` and the step is repeated by an operator. That does **not** reach file-system snapshots, earlier backups, or copies the disk itself may keep (SSD wear levelling, journaling, free space the file system has not reused). The build also has no way to destroy a single workspace's key, which would be the stronger erasure.
4. Your own ledger file is yours; deleting ours does not touch it.

## How your security team can check this

* Read what would be sent before it is: `Syncer.wire_event` in `asof/sync.py`, and the allow-list of `hashes_only` keys in `SPEC.md` section 4.3 (machine-checked against the code).
* The receiver rejects anything outside that list; the tests that prove it (raw fields, credentials, payloads from a `hashes_only` client, deletion) are in the Strathora engine's `tests/test_asof_sync.py`.
* Run your own session with sync pointed at a local test receiver and inspect the request bodies.

## What is not covered here

Automatic retention, deletion of generated reports and of backups, single sign-on and two-factor authentication, audit of our staff's access, destruction of a single workspace's key, and any certification. If you need one of these for the pilot, say so before the first sync.
