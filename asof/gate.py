"""The decision gate: a pure function from an intent, a policy and the state so far to ``APPROVE`` / ``HOLD`` / ``BLOCK`` with reasons.

* ``BLOCK``   a hard rule is broken and waiting will not fix it: a symbol that is not allowed, a size or exposure over a limit, a model or model version that is not allowed, a duplicate intent.
* ``HOLD``    not now, or not without a person: the kill switch is on, the data is too old (or its age is unknown), the rate limit is used up, or the size is over the human-approval threshold.
* ``APPROVE`` every check passed.

Precedence is BLOCK over HOLD over APPROVE, and **a check that cannot be made is never a pass**: if the policy asks for a limit and the intent does not carry what is needed to test it (no price for a notional
limit, no data time for freshness), that check is ``unknown`` and the decision is at best HOLD.

This module is deterministic and pure: it reads no clock (the caller passes ``now``), keeps no state (the caller passes the history), and **sends nothing anywhere**. Strathora never sends orders; ``approve`` here
means "this intent is inside the policy", and what is done with that is the agent's own business, recorded in the ledger by ``Session.gate`` / ``Session.order``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping, Sequence

from .clock import TimeLike, parse_time
from .errors import AsOfError

APPROVE, HOLD, BLOCK = "APPROVE", "HOLD", "BLOCK"
DECISIONS = (APPROVE, HOLD, BLOCK)
PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"
SIDES = ("buy", "sell")


class GateError(AsOfError, ValueError):
    """An intent or a policy is malformed (not a refusal: a refusal is a BLOCK decision)."""


@dataclass(frozen=True)
class Intent:
    """What an agent wants to do, before any order exists. ``intent_id`` is the idempotency key: the same id with the same content is a duplicate; the same id with different content is a conflict."""
    intent_id: str
    symbol: str
    side: str
    size: float
    price: float | None = None                      # the agent's reference price, needed only to test notional limits
    agent_id: str | None = None
    model: str | None = None                        # "provider:name"
    model_version: str | None = None                # the version the provider reported having served
    data_as_of: TimeLike | None = None              # the latest availability time among the data this intent rests on (freshness is measured from it)

    def content(self) -> tuple[str, str, float, float | None, str | None]:
        return (self.symbol.upper(), self.side, float(self.size), None if self.price is None else float(self.price), self.agent_id)


@dataclass(frozen=True)
class Policy:
    """Every field is optional: a limit that is not set is not checked (and says so). Set ``kill_switch`` to hold everything."""
    kill_switch: bool = False
    max_staleness_seconds: float | None = None
    symbol_allowlist: Sequence[str] | None = None
    max_order_size: float | None = None
    max_position: float | None = None               # absolute position in units of the symbol, after this intent
    max_notional: float | None = None               # size x price of this intent (needs a price)
    allowed_models: Mapping[str, Sequence[str]] | None = None     # {"provider:name": ["version prefix", ...]}; an empty list allows any version of that model
    rate_limit: tuple[int, float] | None = None     # (max intents, per this many seconds), counted over the history passed in
    approval_size: float | None = None              # at or above this size a human must approve (HOLD)
    duplicate_window_seconds: float | None = None   # how far back an intent id counts as already seen; None = any time


@dataclass(frozen=True)
class History:
    """What has already happened, supplied by the caller. ``seen`` is ``[(intent_id, content, time)]`` for earlier intents; ``positions`` is ``{SYMBOL: size}``."""
    seen: Sequence[tuple[str, tuple, TimeLike]] = ()
    positions: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Check:
    name: str
    status: str                                     # pass | fail | unknown | not_set
    outcome: str                                    # what this check contributes: APPROVE | HOLD | BLOCK
    detail: str


@dataclass(frozen=True)
class GateResult:
    decision: str
    reasons: tuple[Check, ...]
    intent_id: str
    sends_orders: bool = False                      # always False: the gate decides, it does not trade
    call_id: str | None = None                      # set by Session.gate: the ledger call that holds the intent and this decision (an order links to it)

    def as_event(self) -> dict[str, Any]:
        """The ledger form: check names and statuses are metadata; the human-readable details (which can hold sizes and prices) are returned separately so a privacy mode can hash them."""
        return {"intent_id": self.intent_id, "decision": self.decision, "reasons": [{"check": c.name, "status": c.status, "outcome": c.outcome} for c in self.reasons],
                "details": [c.detail for c in self.reasons]}

    @property
    def blocked_by(self) -> list[str]:
        return [c.name for c in self.reasons if c.outcome == BLOCK]

    @property
    def held_by(self) -> list[str]:
        return [c.name for c in self.reasons if c.outcome == HOLD]


def _check(name: str, status: str, outcome: str, detail: str) -> Check:
    return Check(name, status, outcome, detail)


def _validate(i: Intent) -> None:
    if not i.intent_id or not str(i.intent_id).strip():
        raise GateError("an intent needs an intent_id (the idempotency key)")
    if not i.symbol or not str(i.symbol).strip():
        raise GateError("an intent needs a symbol")
    if i.side not in SIDES:
        raise GateError(f"side must be one of {SIDES}, not {i.side!r}")
    if not isinstance(i.size, (int, float)) or isinstance(i.size, bool) or not (i.size > 0) or i.size != i.size or i.size in (float("inf"),):
        raise GateError("size must be a positive finite number")
    if i.price is not None and (not isinstance(i.price, (int, float)) or isinstance(i.price, bool) or not (i.price > 0) or i.price in (float("inf"),)):
        raise GateError("price, if given, must be a positive finite number")


def evaluate(intent: Intent, policy: Policy, history: History | None = None, *, now: TimeLike) -> GateResult:
    """Decide. ``now`` is the time to measure against (for a session, the session clock: not the wall clock, so a backtest is gated as of its own T)."""
    _validate(intent)
    h = history or History()
    t = parse_time(now, what="now")
    sym = intent.symbol.strip().upper()
    out: list[Check] = []

    # 1. kill switch: one flag holds everything
    out.append(_check("kill_switch", FAIL if policy.kill_switch else PASS, HOLD if policy.kill_switch else APPROVE,
                      "the kill switch is on: everything is held" if policy.kill_switch else "the kill switch is off"))

    # 2. duplicate intent (idempotency)
    prior = [(iid, content, parse_time(at, what="history time")) for iid, content, at in h.seen if iid == intent.intent_id]
    if policy.duplicate_window_seconds is not None:
        prior = [p for p in prior if (t - p[2]) <= timedelta(seconds=policy.duplicate_window_seconds)]
    if prior:
        same = any(tuple(c) == intent.content() for _, c, _ in prior)
        out.append(_check("duplicate_intent", FAIL, BLOCK, f"intent id {intent.intent_id!r} was already used" + (" for the same content (a repeat)" if same else " for DIFFERENT content (a conflict)")))
    else:
        out.append(_check("duplicate_intent", PASS, APPROVE, f"intent id {intent.intent_id!r} has not been used"))

    # 3. data freshness
    if policy.max_staleness_seconds is None:
        out.append(_check("data_freshness", "not_set", APPROVE, "no maximum staleness is set"))
    elif intent.data_as_of is None:
        out.append(_check("data_freshness", UNKNOWN, HOLD, "the intent does not say how old its data is, so freshness cannot be shown"))
    else:
        age = (t - parse_time(intent.data_as_of, what="data_as_of")).total_seconds()
        if age < 0:
            out.append(_check("data_freshness", FAIL, HOLD, f"the data is dated {-age:.0f} s AFTER now, which is not possible: it cannot be trusted"))
        elif age > policy.max_staleness_seconds:
            out.append(_check("data_freshness", FAIL, HOLD, f"the data is {age:.0f} s old; the maximum is {policy.max_staleness_seconds:.0f} s"))
        else:
            out.append(_check("data_freshness", PASS, APPROVE, f"the data is {age:.0f} s old (maximum {policy.max_staleness_seconds:.0f} s)"))

    # 4. symbol allowlist
    if policy.symbol_allowlist is None:
        out.append(_check("symbol_allowlist", "not_set", APPROVE, "no symbol allowlist is set"))
    else:
        ok = sym in {str(s).strip().upper() for s in policy.symbol_allowlist}
        out.append(_check("symbol_allowlist", PASS if ok else FAIL, APPROVE if ok else BLOCK, f"{sym} is {'' if ok else 'not '}on the allowlist"))

    # 5. size and exposure limits
    if policy.max_order_size is None:
        out.append(_check("max_order_size", "not_set", APPROVE, "no order-size limit is set"))
    else:
        ok = intent.size <= policy.max_order_size
        out.append(_check("max_order_size", PASS if ok else FAIL, APPROVE if ok else BLOCK, f"size {intent.size:g} {'is within' if ok else 'exceeds'} the limit {policy.max_order_size:g}"))
    if policy.max_position is None:
        out.append(_check("max_position", "not_set", APPROVE, "no position limit is set"))
    else:
        after = float(h.positions.get(sym, 0.0)) + (intent.size if intent.side == "buy" else -intent.size)
        ok = abs(after) <= policy.max_position
        out.append(_check("max_position", PASS if ok else FAIL, APPROVE if ok else BLOCK, f"the position would be {after:g}; the limit is {policy.max_position:g}"))
    if policy.max_notional is None:
        out.append(_check("max_notional", "not_set", APPROVE, "no notional limit is set"))
    elif intent.price is None:
        out.append(_check("max_notional", UNKNOWN, HOLD, "a notional limit is set but the intent has no price, so its value cannot be computed"))
    else:
        n = intent.size * intent.price
        ok = n <= policy.max_notional
        out.append(_check("max_notional", PASS if ok else FAIL, APPROVE if ok else BLOCK, f"notional {n:,.2f} {'is within' if ok else 'exceeds'} the limit {policy.max_notional:,.2f}"))

    # 6. model and version
    if policy.allowed_models is None:
        out.append(_check("model_allowed", "not_set", APPROVE, "no model allowlist is set"))
    elif not intent.model:
        out.append(_check("model_allowed", UNKNOWN, HOLD, "a model allowlist is set but the intent does not say which model produced it"))
    else:
        versions = policy.allowed_models.get(intent.model)
        if versions is None:
            out.append(_check("model_allowed", FAIL, BLOCK, f"{intent.model} is not an allowed model"))
        elif len(versions) == 0:
            out.append(_check("model_allowed", PASS, APPROVE, f"{intent.model} is allowed (any version)"))
        elif not intent.model_version:
            out.append(_check("model_allowed", UNKNOWN, HOLD, f"{intent.model} is allowed only for pinned versions, and the intent does not say which version served it"))
        else:
            ok = any(intent.model_version.startswith(v) for v in versions)
            out.append(_check("model_allowed", PASS if ok else FAIL, APPROVE if ok else BLOCK, f"{intent.model} version {intent.model_version} is {'' if ok else 'not '}an allowed version"))

    # 7. rate limit
    if policy.rate_limit is None:
        out.append(_check("rate_limit", "not_set", APPROVE, "no rate limit is set"))
    else:
        n_max, per = policy.rate_limit
        recent = [1 for iid, _, at in h.seen if iid != intent.intent_id and timedelta(0) <= (t - parse_time(at, what="history time")) <= timedelta(seconds=per)]
        ok = len(recent) < n_max
        out.append(_check("rate_limit", PASS if ok else FAIL, APPROVE if ok else HOLD, f"{len(recent)} intent(s) in the last {per:g} s; the limit is {n_max}"))

    # 8. human approval
    if policy.approval_size is None:
        out.append(_check("human_approval", "not_set", APPROVE, "no human-approval threshold is set"))
    else:
        needs = intent.size >= policy.approval_size
        out.append(_check("human_approval", FAIL if needs else PASS, HOLD if needs else APPROVE,
                          f"size {intent.size:g} is at or above {policy.approval_size:g}: a human must approve" if needs else f"size {intent.size:g} is below the human-approval threshold {policy.approval_size:g}"))

    decision = BLOCK if any(c.outcome == BLOCK for c in out) else HOLD if any(c.outcome == HOLD for c in out) else APPROVE
    return GateResult(decision, tuple(out), intent.intent_id)


def history_from(rows: Sequence[Mapping[str, Any]]) -> History:
    """A convenience for tests and callers: ``[{"intent_id", "content", "at"}]`` -> History (positions are supplied separately)."""
    return History(tuple((r["intent_id"], tuple(r["content"]), r["at"]) for r in rows))
