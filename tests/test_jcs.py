"""RFC 8785 canonical JSON (SPEC 1.1, ledger schema 3): the cases the RFC prints, the limits we add, agreement with Node's own number formatting, and the property that makes
the event hash identical under SPEC 1.0 and 1.1."""
import json
import random
import shutil
import struct
import subprocess
from datetime import date, datetime, timezone

import pytest

from asof import canonical_json, jcs
from asof.jcs import CanonicalizationError, canonicalize, es_number, loads
from asof.records import legacy_canonical_json

NODE = shutil.which("node")


@pytest.mark.parametrize("x, want", [(0.0, "0"), (-0.0, "0"), (2592000.0, "2592000"), (1e21, "1e+21"), (1e-7, "1e-7"), (0.000001, "0.000001"), (1.5e-9, "1.5e-9"), (5e-324, "5e-324"),
                                     (1.7976931348623157e308, "1.7976931348623157e+308"), (333333333.33333329, "333333333.3333333"), (0.1 + 0.2, "0.30000000000000004"),
                                     (-4.5, "-4.5"), (123456789012345680000.0, "123456789012345680000"), (1e20, "100000000000000000000"), (12345.678, "12345.678")])
def test_numbers_are_written_as_ecmascript_writes_them(x, want):
    assert es_number(x) == want and canonicalize(x) == want


def test_the_rfc_examples_come_out_as_the_rfc_prints_them():
    text = '{"numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001], "string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/", "literals": [null, true, false]}'
    assert canonicalize(loads(text)) == '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    keys = ["€", "\r", "דּ", "1", "\U0001F600", "\u0080", "ö"]
    out = list(json.loads(canonicalize({k: 1 for k in keys})))
    assert out == ["\r", "1", "\u0080", "ö", "€", "\U0001F600", "דּ"]                         # UTF-16 order: the emoji (D83D...) sorts before U+FB33, unlike code point order
    assert sorted(keys)[-2:] == ["דּ", "\U0001F600"]                                                    # ...which is what the old rule would have done


def test_whole_floats_lose_their_point_and_the_old_form_is_kept_only_for_reading_old_ledgers():
    assert canonicalize({"lookback": 2592000.0}) == '{"lookback":2592000}'
    assert legacy_canonical_json({"lookback": 2592000.0}) == '{"lookback":2592000.0}'                    # SPEC 1.0: why another language could not reproduce it
    assert canonical_json({"b": 1, "a": [2.0, 1e21]}) == '{"a":[2,1e+21],"b":1}'


def test_what_cannot_be_written_is_refused_not_guessed():
    for bad in (2**53, -(2**53), float("nan"), float("inf"), "\ud800", {1: "a"}, {"a": object()}):
        with pytest.raises((CanonicalizationError, TypeError)):
            canonicalize(bad)
    assert canonicalize(2**53 - 1) == "9007199254740991"
    with pytest.raises(CanonicalizationError, match="lone surrogate"):
        canonicalize({"\ud800": 1})
    with pytest.raises(CanonicalizationError, match="write it as a string"):
        canonical_json({"id": 12345678901234567890})
    # reading is different: JSON from another implementation holds doubles, so a big integer is read as the nearest double
    assert loads("[9007199254740993, 1]") == [9007199254740992.0, 1] and canonicalize(loads("[9007199254740993]")) == "[9007199254740992]"


def test_python_values_are_normalised_before_canonicalising():
    d = datetime(2023, 1, 20, 23, 0, tzinfo=timezone.utc)
    assert canonical_json({"t": d, "d": date(2023, 1, 20), "s": {3, 1, 2}, "n": (1, 2), 7: "x", "b": b"ab"}) == (
        '{"7":"x","b":{"__bytes_sha256__":"fb8e20fc2e4c3f248c60c39bd652f3c1347298bb977b8b4d5903b85055620603"},"d":"2023-01-20","n":[1,2],"s":[1,2,3],"t":"2023-01-20T23:00:00Z"}')
    assert canonical_json({"t": datetime(2023, 1, 1)}) == '{"t":{"__naive_datetime__":"2023-01-01T00:00:00"}}'     # never silently UTC


def test_the_event_hash_list_is_the_same_under_both_canonical_forms():
    """The list holds only strings, integers and null, so SPEC 1.0 and 1.1 agree on it: the domain string, not the canonical form, tells the versions apart."""
    rng = random.Random(8785)
    pool = "abcXYZ019 _-\"\\\n\té中\U0001F600 \u007f\u0000"
    for _ in range(2000):
        lst = [rng.choice(["asof-ledger/2", "asof-ledger/3"]), rng.randrange(1, 2**53), "".join(rng.choice(pool) for _ in range(rng.randrange(0, 12))), None,
               "".join(rng.choice(pool) for _ in range(rng.randrange(0, 12)))]
        assert legacy_canonical_json(lst) == canonicalize(lst)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_number_formatting_agrees_with_nodes_for_many_doubles():
    rng = random.Random(1)
    xs = [struct.unpack(">d", struct.pack(">Q", rng.getrandbits(64)))[0] for _ in range(20000)] + [10.0**e * m for e in range(-30, 31) for m in (1, 1.5, 9.999999, 0.1)]
    xs = [x for x in xs if x == x and abs(x) != float("inf")]
    out = subprocess.run([NODE, "-e", "const a=JSON.parse(require('fs').readFileSync(0,'utf8'));console.log(JSON.stringify(a.map(x=>Object.is(x,-0)?'0':String(x))))"],
                         input=json.dumps(xs), capture_output=True, text=True, check=True).stdout
    assert [es_number(x) for x in xs] == json.loads(out)


def test_module_docstring_and_spec_agree_on_the_name():
    assert "RFC 8785" in (jcs.__doc__ or "") and "RFC 8785" in open(__file__.replace("tests/test_jcs.py", "SPEC.md")).read()
