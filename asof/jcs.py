"""RFC 8785 JSON Canonicalization Scheme (JCS), the canonical form of ledger schema 3 (SPEC 1.1).

Why: the v1.0 form was "what Python's ``json.dumps`` prints", so another language could not reproduce a hash that covered a float (``2592000.0`` is ``2592000`` in
JavaScript). RFC 8785 fixes the bytes for every JSON value, and implementations exist in most languages. This module is the Python one; ``verifier/verify.mjs`` is a Node one;
``spec/vectors`` is the shared set of test vectors.

The rules, in short: no whitespace; object members sorted by the UTF-16 code units of their names; strings escaped only where JSON requires it (``\\b \\f \\n \\r \\t``, other control
characters as lowercase ``\\u00xx``, ``"`` and ``\\``), everything else as UTF-8; numbers written as ECMAScript's ``Number.prototype.toString`` writes them (shortest digits that round-trip,
no ``.0`` on whole numbers, ``e+21`` and ``e-7`` style exponents, ``-0`` is ``0``).

Two limits that come with I-JSON (RFC 7493), which JCS builds on, and that this module enforces by raising :class:`CanonicalizationError` rather than guessing:
integers beyond +-(2**53 - 1) cannot be written exactly as a JSON number (put them in a string), and NaN, infinity and lone surrogates are not JSON.
"""
from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import Any

MAX_SAFE_INT = 2**53 - 1


class CanonicalizationError(ValueError):
    """The value has no RFC 8785 form (a number outside I-JSON, a non-finite float, a lone surrogate, a non-string key)."""


def es_number(x: float) -> str:
    """A finite float as ECMAScript's Number::toString writes it (RFC 8785 section 3.2.2.3)."""
    if not math.isfinite(x):
        raise CanonicalizationError("NaN and infinity cannot be written as JSON")
    if x == 0:
        return "0"                                              # also -0
    sign = "-" if x < 0 else ""
    t = Decimal(repr(abs(x))).as_tuple()                       # repr gives the shortest digits that round-trip
    exponent = t.exponent
    assert isinstance(exponent, int)                           # finite, so never 'n', 'N' or 'F'
    digits = "".join(map(str, t.digits)).rstrip("0") or "0"
    k = len(digits)
    n = len(t.digits) + exponent                               # value = 0.DIGITS * 10**n
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        mant = digits[0] + ("." + digits[1:] if k > 1 else "")
        body = f"{mant}e{'+' if e >= 0 else '-'}{abs(e)}"
    return sign + body


def _string(s: str) -> str:
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        raise CanonicalizationError("a string contains a lone surrogate, which is not valid JSON text") from None
    return json.dumps(s, ensure_ascii=False)                    # escapes exactly what RFC 8785 requires; DEL, U+2028 and U+2029 stay literal, as in ECMAScript


def canonicalize(value: Any) -> str:
    """The RFC 8785 canonical form of a JSON-compatible value (dict with str keys, list/tuple, str, bool, None, int, float)."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            raise CanonicalizationError(f"the integer {value} is outside +-(2**53-1) and cannot be a JSON number under RFC 8785; write it as a string")
        return str(value)
    if isinstance(value, float):
        return es_number(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonicalize(v) for v in value) + "]"
    if isinstance(value, dict):
        for k in value:
            if not isinstance(k, str):
                raise CanonicalizationError(f"object keys must be strings, got {type(k).__name__}")
        items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-16-be", "surrogatepass"))      # UTF-16 code unit order, as the RFC requires
        return "{" + ",".join(_string(k) + ":" + canonicalize(v) for k, v in items) + "}"
    raise CanonicalizationError(f"cannot canonicalise a {type(value).__name__}")


def loads(text: str | bytes) -> Any:
    """Parse JSON the way RFC 8785 reads numbers: as IEEE-754 doubles. An integer beyond +-(2**53 - 1) becomes the nearest double (what a JavaScript parser gives too), so reading
    never fails, while :func:`canonicalize` still refuses to *write* a Python ``int`` that large. Use this to re-canonicalise JSON that arrived from another implementation."""
    return json.loads(text, parse_int=lambda t: int(t) if abs(int(t)) <= MAX_SAFE_INT else float(t))
