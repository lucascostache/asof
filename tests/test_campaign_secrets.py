"""Pre-launch campaign, area D: secrets and privacy.

15. redaction fuzzing: a provider (or a tool, or a source) that repeats the key back in many different forms; none may reach the error text, the ledger, the receipts, a report, logs or output
17. hashes_only: a canary string placed in every kind of content; none of it may be in the ledger file, the receipts, a report or a sync body
18. the key is never on a command line, in a traceback, or in what the tools print

(16, searching every artifact of a live run for the key and its fragments, is scripts/secret_search.py, run after each live run.)

No real key is used anywhere here: the "keys" are random strings made in this file, with the same shapes as real ones.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from asof import GatewayError, ModelResponse, Session, sources
from asof.widgets import report

from conftest import ListSource, rec

ROOT = Path(__file__).resolve().parent.parent
_rnd = random.Random(20261111)
_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"                                                              # upper case and digits only: cannot be mistaken for the lower-case hex of a hash or an id


def _random(n):
    return "".join(_rnd.choice(_ALPHA) for _ in range(n))


SK_PREFIX = "sk-" + "ant-api03-"
KEY_SK = SK_PREFIX + _random(60)                                                                          # shaped like a provider key
KEY_OPAQUE = "tok" + _random(45)                                                                          # no recognisable shape at all: only knowing the value can redact it
KEY_SYMBOLS = "kq+/" + _random(20) + "+/" + _random(20) + "=="                                            # one whose URL-encoded form differs from itself
MSGS = [{"role": "user", "content": "hello"}]


def secret_part(key):
    return key[len(SK_PREFIX):] if key.startswith(SK_PREFIX) else key


def windows(text, n=8):
    return {text[i:i + n] for i in range(len(text) - n + 1)}


# ------------------------------------------------------------------ a provider that says whatever it is told to
@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ASOF_LOCAL_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    mode = {"status": 401, "body": b"{}", "content_type": "application/json"}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = mode["body"]
            self.send_response(mode["status"])
            self.send_header("Content-Type", mode["content_type"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", mode
    srv.shutdown()


def chunks(s, n, sep):
    return sep.join(s[i:i + n] for i in range(0, len(s), n))


def echoes(key):
    """Ways a provider's error body might repeat a key. Each is (name, body text, strings that must then be nowhere)."""
    part = secret_part(key)
    full = windows(part)
    out = [
        ("the whole key in a sentence", f"Invalid API key: {key}. Check your settings.", full),
        ("the whole key, nothing else", key, full),
        ("as a JSON field", json.dumps({"error": {"type": "auth", "message": "bad key", "received": key}}), full),
        ("deep in nested JSON", json.dumps({"a": [{"b": {"c": [1, {"request": {"headers": {"x-api-key": key}}}]}}]}), full),
        ("as an echoed header line", f"x-api-key: {key}\nanthropic-version: 2023-06-01", full),
        ("as a bearer header", f"Authorization: Bearer {key}", full),
        ("as basic auth, base64 of user:key", "Authorization: Basic " + base64.b64encode(f"user:{key}".encode()).decode(), full | windows(base64.b64encode(f"user:{key}".encode()).decode()[8:-4], 10)),
        ("base64 of the key", "token=" + base64.b64encode(key.encode()).decode(), windows(base64.b64encode(key.encode()).decode()[4:-4], 10)),
        ("url-safe base64 of the key", base64.urlsafe_b64encode(key.encode()).decode().rstrip("="), windows(base64.urlsafe_b64encode(key.encode()).decode()[4:-4], 10)),
        ("base64 with a one-byte prefix", base64.b64encode(b":" + key.encode()).decode(), windows(base64.b64encode(b":" + key.encode()).decode()[4:-4], 10)),
        ("hex of the key", "key bytes: " + key.encode().hex(), windows(key.encode().hex()[8:], 12)),
        ("upper-case hex of the key", key.encode().hex().upper(), windows(key.encode().hex().upper()[8:], 12)),
        ("url-encoded", "api_key=" + urllib.parse.quote(key, safe=""), windows(urllib.parse.quote(secret_part(key), safe=""))),
        ("in a query string", f"GET https://api.example.invalid/v1/models?api_key={key}&limit=5 failed", full),
        ("as URL credentials", f"could not connect to https://svc:{key}@api.example.invalid/v1", full),
        ("twice", f"{key} is not valid; you sent {key}", full),
        ("in the middle of a long body", "x" * 300 + key + "y" * 300, full),
        ("straddling the point where a long body is cut", "x" * (500 - len(key) // 2) + key + "y" * 300, windows(part[: max(8, len(part) // 2 - 8)])),
        ("at the very end of a long body", "z" * 2000 + key, full),
        ("quoted and escaped as a JSON string inside a string", json.dumps({"detail": json.dumps({"key": key})}), full),
        ("with the first 30 characters only", key[:30] + " (truncated)", windows(key[len(SK_PREFIX):30] if key.startswith(SK_PREFIX) else key[:30])),
        ("with the prefix missing", "..." + key[10:], windows(key[max(10, len(key) - len(part)):])),
        ("split over lines, 10 characters each", chunks(key, 10, "\n"), {c for c in chunks(part, 10, "\n").split("\n") if len(c) >= 6} | {"COMPACT"}),
        ("split over lines, 4 characters each", chunks(key, 4, "\r\n"), {"COMPACT"}),
        ("with a space every 5 characters", chunks(key, 5, " "), {"COMPACT"}),
        ("with a literal backslash-n every 8 characters", chunks(key, 8, "\\n"), {"COMPACT"}),
        ("with a tab and indentation between two halves", key[: len(key) // 2] + "\n\t    " + key[len(key) // 2:], {"COMPACT"}),
    ]
    for head, tail in ((8, 4), (4, 4), (16, 0), (0, 6), (6, 6), (3, 3), (20, 20), (24, 4), (0, 4), (len(SK_PREFIX) + 4, 4)):
        for mask in ("*" * 12, "****", "...", "\u2026", "\u2022" * 6, "XXXXXXXX", "xxxx", "####", "[...]", "<redacted>", "-" * 5):
            token = key[:head] + mask + (key[-tail:] if tail else "")
            must_go = {token}
            if tail >= 4:
                must_go.add(key[-tail:])
            if head > len(key) - len(part) + 3:
                must_go.add(key[:head])
            out.append((f"masked by the provider: first {head}, {mask!r}, last {tail}", f"Incorrect API key provided: {token}. You can find your key in the console.", must_go))
    return out


def everything_written(session, ledger_path, error_text, capsys, caplog):
    """Every place a string could end up: the error the caller gets, the ledger and the files beside it, every receipt (both views), a report, what was printed, what was logged."""
    blobs = {"the error": error_text}
    for f in sorted(ledger_path.parent.iterdir()):
        if f.is_file():
            blobs[f"file {f.name}"] = f.read_bytes().decode("latin-1")
    blobs["the ledger's events"] = json.dumps([[e.type, e.payload] for e in session.events()])
    blobs["the receipts"] = json.dumps([[r.to_json(), r.agent_view(), r.summary()] for r in session.receipt(last=False)], default=str)
    blobs["the model receipt"] = json.dumps(session.model_receipt(), default=str) if session.events("model_call") else ""
    blobs["a report"] = report.build(ledger_path)
    out = capsys.readouterr()
    blobs["stdout"], blobs["stderr"], blobs["logs"] = out.out, out.err, caplog.text
    return blobs


def assert_absent(blobs, forbidden, key, label):
    compact = "COMPACT" in forbidden
    for where, text in blobs.items():
        assert key not in text, f"{label}: the whole key is in {where}"
        for bad in forbidden - {"COMPACT"}:
            assert bad not in text, f"{label}: {bad!r} is in {where}"
        if compact:                                                                                       # a key broken up by white space or line breaks: put the pieces back together and look again
            joined = re.sub(r"\\[nrt]|\s", "", text)
            hit = next((w for w in windows(secret_part(key)) if w in joined), None)
            assert hit is None, f"{label}: the key, split up, is in {where} (found {hit!r} once the breaks are removed)"


CASES = [(shape, name) for shape in ("sk", "opaque") for name, _, _ in echoes(KEY_SK if shape == "sk" else KEY_OPAQUE)]


@pytest.mark.parametrize("shape, name", CASES, ids=[f"{s}: {n}" for s, n in CASES])
def test_a_provider_error_that_repeats_the_key_in_any_form_leaves_no_trace_of_it(provider, tmp_path, capsys, caplog, shape, name):
    base, mode = provider
    key = KEY_SK if shape == "sk" else KEY_OPAQUE
    body, forbidden = next((b, f) for n, b, f in echoes(key) if n == name)
    mode.update(status=401, body=body.encode("utf-8"), content_type="text/plain" if not body.startswith("{") else "application/json")
    caplog.set_level(logging.DEBUG)
    ledger = tmp_path / "ledger.db"
    with Session("2023-01-21T14:00:00Z", ledger_path=ledger, privacy="full") as s:                         # full mode: the error text is stored readable, so a leak would be visible
        with pytest.raises(GatewayError) as e:
            if shape == "sk":
                s.model("anthropic:m", messages=MSGS, api_key=key, base_url=base)
            else:
                s.model("local:m", messages=MSGS, api_key=key, base_url=base + "/v1")
        assert "401" in str(e.value)                                                                       # still a useful error: what happened is said
        blobs = everything_written(s, ledger, str(e.value) + repr(e.value) + "".join(map(str, e.value.args)), capsys, caplog)
        assert s.verify_ledger().ok
    assert_absent(blobs, set(forbidden), key, name)


@pytest.mark.parametrize("status", [200, 400, 401, 403, 404, 429, 500, 502, 503])
def test_the_key_is_removed_whatever_the_status_and_even_from_a_successful_reply(provider, tmp_path, capsys, caplog, status):
    base, mode = provider
    for key in (KEY_SK, KEY_OPAQUE, KEY_SYMBOLS):
        if status == 200:                                                                                  # a provider that puts the key in its ANSWER
            mode.update(status=200, body=json.dumps({"model": "m-1", "content": [{"type": "text", "text": f"your key is {key}, base64 {base64.b64encode(key.encode()).decode()}"}], "stop_reason": "end_turn",
                                                     "usage": {"input_tokens": 1}, "debug": {"x-api-key": key}}).encode())
        else:
            mode.update(status=status, body=json.dumps({"error": {"message": f"request with key {key} failed", "url": f"https://x.invalid/?key={urllib.parse.quote(key, safe='')}"}}).encode())
        d = tmp_path / f"{status}-{key[:3]}"
        d.mkdir()
        with Session("2023-01-21T14:00:00Z", ledger_path=d / "ledger.db") as s:
            text = ""
            try:
                r = s.model("anthropic:m", messages=MSGS, api_key=key, base_url=base)
                text = r.text + json.dumps(dict(r.usage)) + str(r)
            except GatewayError as e:
                text = str(e)
            blobs = everything_written(s, d / "ledger.db", text, capsys, caplog)
        assert_absent(blobs, windows(secret_part(key)) | windows(urllib.parse.quote(secret_part(key), safe="")), key, f"HTTP {status}")


def test_a_key_from_the_environment_is_protected_the_same_way(provider, tmp_path, capsys, caplog, monkeypatch):
    base, mode = provider
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY_OPAQUE)                                                    # not passed as an argument: read from the environment by the gateway
    mode.update(status=403, body=f"key {KEY_OPAQUE} lacks permission; masked: {KEY_OPAQUE[:6]}...{KEY_OPAQUE[-4:]}".encode())
    with Session("2023-01-21T14:00:00Z", ledger_path=tmp_path / "ledger.db") as s:
        with pytest.raises(GatewayError) as e:
            s.model("anthropic:m", messages=MSGS, base_url=base)
        blobs = everything_written(s, tmp_path / "ledger.db", str(e.value), capsys, caplog)
    assert_absent(blobs, windows(KEY_OPAQUE) | {KEY_OPAQUE[-4:] + "".join(()), f"{KEY_OPAQUE[:6]}...{KEY_OPAQUE[-4:]}"}, KEY_OPAQUE, "environment key")


def test_secrets_in_tool_errors_source_errors_and_custom_providers_do_not_reach_the_ledger(tmp_path, capsys, caplog):
    """Not the gateway's own key this time: things an operator's own code might put in an exception. Shapes that are recognisable are removed without being told; an opaque value is
    removed once it has been registered (`asof.register_secret`)."""
    import asof
    dsn = f"postgresql://svc_reader:{KEY_OPAQUE[:24]}@db.internal.example:5432/prices"
    bearer = "Bearer " + _random(40)
    jwt = "eyJ" + base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")[3:] + "." + "eyJ" + _random(30) + "." + _random(43)
    pem = "-----BEGIN PRIV" + "ATE KEY-----\n" + chunks(_random(120), 64, "\n") + "\n-----END PRIV" + "ATE KEY-----"      # in two parts, so this file holds no key-shaped text
    shaped = {"a database address with a password": (dsn, {KEY_OPAQUE[:24]}), "a bearer token": (bearer, {bearer[7:]}), "a JWT": (jwt, {jwt, jwt.split(".")[2]}),
              "a private key block": (pem, windows(pem.split("\n")[1], 16)), "a github token": ("ghp_" + _random(36), set()), "an AWS key id": ("AK" + "IA" + _random(16), set()),
              "password=... in text": (f"login failed: password={KEY_OPAQUE[:20]} user=svc", {KEY_OPAQUE[:20]}), "api_key: ... in text": (f"api_key: {KEY_SYMBOLS}", windows(KEY_SYMBOLS[4:24]))}
    registered = "cust_" + _random(40)
    asof.register_secret(registered)
    with Session("2023-01-21T14:00:00Z", ledger_path=tmp_path / "ledger.db") as s:
        for what, (text, _) in shaped.items():
            def boom(text=text):
                raise RuntimeError(f"upstream failure: {text}")
            s.add_tool(f"t{len(s.tools)}", boom)
            with pytest.raises(RuntimeError):
                s.tool(s.tools[-1])

        class Broken:
            name = "db"

            def records(self):
                raise ConnectionError(f"cannot connect: {dsn} (token {registered})")
        with pytest.raises(ConnectionError):
            s.add_source(Broken())
        s.register_provider("mine", lambda req: (_ for _ in ()).throw(RuntimeError(f"provider said no; key {registered}; b64 {base64.b64encode(registered.encode()).decode()}")))
        with pytest.raises(RuntimeError):
            s.model("mine:m", messages=MSGS)
        s.add_tool("leaky", lambda: [{"timestamp": "2023-01-01T00:00:00Z", "note": f"served item holding {registered} and {dsn}"}])
        s.tool("leaky")                                                                                    # a secret inside a served item is stored redacted too
        s.decision("hold", reason=f"because {registered}")
        blobs = everything_written(s, tmp_path / "ledger.db", "", capsys, caplog)
    for what, (text, extra) in shaped.items():
        assert_absent(blobs, {text} | extra, text, what)
    assert_absent(blobs, windows(registered[5:]) | windows(base64.b64encode(registered.encode()).decode()[4:-4], 10), registered, "a registered secret")
    assert "[REDACTED]" in blobs["the ledger's events"] and "upstream failure" in blobs["the ledger's events"]      # the errors themselves are still there


# ------------------------------------------------------------------ 17. hashes_only
CANARY = "CANARYQZX"                                                                                       # every piece of content below contains this; the ledger must not


def test_hashes_only_keeps_no_content_anywhere_only_hashes(tmp_path, capsys, caplog):
    import asof
    from asof.sync import SyncConfig, Syncer
    bodies = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            bodies.append(raw.decode("utf-8"))
            out = json.dumps({"accepted_through": json.loads(raw)["events"][-1]["seq"]}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    csv = tmp_path / "prices.csv"
    csv.write_text(f"id,symbol,date,close,available_at,note\n{CANARY}-row1,{CANARY}-SYM,2023-01-19,148.257391,2023-01-19T21:00:00Z,{CANARY} note one\n"
                   f"{CANARY}-row2,{CANARY}-SYM,2023-01-20,150.573192,2023-01-20T21:00:00Z,{CANARY} note two\n{CANARY}-row2,{CANARY}-SYM,2023-01-20,151.753294,2023-01-20T22:00:00Z,{CANARY} restated\n"
                   f"{CANARY}-row3,{CANARY}-SYM,2023-01-23,155.073195,2023-01-23T21:00:00Z,{CANARY} from the future\n{CANARY}-row4,{CANARY}-SYM,2023-01-24,,,{CANARY} undated\n")
    ledger = tmp_path / "ledger.db"
    try:
        with Session("2023-01-21T14:00:00Z", ledger_path=ledger, privacy="hashes_only", decision_gate=False) as s:
            s.add_source(sources.CSV(csv, name="prices", event_time="date", available_at="available_at", record_id="id", assume_tz="UTC"))
            s.add_source(ListSource("mem", [rec(f"{CANARY}-mem{i}", f"2023-01-{10 + i:02d}T00:00:00Z", {"text": f"{CANARY} memory {i}", "n": i}) for i in range(1, 15)]))
            s.query("prices", filters={"symbol": f"{CANARY}-SYM", "note": [f"{CANARY} note one", f"{CANARY} nothing"]}, lookback="30d", limit=5)
            s.query("mem", text=f"{CANARY} memory 3")
            s.add_tool("search", lambda query, extra=None: [{"title": f"{CANARY} headline about {query}", "published_at": "2023-01-20T12:00:00Z", "body": {"deep": [f"{CANARY} nested"]}},
                                                              {"title": f"{CANARY} later headline", "published_at": "2023-02-01T00:00:00Z"}], timestamp_field="published_at")
            s.tool("search", query=f"{CANARY} what the agent asked", extra={"k": f"{CANARY} argument"})
            s.add_tool("fails", lambda: (_ for _ in ()).throw(ValueError(f"{CANARY} in an error message")))
            with pytest.raises(ValueError):
                s.tool("fails")
            s.register_provider("fake", lambda req: ModelResponse(f"{CANARY} is what the model said", "fake-1", "stop", {"input_tokens": 3}))
            s.model("fake:m", messages=[{"role": "system", "content": f"{CANARY} system prompt"}, {"role": "user", "content": f"{CANARY} user prompt"}], temperature=0.257396, stop=[f"{CANARY} stop word"],
                    metadata={"user_id": f"{CANARY} parameter value"})
            s.register_provider("bad", lambda req: (_ for _ in ()).throw(RuntimeError(f"{CANARY} provider error")))
            with pytest.raises(RuntimeError):
                s.model("bad:m", messages=[{"role": "user", "content": f"{CANARY} prompt to a failing model"}])
            call = s.decision("buy", reason=f"{CANARY} reasoning", symbol=f"{CANARY}-SYM", size=3)
            s.order("o1", symbol=f"{CANARY}-SYM", side="buy", size=3, intended_price=150.573297, expected_price=150.753298, decision=call)
            s.fill("o1", price=151.073299, size=3, fees=0.373191)
            s.mark(f"{CANARY}-SYM", size=3, price=152.073193)
            s.advance_to("2023-01-22T00:00:00Z")
            receipts = json.dumps([[r.to_json(), r.agent_view(), r.summary()] for r in s.receipt(last=False)], default=str) + json.dumps(s.model_receipt(), default=str)
            quality = json.dumps(s.quality(), default=str)
            assert s.verify_ledger().ok
        res = Syncer(asof.Ledger(ledger), SyncConfig(enabled=True, endpoint=f"http://127.0.0.1:{srv.server_port}/sync", api_key="k" * 40, allow_insecure_localhost=True)).push()
        assert res.sent > 20 and bodies
    finally:
        srv.shutdown()
    out = capsys.readouterr()
    blobs = {"receipts": receipts, "the per-source quality summary": quality, "a report": report.build(ledger), "an external report": report.build(ledger, external=True), "sync bodies": "".join(bodies),
             "stdout": out.out, "stderr": out.err, "logs": caplog.text}
    for f in sorted(tmp_path.iterdir()):
        if f.is_file() and f.name != "prices.csv":
            blobs[f"file {f.name}"] = f.read_bytes().decode("latin-1")
    for where, text in blobs.items():
        for needle in (CANARY, CANARY.lower(), "148.2573", "150.5731", "151.7532", "155.0731", "150.5732", "150.7532", "151.0732", "0.3731", "152.0731", "0.2573"):      # the canary, and the numbers that were in the data, the order and the parameters
            assert needle not in text, f"{needle!r} is in {where}"
    events = [json.loads(e.payload) for e in asof.Ledger(ledger).events()]
    hashed = sum(1 for e in events for k in e if k.endswith("_sha256"))
    assert hashed >= 25                                                                                    # the content is there as hashes: the ledger is not simply empty
    kinds = {e.type for e in asof.Ledger(ledger).events()}
    assert {"record_served", "tool_item_served", "query", "tool_call", "tool_error", "model_call", "model_error", "decision", "order", "fill", "position_mark", "block", "clock_advance"} <= kinds


# ------------------------------------------------------------------ 18. the key and the process
def test_the_key_is_never_on_a_command_line_or_in_what_a_failing_script_prints(tmp_path):
    """The scripts take keys from the environment only. While one is running (against a provider that takes its time), the process list shows no key; and when it fails, neither its
    output nor its traceback (there should be none) contains the key."""
    hold = threading.Event()

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            hold.wait(timeout=20)                                                                          # keep the client waiting so the process list can be read
            raw = json.dumps({"error": {"message": f"Incorrect API key provided: {KEY_SK[:8]}****{KEY_SK[-4:]}; full: {KEY_SK}"}}).encode()
            self.send_response(401); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = {**os.environ, "PYTHONPATH": str(ROOT), "ANTHROPIC_API_KEY": KEY_SK, "NO_PROXY": "*", "no_proxy": "*"}
    try:
        p = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "smoke_anthropic.py"), "--model", "m", "--base-url", f"http://127.0.0.1:{srv.server_port}", "--ledger", str(tmp_path / "l.db")],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        time.sleep(1.5)
        listing = subprocess.run(["ps", "-ww", "-A", "-o", "pid=,args="], capture_output=True, text=True).stdout    # what any user of the machine can see; -ww: whole command lines (Linux's ps cuts them to ~80 columns without a terminal, which would hide a key)
        mine = [ln for ln in listing.splitlines() if ln.strip().startswith(str(p.pid) + " ")]
        assert mine and "smoke_anthropic.py" in mine[0], listing[:500]
        hold.set()
        out, err = p.communicate(timeout=60)
    finally:
        hold.set()
        srv.shutdown()
    assert KEY_SK not in listing and secret_part(KEY_SK)[:12] not in listing
    assert p.returncode == 1 and "RESULT: FAIL" in err
    for text in (out, err):
        assert "Traceback" not in text
        assert KEY_SK not in text and not (windows(secret_part(KEY_SK)) & windows(text)) and KEY_SK[-4:] + ";" not in text
    for f in tmp_path.iterdir():
        assert KEY_SK.encode() not in f.read_bytes() and secret_part(KEY_SK)[:10].encode() not in f.read_bytes()


def test_no_script_or_command_accepts_a_key_as_an_argument():
    """A key given as an argument would be visible in the process list and in shell history. Keys come from the environment (or, in Python, from a parameter), never from argv."""
    offenders = []
    for f in sorted([*ROOT.glob("scripts/*.py"), *ROOT.glob("scripts/*.sh"), *ROOT.glob("demo/*.py"), *ROOT.glob("asof/*.py"), *ROOT.glob("examples/**/*.py"), *ROOT.glob("docs/pilot/examples/*.py")]):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r"add_argument\(\s*[\"']--?(api[-_]?key|key|token|secret|password)\b", line, re.I):
                offenders.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()[:100]}")
    assert offenders == [], offenders


