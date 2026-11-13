"""Keeping credentials out of everything AsOf writes and returns (rule R7).

Two layers:

* :func:`redact` is applied to **every** ledger event in every privacy mode. It removes values under credential-like keys, strings with a recognisable credential shape (provider keys,
  bearer and basic headers, tokens, JWTs, private-key blocks, a password inside a URL, ``password=...``), and any **registered secret** that appears whole.
* :func:`scrub` is applied to error text (a provider's error body, an exception from a tool or a source) before it is raised, returned or logged. It does all of the above and also
  looks for registered secrets in the forms a careless server repeats them in: URL-encoded, base64 (at any alignment), hex, broken over lines, or "masked" with only the middle
  replaced (``sk-proj-****wxyz`` still shows real characters of the key).

A **registered secret** is a value AsOf has been told is secret: the model gateway registers every key it sends, the sync client registers its key, and you can add your own with
:func:`register_secret` (a tool's API key, a database password). A shape can be recognised without help; an opaque value can only be removed by knowing it.

Limits, stated plainly: a transformation not listed above (a reversed key, a hash of it, a different encoding) is not recognised; a fragment shorter than eight characters of a key that is
not next to a mask is not recognised; and text AsOf never sees (your own tool printing its key) is not its to protect.
"""
from __future__ import annotations

import base64
import re
import threading
import urllib.parse
from functools import lru_cache
from typing import Any, Iterable

REDACTED = "[REDACTED]"
MIN_SECRET = 8                         # shorter values are not registered: removing every occurrence of a short string would damage ordinary text
_WINDOW = 8                            # a run of this many consecutive characters of a secret (in any of its forms) is enough to remove the token it sits in
_DEEP_LIMIT = 1_000_000                # text longer than this is only checked for whole secrets and shapes (the fragment scan is linear, but not free)

_SECRET_KEY = re.compile(r"(api[-_ ]?key|apikey|authorization|auth[-_ ]?token|access[-_ ]?token|secret|password|passwd|bearer|x-api-key|private[-_ ]?key)", re.IGNORECASE)
_SHAPES = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"      # a private-key block, whole
    r"|sk-[A-Za-z0-9_\-]{12,}"                                                                # provider keys
    r"|(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{12,}"
    r"|Bearer\s+[A-Za-z0-9._\-+/=]{8,}"
    r"|Basic\s+[A-Za-z0-9+/=]{12,}"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9\-]{10,}"
    r"|\bAIza[0-9A-Za-z_\-]{30,}"
    r"|\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})")                   # a JWT
_URL_PASSWORD = re.compile(r"(://[^/\s:@'\"<>]*:)[^/\s@'\"<>]+(@)")                             # scheme://user:PASSWORD@host
_ASSIGNMENT = re.compile(r"(?i)(\b(?:api[_-]?key|x-api-key|access[_-]?token|auth[_-]?token|client[_-]?secret|secret|password|passwd|token)\b[\"']?\s*[:=]\s*[\"']?)([^\s\"',;&<>]{8,})")
_MASK = r"(?:\*{3,}|\.{3,}|…+|•{3,}|[Xx]{4,}|#{3,}|-{4,}|\[\.{3}\]|<redacted>|<hidden>|<masked>)"
_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-+/=%.~*")

_known: dict[str, None] = {}           # insertion-ordered set of registered secrets
_lock = threading.Lock()


def register_secret(value: Any) -> None:
    """Tell AsOf that ``value`` is a secret: from now on (in this process) it is removed wherever it appears in a ledger event or an error, whole or in the forms :func:`scrub` knows.
    Values shorter than 8 characters, and anything that is not a string, are ignored. The gateway and the sync client call this for the keys they use."""
    if isinstance(value, str) and len(value) >= MIN_SECRET and value not in _known:
        with _lock:
            _known[value] = None
        _forms_of.cache_clear()


def known_secrets() -> tuple[str, ...]:
    return tuple(_known)


def forget_secrets() -> None:
    """Empty the registry (for tests)."""
    with _lock:
        _known.clear()
    _forms_of.cache_clear()


def _shapes(text: str) -> str:
    text = _SHAPES.sub(REDACTED, text)
    text = _URL_PASSWORD.sub(lambda m: m.group(1) + REDACTED + m.group(2), text)
    return _ASSIGNMENT.sub(lambda m: m.group(1) + REDACTED, text)


