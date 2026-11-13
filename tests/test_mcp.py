"""B1: the MCP server. The clock belongs to the server; R1-R8 are enforced by the same core as the Python API."""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import Client                                          # noqa: E402
from mcp.client.stdio import StdioServerParameters             # noqa: E402

from asof import Ledger                                         # noqa: E402
from asof.mcp_server import ServerPolicy, build_server, load_config, session_from_config   # noqa: E402

from mcp_fixture_tools import SECRET                            # noqa: E402

TESTS = Path(__file__).resolve().parent
START = "2023-01-21T14:00:00Z"
CEILING = "2023-02-01T00:00:00Z"
KEY = "sk-" + "ant-MCPCANARY0123456789abcdef"          # a fake key, written in two parts so the source holds no key-shaped string (secret scanners, push protection)

PRICES = (f"symbol,date,close,available_at,note\nACME-SYN,2023-01-19,148.0,2023-01-19T21:00:00Z,a\nACME-SYN,2023-01-20,150.0,2023-01-20T21:00:00Z,b\n"
          f"ACME-SYN,2023-01-23,155.0,2023-01-23T21:00:00Z,{SECRET}\nACME-SYN,2023-01-30,160.0,2023-01-30T21:00:00Z,c\n")


def make_config(tmp_path, **over):
    (tmp_path / "prices.csv").write_text(PRICES)
    cfg = {"as_of": START, "max_as_of": CEILING, "ledger_path": "ledger.db",
           "sources": [{"type": "csv", "path": "prices.csv", "event_time": "date", "available_at": "available_at", "key": ["symbol", "date"], "assume_tz": "UTC"}],
           "tools": [{"name": "news_search", "callable": "mcp_fixture_tools:news", "timestamp_field": "published_at"},
                     {"name": "clocked", "callable": "mcp_fixture_tools:clocked", "timestamp_field": "published_at", "pass_as_of": True}],
           "providers": [{"name": "fake", "callable": "mcp_fixture_tools:llm"}]}
    cfg.update(over)
    p = tmp_path / "asof.config.json"
    p.write_text(json.dumps(cfg))
    return p


class Rig:
    """A server over a real session; `call` goes through the MCP client, so the protocol layer is exercised."""
    def __init__(self, tmp_path, policy_over=None, **cfg_over):
        cfg_path = make_config(tmp_path, **cfg_over)
        cfg, base = load_config(cfg_path)
        self.session, policy = session_from_config(cfg, base)
        if policy_over:
            policy = ServerPolicy(**{**policy.__dict__, **policy_over})
        self.server = build_server(self.session, policy)
        self.ledger_path = tmp_path / "ledger.db"

    def call(self, name, args=None):
        async def go():
            async with Client(self.server) as c:
                r = await c.call_tool(name, args or {})
                return r
        r = asyncio.run(go())
        text = r.content[0].text if r.content else ""
        data = r.structured_content if r.structured_content is not None else (json.loads(text) if not r.is_error and text.startswith("{") else None)
        return r.is_error, data, text

    def tools(self):
        async def go():
            async with Client(self.server) as c:
                return sorted(t.name for t in (await c.list_tools()).tools)
        return asyncio.run(go())

    def events(self, **kw):
        return self.session.events(**kw)


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.session.close()


@pytest.fixture
def clockrig(tmp_path):
    r = Rig(tmp_path, allow_model_clock=True)
    yield r
    r.session.close()


def test_the_tool_list_matches_the_brief_and_model_call_is_off_by_default(rig, tmp_path):
    assert rig.tools() == ["advance_clock", "get_receipt", "ledger_head", "query", "set_clock", "tool_call", "verify_ledger"]
    (tmp_path / "x").mkdir()
    other = Rig(tmp_path / "x", policy_over={"enable_model_call": True})
    assert "model_call" in other.tools()
    other.session.close()


