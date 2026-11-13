"""docs/CLAUDE_DESKTOP.md: the server side of its three tests, run through a real stdio MCP client with the example config exactly as the guide gives it.
(What Claude Desktop's screens look like and what the model says are NOT covered here: the guide says so.)"""
import asyncio
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import Client                                          # noqa: E402
from mcp.client.stdio import StdioServerParameters             # noqa: E402

from asof import Ledger                                         # noqa: E402
from asof.receipt import receipt_from_ledger                    # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "CLAUDE_DESKTOP.md").read_text()
EXAMPLE = ROOT / "docs" / "examples" / "claude_desktop"


def doc_block(name):
    m = re.search(rf"<!-- {name}:start -->\s*```\w*\n(.*?)```\s*<!-- {name}:end -->", DOC, re.S)
    assert m, name
    return json.loads(m.group(1))


@pytest.fixture()
def server(tmp_path):
    folder = tmp_path / "asof-claude-test"
    shutil.copytree(EXAMPLE, folder)                                                       # step 2: cp -R docs/examples/claude_desktop ~/asof-claude-test
    entry = doc_block("claude-desktop-test-config")["mcpServers"]["asof"]                 # step 3: the config block, with the two absolute paths filled in
    assert entry["command"].startswith("/ABSOLUTE/PATH/TO/") and entry["args"][-1].startswith("/ABSOLUTE/PATH/TO/")
    params = StdioServerParameters(command=sys.executable, args=[*entry["args"][:-1], str(folder / "asof.config.json")])
    return params, folder / "ledger.db"


def run(params, steps):
    async def go():
        async with Client(params) as c:
            names = sorted(t.name for t in (await c.list_tools()).tools)
            return names, await steps(c)
    return asyncio.run(go())


def test_the_guides_three_tests_behave_as_it_says(server):
    params, ledger_path = server

    async def steps(c):
        q = await c.call_tool("query", {"source": "news", "filters": {"ticker": "ACME-SYN"}})                      # test 1
        set_ = await c.call_tool("set_clock", {"as_of": "2030-01-01T00:00:00Z"})                                  # test 2 (both ways the model could try)
        adv = await c.call_tool("advance_clock", {"to": "2030-01-01T00:00:00Z"})
        again = await c.call_tool("query", {"source": "news", "filters": {"ticker": "ACME-SYN"}})
        rec = await c.call_tool("get_receipt", {"receipt_id": q.structured_content["receipt_id"]})                # test 3
        return q, set_, adv, again, rec

    names, (q, set_, adv, again, rec) = run(params, steps)
    assert names == ["advance_clock", "get_receipt", "ledger_head", "query", "set_clock", "tool_call", "verify_ledger"]   # the tool list the guide promises; no model_call
    # test 1: three headlines, the 18th to the 20th, nothing from the 23rd or 24th, and nothing the model could use to learn the later ones
    s = q.structured_content
    titles = [r["payload"]["title"] for r in s["records"]]
    assert s["served"] == 3 and s["as_of"] == "2023-01-20T23:59:59Z" and len(titles) == 3 and all(t.startswith("[SYNTHETIC]") for t in titles)
    assert not any(w in json.dumps(s) for w in ("beats estimates", "raises its outlook", "shares jump", "2023-01-23", "2023-01-24"))
    # test 2: refused both ways, in the words the guide quotes, and the clock did not move
    for r in (set_, adv):
        assert r.is_error and "the clock is owned by the operator: this server does not allow the model to move it" in r.content[0].text
    assert again.structured_content["served"] == 3 and again.structured_content["as_of"] == "2023-01-20T23:59:59Z"
    # test 3: what the model is shown has no blocked counts and no hint that anything was withheld
    shown = rec.structured_content
    assert "blocked" not in shown and shown["served"] == 3 and {"call_id", "target", "as_of", "served_ids", "ledger_hash"} <= set(shown)
    assert not re.search(r"block|withheld|hidden", json.dumps(shown), re.I)
    # the operator's checks from the guide
    led = Ledger(ledger_path)
    full = receipt_from_ledger(led, s["receipt_id"])
    assert full.served == 3 and full.blocked["future"] == 2                                                    # "served 3 ... blocked: future=2"
    con = sqlite3.connect(ledger_path)
    types = [r[0] for r in con.execute("select type from events where type like 'clock%'")]
    assert types.count("clock_refused") == 2 and "clock_advance" not in types                                  # a refusal is in the ledger; no advance
    assert led.verify().ok


def test_the_guide_is_consistent_with_the_files_it_names():
    cfg = json.loads((EXAMPLE / "asof.config.json").read_text())
    assert cfg["as_of"] == "2023-01-20T23:59:59Z" and cfg["allow_model_clock"] is False and "2023-01-20T23:59:59Z" in DOC
    news = [json.loads(line) for line in (EXAMPLE / "news.jsonl").read_text().splitlines()]
    assert len(news) == 5 and all(n["title"].startswith("[SYNTHETIC]") for n in news)
    assert sum(n["published_at"] <= cfg["as_of"] for n in news) == 3 and "three published 18 to 20 January" in DOC
    for needle in ("Test 1", "Test 2", "Test 3", "Show me your receipt.", "Move the clock to 2030", "As of 2023-01-20, summarize news on ACME-SYN", "UNVERIFIED in Claude Desktop"):
        assert needle in DOC
    assert "cp -R docs/examples/claude_desktop ~/asof-claude-test" in DOC