def redact(obj: Any) -> Any:
    """Replace anything that looks like a credential, and every registered secret that appears whole. Applied to every event in every privacy mode, as the last line of defence (R7)."""
    if isinstance(obj, dict):
        return {k: (REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        out = _shapes(obj)
        for s in _known:
            if s in out:
                out = out.replace(s, REDACTED)
        return out
    return obj


@lru_cache(maxsize=256)
def _forms_of(secret: str) -> tuple[frozenset[str], Any]:
    """For one secret: every 8-character window of each form it may be repeated in, and a pattern for provider-style masks of it."""
    raw = secret.encode("utf-8", "ignore")
    forms = {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret), raw.hex(), raw.hex().upper()}
    for pad in (b"", b"\x00", b"\x00\x00"):                                    # base64 depends on how many bytes precede the secret (modulo 3): take all three alignments,
        for enc in (base64.b64encode, base64.urlsafe_b64encode):               # without the first group (it mixes in the preceding bytes) and the last few characters (the following ones)
            text = enc(pad + raw).decode().rstrip("=")
            core = text[4 if pad else 0:]
            forms.add(core[:-3] if len(core) > _WINDOW + 3 else core)
    windows = frozenset(f[i:i + _WINDOW] for f in forms if len(f) >= _WINDOW for i in range(len(f) - _WINDOW + 1))
    n = min(len(secret), 48)
    heads = "|".join(re.escape(secret[:k]) for k in range(n, 2, -1))            # the first 3 or more characters ...
    tails = "|".join(re.escape(secret[-k:]) for k in range(n, 2, -1))           # ... or the last 3 or more, next to a mask
    with_tail = re.compile(rf"(?<![A-Za-z0-9])(?:{heads})?{_MASK}(?:{tails})")
    head_only = re.compile(rf"(?<![A-Za-z0-9])(?:{heads}){_MASK}")
    return windows, (with_tail, head_only)


def scrub(text: Any, extra: Iterable[Any] = ()) -> Any:
    """Remove credentials from a piece of text that came from outside (an error body, an exception message): :func:`redact`, plus every registered secret (and those in ``extra``) in
    encoded, split or masked form. Returns non-strings unchanged."""
    if not isinstance(text, str) or not text:
        return text
    secrets = [s for s in (*_known, *extra) if isinstance(s, str) and len(s) >= MIN_SECRET]
    for s in secrets:
        if s in text:
            text = text.replace(s, REDACTED)
    if secrets and len(text) <= _DEEP_LIMIT:
        windows: set[str] = set()
        forms = [_forms_of(s) for s in secrets]
        for w, _ in forms:
            windows |= w
        # Masks in two passes over ALL the secrets. Keys of one provider begin alike ("sk-ant-..."), so the beginning of a masked key matches every one of them: if each secret were
        # tried once, in turn, an earlier secret would take "head + mask" and leave the last characters of the key that was really masked. First every mask that ends in a known tail;
        # only then a head and a mask with nothing recognisable after it.
        for _, (with_tail, _h) in forms:
            text = with_tail.sub(REDACTED, text)
        for _, (_t, head_only) in forms:
            text = head_only.sub(REDACTED, text)
        text = _remove_fragments(text, windows)
    return redact(text)


def _remove_fragments(text: str, windows: set[str]) -> str:
    """Remove every token that holds 8 consecutive characters of a secret (in any form), looking through white space and literal ``\\n`` / ``\\r`` / ``\\t`` so a secret broken over lines is found too."""
    kept: list[str] = []
    where: list[int] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == "\\" and i + 1 < n and text[i + 1] in "nrt":
            i += 2
        else:
            kept.append(c)
            where.append(i)
            i += 1
    compact = "".join(kept)
    hit = bytearray(n)
    found = False
    for j in range(len(compact) - _WINDOW + 1):
        if compact[j:j + _WINDOW] in windows:
            found = True
            for k in range(where[j], where[j + _WINDOW - 1] + 1):
                hit[k] = 1
    if not found:
        return text
    out: list[str] = []
    i = 0
    while i < n:
        if not hit[i]:
            out.append(text[i])
            i += 1
            continue
        start = i
        while start > 0 and text[start - 1] in _TOKEN_CHARS and not hit[start - 1]:      # widen to the whole token the fragment sits in
            start -= 1
        end = i
        while end < n and (hit[end] or text[end] in _TOKEN_CHARS):
            end += 1
        del out[len(out) - (i - start):]
        out.append(REDACTED)
        i = end
    return "".join(out)
