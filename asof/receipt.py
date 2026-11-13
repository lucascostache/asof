"""Receipts: what one call could see, and what it could not.

A receipt is rebuilt from the ledger events of one call (``call_id``), so the receipt a session hands back and the one ``strathora receipt <id>`` prints
are the same object made from the same evidence, and every receipt id is a ledger id.

A receipt is for the **operator or auditor**, not for the agent: it says how many records were blocked, and even a count reveals that something exists
after T. :meth:`Receipt.agent_view` is the version that is safe to show a model: it leaves the blocked counts out.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

from .errors import AsOfError

if TYPE_CHECKING:
    from .ledger import Ledger, LedgerEvent

STANDING_LIMITS = (
    "Source timestamps (available_at) are declared by the data's owner. AsOf enforces them; it cannot verify them.",
    "AsOf controls only what flows through it. Anything the agent can reach another way (its own memory, other tools, the open web) is outside the lock.",
)
MODEL_LIMIT = ("A model's memory is not controlled by AsOf. The declared cutoff is a statement by whoever registered it, a contamination score exists only if one "
               "was registered, and the label never exceeds 'observed'.")


@dataclass(frozen=True)
class Receipt:
    call_id: str
    session_id: str
    kind: str                                   # "query" | "tool" | "model" | "decision"
    target: str                                 # source name, tool name, model id or decision action
    as_of: str                                  # the session clock T for this call (UTC ISO-8601)
    recorded_at: str                            # wall clock, from the ledger
    privacy: str
    served: int = 0
    served_timing: Mapping[str, int] = field(default_factory=dict)     # {"declared": n, "unknown": m}
    blocked: Mapping[str, int] = field(default_factory=dict)
    served_ids: tuple[str, ...] = ()            # record ids (full) or payload hashes (hashes_only)
    quality: Mapping[str, Any] | None = None    # data-quality summary of this query (asof.quality.query_quality); None for a call that has none or for a ledger from before it existed
    universe: Mapping[str, Any] | None = None   # set when the query was restricted to a universe as of T: who, how many members, how many records that removed
    model: Mapping[str, Any] | None = None
    warnings: tuple[str, ...] = ()
    ledger_seq: int | None = None               # the event that closes the call (query, tool_call, model_call, decision)
    ledger_hash: str | None = None
    event_seqs: tuple[int, ...] = ()            # every ledger event belonging to this call
    limits: tuple[str, ...] = STANDING_LIMITS

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("served_ids", "warnings", "limits", "event_seqs"):
            d[k] = list(d[k])
        return d

    def to_json(self, **kw: Any) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, **kw)

    def agent_view(self) -> dict[str, Any]:
        """A version safe to show the agent: no blocked counts and no warnings about the data (both reveal that something exists after T), and no position in the ledger either.
        The ledger grows by one more event when a call withheld something, so ``ledger_seq`` would let an agent compare positions and learn that later data exists (and, with a filter
        on a guessed value, what it is). The hash identifies the same event without saying where it sits."""
        d: dict[str, Any] = {"call_id": self.call_id, "kind": self.kind, "target": self.target, "as_of": self.as_of, "served": self.served,
                             "served_timing": dict(self.served_timing), "served_ids": list(self.served_ids), "ledger_hash": self.ledger_hash}
        if self.model is not None:
            d["model"] = dict(self.model)
            d["warnings"] = list(self.warnings)           # these concern the model's memory, not the data
        return d

    def summary(self) -> str:
        lines = [f"Receipt {self.call_id} ({self.kind}: {self.target}) as of {self.as_of}"]
        if self.kind == "model" and self.model:
            m = self.model
            cut = m.get("declared_cutoff")
            lines.append(f"  model {m.get('id')}: declared cutoff {cut + ' (declared by ' + str(m.get('declared_by')) + ')' if cut else 'unknown'}, contamination score "
                         f"{m.get('contamination_score') if m.get('contamination_score') is not None else 'unknown'}, memory label '{m.get('label')}'")
        elif self.kind in ("query", "tool"):
            t = dict(self.served_timing)
            lines.append(f"  served {self.served} ({t.get('declared', 0)} with declared timing, {t.get('unknown', 0)} unknown)")
            b = {k: v for k, v in self.blocked.items() if v}
            lines.append("  blocked: " + (", ".join(f"{k}={v}" for k, v in b.items()) if b else "nothing"))
            if self.kind == "query":
                lines.extend(quality_lines(self.quality, self.universe))
        for w in self.warnings:
            lines.append(f"  warning: {w}")
        if self.ledger_seq is not None:
            lines.append(f"  ledger event {self.ledger_seq}, hash {str(self.ledger_hash)[:16]}...")
        return "\n".join(lines)

    __str__ = summary


def _pct(n: int, total: int) -> str:
    return "n/a" if not total else f"{round(100 * n / total)}%"


def quality_lines(q: Mapping[str, Any] | None, uni: Mapping[str, Any] | None = None) -> list[str]:
    """The receipt's data-quality lines. A receipt from a ledger that predates the summary says so instead of implying the data was clean."""
    out: list[str] = []
    if uni:
        out.append(f"  universe {uni.get('source')}: {uni.get('n_members')} members at T; {uni.get('excluded')} matching record(s) removed because they were not members")
    if q is None:
        out.append("  data quality: not recorded (this ledger predates the data-quality summary)")
        return out
    n = q.get("served", 0)
    fr = q.get("freshness") or {}
    parts = [f"timestamps {_pct(q.get('served_with_event_time', 0), n)} event_time / {_pct(q.get('served_with_available_at', 0), n)} available_at"]
    if fr.get("age_seconds") is not None:
        parts.append(f"newest served record {fr['age_seconds']} s before T" + (" (stale)" if fr.get("stale") else "" if fr.get("stale") is False else " (staleness unknown)"))
    out.append("  data quality: " + "; ".join(parts))
    flags = list(q.get("flags", []))
    src = (q.get("source") or {}).get("flags", [])
    out.append("  quality flags: " + (", ".join(flags) if flags else "none") + ("   source: " + ", ".join(src) if src else ""))
    return out


