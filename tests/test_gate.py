"""The decision gate: a pure function (every check, precedence, unknown is never a pass), and the chain it makes in a session:
data seen -> model and version -> intent -> gate decision -> order -> fill. The gate sends nothing."""
from __future__ import annotations

import dataclasses
import json
import math

import pytest

from asof import AsOfError, Intent, ModelResponse
from asof.gate import APPROVE, BLOCK, HOLD, GateError, History, Policy, evaluate

from conftest import ListSource, rec

NOW = "2023-01-21T14:00:00Z"


def intent(**kw):
    base = dict(intent_id="i-1", symbol="acme", side="buy", size=10.0, price=100.0, agent_id="agent-a", model="anthropic:claude-x", model_version="claude-x-20250101", data_as_of="2023-01-21T13:59:00Z")
    base.update(kw)
    return Intent(**base)


def run(policy=None, i=None, history=None, now=NOW):
    return evaluate(i or intent(), policy or Policy(), history, now=now)


def status(r, name):
    return next(c for c in r.reasons if c.name == name)


# ---------------------------------------------------------------- the empty policy, and precedence
def test_with_no_limits_set_everything_is_not_set_and_the_decision_is_approve():
    r = run()
    assert r.decision == APPROVE and r.sends_orders is False and r.blocked_by == [] and r.held_by == []
    assert {c.status for c in r.reasons} <= {"pass", "not_set"} and status(r, "max_order_size").status == "not_set"
    assert [c.name for c in r.reasons] == ["kill_switch", "duplicate_intent", "data_freshness", "symbol_allowlist", "max_order_size", "max_position", "max_notional", "model_allowed", "rate_limit", "human_approval"]


def test_block_beats_hold_beats_approve_and_every_reason_is_listed_not_just_the_first():
    r = run(Policy(kill_switch=True, symbol_allowlist=["ZZZ"], max_order_size=5, approval_size=1))
    assert r.decision == BLOCK and set(r.blocked_by) == {"symbol_allowlist", "max_order_size"} and set(r.held_by) == {"kill_switch", "human_approval"}
    held = run(Policy(kill_switch=True))
    assert held.decision == HOLD and held.held_by == ["kill_switch"]


def test_the_result_is_deterministic_and_reads_no_wall_clock():
    p = Policy(max_staleness_seconds=600, max_order_size=50)
    a, b = run(p, now="2001-01-21T14:00:00Z", i=intent(data_as_of="2001-01-21T13:59:00Z")), run(p, now="2001-01-21T14:00:00Z", i=intent(data_as_of="2001-01-21T13:59:00Z"))
    assert a == b and a.decision == APPROVE                                                    # 22 years ago: judged as of `now`, not as of today


def test_results_and_inputs_are_immutable_and_the_gate_never_sends_anything():
    r = run()
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.decision = "APPROVE"                                                                 # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        Policy().kill_switch = True                                                            # type: ignore[misc]
    assert r.sends_orders is False and "send" not in "".join(dir(r)).replace("sends_orders", "")


# ---------------------------------------------------------------- each check
def test_kill_switch_holds_everything():
    r = run(Policy(kill_switch=True))
    assert status(r, "kill_switch").status == "fail" and r.decision == HOLD
    assert run(Policy(kill_switch=False)).decision == APPROVE


def test_duplicate_intent_same_content_is_a_repeat_and_different_content_is_a_conflict_both_block():
    i = intent()
    seen = [("i-1", i.content(), "2023-01-21T13:00:00Z")]
    rep = run(i=i, history=History(seen))
    assert rep.decision == BLOCK and "same content" in status(rep, "duplicate_intent").detail
    con = run(i=intent(size=99.0), history=History(seen))
    assert con.decision == BLOCK and "DIFFERENT content" in status(con, "duplicate_intent").detail
    assert run(i=intent(intent_id="i-2"), history=History(seen)).decision == APPROVE
    old = run(Policy(duplicate_window_seconds=60), i=i, history=History(seen))                 # seen an hour ago, window is a minute: not counted
    assert old.decision == APPROVE


