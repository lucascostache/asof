"""Pre-launch campaign, area F: the MCP server as a process, and its clients.

24. the MCP Inspector (another client, in another language): every tool, every documented error, arguments the schema does not allow. Runs when the Inspector is on this machine
    (`MCP_INSPECTOR=/path/to/mcp-inspector`, or one found in the npx cache); skipped otherwise, and said so.
25. lifecycle: 100 starts and stops, stopped four different ways; two servers on one ledger at the same time
26. a client that starts the server afresh for each request (see also test_mcp.py: the receipts of earlier requests are in the ledger, not in the session)
27. docs/CLAUDE_DESKTOP_CHECK.md: what the page tells a person to expect is what the server returns
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import Client                                          # noqa: E402
from mcp.client.stdio import StdioServerParameters             # noqa: E402

from asof import Ledger                                         # noqa: E402
from asof.cli import main                                       # noqa: E402

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
ENV = {**os.environ, "PYTHONPATH": str(TESTS) + os.pathsep + str(ROOT)}
START = "2023-01-21T14:00:00Z"
PRICES = ("symbol,date,close,available_at\nACME-SYN,2023-01-19,148.0,2023-01-19T21:00:00Z\nACME-SYN,2023-01-20,150.0,2023-01-20T21:00:00Z\n"
          "ACME-SYN,2023-01-23,155.0,2023-01-23T21:00:00Z\nACME-SYN,2023-01-30,160.0,2023-01-30T21:00:00Z\n")


def config(d, **over):
    d.mkdir(parents=True, exist_ok=True)
    (d / "prices.csv").write_text(PRICES)
    cfg = {"as_of": START, "max_as_of": "2023-02-01T00:00:00Z", "ledger_path": "ledger.db",
           "sources": [{"type": "csv", "path": "prices.csv", "event_time": "date", "available_at": "available_at", "key": ["symbol", "date"], "assume_tz": "UTC"}], **over}
    (d / "asof.config.json").write_text(json.dumps(cfg))
    return d / "asof.config.json"


def stdio(cfg):
    return StdioServerParameters(command=sys.executable, args=["-m", "asof.mcp_server", "--config", str(cfg)], env=ENV)


# ------------------------------------------------------------------ 25. lifecycle
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_100_starts_and_stops_four_different_ways_each_leave_a_closed_session(tmp_path):
    """A client may end the server by closing its input, with SIGTERM, with Ctrl-C (SIGINT) or by closing the terminal (SIGHUP). Found in the campaign: only the first two wrote
    `session_end`. All four do now. 25 of each, one after the other, on one ledger."""
    cfg = config(tmp_path)
    ways = [None, signal.SIGTERM, signal.SIGINT, signal.SIGHUP]
    codes = []
    for n in range(100):
        p = subprocess.Popen([sys.executable, "-m", "asof.mcp_server", "--config", str(cfg)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ENV)
        try:
            assert "serving session" in p.stderr.readline(), n
            how = ways[n % 4]
            if how is None:
                p.stdin.close()
            else:
                p.send_signal(how)
            codes.append(p.wait(timeout=30))
            assert "Traceback" not in p.stderr.read(), (n, how)
        finally:
            if p.poll() is None:
                p.kill()
            for stream in (p.stdin, p.stdout, p.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
    assert set(codes) == {0}, sorted(set(codes))                                                           # every one of them is an ordinary exit
    led = Ledger(tmp_path / "ledger.db")
    types = [e.type for e in led.events()]
    assert types == ["session_start", "source_added", "source_loaded", "session_end"] * 100               # 100 sessions, each opened and closed, none interleaved
    assert len({e.session_id for e in led.events()}) == 100
    v = led.verify()
    assert v.ok and v.anchor == "ok" and v.n_events == 400
    led.close()
    assert sorted(x.name for x in tmp_path.iterdir()) == ["asof.config.json", "head.json", "ledger.db", "prices.csv"]      # nothing left behind: no journal, no temporary anchor


def test_two_servers_on_one_ledger_at_the_same_time_share_one_valid_chain_and_keep_their_sessions_apart(tmp_path):
    """Two clients pointed at the same config start two servers on the same ledger file. Nothing refuses that; the ledger serialises their writes (one writer at a time), so there is
    one valid chain holding two sessions, and neither session can read the other's receipts."""
    cfg = config(tmp_path)

    async def one(c, n):
        ids = []
        for _ in range(n):
            q = (await c.call_tool("query", {"source": "prices"})).structured_content
            assert [r["payload"]["close"] for r in q["records"]] == [148.0, 150.0]
            ids.append(q["receipt_id"])
            await c.call_tool("advance_clock", {"by": "1d"}, raise_on_error=False) if "raise_on_error" in c.call_tool.__code__.co_varnames else None
        return ids

    async def go():
        async with Client(stdio(cfg)) as a, Client(stdio(cfg)) as b:
            ids_a, ids_b = await asyncio.gather(one(a, 15), one(b, 15))
            own = (await a.call_tool("get_receipt", {"receipt_id": ids_a[0]})).structured_content
            other = await a.call_tool("get_receipt", {"receipt_id": ids_b[0]}, raise_on_error=False) if "raise_on_error" in a.call_tool.__code__.co_varnames else None
            va, vb = (await a.call_tool("verify_ledger", {})).structured_content, (await b.call_tool("verify_ledger", {})).structured_content
            return ids_a, ids_b, own, other, va, vb
    ids_a, ids_b, own, other, va, vb = asyncio.run(go())
    assert own["call_id"] == ids_a[0] and va["ok"] and vb["ok"]
    if other is not None:
        assert other.is_error and "in this session" in other.content[0].text                               # a receipt of the other server's session is refused
    led = Ledger(tmp_path / "ledger.db")
    events = led.events()
    assert [e.seq for e in events] == list(range(1, len(events) + 1)) and led.verify().ok and led.verify().anchor == "ok"
    sessions = {e.session_id for e in events}
    assert len(sessions) == 2 and len(set(ids_a) | set(ids_b)) == 30
    for sid in sessions:
        mine = [e.type for e in events if e.session_id == sid]
        assert mine[0] == "session_start" and mine[-1] == "session_end" and mine.count("query") == 15 and mine.count("record_served") == 30
    for rid in (ids_a[0], ids_b[-1]):                                                                      # 26: the operator reads any session's receipt from the ledger, whichever server made it
        assert main(["receipt", rid, "--ledger", str(tmp_path / "ledger.db")]) == 0