def test_the_model_cannot_read_data_past_T_through_any_tool(rig):
    err, q, text_q = rig.call("query", {"source": "prices"})
    assert not err and [r["payload"]["close"] for r in q["records"]] == [148.0, 150.0] and q["as_of"] == START
    err, t, text_t = rig.call("tool_call", {"name": "news_search", "arguments": {"query": "ACME-SYN"}})
    assert [i["title"] for i in t["items"]] == ["ACME-SYN raises guidance"]
    everything = [text_q, text_t]
    for name, args in (("get_receipt", {"receipt_id": q["receipt_id"]}), ("get_receipt", {"receipt_id": t["receipt_id"]}), ("verify_ledger", {}), ("ledger_head", {})):
        everything.append(rig.call(name, args)[2])
    assert SECRET not in " ".join(everything) and "2023-02-22" not in " ".join(everything) and "160.0" not in " ".join(everything)


def test_the_agent_view_of_a_receipt_has_no_blocked_counts_the_full_view_does(tmp_path):
    for detail, expect_blocked in (("agent", False), ("full", True)):
        d = tmp_path / detail
        d.mkdir()
        r = Rig(d, policy_over={"receipt_detail": detail})
        _, q, _ = r.call("query", {"source": "prices"})
        _, rc, text = r.call("get_receipt", {"receipt_id": q["receipt_id"]})
        assert ("blocked" in rc) is expect_blocked and rc["served"] == 2 and rc["call_id"] == q["receipt_id"]
        if expect_blocked:
            assert rc["blocked"]["future"] == 2
        else:
            assert "future" not in text
        r.session.close()


def test_the_model_cannot_override_the_clock_with_arguments(rig):
    import mcp_fixture_tools as mt
    mt.CALLS.clear()
    _, q, _ = rig.call("query", {"source": "prices", "as_of": "2030-01-01T00:00:00Z", "filters": {"note": SECRET}})
    assert q["as_of"] == START and q["records"] == []                                            # the future row is not reachable even by filtering for its text
    _, t, _ = rig.call("tool_call", {"name": "clocked", "arguments": {"query": "x", "as_of": "2099-01-01T00:00:00Z"}})
    assert mt.CALLS[-1]["as_of"].startswith("2023-01-21T14:00:00") and t["items"][0]["as_of_seen"].startswith("2023-01-21T14:00:00")   # the proxy overwrote it
    assert rig.session.as_of.isoformat().startswith("2023-01-21T14:00:00")


def test_set_and_advance_are_refused_unless_the_operator_allowed_them_and_the_refusal_is_logged(rig):
    for name, args in (("set_clock", {"as_of": "2023-01-25T00:00:00Z"}), ("advance_clock", {"by": "1d"}), ("advance_clock", {"to": "2023-01-25T00:00:00Z"})):
        err, _, text = rig.call(name, args)
        assert err and "owned by the operator" in text
    assert rig.session.as_of.isoformat().startswith("2023-01-21T14:00:00")
    assert len(rig.events(type="clock_refused")) == 3 and not rig.events(type="clock_advance")


def test_when_allowed_the_clock_moves_forward_only_and_every_change_is_logged(clockrig):
    err, r, _ = clockrig.call("set_clock", {"as_of": "2023-01-24T00:00:00Z"})
    assert not err and r["as_of"] == "2023-01-24T00:00:00Z" and r["changed"] is True and r["ceiling"] == CEILING
    _, q, _ = clockrig.call("query", {"source": "prices"})
    assert [x["payload"]["close"] for x in q["records"]] == [148.0, 150.0, 155.0]                # the 23rd is knowable now
    err, r, _ = clockrig.call("advance_clock", {"by": "2d"})
    assert not err and r["as_of"] == "2023-01-26T00:00:00Z"
    err, _, text = clockrig.call("set_clock", {"as_of": "2023-01-22T00:00:00Z"})                  # rewind
    assert err and "backwards" in text
    err, _, text = clockrig.call("advance_clock", {"to": "2024-01-01T00:00:00Z"})                # past the ceiling
    assert err and "ceiling" in text
    err, r, _ = clockrig.call("set_clock", {"as_of": "2023-01-26T00:00:00Z"})                      # same instant: a no-op
    assert not err and r["changed"] is False
    assert clockrig.session.as_of.isoformat().startswith("2023-01-26T00:00:00")
    assert [e.data()["to"] for e in clockrig.events(type="clock_advance")] == ["2023-01-24T00:00:00Z", "2023-01-26T00:00:00Z"]
    assert len(clockrig.events(type="clock_rewind_rejected")) == 1 and len(clockrig.events(type="clock_ceiling_rejected")) == 1


