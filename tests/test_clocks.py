"""A1: the three ledger clocks, for every event type.

event_time   = when the thing happened   (served record: the record's own; agent action: session clock T)
available_at = when it became knowable   (served record: the record's own; agent action: T; NEVER wall-clock)
recorded_at  = wall clock when the ledger wrote it; set ONLY by the ledger
"""
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from asof import Ledger, ModelResponse, Session, sources
from asof.ledger import EventSpec

from conftest import ListSource, T, rec

START = "2023-01-21T14:00:00Z"
LATER = "2023-02-01T09:30:00Z"


@pytest.fixture
def sess(make_session):
    s = make_session(START, max_as_of="2023-03-01T00:00:00Z")
    s.add_source(ListSource("d", [rec("old", "2023-01-10T08:00:00Z", {"v": 1}, event_time="2023-01-09T16:00:00Z"),
                                  rec("noevent", "2023-01-11T08:00:00Z", {"v": 2}),
                                  rec("future", "2023-02-15T00:00:00Z", {"v": 3}),
                                  rec("undated", None, {"v": 4})]))
    s.add_tool("news", lambda: [{"title": "a", "published_at": "2023-01-20T12:00:00Z"}, {"title": "future", "published_at": "2023-03-20T12:00:00Z"}, {"title": "undated"}],
               timestamp_field="published_at")
    s.register_provider("fake", lambda req: ModelResponse("ok"))
    return s


def by_type(s, type_):
    return [e for e in s.events() if e.type == type_]


def agent_action_events(s):
    """Everything that is an agent action (not a served record or tool item)."""
    return [e for e in s.events() if e.type not in ("record_served", "tool_item_served")]


def test_agent_actions_are_stamped_with_the_session_clock_for_both_event_time_and_available_at(sess):
    sess.allow_untimestamped = False
    sess.query("d"); sess.tool("news"); sess.model("fake:m", messages=[{"role": "user", "content": "q"}]); sess.decision("hold", reason="r")
    sess.close()
    kinds = {e.type for e in agent_action_events(sess)}
    assert {"session_start", "source_added", "source_loaded", "tool_added", "query", "block", "tool_call", "model_call", "decision", "session_end"} <= kinds
    for e in agent_action_events(sess):
        assert e.event_time == START and e.available_at == START, (e.type, e.event_time, e.available_at)


def test_clock_changes_are_stamped_with_the_clock_at_that_moment(sess):
    sess.advance_to(LATER)
    with pytest.raises(Exception):
        sess.advance_to("2023-01-01T00:00:00Z")                                                 # rewind
    with pytest.raises(Exception):
        sess.advance_to("2024-01-01T00:00:00Z")                                                 # past the ceiling
    adv, rew, ceil = by_type(sess, "clock_advance")[0], by_type(sess, "clock_rewind_rejected")[0], by_type(sess, "clock_ceiling_rejected")[0]
    assert (adv.event_time, adv.available_at) == (LATER, LATER)                                 # the NEW time: the moment after the change
    assert (rew.event_time, rew.available_at) == (LATER, LATER) and (ceil.event_time, ceil.available_at) == (LATER, LATER)
    assert rew.data()["attempted"] == "2023-01-01T00:00:00Z"


def test_actions_after_an_advance_carry_the_new_clock(sess):
    sess.query("d")
    sess.advance_to(LATER)
    sess.query("d")
    q = by_type(sess, "query")
    assert (q[0].event_time, q[0].available_at) == (START, START) and (q[1].event_time, q[1].available_at) == (LATER, LATER)


def test_a_served_record_carries_its_own_two_clocks_not_the_session_clock(sess):
    sess.query("d")
    served = {e.data()["record_id"]: e for e in by_type(sess, "record_served")}
    assert set(served) == {"old", "noevent"}
    old, noevent = served["old"], served["noevent"]
    assert (old.event_time, old.available_at) == ("2023-01-09T16:00:00Z", "2023-01-10T08:00:00Z")      # neither is T (14:00 on the 21st)
    assert noevent.event_time is None and noevent.available_at == "2023-01-11T08:00:00Z"                # unknown stays unknown: never invented
    assert all(e.event_time != START and e.available_at != START for e in served.values())


def test_a_record_served_with_unknown_timing_has_a_null_available_at(make_session):
    s = make_session(START, allow_untimestamped=True)
    s.add_source(ListSource("d", [rec("undated", None, {"v": 4})]))
    s.query("d")
    e = by_type(s, "record_served")[0]
    assert e.data()["timing"] == "unknown" and e.available_at is None and e.event_time is None


def test_a_served_tool_item_keeps_its_own_declared_time(make_session):
    s = make_session(START, allow_untimestamped=True)
    s.add_tool("news", lambda: [{"t": "2023-01-20T12:00:00Z"}, {"x": "undated"}], timestamp_field="t")
    s.tool("news")
    items = {e.data()["timing"]: e for e in by_type(s, "tool_item_served")}
    assert items["declared"].available_at == "2023-01-20T12:00:00Z" and items["declared"].event_time is None
    assert items["unknown"].available_at is None
    assert by_type(s, "tool_call")[0].available_at == START                                     # the call itself is an agent action


