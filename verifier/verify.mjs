#!/usr/bin/env node
// AsOf reference verifier in Node (no dependencies). It shares no code with the Python library: if it agrees with spec/vectors, the format is language-neutral.
//
//   node verify.mjs vectors ../spec/vectors     check every test vector (JCS, event hashes, whole chains); exit 1 on any difference
//   node verify.mjs body batch.json             check the hash chain inside an asof.sync/2 (or /1) request body
//
// Canonical JSON is RFC 8785 (ledger schema 3). Schema 2 (SPEC 1.0) payloads were canonicalised with Python's number formatting, which cannot be reproduced here in general,
// so for schema 2 the event hash and the hash of the payload text AS STORED are checked, not a re-canonicalisation.
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'

const sha256 = (text) => createHash('sha256').update(text, 'utf8').digest('hex')

/** RFC 8785. JSON.stringify writes numbers as Number.prototype.toString (what the RFC specifies) and strings with the escapes the RFC requires. */
export function canonicalize(v) {
  if (v === null) return 'null'
  switch (typeof v) {
    case 'boolean': return v ? 'true' : 'false'
    case 'number':
      if (!Number.isFinite(v)) throw new Error('NaN and infinity are not JSON')
      return Object.is(v, -0) ? '0' : String(v)
    case 'string': return JSON.stringify(v)
    case 'object':
      if (Array.isArray(v)) return '[' + v.map(canonicalize).join(',') + ']'
      // default sort() compares UTF-16 code units, which is exactly the order RFC 8785 requires
      return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + canonicalize(v[k])).join(',') + '}'
    default: throw new Error(`cannot canonicalise ${typeof v}`)
  }
}

export const eventHash = (domain, e) =>
  sha256(canonicalize([domain, e.seq, e.prev_hash, e.id, e.session_id, e.type, e.call_id, e.event_time, e.available_at, e.recorded_at, e.payload_sha256]))

const DOMAINS = { 2: 'asof-ledger/2', 3: 'asof-ledger/3' }

/** Verify a list of wire/vector events of one chain. Returns null if sound, else {seq, reason}. `schema` is 2 or 3. */
export function verifyEvents(events, schema, prev = '0'.repeat(64), expectSeq = 1) {
  if (!DOMAINS[schema]) return { seq: null, reason: `unsupported schema ${schema}` }
  for (const e of events) {
    if (e.seq !== expectSeq) return { seq: e.seq, reason: `expected event ${expectSeq}` }
    if (e.prev_hash !== prev) return { seq: e.seq, reason: 'prev_hash does not match the previous event' }
    if (schema === 3 && e.payload !== undefined && sha256(canonicalize(e.payload)) !== e.payload_sha256) return { seq: e.seq, reason: 'payload does not match payload_sha256 (RFC 8785)' }
    if (schema === 2 && e.payload_text !== undefined && sha256(e.payload_text) !== e.payload_sha256) return { seq: e.seq, reason: 'payload text does not match payload_sha256' }
    if (eventHash(DOMAINS[schema], e) !== e.hash) return { seq: e.seq, reason: 'event hash does not match its fields' }
    prev = e.hash
    expectSeq++
  }
  return null
}

function checkVectors(dir) {
  const read = (n) => JSON.parse(readFileSync(`${dir}/${n}`, 'utf8'))
  let bad = 0
  const fail = (what) => { bad++; console.log(`FAIL  ${what}`) }
  const jcs = read('jcs.json')
  let n = 0
  for (const c of jcs.cases) {
    n++
    let got
    try { got = canonicalize(JSON.parse(c.input)) } catch (e) { fail(`jcs: ${c.name}: ${e.message}`); continue }
    if (got !== c.canonical) fail(`jcs: ${c.name}\n   got      ${got}\n   expected ${c.canonical}`)
    else if (sha256(got) !== c.sha256) fail(`jcs sha256: ${c.name}`)
  }
  console.log(`${bad ? 'FAIL' : 'PASS'}  jcs: ${n} cases`)
  const ev = read('events.json')
  const b0 = bad
  for (const c of ev.event_hash) if (eventHash(DOMAINS[c.schema_version], c.fields) !== c.hash) fail(`event hash: ${c.name}`)
  console.log(`${bad > b0 ? 'FAIL' : 'PASS'}  event_hash: ${ev.event_hash.length} cases`)
  const b1 = bad
  for (const ch of ev.chains) {
    const r = verifyEvents(ch.events, ch.schema_version)
    if (r) fail(`chain "${ch.name}": event ${r.seq}: ${r.reason}`)
    if (ch.schema_version === 3) for (const e of ch.events) if (canonicalize(e.payload) !== e.payload_canonical) fail(`chain "${ch.name}": event ${e.seq} canonical payload differs`)
  }
  console.log(`${bad > b1 ? 'FAIL' : 'PASS'}  chains: ${ev.chains.length} (${ev.chains.map((c) => c.events.length).join(', ')} events)`)
  return bad ? 1 : 0
}

function checkBody(file) {
  const b = JSON.parse(readFileSync(file, 'utf8'))
  const schema = b.ledger?.schema_version
  if (!b.events?.length) { console.log('FAIL  no events'); return 1 }
  const first = b.events[0]
  const r = verifyEvents(b.events, schema, first.prev_hash, first.seq)
  console.log(r ? `FAIL  event ${r.seq}: ${r.reason}` : `PASS  ${b.events.length} events, schema ${schema}, chain ${b.chain?.id ?? '(none: asof.sync/1)'}, events ${first.seq}-${b.events.at(-1).seq}`)
  if (schema === 2) console.log('note  schema-2 payloads are not re-canonicalised here (SPEC 1.0 number formatting); event hashes and links were checked')
  return r ? 1 : 0
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const [cmd, arg] = process.argv.slice(2)
  if (cmd === 'vectors' && arg) process.exit(checkVectors(arg))
  if (cmd === 'body' && arg) process.exit(checkBody(arg))
  console.error('usage: node verify.mjs vectors <dir> | node verify.mjs body <file>')
  process.exit(2)
}