def test_clock_argument_validation(clockrig):
    for args in ({}, {"to": "2023-01-25T00:00:00Z", "by": "1d"}):
        assert clockrig.call("advance_clock", args)[0]
    assert clockrig.call("set_clock", {"as_of": "2023-01-25T00:00:00"})[0]                       # naive: refused
    assert clockrig.call("advance_clock", {"by": "1mo"})[0]                                       # months have no fixed length
    assert clockrig.session.as_of.isoformat().startswith("2023-01-21T14:00:00")


def test_every_call_produces_ledger_events_and_receipt_ids_are_ledger_ids(rig):
    n0 = len(rig.events())
    _, q, _ = rig.call("query", {"source": "prices"})
    _, t, _ = rig.call("tool_call", {"name": "news_search", "arguments": {"query": "x"}})
    assert len(rig.events()) > n0 + 2
    for rid, closing in ((q["receipt_id"], "query"), (t["receipt_id"], "tool_call")):
        evs = rig.events(call_id=rid)
        assert evs and evs[-1].type == closing and all(e.call_id == rid for e in evs)
        _, rc, _ = rig.call("get_receipt", {"receipt_id": rid})
        assert rc["ledger_hash"] == evs[-1].hash and "ledger_seq" not in rc                  # the receipt names the very event in the ledger, by its hash: the agent is not told its position
    assert rig.session.verify_ledger().ok


def test_get_receipt_rejects_unknown_ids_and_other_sessions(rig, tmp_path):
    assert rig.call("get_receipt", {"receipt_id": "nope"})[0]
    from asof import Session
    other = Session("2023-01-21T14:00:00Z", ledger=rig.session.ledger)                           # a different session sharing the same ledger file
    cid = other.decision("hold")
    assert rig.call("get_receipt", {"receipt_id": cid})[0]
    other.close()


def test_errors_are_returned_as_tool_errors_and_never_crash_the_server(rig):
    for name, args in (("query", {"source": "nope"}), ("query", {"source": "prices", "lookback": "1mo"}), ("tool_call", {"name": "nope"}),
                       ("query", {"source": "prices", "limit": -1})):
        err, _, text = rig.call(name, args)
        assert err, (name, args)
    assert not rig.call("query", {"source": "prices"})[0]                                         # still serving


def test_query_row_cap(tmp_path):
    r = Rig(tmp_path, policy_over={"max_rows": 1})
    _, q, _ = r.call("query", {"source": "prices"})
    assert q["served"] == 1 and q["records"][0]["payload"]["close"] == 150.0                     # the most recent row
    r.session.close()


