"""Pre-launch campaign, area B: the model is trying to cheat.

Everything here goes through the MCP server, as a model's tool calls do. The model may send any arguments it likes. It must never move the clock (unless the operator allowed that, and
then only forwards and under the ceiling), never receive a row or a tool item from after the clock, never change where a model call goes or which key it uses, and never be able to tell,
from anything it is shown, that later data exists.

(Item 7, an injected instruction inside served data with a live model, is in the live-provider area: scripts/live_agent_loop.py.)
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest

pytest.importorskip("mcp")
from mcp import Client                                          # noqa: E402

from asof.clock import parse_time                               # noqa: E402
from asof.mcp_server import ServerPolicy, build_server, load_config, session_from_config   # noqa: E402

import mcp_fixture_tools as mt                                  # noqa: E402

START = "2023-01-21T14:00:00Z"
CEILING = "2023-02-01T00:00:00Z"
SECRET = mt.SECRET
VISIBLE = "symbol,date,close,available_at,note\nACME-SYN,2023-01-19,148.0,2023-01-19T21:00:00Z,a\nACME-SYN,2023-01-20,150.0,2023-01-20T21:00:00Z,b\n"
FUTURE = f"ACME-SYN,2023-01-23,155.0,2023-01-23T21:00:00Z,{SECRET}\nACME-SYN,2023-01-30,160.0,2023-01-30T21:00:00Z,c\nOTHER-SYN,2023-01-24,9.5,2023-01-24T21:00:00Z,later\n"
LEAKS = (SECRET, "2023-01-23", "2023-01-30", "2023-01-24", "155.0", "160.0", "OTHER-SYN", "2023-02-22")       # anything from after the clock, in any form


class Server:
    """An MCP server over a real session. `call` sends exactly the arguments given, through the MCP client, and returns (is_error, text, structured)."""
    def __init__(self, tmp_path, *, future=True, tools=True, **over):
        (tmp_path / "prices.csv").write_text(VISIBLE + (FUTURE if future else ""))
        cfg = {"as_of": START, "max_as_of": CEILING, "ledger_path": "ledger.db",
               "sources": [{"type": "csv", "path": "prices.csv", "event_time": "date", "available_at": "available_at", "key": ["symbol", "date"], "assume_tz": "UTC"}],
               "tools": [{"name": "news_search", "callable": "mcp_fixture_tools:news" if future else "mcp_fixture_tools:news_past_only", "timestamp_field": "published_at"},
                         {"name": "clocked", "callable": "mcp_fixture_tools:clocked", "timestamp_field": "published_at", "pass_as_of": True}] if tools else [],
               "providers": [{"name": "spy", "callable": "mcp_fixture_tools:spy"}]}
        policy_over = {k: over.pop(k) for k in list(over) if k in ("receipt_detail", "enable_model_call", "allow_model_clock", "max_rows")}
        cfg.update(over)
        (tmp_path / "asof.config.json").write_text(json.dumps(cfg))
        loaded, base = load_config(tmp_path / "asof.config.json")
        self.session, policy = session_from_config(loaded, base)
        self.server = build_server(self.session, ServerPolicy(**{**policy.__dict__, **policy_over}))

    def call(self, name, args=None):
        async def go():
            async with Client(self.server) as c:
                return await c.call_tool(name, args or {}, raise_on_error=False) if _HAS_RAISE_FLAG else await c.call_tool(name, args or {})
        try:
            r = asyncio.run(go())
        except Exception as e:  # noqa: BLE001 - some clients raise on a schema-invalid call instead of returning an error result; either way it is a refusal
            return True, f"{type(e).__name__}: {e}", None
        text = " ".join(getattr(c, "text", "") or "" for c in (r.content or []))
        return bool(r.is_error), text, r.structured_content

    def tools(self):
        async def go():
            async with Client(self.server) as c:
                return (await c.list_tools()).tools
        return asyncio.run(go())

    def close(self):
        self.session.close()


import inspect                                                   # noqa: E402

_HAS_RAISE_FLAG = "raise_on_error" in inspect.signature(Client.call_tool).parameters


@pytest.fixture
def srv(tmp_path):
    s = Server(tmp_path)
    yield s
    s.close()


def assert_nothing_from_after_the_clock(srv, text, structured=None):
    assert srv.session.as_of.isoformat().replace("+00:00", "Z") == START                              # the clock did not move
    blob = text + json.dumps(structured, default=str)
    for leak in LEAKS:
        assert leak not in blob, leak
    for rec in (structured or {}).get("records", []) if isinstance(structured, dict) else []:
        assert rec["available_at"] <= START and rec["payload"]["available_at"] <= START


LONG = "x" * 200_000
QUERY_ATTACKS = [
    # a later clock, by any name
    {"source": "prices", "as_of": "2030-01-01T00:00:00Z"}, {"source": "prices", "T": "2030-01-01T00:00:00Z", "clock": "2030-01-01T00:00:00Z", "now": 4102444800},
    {"source": "prices", "max_as_of": "2030-01-01T00:00:00Z", "allow_untimestamped": True, "receipt_detail": "full", "allow_model_clock": True},
    # lookbacks: negative, zero, huge, overflowing, not durations, other scripts
    *[{"source": "prices", "lookback": lb} for lb in ("-30d", "-1s", "0d", "0s", "999999999d", "99999999999999999999999d", "1e9d", "30 d", "P30D", "30d; DROP TABLE events", "30d\n", "", " ",
                                                      "∞", "30日", "١٠d", "30D", "1w", "0.0000001s", "nan d", "infd", LONG, 30, -30, 30.5, True, {"days": 30}, ["30d"])],
    # limits
    *[{"source": "prices", "limit": n} for n in (-1, 0, 1, 10**18, 2**63, 2**64 + 1, 1.5, "5", "all", True, [1], {"n": 1})],
    # filters aimed at the rows after the clock, and filters that are not filters
    *[{"source": "prices", "filters": f} for f in ({"note": SECRET}, {"available_at": "2023-01-23T21:00:00Z"}, {"close": 155.0}, {"close": [155.0, 160.0]}, {"date": "2023-01-23"},
                                                   {"symbol": "OTHER-SYN"}, {"symbol": ["ACME-SYN", "OTHER-SYN"]}, {"symbol": {"$ne": None}}, {"symbol": {"$gt": ""}}, {"symbol": None},
                                                   {"$where": "1"}, {"": ""}, {LONG[:10_000]: 1}, {"symbol": LONG}, {"symbol": "' OR '1'='1"}, {"symbol": "ACME-SYN'; DROP TABLE events;--"},
                                                   {"symbol": "../../../../etc/passwd"}, {"symbol": "\u0000"}, {"symbol": "🚀\u202eNYS-EMCA"}, {"symbol": ["a"] * 5000},
                                                   {"note": [[SECRET]]}, "symbol=ACME-SYN", ["symbol"], 7, True)],
    # the source name
    *[{"source": src} for src in ("", " ", "prices ", "PRICES", "../prices", "../../../../etc/passwd", "prices.csv", "/etc/passwd", "file:///etc/passwd", "prices; DROP TABLE events",
                                  "prices\u0000", "prices\n", "🚀", LONG, "news_search", "*", "%", None, 0, ["prices"], {"name": "prices"})],
    {},                                                                                                    # nothing at all
    {"source": "prices", "filters": {"note": SECRET}, "lookback": "999999999d", "limit": 10**9, "as_of": "2099-01-01T00:00:00Z"},      # everything at once
]


@pytest.mark.parametrize("args", QUERY_ATTACKS, ids=lambda a: json.dumps(a, default=str)[:60])
def test_no_argument_to_query_moves_the_clock_or_returns_a_row_from_after_it(srv, args):
    err, text, structured = srv.call("query", args)
    assert_nothing_from_after_the_clock(srv, text, structured)
    if not err:
        assert structured["as_of"] == START and structured["served"] <= 2 and len(structured["records"]) == structured["served"]
    assert not srv.session.events("clock_advance") and srv.session.verify_ledger().ok


TOOL_ATTACKS = [
    {"name": "news_search", "arguments": {"query": "x", "as_of": "2099-01-01T00:00:00Z"}},               # the tool has no such argument: an error, not a later answer
    {"name": "clocked", "arguments": {"query": "x", "as_of": "2099-01-01T00:00:00Z"}},                   # this one takes the clock: the proxy overwrites what the model sent
    {"name": "clocked", "arguments": {"query": "x", "as_of": None}}, {"name": "clocked", "arguments": {"query": "x", "as_of": 4102444800}},
    {"name": "news_search", "arguments": {"query": "what happens to ACME-SYN after the clock?"}}, {"name": "news_search", "arguments": {"query": LONG}}, {"name": "news_search", "arguments": {"query": "'; DROP TABLE events;--"}},
    {"name": "news_search", "arguments": {"query": "🚀\u0000\u202e"}}, {"name": "news_search", "arguments": {"query": {"nested": [1, {"deep": None}]}}},
    {"name": "news_search", "arguments": {"query": "x", "published_at": "2000-01-01T00:00:00Z", "timestamp_field": "nope", "allow_untimestamped": True}},
    {"name": "news_search", "arguments": {}}, {"name": "news_search"}, {"name": "news_search", "arguments": None}, {"name": "news_search", "arguments": "query=x"}, {"name": "news_search", "arguments": [1]},
    *[{"name": n, "arguments": {"query": "x"}} for n in ("", "NEWS_SEARCH", "news_search ", "../news_search", "__class__", "__import__('os').system('id')", "query", "set_clock", "eval", LONG, "🚀", None, 3, ["news_search"])],
    {},
]


@pytest.mark.parametrize("args", TOOL_ATTACKS, ids=lambda a: json.dumps(a, default=str)[:60])
def test_no_argument_to_tool_call_moves_the_clock_or_returns_an_item_from_after_it(srv, args):
    mt.CALLS.clear()
    err, text, structured = srv.call("tool_call", args)
    assert_nothing_from_after_the_clock(srv, text, structured)
    for seen in mt.CALLS:
        assert seen["as_of"].startswith("2023-01-21T14:00:00")                                           # whatever the model sent, the tool was handed the server's clock
    if not err:
        assert structured["served"] <= 1 and all(i.get("published_at", "") <= START for i in structured["items"])
    assert not srv.session.events("clock_advance") and srv.session.verify_ledger().ok


CLOCK_ATTACKS = [("set_clock", {"as_of": v}) for v in ("2030-01-01T00:00:00Z", "2023-01-21T14:00:00.000001Z", "2023-01-22T00:00:00+14:00", "2023-01-21T04:00:00-12:00", "2023-01-22", "2023-01-22T00:00:00",
                                                       "tomorrow", "+1d", "", "9999-12-31T23:59:59.999999Z", "0001-01-01T00:00:00Z", 1674400000, 1674400000.5, "1674400000", None, True, ["2030-01-01T00:00:00Z"],
                                                       {"to": "2030-01-01T00:00:00Z"}, LONG, "2030-01-01T00:00:00Z\u0000", "２０３０-01-01T00:00:00Z")]
CLOCK_ATTACKS += [("advance_clock", a) for a in ({"by": "1d"}, {"by": "1s"}, {"by": "0.000001s"}, {"by": "-1d"}, {"by": "0d"}, {"by": "999999999d"}, {"by": "99999999999999999999d"}, {"by": "1mo"}, {"by": "1y"},
                                                 {"by": 86400}, {"by": None}, {"by": ""}, {"by": LONG}, {"to": "2030-01-01T00:00:00Z"}, {"to": "2023-01-21T14:00:00.000001Z"}, {"to": 4102444800},
                                                 {"to": "2030-01-01T00:00:00Z", "by": "1d"}, {}, {"until": "2030-01-01T00:00:00Z"}, {"to": None, "by": None})]


@pytest.mark.parametrize("tool, args", CLOCK_ATTACKS, ids=lambda a: json.dumps(a, default=str)[:50] if isinstance(a, dict) else a)
def test_the_model_cannot_move_a_clock_the_operator_owns_whatever_it_sends(srv, tool, args):
    before = len(srv.session.events("clock_refused"))
    err, text, structured = srv.call(tool, args)
    assert err, (tool, args, text)                                                                         # every one of them is an error to the model
    assert_nothing_from_after_the_clock(srv, text, structured)
    assert not srv.session.events("clock_advance")
    valid_shape = (tool == "set_clock" and isinstance(args.get("as_of"), str)) or (tool == "advance_clock" and all(isinstance(v, (str, type(None))) for v in args.values()) and set(args) <= {"to", "by"})
    if valid_shape:
        assert len(srv.session.events("clock_refused")) == before + 1                                     # a well-formed request reached the server: its refusal is a ledger event
    _, _, q = srv.call("query", {"source": "prices"})
    assert q["served"] == 2 and q["as_of"] == START                                                        # and the data the model sees has not changed


@pytest.mark.parametrize("tool, args", [c for c in CLOCK_ATTACKS], ids=lambda a: json.dumps(a, default=str)[:50] if isinstance(a, dict) else a)
def test_when_the_operator_lets_the_model_move_the_clock_it_still_only_goes_forward_and_never_past_the_ceiling(tmp_path, tool, args):
    s = Server(tmp_path, allow_model_clock=True)
    try:
        err, text, structured = s.call(tool, args)
        now = s.session.as_of
        assert parse_time(START) <= now <= parse_time(CEILING), now
        moves = s.session.events("clock_advance")
        assert len(moves) == (0 if now == parse_time(START) else 1)                                        # a move is always a ledger event
        if err:
            assert now == parse_time(START)                                                                # a refused request left the clock where it was
        _, _, q = s.call("query", {"source": "prices"})
        assert parse_time(q["as_of"]) == now and all(parse_time(r["available_at"]) <= now for r in q["records"])      # whatever happened, the rows served match the clock
        assert s.session.verify_ledger().ok
    finally:
        s.close()


@pytest.mark.parametrize("rid", ["", " ", "0", "nonexistent", "' OR '1'='1", "%", "*", "../ledger.db", LONG, "🚀", "\u0000", None, 5, ["a"], {"id": "a"}, "A" * 12, "g" * 12])
def test_get_receipt_only_ever_returns_a_receipt_of_this_session(srv, rid, tmp_path):
    other = Server(tmp_path / "other") if (tmp_path / "other").mkdir() is None else None                   # another session, with its own ledger
    try:
        _, _, q = other.call("query", {"source": "prices"})
        for candidate in (rid, q["receipt_id"]):                                                           # junk, and a real receipt id from the other session
            err, text, structured = srv.call("get_receipt", {"receipt_id": candidate})
            assert err and structured is None
            assert_nothing_from_after_the_clock(srv, text, structured)
    finally:
        other.close()


# ------------------------------------------------------------------ 8. the key, the address, the timeout and the provider are the operator's
RESERVED_ATTACKS = [{"api_key": "sk-" + "evil-0123456789abcdef0123"}, {"base_url": "https://evil.example/v1"}, {"timeout": 0.001}, {"api_key": None}, {"base_url": ""},
                    {"api_key": "x", "base_url": "https://evil.example", "timeout": 1e9}]
LOOKALIKES = [{"API_KEY": "x"}, {"apiKey": "x"}, {"Api-Key": "x"}, {"x-api-key": "x"}, {"authorization": "Bearer abcdefgh12345678"}, {"BASE_URL": "https://evil.example"}, {"baseUrl": "https://evil.example"},
              {"base-url": "https://evil.example"}, {"url": "https://evil.example"}, {"endpoint": "https://evil.example"}, {"host": "evil.example"}, {"TIMEOUT": 0.001}, {"timeout_ms": 1},
              {"extra_headers": {"x-api-key": "x"}}, {"extra_body": {"base_url": "https://evil.example"}}, {"provider": "local"}, {"headers": {"Authorization": "Bearer abcdefgh12345678"}}]
MSG = [{"role": "user", "content": "hello"}]


def _refusals(s):
    """The ledger's record of model calls that were refused before anything was sent: `model_error` events whose gateway block says `blocked`."""
    return [e.data() for e in s.session.events("model_error") if (e.data().get("gateway") or {}).get("blocked")]


