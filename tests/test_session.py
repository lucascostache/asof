"""The Session end to end, including the scenario the whole package exists for."""
import json

import pytest

from asof import AsOfError, Ledger, ModelResponse, Session, SourceError, sources

from conftest import ListSource, T, rec


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_the_spec_quickstart_flow(tmp_path, ledger_path):
    prices = write(tmp_path, "prices.csv",
                   "symbol,date,close,available_at\nACME-SYN,2023-01-19,148.0,2023-01-19T21:00:00Z\nACME-SYN,2023-01-20,150.0,2023-01-20T21:00:00Z\n"
                   "ACME-SYN,2023-01-23,155.0,2023-01-23T21:00:00Z\nAMD,2023-01-20,70.0,2023-01-20T21:00:00Z\n")
    s = Session(as_of="2023-01-21T14:00:00Z", privacy="full", allow_untimestamped=False, ledger_path=ledger_path)
    s.add_source(sources.CSV(prices, event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))
    rows = s.query("prices", symbol="ACME-SYN", lookback="30d")
    assert [r.payload["close"] for r in rows] == [148.0, 150.0]                              # the 23rd is not knowable yet
    s.advance_to("2023-01-24T14:00:00Z")
    assert [r.payload["close"] for r in s.query("prices", symbol="ACME-SYN", lookback="30d")] == [148.0, 150.0, 155.0]
    s.add_tool("news_search", lambda query: [{"title": "guidance", "timestamp": "2023-01-20T12:00:00Z", "query": query}])
    assert s.tool("news_search", query="ACME-SYN guidance")[0]["title"] == "guidance"
    s.register_provider("fake", lambda req: ModelResponse("ok"))
    assert s.model("fake:model", messages=[{"role": "user", "content": "summarise"}]).text == "ok"
    receipt = s.receipt(last=True)
    assert receipt.kind == "model" and receipt.as_of == "2023-01-24T14:00:00Z"
    assert s.verify_ledger().ok
    s.close()
    assert s.verify_ledger().ok                                                              # still verifiable after close


def test_no_future_information_reaches_the_agent_through_any_path(tmp_path, ledger_path):
    secret = "FUTURE-SECRET-EARNINGS-MISS"
    p = write(tmp_path, "d.csv", f"id,t,a,note\nk,2023-01-01T00:00:00Z,2023-01-01T00:00:00Z,fine\nk,2023-01-01T00:00:00Z,2023-02-01T00:00:00Z,{secret}\n"
                                  f"f,2023-02-01T00:00:00Z,2023-02-01T00:00:00Z,{secret}\n")
    s = Session("2023-01-21T14:00:00Z", ledger_path=ledger_path, allow_untimestamped=True)
    s.add_source(sources.CSV(p, event_time="t", available_at="a", record_id="id"))
    s.add_tool("search", lambda: [{"timestamp": "2023-01-05T00:00:00Z", "text": "fine"}, {"timestamp": "2023-02-05T00:00:00Z", "text": secret}])
    seen = [str(r.to_dict()) for r in s.query("d")] + [str(x) for x in s.tool("search")]
    assert secret not in " ".join(seen) and any("fine" in x for x in seen)
    for t in ("2023-01-31T23:59:59Z", "2023-02-01T00:00:00Z"):                                # right up to the boundary, and exactly on it
        s.advance_to(t)
        out = " ".join(str(r.to_dict()) for r in s.query("d"))
        assert (secret in out) == (t == "2023-02-01T00:00:00Z")
    s.close()


def test_receipts_api(make_session):
    s = make_session()
    with pytest.raises(AsOfError, match="no calls"):
        s.receipt()
    s.add_source(ListSource("d", [rec("a", "2023-01-01T00:00:00Z")]))
    s.query("d"); s.query("d")
    first, second = s.receipt(last=False)
    assert s.receipt() is second and s.receipt(call_id=first.call_id) is first and first.call_id != second.call_id
    with pytest.raises(AsOfError, match="no receipt"):
        s.receipt(call_id="nope")
    text = str(second)
    assert "served 1" in text and "ledger event" in text
    assert second.ledger_hash == next(e.hash for e in s.events() if e.seq == second.ledger_seq)
    assert "declared by the data's owner" in " ".join(second.limits)                          # the standing caveat travels with every receipt