def test_an_unexpected_exception_inside_the_library_does_not_carry_the_key_in_its_traceback(provider, tmp_path):
    import traceback
    base, mode = provider
    mode.update(status=200, body=b"this is not json at all " + KEY_OPAQUE.encode())                        # a 200 whose body cannot be parsed, and which contains the key
    with Session("2023-01-21T14:00:00Z", ledger_path=tmp_path / "ledger.db") as s:
        try:
            s.model("anthropic:m", messages=MSGS, api_key=KEY_OPAQUE, base_url=base)
            raise AssertionError("expected a GatewayError")
        except GatewayError:
            tb = traceback.format_exc()
    assert KEY_OPAQUE not in tb and not (windows(KEY_OPAQUE) & windows(tb))                                # the traceback shows code and the message, and neither holds the key
    assert KEY_OPAQUE.encode() not in (tmp_path / "ledger.db").read_bytes()


@pytest.mark.parametrize("mask", ["****", "************", "...", "…", "••••••", "XXXXXXXX", "xxxx", "####", "[...]", "<redacted>", "-----"])
@pytest.mark.parametrize("keep", [4, 6])
@pytest.mark.parametrize("order", ["the masked key was registered last", "the masked key was registered first"])
def test_two_keys_that_begin_alike_a_masked_echo_of_one_leaves_none_of_its_last_characters(mask, keep, order):
    """Found when the whole suite ran in one process (the file alone passed): keys of one provider begin alike, so the beginning of a masked key matched an EARLIER registered key, which
    took "beginning + mask" and left the last characters of the key that was really masked in the error text. An operator with two keys of one provider had this for real."""
    from asof import redaction
    before = redaction.known_secrets()
    head = "sk-" + "ant-" + "api03-"                                                              # in pieces: this is not a key, and must not look like one to a scanner
    key_a, key_b = head + "Qm7Zr2Lx9Tn4Vb6Hc1Jd8Kf3Wg5Ys0PaEu", head + "Bd4Nw8Rk2Xc6Mt1Hs9Lp3Vz7Jq5Fy0GoIe"
    try:
        redaction.forget_secrets()
        for k in ((key_a, key_b) if order.endswith("last") else (key_b, key_a)):
            redaction.register_secret(k)
        echo = f"Incorrect API key provided: {key_b[:keep]}{mask}{key_b[-keep:]}. You can find your key in the console."
        out = redaction.scrub(echo)
        assert key_b[-keep:] not in out and key_b[-3:] not in out, out
        assert out == f"Incorrect API key provided: {redaction.REDACTED}. You can find your key in the console.", out
        echo_tail_only = f"key ending in {mask}{key_a[-keep:]} was rejected"                    # the other key, masked with no beginning at all
        assert key_a[-keep:] not in redaction.scrub(echo_tail_only)
    finally:
        redaction.forget_secrets()
        for k in before:
            redaction.register_secret(k)