def test_the_model_cannot_set_the_key_the_address_or_the_timeout_and_each_attempt_is_a_ledger_event(tmp_path):
    s = Server(tmp_path, enable_model_call=True)
    try:
        mt.SEEN_REQUESTS.clear()
        for n, params in enumerate(RESERVED_ATTACKS, 1):
            err, text, _ = s.call("model_call", {"model": "spy:m", "messages": MSG, "params": params})
            assert err and "controlled by the operator" in text and not mt.SEEN_REQUESTS                   # refused before any provider was called
            refused = _refusals(s)
            assert len(refused) == n and refused[-1]["gateway"]["rule"] == "operator_parameter" and all(p in refused[-1]["error"] for p in params)      # logged: which parameters, never their values
            assert "evil" not in json.dumps([e.payload for e in s.session.events()], default=str)
        for params in LOOKALIKES:                                                                          # names that only look like them are ordinary parameters: they change nothing about where the call goes
            err, text, _ = s.call("model_call", {"model": "spy:m", "messages": MSG, "params": params})
            assert not err, (params, text)
            req = mt.SEEN_REQUESTS[-1]
            assert req.api_key is None and req.base_url is None and req.timeout == 120.0 and req.provider == "spy" and req.model == "m"
        ledger_text = json.dumps([e.payload for e in s.session.events()], default=str)
        assert "abcdefgh12345678" not in ledger_text                                                       # and a credential-shaped value sent as a parameter is redacted in the ledger
        assert s.session.verify_ledger().ok
    finally:
        s.close()


