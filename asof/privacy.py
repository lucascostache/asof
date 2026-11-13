"""Privacy modes for what is written to the ledger (rule R7).

``full``         the ledger keeps arguments, served payloads, prompts and responses (credentials are still redacted).
``hashes_only``  the ledger keeps counts, times, names and SHA-256 hashes only. Raw payloads, tool arguments, query filters, record ids,
                 prompts and responses are replaced by hashes, so the file can be shared for audit without exposing the data.
"""
from __future__ import annotations

from typing import Any, Iterable

from .records import canonical_json, sha256_hex

MODES = ("full", "hashes_only")


def check_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"privacy must be one of {MODES}, not {mode!r}")
    return mode


def digest(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def shape(payload: dict[str, Any], mode: str, raw: Iterable[str] = ()) -> dict[str, Any]:
    """Return ``payload`` with each key in ``raw`` kept (full) or replaced by ``<key>_sha256`` (hashes_only). Other keys are metadata and stay."""
    raw = set(raw)
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k in raw:
            if mode == "full":
                out[k] = v
            else:
                out[f"{k}_sha256"] = digest(v)
        else:
            out[k] = v
    return out