@pytest.mark.parametrize("age,policy_max,decision,st", [(30, 60, APPROVE, "pass"), (60, 60, APPROVE, "pass"), (61, 60, HOLD, "fail"), (-5, 60, HOLD, "fail")])
def test_data_freshness_is_inclusive_at_the_maximum_and_future_dated_data_is_distrusted(age, policy_max, decision, st):
    from datetime import datetime, timedelta, timezone
    t = datetime(2023, 1, 21, 14, 0, tzinfo=timezone.utc)
    r = run(Policy(max_staleness_seconds=policy_max), i=intent(data_as_of=t - timedelta(seconds=age)))
    assert r.decision == decision and status(r, "data_freshness").status == st


def test_freshness_that_cannot_be_shown_is_unknown_and_holds_never_passes():
    r = run(Policy(max_staleness_seconds=60), i=intent(data_as_of=None))
    assert status(r, "data_freshness").status == "unknown" and r.decision == HOLD


def test_symbol_allowlist_is_case_insensitive_and_blocks_others():
    assert run(Policy(symbol_allowlist=["ACME", "beta"])).decision == APPROVE
    r = run(Policy(symbol_allowlist=["BETA"]))
    assert r.decision == BLOCK and r.blocked_by == ["symbol_allowlist"]


def test_size_and_position_limits_block_and_a_sell_reduces_the_position():
    assert run(Policy(max_order_size=10)).decision == APPROVE and run(Policy(max_order_size=9.99)).decision == BLOCK
    long = History(positions={"ACME": 95.0})
    assert run(Policy(max_position=100), i=intent(size=10.0), history=long).decision == BLOCK                 # 105 > 100
    assert run(Policy(max_position=100), i=intent(side="sell", size=10.0), history=long).decision == APPROVE   # 85
    short = History(positions={"ACME": -95.0})
    assert run(Policy(max_position=100), i=intent(side="sell", size=10.0), history=short).decision == BLOCK   # -105: absolute


def test_notional_limit_blocks_and_without_a_price_it_is_unknown_and_holds():
    assert run(Policy(max_notional=1000), i=intent(size=10, price=100)).decision == APPROVE
    assert run(Policy(max_notional=999.99), i=intent(size=10, price=100)).decision == BLOCK
    r = run(Policy(max_notional=1000), i=intent(price=None))
    assert status(r, "max_notional").status == "unknown" and r.decision == HOLD


def test_model_and_version_allowlist():
    allowed = {"anthropic:claude-x": ["claude-x-2025"], "openai:gpt-y": []}
    p = Policy(allowed_models=allowed)
    assert run(p).decision == APPROVE                                                              # version claude-x-20250101 starts with the pin
    assert run(p, i=intent(model="openai:gpt-y", model_version="anything")).decision == APPROVE   # empty list: any version
    assert run(p, i=intent(model="meta:other")).decision == BLOCK
    assert run(p, i=intent(model_version="claude-x-2024")).decision == BLOCK
    r = run(p, i=intent(model_version=None))
    assert status(r, "model_allowed").status == "unknown" and r.decision == HOLD                  # pinned, but the version is not stated
    r2 = run(p, i=intent(model=None))
    assert status(r2, "model_allowed").status == "unknown" and r2.decision == HOLD


def test_rate_limit_counts_only_other_intents_inside_the_window():
    seen = [(f"x{n}", ("A", "buy", 1.0, None, None), f"2023-01-21T13:{50 + n}:00Z") for n in range(3)]      # 13:50, 13:51, 13:52
    p = Policy(rate_limit=(3, 600))
    r = run(p, history=History(seen))
    assert r.decision == HOLD and status(r, "rate_limit").status == "fail"
    assert run(Policy(rate_limit=(4, 600)), history=History(seen)).decision == APPROVE
    assert run(Policy(rate_limit=(3, 60)), history=History(seen)).decision == APPROVE              # only 13:52 is within the last 60 s of 14:00? no: 8 minutes ago, so none count


def test_human_approval_threshold_is_at_or_above():
    assert run(Policy(approval_size=10)).decision == HOLD
    assert run(Policy(approval_size=10.01)).decision == APPROVE


@pytest.mark.parametrize("kw", [dict(intent_id=""), dict(intent_id="  "), dict(symbol=""), dict(side="hold"), dict(size=0), dict(size=-1), dict(size=math.nan), dict(size=math.inf),
                                dict(size=True), dict(price=0), dict(price=-3)])
def test_a_malformed_intent_is_an_error_not_a_quiet_block(kw):
    with pytest.raises(GateError):
        run(i=intent(**kw))


