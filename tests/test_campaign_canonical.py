"""Pre-launch campaign, area C, item 13: canonical JSON (RFC 8785), the thing every hash in the ledger rests on.

* random JSON values (Hypothesis): the Python canonical form and its SHA-256 against the Node verifier, which shares no code with the library
* key order, Unicode normalisation (there is none, on purpose), the float edge cases, and what is refused
* the same event gives the same hash whatever the process's hash seed, locale or time zone

"Same hash on macOS and Linux" could not be run on Linux in the campaign (no Linux machine or container was available). What stands in for it here: an independent implementation in
another language on the same machine, and the committed vectors in spec/vectors, which any platform can check with `python verifier/verify.py vectors spec/vectors`.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from asof import Ledger
from asof.jcs import CanonicalizationError, canonicalize
from asof.ledger import HASH_DOMAIN, _event_hash
from asof.records import canonical_json, payload_sha256

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings          # noqa: E402
from hypothesis import strategies as st                      # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
N = int(os.environ.get("ASOF_PROPERTY_EXAMPLES", "200"))
BATCH = 40

NODE_PROGRAM = """
import { canonicalize } from %s
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
const values = JSON.parse(readFileSync(0, 'utf8'))
console.log(JSON.stringify(values.map((v) => { const c = canonicalize(v); return [c, createHash('sha256').update(c, 'utf8').digest('hex')] })))
""" % json.dumps((ROOT / "verifier" / "verify.mjs").as_uri())


def node(values):
    out = subprocess.run([NODE, "--input-type=module", "-e", NODE_PROGRAM], input=json.dumps(values, ensure_ascii=True, allow_nan=False), capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[:2000]
    return json.loads(out.stdout)


_scalars = st.one_of(st.none(), st.booleans(), st.integers(-(2**53 - 1), 2**53 - 1), st.floats(allow_nan=False, allow_infinity=False), st.text(max_size=30),
                     st.sampled_from([0.0, -0.0, 1e21, 1e20, 1e-7, 1e-6, 5e-324, 1.7976931348623157e308, 0.1 + 0.2, 1 / 3, 2.5e-5, 123456789012345680000.0, 4.5, -4.5, 1e300 * 10]),
                     st.sampled_from(["", "\u0000", "\u001f", "\u007f", " ", " ", "\"", "\\", "/", "é", "é", "\U0001f600", "דּ", "€", "퟿", "﻿", "\r\n\t\b\f"]))
json_values = st.recursive(_scalars, lambda inner: st.one_of(st.lists(inner, max_size=5), st.dictionaries(st.text(max_size=12), inner, max_size=5)), max_leaves=25)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@settings(max_examples=max(5, N // BATCH), deadline=None, database=None, suppress_health_check=list(HealthCheck))
@given(st.lists(json_values, min_size=BATCH, max_size=BATCH))
def test_random_json_values_have_the_same_canonical_form_and_hash_in_python_and_in_node(values):
    theirs = node(values)
    for v, (canon, digest) in zip(values, theirs, strict=True):
        mine = canonicalize(v)
        assert mine == canon, (v, mine, canon)
        assert hashlib.sha256(mine.encode("utf-8")).hexdigest() == digest == payload_sha256(v)
        assert json.loads(mine, parse_int=float) == json.loads(json.dumps(v), parse_int=float)           # and it is still the same value when read back (numbers read as doubles, as RFC 8785 does:
                                                                                                          # a whole double above 2**53 is written as plain digits, shortest that give the same double)


def test_object_members_are_sorted_by_utf16_code_units_not_by_code_point_and_not_by_locale():
    keys = ["€", "\r", "דּ", "1", "\U0001f600", "\u0080", "ö", "A", "a", "B", "", "aa", "퟿", ""]
    obj = {k: i for i, k in enumerate(keys)}
    order = list(json.loads(canonicalize(obj)))
    assert order == sorted(keys, key=lambda k: k.encode("utf-16-be"))
    assert order.index("\U0001f600") < order.index("") < order.index("דּ")                    # an astral character (a surrogate pair, 0xD83D...) sorts BEFORE U+E000: the difference from code-point order
    assert sorted(keys).index("\U0001f600") > sorted(keys).index("דּ")                               # (code-point order, which Python's own sort uses, would put it last)
    shuffled = dict(reversed(list(obj.items())))
    assert canonicalize(shuffled) == canonicalize(obj)                                                    # insertion order never matters
    nested = {"b": {"z": 1, "a": [{"y": 1, "x": 2}]}, "a": 0}
    assert canonicalize(nested) == '{"a":0,"b":{"a":[{"x":2,"y":1}],"z":1}}'
    if NODE:
        assert node([obj, nested]) == [[canonicalize(obj), payload_sha256(obj)], [canonicalize(nested), payload_sha256(nested)]]


def test_strings_are_not_unicode_normalised_two_spellings_of_the_same_letter_are_two_different_payloads():
    composed, decomposed = "café", "café"                                                    # they look the same; they are different sequences of characters
    assert composed != decomposed
    assert canonicalize(composed) != canonicalize(decomposed) and payload_sha256({"name": composed}) != payload_sha256({"name": decomposed})
    assert json.loads(canonicalize(decomposed)) == decomposed                                             # nothing was changed on the way through
    both = {composed: 1, decomposed: 2}
    assert len(json.loads(canonicalize(both))) == 2                                                       # and as keys they stay two keys
    for s in ("﻿bom", "tab\there", "nul\u0000", "line sep", "ｆｕｌｌｗｉｄｔｈ", "​zero-width"):
        assert json.loads(canonicalize(s)) == s


@pytest.mark.parametrize("x, text", [(0.0, "0"), (-0.0, "0"), (1.0, "1"), (-1.0, "-1"), (1e21, "1e+21"), (1e20, "100000000000000000000"), (999999999999999900000.0, "999999999999999900000"),
                                     (1e-6, "0.000001"), (1e-7, "1e-7"), (5e-324, "5e-324"), (1.7976931348623157e308, "1.7976931348623157e+308"), (0.1 + 0.2, "0.30000000000000004"),
                                     (2.0**53, "9007199254740992"), (123.456, "123.456"), (1e23, "1e+23"), (4.35, "4.35"), (0.000001234, "0.000001234"), (1234567.0, "1234567")])
def test_float_edge_cases(x, text):
    assert canonicalize(x) == text
    assert canonicalize([x]) == f"[{text}]" and canonicalize({"v": x}) == f'{{"v":{text}}}'
    assert float(text) == x                                                                               # the text reads back as exactly the same number


def test_what_has_no_canonical_form_is_refused_and_never_reaches_the_ledger(tmp_path):
    for bad in (float("nan"), float("inf"), float("-inf"), 2**53, -(2**53), 10**30, "\ud800", {"k": "\udfff"}, {1: "int key"}, {None: 1}, object(), b"bytes", {"s": {1, 2}}):
        with pytest.raises((CanonicalizationError, TypeError)):
            canonicalize(bad)
    led = Ledger(tmp_path / "ledger.db")
    T = datetime(2023, 1, 21, 14, tzinfo=timezone.utc)
    led.append("s", "ok", event_time=T, available_at=T, payload={"v": 1})
    for bad in ({"v": float("nan")}, {"v": float("inf")}, {"v": 2**60}, {"v": "\ud800"}):
        with pytest.raises((CanonicalizationError, TypeError, ValueError)):
            led.append("s", "bad", event_time=T, available_at=T, payload=bad)
    assert len(led) == 1 and led.verify().ok and led.verify().anchor == "ok"                              # the refused events left no trace and broke nothing
    led.append("s", "ok", event_time=T, available_at=T, payload={"v": 2, "when": T, "tags": {"b", "a"}, "blob": b"\x00\x01"})      # what CAN be normalised first (times, sets, bytes) is
    assert json.loads(led.events()[-1].payload) == {"blob": {"__bytes_sha256__": hashlib.sha256(b"\x00\x01").hexdigest()}, "tags": ["a", "b"], "v": 2, "when": "2023-01-21T14:00:00Z"}
    assert led.verify().ok


PROBE = """
import json, sys
from datetime import datetime, timezone
from asof.ledger import HASH_DOMAIN, _event_hash
from asof.records import canonical_json, payload_sha256
payload = {"z": [1, 2.5, -0.0, 1e21, 1e-7], "a": {"\\u00e9": "caf\\u00e9", "e\\u0301": None, "\\U0001f600": True}, "set": {"b", "a", "c"}, "when": datetime(2023, 1, 21, 14, 0, 0, 123456, tzinfo=timezone.utc),
           "n": 9007199254740991, "s": "line\\nbreak \\u2028 tab\\t"}