def test_verify_and_head_tools_and_tampering_is_reported(rig, tmp_path):
    """The agent is told whether the ledger is intact and the hash of its newest event, but not how many events there are or which number an event has: the ledger grows by one more
    event when a call withheld something, so a count or a position would tell the agent that later data exists. The operator's view (receipt_detail "full") has both."""
    rig.call("query", {"source": "prices"})
    _, v, _ = rig.call("verify_ledger")
    assert v == {"ok": True, "anchor": "ok", "head_hash": rig.session.ledger.head().hash, "reason": None}
    _, h, _ = rig.call("ledger_head")
    assert set(h) == {"hash", "recorded_at"} and h["hash"] == v["head_hash"] and h["recorded_at"]
    (tmp_path / "op").mkdir()
    op = Rig(tmp_path / "op", policy_over={"receipt_detail": "full"})
    op.call("query", {"source": "prices"})
    _, fv, _ = op.call("verify_ledger")
    _, fh, _ = op.call("ledger_head")
    assert fv["ok"] and fv["anchor"] == "ok" and fv["n_events"] == len(op.session.ledger.events()) and fh["seq"] == fv["n_events"] and fh["hash"] == fv["head_hash"]
    for r in (rig, op):
        c = sqlite3.connect(r.ledger_path)
        c.execute("DROP TRIGGER events_no_update")
        c.execute("UPDATE events SET type='forged' WHERE seq=3")
        c.commit()
        c.close()
    _, v2, _ = rig.call("verify_ledger")
    assert not v2["ok"] and "does not verify" in v2["reason"] and "strathora verify" in v2["reason"] and not any(ch.isdigit() for ch in v2["reason"])      # told that it fails, not where
    _, fv2, _ = op.call("verify_ledger")
    assert not fv2["ok"] and "altered" in fv2["reason"]
    op.session.close()


def test_model_call_when_enabled_forbids_credentials_and_endpoints_and_never_logs_keys(tmp_path):
    r = Rig(tmp_path, policy_over={"enable_model_call": True})
    err, m, _ = r.call("model_call", {"model": "fake:m", "messages": [{"role": "user", "content": "hi"}], "params": {"temperature": 0}})
    assert not err and m["text"] == "echo:hi" and m["as_of"] == START
    for bad in ("api_key", "base_url", "timeout"):
        err, _, text = r.call("model_call", {"model": "fake:m", "messages": [{"role": "user", "content": "hi"}], "params": {bad: KEY}})
        assert err and "operator" in text
    assert r.events(type="model_call") and len(r.events(type="model_call")) == 1
    assert KEY.encode() not in r.ledger_path.read_bytes()
    _, rc, _ = r.call("get_receipt", {"receipt_id": m["receipt_id"]})
    assert rc["model"]["label"] == "unknown" and rc["model"]["declared_by"] == "unknown"
    r.session.close()


# ---------------------------------------------------------------- config
def test_config_rejects_unknown_keys_and_missing_as_of(tmp_path):
    led = {"ledger_path": "l.db"}                                      # never the default ~/.asof/ledger.db: a test must not write to the user's home
    for cfg, msg in (({"as_of": START, "oops": 1, **led}, "unknown key"), ({"sources": [], **led}, "as_of"), ({"as_of": START, **led, "sources": [{"type": "csv", "path": "p", "extra": 1}]}, "unknown key"),
                     ({"as_of": START, **led, "sources": [{"type": "ftp"}]}, "unknown source type"), ({"as_of": START, **led, "tools": [{"name": "t", "callable": "nodots"}]}, "package.module:function")):
        with pytest.raises(ValueError, match=msg):
            session_from_config(cfg, tmp_path)
    with pytest.raises(ValueError, match="receipt_detail"):
        ServerPolicy(receipt_detail="everything")


# ---------------------------------------------------------------- the real thing: a subprocess speaking MCP over stdio
def run_stdio(cfg_path, steps, extra_args=()):
    env = {**os.environ, "PYTHONPATH": str(TESTS) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "asof.mcp_server", "--config", str(cfg_path), *extra_args], env=env)

    async def go():
        async with Client(params) as c:
            return await steps(c)
    return asyncio.run(go())


