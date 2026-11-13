"""R4: tool results."""
import pytest

from asof import ToolError


def news(query):
    return [{"title": "old", "published_at": "2023-01-20T10:00:00Z", "q": query}, {"title": "boundary", "published_at": "2023-01-21T14:00:00Z"},
            {"title": "future", "published_at": "2023-01-22T10:00:00Z"}, {"title": "undated"}]


def test_r4_future_items_are_dropped_and_undated_ones_withheld_and_both_are_logged(make_session):
    s = make_session("2023-01-21T14:00:00Z")
    s.add_tool("news_search", news, timestamp_field="published_at")
    got = s.tool("news_search", query="ACME-SYN guidance")
    assert [g["title"] for g in got] == ["old", "boundary"]                                   # inclusive boundary, future dropped, undated withheld
    r = s.receipt()
    assert r.kind == "tool" and r.served == 2 and r.blocked == {"future": 1, "untimestamped": 1}
    blocks = {e.data()["reason"]: e.data() for e in s.events("block")}
    assert blocks["future"]["count"] == 1 and blocks["untimestamped"]["count"] == 1
    call = s.events("tool_call")[0].data()
    assert call["args"] == {"query": "ACME-SYN guidance"} and call["blocked_future"] == 1


def test_r4_undated_items_are_served_as_unknown_only_when_the_session_allows_it(make_session):
    s = make_session("2023-01-21T14:00:00Z", allow_untimestamped=True)
    s.add_tool("news_search", news, timestamp_field="published_at")
    got = s.tool("news_search", query="x")
    assert [g["title"] for g in got] == ["old", "boundary", "undated"]
    assert s.receipt().served_timing == {"declared": 2, "unknown": 1} and s.receipt().blocked["future"] == 1


def test_r4_advancing_the_clock_releases_items_that_have_become_knowable(make_session):
    s = make_session("2023-01-21T14:00:00Z")
    s.add_tool("news_search", news, timestamp_field="published_at")
    assert len(s.tool("news_search", query="x")) == 2
    s.advance_to("2023-01-22T10:00:00Z")
    assert [g["title"] for g in s.tool("news_search", query="x")] == ["old", "boundary", "future"]


def test_pass_as_of_lets_a_tool_filter_at_the_source_and_the_proxy_still_enforces(make_session):
    seen = {}

    def lying(query, as_of):
        seen["as_of"] = as_of
        return [{"t": "2099-01-01T00:00:00Z", "x": 1}]                                         # ignores the lock

    s = make_session("2023-01-21T14:00:00Z")
    s.add_tool("t", lying, timestamp_field="t", pass_as_of=True)
    assert s.tool("t", query="q") == [] and seen["as_of"] == s.as_of
    with pytest.raises(ToolError, match="as_of"):
        s.add_tool("bad", lambda q: [], pass_as_of=True)


def test_result_shapes(make_session):
    s = make_session("2023-01-21T14:00:00Z")
    s.add_tool("wrapped", lambda: {"results": [{"ts": "2023-01-01T00:00:00Z"}, {"ts": "2024-01-01T00:00:00Z"}]}, timestamp_field="ts", items="results")
    s.add_tool("single", lambda: {"ts": "2023-01-01T00:00:00Z", "v": 1}, timestamp_field="ts")
    s.add_tool("text", lambda: "the market rose")
    s.add_tool("badwrap", lambda: [1], items="results")
    assert len(s.tool("wrapped")) == 1 and s.tool("single") == [{"ts": "2023-01-01T00:00:00Z", "v": 1}]
    assert s.tool("text") == [] and s.receipt().blocked["untimestamped"] == 1                    # free text cannot be time-checked, so it is withheld
    with pytest.raises(ToolError, match="items="):
        s.tool("badwrap")


def test_naive_item_timestamps_are_untrusted_unless_a_zone_is_declared(make_session):
    s = make_session("2023-01-21T14:00:00Z")
    s.add_tool("naive", lambda: [{"ts": "2023-01-01T00:00:00"}], timestamp_field="ts")
    s.add_tool("ny", lambda: [{"ts": "2023-01-21T08:30:00"}, {"ts": "2023-01-21T09:30:00"}], timestamp_field="ts", assume_tz="America/New_York")   # 13:30Z and 14:30Z
    assert s.tool("naive") == [] and [i["ts"] for i in s.tool("ny")] == ["2023-01-21T08:30:00"]        # 14:30Z is after T=14:00Z


def test_unknown_tools_and_tool_exceptions_are_errors_and_logged(make_session):
    s = make_session()
    with pytest.raises(ToolError, match="unknown tool"):
        s.tool("nope")

    fake = "sk-" + "abcdefghijklmnop1234"                  # a fake key, written in two parts so the source holds no key-shaped string (secret scanners, push protection)

    def boom():
        raise RuntimeError(f"upstream 500 with key {fake}")

    s.add_tool("boom", boom)
    with pytest.raises(RuntimeError):
        s.tool("boom")
    errs = [e.data() for e in s.events("tool_error")]
    assert [e["tool"] for e in errs] == ["nope", "boom"] and fake not in str(errs) and "[REDACTED]" in errs[1]["error"]


def test_a_result_that_cannot_be_hashed_is_refused_not_silently_dropped(make_session):
    s = make_session("2023-01-21T14:00:00Z")
    s.add_tool("obj", lambda: [{"ts": "2023-01-01T00:00:00Z", "o": object()}], timestamp_field="ts")
    with pytest.raises(ToolError, match="hash"):
        s.tool("obj")


def test_r4_a_future_dated_tool_result_is_logged_with_the_label_blocked_future(make_session):
    s = make_session("2023-01-21T14:00:00Z")
    s.add_tool("n", lambda: [{"ts": "2023-01-01T00:00:00Z"}, {"ts": "2023-02-01T00:00:00Z"}, {"x": 1}], timestamp_field="ts")
    s.tool("n")
    labels = {e.data()["label"]: e.data()["count"] for e in s.events("block")}
    assert labels == {"blocked_future": 1, "blocked_untimestamped": 1}
    assert [e.data()["kind"] for e in s.events("block")] == ["tool", "tool"]
