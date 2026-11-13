# AsOf specification

Status: **SPEC 1.3**, describing AsOf **0.5.0**. Versions defined here: record format **1**, ledger schema **3** (hash domain `asof-ledger/3`, canonical JSON = RFC 8785), head file **1**,
sync wire format **`asof.sync/2`** (adds client and chain ids). **SPEC 1.2 and 1.3** only *add* optional payload keys (1.2: `quality` on `source_loaded` and `query`, `universe` on `query`, section 4.5; 1.3: `gateway` on `model_call` and
`model_error`, section 4.8): no hash rule, schema version or wire format changed, so a ledger written under 1.1 verifies and syncs unchanged and a 1.2 or 1.3 ledger verifies under any 1.1 verifier (an unknown payload key does not change how an event is hashed). **SPEC 1.0** (AsOf 0.2.0: ledger schema **2**, hash domain `asof-ledger/2`, Python-formatted canonical JSON, wire format `asof.sync/1`) is still read, verified
and accepted by receivers; the `schema_version` in the ledger decides which rules apply to it (section 10). Nothing writes schema 2 any more.
This document is what other implementations (a verifier, a receiving service, another language) need; the library's tests check it (see section 9).

## 1. Conventions

**1.1 Times.** Every time is an instant in UTC written `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`: fractional seconds appear only when non-zero, always 6 digits, always the letter `Z`.
A time with no time zone is not an instant and is never accepted where an instant is required. `null` means *unknown*; it is never replaced by a guess.

**1.2 Canonical JSON.** Before canonicalising, a datetime inside a payload is written as 1.1; a date as `YYYY-MM-DD`; a set as a sorted array; bytes as `{"__bytes_sha256__": "<hex>"}`; tuples as arrays.
* **Schema 3 (SPEC 1.1): RFC 8785, the JSON Canonicalization Scheme.** No whitespace; object members sorted by the UTF-16 code units of their names; strings escaped only where JSON requires it, everything else UTF-8;
  numbers written as ECMAScript's `Number.prototype.toString` writes them (shortest round-trip digits, `2592000.0` is `2592000`, `1e+21`, `1e-7`, `-0` is `0`). Beyond what RFC 8785 itself says (it builds on I-JSON, RFC 7493),
  a writer **must refuse** integers outside +-(2^53 - 1) (put such a value in a string), NaN, infinity and lone surrogates. Any RFC 8785 implementation in any language produces the same bytes;
  `spec/vectors/jcs.json` is the shared test set and `verifier/verify.mjs` is a Node implementation checked against it.
* **Schema 2 (SPEC 1.0, legacy):** keys sorted by code point, `,` and `:` separators, UTF-8 kept, NaN and infinity errors, floats as Python's `json.dumps` writes them. Another language cannot reproduce this in general
  (`2592000.0` stays `2592000.0`), which is why 1.1 replaced it. A receiver that must re-canonicalise a schema-2 payload to check its hash does it with Python's rules.
The event hash (4.1) covers only strings, integers and null, so its canonical form is identical under both.

**1.3 Hashes.** `sha256(text)` is the lowercase hex SHA-256 of the UTF-8 bytes of `text`.

## 2. Record format

A source returns records. One record is one *version* of one thing.

| field | type | meaning |
|---|---|---|
| `source_id` | string | the source's name |
| `record_id` | string | identity across versions (restatements share it) |
| `event_time` | instant \| null | when the thing happened |
| `available_at` | instant \| null | when it became knowable. Time-locking uses only this |
| `payload` | object | the data |
| `payload_sha256` | hex | `sha256(canonical_json(payload))` (1.2); a record whose hash does not match its payload is rejected |
| `flags` | array of strings | data-quality notes, e.g. `naive_available_at`, `missing_available_at`, `unparseable_available_at`, `available_before_event` |

Both times are declared by the data's owner. AsOf enforces them; it cannot verify them.

## 3. Time-lock semantics (T is the session clock)

1. **R1.** A version is visible iff `available_at <= T`. The boundary is inclusive: equal is visible, one microsecond later is not.
2. **R2.** `available_at = null` is never visible, unless the session allows untimestamped data; then it is served labelled `timing: "unknown"`. A timestamp without a time zone counts as null.
3. **R3.** For each `record_id`, among visible versions only the one with the greatest `available_at` is a candidate (ties: the later row in the source, reported as a warning). A later version is never served.
4. Filters are applied to the candidate, **after** step 3. A lookback window tests `event_time`.
5. **R4.** A tool item is visible iff its timestamp field parses to an instant `<= T`; an item without one is treated as R2.
6. **R5.** The clock only moves forward; it may also have a ceiling it cannot pass.