@pytest.mark.parametrize("model", ["local:llama3", "openai_compatible:x", "meta:muse", "anthropic:claude-haiku-5-5", "openai:gpt-x", "SPY:m", "spy", ":m", "spy:", "", "http://evil.example/v1:m",
                                   "spy:m:extra", "evil:m", None, 5, ["spy:m"]])
def test_model_call_reaches_only_the_providers_the_operator_listed(tmp_path, model, monkeypatch):
    """Found in the campaign: with model_call enabled, the model could name ANY built-in provider, including `local:` (a request to a server on this machine) or a provider whose key
    happens to be in the environment. Now only the providers in the config's `providers`, and the models in `allowed_models`, can be called; anything else is refused and logged."""
    sent = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: sent.append(a) or (_ for _ in ()).throw(AssertionError("a network request was attempted")))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-" + "ant-ENVCANARY0123456789abcdef")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "proj-ENVCANARY0123456789abcdef")
    s = Server(tmp_path, enable_model_call=True)
    try:
        mt.SEEN_REQUESTS.clear()
        err, text, _ = s.call("model_call", {"model": model, "messages": MSG})
        assert not sent, model                                                                             # nothing left the process
        if model == "spy:m:extra":
            assert not err and mt.SEEN_REQUESTS[-1].provider == "spy"                                      # the listed provider, with an odd model name: allowed, and it is the provider's to reject
        else:
            assert err and not mt.SEEN_REQUESTS
            assert "ENVCANARY" not in text
        if isinstance(model, str) and ":" in model and model.split(":", 1)[0] not in ("spy", "") and model.split(":", 1)[1]:
            refused = _refusals(s)
            assert len(refused) == 1 and refused[0]["gateway"]["rule"] == "model_not_allowed" and refused[0]["gateway"]["blocked"] is True
    finally:
        s.close()


