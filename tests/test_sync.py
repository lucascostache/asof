"""B4 + D: sync is opt-in, respects privacy, and never sends or logs credentials. Tested against local mock servers (http and https)."""
import hashlib
import json
import logging
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from asof import Ledger, ModelResponse, Session
from asof.sync import SCHEMA, SyncConfig, SyncDisabledError, SyncError, Syncer

from conftest import ListSource, rec

SYNC_KEY = "synckey-SECRET-0123456789abcdef"
MODEL_KEY = "sk-" + "ant-SYNCMODELKEY0123456789abcd"          # a fake key, written in two parts so the source holds no key-shaped string (secret scanners, push protection)
CANARIES = ["CANARY-PAYLOAD-ACME-SYN-ALPHA", "CANARY-FILTER-STRATEGY", "CANARY-TOOL-ARG", "CANARY-TOOL-RESULT", "CANARY-PROMPT-TEXT", "CANARY-RESPONSE-TEXT",
            "CANARY-SYSTEM-PARAM", "CANARY-RECORD-ID", "CANARY-DECISION-DETAIL", "CANARY-ERROR-TEXT"]


class Mock:
    """A tiny receiving endpoint. ``mode``: ok | flaky (500 first) | deny (401) | partial (accept only n) | badjson."""
    def __init__(self, tls=None):
        self.requests, self.mode, self.partial = [], "ok", 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "raw": body, "json": json.loads(body)})
                if outer.mode == "deny":
                    return self._reply(401, {"error": f"bad key {SYNC_KEY}"})
                if outer.mode == "flaky" and len(outer.requests) == 1:
                    return self._reply(503, {"error": "try again"})
                if outer.mode == "badjson":
                    return self._reply(200, "<html>")
                evs = outer.requests[-1]["json"]["events"]
                through = evs[-1]["seq"] if outer.mode != "partial" else evs[min(outer.partial, len(evs)) - 1]["seq"]
                self._reply(200, {"accepted_through": through})

            def _reply(self, code, obj):
                raw = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*tls)
            self.srv.socket = ctx.wrap_socket(self.srv.socket, server_side=True)
        self.scheme = "https" if tls else "http"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"{self.scheme}://127.0.0.1:{self.srv.server_port}/ingest"

    def all_bytes(self):
        return b"".join(r["raw"] for r in self.requests)

    def events(self):
        return [e for r in self.requests for e in r["json"]["events"]]


@pytest.fixture
def mock():
    m = Mock()
    yield m
    m.srv.shutdown()


def cfg(m, **kw):
    kw = {"endpoint": m.url, "api_key": SYNC_KEY, "enabled": True, "allow_insecure_localhost": True, **kw}
    return SyncConfig(**kw)


def run_everything(path, privacy):
    s = Session("2023-01-21T14:00:00Z", ledger_path=path, privacy=privacy)
    s.add_source(ListSource("d", [rec("CANARY-RECORD-ID", "2023-01-01T00:00:00Z", {"note": "CANARY-PAYLOAD-ACME-SYN-ALPHA"}),
                                  rec("CANARY-RECORD-ID-FUT", "2030-01-01T00:00:00Z", {"note": "CANARY-PAYLOAD-ACME-SYN-ALPHA"})]))
    s.add_tool("t", lambda q: [{"timestamp": "2023-01-01T00:00:00Z", "text": "CANARY-TOOL-RESULT"}])
    s.add_tool("boom", lambda: (_ for _ in ()).throw(RuntimeError("CANARY-ERROR-TEXT")))
    s.register_provider("fake", lambda req: ModelResponse("CANARY-RESPONSE-TEXT"))
    s.query("d", note="CANARY-PAYLOAD-ACME-SYN-ALPHA", k="CANARY-FILTER-STRATEGY")
    s.tool("t", q="CANARY-TOOL-ARG")
    with pytest.raises(RuntimeError):
        s.tool("boom")
    s.model("fake:m", messages=[{"role": "system", "content": "CANARY-SYSTEM-PARAM"}, {"role": "user", "content": "CANARY-PROMPT-TEXT " + MODEL_KEY}],
            api_key=MODEL_KEY, system="CANARY-SYSTEM-PARAM", temperature=0.5)
    s.decision("act", detail="CANARY-DECISION-DETAIL")
    s.close()
    return s


