"""``strathora report --html``: one self-contained HTML file made from a ledger alone, with the widgets inlined. No network, no clock: the same ledger gives the same bytes."""
from __future__ import annotations

import html as _html
from pathlib import Path
from typing import Any

from ..errors import AsOfError
from ..ledger import open_for_reading
from ..receipt import build_receipt, session_privacy
from . import element, page, redact

CALLS = ("query", "tool_call", "model_call", "decision")


def merge_quality(per_source: dict[str, dict[str, Any]], *, view: str = "internal", secret: str | bytes | None = None) -> dict[str, Any]:
    """One quality-panel summary from the per-source summaries: counts by severity added up, issues named by source. There is no combined score.
    ``view`` is ``internal`` (default), ``shared`` or ``showcase``; ``secret`` keys the row ids (default ``$ASOF_CLIENT_SECRET``): see :mod:`asof.widgets.redact`."""
    return redact.apply(_merge(per_source), view, secret)


def _merge(per_source: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not per_source:
        return {"recorded": False}
    sev = {"error": 0, "warning": 0, "info": 0}
    issues: list[dict[str, Any]] = []
    n = 0
    for name, q in sorted(per_source.items()):
        n += int(q.get("n_records") or 0)
        for k, v in (q.get("by_severity") or {}).items():
            sev[k] = sev.get(k, 0) + int(v)
        issues += [{**i, "flag": f"{name}: {i['flag']}"} for i in q.get("issues", [])]
    sources = [{"name": name, "kind": q.get("kind"), "n_records": q.get("n_records"), "flags": list(q.get("flags", [])), "event_time": q.get("event_time"), "available_at": q.get("available_at"),
                "lag_seconds": (q.get("timing") or {}).get("lag_seconds"), "thresholds": q.get("thresholds")} for name, q in sorted(per_source.items())]
    return {"v": 1, "kind": "series", "n_records": n, "by_severity": sev, "issues": issues, "flags": sorted({f for q in per_source.values() for f in q.get("flags", [])}), "sources": sources}


def build(ledger_path: str | Path, *, session_id: str | None = None, synthetic: bool = False, title: str | None = None, max_receipts: int = 12,
          external: bool = False, view: str | None = None, secret: str | bytes | None = None) -> str:
    """The report as an HTML string. A ledger written before data-quality summaries says "not recorded" for quality, never "clean".
    ``external=True`` marks the export as leaving the building: the data-quality panel and the receipts are then drawn in the ``shared`` view (``view=`` overrides). The chain and the three clocks are not
    redacted, and a receipt still names its target source, so an external report needs those reviewed too."""
    view = view or ("shared" if external else "internal")
    redact.check_view(view)
    path = Path(ledger_path).expanduser()
    if not path.exists():
        raise AsOfError(f"no ledger at {path}")
    led = open_for_reading(path)
    try:
        events = led.events(session_id=session_id) if session_id else led.events()
        if not events:
            raise AsOfError("no events to report" + (f" for session {session_id!r}" if session_id else ""))
        v = led.verify()
        chain = {"ok": v.ok, "status": "intact" if v.ok else "broken", "n_events": v.n_events, "head_hash": v.head_hash, "first_bad_seq": v.first_bad_seq, "reason": v.reason, "anchor": v.anchor,
                 "warnings": list(v.warnings)}
        quality = merge_quality({str(e.data().get("name")): e.data()["quality"] for e in events if e.type == "source_loaded" and e.data().get("quality")}, view=view, secret=secret)
        timeline = {"events": [{"seq": e.seq, "type": e.type, "event_time": e.event_time, "available_at": e.available_at, "recorded_at": e.recorded_at} for e in events]}
        by_call: dict[str, list] = {}
        for e in events:
            if e.call_id:
                by_call.setdefault(e.call_id, []).append(e)
        receipts = []
        for evs in by_call.values():
            if any(x.type in CALLS for x in evs):
                try:
                    receipts.append(build_receipt(evs, session_privacy(led, evs[0].session_id)).to_dict())
                except AsOfError:
                    continue
        receipts.sort(key=lambda r: (r.get("ledger_seq") or 0))
    finally:
        led.close()
    shared_note = (' Shared view: in the data-quality panel source names are aliased and lag statistics, row ids and custom thresholds are withheld; in the receipts the session clock T is withheld and served ids are keyed. '
                   'The chain and the three clocks below are not redacted.') if view == "shared" else ""
    body = [f"<h1>{_html.escape(title or 'AsOf report')}</h1>",
            f'<p class="note">Made from the ledger alone: no network was used and nothing here was recomputed from outside it. An intact chain shows the record is unchanged since it was written; it does not show that any timestamp is true.{shared_note}</p>',
            "<h2>Chain</h2>", element("asof-chain-status", chain, synthetic=synthetic), "<h2>Data quality</h2>", element("asof-quality-panel", quality, synthetic=synthetic),
            "<h2>Three clocks</h2>", element("asof-clock-timeline", timeline, synthetic=synthetic), f"<h2>Receipts ({min(len(receipts), max_receipts)} of {len(receipts)})</h2>"]
    body += [element("asof-receipt", redact.widget("asof-receipt", r, view, secret), synthetic=synthetic) for r in receipts[:max_receipts]]
    return page("\n".join(body), title=title or "AsOf report")
