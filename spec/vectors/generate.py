"""Writes spec/vectors/*.json from the AsOf library. Deterministic: run it twice, get the same bytes. `python spec/vectors/generate.py --check` fails if the committed files are stale.

The vectors are for OTHER implementations: JSON in, expected bytes or hash out. The library produced the expected values; `verifier/verify.mjs` (Node) and `verifier/verify.py` (stdlib) check
them without importing AsOf, and tests/test_vectors.py runs both, so a value is only trusted if two or three implementations agree on it.
"""
import json
import struct
import sys
from pathlib import Path

from asof import jcs
from asof.ledger import DOMAINS, _event_hash
from asof.records import legacy_canonical_json, sha256_hex

OUT = Path(__file__).parent


def dump(name, obj):
    return name, json.dumps(obj, indent=1, ensure_ascii=False) + "\n"


def double(bits):
    return struct.unpack(">d", struct.pack(">Q", bits))[0]


def jcs_vectors():
    cases = []

    def add(name, text, note=""):
        canon = jcs.canonicalize(jcs.loads(text))
        cases.append({"name": name, "input": text, "canonical": canon, "sha256": sha256_hex(canon), **({"note": note} if note else {})})

    add("RFC 8785 section 3.2.2: numbers, literals and string escapes",
        '{"numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001], "string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/", "literals": [null, true, false]}',
        "the example printed in the RFC")
    add("RFC 8785 section 3.2.3: members sort by UTF-16 code units, not by code point",
        '{"\\u20ac": "Euro Sign", "\\r": "Carriage Return", "\\ufb33": "Hebrew Letter Dalet With Dagesh", "1": "One", "\\ud83d\\ude00": "Emoji: Grinning Face", "\\u0080": "Control", "\\u00f6": "Latin Small Letter O With Diaeresis"}',
        "U+1F600 (a surrogate pair, D83D DE00) sorts BEFORE U+FB33 in UTF-16 and after it by code point")
    for name, text in [("empty object", "{}"), ("empty array", "[]"), ("null", "null"), ("true", "true"), ("false", "false"),
                       ("nested", '{"b": [1, {"d": 4, "c": 3}], "a": {"z": [], "y": {}}}'),
                       ("whitespace is dropped", ' {  "a" : [ 1 , 2 ] ,\n "b":"x" } ')]:
        add(name, text)
    numbers = [("zero", "0"), ("negative zero is written 0", "-0.0"), ("a whole number written with .0 loses it", "2592000.0"), ("exponent form of a whole number", "1E2"),
               ("smallest denormal", "5e-324"), ("negative smallest denormal", "-5e-324"), ("largest double", "1.7976931348623157e308"),
               ("2^53", "9007199254740992"), ("2^53 + 2", "9007199254740994"), ("1e21 switches to exponent form", "1e21"), ("just below 1e21 stays digits", "999999999999999900000"),
               ("1e-6 stays decimal", "0.000001"), ("1e-7 goes exponent", "0.0000001"), ("9.999999999999997e-7", "9.999999999999997e-7"),
               ("333333333.3333332", "333333333.3333332"), ("333333333.33333325", "333333333.33333325"), ("333333333.3333333", "333333333.3333333"),
               ("333333333.3333334", "333333333.3333334"), ("333333333.33333343", "333333333.33333343"), ("0.1", "0.1"), ("4.5 written 4.50", "4.50"),
               ("largest safe integer", "9007199254740991"), ("negative safe integer", "-9007199254740991"), ("a float that is not exact", "0.30000000000000004"),
               ("1.2345678901234568e20 (a whole float)", "1.2345678901234568e20"), ("-1.5e-9", "-1.5e-9")]
    for n, t in numbers:
        add("number: " + n, "[" + t + "]")
    for b in (0x0000000000000001, 0x7fefffffffffffff, 0x444b1ae4d6e2ef50, 0x3eb0c6f7a0b5ed8d, 0x41b3de4355555555, 0xbecbf647612f3696):
        add(f"number from IEEE bits {b:016x}", "[" + repr(double(b)) + "]")
    strings = [("quotes and backslash", r'"a\"b\\c"'), ("control characters", '"\\u0000\\u0001\\b\\t\\n\\f\\r\\u001f"'), ("DEL and U+2028 stay literal", '"\\u007f\\u2028\\u2029"'),
               ("non-ASCII is UTF-8", '"caf\\u00e9 \\u00fcber \\u4e2d\\u6587"'), ("astral plane", '"\\ud83d\\ude00 \\ud835\\udd0a"'), ("solidus is not escaped", '"a\\/b"'),
               ("combining character", '"e\\u0301"')]
    for n, t in strings:
        add("string: " + n, "[" + t + "]")
    return {"description": "RFC 8785 test vectors: parse `input` as JSON, canonicalise, compare to `canonical`; sha256 is of the UTF-8 bytes of `canonical`.", "cases": cases}


