"""Privacy for the data-quality widget: keyed row ids and the three views.

Why this exists. A data-quality summary lists a few affected record ids so an operator can find the rows. The summary carries them as plain SHA-256 hashes (the same hash a ``hashes_only``
ledger writes). A plain hash of a guessable id ("SYN01|2023-01-02") can be reversed by trying candidates, and the same row gives the same hash for every client. What a widget shows or embeds is
therefore re-keyed here.

* **Row ids** are ``HMAC-SHA256(client secret, plain hash)``, domain-separated, so the same row gives a different id for every client, nothing can be guessed without the secret, and one client
  can still link an issue to its rows (same secret, same id). The secret comes from the caller or from the environment variable ``ASOF_CLIENT_SECRET``. It must be at least 16 characters. It is
  never stored in the data, never put in an error message and never logged. With no secret, ids are **withheld** (not shown as plain hashes) and the widget says so.
* **Views**: ``internal`` (everything, row ids keyed), ``shared`` (what may leave the building: source names aliased to Source A, B, ...; lag statistics, row ids and customised thresholds
  withheld) and ``showcase`` (a website or demo: full content of demo data, but only the generic default thresholds).

What this does **not** change: the ledger. ``analyse_source`` still writes the plain hashes into the ``source_loaded`` event, which is what lets a ``hashes_only`` ledger link an issue to its rows;
a ledger file that is shared is a separate exposure from a widget that is shared.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import os
from typing import Any, Iterable, Mapping

from ..errors import AsOfError
from ..privacy import digest
from ..quality import THRESHOLDS
from .compute import percent_shares

ENV_SECRET = "ASOF_CLIENT_SECRET"
MIN_SECRET_CHARS = 16
VIEWS = ("internal", "shared", "showcase")
PLAIN_KEY = "example_record_ids_sha256"           # the unkeyed list quality.py writes; it is never shown or embedded by a widget
KEYED_KEY = "example_row_ids_hmac"
_DOMAIN = b"asof.quality.row-id.v1\x00"


def check_view(view: str) -> str:
    if view not in VIEWS:
        raise ValueError(f"view must be one of {VIEWS}, not {view!r}")
    return view


def client_secret(secret: str | bytes | None = None) -> bytes | None:
    """The per-client secret as bytes, from ``secret`` or ``$ASOF_CLIENT_SECRET``; ``None`` when neither is set. A secret that is too short is refused (the message never contains it)."""
    raw = secret if secret is not None else os.environ.get(ENV_SECRET)
    if raw is None or raw == "" or raw == b"":
        return None
    key = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(key) < MIN_SECRET_CHARS:
        raise AsOfError(f"the client secret is too short: use at least {MIN_SECRET_CHARS} characters (set {ENV_SECRET} or pass secret=)")
    return key


def row_id(plain_sha256: str, secret: bytes) -> str:
    """The keyed id of one row: HMAC-SHA256 over its plain hash, as hex. Stable for one client, different for every other."""
    return hmac.new(secret, _DOMAIN + str(plain_sha256).encode("utf-8"), hashlib.sha256).hexdigest()


def alias(i: int) -> str:
    """``Source A`` ... ``Source Z``, then ``Source AA``, ``Source AB`` ..."""
    s, n = "", i
    while True:
        s = chr(65 + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            return f"Source {s}"


def aliases(names: Iterable[str]) -> dict[str, str]:
    """Each source name to its alias, in sorted order of the real names (the same names always give the same aliases)."""
    return {n: alias(i) for i, n in enumerate(sorted(set(names)))}


def _index_pair(*series: list[dict[str, Any]]) -> tuple[float, float] | None:
    vals = [p["v"] for s in series for p in s if isinstance(p.get("v"), (int, float))]
    return (min(vals), max(vals)) if vals and max(vals) > min(vals) else None


def _indexed(series: list[dict[str, Any]], lo: float, hi: float) -> list[dict[str, Any]]:
    return [{**p, "v": None if p.get("v") is None else round((p["v"] - lo) / (hi - lo) * 100, 1)} for p in series]


def _shared_sharpe(d: dict[str, Any], key: bytes | None) -> dict[str, Any]:
    lo_hi = _index_pair(d.get("points", []))
    d["points"] = _indexed(d.get("points", []), *lo_hi) if lo_hi else [{**p, "v": None} for p in d.get("points", [])]
    d["scale"] = "indexed 0-100 over this series"
    d.pop("lag_seconds", None)                                                              # lag statistics are withheld in the shared view
    return d


def _shared_overlay(d: dict[str, Any], key: bytes | None) -> dict[str, Any]:
    lo_hi = _index_pair(d.get("locked", []), d.get("unlocked", []))
    if lo_hi:
        lo, hi = lo_hi
        d["locked"], d["unlocked"] = _indexed(d["locked"], lo, hi), _indexed(d["unlocked"], lo, hi)
        k = 100 / (hi - lo)
        s = d.get("summary", {})
        d["summary"] = {**s, "max_abs_gap": None if s.get("max_abs_gap") is None else round(s["max_abs_gap"] * k, 1), "mean_abs_gap": None if s.get("mean_abs_gap") is None else round(s["mean_abs_gap"] * k, 1)}
    else:
        d["locked"], d["unlocked"] = ([{**p, "v": None} for p in d.get(n, [])] for n in ("locked", "unlocked"))
        d["summary"] = {**d.get("summary", {}), "max_abs_gap": None, "mean_abs_gap": None}
    d["scale"] = "indexed 0-100 over both series; gaps are % of that range"
    d.pop("lag_seconds", None)                                                              # lag statistics are withheld in the shared view
    return d


def _shared_gap(d: dict[str, Any], key: bytes | None) -> dict[str, Any]:
    gap, comps = d.get("gap"), d.get("components", [])
    have = [c.get("share") for c in comps]
    shares = have if comps and all(isinstance(s, int) and not isinstance(s, bool) for s in have) else percent_shares([c.get("value") for c in comps], gap)       # the shares the internal view draws, never a second calculation
    for c, s in zip(comps, shares, strict=True):
        c.pop("value", None)
        c["share"] = s
    d["gap"] = None
    for k in ("unit", "headline", "as_of", "row_counts"):                      # the Sharpe gap is a value too; how the return was annualised is a method, so it stays
        d.pop(k, None)
    return d


def _shared_receipt(d: dict[str, Any], key: bytes | None) -> dict[str, Any]:
    ids = d.pop("served_ids", None) or []
    d.pop("as_of", None)                                                                    # the session clock T is withheld
    if isinstance(d.get("quality"), dict):
        d["quality"].pop("lag_seconds", None)                                               # lag statistics are withheld in the shared view, as in the data-quality panel
    if key is not None:
        d["served_ids_hmac"] = [row_id(digest(i), key) for i in ids]                         # the same keyed id the data-quality panel gives that row
    d["row_ids"] = "hmac" if key is not None else ("no_secret" if ids else "none")
    return d


_SHARED = {"asof-receipt": _shared_receipt, "asof-rolling-sharpe": _shared_sharpe, "asof-leak-overlay": _shared_overlay, "asof-gap-breakdown": _shared_gap}


def widget(tag: str, data: Mapping[str, Any], view: str = "internal", secret: str | bytes | None = None) -> dict[str, Any]:
    """The data of a receipt, rolling Sharpe, leak overlay or gap breakdown as ``view`` allows it. ``internal`` and ``showcase`` return it unchanged. ``shared`` returns a copy with what must not leave
    withheld: the Sharpe and overlay values become an index of 0-100 over the series (the shape and the gap stay, the levels do not) and no table is drawn; the gap breakdown keeps only each part's share
    of the gap; the receipt loses the session clock T and its served ids become keyed ids (see :func:`row_id`)."""
    check_view(view)
    key = client_secret(secret)
    if view != "shared" or tag not in _SHARED:
        return dict(data)
    d = copy.deepcopy(dict(data))
    d = _SHARED[tag](d, key)
    d["view"] = "shared"
    return scrub_plain(d)


def shared_payload(tag: str, data: Any, secret: str | bytes | None = None) -> dict[str, Any] | None:
    """The shared-view form of a widget's data, for the "Download image" button (the browser cannot redact: the secret and the rules live here). ``None`` when the data is already a shared or showcase
    view, or when the widget has no shared form (the chain status and the three clocks are not redacted: see the README)."""
    if not isinstance(data, Mapping) or data.get("view") in ("shared", "showcase"):
        return None
    if tag in _SHARED:
        return widget(tag, data, "shared", secret)
    if tag == "asof-quality-panel":
        return apply(data, "shared", secret)
    return None


def scrub_plain(data: Any) -> Any:
    """``data`` without any plain-hash row id lists, wherever they are. Used on everything a widget embeds, so an unkeyed hash cannot ride along inside an element."""
    if isinstance(data, Mapping):
        return {k: scrub_plain(v) for k, v in data.items() if k != PLAIN_KEY}
    if isinstance(data, (list, tuple)):
        return [scrub_plain(v) for v in data]
    return data


def _rename_flag(flag: str, names: Mapping[str, str]) -> str:
    for n in sorted(names, key=len, reverse=True):
        if flag.startswith(n + ": "):
            return names[n] + flag[len(n):]
    return flag


def apply(summary: Mapping[str, Any], view: str = "internal", secret: str | bytes | None = None) -> dict[str, Any]:
    """The summary as ``view`` allows it: a new dict, the input is not changed. Works on the merged panel shape and on one source's own summary."""
    check_view(view)
    key = client_secret(secret)
    d: dict[str, Any] = copy.deepcopy(dict(summary))
    if d.get("recorded") is False:
        return d
    shared = view == "shared"
    names = aliases(str(s["name"]) for s in d.get("sources", []) if isinstance(s, Mapping) and s.get("name") is not None) if shared else {}
    had_ids = False
    for i in d.get("issues", []):
        plain = i.pop(PLAIN_KEY, None) or []
        had_ids = had_ids or bool(plain)
        if key is not None and not shared:
            i[KEYED_KEY] = [row_id(h, key) for h in plain]
        if shared:
            i.pop(KEYED_KEY, None)                              # a summary that was already keyed for an internal view loses its ids here too
            if isinstance(i.get("flag"), str):
                i["flag"] = _rename_flag(i["flag"], names)
    for s in d.get("sources", []):
        if shared:
            s["name"] = names.get(s.get("name"), s.get("name"))
            s["lag_seconds"] = None
        if shared or view == "showcase":
            s["thresholds"] = dict(THRESHOLDS)
    if shared or view == "showcase":
        if "thresholds" in d:
            d["thresholds"] = dict(THRESHOLDS)
        d["thresholds_view"] = "defaults"
    if shared:
        (d.get("timing") or {}).pop("lag_seconds", None)
    d["view"] = view
    d["row_ids"] = "hidden" if shared else ("hmac" if key is not None else ("no_secret" if had_ids else "none"))
    return d