def _flag_text(f: dict) -> str:
    if f.get("kind") == "SERVER_TOOL":
        names = ", ".join(sorted(set(f.get("requested", [])) | set(f.get("reported_by_provider", [])))) or "unnamed"
        return f"the provider's own tools ({names}) were used outside the AsOf tool proxy, so this run cannot be called clean"
    return str(f.get("rule") or f.get("kind"))


def model_receipt(ledger: "Ledger", session_id: str, call_id: str, *, data_quality: dict | None = None) -> dict:
    """The receipt of one model call as a flat record, rebuilt from the ledger (plus the session's data-quality summary): who was called, at what clock, with what settings, what it cost in tokens, whether the
    provider ran tools of its own, how many items had been served and blocked in the session up to this call, its labels, and the hash links into the chain. Hashes and counts only unless the
    session's privacy mode is ``full`` (then ``settings`` holds the parameters). No prompt or output text is ever in it."""
    from .labels import Label, label_of, worst
    evs = ledger.events(session_id=session_id)
    ev = next((e for e in evs if e.call_id == call_id and e.type == "model_call"), None)
    if ev is None:
        raise AsOfError(f"no model call with call id {call_id!r} in session {session_id!r}")
    d = ev.data()
    gw = d.get("gateway") or {}
    before = [e for e in evs if e.seq < ev.seq]
    served = sum(1 for e in before if e.type in ("record_served", "tool_item_served"))
    blocked = sum(int(e.data().get("count") or 0) for e in before if e.type == "block")
    flags = list(gw.get("flags") or [])
    memory = label_of(str(d.get("label") or "unknown"))
    labels = [memory.value] + ([Label.FLAGGED.value] if flags else [])
    summary = None
    if data_quality is not None:
        summary = {name: {"flags": list(q.get("flags", []))} for name, q in sorted(data_quality.items())}
    return {
        "run_id": session_id, "call_id": call_id, "clock_T": gw.get("clock_T"), "provider": d.get("provider"), "model": str(d.get("model_id") or "").partition(":")[2] or d.get("model_id"), "model_version": d.get("version_reported"),
        "reasoning_effort": gw.get("reasoning_effort"),
        "settings": d["params"] if "params" in d else {"params_sha256": d.get("params_sha256"), "keys": sorted((d.get("param_sha256") or {}).keys())},
        "prompt_hash": d.get("prompt_sha256"), "output_hash": d.get("response_sha256", d.get("response_text_sha256")),
        "tokens": gw.get("tokens") or {"input": None, "output": None, "cached": None, "reasoning": None},
        "server_tools_used": sorted(set(gw.get("server_tools_reported") or []) | set(gw.get("server_tools_requested") or [])),
        "items_served": served, "items_blocked": blocked, "labels": labels, "label": worst(*labels).value, "flags": flags,
        "provider_verified": bool(gw.get("provider_verified", False)), "data_quality_summary": summary,
        "prev_hash": ev.prev_hash, "hash": ev.hash,
    }