def test_end_to_end_over_stdio_with_a_real_subprocess(tmp_path):
    cfg = make_config(tmp_path)

    async def steps(c):
        names = sorted(t.name for t in (await c.list_tools()).tools)
        q = (await c.call_tool("query", {"source": "prices", "lookback": "30d"})).structured_content
        denied = await c.call_tool("set_clock", {"as_of": "2023-01-30T00:00:00Z"})
        rc = (await c.call_tool("get_receipt", {"receipt_id": q["receipt_id"]})).structured_content
        v = (await c.call_tool("verify_ledger", {})).structured_content
        return names, q, denied.is_error, rc, v

    names, q, denied, rc, v = run_stdio(cfg, steps)
    assert names == ["advance_clock", "get_receipt", "ledger_head", "query", "set_clock", "tool_call", "verify_ledger"]
    assert [r["payload"]["close"] for r in q["records"]] == [148.0, 150.0] and denied and rc["call_id"] == q["receipt_id"] and v["ok"]
    led = Ledger(tmp_path / "ledger.db")                                                         # after the server has exited the file is intact and verifiable
    assert led.verify().ok and {e.type for e in led.events()} >= {"session_start", "record_served", "query", "clock_refused", "session_end"}


def test_stdio_override_flags_and_bad_config_exit_codes(tmp_path):
    cfg = make_config(tmp_path)

    async def steps(c):
        return (await c.call_tool("advance_clock", {"by": "1d"})).structured_content, (await c.call_tool("advance_clock", {"to": "2023-03-01T00:00:00Z"})).is_error
    moved, past = run_stdio(cfg, steps, ["--allow-model-clock"])
    assert moved["as_of"] == "2023-01-22T14:00:00Z" and past                                      # allowed to move, still never past the ceiling
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"as_of": START, "nonsense": True}))
    r = subprocess.run([sys.executable, "-m", "asof.mcp_server", "--config", str(bad)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "unknown key" in r.stderr and r.stdout == ""                   # a failure says so on stderr and writes nothing to the protocol channel


# ------------------------------------------------------------------ docs/MCP_CLIENTS.md: the differences it lists are what the server really does
CLIENTS_DOC = (TESTS.parent / "docs" / "MCP_CLIENTS.md").read_text()


def test_the_client_notes_name_every_tool_argument_that_is_advertised_as_optional(rig):
    async def go():
        async with Client(rig.server) as c:
            return (await c.list_tools()).tools
    nullable = {}
    for t in asyncio.run(go()):
        for arg, schema in t.input_schema.get("properties", {}).items():
            if any(x.get("type") == "null" for x in schema.get("anyOf", [])):
                nullable.setdefault(t.name, []).append(arg)
    assert nullable == {"advance_clock": ["to", "by"], "query": ["filters", "lookback", "limit"], "tool_call": ["arguments"]}      # difference 5, exactly
    section = CLIENTS_DOC[CLIENTS_DOC.index("5. **Optional arguments"):CLIENTS_DOC.index("6. **Paths")]
    for tool, args in nullable.items():
        assert f"`{tool}`" in section and all(f"`{a}`" in section for a in args), tool
    assert 'anyOf: [{type}, {type: "null"}]' in section and "this has not been tested with openai or any other provider" in section.lower() and "docs/LIVE_TEST.md" in section


def test_results_arrive_as_text_and_structured_content_alike_and_errors_as_prefixed_text(rig):
    async def go():
        async with Client(rig.server) as c:
            return await c.call_tool("query", {"source": "prices"}), await c.call_tool("advance_clock", {"by": "1d"}), await c.call_tool("query", {"source": "nope"})
    ok, refused, unknown = asyncio.run(go())
    assert not ok.is_error and json.loads(ok.content[0].text) == ok.structured_content                         # difference 4: a client that reads only one of them sees the same data
    for r, tool in ((refused, "advance_clock"), (unknown, "query")):
        assert r.is_error and len(r.content) == 1 and r.content[0].text.startswith(f"Error executing tool {tool}: ") and not r.structured_content
    assert "the clock is owned by the operator: this server does not allow the model to move it" in refused.content[0].text
    for phrase in ("Error executing tool <name>: ", "the clock is owned by the operator: this server does not allow the model to move it", "no structured content on an error"):
        assert phrase in CLIENTS_DOC, phrase


def test_a_new_server_process_is_a_new_session_and_cannot_read_an_earlier_sessions_receipt_but_the_operator_can(tmp_path, capsys):
    from asof.cli import main
    cfg = make_config(tmp_path, tools=[], providers=[])                                                        # one source and nothing else, like the example the clients were run against

    async def first(c):
        return (await c.call_tool("query", {"source": "prices"})).structured_content["receipt_id"]
    rid = run_stdio(cfg, first)                                                                                 # one request, then the server exits (what a one-shot client does)

    async def second(c):
        return await c.call_tool("get_receipt", {"receipt_id": rid})
    again = run_stdio(cfg, second)
    assert again.is_error and again.content[0].text == f"Error executing tool get_receipt: no receipt '{rid}' in this session"      # difference 2
    assert "no receipt '...' in this session" in CLIENTS_DOC
    assert main(["receipt", rid, "--ledger", str(tmp_path / "ledger.db")]) == 0                               # the operator reads any session's receipt
    assert "served 2" in capsys.readouterr().out
    led = Ledger(tmp_path / "ledger.db")
    assert [e.type for e in led.events()].count("session_start") == 2 and led.verify().ok                      # difference 3: every server start is in the ledger

    async def nothing(c):
        return [t.name for t in (await c.list_tools()).tools]
    before = len(Ledger(tmp_path / "ledger.db"))
    run_stdio(cfg, nothing)                                                                                     # only list the tools, call none
    added = [e.type for e in Ledger(tmp_path / "ledger.db").events()][before:]
    assert added == ["session_start", "source_added", "source_loaded", "session_end"]
    assert "`session_start`, `source_added`, `source_loaded` and `session_end` each time (4 events here)" in CLIENTS_DOC


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is not delivered this way on Windows")
def test_a_client_that_stops_the_server_with_sigterm_still_gets_a_session_end_in_the_ledger(tmp_path):
    """Seen with opencode: when the client was stopped, it ended the server with a signal, not by closing its input, and the session had no `session_end` (the chain still verified)."""
    import signal
    cfg = make_config(tmp_path, tools=[], providers=[])
    p = subprocess.Popen([sys.executable, "-m", "asof.mcp_server", "--config", str(cfg)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert "serving session" in p.stderr.readline()                                                        # the server is up (its one startup line)
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=30) == 0
    finally:
        if p.poll() is None:
            p.kill()
        for stream in (p.stdin, p.stdout, p.stderr):
            stream.close()
    led = Ledger(tmp_path / "ledger.db")
    assert [e.type for e in led.events()] == ["session_start", "source_added", "source_loaded", "session_end"] and led.verify().ok
    assert "8. **A client that stops the server with a signal.**" in CLIENTS_DOC and "A hard kill (`SIGKILL`, a crash, power loss) cannot be recorded" in CLIENTS_DOC


def test_the_client_notes_say_what_was_not_tested():
    low = CLIENTS_DOC.lower()
    for phrase in ("**no model was run**", "not tested: any other mcp client application with a model in the loop**, including claude desktop's chat tab", "**unverified**", "there is no network transport, no login and one session per server process",
                   "driven by a real model", "through a script that plays the client's part"):
        assert phrase in low, phrase
    for tool in ("set_clock", "advance_clock", "query", "get_receipt", "verify_ledger"):
        assert f"`{tool}`" in CLIENTS_DOC, tool
    for row in ("\n| MCP Python SDK client |", "\n| **MCP Inspector** ", "\n| **opencode** ", "\n| Claude Desktop |"):      # each client is its own row of the table
        assert row in CLIENTS_DOC, row
    assert '("Free usage limit reached")' in CLIENTS_DOC                                                    # the attempt with a model, and why it did not run
