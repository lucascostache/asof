"""The model gateway, against a real local HTTP server (no network, no real keys)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from asof import GatewayError, ModelResponse, Session

KEY = "sk-" + "test-SECRETKEY0123456789"          # a fake key, written in two parts so the source holds no key-shaped string (secret scanners, push protection)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    seen = []
    mode = {"status": 200}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            if mode["status"] != 200:
                self.send_response(mode["status"]); self.end_headers()
                self.wfile.write(json.dumps({"error": {"message": mode.get("error_message", f"bad key {KEY}")}}).encode()); return
            if self.path.endswith("/v1/messages"):
                out = {"model": "claude-served-1", "content": [{"type": "text", "text": "anthropic says hi"}], "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 4}}
            else:
                out = {"model": "gpt-served-1", "choices": [{"message": {"content": "openai says hi"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3}}
            raw = json.dumps(out).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen, mode
    srv.shutdown()


MSGS = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hello"}]


def test_openai_compatible_request_shape_and_response(server, make_session):
    base, seen, _ = server
    s = make_session()
    r = s.model("openai:gpt-x", messages=MSGS, api_key=KEY, base_url=base + "/v1", temperature=0.2)
    assert r.text == "openai says hi" and r.model_version == "gpt-served-1" and r.finish_reason == "stop"
    req = seen[0]
    assert req["path"] == "/v1/chat/completions" and req["headers"]["authorization"] == f"Bearer {KEY}"
    assert req["body"] == {"model": "gpt-x", "temperature": 0.2, "messages": MSGS}                # api_key/base_url are not forwarded in the body


def test_anthropic_request_shape_system_split_and_default_max_tokens(server, make_session):
    base, seen, _ = server
    s = make_session()
    r = s.model("anthropic:claude-x", messages=MSGS, api_key=KEY, base_url=base)
    assert r.text == "anthropic says hi" and r.usage == {"input_tokens": 3, "output_tokens": 4}
    req = seen[0]
    assert req["path"] == "/v1/messages" and req["headers"]["x-api-key"] == KEY and req["headers"]["anthropic-version"] == "2023-06-01"
    assert req["body"] == {"model": "claude-x", "max_tokens": 1024, "system": "be brief", "messages": [{"role": "user", "content": "hello"}]}


def test_local_provider_needs_no_key_and_uses_base_url(server, make_session):
    base, seen, _ = server
    r = make_session().model("local:llama3", messages=MSGS[1:], base_url=base + "/v1")
    assert r.text == "openai says hi" and "authorization" not in seen[0]["headers"]


def test_keys_come_from_the_environment_and_a_missing_key_is_a_clear_error(server, make_session, monkeypatch):
    base, seen, _ = server
    s = make_session()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(GatewayError, match="OPENAI_API_KEY"):
        s.model("openai:gpt-x", messages=MSGS, base_url=base + "/v1")
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    s.model("openai:gpt-x", messages=MSGS, base_url=base + "/v1")
    assert seen[-1]["headers"]["authorization"] == f"Bearer {KEY}"


def test_the_key_never_reaches_the_ledger_a_receipt_or_an_error_message(server, make_session, ledger_path):
    base, seen, mode = server
    s = make_session()
    s.model("openai:gpt-x", messages=MSGS, api_key=KEY, base_url=base + "/v1")
    mode["status"] = 401                                                                        # the provider echoes the key back in its error body
    with pytest.raises(GatewayError) as e:
        s.model("openai:gpt-x", messages=MSGS, api_key=KEY, base_url=base + "/v1")
    assert KEY not in str(e.value) and "401" in str(e.value)
    s.close()
    assert KEY not in ledger_path.read_bytes().decode("latin-1")
    assert all(KEY not in r.to_json() for r in s.receipt(last=False))
    assert s.events("model_error") and "SECRETKEY" not in str(s.events("model_error")[0].payload)


def test_a_providers_masked_echo_of_the_key_is_removed_too(server, make_session, ledger_path):
    """Seen against the live OpenAI endpoint (with a string that was not a key): its 401 says "Incorrect API key provided: <first 8 characters>*****<last 4 characters>".
    The ends are real characters of the key. They must not reach the error message or the ledger, in the default (full) privacy mode."""
    base, _, mode = server
    masked = KEY[:8] + "*" * (len(KEY) - 12) + KEY[-4:]
    assert masked == "sk-test-***************6789" and KEY not in masked
    mode["status"] = 401
    mode["error_message"] = f"Incorrect API key provided: {masked}. You can find your API key at https://platform.openai.com/account/api-keys."
    s = make_session()
    with pytest.raises(GatewayError) as e:
        s.model("openai:gpt-x", messages=MSGS, api_key=KEY, base_url=base + "/v1")
    msg = str(e.value)
    assert "401" in msg and "Incorrect API key provided: [REDACTED]. You can find your API key" in msg       # the sentence survives, the token does not
    for fragment in (masked, KEY[-4:], KEY[:8], "****"):
        assert fragment not in msg, fragment
    s.close()
    on_disk = ledger_path.read_bytes().decode("latin-1")
    assert "Incorrect API key provided" in on_disk                                                           # the error really is in the ledger (full mode)...
    for fragment in (masked, KEY[-4:] + ".", "****"):
        assert fragment not in on_disk, fragment                                                             # ...without the key's ends
    mode["error_message"] = "rate limited: retry in 20s (2 ** 3 attempts, see **docs**)"                    # ordinary text with asterisks is left alone
    with pytest.raises(GatewayError, match=r"2 \*\* 3 attempts, see \*\*docs\*\*"):
        make_session().model("openai:gpt-x", messages=MSGS, api_key=KEY, base_url=base + "/v1")


def test_unreachable_provider_and_bad_arguments_are_gateway_errors(make_session):
    s = make_session()
    with pytest.raises(GatewayError, match="could not reach"):
        s.model("local:x", messages=MSGS, base_url="http://127.0.0.1:9", timeout=2)
    with pytest.raises(GatewayError, match="provider:name"):
        s.model("noprovider", messages=MSGS)
    with pytest.raises(GatewayError, match="unknown provider"):
        s.model("mystery:x", messages=MSGS)
    with pytest.raises(GatewayError, match="messages"):
        s.model("local:x", messages=[])
    with pytest.raises(GatewayError, match="no model"):
        s.model(messages=MSGS)


def test_a_default_model_and_a_custom_provider(make_session):
    s = make_session(model="fake:m")
    s.register_provider("fake", lambda req: ModelResponse(f"echo:{req.messages[-1]['content']}:{sorted(req.params)}"))
    assert s.model(messages=[{"role": "user", "content": "yo"}], temperature=1).text == "echo:yo:['temperature']"
    ev = s.events("model_call")[0].data()
    assert len(ev["prompt_sha256"]) == 64 and ev["response_text"].startswith("echo:") and ev["messages"][0]["content"] == "yo"


def test_prompt_and_response_hashes_are_stable_and_content_dependent(make_session):
    s = make_session()
    s.register_provider("fake", lambda req: ModelResponse(req.messages[-1]["content"].upper()))
    for text in ("a", "a", "b"):
        s.model("fake:m", messages=[{"role": "user", "content": text}])
    p = [e.data()["prompt_sha256"] for e in s.events("model_call")]
    r = [e.data()["response_sha256"] for e in s.events("model_call")]
    assert p[0] == p[1] != p[2] and r[0] == r[1] != r[2]
