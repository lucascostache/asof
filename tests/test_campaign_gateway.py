"""Pre-launch campaign, area E, the parts that need no key: provider failures against local stand-ins, streaming, and passing a model's turn back.

The live runs (a real model through the gateway and the MCP tools, real provider errors, a real provider-side tool) are scripts/live_test.py and scripts/live_agent_loop.py;
docs/LIVE_TEST.md records what they showed. What could NOT sensibly be done live is here: a request too large (sending megabytes to find a provider's limit costs money if it is
accepted), rate limiting and overload (cannot be produced on demand), and a connection that drops part-way.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from asof import GatewayError, GatewayRuleError, Session

ROOT = Path(__file__).resolve().parent.parent
KEY = "sk-" + "ant-GATEWAYCAMPAIGN0123456789abcdef"
MSGS = [{"role": "user", "content": "hello"}]


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    seen = []
    mode = {"status": 200, "body": {}, "headers": {}, "raw": None}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers["Content-Length"])
            body = self.rfile.read(n)
            seen.append({"bytes": n, "json": json.loads(body), "headers": {k.lower(): v for k, v in self.headers.items()}})
            raw = mode["raw"] if mode["raw"] is not None else json.dumps(mode["body"]).encode()
            self.send_response(mode["status"])
            for k, v in mode["headers"].items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen, mode
    srv.shutdown()


def tcp(behaviour):
    """A server that is not a well-behaved HTTP server: it accepts one connection and then does `behaviour(conn)`."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(4)

    def serve():
        while True:
            try:
                conn, _ = s.accept()
            except OSError:
                return
            try:
                behaviour(conn)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{s.getsockname()[1]}", s


ERRORS = [
    (413, {"type": "error", "error": {"type": "request_too_large", "message": "Request exceeds the maximum allowed number of bytes."}}, {}),
    (400, {"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long: 1203441 tokens > 1000000 maximum"}}, {}),
    (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "This request would exceed your organization's rate limit."}}, {"retry-after": "17"}),
    (529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, {}),
    (500, {"type": "error", "error": {"type": "api_error", "message": "Internal server error"}}, {}),
    (502, None, {"Content-Type": "text/html"}),
    (403, {"type": "error", "error": {"type": "permission_error", "message": "Your API key does not have permission to use the specified resource."}}, {}),
]


@pytest.mark.parametrize("status, body, headers", ERRORS, ids=[f"{s} {(b or {}).get('error', {}).get('type', 'an HTML error page')}" for s, b, _ in ERRORS])
def test_each_kind_of_provider_failure_is_one_clean_gateway_error_one_ledger_event_and_no_retry(http, make_session, ledger_path, status, body, headers):
    base, seen, mode = http
    mode.update(status=status, body=body or {}, headers=headers, raw=None if body else b"<html><body><h1>502 Bad Gateway</h1></body></html>")
    s = make_session()
    with pytest.raises(GatewayError) as e:
        s.model("anthropic:m", messages=MSGS, api_key=KEY, base_url=base)
    msg = str(e.value)
    assert f"HTTP {status}" in msg and KEY not in msg
    if body:
        assert body["error"]["type"] in msg and body["error"]["message"] in msg                            # the provider's own words reach the caller
    if status == 429:
        assert "retry after 17 seconds" in msg                                                             # and when it says how long to wait, so does the error
    else:
        assert "retry after" not in msg
    assert len(seen) == 1                                                                                  # the gateway does not retry by itself: a retry is a second model call, and the caller's decision
    ev = s.events("model_error")
    assert len(ev) == 1 and ev[0].data()["error_type"] == "GatewayError" and f"HTTP {status}" in ev[0].data()["error"]
    assert not s.events("model_call") and s.verify_ledger().ok
    assert KEY.encode() not in ledger_path.read_bytes()


def test_a_request_too_large_is_refused_by_the_provider_cleanly_and_its_megabytes_do_not_go_into_the_ledger(http, make_session, ledger_path):
    base, seen, mode = http
    mode.update(status=413, body={"type": "error", "error": {"type": "request_too_large", "message": "Request exceeds the maximum size"}})
    big = "ACME-SYN quarterly notes. " * 200_000                                                           # about 5 MB of prompt
    s = make_session()
    with pytest.raises(GatewayError, match="HTTP 413"):
        s.model("anthropic:m", messages=[{"role": "user", "content": big}], api_key=KEY, base_url=base)
    assert seen[0]["bytes"] > 5_000_000                                                                    # it really was sent
    s.close()
    assert ledger_path.stat().st_size < 200_000                                                            # the failed call is recorded by the HASH of its prompt, not the prompt
    assert "ACME-SYN quarterly notes. ACME-SYN" not in ledger_path.read_bytes().decode("latin-1")