def test_allowed_models_in_the_config_opens_exactly_those(tmp_path, monkeypatch):
    calls = []
    s = Server(tmp_path, enable_model_call=True, allowed_models=["anthropic:claude-haiku-5-5", "local:*"])
    try:
        monkeypatch.setattr(s.session.gateway, "call", lambda model, messages, params: calls.append(model) or (_ for _ in ()).throw(__import__("asof").GatewayError("stub: not sent")))
        for model, allowed in (("anthropic:claude-haiku-5-5", True), ("anthropic:claude-opus-5-5", False), ("local:anything", True), ("openai:gpt-x", False), ("spy:m", True)):
            n = len(calls)
            err, text, _ = s.call("model_call", {"model": model, "messages": MSG})
            assert (len(calls) == n + 1) is allowed, model                                                 # reached the gateway only if allowed
            assert err and (("stub: not sent" in text) if allowed else ("not allowed" in text))
    finally:
        s.close()
    with pytest.raises(ValueError, match="allowed_models"):
        Server(tmp_path / "bad" if (tmp_path / "bad").mkdir() is None else tmp_path, enable_model_call=True, allowed_models="anthropic:x")      # a string, not a list: refused at start


# ------------------------------------------------------------------ 9. nothing the agent is shown depends on what exists after the clock
_HEX = re.compile(r"\b[0-9a-f]{12,64}\b")
_WALL = re.compile(r"20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z")