c = canonical_json(payload)
print(json.dumps([c, payload_sha256(payload), _event_hash(7, "0" * 64, "id", "session", "query", "call", "2023-01-21T14:00:00Z", None, "2026-11-11T00:00:00.000001Z", payload_sha256(payload), HASH_DOMAIN)]))
"""


def test_the_same_event_has_the_same_hash_whatever_the_hash_seed_locale_or_time_zone():
    results = set()
    for seed, lang, tz in (("0", "C", "UTC"), ("1", "en_US.UTF-8", "America/New_York"), ("12345", "de_DE.UTF-8", "Asia/Kolkata"), ("random", "tr_TR.UTF-8", "Pacific/Auckland"), ("4294967295", "POSIX", "Africa/Lagos")):
        env = {"PYTHONPATH": str(ROOT), "PYTHONHASHSEED": seed, "LANG": lang, "LC_ALL": lang, "TZ": tz, "PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}
        r = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, r.stderr
        results.add(r.stdout.strip())
    assert len(results) == 1, results
    canon, psha, ehash = json.loads(results.pop())
    assert canon.startswith('{"a":{"é":null,"é":"café","\U0001f600":true},"n":9007199254740991,"s":')      # "e" + accent (0x65...) sorts before the single letter (0xE9) and '"set":["a","b","c"]' in canon and '"z":[1,2.5,0,1e+21,1e-7]' in canon
    assert psha == hashlib.sha256(canon.encode("utf-8")).hexdigest()
    assert ehash == _event_hash(7, "0" * 64, "id", "session", "query", "call", "2023-01-21T14:00:00Z", None, "2026-11-11T00:00:00.000001Z", psha, HASH_DOMAIN)
    if NODE:                                                                                              # and the other implementation agrees on the payload, byte for byte
        assert node([json.loads(canon)]) == [[canon, psha]]
    assert canonical_json(json.loads(canon)) == canon                                                     # canonicalising the canonical form changes nothing