def build_receipt(events: "list[LedgerEvent]", privacy: str) -> Receipt:
    """Make the receipt of one call from its ledger events (all events with that ``call_id``, in order)."""
    if not events:
        raise AsOfError("no ledger events for that call id")
    closing = next((e for e in reversed(events) if e.type in ("query", "tool_call", "model_call", "decision")), None)
    if closing is None:
        raise AsOfError(f"the events for call {events[0].call_id} do not include a completed call (found: {sorted({e.type for e in events})})")
    d = closing.data()
    seqs = tuple(e.seq for e in events)
    base: dict[str, Any] = dict(call_id=closing.call_id or "", session_id=closing.session_id, as_of=closing.event_time or "", recorded_at=closing.recorded_at, privacy=privacy,
                                ledger_seq=closing.seq, ledger_hash=closing.hash, event_seqs=seqs)
    blocks = [e.data() for e in events if e.type == "block"]
    block_counts = {b["reason"]: b["count"] for b in blocks}
    if closing.type == "query":
        served = [e for e in events if e.type == "record_served"]
        unknown = sum(1 for e in served if e.data().get("timing") == "unknown")
        ids = tuple((e.data().get("record_id") if privacy == "full" else e.data().get("payload_sha256")) or "" for e in served)
        c = d["counts"]
        return Receipt(kind="query", target=d["source"], served=len(served), served_timing={"declared": len(served) - unknown, "unknown": unknown},
                       blocked={"future": block_counts.get("future", 0), "untimestamped": block_counts.get("untimestamped", 0), "superseded": c["superseded"],
                                "outside_window": c["outside_window"]}, served_ids=ids, warnings=tuple(d.get("warnings", [])),
                       quality=d.get("quality"), universe=d.get("universe"), **base)
    if closing.type == "tool_call":
        items = [e for e in events if e.type == "tool_item_served"]
        unknown = sum(1 for e in items if e.data().get("timing") == "unknown")
        return Receipt(kind="tool", target=d["tool"], served=len(items), served_timing={"declared": len(items) - unknown, "unknown": unknown},
                       blocked={"future": block_counts.get("future", 0), "untimestamped": block_counts.get("untimestamped", 0)},
                       served_ids=tuple(e.data()["payload_sha256"] for e in items), **base)
    if closing.type == "model_call":
        info = {k: d.get(k) for k in ("provider", "version_reported", "declared_cutoff", "declared_by", "declared_cutoff_after_as_of", "contamination_score", "score_source", "label")}
        info["id"] = d["model_id"]
        gw = d.get("gateway") or {}
        info["gateway"] = gw or None
        info["flagged"] = bool(gw.get("flags"))
        warnings = list(d.get("warnings", [])) + [f"FLAGGED ({f.get('kind')}): " + _flag_text(f) for f in gw.get("flags", [])]
        return Receipt(kind="model", target=d["model_id"], model=info, warnings=tuple(warnings), limits=STANDING_LIMITS + (MODEL_LIMIT,), **base)
    return Receipt(kind="decision", target=d.get("action", ""), **base)                                  # decision


def session_privacy(ledger: "Ledger", session_id: str) -> str:
    start = ledger.events(session_id=session_id, type="session_start")
    return start[0].data().get("privacy", "full") if start else "full"


def receipt_from_ledger(ledger: "Ledger", call_id: str) -> Receipt:
    events = ledger.events(call_id=call_id)
    if not events:
        raise AsOfError(f"no ledger events with call id {call_id!r}")
    return build_receipt(events, session_privacy(ledger, events[0].session_id))