# ------------------------------------------------------------------ 27. the Claude Desktop checklist
CHECK = (ROOT / "docs" / "CLAUDE_DESKTOP_CHECK.md").read_text()


def check_block(name):
    m = re.search(rf"<!-- {name}:start -->\n```\w*\n(.*?)```\n<!-- {name}:end -->", CHECK, re.S)
    assert m, name
    return m.group(1)


def test_what_the_claude_desktop_checklist_says_to_expect_is_what_the_server_returns(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))                                                              # the page uses ~/asof-desktop-check
    init = shlex.split(check_block("desktop-check-init").strip())
    assert init[0] == "strathora" and main([os.path.expanduser(a) for a in init[1:]]) == 0                 # step 1, the page's own command
    printed = capsys.readouterr().out
    real = json.loads(printed[printed.index("{\n"):])["mcpServers"]["asof"]
    shown = json.loads(check_block("desktop-check-config"))["mcpServers"]["asof"]
    assert set(shown) == set(real) == {"command", "args"} and shown["args"][:4] == real["args"][:4] == ["-m", "asof", "serve-mcp", "--config"]
    assert real["args"][4] == str(tmp_path / "asof-desktop-check" / "asof.config.json")

    async def go():
        async with Client(StdioServerParameters(command=real["command"], args=real["args"], env=ENV)) as c:
            names = sorted(t.name for t in (await c.list_tools()).tools)
            q = (await c.call_tool("query", {"source": "prices"})).structured_content
            refused = []
            for tool, args in (("set_clock", {"as_of": "2030-01-01T00:00:00Z"}), ("advance_clock", {"to": "2030-01-01T00:00:00Z"})):
                try:
                    r = await c.call_tool(tool, args)
                    refused.append(r.content[0].text if r.is_error else "NOT REFUSED")
                except Exception as e:  # noqa: BLE001 - a client that raises on a tool error
                    refused.append(str(e))
            rc = (await c.call_tool("get_receipt", {"receipt_id": q["receipt_id"]})).structured_content
            return names, q, refused, rc
    names, q, refused, rc = asyncio.run(go())
    for tool in ("query", "tool_call", "get_receipt", "verify_ledger", "ledger_head", "set_clock", "advance_clock"):
        assert tool in names and f"`{tool}`" in CHECK
    assert len(names) == 7 and "The seven tools appeared" in CHECK
    closes = [r["payload"]["close"] for r in q["records"]]                                                 # prompt 1
    assert closes == [147.0, 148.0, 150.0] and q["records"][-1]["payload"]["date"] == "2023-01-20" and "**150.0 on 2023-01-20**" in CHECK
    assert "**155.0, 152.5 or 158.0**" in CHECK and not {155.0, 152.5, 158.0} & set(closes)
    for text in refused:                                                                                   # prompt 2
        assert "the clock is owned by the operator: this server does not allow the model to move it" in text
    assert '*"the clock is owned by the operator: this server does not allow the model to move it"*' in CHECK
    fields = [ln.split()[0] for ln in check_block("desktop-check-receipt").strip().splitlines()]           # prompt 3: exactly the fields the page lists
    assert fields == list(rc) and len(fields) == 8 and "blocked" not in rc and "ledger_seq" not in rc
    assert rc["kind"] == "query" and rc["target"] == "prices" and rc["as_of"] == START and rc["served"] == 3 and rc["served_timing"] == {"declared": 3, "unknown": 0}
    assert rc["served_ids"] == ["ACME-SYN|2023-01-18", "ACME-SYN|2023-01-19", "ACME-SYN|2023-01-20"] and len(rc["ledger_hash"]) == 64 and len(rc["call_id"]) == 12
    for line in check_block("desktop-check-receipt").strip().splitlines():
        name, shown_value = line.split(None, 1)
        if shown_value.startswith(("\"", "{", "[")) or shown_value.strip().isdigit():
            assert json.loads(shown_value) == rc[name], name                                               # the values shown on the page are the real ones
    capsys.readouterr()
    for cmd in check_block("desktop-check-operator").strip().splitlines():                                 # step 3, the page's own commands
        args = [os.path.expanduser(a).replace("RECEIPT_ID", q["receipt_id"]) for a in shlex.split(cmd)]
        assert args[0] == "strathora" and main(args[1:]) == 0
    out = capsys.readouterr().out
    assert "served 3" in out and "blocked: future=3" in out and "**`blocked: future=3`**" in CHECK
    assert re.search(r"OK: \d+ events, hash chain intact, tail anchor: ok", out) and "`OK: N events, hash chain intact, tail anchor: ok`" in CHECK
    types = [e.type for e in Ledger(tmp_path / "asof-desktop-check" / "ledger.db").events()]
    assert types.count("clock_refused") == 2 and "clock_advance" not in types                              # "one line per attempt, and no clock_advance line at all"
    assert "strathora-asof 0.5.0" in CHECK and "asof 0.5.0: serving session" in CHECK