# ---------------------------------------------------------------- disabled by default
def test_sync_is_off_by_default_and_nothing_is_sent_without_an_explicit_push(mock, ledger_path, monkeypatch, tmp_path):
    cfg0 = SyncConfig.from_env({}, config_file=tmp_path / "none.json")
    assert cfg0.enabled is False
    led = Ledger(ledger_path)
    with pytest.raises(SyncDisabledError, match="off by default"):
        Syncer(led, cfg0).push()
    with pytest.raises(SyncDisabledError):
        Syncer(led, SyncConfig(endpoint=mock.url, api_key=SYNC_KEY)).push()                       # configured but not enabled: still refused
    monkeypatch.setenv("ASOF_SYNC_ENABLED", "1")
    monkeypatch.setenv("ASOF_SYNC_ENDPOINT", mock.url)
    monkeypatch.setenv("ASOF_SYNC_API_KEY", SYNC_KEY)
    run_everything(ledger_path, "full")                                                           # a whole session with sync enabled in the environment
    assert mock.requests == []                                                                    # a session never pushes by itself


def test_config_from_environment_and_file(tmp_path):
    f = tmp_path / "sync.json"
    f.write_text(json.dumps({"enabled": True, "endpoint": "https://sync.example.org/in", "api_key_env": "MY_SYNC_KEY", "send_payloads": False}))
    c = SyncConfig.from_env({"MY_SYNC_KEY": "k123"}, config_file=f)
    assert c.enabled and c.endpoint == "https://sync.example.org/in" and c.api_key == "k123" and c.send_payloads is False
    assert SyncConfig.from_env({"MY_SYNC_KEY": "k", "ASOF_SYNC_ENABLED": "0"}, config_file=f).enabled is False                        # the environment wins
    assert "k123" not in repr(c) and "k123" not in str(c)
    f.write_text(json.dumps({"enabled": True, "api_key": "literal"}))
    with pytest.raises(SyncError, match="do not put the API key in a file"):
        SyncConfig.from_env({}, config_file=f)


def test_endpoint_rules():
    base = dict(api_key="k", enabled=True)
    for bad in ("", "ftp://x/y", "http://example.org/in", "http://127.0.0.1/in"):
        with pytest.raises(SyncError):
            SyncConfig(endpoint=bad, **base).validate()
    SyncConfig(endpoint="http://127.0.0.1:9/in", allow_insecure_localhost=True, **base).validate()
    SyncConfig(endpoint="https://sync.example.org/in", **base).validate()
    with pytest.raises(SyncError, match="API key"):
        SyncConfig(endpoint="https://x/y", enabled=True).validate()
    with pytest.raises(SyncError, match="batch_size"):
        SyncConfig(endpoint="https://x/y", api_key="k", enabled=True, batch_size=0).validate()


# ---------------------------------------------------------------- D: nothing raw leaves the machine in hashes_only mode
def test_hashes_only_ledger_and_sync_payload_contain_no_known_raw_string(mock, ledger_path):
    run_everything(ledger_path, "hashes_only")
    db = ledger_path.read_bytes().decode("latin-1")
    res = Syncer(Ledger(ledger_path), cfg(mock, send_payloads=True)).push()                       # even with send_payloads on
    wire = mock.all_bytes().decode("utf-8")
    assert res.sent > 10 and res.payloads_included == res.sent                                    # payloads ARE sent in this mode: they are hash-only
    for c in CANARIES:
        assert c not in db, ("ledger", c)
        assert c not in wire, ("sync payload", c)
    assert MODEL_KEY not in db and MODEL_KEY not in wire and "SYNCMODELKEY" not in wire
    assert SYNC_KEY not in db and SYNC_KEY not in wire                                            # the sync key travels only in the Authorization header
    assert mock.requests[0]["headers"]["authorization"] == f"Bearer {SYNC_KEY}"
    assert '"param_sha256"' in wire and '"prompt_sha256"' in wire and '"response_sha256"' in wire


