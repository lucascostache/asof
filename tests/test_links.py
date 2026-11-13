"""Evidence links (SPEC 1.2, section 4.6): a decision names, in its own event, the served records and the calls it rests on. Nothing is inferred from the order of events."""
import json

import pytest

from asof import ModelResponse, Receipt, Record
from asof import session as session_module
from asof.errors import AsOfError
from conftest import ListSource, T


def recs(n, source="px", start=0):
    return [Record(source, f"{source}{start + i}", T(f"2023-01-{10 + i:02d}T00:00:00Z"), T(f"2023-01-{10 + i:02d}T21:00:00Z"), {"v": start + i}, order=i) for i in range(n)]


def rich(make_session, privacy="full"):
    s = make_session("2023-01-20T23:00:00Z", privacy=privacy)
    s.add_source(ListSource("px", recs(5)))
    s.add_tool("news", lambda: [{"t": "a", "published_at": "2023-01-12T00:00:00Z"}, {"t": "b", "published_at": "2023-01-13T00:00:00Z"}, {"t": "future", "published_at": "2030-01-01T00:00:00Z"}], timestamp_field="published_at")
    s.register_provider("fake", lambda req: ModelResponse("ok"))
    return s


def ids(s, call_id, type_):
    return [e.id for e in s.events(call_id=call_id) if e.type == type_]


def test_a_decision_links_the_exact_events_the_calls_before_it_produced(make_session):
    s = rich(make_session)
    s.query("px", limit=3)
    q = s.receipt().call_id
    s.tool("news")
    t = s.receipt().call_id
    s.model("fake:m", messages=[{"role": "user", "content": "q"}])
    m = s.receipt().call_id
    d = s.decision("buy", why="because")
    links = s.events(call_id=d)[0].data()["links"]
    assert links["basis"] == "since_previous_decision" and links["query_call_ids"] == [q] and links["tool_call_ids"] == [t] and links["model_call_ids"] == [m]
    assert links["served_event_ids"] == ids(s, q, "record_served") + ids(s, t, "tool_item_served") and links["n_served"] == 5 and links["truncated"] is False
    assert len(ids(s, q, "record_served")) == 3 and len(ids(s, t, "tool_item_served")) == 2          # the future tool item was blocked, so it is not linked


def test_a_blocked_record_is_never_linked(make_session):
    s = make_session("2023-01-12T00:00:00Z")
    s.add_source(ListSource("px", recs(5)))
    s.query("px")
    served = {e.id for e in s.events("record_served")}
    d = s.decision("hold")
    linked = set(s.events(call_id=d)[0].data()["links"]["served_event_ids"])
    assert linked == served and s.receipt(last=False)[0].blocked["future"] > 0


def test_the_next_decision_links_only_what_came_after_the_previous_one(make_session):
    s = rich(make_session)
    s.query("px", limit=1)
    first = s.receipt().call_id
    d1 = s.decision("one")
    s.query("px", limit=2)
    second = s.receipt().call_id
    d2 = s.decision("two")
    l1, l2 = (s.events(call_id=d)[0].data()["links"] for d in (d1, d2))
    assert l1["query_call_ids"] == [first] and l2["query_call_ids"] == [second] and set(l1["served_event_ids"]).isdisjoint(l2["served_event_ids"])


def test_a_decision_with_nothing_before_it_has_empty_links_not_missing_ones(make_session):
    s = rich(make_session)
    d = s.decision("hold")
    assert s.events(call_id=d)[0].data()["links"] == {"basis": "since_previous_decision", "query_call_ids": [], "tool_call_ids": [], "model_call_ids": [], "served_event_ids": [], "n_served": 0, "truncated": False}