## 4. Ledger event format (schema 3; schema 2 in section 10)

A SQLite table `events` in an append-only file. `meta(key, value)` holds `schema_version = "3"` and `chain_id` (7.2). Triggers refuse UPDATE and DELETE.

| column | type | meaning |
|---|---|---|
| `seq` | integer | 1, 2, 3, ... with no gaps; part of the hash |
| `id` | text | unique event id (random) |
| `session_id` | text | the session that wrote it |
| `type` | text | see 4.3 |
| `call_id` | text \| null | groups the events of one call; a receipt id is a `call_id` |
| `event_time` | instant \| null | **when the underlying thing happened** (4.2) |
| `available_at` | instant \| null | **when it became knowable** (4.2). Never wall-clock time |
| `recorded_at` | instant | **wall-clock UTC when the ledger wrote the event.** Set only by the ledger; callers cannot supply it. All events of one atomic batch share it |
| `payload` | text | canonical JSON (1.2) of the (privacy-shaped, redacted) payload |
| `payload_sha256` | hex | `sha256(payload)` (the column's text) |
| `prev_hash` | hex | the previous event's `hash`; 64 zeros for event 1 |
| `hash` | hex | see 4.1 |

### 4.1 Hash

`hash = sha256(canonical_json(["asof-ledger/3", seq, prev_hash, id, session_id, type, call_id, event_time, available_at, recorded_at, payload_sha256]))` (`asof-ledger/2` for a schema-2 ledger)

Every field except `payload` is covered directly, `payload` through `payload_sha256`, and the position through `seq` and `prev_hash`. Changing, removing, reordering or splicing in any event breaks the chain.

### 4.2 The three clocks

| event | `event_time` | `available_at` |
|---|---|---|
| a **served record** (`record_served`) | the record's own `event_time` (null if unknown) | the record's own `available_at` (null if served as unknown) |
| a **served tool item** (`tool_item_served`) | null (a tool declares one timestamp) | the item's declared timestamp (null if unknown) |
| every **agent action** (all other types) | the session clock T at that moment | T (the agent acts at T) |

For `clock_advance` T is the new time (the moment after the change). For a refused move it is the unchanged current time. `recorded_at` is the only wall-clock field.

### 4.3 Event types and their payload keys

Agent actions: `session_start`, `session_end`, `source_added`, `source_loaded`, `source_error`, `tool_added`, `query`, `tool_call`, `model_call`, `decision`, `block`, `tool_error`, `model_error`,
`clock_advance`, `clock_rewind_rejected`, `clock_ceiling_rejected`, `clock_gate_rejected`, `clock_refused`. Served things: `record_served`, `tool_item_served`. Execution (SPEC 1.2, section 4.6): `order`, `fill`, `position_mark`. Decision gate (SPEC 1.3, section 4.9): `intent`, `gate_decision`.
A `block` has `reason` (`future` or `untimestamped`) and `label` (`blocked_future` / `blocked_untimestamped`).

The exact payload keys, as emitted (this block is machine-checked against the library):

<!-- asof-event-schema:start -->
```json
{
 "full": {
  "block": [
   "count",
   "kind",
   "label",
   "list_truncated",
   "listed_payload_sha256",
   "listed_record_ids",
   "reason",
   "target"
  ],
  "clock_advance": [
   "from",
   "to"
  ],
  "clock_ceiling_rejected": [
   "attempted",
   "current"
  ],
  "clock_gate_rejected": [
   "actions_without_decision",
   "attempted",
   "current"
  ],
  "clock_refused": [
   "current",
   "reason"
  ],
  "clock_rewind_rejected": [
   "attempted",
   "current"
  ],
  "decision": [
   "action",
   "details",
   "links"
  ],
  "fill": [
   "fill_id",
   "intrabar_ambiguous",
   "order_id",
   "terms"
  ],
  "gate_decision": [
   "decision",
   "details",
   "intent_id",
   "policy_sha256",
   "reasons"
  ],
  "intent": [
   "agent_id",
   "data_as_of",
   "intent_id",
   "links",
   "model_id",
   "model_version",
   "terms"
  ],
  "model_call": [
   "contamination_score",
   "declared_by",
   "declared_cutoff",
   "declared_cutoff_after_as_of",
   "finish_reason",
   "gateway",
   "label",
   "messages",
   "model_id",
   "param_sha256",
   "params",
   "prompt_sha256",
   "provider",
   "response_sha256",
   "response_text",
   "score_source",
   "usage",
   "version_reported",
   "warnings"
  ],
  "model_error": [
   "error",
   "error_type",
   "gateway",
   "model",
   "prompt_sha256"
  ],
  "order": [
   "decision_call_id",
   "expected_source",
   "intent_call_id",
   "order_id",
   "terms"
  ],
  "position_mark": [
   "terms"
  ],
  "query": [
   "counts",
   "filters",
   "limit",
   "lookback",
   "quality",
   "served",
   "source",
   "source_fingerprint_sha256",
   "universe",
   "warnings"
  ],
  "record_served": [
   "flags",
   "payload_sha256",
   "record_id",
   "record_payload",
   "source",
   "timing"
  ],
  "session_end": [
   "n_calls"
  ],
  "session_start": [
   "allow_untimestamped",
   "as_of",
   "asof_version",
   "decision_gate",
   "max_as_of",
   "privacy",
   "python"
  ],
  "source_added": [
   "name",
   "type"
  ],
  "source_error": [
   "error",
   "error_type",
   "name"
  ],
  "source_loaded": [
   "fingerprint_sha256",
   "flags",
   "n_record_ids",
   "n_records",
   "n_with_available_at",
   "n_without_available_at",
   "name",
   "quality"
  ],
  "tool_added": [
   "name",
   "pass_as_of",
   "timestamp_field"
  ],
  "tool_call": [
   "args",
   "blocked_future",
   "blocked_untimestamped",
   "n_items",
   "served",
   "served_unknown_timing",
   "tool"
  ],
  "tool_error": [
   "args",
   "error",
   "error_type",
   "tool"
  ],
  "tool_item_served": [
   "item",
   "payload_sha256",
   "timing",
   "tool"
  ]
 },
 "hashes_only": {
  "block": [
   "count",
   "kind",
   "label",
   "list_truncated",
   "listed_payload_sha256",
   "listed_record_ids_sha256",
   "reason",
   "target"
  ],
  "clock_advance": [
   "from",
   "to"
  ],
  "clock_ceiling_rejected": [
   "attempted",
   "current"
  ],
  "clock_gate_rejected": [
   "actions_without_decision",
   "attempted",
   "current"
  ],
  "clock_refused": [
   "current",
   "reason"
  ],
  "clock_rewind_rejected": [
   "attempted",
   "current"
  ],
  "decision": [
   "action",
   "details_sha256",
   "links"
  ],
  "fill": [
   "fill_id",
   "intrabar_ambiguous",
   "order_id",
   "terms_sha256"
  ],
  "gate_decision": [
   "decision",
   "details_sha256",
   "intent_id",
   "policy_sha256",
   "reasons"
  ],
  "intent": [
   "agent_id",
   "data_as_of",
   "intent_id",
   "links",
   "model_id",
   "model_version",
   "terms_sha256"
  ],
  "model_call": [
   "contamination_score",
   "declared_by",
   "declared_cutoff",
   "declared_cutoff_after_as_of",
   "finish_reason",
   "gateway",
   "label",
   "messages_sha256",
   "model_id",
   "param_sha256",
   "params_sha256",
   "prompt_sha256",
   "provider",
   "response_sha256",
   "response_text_sha256",
   "score_source",
   "usage",
   "version_reported",
   "warnings"
  ],
  "model_error": [
   "error_sha256",
   "error_type",
   "gateway",
   "model",
   "prompt_sha256"
  ],
  "order": [
   "decision_call_id",
   "expected_source",
   "intent_call_id",
   "order_id",
   "terms_sha256"
  ],
  "position_mark": [
   "terms_sha256"
  ],
  "query": [
   "counts",
   "filters_sha256",
   "limit",
   "lookback",
   "quality",
   "served",
   "source",
   "source_fingerprint_sha256",
   "universe",
   "warnings"
  ],
  "record_served": [
   "flags",
   "payload_sha256",
   "record_id_sha256",
   "record_payload_sha256",
   "source",
   "timing"
  ],
  "session_end": [
   "n_calls"
  ],
  "session_start": [
   "allow_untimestamped",
   "as_of",
   "asof_version",
   "decision_gate",
   "max_as_of",
   "privacy",
   "python"
  ],
  "source_added": [
   "name",
   "type"
  ],
  "source_error": [
   "error_sha256",
   "error_type",
   "name"
  ],
  "source_loaded": [
   "fingerprint_sha256",
   "flags",
   "n_record_ids",
   "n_records",
   "n_with_available_at",
   "n_without_available_at",
   "name",
   "quality"
  ],
  "tool_added": [
   "name",
   "pass_as_of",
   "timestamp_field"
  ],
  "tool_call": [
   "args_sha256",
   "blocked_future",
   "blocked_untimestamped",
   "n_items",
   "served",
   "served_unknown_timing",
   "tool"
  ],
  "tool_error": [
   "args_sha256",
   "error_sha256",
   "error_type",
   "tool"
  ],
  "tool_item_served": [
   "item_sha256",
   "payload_sha256",
   "timing",
   "tool"
  ]
 }
}
```
<!-- asof-event-schema:end -->

### 4.4 Privacy shaping and redaction

`full` keeps the raw keys above. `hashes_only` replaces each *raw* key by `<key>_sha256` (the sha256 of its canonical JSON): `filters`, `listed_record_ids`, `record_id`, `record_payload`, `args`, `item`,
`messages`, `response_text`, `params`, `details`, `error`. Counts, names, ids of sessions and calls, hashes and times stay. `param_sha256` holds one hash per model parameter in both modes.
In **both** modes, any object key that looks like a credential (`api_key`, `authorization`, `token`-like secrets, `password`, ...) is replaced by `"[REDACTED]"`, and credential-shaped string values
(`sk-...`, `Bearer ...`, `AKIA...`, `ghp_...`) are redacted, before hashing and storing.

### 4.5 Data-quality summaries and universes (SPEC 1.2)

Two optional payload keys carry what AsOf could tell, from the records alone, about how much to trust a source's timestamps and rows. Both are plain JSON of integers, strings and `null` (no floats), and both are
**metadata**: counts, times and flag names, never a record value, so they are the same in `full` and `hashes_only`. `null` always means *unknown* (a check that could not run), never `0`. They cannot verify a
timestamp (section 8); they look for patterns honest clocks do not produce, and each summary names its thresholds.

**`source_loaded.quality`** (operator view; it covers the **whole** source, including records after any session clock, so a receiver must not show it to the agent): `v` (1), `evaluated_at` (the wall clock when computed,
used only for `future_dated`), `n_records`, `kind` (`series`, or `change_log` for a universe: a log of changes that are announced before they take effect and arrive irregularly, so `available_before_event` is 0 and
`timing.announced_ahead` counts them instead, and gaps and staleness are `null`), `identity` (`record_id`, or `row_index` when the source gave no key so every id is `row:<n>`), `event_time` and `available_at` (`n_present, n_missing, n_naive, n_unparseable, min, max`),
`timing` (`available_before_event`, `future_dated` = an `available_at` or `event_time` after `evaluated_at`, `lag_seconds{min,median,max}`), `bursts` (`n_bursts`, `n_records_in_bursts`, `largest{available_at_hour, n_records,
event_span_days}`), `restatements` (`detectable`, `n_ids_restated`, `n_extra_versions`; all `null` and `detectable` false when `identity` is `row_index`), `duplicates` (`basis`, `n_duplicate_rows`),
`gaps` (`median_step_seconds`, `n_gaps`, `largest_gap_seconds`, `largest_gap_after`; `null` with fewer than 3 distinct event times; for a source with a market calendar also `calendar`, `median_sessions_skipped`,
`n_gaps_within_day`, `n_gaps_missed_sessions`, `n_sessions_missed`, and `median_step_seconds` is then the median step within one day; these keys are absent without a calendar), `thresholds`, and `flags` (sorted names: `missing_event_time`, `missing_available_at`,
`unusable_timestamps`, `available_before_event`, `future_dated`, `bulk_backfill`, `restatements`, `no_record_identity`, `duplicate_rows`, `gaps`, `too_few_points_for_gaps`, and from a source's `notes`:
`partial_bars_withheld`, `availability_moved_to_close`).

**`query.quality`** (computed only from the record versions **visible at T** and the records served, so it adds nothing about the future): `v`, `kind`, `n_visible_versions`, `served`, `served_with_event_time`, `served_with_available_at`,
`available_before_event`, `in_backfill_burst`, `restated_ids` (served ids with more than one distinct version visible at T), `duplicate_rows`, `lag_seconds`, `freshness{newest_event_time, age_seconds, median_step_seconds, stale}`
(`stale` is true when `age_seconds` exceeds 5 median steps, `null` when no step can be measured), `gaps_between_served`, `thresholds`, `flags`, `source{flags, summary_sha256}` (the flags of the source summary and the hash of
that summary, tying the receipt to the `source_loaded` event), and for a `Universe` source `membership{n_members, members_sha256, pending_adds, pending_removes, duplicate_adds, orphan_removes}`.
With a market calendar on the source, `freshness` also has `calendar`, `sessions_since_newest` and `median_sessions_skipped`. A `Universe` declared with `listed_in` adds `membership.listed{source, evidence_date,
n_listed, n_listed_not_members, listed_not_members_sha256}` (and `listed_not_members`, the sorted names, in privacy mode `full` only) and the flag `survivorship` when `n_listed_not_members` is above 0.
Every key named in this paragraph appears only when its option is used, so summaries without it, and their hashes, are as before.

Thresholds (`thresholds` in every summary): a **gap** is a step between consecutive distinct `event_time` values greater than 5 times the median step; a **burst** is one UTC hour of `available_at` holding at least 20
records whose `event_time` spans at least 30 days; **stale** is as above. These are heuristics with stated parameters, not findings of fact.

**`query.universe`** (only when the query was restricted to a universe as of T): `source`, `field_sha256` (and `field` in `full`), `n_members`, `members_sha256` (SHA-256 of the canonical JSON of the sorted member list),
`universe_call_id` (the logged query that fetched the membership), `excluded` (records the unrestricted query would have served that the restriction removed: the survivorship effect).

**Universe sources.** A universe is a log of changes, one record per `add` or `remove`, with `event_time` = the effective date and `available_at` = the announcement. The members at T are the changes with
`available_at <= T` and `event_time <= T`, replayed in order of effective date; a change announced by T and effective later is *pending*; a change effective by T but announced after T is blocked like any future record.

**SPEC 1.3 additions to `source_loaded.quality`** (all optional for a reader): `timing.late_availability` (the number of records that became available more than 10 times the median lag, and more than a day,
after their event; `null` for a change log; a source whose every record is equally late raises nothing), the flag `late_availability`, and two keys that replace any single score with a list and counts:
**`issues`**, one entry per flag raised, most serious first: `flag`, `severity` (`error`: the timestamps cannot be honest; `warning`: a pattern honest data does not usually show; `info`: something the source did),
`basis` (`counted`: an exact count of something observable; `heuristic`: rests on a threshold, so never proof), `n` (records touched, or `null` if not countable), and `example_record_ids_sha256`, up to 5 affected
record ids **as hashes**, equal to `record_id_sha256` on the matching `record_served` event, so a platform can open the exact rows without a raw id ever being in the summary; and **`by_severity`**
(`error`, `warning`, `info`: counts of issues). `thresholds` also holds `late_lag_factor` (10) and `late_lag_min_seconds` (86400). There is no overall score.

A source may add **`notes`** to its `source_loaded.quality`: integer counts of what the source itself did to the data before AsOf saw it. A bars source reports `partial_bars_withheld` and
`availability_moved_to_close` (section 4.7), each raising the flag of the same name.

A reader of a ledger without these keys (anything written before SPEC 1.2) reports the quality as **not recorded**, never as clean.

### 4.6 Evidence links and execution events (SPEC 1.2)

**`decision.links`** (always present on a decision written by 0.4.0): `basis` (`since_previous_decision`, or `explicit` when the agent named the calls with `based_on`), `query_call_ids`, `tool_call_ids`, `model_call_ids`
(the `call_id`s of those calls), `served_event_ids` (the ledger `id`s of the `record_served` and `tool_item_served` events those calls produced, at most 2000), `n_served` (the true count) and `truncated`.
Every id names an event or call **earlier in the same session**. `since_previous_decision` states what was *available* to the decision, not what the agent used; `explicit` is the agent's own statement.
A reader must draw an evidence graph from these ids and never from the order or timing of events. A decision without `links` (written before SPEC 1.2) has **no recorded links**, which is not the same as none.

**`order`**: `order_id` (unique in the session), `decision_call_id` (the `call_id` of the decision it implements, or `null`), `expected_source` (`model` or `backtest`: where `expected_price` came from) and `terms`
(raw, so `terms_sha256` in `hashes_only`): `symbol, side` (`buy`|`sell`), `size, intended_price` (the market price when the order was sent), `expected_price` (the price the model or backtest assumed it would fill at),
`stop_loss, take_profit`. Stamped `event_time = available_at = T`.

**`fill`**: `order_id`, `fill_id`, `intrabar_ambiguous` (`true`, `false`, or `null` = not assessed; see 4.7) and `terms` (raw): `price, size, fees, filled_at, reference_price` (the market price at the fill, optional).
Stamped `event_time = filled_at`, `available_at = T`; `filled_at` is never after T and never before its order, and the fills of an order never sum to more than its size.

**`position_mark`**: `terms` (raw): `symbol, size, price, marked_at`. Stamped `event_time = marked_at`, `available_at = T`, `marked_at <= T`.

Numbers live only in `terms`, so in `hashes_only` a receiver can count orders, fills and marks and follow the links, but every price, size and fee is a hash: whatever needs a value is **not synced**.

### 4.7 Bars

A bar is **available when it closes**. For a `Bars` source: `available_at` = the bar's close = `open + timeframe` (`5m`, `1h`, `1d`, `1w`), or the session close for a daily bar given `session_close` and `session_tz`, or an explicit
`close_time`. A vendor's `available_at` can only make it later; an earlier one is replaced by the close and the record is flagged `availability_moved_to_close`. `event_time` is the bar's open. A bar still forming at T has its
close after T and is withheld by R1 like any future record; rows the source marks incomplete are dropped and counted (`partial_bars_withheld`) unless the source allows partial bars, in which case they are served
flagged `partial_bar` and the query warns. Timeframes are per row, so a daily or weekly bar next to hourly bars is available only after its own close.

**Intrabar ambiguity.** When a stop loss and a take profit both lie within one bar's `[low, high]` the bar cannot say which was reached first. For a long position, both are inside when `low <= stop` and `high >= target`;
for a short, `high >= stop` and `low <= target`. `fill.intrabar_ambiguous` records the result when the caller supplies the bar (`null` otherwise), and a report labels such an exit **Observed**, never Verified.

### 4.8 The gateway block on model calls (SPEC 1.3)

`model_call` and `model_error` carry one optional key, `gateway`: an object of **names, counts, flags and settings only, never content**, so it is metadata and stays as it is in a `hashes_only` ledger. Fields (all optional for a reader; a writer of 1.3 always writes them):
`rules_version` (1); `blocked` (true on a `model_error` written because a gateway rule refused the call, before anything was sent); `rule` (on a blocked call: `server_tools_off`, `contributor_model` or `provider_state`);
`clock_T` (the session clock at the call); `provider_verified` (false unless the operator has marked the provider as run live; a mocked test never sets it); `server_tools_requested` and `server_tools_reported` (names of provider-side tools that were
asked for, and that the provider said it ran); `server_state` (`none`, or `provider_state_allowed`); `n_messages_sent` (the full conversation is sent on every call); `reasoning_effort` (a short string or null);
`tokens` (`input`, `output`, `cached`, `reasoning`, each an integer or null: null means the provider did not say); `tool_calls_requested` (how many calls the model asked the caller to make: they must be run through the tool proxy);
`pinned_version` (the version prefix the policy required, or null); `flags` (a list of `{"kind": "SERVER_TOOL" | "POLICY", ...}`).
A call with any flag is **FLAGGED**: a report cannot call a run that used provider-side tools clean. A 1.2 reader ignores `gateway` (an unknown payload key does not change how an event is hashed).

### 4.9 Intents and gate decisions (SPEC 1.3)

The chain **data seen -> model and version -> intent -> gate decision -> order -> fill** is explicit in the ledger. `Session.gate(intent, policy)` writes two events under **one `call_id`**, in one transaction:
`intent` (`intent_id`, the idempotency key; `agent_id`; `model_id` and `model_version` that produced it; `data_as_of`, the latest availability time of the data it rests on; `links`, the same structure as `decision.links`, 4.6, naming the queries,
tool calls, model calls and served events it rests on; `terms`: symbol, side, size and price, which are values and are hashed in `hashes_only` as `terms_sha256`) and `gate_decision` (`intent_id`; `decision`, one of `APPROVE`,
`HOLD`, `BLOCK`; `reasons`, a list of `{check, status, outcome}` with `status` in `pass`, `fail`, `unknown`, `not_set` and `outcome` in `APPROVE`, `HOLD`, `BLOCK`; `details`, readable sentences that can hold sizes and prices and are hashed as
`details_sha256` in `hashes_only`; `policy_sha256`, the hash of the policy that was applied). BLOCK (a rule broken that waiting will not fix) beats HOLD (not now, or not without a person) beats APPROVE, and a check that
cannot be made is `unknown` with outcome HOLD, never a pass. An `order` may carry `intent_call_id`, the `call_id` of its intent and gate decision; the library refuses to link an order to a gate decision that is not `APPROVE`.
**The gate sends nothing**: it records a decision, and any order is the agent's own action recorded afterwards. A reader of a ledger without these events (anything before 1.3) reports the gate as **not recorded**, never as passed.

## 5. Verification and the head anchor

After every write the writer exports `{"version": 1, "seq": <int>, "hash": <hex>, "recorded_at": <instant>}` to `head.json` beside the ledger (`ledger.db` -> `head.json`; `x.db` -> `x.head.json`).
It never moves backwards. Verification:

1. Read `meta.schema_version` and take the hash domain from it (`2` or `3`; anything else is unsupported). Recompute the chain from event 1: `seq` contiguous, `sha256(payload) == payload_sha256`, `prev_hash` equals the previous `hash`, `hash` recomputes (4.1). The first failure is reported with its `seq`.
2. Anchor: if a head is supplied (or `head.json` exists) its `seq` must not exceed the ledger length (else: **truncated**), and the event at that `seq` must have the anchored `hash` (else: **rewritten**).
   A missing `head.json` on a non-empty ledger is a failure (truncation cannot be ruled out). An anchor *behind* the ledger is a warning, not a failure.

**The anchor is only as trustworthy as where it is stored.** A `head.json` beside the ledger can be rewritten together with it; store a copy (`strathora head`) somewhere else you trust, and verify against *that*.

Reference verifier (standard library only). `tests/test_spec.py` runs exactly this code against real ledgers:

<!-- asof-reference-verifier:start -->
```python
import hashlib, json, sqlite3

DOMAINS = {"2": "asof-ledger/2", "3": "asof-ledger/3"}

def canon(x):                                   # canonical JSON (1.2) of a list of strings, integers and null: identical under RFC 8785 and under schema 2
    return json.dumps(x, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

def sha(text):                                  # lowercase hex SHA-256 of UTF-8 (section 1.3)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def verify(db_path, head_path=None):
    """Raises AssertionError with the reason on the first problem; returns the number of events if the chain (and anchor, if given) is sound."""
    con = sqlite3.connect(db_path)
    version = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert version in DOMAINS, f"unsupported ledger schema {version}"
    domain = DOMAINS[version]                   # the version field decides how an existing ledger is verified
    rows = con.execute(
        "SELECT seq,id,session_id,type,call_id,event_time,available_at,recorded_at,payload,payload_sha256,prev_hash,hash FROM events ORDER BY seq").fetchall()
    prev, n = "0" * 64, 0
    hashes = {}
    for seq, id_, sid, typ, cid, et, av, rec, payload, psha, prev_hash, h in rows:
        n += 1
        assert seq == n, f"sequence gap at {n}"
        assert sha(payload) == psha, f"payload altered at event {seq}"
        assert prev_hash == prev, f"broken link at event {seq}"
        assert sha(canon([domain, seq, prev_hash, id_, sid, typ, cid, et, av, rec, psha])) == h, f"event {seq} altered"
        prev = hashes[seq] = h
    if head_path is not None:
        head = json.load(open(head_path))
        assert head["seq"] <= n, f"truncated: anchor records event {head['seq']}, ledger ends at {n}"
        assert head["seq"] == 0 or hashes[head["seq"]] == head["hash"], "anchored event has a different hash: rewritten"
    return n
```
<!-- asof-reference-verifier:end -->

## 6. Receipt

A receipt is rebuilt from the events sharing a `call_id`; its `call_id` is a ledger id. Fields: `call_id, session_id, kind` (`query|tool|model|decision`), `target, as_of` (= the closing event's `event_time`),
`recorded_at, privacy, served, served_timing{declared,unknown}, blocked{future,untimestamped,superseded,outside_window}, served_ids` (record ids, or payload hashes in `hashes_only`), `quality` and `universe` (SPEC 1.2, section 4.5; `null` when
the ledger has none), `model`
(`id, provider, version_reported, declared_cutoff, declared_by` = `user|provider|unknown, `declared_cutoff_after_as_of, contamination_score, score_source, label`), `warnings, ledger_seq, ledger_hash, event_seqs, limits`.
`model.label` is `unknown`, or `observed` when a contamination score is registered; **no stronger label exists**. The *agent view* omits `blocked` (a count reveals that something exists after T), `quality` and `universe` (operator information), and `ledger_seq` and `event_seqs`:
a call that withheld something writes one more event, so a position in the ledger reveals the same thing as a count. The agent view keeps `ledger_hash`, which identifies the event without placing it.
For the same reason an agent-facing interface must not report the number of events in the ledger or the sequence number of its head (the MCP server's `verify_ledger` and `ledger_head` do not, unless the operator set `receipt_detail` to `full`).

## 7. Sync wire format (opt-in)

`POST <endpoint>` with `Authorization: Bearer <key>`, `Content-Type: application/json`, `X-AsOf-Schema: <the body's schema>`. The API key is only ever in the `Authorization` header.
Every event is sent as an envelope (all fields but `payload`): enough to verify the whole chain with section 5. `payload` is present only for events of `hashes_only` sessions (already hashes and metadata),
or of `full` sessions when the sender set `send_payloads`. The reply is `2xx` JSON `{"accepted_through": <int>, ...}`: the highest `seq` of this batch's chain that the receiver now holds. The sender resumes after it,
per endpoint. `5xx`/`429` are retried; other errors are not.

### 7.1 `asof.sync/2` (SPEC 1.1)

```json
{"schema": "asof.sync/2", "client": {"name": "asof", "version": "0.5.0", "id": "<optional>"}, "chain": {"id": "<chain id>", "label": "<optional>"},
 "ledger": {"schema_version": 3, "hash_domain": "asof-ledger/3", "canonicalization": "rfc8785"},
 "events": [{"seq": 1, "id": "...", "session_id": "...", "type": "session_start", "call_id": null, "event_time": "...", "available_at": "...", "recorded_at": "...",
             "payload_sha256": "...", "prev_hash": "...", "hash": "...", "privacy": "full|hashes_only", "payload": { } }]}
```

* **`client.id`** (optional): the identifier the receiver gave the credential's owner (its `client_id`). A receiver authenticates by key; if `client.id` is present and is not the key's owner, it refuses (403).
* **`chain.id`** (required): identifies one ledger as a chain. AsOf makes it when it creates the ledger file (a random 128-bit hex string kept in `meta.chain_id`; an older file gets one the first time it is asked for).
  It is not part of any hash. `chain.label` is a free-text name for people (a machine, a strategy). **One credential may own many chains.**
* **`ledger`**: `schema_version` 3 with `hash_domain` `asof-ledger/3` and `canonicalization` `rfc8785`, or `schema_version` 2 with `asof-ledger/2` and `asof-json-v1` (an older ledger being synced).
  Versions are never mixed inside one chain.
* **Receiver rules**, per `(client, chain id)`: the first batch for a chain id must start at `seq` 1 with a `prev_hash` of 64 zeros, and **that creates the chain; a new chain id is not a fork**. Every later batch must continue
  from the stored head of **that chain**. Resending events the receiver already holds is accepted and changes nothing; an event at a held `seq` with a different hash, or a batch that does not continue from the head,
  is a **fork of that chain** and is refused, and it is a fork only relative to the same chain id. A chain's `schema_version` is fixed by its first batch.
* The receiver stores one head per chain. A head is `{client_id, chain_id, seq, hash, recorded_at}`.

### 7.2 `asof.sync/1` (SPEC 1.0, accepted)

The body has no `client` id and no `chain`; `ledger` is `{"schema_version": 2, "hash_domain": "asof-ledger/2"}`. A receiver treats everything a credential sends under `asof.sync/1` as **one chain**, identified internally
as `default`. A credential that has used `asof.sync/1` and then sends `asof.sync/2` for its original ledger must use `chain.id = default` to continue that chain; any other chain id starts a new chain.

## 8. What this specification does not guarantee

* The truth of any timestamp. `available_at` is declared by a data owner; AsOf enforces it, and the ledger records what was enforced.
* That a model forgot anything. A model's memory is outside the lock; receipts carry the declared cutoff (and who declared it) and a label that never exceeds `observed`.
* Detection of tail truncation without a trusted copy of the head (section 5).
* Authorship. The ledger is local and unsigned: it shows what was asked and served, not who asked.

## 9. Conformance

**Test vectors and verifiers** (no AsOf code needed): `spec/vectors/jcs.json` (JSON in, canonical bytes and sha256 out, including the examples of RFC 8785 sections 3.2.2 and 3.2.3), `spec/vectors/events.json`
(event fields in, event hash out, for domains 2 and 3, plus small whole chains with payloads), and two independent implementations that check them: `verifier/verify.py` (standard library) and `verifier/verify.mjs` (Node, no
dependencies). `node verifier/verify.mjs vectors spec/vectors` and `python verifier/verify.py vectors spec/vectors` must print the same results; `tests/test_vectors.py` runs both. A new implementation is conformant when it passes the vectors.

`tests/test_spec.py` (a) extracts the reference verifier above and runs it on real ledgers, accepting good ones and rejecting tampered and truncated ones; (b) checks the schema block in 4.3 against the
events the library emits in both privacy modes; (c) checks the hash and clock rules of section 4 against stored events.

## 10. Versions

| | SPEC 1.0 (AsOf 0.2.0) | SPEC 1.1 (AsOf 0.3.0) |
|---|---|---|
| ledger schema / hash domain | 2 / `asof-ledger/2` | 3 / `asof-ledger/3` |
| canonical JSON | Python `json.dumps` rules | RFC 8785 |
| wire format | `asof.sync/1`, one implicit chain | `asof.sync/2`, `client.id` and `chain.id` |
| written by | 0.2.0 | 0.3.0 and later (SPEC 1.2, 0.4.0, and 1.3, 0.5.0, add optional payload keys only: sections 4.5 and 4.8) |

`meta.schema_version` (ledger) and `ledger.schema_version` (wire) decide how existing material is read. 0.3.0 and later open, verifies and syncs schema-2 ledgers and **refuses to append to one** (a chain never mixes
versions): keep the file for verification and start a new ledger (`strathora init --archive-legacy`). A receiver that accepts both must verify each chain under the rules of that chain's version.