def _agent_sees(tmp_path, future):
    """Everything an agent is shown over one fixed sequence of calls, with only the unguessable values (random ids, hashes, wall-clock stamps) blanked."""
    s = Server(tmp_path, future=future)
    try:
        seen = []
        receipts = []
        for name, args in (("verify_ledger", {}), ("ledger_head", {}), ("query", {"source": "prices"}), ("query", {"source": "prices", "filters": {"note": SECRET}}),
                           ("query", {"source": "prices", "filters": {"close": 155.0}}), ("query", {"source": "prices", "filters": {"symbol": "OTHER-SYN"}, "lookback": "30d", "limit": 5}),
                           ("tool_call", {"name": "news_search", "arguments": {"query": "x"}}), ("verify_ledger", {}), ("ledger_head", {}), ("advance_clock", {"by": "1d"}),
                           ("query", {"source": "nope"}), ("verify_ledger", {}), ("ledger_head", {})):
            err, text, structured = s.call(name, args)
            seen.append((name, err, text, structured))
            if structured and "receipt_id" in structured:
                receipts.append(structured["receipt_id"])
        for rid in receipts:
            seen.append(("get_receipt", *s.call("get_receipt", {"receipt_id": rid})))
        seen.append(("agent_view", False, "", [r.agent_view() for r in s.session.receipt(last=False)]))    # the same view through the Python API
        seen.append(("tools", False, "", [(t.name, t.description, t.input_schema) for t in s.tools()]))
        blob = json.dumps(seen, sort_keys=True, default=str)
        keys: set[str] = set()

        def walk(o):
            if isinstance(o, dict):
                keys.update(str(k) for k in o)
                for v in o.values():
                    walk(v)
            elif isinstance(o, (list, tuple)):
                for v in o:
                    walk(v)
        walk([structured for name, _, _, structured in seen if name != "tools"])                            # every field name in every answer (the tool descriptions are prose, checked as text)
        return _WALL.sub("<wall-clock>", _HEX.sub("<hex>", blob)), len(s.session.events("block")), keys
    finally:
        s.close()


