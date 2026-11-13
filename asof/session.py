"""The Session: one clock, its sources, tools and model gateway, and a ledger of everything that happened.

Ledger clocks (see :mod:`asof.ledger` and SPEC.md): an *agent action* (query, tool call, model call, decision, block, clock change) is stamped
``event_time = available_at = T``, the session clock at that moment. A *served record* is stamped with the record's own ``event_time`` and
``available_at`` (``NULL`` where unknown). ``recorded_at`` is the wall clock and is set only by the ledger.
"""
from __future__ import annotations

import math
import sys
import threading
import uuid
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence, overload

from . import __version__, contam, modelrules, quality as dq
from .clock import Clock, TimeLike, parse_duration, parse_time
from .errors import AsOfError, ClockError, DecisionRequiredError, GatewayError, GatewayRuleError, SourceError, ToolError
from .gate import APPROVE, GateResult, History, Intent, Policy
from .gate import evaluate as gate_evaluate
from .gateway import RESERVED, ModelGateway, ModelResponse, Provider
from .ledger import EventSpec, Ledger, LedgerEvent, VerifyResult, iso, redact
from .redaction import scrub
from .memory import Recalled, rank
from .privacy import check_mode, digest, shape
from .query import MAX_LISTED, pit_query
from .receipt import Receipt, build_receipt, model_receipt
from .records import Record, payload_sha256, sha256_hex
from .sources.bars import intrabar_ambiguous
from .sources.base import Source
from .sources.universe import Universe, members_as_of
from .tools import ToolProxy

_T: Any = object()           # "stamp with the session clock"
MAX_LINKED = 2000            # served-event ids listed in one decision's links; the true count is always recorded (n_served), and truncation is flagged
GATEWAY_RULES_VERSION = 1    # the version of the `gateway` block written on model_call / model_error events (SPEC 1.3)


def _wall() -> datetime:
    """The wall clock, in one place so a test can fix it. Used only for the data-quality summary's ``future_dated`` count, never for a time lock."""
    return datetime.now(timezone.utc)