def test_receipts_say_how_many_were_unknown_and_what_was_superseded(make_session):
    s = make_session(allow_untimestamped=True)
    s.add_source(ListSource("d", [rec("u", None), rec("v", "2023-01-01T00:00:00Z", order=0), rec("v", "2023-01-10T00:00:00Z", order=1), rec("f", "2030-01-01T00:00:00Z")]))
    s.query("d")
    r = s.receipt()
    assert r.served == 2 and r.served_timing == {"declared": 1, "unknown": 1} and r.blocked == {"future": 1, "untimestamped": 0, "superseded": 1, "outside_window": 0}


def test_sessions_share_one_ledger_without_mixing_their_events(ledger_path):
    a, b = Session("2023-01-01T00:00:00Z", ledger_path=ledger_path), Session("2024-01-01T00:00:00Z", ledger_path=ledger_path)
    a.add_source(ListSource("d", [rec("x", "2023-06-01T00:00:00Z")])); b.add_source(ListSource("d", [rec("x", "2023-06-01T00:00:00Z")]))
    assert a.query("d") == [] and len(b.query("d")) == 1
    assert {e.session_id for e in a.events()} == {a.id} and a.verify_ledger().ok and a.verify_ledger().n_events == b.verify_ledger().n_events
    a.close(); b.close()


def test_the_default_ledger_lives_under_the_users_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    s = Session("2023-01-01T00:00:00Z")
    s.close()
    assert (tmp_path / ".asof" / "ledger.db").exists()
    head = json.loads((tmp_path / ".asof" / "head.json").read_text())                       # A2: the anchor is ~/.asof/head.json by default
    assert head["seq"] == len(Ledger(tmp_path / ".asof" / "ledger.db")) and set(head) == {"version", "seq", "hash", "recorded_at"}


def test_source_errors_are_logged_and_raised(make_session, tmp_path):
    s = make_session()
    s.add_source(ListSource("d", []))
    with pytest.raises(SourceError, match="already added"):
        s.add_source(ListSource("d", []))
    with pytest.raises(SourceError, match="unknown source"):
        s.query("nope")
    with pytest.raises(SourceError, match="cannot open"):
        s.add_source(sources.CSV(tmp_path / "missing.csv", event_time="a", available_at="b"))
    assert s.events("source_error")
    with pytest.raises(SourceError, match="must yield Record"):
        s.add_source(type("Bad", (), {"name": "bad", "records": lambda self: iter([{"not": "a record"}])})())
    bad = rec("x", "2023-01-01T00:00:00Z")
    object.__setattr__(bad, "payload_sha256", "0" * 64)                                       # a record whose hash does not match its payload
    with pytest.raises(SourceError, match="payload_sha256"):
        s.add_source(ListSource("tampered", [bad]))


def test_lazy_sources_load_on_first_query_and_a_session_sees_one_snapshot(make_session, tmp_path):
    p = write(tmp_path, "d.csv", "id,t,a,v\n1,2023-01-01T00:00:00Z,2023-01-01T00:00:00Z,old\n")
    s = make_session()
    s.add_source(sources.CSV(p, event_time="t", available_at="a", record_id="id"), lazy=True)
    assert s.events("source_loaded") == []
    assert s.query("d")[0].payload["v"] == "old" and len(s.events("source_loaded")) == 1
    p.write_text("id,t,a,v\n1,2023-01-01T00:00:00Z,2023-01-01T00:00:00Z,CHANGED\n")           # the file changes mid-session
    assert s.query("d")[0].payload["v"] == "old" and len(s.events("source_loaded")) == 1


def test_the_source_fingerprint_changes_when_the_data_does_and_is_stable_otherwise(make_session):
    def fp(records):
        s = make_session()
        s.add_source(ListSource("d", records))
        return s.events("source_loaded")[0].data()["fingerprint_sha256"]
    a = [rec("a", "2023-01-01T00:00:00Z", {"v": 1})]
    assert fp(a) == fp([rec("a", "2023-01-01T00:00:00Z", {"v": 1})]) != fp([rec("a", "2023-01-01T00:00:00Z", {"v": 2})])


def test_a_payload_field_called_limit_or_lookback_is_reachable_through_filters(make_session):
    s = make_session()
    s.add_source(ListSource("d", [rec("a", "2023-01-01T00:00:00Z", {"limit": 5}), rec("b", "2023-01-01T00:00:00Z", {"limit": 6})]))
    assert [r.record_id for r in s.query("d", filters={"limit": 6})] == ["b"]


def test_a_context_manager_closes_the_session(ledger_path):
    with Session("2023-01-01T00:00:00Z", ledger_path=ledger_path) as s:
        pass
    assert s.events("session_end")[0].data() == {"n_calls": 0}
    with pytest.raises(AsOfError, match="closed"):
        s.query("d")
