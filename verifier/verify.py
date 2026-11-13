#!/usr/bin/env python3
"""AsOf reference verifier, Python standard library only. It does NOT import `asof`: it is the second, independent implementation next to verify.mjs.

    python verify.py vectors ../spec/vectors     check every test vector; exit 1 on any difference
    python verify.py body batch.json             check the hash chain inside an asof.sync/2 (or /1) request body
"""
import hashlib
import json
import math
import sys
from decimal import Decimal

DOMAINS = {2: "asof-ledger/2", 3: "asof-ledger/3"}
MAX_SAFE = 2**53 - 1


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def es_number(x):
    if x == 0:
        return "0"
    d = Decimal(repr(abs(x)))
    digits = "".join(map(str, d.as_tuple().digits)).rstrip("0") or "0"
    k, n = len(digits), len(d.as_tuple().digits) + d.as_tuple().exponent
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        body = digits[0] + ("." + digits[1:] if k > 1 else "") + ("e+" if e >= 0 else "e-") + str(abs(e))
    return ("-" if x < 0 else "") + body


def canon(v):
    """RFC 8785 for what JSON parsed with doubles gives."""
    if v is None:
        return "null"
    if v is True or v is False:
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, int):
        assert abs(v) <= MAX_SAFE, "integer outside +-(2^53-1)"
        return str(v)
    if isinstance(v, float):
        assert math.isfinite(v)
        return es_number(v)
    if isinstance(v, list):
        return "[" + ",".join(canon(x) for x in v) + "]"
    return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canon(v[k]) for k in sorted(v, key=lambda k: k.encode("utf-16-be"))) + "}"


def loads(text):
    return json.loads(text, parse_int=lambda t: int(t) if abs(int(t)) <= MAX_SAFE else float(t))


def event_hash(domain, e):
    return sha(canon([domain, e["seq"], e["prev_hash"], e["id"], e["session_id"], e["type"], e["call_id"], e["event_time"], e["available_at"], e["recorded_at"], e["payload_sha256"]]))


def verify_events(events, schema, prev="0" * 64, expect=1):
    if schema not in DOMAINS:
        return None, f"unsupported schema {schema}"
    for e in events:
        if e["seq"] != expect:
            return e["seq"], f"expected event {expect}"
        if e["prev_hash"] != prev:
            return e["seq"], "prev_hash does not match the previous event"
        if schema == 3 and "payload" in e and sha(canon(e["payload"])) != e["payload_sha256"]:
            return e["seq"], "payload does not match payload_sha256 (RFC 8785)"
        if schema == 2 and "payload_text" in e and sha(e["payload_text"]) != e["payload_sha256"]:
            return e["seq"], "payload text does not match payload_sha256"
        if event_hash(DOMAINS[schema], e) != e["hash"]:
            return e["seq"], "event hash does not match its fields"
        prev, expect = e["hash"], expect + 1
    return None


def check_vectors(d):
    bad = 0
    jc = json.load(open(f"{d}/jcs.json"))
    b = bad
    for c in jc["cases"]:
        got = canon(loads(c["input"]))
        if got != c["canonical"] or sha(got) != c["sha256"]:
            bad += 1
            print(f"FAIL  jcs: {c['name']}\n   got      {got}\n   expected {c['canonical']}")
    print(("FAIL" if bad > b else "PASS") + f"  jcs: {len(jc['cases'])} cases")
    ev = json.load(open(f"{d}/events.json"))
    b = bad
    for c in ev["event_hash"]:
        if event_hash(DOMAINS[c["schema_version"]], c["fields"]) != c["hash"]:
            bad += 1
            print(f"FAIL  event hash: {c['name']}")
    print(("FAIL" if bad > b else "PASS") + f"  event_hash: {len(ev['event_hash'])} cases")
    b = bad
    for ch in ev["chains"]:
        r = verify_events(ch["events"], ch["schema_version"])
        if r:
            bad += 1
            print(f"FAIL  chain \"{ch['name']}\": event {r[0]}: {r[1]}")
        if ch["schema_version"] == 3:
            for e in ch["events"]:
                if canon(e["payload"]) != e["payload_canonical"]:
                    bad += 1
                    print(f"FAIL  chain \"{ch['name']}\": event {e['seq']} canonical payload differs")
    print(("FAIL" if bad > b else "PASS") + f"  chains: {len(ev['chains'])} ({', '.join(str(len(c['events'])) for c in ev['chains'])} events)")
    return 1 if bad else 0


def check_body(path):
    body = loads(open(path).read())
    schema = body.get("ledger", {}).get("schema_version")
    evs = body.get("events") or []
    if not evs:
        print("FAIL  no events")
        return 1
    r = verify_events(evs, schema, evs[0]["prev_hash"], evs[0]["seq"])
    print(f"FAIL  event {r[0]}: {r[1]}" if r else f"PASS  {len(evs)} events, schema {schema}, chain {body.get('chain', {}).get('id', '(none: asof.sync/1)')}, events {evs[0]['seq']}-{evs[-1]['seq']}")
    return 1 if r else 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 2 and a[0] == "vectors":
        sys.exit(check_vectors(a[1]))
    if len(a) == 2 and a[0] == "body":
        sys.exit(check_body(a[1]))
    print("usage: verify.py vectors <dir> | verify.py body <file>", file=sys.stderr)
    sys.exit(2)