def test_based_on_names_the_calls_a_decision_rests_on_and_may_reach_back(make_session):
    s = rich(make_session)
    s.query("px", limit=1)
    old = s.receipt()
    s.decision("first")
    s.query("px", limit=2)
    new = s.receipt()
    s.model("fake:m", messages=[{"role": "user", "content": "q"}])
    d = s.decision("second", based_on=[old, new.call_id])                         # a receipt or a call id, from this window or an earlier one
    links = s.events(call_id=d)[0].data()["links"]
    assert links["basis"] == "explicit" and links["query_call_ids"] == [old.call_id, new.call_id] and links["model_call_ids"] == []
    assert links["served_event_ids"] == ids(s, old.call_id, "record_served") + ids(s, new.call_id, "record_served")
    assert "based_on" not in s.events(call_id=d)[0].data()["details"]                # reserved: not stored as a free-form detail
    d3 = s.decision("third")                                                          # the explicit decision still closed the window: the model call is not carried over
    assert s.events(call_id=d3)[0].data()["links"]["model_call_ids"] == []


def test_based_on_refuses_a_call_this_session_never_produced(make_session):
    s = rich(make_session)
    s.query("px")
    with pytest.raises(AsOfError, match="never produced"):
        s.decision("x", based_on=["0123456789ab"])
    assert [e.type for e in s.events() if e.type == "decision"] == []                 # nothing half-written


def test_every_linked_id_exists_is_of_the_right_type_and_is_earlier_than_the_decision(make_session):
    s = rich(make_session)
    for k in range(3):
        s.query("px", limit=k + 1)
        s.tool("news")
        s.model("fake:m", messages=[{"role": "user", "content": str(k)}])
        s.decision(f"d{k}", based_on=None if k != 1 else [s.receipt(last=False)[-3].call_id])
    events = s.events()
    by_id = {e.id: e for e in events}
    calls = {}
    for e in events:
        calls.setdefault(e.call_id, []).append(e)
    for d in (e for e in events if e.type == "decision"):
        links = d.data()["links"]
        for i in links["served_event_ids"]:
            assert i in by_id and by_id[i].type in ("record_served", "tool_item_served") and by_id[i].seq < d.seq
        for key, closing in (("query_call_ids", "query"), ("tool_call_ids", "tool_call"), ("model_call_ids", "model_call")):
            for c in links[key]:
                assert c in calls and any(x.type == closing for x in calls[c]) and max(x.seq for x in calls[c]) < d.seq
        ids_of_calls = set(links["query_call_ids"] + links["tool_call_ids"])
        assert {by_id[i].call_id for i in links["served_event_ids"]} <= ids_of_calls          # a served event is linked only through a call that is linked
    assert s.verify_ledger(anchor="skip").ok


@pytest.mark.parametrize("privacy", ["full", "hashes_only"])
def test_links_are_metadata_and_stored_the_same_way_in_both_privacy_modes(make_session, privacy):
    s = rich(make_session, privacy)
    s.query("px")
    d = s.decision("buy", secret_reason="CONFIDENTIAL")
    data = s.events(call_id=d)[0].data()
    assert data["links"]["n_served"] == 5 and len(data["links"]["served_event_ids"]) == 5 and all(len(i) == 32 for i in data["links"]["served_event_ids"])
    assert ("details" in data) == (privacy == "full") and "CONFIDENTIAL" not in json.dumps({k: v for k, v in data.items() if k != "details"})


def test_a_very_large_link_list_is_cut_and_says_so(make_session, monkeypatch):
    monkeypatch.setattr(session_module, "MAX_LINKED", 3)
    s = rich(make_session)
    s.query("px")
    d = s.decision("buy")
    links = s.events(call_id=d)[0].data()["links"]
    assert len(links["served_event_ids"]) == 3 and links["n_served"] == 5 and links["truncated"] is True


def test_the_receipt_of_a_decision_is_unchanged_and_the_agent_view_has_no_links(make_session):
    s = rich(make_session)
    s.query("px")
    s.decision("buy")
    r = s.receipt()
    assert isinstance(r, Receipt) and r.kind == "decision" and "links" not in r.agent_view()