def event_vectors():
    cases = []
    base = dict(seq=7, prev_hash="a" * 64, id="0123456789abcdef0123456789abcdef", session_id="s1", type="query", call_id="c1", event_time="2023-01-20T23:00:00Z",
                available_at="2023-01-20T23:00:00Z", recorded_at="2026-11-08T18:00:00.123456Z", payload_sha256="b" * 64)
    variants = [("all fields present", {}), ("null call_id", dict(call_id=None)), ("null event_time and available_at", dict(event_time=None, available_at=None)),
                ("first event, genesis prev_hash", dict(seq=1, prev_hash="0" * 64)), ("non-ASCII session id", dict(session_id="séssion-\U0001F600")),
                ("session id with quotes and a newline", dict(session_id='a"b\nc')), ("large seq", dict(seq=9007199254740991))]
    for domain in (3, 2):
        for name, over in variants:
            f = {**base, **over}
            h = _event_hash(f["seq"], f["prev_hash"], f["id"], f["session_id"], f["type"], f["call_id"], f["event_time"], f["available_at"], f["recorded_at"], f["payload_sha256"], DOMAINS[domain])
            cases.append({"name": f"schema {domain}: {name}", "schema_version": domain, "fields": f, "hash": h})
    return cases


def chain_vector(name, schema, payloads, texts=None):
    """A whole chain with payloads. Schema 3: the verifier canonicalises each payload (RFC 8785). Schema 2: the payload text is given as stored, and only its hash is checked."""
    prev, events = "0" * 64, []
    for i, (typ, payload) in enumerate(payloads, 1):
        text = jcs.canonicalize(payload) if schema == 3 else legacy_canonical_json(payload)
        psha = sha256_hex(text)
        e = dict(seq=i, id=f"{i:032x}", session_id="vector-session", type=typ, call_id=None if i == 1 else "call-1", event_time="2023-01-20T23:00:00Z",
                 available_at="2023-01-20T23:00:00Z", recorded_at="2026-11-08T18:00:00Z", payload_sha256=psha, prev_hash=prev, payload=payload)
        e["hash"] = _event_hash(i, prev, e["id"], e["session_id"], typ, e["call_id"], e["event_time"], e["available_at"], e["recorded_at"], psha, DOMAINS[schema])
        if schema == 2:
            e["payload_text"] = text
        else:
            e["payload_canonical"] = text
        events.append(e)
        prev = e["hash"]
    return {"name": name, "schema_version": schema, "hash_domain": DOMAINS[schema], "events": events}


def vectors():
    p3 = [("session_start", {"as_of": "2023-01-20T23:00:00Z", "privacy": "hashes_only", "max_as_of": None}),
          ("query", {"lookback": 2592000.0, "limit": None, "served": 3, "counts": {"served": 3, "blocked_future": 1}, "source": "prices", "warnings": []}),
          ("decision", {"action": "hôld \U0001F600", "details_sha256": "c" * 64, "z": 0.1, "a": [1, 2.5, 1e21], "\U0001F600": 1, "דּ": 2})]
    p2 = [("session_start", {"as_of": "2023-01-20T23:00:00Z", "privacy": "hashes_only"}), ("query", {"served": 3, "source": "prices", "lookback": 30, "warnings": []}),
          ("decision", {"action": "hold", "details_sha256": "c" * 64})]
    return [dump("jcs.json", jcs_vectors()),
            dump("events.json", {"description": "Event hash vectors: build the list [hash_domain, seq, prev_hash, id, session_id, type, call_id, event_time, available_at, recorded_at, payload_sha256], "
                                                "canonicalise it (RFC 8785), sha256 the UTF-8 bytes; domain is asof-ledger/<schema_version>.", "event_hash": event_vectors(),
                                 "chains": [chain_vector("schema 3 chain with payloads (floats, non-ASCII, UTF-16 key order)", 3, p3), chain_vector("schema 2 chain (SPEC 1.0)", 2, p2)]})]


if __name__ == "__main__":
    stale = []
    for name, text in vectors():
        path = OUT / name
        if "--check" in sys.argv:
            if not path.exists() or path.read_text() != text:
                stale.append(name)
        else:
            path.write_text(text)
            print("wrote", path)
    if stale:
        print("stale:", stale, "- run python spec/vectors/generate.py")
        sys.exit(1)