def test_a_connection_that_drops_or_stalls_is_a_gateway_error_not_a_crash(make_session, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")

    def read_request(conn):
        conn.settimeout(5)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk

    def hang_up(conn):                                                                                     # reads the request, then closes without a word
        read_request(conn)

    def half_a_reply(conn):                                                                                # promises 5,000 bytes, sends 20, closes
        read_request(conn)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 5000\r\n\r\n{\"model\": \"m\", \"cont")

    def not_http(conn):                                                                                    # answers with something that is not HTTP at all
        read_request(conn)
        conn.sendall(b"\x00\x01\x02 this is not http\r\n\r\n")

    def stall(conn):                                                                                       # accepts, then says nothing for longer than the caller will wait
        read_request(conn)
        threading.Event().wait(3)

    s = make_session()
    servers = []
    try:
        for name, behaviour, kw in (("hang up", hang_up, {}), ("half a reply", half_a_reply, {}), ("not HTTP", not_http, {}), ("stall", stall, {"timeout": 0.5})):
            base, sock = tcp(behaviour)
            servers.append(sock)
            n = len(s.events("model_error"))
            with pytest.raises(GatewayError) as e:
                s.model("anthropic:m", messages=MSGS, api_key=KEY, base_url=base, **kw)
            assert KEY not in str(e.value), name
            assert len(s.events("model_error")) == n + 1, name                                             # each one is in the ledger
        sock_closed = socket.socket()
        sock_closed.bind(("127.0.0.1", 0))
        port = sock_closed.getsockname()[1]
        sock_closed.close()                                                                                # nothing is listening here at all
        with pytest.raises(GatewayError, match="could not reach the provider"):
            s.model("anthropic:m", messages=MSGS, api_key=KEY, base_url=f"http://127.0.0.1:{port}")
        assert s.verify_ledger().ok and not s.events("model_call")
    finally:
        for sock in servers:
            sock.close()


def test_a_200_that_is_not_json_or_not_the_expected_shape_is_handled(http, make_session):
    base, seen, mode = http
    s = make_session()
    mode.update(status=200, raw=b"<html>maintenance</html>")
    with pytest.raises(GatewayError, match="not JSON"):
        s.model("anthropic:m", messages=MSGS, api_key=KEY, base_url=base)
    for odd in (b"[]", b"null", b'"a string"', b"42", b'{"content": "not a list"}', b'{"content": [1, 2, null]}', b'{"content": [{"type": "text"}], "usage": "none"}', b"{}"):
        mode.update(status=200, raw=odd)
        try:
            r = s.model("anthropic:m", messages=MSGS, api_key=KEY, base_url=base)
            assert isinstance(r.text, str)                                                                 # an empty answer is an answer
        except GatewayError:
            pass                                                                                           # or a clean refusal; never anything else
    assert s.verify_ledger().ok


def test_streaming_is_refused_before_anything_is_sent(http, make_session):
    """The gateway hashes one whole response per call for the receipt. A streamed reply would be sent (and billed) and then fail to parse, so it is refused first."""
    base, seen, mode = http
    s = make_session()
    for value in (True, 1, "true"):
        with pytest.raises(GatewayRuleError) as e:
            s.model("anthropic:m", messages=MSGS, api_key=KEY, base_url=base, stream=value)
        assert e.value.rule == "streaming_unsupported"
    assert seen == []                                                                                      # nothing left the process
    ev = [x.data() for x in s.events("model_error")]
    assert len(ev) == 3 and all(x["gateway"]["blocked"] is True and x["gateway"]["rule"] == "streaming_unsupported" for x in ev)
    mode.update(status=200, body={"model": "m-1", "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn", "usage": {}})
    assert s.model("anthropic:m", messages=MSGS, api_key=KEY, base_url=base, stream=False).text == "hi" and len(seen) == 1      # stream=False is just a normal call


def test_the_providers_own_turn_is_returned_unchanged_so_a_tool_loop_can_pass_it_back(http, make_session):
    """Seen live: a model that thinks before calling a tool returns a `thinking` block (its text empty, only a signature) before the `tool_use` block, and asks for it back with the
    tool result. `text` and `tool_calls` alone cannot rebuild that turn; `raw_message` is the turn itself."""
    base, seen, mode = http
    signature = "sk-" + "looksLikeAKeyButIsASignature0123456789" + "A" * 300                             # opaque provider bytes: must survive even if they resemble a credential
    content = [{"type": "thinking", "thinking": "", "signature": signature}, {"type": "text", "text": "Looking that up."},
               {"type": "tool_use", "id": "toolu_01", "name": "query", "input": {"source": "news", "filters": None}}]
    mode.update(status=200, body={"model": "m-1", "content": content, "stop_reason": "tool_use", "usage": {"input_tokens": 5, "output_tokens": 9}})
    s = make_session()
    r = s.model("anthropic:m", messages=MSGS, api_key=KEY, base_url=base, tools=[{"name": "query", "description": "q", "input_schema": {"type": "object"}}])
    assert r.raw_message == {"role": "assistant", "content": content}                                      # byte for byte
    assert r.text == "Looking that up." and [c["name"] for c in r.tool_calls] == ["query"] and r.tool_calls[0]["id"] == "toolu_01"
    follow_up = [*MSGS, dict(r.raw_message), {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": "3 headlines"}]}]
    mode.update(body={"model": "m-1", "content": [{"type": "text", "text": "Three headlines."}], "stop_reason": "end_turn", "usage": {}})
    assert s.model("anthropic:m", messages=follow_up, api_key=KEY, base_url=base).text == "Three headlines."
    assert seen[1]["json"]["messages"][1] == {"role": "assistant", "content": content}                    # what went back to the provider is what it sent
    mode.update(body={"model": "g-1", "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "query", "arguments": "{}"}}]},
                                                   "finish_reason": "tool_calls"}], "usage": {}})
    o = s.model("openai:g", messages=MSGS, api_key=KEY, base_url=base + "/v1")
    assert o.raw_message == {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "query", "arguments": "{}"}}]}
    s.register_provider("mine", lambda req: __import__("asof").ModelResponse("plain"))
    assert s.model("mine:m", messages=MSGS).raw_message is None                                            # a custom provider that does not supply one: None, and `text` still works


@pytest.mark.parametrize("script", ["live_test.py", "live_agent_loop.py"])
def test_the_live_scripts_send_nothing_without_a_key(script, tmp_path):
    env = {"PYTHONPATH": str(ROOT), "PATH": ""}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--out", str(tmp_path / "o")], capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 2 and "ANTHROPIC_API_KEY is not set. Nothing was sent." in r.stderr and not (tmp_path / "o").exists()