def test_full_mode_sends_only_envelopes_unless_payloads_are_separately_enabled(mock, ledger_path):
    run_everything(ledger_path, "full")
    res = Syncer(Ledger(ledger_path), cfg(mock)).push()
    wire = mock.all_bytes().decode("utf-8")
    assert res.sent > 10 and res.payloads_included == 0
    assert all("payload" not in e for e in mock.events()) and not any(c in wire for c in CANARIES)
    mock.requests.clear()
    led = Ledger(ledger_path)
    Syncer(led, cfg(mock, send_payloads=True)).cursor_path.unlink()                                # forget the cursor: send everything again, this time with payloads
    res2 = Syncer(led, cfg(mock, send_payloads=True)).push()
    wire2 = mock.all_bytes().decode("utf-8")
    assert res2.payloads_included == res2.sent and "CANARY-PAYLOAD-ACME-SYN-ALPHA" in wire2 and "CANARY-PROMPT-TEXT" in wire2
    assert MODEL_KEY not in wire2 and "SYNCMODELKEY" not in wire2 and "[REDACTED]" in wire2       # credentials are redacted even when payloads are sent


def receiver_verifies_chain(events):
    """What a receiving service can do with envelopes alone (no payloads): recompute every hash. Written against the spec, not the library."""
    prev = "0" * 64
    for e in events:
        h = hashlib.sha256(json.dumps(["asof-ledger/3", e["seq"], prev, e["id"], e["session_id"], e["type"], e["call_id"], e["event_time"], e["available_at"],
                                       e["recorded_at"], e["payload_sha256"]], separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        assert e["prev_hash"] == prev and e["hash"] == h, e["seq"]
        prev = h
    return prev


def test_a_receiver_can_verify_the_whole_chain_from_envelopes_alone(mock, ledger_path):
    run_everything(ledger_path, "full")
    Syncer(Ledger(ledger_path), cfg(mock)).push()
    head = receiver_verifies_chain(mock.events())
    assert head == Ledger(ledger_path).head().hash


# ---------------------------------------------------------------- protocol behaviour
def test_batching_cursor_and_incremental_pushes(mock, ledger_path):
    run_everything(ledger_path, "hashes_only")
    led = Ledger(ledger_path)
    n = len(led)
    syncer = Syncer(led, cfg(mock, batch_size=7))
    r1 = syncer.push()
    assert r1.sent == n and r1.batches == -(-n // 7) and r1.acked_through == n and mock.requests[0]["json"]["schema"] == SCHEMA
    assert all(len(r["json"]["events"]) <= 7 for r in mock.requests)
    assert syncer.push().sent == 0                                                                  # nothing new
    s = Session("2023-01-21T14:00:00Z", ledger_path=ledger_path, privacy="hashes_only")
    s.decision("more")
    s.close()
    before = len(mock.requests)
    r2 = Syncer(Ledger(ledger_path), cfg(mock, batch_size=7)).push()                                 # a new Syncer object: the cursor is on disk
    assert r2.sent == 3 and len(mock.requests) == before + 1 and mock.requests[-1]["json"]["events"][0]["seq"] == n + 1


def test_the_cursor_is_per_endpoint(mock, ledger_path):
    run_everything(ledger_path, "hashes_only")
    led = Ledger(ledger_path)
    Syncer(led, cfg(mock)).push()
    other = Mock()
    try:
        r = Syncer(led, cfg(other)).push()
        assert r.sent == len(led)                                                                     # a different endpoint has not seen anything yet
    finally:
        other.srv.shutdown()


def test_partial_acceptance_stops_and_resumes_where_the_receiver_got_to(mock, ledger_path):
    run_everything(ledger_path, "hashes_only")
    led = Ledger(ledger_path)
    mock.mode, mock.partial = "partial", 5
    r = Syncer(led, cfg(mock, batch_size=100)).push()
    assert r.acked_through == 5 and r.batches == 1
    mock.mode = "ok"
    r2 = Syncer(led, cfg(mock)).push()
    assert mock.requests[-1]["json"]["events"][0]["seq"] == 6 and r2.acked_through == len(led)


def test_server_errors_are_retried_and_client_errors_are_not(mock, ledger_path):
    run_everything(ledger_path, "hashes_only")
    sleeps = []
    mock.mode = "flaky"
    r = Syncer(Ledger(ledger_path), cfg(mock), sleep=sleeps.append).push()
    assert r.acked_through == len(Ledger(ledger_path)) and sleeps == [1]
    mock.mode, mock.requests = "deny", []
    s2 = Syncer(Ledger(ledger_path), cfg(mock), sleep=sleeps.append)
    s2.cursor_path.unlink()
    with pytest.raises(SyncError, match="HTTP 401"):
        s2.push()
    assert len(mock.requests) == 1                                                                   # no retry on a 4xx


def test_the_api_key_is_never_in_errors_logs_the_ledger_or_the_cursor(mock, ledger_path, caplog):
    run_everything(ledger_path, "hashes_only")
    mock.mode = "deny"                                                                               # the server even echoes the key back in its error body
    syncer = Syncer(Ledger(ledger_path), cfg(mock))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SyncError) as e:
            syncer.push()
    assert SYNC_KEY not in str(e.value) and "SYNCKEY" not in str(e.value) and "[REDACTED]" in str(e.value)
    assert SYNC_KEY not in caplog.text and SYNC_KEY not in repr(syncer.cfg)
    mock.mode = "ok"
    syncer.push()
    assert SYNC_KEY.encode() not in ledger_path.read_bytes() and SYNC_KEY not in syncer.cursor_path.read_text()
    assert SYNC_KEY not in caplog.text


def test_a_reply_without_accepted_through_is_an_error_and_not_a_silent_success(mock, ledger_path):
    run_everything(ledger_path, "hashes_only")
    mock.mode = "badjson"
    with pytest.raises(SyncError, match="not JSON"):
        Syncer(Ledger(ledger_path), cfg(mock)).push()
    assert Syncer(Ledger(ledger_path), cfg(mock)).acked() == 0                                       # nothing was marked as sent


def test_unreachable_endpoint_fails_clearly(ledger_path):
    run_everything(ledger_path, "hashes_only")
    c = SyncConfig(endpoint="http://127.0.0.1:9/in", api_key=SYNC_KEY, enabled=True, allow_insecure_localhost=True, retries=0, timeout=2)
    with pytest.raises(SyncError, match="could not reach") as e:
        Syncer(Ledger(ledger_path), c).push()
    assert SYNC_KEY not in str(e.value)


# ---------------------------------------------------------------- real TLS with a private CA
@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")
def test_https_with_a_private_ca_and_refusal_without_it(tmp_path, ledger_path):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt), "-days", "2", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
    m = Mock(tls=(str(crt), str(key)))
    try:
        run_everything(ledger_path, "hashes_only")
        led = Ledger(ledger_path)
        ok = Syncer(led, SyncConfig(endpoint=m.url, api_key=SYNC_KEY, enabled=True, ca_file=str(crt))).push()
        assert ok.sent == len(led) and m.requests[0]["headers"]["authorization"] == f"Bearer {SYNC_KEY}"
        s2 = Syncer(led, SyncConfig(endpoint=m.url, api_key=SYNC_KEY, enabled=True, retries=0))
        s2.cursor_path.unlink()
        with pytest.raises(SyncError, match="could not reach") as e:                                  # an unknown CA is refused, not bypassed
            s2.push()
        assert SYNC_KEY not in str(e.value)
    finally:
        m.srv.shutdown()