# ---------------------------------------------------------------- in a session: the chain
def ledger_events(s, type_=None):
    return s.ledger.events(session_id=s.id, type=type_)


def test_gate_writes_the_intent_and_its_decision_as_one_call_linked_to_the_data_and_the_model(make_session):
    s = make_session()
    s.add_source(ListSource("d", [rec("a", "2023-01-01T00:00:00Z"), rec("later", "2023-06-01T00:00:00Z")]))
    s.register_provider("fake", lambda req: ModelResponse("buy", "fake-1", "stop", {}))
    s.query("d")
    q = s.receipt().call_id
    s.model("fake:m", messages=[{"role": "user", "content": "go?"}])
    m = s.receipt().call_id
    r = s.gate(intent(model="fake:m", model_version="fake-1", data_as_of="2023-01-21T13:59:00Z"), Policy(max_staleness_seconds=600, max_order_size=50))
    assert r.decision == APPROVE and r.call_id
    ev_i, ev_g = ledger_events(s, "intent")[0], ledger_events(s, "gate_decision")[0]
    assert ev_i.call_id == ev_g.call_id == r.call_id and ev_g.seq == ev_i.seq + 1
    links = ev_i.data()["links"]
    assert links["query_call_ids"] == [q] and links["model_call_ids"] == [m] and links["basis"] == "since_previous_decision"
    g = ev_g.data()
    assert g["decision"] == "APPROVE" and g["intent_id"] == "i-1" and len(g["policy_sha256"]) == 64
    assert {x["check"] for x in g["reasons"]} >= {"kill_switch", "data_freshness", "max_order_size"} and all(set(x) == {"check", "status", "outcome"} for x in g["reasons"])
    assert s.verify_ledger().ok


def test_data_seen_to_fill_can_be_walked_through_the_ledger_by_ids_alone(make_session):
    s = make_session()
    s.add_source(ListSource("d", [rec("a", "2023-01-01T00:00:00Z")]))
    s.register_provider("fake", lambda req: ModelResponse("buy", "fake-1", "stop", {}))
    s.query("d")
    s.model("fake:m", messages=[{"role": "user", "content": "go?"}])
    g = s.gate(intent(model="fake:m", model_version="fake-1"), Policy())
    s.order("o1", symbol="acme", side="buy", size=10, intended_price=100.0, expected_price=100.0, intent=g.call_id)
    s.fill("o1", price=100.2, size=10, fees=0.1)
    by_call = {}
    for e in s.ledger.events(session_id=s.id):
        by_call.setdefault(e.call_id, []).append(e)
    order = ledger_events(s, "order")[0].data()
    gate_events = by_call[order["intent_call_id"]]                                                # order -> intent + gate decision (one call)
    assert [e.type for e in gate_events] == ["intent", "gate_decision"] and gate_events[1].data()["decision"] == "APPROVE"
    model_call_ids = gate_events[0].data()["links"]["model_call_ids"]                              # intent -> the model call that produced it
    model_ev = next(e for e in by_call[model_call_ids[0]] if e.type == "model_call").data()
    assert model_ev["model_id"] == "fake:m" and model_ev["version_reported"] == "fake-1" and gate_events[0].data()["model_version"] == "fake-1"
    served_ids = gate_events[0].data()["links"]["served_event_ids"]                                # intent -> the data it saw
    assert served_ids and all(any(e.id == i and e.type == "record_served" for e in s.ledger.events(session_id=s.id)) for i in served_ids)
    fill = ledger_events(s, "fill")[0].data()
    assert fill["order_id"] == "o1" and order["order_id"] == "o1"                                  # order -> fill by id