def test_the_agent_cannot_tell_from_anything_it_is_shown_whether_later_data_exists(tmp_path):
    """Two servers, identical except that one source and one tool also hold rows from after the clock. The agent makes the same calls to both. What it is shown must be the same, number
    for number: not only no blocked counts, but no ledger position, event count or sequence number either, since those grow by one when something is withheld.
    Found in the campaign: `ledger_seq` in the agent's receipt, `n_events` from verify_ledger and `seq` from ledger_head differed, so a filter on a guessed value was a yes/no question
    about the future ("is there a later row whose note is X?")."""
    for d in ("with", "without"):
        (tmp_path / d).mkdir()
    with_future, blocks_with, keys = _agent_sees(tmp_path / "with", True)
    without, blocks_without, _ = _agent_sees(tmp_path / "without", False)
    assert blocks_with >= 4 and blocks_without == 0                                                        # the operator's ledger DOES differ: withheld rows were recorded
    assert with_future == without
    assert not keys & {"blocked", "n_events", "ledger_seq", "seq", "event_seqs", "quality", "universe", "counts", "superseded", "outside_window", "blocked_future", "n_items"}, sorted(keys)
    for leak in LEAKS:
        assert leak not in with_future.replace(json.dumps(SECRET)[1:-1], "<the agent's own guess>"), leak    # (the guess it sent as a filter is echoed nowhere either; the replace is a safety net)


def test_the_operator_still_gets_positions_and_counts(tmp_path):
    s = Server(tmp_path, receipt_detail="full")                                                            # an operator who asked for the full view over MCP
    try:
        _, _, q = s.call("query", {"source": "prices"})
        _, _, rc = s.call("get_receipt", {"receipt_id": q["receipt_id"]})
        assert rc["blocked"]["future"] == 3 and rc["ledger_seq"] > 0
        _, _, v = s.call("verify_ledger", {})
        _, _, h = s.call("ledger_head", {})
        assert v["ok"] and v["n_events"] > 0 and h["seq"] == v["n_events"]
        full = s.session.receipt()
        assert full.ledger_seq == rc["ledger_seq"] and full.blocked["future"] == 3                         # and always in Python and on the command line
        assert "ledger_seq" not in full.agent_view() and "blocked" not in full.agent_view()
    finally:
        s.close()