# ------------------------------------------------------------------ 24. the MCP Inspector
def _inspector():
    found = os.environ.get("MCP_INSPECTOR") or next(iter(sorted(glob.glob(os.path.expanduser("~/.npm/_npx/*/node_modules/.bin/mcp-inspector")))), None)
    return found if found and Path(found).exists() and shutil.which("node") else None


INSPECTOR = _inspector()


@pytest.mark.skipif(INSPECTOR is None, reason="the MCP Inspector is not on this machine (set MCP_INSPECTOR to its mcp-inspector binary, or run it once with npx)")
def test_the_mcp_inspector_sees_every_tool_every_documented_error_and_the_schema_refuses_bad_arguments(tmp_path):
    cfg = config(tmp_path, enable_model_call=True, allowed_models=["fake:allowed"],
                 tools=[{"name": "news_search", "callable": "mcp_fixture_tools:news", "timestamp_field": "published_at"}], providers=[{"name": "fake", "callable": "mcp_fixture_tools:llm"}])
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {"asof": {"command": sys.executable, "args": ["-m", "asof.mcp_server", "--config", str(cfg)], "env": {"PYTHONPATH": ENV["PYTHONPATH"]}}}}))

    def run(*args):
        r = subprocess.run(["node", INSPECTOR, "--cli", "--config", str(mcp_json), "--server", "asof", *args], capture_output=True, text=True, timeout=180, cwd=tmp_path)
        try:
            return r.returncode, json.loads(r.stdout), r.stderr
        except ValueError:
            return r.returncode, None, r.stdout + r.stderr

    def call(tool, *pairs):
        argv = ["--method", "tools/call", "--tool-name", tool]
        for p in pairs:
            argv += ["--tool-arg", p]
        return run(*argv)

    def text(result):
        code, data, raw = result
        return (data["content"][0]["text"] if data and data.get("content") else "") + raw

    def is_error(result):
        code, data, raw = result
        return code != 0 or bool(data and data.get("isError"))

    code, listed, raw = run("--method", "tools/list")
    assert code == 0, raw
    tools = {t["name"]: t for t in listed["tools"]}
    assert sorted(tools) == ["advance_clock", "get_receipt", "ledger_head", "model_call", "query", "set_clock", "tool_call", "verify_ledger"]      # all eight, model_call included when enabled
    for t in tools.values():
        assert t["description"] and t["inputSchema"]["type"] == "object"
    assert tools["query"]["inputSchema"]["required"] == ["source"] and tools["get_receipt"]["inputSchema"]["required"] == ["receipt_id"]

    ok = call("query", "source=prices")                                                                    # every tool, used as intended
    q = json.loads(text(ok))
    assert not is_error(ok) and q["served"] == 2 and [r["payload"]["close"] for r in q["records"]] == [148.0, 150.0] and ok[1]["structuredContent"] == q
    ok = call("query", "source=prices", 'filters={"close":150.0}', "lookback=30d", "limit=1")
    assert not is_error(ok) and json.loads(text(ok))["served"] == 1
    ok = call("tool_call", "name=news_search", 'arguments={"query":"x"}')
    assert not is_error(ok) and [i["title"] for i in json.loads(text(ok))["items"]] == ["ACME-SYN raises guidance"]
    ok = call("model_call", "model=fake:allowed", 'messages=[{"role":"user","content":"hi"}]')
    assert not is_error(ok) and json.loads(text(ok))["text"] == "echo:hi"
    ok = call("verify_ledger")
    assert not is_error(ok) and json.loads(text(ok)) == {"ok": True, "anchor": "ok", "head_hash": json.loads(text(ok))["head_hash"], "reason": None}
    ok = call("ledger_head")
    assert not is_error(ok) and set(json.loads(text(ok))) == {"hash", "recorded_at"}

    documented = [                                                                                         # every documented error
        (("query", "source=nope"), "unknown source 'nope'"),
        (("query", "source=prices", "lookback=1mo"), "cannot read duration"),
        (("query", "source=prices", "lookback=-5d"), "cannot read duration"),
        (("query", "source=prices", "limit=-1"), "limit must not be negative"),
        (("set_clock", "as_of=2023-01-25T00:00:00Z"), "the clock is owned by the operator"),
        (("advance_clock", "by=1d"), "the clock is owned by the operator"),
        (("advance_clock", "to=2023-01-25T00:00:00Z"), "the clock is owned by the operator"),
        (("get_receipt", "receipt_id=nope"), "no receipt 'nope' in this session"),
        (("get_receipt", f"receipt_id={q['receipt_id']}"), "in this session"),                             # a real id, but from an earlier server process: the Inspector starts a new one each time
        (("tool_call", "name=nope"), "unknown tool 'nope'"),
        (("tool_call", "name=news_search", 'arguments={"nope":1}'), "unexpected keyword argument"),
        (("model_call", "model=fake:allowed", 'messages=[{"role":"user","content":"hi"}]', 'params={"api_key":"x"}'), "controlled by the operator"),
        (("model_call", "model=openai:gpt-x", 'messages=[{"role":"user","content":"hi"}]'), "is not allowed on this server"),      # a built-in provider the operator did not list
        (("model_call", "model=local:llama", 'messages=[{"role":"user","content":"hi"}]'), "is not allowed on this server"),
        (("model_call", "model=fake:allowed", 'messages=[{"role":"user","content":"hi"}]', 'params={"stream":true}'), "streaming is not supported"),
        (("model_call", "model=nonsense", 'messages=[{"role":"user","content":"hi"}]'), "provider:name"),
    ]
    for args, expect in documented:
        r = call(*args)
        assert is_error(r) and expect in text(r), (args, text(r)[:300])
        assert "Traceback" not in text(r)

    for args in (("query",), ("query", "source=123"), ("query", "source=prices", "limit=many"), ("query", "source=prices", "filters=notanobject"), ("get_receipt",),      # what the schema does not allow
                 ("tool_call", "name=news_search", "arguments=7"), ("set_clock",), ("model_call", "model=fake:allowed"), ("no_such_tool",)):
        r = call(*args)
        assert is_error(r), (args, text(r)[:300])
    led = Ledger(tmp_path / "ledger.db")
    assert led.verify().ok and not led.events(type="clock_advance")
    assert len(led.events(type="clock_refused")) == 3
    refused = [e.data()["gateway"]["rule"] for e in led.events(type="model_error") if (e.data().get("gateway") or {}).get("blocked")]
    assert sorted(refused) == ["model_not_allowed", "model_not_allowed", "operator_parameter", "streaming_unsupported"]
    starts = len(led.events(type="session_start"))
    assert starts == len(led.events(type="session_end")) >= 30                                             # one server process, one closed session, per Inspector request