class Session:
    """Binds a clock ``as_of`` (timezone-aware; naive is rejected), sources, tools, a model gateway, a ledger and a privacy mode.

    ``privacy="full"`` records arguments, payloads, prompts and responses in the ledger; ``"hashes_only"`` records only counts, names, times and hashes.
    In both modes credentials are redacted. ``allow_untimestamped=True`` lets records and tool items with no usable timestamp be served, labelled
    ``unknown``; the default withholds them. ``max_as_of`` is a ceiling the clock can never pass. ``decision_gate=True`` makes the clock refuse to
    advance while the agent has acted (queried, called a tool or a model) since its last :meth:`decision`: "every decision is logged".
    """

    def __init__(self, as_of: TimeLike, *, privacy: str = "full", allow_untimestamped: bool = False, ledger_path: str | Path = "~/.asof/ledger.db",
                 ledger: Ledger | None = None, session_id: str | None = None, model: str | None = None, gateway: ModelGateway | None = None,
                 max_as_of: TimeLike | None = None, decision_gate: bool = False, max_listed_blocked: int = MAX_LISTED) -> None:
        """``max_listed_blocked``: how many withheld records (or tool items) a ``block`` event names one by one (default 1000). The COUNT is always exact. Each named record costs about
        75 bytes of ledger, at every query that withholds it, so a long replay over a large source can lower this (0 lists none) and keep the ledger small."""
        if not isinstance(max_listed_blocked, int) or isinstance(max_listed_blocked, bool) or max_listed_blocked < 0:
            raise ValueError("max_listed_blocked must be a whole number, 0 or more")
        self.max_listed_blocked = max_listed_blocked
        self.id = session_id or uuid.uuid4().hex[:16]
        self.privacy = check_mode(privacy)
        self.allow_untimestamped = bool(allow_untimestamped)
        self.decision_gate = bool(decision_gate)
        self.default_model = model
        self._lock = threading.RLock()
        self.clock = Clock(as_of, max_as_of=max_as_of, on_change=self._on_advance, on_reject=self._on_clock_rejected)
        self.ledger = ledger if ledger is not None else Ledger(ledger_path)
        self._owns_ledger = ledger is None
        self.gateway = gateway or ModelGateway()
        self._tools = ToolProxy()
        self._sources: dict[str, Source] = {}
        self._loaded: dict[str, tuple[list[Record], str]] = {}
        self._evidence: dict[str, dict[str, Any]] = {}                    # call id -> what the call served, and whether the next decision has not yet linked to it
        self._decisions: dict[str, dict[str, Any]] = {}                   # decision call id -> its ledger event id (orders link to decisions)
        self._orders: dict[str, dict[str, Any]] = {}                      # order id -> time, size, filled so far, stop and target
        self._fills: dict[str, list[str]] = {}
        self._quality: dict[str, dict[str, Any]] = {}                   # per-source data-quality summary, made when the source is loaded (operator view: it spans the whole source)
        self._receipts: list[Receipt] = []
        self._flags: list[dict[str, Any]] = []                           # what the gateway flagged on model calls (provider-side tools, policy exceptions, version-pin misses)
        self._intents: dict[str, dict[str, Any]] = {}                    # intent id -> the first use of it (content, time, call id): the gate's duplicate check and rate limit read this
        self._gates: dict[str, dict[str, Any]] = {}                      # gate call id -> its decision (an order may link only to an APPROVED one)
        self._pending_actions = 0
        self._closed = False
        self._log("session_start", {"as_of": iso(self.clock.now), "max_as_of": iso(self.clock.ceiling), "privacy": self.privacy,
                                     "allow_untimestamped": self.allow_untimestamped, "decision_gate": self.decision_gate, "asof_version": __version__,
                                     "python": sys.version.split()[0]})

    # ------------------------------------------------------------------ basics
    @property
    def as_of(self) -> datetime:
        """The session clock (UTC)."""
        return self.clock.now

    @property
    def sources(self) -> list[str]:
        return sorted(self._sources)

    @property
    def tools(self) -> list[str]:
        return self._tools.names()

    def _check_open(self) -> None:
        if self._closed:
            raise AsOfError("this session is closed")

    def _spec(self, type_: str, payload: dict[str, Any], raw: tuple[str, ...] = (), *, call_id: str | None = None, event_time: Any = _T,
              available_at: Any = _T) -> EventSpec:
        """One event, stamped. Agent actions default to the session clock T for both ``event_time`` and ``available_at`` (A1); privacy shaping
        and redaction happen here and in the ledger, so they cannot be skipped (R7)."""
        T = self.clock.now
        return EventSpec(self.id, type_, T if event_time is _T else event_time, T if available_at is _T else available_at, shape(payload, self.privacy, raw), call_id)

    def _log(self, type_: str, payload: dict[str, Any], raw: tuple[str, ...] = (), *, call_id: str | None = None) -> LedgerEvent:
        return self.ledger.append_many([self._spec(type_, payload, raw, call_id=call_id)])[0]

    def _new_call(self) -> str:
        return uuid.uuid4().hex[:12]

    def _error_payload(self, e: Exception) -> dict[str, Any]:
        return {"error_type": type(e).__name__, "error": scrub(str(e))}             # "error" is raw: hashed in hashes_only mode. scrub: credentials in any form a server may have repeated them in

    # ------------------------------------------------------------------ clock (R5)
    def advance_to(self, to: TimeLike) -> datetime:
        """Move the clock forward. Moving it back, or past ``max_as_of``, raises :class:`~asof.errors.ClockError` (and the attempt is logged). Every
        change is logged. With ``decision_gate``, raises :class:`~asof.errors.DecisionRequiredError` if the agent acted since its last decision."""
        with self._lock:
            self._check_open()
            target = parse_time(to, what="advance_to")
            if self.decision_gate and self._pending_actions and target != self.clock.now:
                self._log("clock_gate_rejected", {"current": iso(self.clock.now), "attempted": iso(target), "actions_without_decision": self._pending_actions})
                raise DecisionRequiredError(f"{self._pending_actions} action(s) since the last decision: call decision() before advancing the clock")
            now = self.clock.advance_to(target)
            self._pending_actions = 0
            return now

    def refuse_clock_move(self, reason: str) -> None:
        """Log that a request to move the clock was refused by policy (for example by the MCP server) and raise :class:`~asof.errors.ClockError`."""
        with self._lock:
            self._check_open()
            self._log("clock_refused", {"reason": reason, "current": iso(self.clock.now)})
            raise ClockError(reason)

    def _on_advance(self, old: Any, new: Any) -> None:
        self._log("clock_advance", {"from": iso(old), "to": iso(new)})

    def _on_clock_rejected(self, current: Any, attempted: Any, reason: str) -> None:
        self._log("clock_rewind_rejected" if reason == "rewind" else "clock_ceiling_rejected", {"current": iso(current), "attempted": iso(attempted)})

    # ------------------------------------------------------------------ decisions
    def _track(self, kind: str, call_id: str, events: Sequence[LedgerEvent]) -> None:
        """Remember what a call produced, so the next decision can link to it explicitly (the ids of the events that served records or tool items)."""
        served = [e.id for e in events if e.type in ("record_served", "tool_item_served")]
        self._evidence[call_id] = {"kind": kind, "served": served, "pending": True}

    def decision(self, action: str, /, *, based_on: Sequence[str | Receipt] | None = None, **details: Any) -> str:
        """Record an agent decision at the current clock time (``event_time = available_at = T``). Returns the call id (a ledger id).

        The decision event carries explicit **links** (SPEC 1.2, section 4.6) to what the agent had been given, so nobody has to guess from the order of events: the ledger ids of the served
        record and tool-item events, and the call ids of the queries, tool calls and model calls. By default these are all the calls since the previous decision (``basis`` =
        ``since_previous_decision``: what was *available* to the decision, not proof of what the agent used). Pass ``based_on=[call ids or receipts]`` to say which calls this decision rests
        on (``basis`` = ``explicit``); an id this session never produced is refused. ``based_on`` is reserved; other keywords are free-form ``details``."""
        with self._lock:
            self._check_open()
            links = self._links_for(based_on)
            call_id = self._new_call()
            ev = self.ledger.append_many([self._spec("decision", {"action": action, "details": details, "links": links}, raw=("details",), call_id=call_id)])
            for v in self._evidence.values():
                v["pending"] = False
            self._pending_actions = 0
            self._decisions[call_id] = {"event_id": ev[0].id}
            self._receipts.append(build_receipt(ev, self.privacy))
            return call_id

    def _links_for(self, based_on: Sequence[str | Receipt] | None) -> dict[str, Any]:
        """The evidence links of a decision or an intent (SPEC 4.6): by default every call since the previous decision (``since_previous_decision``), or exactly the calls named (``explicit``)."""
        if based_on is None:
            chosen = [c for c, v in self._evidence.items() if v["pending"]]
            basis = "since_previous_decision"
        else:
            chosen = [b.call_id if isinstance(b, Receipt) else str(b) for b in based_on]
            unknown = [c for c in chosen if c not in self._evidence]
            if unknown:
                raise AsOfError(f"based_on names call id(s) this session never produced: {unknown[:3]}")
            chosen = list(dict.fromkeys(chosen))
            basis = "explicit"
        served = [i for c in chosen for i in self._evidence[c]["served"]]
        return {"basis": basis, "query_call_ids": [c for c in chosen if self._evidence[c]["kind"] == "query"], "tool_call_ids": [c for c in chosen if self._evidence[c]["kind"] == "tool"],
                "model_call_ids": [c for c in chosen if self._evidence[c]["kind"] == "model"], "served_event_ids": served[:MAX_LINKED], "n_served": len(served), "truncated": len(served) > MAX_LINKED}

    # ------------------------------------------------------------------ the decision gate (SPEC 1.3, section 4.9): data seen -> model -> intent -> gate -> order -> fill
    def gate(self, intent: Intent, policy: Policy, *, positions: Mapping[str, float] | None = None, based_on: Sequence[str | Receipt] | None = None) -> GateResult:
        """Put an intent through the decision gate, as of the session clock, and record both in the ledger as one call: an ``intent`` event (what was proposed, and the calls it rests on: the data it
        saw and the model calls that produced it) and a ``gate_decision`` event (APPROVE / HOLD / BLOCK with every check and why). Returns the result, whose ``call_id`` links an order to it
        (``order(..., intent=result.call_id)``). The gate never sends anything: it decides, and the decision is a ledger event. An intent id used before is a duplicate (BLOCK); the first use is the one kept."""
        with self._lock:
            self._check_open()
            now = self.clock.now
            hist = History(tuple((iid, v["content"], v["time"]) for iid, v in self._intents.items()), dict(positions or {}))
            result = gate_evaluate(intent, policy, hist, now=now)
            links = self._links_for(based_on)
            call_id = self._new_call()
            ev = result.as_event()
            terms = {"symbol": intent.symbol.strip().upper(), "side": intent.side, "size": float(intent.size), "price": None if intent.price is None else float(intent.price)}
            specs = [self._spec("intent", {"intent_id": intent.intent_id, "agent_id": intent.agent_id, "model_id": intent.model, "model_version": intent.model_version,
                                           "data_as_of": None if intent.data_as_of is None else iso(parse_time(intent.data_as_of, what="data_as_of")), "links": links, "terms": terms},
                               raw=("terms",), call_id=call_id),
                     self._spec("gate_decision", {"intent_id": intent.intent_id, "decision": result.decision, "reasons": ev["reasons"], "details": ev["details"],
                                                  "policy_sha256": digest({k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in sorted(vars(policy).items())})},
                                raw=("details",), call_id=call_id)]
            self.ledger.append_many(specs)
            self._intents.setdefault(intent.intent_id, {"content": intent.content(), "time": now, "call_id": call_id})
            self._gates[call_id] = {"decision": result.decision, "intent_id": intent.intent_id}
            return replace(result, call_id=call_id)

    # ------------------------------------------------------------------ execution: orders, fills, position marks (SPEC 1.2, section 4.6)
    @staticmethod
    def _num(v: Any, what: str, *, positive: bool = False, optional: bool = False) -> float | None:
        if v is None and optional:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or (positive and v <= 0):
            raise AsOfError(f"{what} must be a finite number{' greater than zero' if positive else ''}, not {v!r}")
        return float(v)

    def order(self, order_id: str, /, *, symbol: str, side: str, size: float, intended_price: float | None = None, expected_price: float | None = None,
              expected_source: str = "model", stop_loss: float | None = None, take_profit: float | None = None, decision: str | None = None, intent: str | None = None) -> str:
        """Record an order at the session clock. ``expected_price`` is the price the model or backtest *assumed* it would fill at (``expected_source`` says which); ``intended_price`` is the
        market price when the order was sent (the arrival price). ``decision`` is the call id :meth:`decision` returned, which links the order to it explicitly. Prices, size, symbol and levels
        are payload values: hashed in ``hashes_only`` mode. Returns the order id."""
        with self._lock:
            self._check_open()
            if not isinstance(order_id, str) or not 1 <= len(order_id) <= 80:
                raise AsOfError("an order id is a text of 1 to 80 characters")
            if order_id in self._orders:
                raise AsOfError(f"order {order_id!r} was already recorded in this session")
            s = str(side).lower()
            if s not in ("buy", "sell"):
                raise AsOfError("side must be 'buy' or 'sell'")
            if expected_source not in ("model", "backtest"):
                raise AsOfError("expected_source must be 'model' or 'backtest'")
            if decision is not None and decision not in self._decisions:
                raise AsOfError(f"decision {decision!r} was not recorded in this session")
            if intent is not None:                                                             # data seen -> model -> intent -> gate -> ORDER: only an approved intent may be linked
                g = self._gates.get(intent)
                if g is None:
                    raise AsOfError(f"intent {intent!r} was not put through the gate in this session (use the call_id that Session.gate returned)")
                if g["decision"] != APPROVE:
                    raise AsOfError(f"the gate decided {g['decision']} for intent {g['intent_id']!r}: an order cannot be linked to it")
            terms = {"symbol": str(symbol), "side": s, "size": self._num(size, "size", positive=True), "intended_price": self._num(intended_price, "intended_price", positive=True, optional=True),
                     "expected_price": self._num(expected_price, "expected_price", positive=True, optional=True), "stop_loss": self._num(stop_loss, "stop_loss", positive=True, optional=True),
                     "take_profit": self._num(take_profit, "take_profit", positive=True, optional=True)}
            stop_f, target_f = self._num(stop_loss, "stop_loss", positive=True, optional=True), self._num(take_profit, "take_profit", positive=True, optional=True)
            if stop_f is not None and target_f is not None:
                intrabar_ambiguous(2.0, 1.0, s, stop_f, target_f)                              # raises if the stop is on the wrong side of the target
            call_id = self._new_call()
            ev = self._spec("order", {"order_id": order_id, "decision_call_id": decision, "intent_call_id": intent, "expected_source": expected_source, "terms": terms}, raw=("terms",), call_id=call_id)
            self._orders[order_id] = {"time": self.clock.now, "size": terms["size"], "filled": 0.0, "side": s, "stop": terms["stop_loss"], "target": terms["take_profit"]}
            self.ledger.append_many([ev])
            return order_id

    def fill(self, order_id: str, /, *, price: float, size: float, fees: float | None = None, filled_at: TimeLike | None = None, fill_id: str | None = None, reference_price: float | None = None,
             bar: Mapping[str, Any] | Record | None = None) -> str:
        """Record a fill of an order. ``filled_at`` (default: now) is when it happened and cannot be after the session clock or before the order; the event is stamped ``event_time = filled_at``
        and ``available_at = T``. ``fees`` are the explicit costs (``None`` = not recorded, which a report treats as unknown, never as free). ``reference_price`` is the market price at the fill (for splitting slippage from timing later). If the order carries both a stop loss
        and a take profit and you pass the OHLC ``bar`` the fill happened in, the event records whether both levels lay inside that bar (``intrabar_ambiguous``): then the bar alone cannot say
        which was hit first, and a report labels the exit **Observed**, not Verified. Without a bar it is recorded as unknown (``None``)."""
        with self._lock:
            self._check_open()
            o = self._orders.get(order_id)
            if o is None:
                raise AsOfError(f"order {order_id!r} was not recorded in this session")
            when = self.clock.now if filled_at is None else parse_time(filled_at, what="filled_at")
            if when > self.clock.now:
                raise ClockError(f"a fill at {iso(when)} cannot be recorded at session time {iso(self.clock.now)}: it has not happened yet")
            if when < o["time"]:
                raise AsOfError(f"a fill at {iso(when)} is before its order was placed at {iso(o['time'])}")
            terms = {"price": self._num(price, "price", positive=True), "size": self._num(size, "size", positive=True), "fees": self._num(fees, "fees", optional=True),
                     "filled_at": iso(when), "reference_price": self._num(reference_price, "reference_price", positive=True, optional=True)}
            if terms["fees"] is not None and terms["fees"] < 0:                    # type: ignore[operator]
                raise AsOfError("fees cannot be negative")
            if o["filled"] + terms["size"] > o["size"] + 1e-9:                    # type: ignore[operator]
                raise AsOfError(f"fills would exceed the order size ({o['filled'] + terms['size']} of {o['size']})")
            ambiguous: bool | None = None
            if bar is not None and o["stop"] is not None and o["target"] is not None:
                b = bar.payload if isinstance(bar, Record) else bar
                ambiguous = intrabar_ambiguous(float(b["high"]), float(b["low"]), o["side"], o["stop"], o["target"])
            fid = fill_id or f"{order_id}-f{len([1 for _ in self._fills.get(order_id, [])]) + 1}"
            if fid in {f for fs in self._fills.values() for f in fs}:
                raise AsOfError(f"fill {fid!r} was already recorded")
            o["filled"] += terms["size"]
            self._fills.setdefault(order_id, []).append(fid)
            self.ledger.append_many([self._spec("fill", {"order_id": order_id, "fill_id": fid, "intrabar_ambiguous": ambiguous, "terms": terms}, raw=("terms",), call_id=self._new_call(),
                                                event_time=when)])
            return fid

    def mark(self, symbol: str, /, *, size: float, price: float, marked_at: TimeLike | None = None) -> None:
        """Record a position mark: ``size`` of ``symbol`` valued at ``price`` at ``marked_at`` (default now, never after the session clock). The values are payload (hashed in ``hashes_only``)."""
        with self._lock:
            self._check_open()
            when = self.clock.now if marked_at is None else parse_time(marked_at, what="marked_at")
            if when > self.clock.now:
                raise ClockError(f"a mark at {iso(when)} cannot be recorded at session time {iso(self.clock.now)}: it has not happened yet")
            terms = {"symbol": str(symbol), "size": self._num(size, "size"), "price": self._num(price, "price", positive=True), "marked_at": iso(when)}
            self.ledger.append_many([self._spec("position_mark", {"terms": terms}, raw=("terms",), call_id=self._new_call(), event_time=when)])

    # ------------------------------------------------------------------ sources and queries (R1-R3)
    def add_source(self, source: Source, *, lazy: bool = False) -> str:
        """Register a source under ``source.name``. By default it is loaded now, so data-quality problems (missing or zone-less timestamps)
        show up immediately in the ledger; ``lazy=True`` waits for the first query."""
        with self._lock:
            self._check_open()
            name = getattr(source, "name", None)
            if not name or not isinstance(name, str):
                raise SourceError("a source needs a name")
            if name in self._sources:
                raise SourceError(f"a source named {name!r} is already added")
            self._sources[name] = source
            self._log("source_added", {"name": name, "type": type(source).__name__})
            if not lazy:
                self._load(name)
            return name

    def _load(self, name: str) -> tuple[list[Record], str]:
        if name in self._loaded:
            return self._loaded[name]
        src = self._sources[name]
        try:
            recs = list(src.records())
        except Exception as e:  # noqa: BLE001 - logged, then re-raised unchanged
            self._log("source_error", {"name": name, **self._error_payload(e)}, raw=("error",))
            raise
        for r in recs:
            if not isinstance(r, Record):
                raise SourceError(f"source {name!r} must yield Record objects, got {type(r).__name__}")
            if not r.verify():
                raise SourceError(f"source {name!r}: record {r.record_id!r} has a payload_sha256 that does not match its payload")
        fp = sha256_hex("\n".join(sorted(f"{r.record_id}|{iso(r.available_at)}|{r.payload_sha256}" for r in recs)))
        flags = Counter(f for r in recs for f in r.flags)
        self._loaded[name] = (recs, fp)                                   # a snapshot: the session sees one consistent version of the data
        notes = src.quality_notes() if callable(getattr(src, "quality_notes", None)) else None             # type: ignore[attr-defined]
        q = dq.analyse_source(recs, now=_wall(), kind=getattr(src, "quality_kind", dq.SERIES), notes=notes, calendar=getattr(src, "calendar", None))
        self._quality[name] = q
        self._log("source_loaded", {"name": name, "n_records": len(recs), "n_record_ids": len({r.record_id for r in recs}),
                                     "n_with_available_at": sum(r.available_at is not None for r in recs),
                                     "n_without_available_at": sum(r.available_at is None for r in recs), "flags": dict(sorted(flags.items())),
                                     "fingerprint_sha256": fp, "quality": q})
        return self._loaded[name]

    def quality(self, source: str | None = None) -> dict[str, Any]:
        """The data-quality summary of one source (or of every loaded source, keyed by name): timestamp coverage, suspicious timing, bursts, restatements, duplicates, gaps.
        It looks at the whole source, **including records after the session clock**, so it is for the operator and must not be shown to the agent (see :mod:`asof.quality`)."""
        with self._lock:
            if source is not None:
                if source not in self._sources:
                    raise SourceError(f"unknown source {source!r}; added sources are {self.sources}")
                self._load(source)
                return dict(self._quality[source])
            for n in list(self._sources):
                self._load(n)
            return {n: dict(q) for n, q in sorted(self._quality.items())}

    def members(self, universe: str, /, **match: Any) -> list[str]:
        """Who was in a :class:`~asof.sources.Universe` at the session clock, as it was knowable then (see :mod:`asof.sources.universe`). Logged as an ordinary query, with its receipt."""
        with self._lock:
            src = self._sources.get(universe)
            if not isinstance(src, Universe):
                raise SourceError(f"{universe!r} is not a Universe source; added sources are {self.sources}")
            served = self.query(universe, **match)
            return list(members_as_of(served, self.clock.now, member=src.member_field, action=src.action_field).members)

    def query(self, source: str, /, *, filters: Mapping[str, Any] | None = None, lookback: Any = None, limit: int | None = None,
              universe: tuple[str, str] | None = None, **match: Any) -> list[Record]:
        """Time-locked read. ``match`` keywords (``symbol="ACME-SYN"``) are equality filters on the payload (a list means any-of); ``lookback="30d"``
        keeps records whose ``event_time`` is within that window before T. Returns the records served; see :meth:`receipt` for what was blocked.

        ``universe=("sp500", "symbol")`` keeps only records whose ``symbol`` was a member of the :class:`~asof.sources.Universe` named ``sp500`` **at T** (as knowable then): the
        way to avoid survivorship bias. The membership lookup is its own logged query; this query's receipt says how many records the restriction removed (``universe.excluded``).
        (A payload field literally named ``universe`` can be matched with ``filters=``.)"""
        with self._lock:
            self._check_open()
            if source not in self._sources:
                raise SourceError(f"unknown source {source!r}; added sources are {self.sources}")
            recs, fp = self._load(source)
            f = {**(filters or {}), **match}
            lb = parse_duration(lookback) if lookback is not None else None
            uni: dict[str, Any] | None = None
            run_filters = f
            if universe is not None:
                uname, ufield = universe
                members = self.members(uname)
                uni_call = self._receipts[-1].call_id
                run_filters = {**f, ufield: members}                                  # an empty universe serves nothing: the honest answer, not "no restriction"
                everyone = pit_query(recs, self.clock.now, filters=f, lookback=lb, limit=None, allow_untimestamped=self.allow_untimestamped).counts["served"]
                members_only = pit_query(recs, self.clock.now, filters=run_filters, lookback=lb, limit=None, allow_untimestamped=self.allow_untimestamped).counts["served"]
                uni = {"source": uname, "field_sha256": digest(ufield), "n_members": len(members), "members_sha256": digest(members), "universe_call_id": uni_call,
                       "excluded": everyone - members_only}
                if self.privacy == "full":
                    uni["field"] = ufield
            res = pit_query(recs, self.clock.now, filters=run_filters, lookback=lb, limit=limit, allow_untimestamped=self.allow_untimestamped, max_listed=self.max_listed_blocked)
            visible = [r for r in recs if r.available_at is not None and r.available_at <= self.clock.now]
            src_obj = self._sources[source]
            qq = dq.query_quality(visible, res.records, self.clock.now, kind=getattr(src_obj, "quality_kind", dq.SERIES), calendar=getattr(src_obj, "calendar", None))
            qq["source"] = {"flags": list(self._quality[source]["flags"]), "summary_sha256": digest(self._quality[source])}
            if isinstance(src_obj, Universe):
                m = members_as_of(res.records, self.clock.now, member=src_obj.member_field, action=src_obj.action_field)
                qq["membership"] = {"n_members": len(m.members), "members_sha256": digest(list(m.members)), "pending_adds": m.pending_adds, "pending_removes": m.pending_removes,
                                    "duplicate_adds": m.duplicate_adds, "orphan_removes": m.orphan_removes}
                if m.duplicate_adds or m.orphan_removes:
                    qq["flags"] = sorted({*qq["flags"], "membership_inconsistent"})
                if src_obj.listed_in is not None:                                    # survivorship: names trading at T that this universe leaves out
                    if src_obj.listed_in not in self._sources:
                        raise SourceError(f"universe {source!r}: listed_in={src_obj.listed_in!r} is not a source in this session; added sources are {self.sources}")
                    lsrc = self._sources[src_obj.listed_in]
                    lrecs, _ = self._load(src_obj.listed_in)
                    sv = dq.survivorship(m.members, lrecs, self.clock.now, field=src_obj.listed_field or src_obj.member_field, calendar=getattr(lsrc, "calendar", None))
                    names = sv.pop("listed_not_members")
                    qq["membership"]["listed"] = {"source": src_obj.listed_in, **sv, **({"listed_not_members": names} if self.privacy == "full" else {})}
                    if names:
                        qq["flags"] = sorted({*qq["flags"], "survivorship"})
            call_id = self._new_call()
            specs: list[EventSpec] = []
            for reason, listed, n in (("future", res.blocked_future, res.counts["blocked_future"]),
                                      ("untimestamped", res.blocked_untimestamped, res.counts["blocked_untimestamped"])):
                if n:
                    specs.append(self._spec("block", {"kind": "query", "target": source, "reason": reason, "label": f"blocked_{reason}", "count": n, "listed_record_ids": [i for i, _ in listed],
                                                      "listed_payload_sha256": [h for _, h in listed], "list_truncated": n > len(listed)},
                                            raw=("listed_record_ids",), call_id=call_id))
            unknown = set(res.served_unknown_ids)
            served = list(res.records)
            for r in served:                                              # a served record keeps ITS OWN two clocks (A1)
                specs.append(self._spec("record_served", {"source": source, "record_id": r.record_id, "timing": "unknown" if r.record_id in unknown else "declared",
                                                          "flags": list(r.flags), "record_payload": dict(r.payload), "payload_sha256": r.payload_sha256},
                                        raw=("record_id", "record_payload"), call_id=call_id, event_time=r.event_time, available_at=r.available_at))
            specs.append(self._spec("query", {"source": source, "filters": f, "lookback": None if lb is None else lb.total_seconds(), "limit": limit,
                                              "source_fingerprint_sha256": fp, "counts": dict(res.counts), "warnings": list(res.warnings), "served": len(served),
                                              "quality": qq, **({"universe": uni} if uni is not None else {})},
                                    raw=("filters",), call_id=call_id))
            evs = self.ledger.append_many(specs)
            self._track("query", call_id, evs)
            self._receipts.append(build_receipt(evs, self.privacy))
            self._pending_actions += 1
            return served

    # ------------------------------------------------------------------ tools (R4)
    def add_tool(self, name: str, fn: Callable[..., Any], *, timestamp_field: str = "timestamp", items: str | None = None, pass_as_of: bool = False,
                 assume_tz: str | None = None) -> None:
        """Register a tool. Its results must carry timestamps in ``timestamp_field``; see :mod:`asof.tools` for exactly how results are read."""
        with self._lock:
            self._check_open()
            self._tools.register(name, fn, timestamp_field=timestamp_field, items=items, pass_as_of=pass_as_of, assume_tz=assume_tz)
            self._log("tool_added", {"name": name, "timestamp_field": timestamp_field, "pass_as_of": pass_as_of})

    def tool(self, name: str, /, **kwargs: Any) -> list[Any]:
        """Call a registered tool through the proxy. Items dated after T are dropped; undated items are withheld unless the session allows them."""
        with self._lock:
            self._check_open()
            call_id = self._new_call()
            try:
                out = self._tools.call(name, self.clock.now, self.allow_untimestamped, kwargs)
            except Exception as e:  # noqa: BLE001 - logged, then re-raised unchanged
                self._log("tool_error", {"tool": name, "args": kwargs, **self._error_payload(e)}, raw=("args", "error"), call_id=call_id)
                raise
            try:
                served_hashes = [payload_sha256(i) for i in out.served]
            except TypeError as e:
                raise ToolError(f"tool {name!r} returned something AsOf cannot hash ({e}); return JSON-serialisable data") from e
            specs: list[EventSpec] = []
            for reason, hashes in (("future", out.blocked_future), ("untimestamped", out.blocked_untimestamped)):
                if hashes:
                    specs.append(self._spec("block", {"kind": "tool", "target": name, "reason": reason, "label": f"blocked_{reason}", "count": len(hashes), "listed_payload_sha256": list(hashes[:self.max_listed_blocked]),
                                                      "list_truncated": len(hashes) > self.max_listed_blocked}, call_id=call_id))
            unknown_ids = {id(i) for i in out.unknown}
            for item, ts, h in zip(out.served, out.served_times, served_hashes, strict=True):      # a served item keeps ITS OWN declared time (A1)
                specs.append(self._spec("tool_item_served", {"tool": name, "timing": "unknown" if id(item) in unknown_ids else "declared", "item": item, "payload_sha256": h},
                                        raw=("item",), call_id=call_id, event_time=None, available_at=ts))
            specs.append(self._spec("tool_call", {"tool": name, "args": kwargs, "n_items": out.n_items, "served": len(out.served), "served_unknown_timing": len(out.unknown),
                                                  "blocked_future": len(out.blocked_future), "blocked_untimestamped": len(out.blocked_untimestamped)},
                                    raw=("args",), call_id=call_id))
            evs = self.ledger.append_many(specs)
            self._track("tool", call_id, evs)
            self._receipts.append(build_receipt(evs, self.privacy))
            self._pending_actions += 1
            return list(out.served)

    # ------------------------------------------------------------------ models (R6, R8)
    def register_provider(self, provider: str, fn: Provider) -> None:
        """Add a model provider (``fn(ModelRequest) -> ModelResponse``) next to the built-in anthropic, openai and local ones."""
        self.gateway.register(provider, fn)

    def refuse_model_call(self, model: Any, messages: Any, rule: str, message: str) -> None:
        """Log that a model call was refused by the operator's policy BEFORE anything was sent (for example by the MCP server: a model the operator did not list, or an attempt to set
        the key, the address or the timeout), then raise :class:`~asof.errors.GatewayRuleError`. The event has the same shape as any other refused model call: ``gateway.blocked`` is true
        and ``gateway.rule`` says which rule. The model name is cut to 200 characters; parameter values are never recorded."""
        with self._lock:
            self._check_open()
            try:
                prompt = digest(list(messages)) if isinstance(messages, (list, tuple)) else digest(str(messages))
            except TypeError:
                prompt = digest(repr(messages))
            err = {"model": str(model)[:200], "prompt_sha256": prompt, "error_type": "GatewayRuleError", "error": redact(message),
                   "gateway": {"rules_version": GATEWAY_RULES_VERSION, "blocked": True, "rule": rule, "clock_T": iso(self.clock.now)}}
            self._log("model_error", err, raw=("error",), call_id=self._new_call())
            raise GatewayRuleError(rule, message)

    def model(self, model: str | None = None, /, *, messages: Sequence[Mapping[str, Any]], **params: Any) -> ModelResponse:
        """Call a model through the gateway and log it. ``params`` go to the provider (``api_key=``, ``base_url=`` and ``timeout=`` are used by
        AsOf and never logged). AsOf does not and cannot make a model forget what it was trained on: see the receipt's model block."""
        with self._lock:
            self._check_open()
            model = model or self.default_model
            if not model:
                raise GatewayError("no model given and the session has no default model")
            T = self.clock.now
            call_id = self._new_call()
            logged_params = {k: v for k, v in params.items() if k not in RESERVED}
            ins = None
            try:
                ins = self.gateway.inspect(model, params)
                resp, req = self.gateway.call(model, messages, params)        # call() enforces the gateway rules again: nothing is sent to a provider that the policy refuses
            except Exception as e:  # noqa: BLE001 - logged, then re-raised unchanged
                err = {"model": model, "prompt_sha256": digest(list(messages)), **self._error_payload(e)}
                if isinstance(e, GatewayRuleError):                              # a refused attempt is itself a ledger event: what was tried, and which rule stopped it
                    err["gateway"] = {"rules_version": GATEWAY_RULES_VERSION, "blocked": True, "rule": e.rule, "clock_T": iso(T)}
                self._log("model_error", err, raw=("error",), call_id=call_id)
                raise
            assert ins is not None
            flags = self.gateway.assess(model, ins, resp)
            gw = {"rules_version": GATEWAY_RULES_VERSION, "blocked": False, "clock_T": iso(T), "provider_verified": self.gateway.provider_verified(req.provider),
                  "server_tools_requested": list(ins.server_tools), "server_tools_reported": list(resp.server_tools_used),
                  "server_state": "provider_state_allowed" if ins.state_params else "none", "n_messages_sent": len(messages),       # the full conversation is sent on every call
                  "reasoning_effort": modelrules.reasoning_effort(params), "tokens": modelrules.normalise_usage(resp.usage), "tool_calls_requested": len(resp.tool_calls),
                  "pinned_version": self.gateway.policy.pins.get(model), "flags": flags}
            prof = contam.lookup(model)
            after = None if prof.declared_cutoff is None else prof.declared_cutoff > T
            warnings = []
            if prof.declared_cutoff is None:
                warnings.append("the model's training cutoff is unknown: it may remember events after T")
            elif after:
                warnings.append(f"the model's declared training cutoff ({iso(prof.declared_cutoff)}, declared by {prof.declared_by}) is after as_of ({iso(T)}): "
                                "it may know events after T")
            payload = {"model_id": model, "provider": req.provider, "version_reported": resp.model_version, "declared_cutoff": iso(prof.declared_cutoff),
                       "declared_by": prof.declared_by, "declared_cutoff_after_as_of": after, "contamination_score": prof.contamination_score,
                       "score_source": prof.score_source, "label": prof.label, "warnings": warnings,
                       "params": logged_params, "param_sha256": {k: digest(v) for k, v in sorted(logged_params.items())},
                       "prompt_sha256": digest(list(messages)), "response_sha256": digest(resp.text), "finish_reason": resp.finish_reason,
                       "usage": dict(resp.usage), "gateway": gw, "messages": list(messages), "response_text": resp.text}
            ev = self.ledger.append_many([self._spec("model_call", payload, raw=("messages", "response_text", "params"), call_id=call_id)])
            self._track("model", call_id, ev)
            for f in flags:
                self._flags.append({**f, "call_id": call_id, "model": model, "clock_T": iso(T)})
            self._receipts.append(build_receipt(ev, self.privacy))
            self._pending_actions += 1
            return resp

    def run_tool_calls(self, resp: ModelResponse) -> list[dict[str, Any]]:
        """Carry out the tool calls a model asked for, **through the AsOf tool proxy** (so each is time-locked and logged like any other tool call). The gateway never runs a tool itself.
        Returns ``[{"id", "name", "result"}]`` or ``{"id", "name", "error"}`` for a call that was refused (an unknown tool, or one that returned something that cannot be time-checked)."""
        out: list[dict[str, Any]] = []
        for c in resp.tool_calls:
            name, args = str(c.get("name")), dict(c.get("arguments") or {})
            try:
                out.append({"id": c.get("id"), "name": name, "result": self.tool(name, **args)})
            except (ToolError, AsOfError) as e:
                out.append({"id": c.get("id"), "name": name, "error": str(e)})
        return out

    @property
    def flags(self) -> list[dict[str, Any]]:
        """What the gateway flagged on this session's model calls (each has ``kind``, ``call_id`` and details). Empty means nothing was flagged, not that nothing could be."""
        return [dict(f) for f in self._flags]

    @property
    def clean(self) -> bool:
        """False once any model call was flagged (a provider-side tool, an allowed policy exception, a version-pin miss). A flagged run cannot be sealed as clean."""
        return not self._flags

    def model_receipt(self, call_id: str | None = None) -> dict[str, Any]:
        """The flat receipt of a model call (the last one by default): run id, clock T, provider, model and version, reasoning effort, settings, prompt and output hashes, tokens, provider-side tools
        used, items served and blocked so far, labels, a data-quality summary, and the previous and own hash. See :func:`asof.receipt.model_receipt`."""
        with self._lock:
            if call_id is None:
                last = [e for e in self.ledger.events(session_id=self.id, type="model_call")]
                if not last:
                    raise AsOfError("this session has made no model call")
                call_id = str(last[-1].call_id)
            return model_receipt(self.ledger, self.id, call_id, data_quality=self.quality())

    def recall(self, source: str, text: str, /, *, limit: int = 5, tags: Sequence[str] | None = None) -> list[Recalled]:
        """Recall from a :class:`~asof.memory.Memory` added as a source: a time-locked, logged query (so the receipt says how many memories were learned after T and withheld), then keyword ranking of what was served.
        Memories learned after the session clock are never returned."""
        return rank(self.query(source), text, limit=limit, tags=tags)

    def show(self, widget: str, **opts: Any) -> Any:
        """Render an AsOf widget for this session (``"receipt"``, ``"clock-timeline"``, ``"quality-panel"``, ``"chain-status"``, ``"rolling-sharpe"``, ``"leak-overlay"``, ``"gap-breakdown"`` or a registered one).
        In Jupyter it displays itself; ``.html()`` and ``.save(path)`` write it out. ``synthetic=True`` adds the small "synthetic" tag. See ``asof/widgets/README.md``."""
        from .widgets import show
        return show(self, widget, **opts)

    # ------------------------------------------------------------------ receipts, ledger, lifecycle
    @overload
    def receipt(self, last: Literal[False], *, call_id: None = None) -> list[Receipt]: ...
    @overload
    def receipt(self, last: Literal[True] = True, *, call_id: str | None = None) -> Receipt: ...
    def receipt(self, last: bool = True, *, call_id: str | None = None) -> Receipt | list[Receipt]:
        """``receipt()`` is the most recent call's receipt; ``receipt(call_id=...)`` a specific one; ``receipt(last=False)`` all of them.
        Receipts are for the operator or auditor: do not put them in the agent's context (blocked counts reveal that something exists after T);
        use ``receipt.agent_view()`` for a version that is safe to show a model."""
        if call_id is not None:
            for r in self._receipts:
                if r.call_id == call_id:
                    return r
            raise AsOfError(f"no receipt with call id {call_id!r} in this session")
        if not last:
            return list(self._receipts)
        if not self._receipts:
            raise AsOfError("no calls have been made in this session yet")
        return self._receipts[-1]

    def _reader(self) -> tuple[Ledger, bool]:
        """The ledger to read from. After ``close()`` an owned ledger has been closed, but the audit trail must stay readable: reopen the file."""
        if self._closed and self._owns_ledger:
            return Ledger(self.ledger.path, head_path=self.ledger.head_path), True
        return self.ledger, False

    def verify_ledger(self, expected_head: Any = None, *, anchor: str = "require") -> VerifyResult:
        """Recompute the ledger's hash chain from the start and check the tail anchor. See :mod:`asof.ledger` for what this can and cannot detect."""
        led, temp = self._reader()
        try:
            return led.verify(expected_head, anchor=anchor)
        finally:
            if temp:
                led.close()

    def ledger_head(self) -> dict[str, Any]:
        """The newest event as ``{seq, hash, recorded_at}``: store it somewhere you trust to detect truncation later."""
        led, temp = self._reader()
        try:
            return led.head().to_dict()
        finally:
            if temp:
                led.close()

    def events(self, type: str | None = None, call_id: str | None = None) -> list[LedgerEvent]:
        """This session's ledger events (all of them, or one type / one call). Still works after ``close()``."""
        led, temp = self._reader()
        try:
            return led.events(session_id=self.id, type=type, call_id=call_id)
        finally:
            if temp:
                led.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._log("session_end", {"n_calls": len(self._receipts)})
            self._closed = True
            if self._owns_ledger:
                self.ledger.close()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
