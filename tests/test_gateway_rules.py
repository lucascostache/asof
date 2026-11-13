"""The gateway rules on every model call: server-side tools OFF by default (and FLAGGED if allowed), contributor models blocked, provider-side state refused, tool calls through the AsOf proxy, version pins,
the Meta provider (config only, UNVERIFIED), tokens and reasoning in one shape, and the flat receipt. Everything runs against a local mock HTTP server: no network, no real keys, no live calls."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from asof import AsOfError, GatewayPolicy, GatewayRuleError, ModelGateway, ProviderSpec, Session
from asof import modelrules as R
from asof.errors import GatewayError

from conftest import ListSource, rec

KEY = "sk-" + "test-SECRETKEY0123456789"          # a fake key, written in two parts so the source holds no key-shaped string (secret scanners, push protection)
MSGS = [{"role": "user", "content": "what is the close?"}]


@pytest.fixture
def server(monkeypatch):
    """A mock provider. ``respond[0]`` is a callable (path, body) -> dict; the default answers in the OpenAI and Anthropic shapes."""
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    seen: list[dict] = []

    def default(path: str, body: dict) -> dict:
        if path.endswith("/v1/messages"):
            return {"model": "claude-served-1", "content": [{"type": "text", "text": "anthropic says hi"}], "stop_reason": "end_turn",
                    "usage": {"input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 5}}
        return {"model": "gpt-served-1", "choices": [{"message": {"content": "openai says hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 2}, "completion_tokens_details": {"reasoning_tokens": 3}}}

    respond = [default]

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            raw = json.dumps(respond[0](self.path, body)).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen, respond
    srv.shutdown()


def events(s, type_):
    return [e for e in s.ledger.events(session_id=s.id, type=type_)]


# ---------------------------------------------------------------- server-side tools: OFF by default
@pytest.mark.parametrize("model,params", [
    ("anthropic:claude-x", {"tools": [{"type": "web_search_20250305", "name": "web_search"}]}),
    ("anthropic:claude-x", {"tools": [{"type": "web_fetch_20250910", "name": "web_fetch"}]}),
    ("anthropic:claude-x", {"tools": [{"type": "code_execution_20250522", "name": "code_execution"}]}),
    ("openai:gpt-x", {"tools": [{"type": "web_search_preview"}]}),
    ("openai:gpt-x", {"web_search_options": {"search_context_size": "low"}}),
    ("openai:gpt-4o-search-preview", {}),                               # a model that searches by itself
    ("openai:gpt-x", {"tools": [{"type": "mcp", "server_url": "https://example.invalid/mcp"}]}),
    ("openai:gpt-x", {"tools": [{"type": "file_search"}]}),
])
def test_server_side_tools_are_refused_by_default_before_anything_is_sent(server, make_session, model, params):
    base, seen, _ = server
    s = make_session()
    with pytest.raises(GatewayRuleError) as ei:
        s.model(model, messages=MSGS, api_key=KEY, base_url=base + ("" if model.startswith("anthropic") else "/v1"), **params)
    assert ei.value.rule == "server_tools_off" and "outside the AsOf tool proxy" in str(ei.value)
    assert seen == []                                                       # nothing reached the provider
    err = events(s, "model_error")
    assert len(err) == 1 and err[0].data()["gateway"]["blocked"] is True and err[0].data()["gateway"]["rule"] == "server_tools_off"
    assert s.clean and s.flags == []                                        # a refused attempt is recorded, and it does not make the run unclean: nothing got in


def test_a_truthy_search_parameter_is_refused_and_an_empty_or_false_one_is_not_a_request(server, make_session):
    base, seen, _ = server
    s = make_session()
    with pytest.raises(GatewayRuleError):
        s.model("openai:gpt-x", messages=MSGS, api_key=KEY, base_url=base + "/v1", web_search_options={"search_context_size": "low"})
    s.model("openai:gpt-x", messages=MSGS, api_key=KEY, base_url=base + "/v1", web_search_options=None, enable_search=False)
    assert len(seen) == 1 and s.clean


def test_plain_function_tools_and_caller_run_typed_tools_are_not_server_side(server, make_session):
    base, seen, _ = server
    s = make_session()
    fn = {"type": "function", "function": {"name": "news_search", "parameters": {"type": "object"}}}
    s.model("openai:gpt-x", messages=MSGS, api_key=KEY, base_url=base + "/v1", tools=[fn])
    s.model("anthropic:claude-x", messages=MSGS, api_key=KEY, base_url=base, tools=[{"name": "news_search", "input_schema": {"type": "object"}}, {"type": "custom", "name": "x", "input_schema": {}},
                                                                                       {"type": "bash_20250124", "name": "bash"}, {"type": "text_editor_20250124", "name": "str_replace_editor"}])
    assert len(seen) == 2 and s.clean
    assert R.requested_server_tools("claude-x", {"tools": [{"type": "web_search_20250305"}, {"type": "bash_20250124"}]}) == ["web_search_20250305"]


def test_allowed_server_tools_flag_the_call_and_the_run_can_no_longer_be_called_clean(server, make_session):
    base, seen, _ = server
    s = make_session(gateway=ModelGateway(GatewayPolicy(allow_server_tools=True)))
    s.model("anthropic:claude-x", messages=MSGS, api_key=KEY, base_url=base, tools=[{"type": "web_search_20250305", "name": "web_search"}])
    assert len(seen) == 1 and not s.clean
    f = s.flags
    assert len(f) == 1 and f[0]["kind"] == "SERVER_TOOL" and f[0]["requested"] == ["web_search_20250305"] and f[0]["unrequested"] is False
    r = s.model_receipt()
    assert r["server_tools_used"] == ["web_search_20250305"] and r["label"] == "FLAGGED" and "FLAGGED" in r["labels"]
    assert "cannot be called clean" in "; ".join(s.receipt().warnings)       # the per-call receipt says it too


def test_a_server_tool_the_provider_reports_using_is_flagged_even_if_none_was_requested(server, make_session):
    base, _, respond = server
    respond[0] = lambda path, body: {"model": "claude-served-1", "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1, "server_tool_use": {"web_search_requests": 2}},
                                     "content": [{"type": "server_tool_use", "name": "web_search", "input": {}}, {"type": "web_search_tool_result", "content": []}, {"type": "text", "text": "done"}]}
    s = make_session()
    s.model("anthropic:claude-x", messages=MSGS, api_key=KEY, base_url=base)
    f = s.flags
    assert not s.clean and f[0]["unrequested"] is True and f[0]["reported_by_provider"] == ["web_search"]
    assert s.model_receipt()["server_tools_used"] == ["web_search"]


def test_openai_url_citations_and_responses_style_search_calls_are_read_as_server_tool_use():
    assert R.server_tools_used({"choices": [{"message": {"content": "x", "annotations": [{"type": "url_citation", "url": "https://example.invalid"}]}}]}) == ["web_search"]
    assert R.server_tools_used({"output": [{"type": "web_search_call"}, {"type": "function_call", "name": "f"}, {"type": "message"}]}) == ["web_search"]
    assert R.server_tools_used({"choices": [{"message": {"content": "plain"}}], "usage": {"prompt_tokens": 1}}) == []


# ---------------------------------------------------------------- contributor models
def test_a_contributor_model_is_blocked_and_the_attempt_is_logged(server, make_session):
    base, seen, _ = server
    s = make_session()
    with pytest.raises(GatewayRuleError) as ei:
        s.model("openai:gpt-9-Contributor", messages=MSGS, api_key=KEY, base_url=base + "/v1")
    assert ei.value.rule == "contributor_model" and seen == []
    g = events(s, "model_error")[0].data()
    assert g["model"] == "openai:gpt-9-Contributor" and g["gateway"]["rule"] == "contributor_model" and g["gateway"]["blocked"] is True
    assert R.is_contributor("meta:muse-contributor-1") and R.is_contributor("CONTRIBUTOR") and not R.is_contributor("muse-spark-1.3")


def test_an_explicit_allow_lets_a_contributor_model_through_flagged_as_a_policy_exception(server, make_session):
    base, seen, _ = server
    s = make_session(gateway=ModelGateway(GatewayPolicy(allow_contributor_models=True)))
    s.model("openai:gpt-contributor-1", messages=MSGS, api_key=KEY, base_url=base + "/v1")
    assert len(seen) == 1 and not s.clean and s.flags[0] == {"kind": "POLICY", "rule": "contributor_model_allowed", "call_id": s.flags[0]["call_id"], "model": "openai:gpt-contributor-1",
                                                           "clock_T": s.flags[0]["clock_T"]}


# ---------------------------------------------------------------- provider-side conversation state
@pytest.mark.parametrize("params", [{"previous_response_id": "resp_1"}, {"conversation": "conv_1"}, {"thread_id": "t1"}, {"cached_content": "cachedContents/abc"}, {"store": True}])
def test_requests_that_rely_on_provider_state_are_refused(server, make_session, params):
    base, seen, _ = server
    s = make_session()
    with pytest.raises(GatewayRuleError) as ei:
        s.model("openai:gpt-x", messages=MSGS, api_key=KEY, base_url=base + "/v1", **params)
    assert ei.value.rule == "provider_state" and seen == []


def test_the_full_conversation_is_sent_every_time_and_store_false_is_fine(server, make_session):
    base, seen, _ = server
    s = make_session()
    convo = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    s.model("openai:gpt-x", messages=convo, api_key=KEY, base_url=base + "/v1", store=False)
    assert seen[0]["body"]["messages"] == convo and seen[0]["body"]["store"] is False
    g = events(s, "model_call")[0].data()["gateway"]
    assert g["n_messages_sent"] == 3 and g["server_state"] == "none"


# ---------------------------------------------------------------- tool calls go through the proxy
def test_tool_calls_the_model_asks_for_are_run_only_through_the_asof_proxy(server, make_session):
    base, _, respond = server
    respond[0] = lambda path, body: {"model": "gpt-served-1", "usage": {}, "choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "news_search", "arguments": json.dumps({"query": "acme"})}},
        {"id": "c2", "type": "function", "function": {"name": "not_registered", "arguments": "{}"}}]}}]}
    s = make_session()
    s.add_tool("news_search", lambda query: [{"title": "old", "published_at": "2023-01-01T00:00:00Z"}, {"title": "from the future", "published_at": "2023-03-01T00:00:00Z"}], timestamp_field="published_at")
    resp = s.model("openai:gpt-x", messages=MSGS, api_key=KEY, base_url=base + "/v1")
    assert [c["name"] for c in resp.tool_calls] == ["news_search", "not_registered"] and resp.tool_calls[0]["arguments"] == {"query": "acme"}
    out = s.run_tool_calls(resp)
    assert [x["title"] for x in out[0]["result"]] == ["old"]                 # the future item was withheld: the call went through the time-locked proxy
    assert "error" in out[1] and "not_registered" in out[1]["error"]         # an unknown tool is refused, not executed
    tc = events(s, "tool_call")
    assert len(tc) == 1 and tc[0].data()["tool"] == "news_search" and s.receipt().blocked["future"] == 1
    assert events(s, "model_call")[0].data()["gateway"]["tool_calls_requested"] == 2


def test_anthropic_tool_use_blocks_are_read_as_caller_tool_calls():
    calls = R.client_tool_calls({"content": [{"type": "text", "text": "x"}, {"type": "tool_use", "id": "t1", "name": "news_search", "input": {"query": "q"}}]})
    assert calls == [{"id": "t1", "name": "news_search", "arguments": {"query": "q"}}]
    assert R.client_tool_calls({"choices": [{"message": {"tool_calls": [{"id": "x", "type": "function", "function": {"name": "f", "arguments": "not json"}}]}}]})[0]["arguments"] == {"_unparsed": "not json"}


# ---------------------------------------------------------------- pins, clock, tokens, reasoning
def test_a_version_pin_that_the_provider_does_not_honour_flags_the_call(server, make_session):
    base, _, _ = server
    ok = make_session(gateway=ModelGateway(GatewayPolicy(pins={"openai:gpt-x": "gpt-served"})))
    ok.model("openai:gpt-x", messages=MSGS, api_key=KEY, base_url=base + "/v1")
    assert ok.clean
    bad = make_session(gateway=ModelGateway(GatewayPolicy(pins={"openai:gpt-x": "gpt-pinned-2024"})))
    bad.model("openai:gpt-x", messages=MSGS, api_key=KEY, base_url=base + "/v1")
    f = bad.flags[0]
    assert f["kind"] == "POLICY" and f["rule"] == "version_pin" and f["pinned"] == "gpt-pinned-2024" and f["reported"] == "gpt-served-1"
    assert events(bad, "model_call")[0].data()["gateway"]["pinned_version"] == "gpt-pinned-2024"


def test_every_call_is_stamped_with_the_session_clock_and_the_stamp_moves_with_it(server, make_session):
    base, _, _ = server
    s = make_session("2023-01-21T14:00:00Z")
    s.model("openai:gpt-x", messages=MSGS, api_key=KEY, base_url=base + "/v1")
    s.decision("hold", why="x")
    s.advance_to("2023-02-01T09:30:00Z")
    s.model("openai:gpt-x", messages=MSGS, api_key=KEY, base_url=base + "/v1")
    stamps = [e.data()["gateway"]["clock_T"] for e in events(s, "model_call")]
    assert stamps == ["2023-01-21T14:00:00Z", "2023-02-01T09:30:00Z"]


def test_tokens_have_one_shape_for_every_provider_and_unknown_is_none_not_zero():
    assert R.normalise_usage({"input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 5}) == {"input": 11, "output": 7, "cached": 5, "reasoning": None}
    assert R.normalise_usage({"prompt_tokens": 9, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 2}, "completion_tokens_details": {"reasoning_tokens": 3}}) == {
        "input": 9, "output": 4, "cached": 2, "reasoning": 3}
    assert R.normalise_usage({}) == {"input": None, "output": None, "cached": None, "reasoning": None}
    assert R.normalise_usage(None)["input"] is None and R.normalise_usage({"prompt_tokens": True})["input"] is None


def test_reasoning_effort_is_read_from_each_providers_form():
    assert R.reasoning_effort({"reasoning_effort": "high"}) == "high"
    assert R.reasoning_effort({"reasoning": {"effort": "low"}}) == "low"
    assert R.reasoning_effort({"thinking": {"type": "enabled", "budget_tokens": 2048}}) == "budget:2048"
    assert R.reasoning_effort({"temperature": 0}) is None


# ---------------------------------------------------------------- Meta: configuration only, UNVERIFIED
def test_meta_is_an_openai_compatible_provider_from_configuration_and_is_marked_unverified(server, make_session, monkeypatch):
    base, seen, _ = server
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    monkeypatch.setenv("META_API_BASE_URL", base + "/v1")                    # the operator's base URL (the default is https://api.meta.ai/v1)
    s = make_session()
    r = s.model("meta:muse-spark-1.3", messages=MSGS)
    assert r.text == "openai says hi" and seen[0]["path"] == "/v1/chat/completions"
    assert seen[0]["headers"]["authorization"] == f"Bearer {KEY}" and seen[0]["body"]["model"] == "muse-spark-1.3"
    g = events(s, "model_call")[0].data()
    assert g["provider"] == "meta" and g["gateway"]["provider_verified"] is False
    assert s.model_receipt()["provider_verified"] is False
    assert KEY not in json.dumps([e.payload for e in s.ledger.events()])


def test_the_meta_default_endpoint_is_the_configured_one_and_a_missing_key_is_a_clear_error(make_session, monkeypatch):
    from asof.gateway import META_SPEC
    assert META_SPEC.base_url == "https://api.meta.ai/v1" and META_SPEC.key_env == "MODEL_API_KEY" and META_SPEC.verified is False and "UNVERIFIED" in META_SPEC.note
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    s = make_session()
    with pytest.raises(GatewayError, match="MODEL_API_KEY"):
        s.model("meta:muse-spark-1.3", messages=MSGS, base_url="http://127.0.0.1:9/v1")          # no key: refused before any connection


def test_adding_a_provider_is_configuration_not_code(server, make_session, monkeypatch):
    base, seen, _ = server
    monkeypatch.setenv("ACME_KEY", KEY)
    gw = ModelGateway()
    gw.register_openai_compatible("acme", ProviderSpec(base + "/v1", "ACME_KEY"))
    s = make_session(gateway=gw)
    assert "acme" in gw.providers() and gw.provider_verified("acme") is False
    s.model("acme:model-1", messages=MSGS)
    assert seen[0]["headers"]["authorization"] == f"Bearer {KEY}"
    assert gw.provider_verified("anthropic") is False and gw.provider_verified("openai") is False          # nothing here has been called live


# ---------------------------------------------------------------- the flat receipt
RECEIPT_FIELDS = {"run_id", "call_id", "clock_T", "provider", "model", "model_version", "reasoning_effort", "settings", "prompt_hash", "output_hash", "tokens", "server_tools_used",
                  "items_served", "items_blocked", "labels", "label", "flags", "provider_verified", "data_quality_summary", "prev_hash", "hash"}


def _chat(s, base):
    s.add_source(ListSource("d", [rec("a", "2023-01-01T00:00:00Z"), rec("b", "2023-01-02T00:00:00Z"), rec("later", "2023-06-01T00:00:00Z")]))
    s.query("d")
    return s.model("openai:gpt-x", messages=MSGS, api_key=KEY, base_url=base + "/v1", temperature=0.2, reasoning_effort="medium")


def test_the_model_receipt_has_every_field_and_links_into_the_chain(server, make_session):
    base, _, _ = server
    s = make_session()
    _chat(s, base)
    r = s.model_receipt()
    assert RECEIPT_FIELDS <= set(r) and r["run_id"] == s.id and r["provider"] == "openai" and r["model"] == "gpt-x" and r["model_version"] == "gpt-served-1"
    assert r["clock_T"] == "2023-01-21T14:00:00Z" and r["reasoning_effort"] == "medium" and r["settings"] == {"temperature": 0.2, "reasoning_effort": "medium"}
    assert r["tokens"] == {"input": 9, "output": 4, "cached": 2, "reasoning": 3} and r["server_tools_used"] == []
    assert r["items_served"] == 2 and r["items_blocked"] == 1                  # two served before the call, the future record blocked
    assert r["labels"] == ["UNKNOWN"] and r["label"] == "UNKNOWN" and r["flags"] == [] and r["provider_verified"] is False
    assert r["data_quality_summary"] is not None and "d" in r["data_quality_summary"]
    mc = events(s, "model_call")[0]
    allev = s.ledger.events(session_id=s.id)
    assert r["hash"] == mc.hash and r["prev_hash"] == mc.prev_hash == allev[mc.seq - 2].hash          # prev_hash is the hash of the event before it in the chain
    assert r["prompt_hash"] == mc.data()["prompt_sha256"] and r["output_hash"] == mc.data()["response_sha256"]
    assert "what is the close" not in json.dumps(r) and "openai says hi" not in json.dumps(r)            # hashes and counts, never the text


def test_in_hashes_only_mode_the_receipt_and_ledger_hold_no_prompt_text_or_parameter_values(server, make_session):
    base, _, _ = server
    s = make_session(privacy="hashes_only")
    _chat(s, base)
    r = s.model_receipt()
    assert set(r["settings"]) == {"params_sha256", "keys"} and r["settings"]["keys"] == ["reasoning_effort", "temperature"] and "0.2" not in json.dumps(r["settings"])
    mc = events(s, "model_call")[0].data()
    assert "messages" not in mc and "response_text" not in mc and "params" not in mc and "gateway" in mc
    assert "what is the close" not in json.dumps([e.payload for e in s.ledger.events()])
    assert r["tokens"]["input"] == 9 and r["reasoning_effort"] == "medium"                                # metadata survives


def _leaves(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from _leaves(v)
    elif isinstance(x, list):
        for v in x:
            yield from _leaves(v)
    else:
        yield x


def test_the_gateway_block_is_metadata_only_so_it_is_safe_in_a_hashes_only_sync(server, make_session):
    base, _, _ = server
    s = make_session(privacy="hashes_only", gateway=ModelGateway(GatewayPolicy(allow_server_tools=True)))
    s.model("anthropic:claude-x", messages=MSGS, api_key=KEY, base_url=base, tools=[{"type": "web_search_20250305", "name": "web_search"}])
    g = events(s, "model_call")[0].data()["gateway"]
    leaves = list(_leaves(g))
    assert all(v is None or isinstance(v, (bool, int, str)) for v in leaves) and all(len(v) <= 64 for v in leaves if isinstance(v, str))
    assert KEY not in json.dumps(g)


def test_a_model_receipt_for_an_unknown_call_or_a_session_with_no_model_call_is_an_error(make_session):
    s = make_session()
    with pytest.raises(AsOfError, match="no model call"):
        s.model_receipt()
    with pytest.raises(AsOfError, match="no model call"):
        s.model_receipt("nope")