def test_blocks_are_agent_actions_stamped_T_even_though_they_concern_future_records(sess):
    sess.query("d"); sess.tool("news")
    blocks = by_type(sess, "block")
    assert {b.data()["kind"] for b in blocks} == {"query", "tool"} and blocks
    assert all(b.event_time == START and b.available_at == START for b in blocks)


def test_available_at_is_never_wall_clock_for_any_event_type(sess):
    sess.query("d"); sess.tool("news"); sess.model("fake:m", messages=[{"role": "user", "content": "q"}]); sess.decision("x")
    sess.advance_to(LATER)
    sess.close()
    wall_year = str(datetime.now(timezone.utc).year)
    for e in sess.events():
        assert e.available_at is None or e.available_at.startswith("2023"), (e.type, e.available_at)
        assert e.event_time is None or e.event_time.startswith("2023"), (e.type, e.event_time)
        assert e.recorded_at.startswith(wall_year)                                              # ONLY recorded_at is wall-clock


def test_recorded_at_is_set_only_by_the_ledger(ledger_path):
    fixed = datetime(2031, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
    led = Ledger(ledger_path, now=lambda: fixed)
    assert "recorded_at" not in inspect.signature(Ledger.append).parameters and "recorded_at" not in inspect.signature(Ledger.append_many).parameters
    assert "recorded_at" not in {f for f in EventSpec.__dataclass_fields__}
    with pytest.raises(TypeError):
        led.append("s", "x", event_time=None, available_at=None, payload={}, recorded_at="2000-01-01T00:00:00Z")      # type: ignore[call-arg]
    with pytest.raises(TypeError):
        EventSpec("s", "x", None, None, {}, recorded_at="2000-01-01T00:00:00Z")                  # type: ignore[call-arg]
    ev = led.append("s", "x", event_time=T("2023-01-01T00:00:00Z"), available_at=T("2023-01-01T00:00:00Z"), payload={"recorded_at": "2000-01-01T00:00:00Z"})
    assert ev.recorded_at == "2031-07-04T12:00:00Z" and ev.event_time == "2023-01-01T00:00:00Z"   # a payload field with that name changes nothing


def test_recorded_at_tracks_the_wall_clock_while_the_session_clock_stands_still(make_session):
    s = make_session(START)
    before = datetime.now(timezone.utc)
    s.add_source(ListSource("d", [rec("a", "2023-01-01T00:00:00Z")]))
    s.query("d")
    after = datetime.now(timezone.utc)
    for e in agent_action_events(s):
        wall = datetime.fromisoformat(e.recorded_at.replace("Z", "+00:00"))
        assert before - timedelta(seconds=1) <= wall <= after + timedelta(seconds=1) and e.event_time == START


def test_a_batch_shares_one_recorded_at_and_is_ordered(sess):
    sess.query("d")
    call = sess.receipt()
    evs = sess.events(call_id=call.call_id)
    assert len({e.recorded_at for e in evs}) == 1 and [e.seq for e in evs] == sorted(e.seq for e in evs) and evs[-1].type == "query"


def test_decisions_are_stamped_T_and_recorded_with_their_details(make_session):
    s = make_session(START)
    cid = s.decision("buy", symbol="ACME-SYN", size=10)
    [e] = s.events(call_id=cid)
    assert (e.type, e.event_time, e.available_at) == ("decision", START, START) and {k: v for k, v in e.data().items() if k != "links"} == {"action": "buy", "details": {"size": 10, "symbol": "ACME-SYN"}}
    assert e.data()["links"] == {"basis": "since_previous_decision", "query_call_ids": [], "tool_call_ids": [], "model_call_ids": [], "served_event_ids": [], "n_served": 0, "truncated": False}      # nothing was served before it
    assert s.receipt().kind == "decision" and s.receipt().call_id == cid


def test_every_event_type_in_a_full_run_is_covered_by_these_rules(sess):
    """If a new event type is added it must be classified here, so no event can slip in with undefined clock semantics."""
    sess.query("d"); sess.tool("news"); sess.model("fake:m", messages=[{"role": "user", "content": "q"}]); sess.decision("x"); sess.advance_to(LATER)
    with pytest.raises(Exception):
        sess.advance_to("2023-01-01T00:00:00Z")
    with pytest.raises(Exception):
        sess.advance_to("2030-01-01T00:00:00Z")
    sess.close()
    agent = {"session_start", "session_end", "source_added", "source_loaded", "tool_added", "query", "tool_call", "model_call", "decision", "block", "clock_advance",
             "clock_rewind_rejected", "clock_ceiling_rejected", "clock_gate_rejected", "source_error", "tool_error", "model_error"}
    served = {"record_served", "tool_item_served"}
    assert {e.type for e in sess.events()} <= agent | served