def test_an_order_can_only_be_linked_to_an_approved_intent(make_session):
    s = make_session()
    approved = s.gate(intent(intent_id="ok"), Policy())
    held = s.gate(intent(intent_id="held"), Policy(kill_switch=True))
    blocked = s.gate(intent(intent_id="blk"), Policy(symbol_allowlist=["ZZZ"]))
    assert (approved.decision, held.decision, blocked.decision) == (APPROVE, HOLD, BLOCK)
    s.order("o-ok", symbol="acme", side="buy", size=10, intent=approved.call_id)
    for bad, word in ((held, "HOLD"), (blocked, "BLOCK")):
        with pytest.raises(AsOfError, match=word):
            s.order(f"o-{word}", symbol="acme", side="buy", size=10, intent=bad.call_id)
    with pytest.raises(AsOfError, match="was not put through the gate"):
        s.order("o-x", symbol="acme", side="buy", size=10, intent="not-a-call-id")
    assert [e.data()["order_id"] for e in ledger_events(s, "order")] == ["o-ok"]                    # a refused link records nothing
    assert ledger_events(s, "order")[0].data()["intent_call_id"] == approved.call_id
    s.order("o-free", symbol="acme", side="buy", size=1)                                           # an order with no intent still works (the gate is opt-in)
    assert ledger_events(s, "order")[-1].data()["intent_call_id"] is None


def test_the_second_use_of_an_intent_id_is_a_duplicate_and_the_first_use_is_the_one_kept(make_session):
    s = make_session()
    a = s.gate(intent(intent_id="dup"), Policy())
    b = s.gate(intent(intent_id="dup"), Policy())
    c = s.gate(intent(intent_id="dup", size=77.0), Policy())
    assert (a.decision, b.decision, c.decision) == (APPROVE, BLOCK, BLOCK) and "DIFFERENT" in status(c, "duplicate_intent").detail
    assert len(ledger_events(s, "intent")) == 3 and len(ledger_events(s, "gate_decision")) == 3            # every attempt is recorded
    s.order("o1", symbol="acme", side="buy", size=10, intent=a.call_id)                                    # the original is still usable
    with pytest.raises(AsOfError):
        s.order("o2", symbol="acme", side="buy", size=10, intent=b.call_id)


def test_the_gate_is_judged_on_the_session_clock_not_the_wall_clock(make_session):
    s = make_session("2023-01-21T14:00:00Z")
    p = Policy(max_staleness_seconds=3600)
    i = intent(intent_id="a", data_as_of="2023-01-21T13:30:00Z")
    assert s.gate(i, p).decision == APPROVE
    s.advance_to("2023-01-21T16:00:00Z")
    r = s.gate(intent(intent_id="b", data_as_of="2023-01-21T13:30:00Z"), p)                          # now 2.5 hours old
    assert r.decision == HOLD and status(r, "data_freshness").status == "fail"


def test_the_rate_limit_uses_the_sessions_own_intents(make_session):
    s = make_session("2023-01-21T14:00:00Z")
    p = Policy(rate_limit=(2, 3600))
    assert [s.gate(intent(intent_id=f"r{n}"), p).decision for n in range(3)] == [APPROVE, APPROVE, HOLD]
    s.advance_to("2023-01-21T16:00:00Z")                                                           # the first two fall out of the window
    assert s.gate(intent(intent_id="r9"), p).decision == APPROVE


def test_positions_are_supplied_by_the_caller_and_checked(make_session):
    s = make_session()
    assert s.gate(intent(intent_id="p1", size=10), Policy(max_position=100), positions={"ACME": 95}).decision == BLOCK
    assert s.gate(intent(intent_id="p2", size=10), Policy(max_position=100), positions={"ACME": 50}).decision == APPROVE


def test_in_hashes_only_mode_values_and_details_are_hashed_and_names_counts_and_statuses_remain(make_session):
    s = make_session(privacy="hashes_only")
    g = s.gate(intent(symbol="SECRETCO", size=12345.0), Policy(max_order_size=100))
    assert g.decision == BLOCK
    text = json.dumps([e.payload for e in s.ledger.events(session_id=s.id)])
    assert "SECRETCO" not in text and "12345" not in text                                         # symbol, size, price and the readable details are not in the ledger
    i, d = ledger_events(s, "intent")[0].data(), ledger_events(s, "gate_decision")[0].data()
    assert "terms" not in i and len(i["terms_sha256"]) == 64 and "details" not in d and len(d["details_sha256"]) == 64
    assert d["decision"] == "BLOCK" and any(x["check"] == "max_order_size" and x["status"] == "fail" and x["outcome"] == "BLOCK" for x in d["reasons"])


def test_a_malformed_intent_through_the_session_records_nothing(make_session):
    s = make_session()
    before = len(s.ledger)
    with pytest.raises(GateError):
        s.gate(intent(size=0), Policy())
    assert len(s.ledger) == before
