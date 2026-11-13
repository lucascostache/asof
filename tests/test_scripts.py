"""G: the smoke scripts you will run with your own keys, tested here against local mock servers with dummy keys."""
import json
import re
import shutil
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
DUMMY = "sk-" + "dummy-SMOKEKEY0123456789abcdef"          # a fake key, written in two parts so the source holds no key-shaped string (secret scanners, push protection)


def run(script, args, env_extra=None, **kw):
    env = {**os.environ, "NO_PROXY": "*", "no_proxy": "*"}
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ASOF_SEC_USER_AGENT"):
        env.pop(k, None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True, timeout=120, env=env, **kw)


class ModelMock:
    def __init__(self, mode="ok"):
        self.seen = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                if mode == "http401":
                    raw = json.dumps({"error": {"message": "invalid x-api-key " + DUMMY}}).encode()           # a provider that echoes the key back in its error
                    self.send_response(401); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
                    return
                out = ({"model": "claude-mock-1", "content": [{"type": "text", "text": "A time lock stops an agent seeing the future."}], "stop_reason": "end_turn", "usage": {"input_tokens": 9}}
                       if self.path.endswith("/v1/messages") else
                       {"model": "gpt-mock-1", "choices": [{"message": {"content": "A time lock hides the future."}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 9}})
                if mode == "empty":
                    out = ({"model": "claude-mock-1", "content": [{"type": "text", "text": "  "}], "stop_reason": "end_turn", "usage": {}}
                           if self.path.endswith("/v1/messages") else {"model": "gpt-mock-1", "choices": [{"message": {"content": ""}, "finish_reason": "stop"}], "usage": {}})
                raw = json.dumps(out).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.srv.server_port}"


@pytest.fixture
def model_mock():
    m = ModelMock()
    yield m
    m.srv.shutdown()


@pytest.mark.parametrize("script, key_env, base_suffix, path", [("smoke_anthropic.py", "ANTHROPIC_API_KEY", "", "/v1/messages"),
                                                                ("smoke_openai.py", "OPENAI_API_KEY", "/v1", "/v1/chat/completions")])
def test_model_smoke_scripts_make_one_call_print_the_receipt_and_verify_the_ledger(model_mock, tmp_path, script, key_env, base_suffix, path):
    r = run(script, ["--model", "mock-model", "--base-url", model_mock.base + base_suffix, "--ledger", str(tmp_path / "l.db")], {key_env: DUMMY})
    assert r.returncode == 0, r.stderr + r.stdout
    out = r.stdout
    assert len(model_mock.seen) == 1 and model_mock.seen[0][0] == path                      # exactly ONE call
    assert "Receipt " in out and "(model: " in out and "memory label 'unknown'" in out and "verifies: True" in out
    assert "tail anchor: ok" in out and "API key present in the ledger file: False" in out and '"seq"' in out
    assert DUMMY not in out and DUMMY not in r.stderr and DUMMY.encode() not in (tmp_path / "l.db").read_bytes()


def test_model_smoke_scripts_default_to_a_temporary_ledger_and_do_not_touch_home(model_mock, tmp_path):
    r = run("smoke_openai.py", ["--model", "m", "--base-url", model_mock.base + "/v1"], {"OPENAI_API_KEY": DUMMY, "HOME": str(tmp_path)})
    assert r.returncode == 0 and not (tmp_path / ".asof").exists()


@pytest.mark.parametrize("script, key_env", [("smoke_anthropic.py", "ANTHROPIC_API_KEY"), ("smoke_openai.py", "OPENAI_API_KEY")])
def test_model_smoke_scripts_refuse_politely_without_a_key_or_a_model(script, key_env):
    r = run(script, ["--model", "m"])
    assert r.returncode == 2 and key_env in r.stderr and "exactly ONE real call" in r.stderr and r.stdout == ""
    r = run(script, [], {key_env: DUMMY})
    assert r.returncode == 2 and "--model" in r.stderr


# ------------------------------------------------------------------ smoke_edgar against a mock EDGAR
UA = "Test Org t@example.org"
ACCESSIONS = ["0009999991-23-000106", "0009999991-23-000077", "0009999991-23-000064"]
RAW = ["2023-11-03T09:08:27.000Z", "2023-08-04T10:01:02.000Z", "2023-05-05T21:30:00.000Z"]


class EdgarMock:
    """``truth``: 'utc' means the JSON's Z label is right (the index page shows NY time = UTC converted); 'ny' means the digits are New York time."""
    def __init__(self, truth, index_html=None):
        outer = self
        self.requests = []

        def accepted(raw):
            d = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=timezone.utc if truth == "utc" else ZoneInfo("America/New_York"))
            return d.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("User-Agent")))
                if self.path.startswith("/submissions/"):
                    body = json.dumps({"cik": "0009999991", "name": "Example Inc", "filings": {"recent": {
                        "accessionNumber": ACCESSIONS, "form": ["10-K", "10-Q", "8-K"], "filingDate": ["2023-11-03", "2023-08-04", "2023-05-05"],
                        "reportDate": ["2023-09-30", "2023-07-01", "2023-05-05"], "acceptanceDateTime": RAW, "primaryDocument": ["a.htm", "b.htm", "c.htm"]}, "files": []}}).encode()
                else:
                    acc = next(a for a in ACCESSIONS if a in self.path)
                    page = index_html if index_html is not None else (f'<div class="formGrouping"><div class="infoHead">Accepted</div><div class="info">{accepted(RAW[ACCESSIONS.index(acc)])}</div></div>')
                    body = f"<html><body>{page}</body></html>".encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def args(self, tmp_path, *extra):
        b = f"http://127.0.0.1:{self.srv.server_port}"
        return ["--cik", "9999991", "--n", "3", "--base-url", b + "/submissions/", "--index-base-url", b + "/data/", "--cache-dir", str(tmp_path / "cache"),
                "--min-interval", "0.1", *extra]


@pytest.mark.parametrize("truth, expect", [("utc", "UTC label AGREES"), ("ny", "NEW YORK time")])
def test_smoke_edgar_reports_which_reading_agrees_and_writes_a_valid_fixture(tmp_path, truth, expect):
    m = EdgarMock(truth)
    try:
        fx = tmp_path / "fx.json"
        r = run("smoke_edgar.py", m.args(tmp_path, "--write-fixture", str(fx)), {"ASOF_SEC_USER_AGENT": UA})
    finally:
        m.srv.shutdown()
    assert r.returncode == 0, r.stderr + r.stdout
    assert r.stdout.count(expect) == 3 and RAW[0] in r.stdout and "AsOf default" in r.stdout
    assert all(ua == UA for _, ua in m.requests) and len(m.requests) == 4                    # 1 submissions + 3 index pages
    filings = json.loads(fx.read_text())["filings"]
    assert [f["accession"] for f in filings] == ACCESSIONS and all(set(f) >= {"acceptance_datetime_raw", "accepted_new_york", "cik", "form"} for f in filings)
    # the fixture it wrote satisfies the safety property the real-filings test asserts: the default reading is never earlier than EDGAR's own accepted time
    from asof import sources
    default = sources.SECFilings(UA, [1], fetch=lambda u, a: b"{}", cache_dir=None)
    for f in filings:
        truth_utc = datetime.fromisoformat(f["accepted_new_york"]).replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
        assert default.acceptance_to_utc(f["acceptance_datetime_raw"])[0] >= truth_utc
    assert ("All filings agree with the UTC reading" in r.stdout) is (truth == "utc")


def test_smoke_edgar_does_not_guess_when_the_index_page_cannot_be_parsed(tmp_path):
    m = EdgarMock("utc", index_html="<p>layout changed</p>")
    try:
        fx = tmp_path / "fx.json"
        r = run("smoke_edgar.py", m.args(tmp_path, "--write-fixture", str(fx)), {"ASOF_SEC_USER_AGENT": UA})
    finally:
        m.srv.shutdown()
    assert r.returncode == 0 and r.stdout.count("could not parse the index page") == 3 and "AGREES" not in r.stdout
    assert json.loads(fx.read_text())["filings"] == []                                      # nothing unverified is written as a fixture


def test_smoke_edgar_requires_the_users_own_user_agent(tmp_path):
    r = run("smoke_edgar.py", ["--cik", "9999991", "--cache-dir", str(tmp_path)])
    assert r.returncode == 2 and "User-Agent" in r.stderr
    r = run("smoke_edgar.py", ["--cik", "9999991", "--cache-dir", str(tmp_path), "--user-agent", "<set by user>"])
    assert r.returncode == 2


def test_live_test_sends_nothing_without_both_keys_and_a_model(tmp_path):
    r = run("live_test.py", ["--out", str(tmp_path / "o")])
    assert r.returncode == 2 and "ANTHROPIC_API_KEY is not set. Nothing was sent." in r.stderr and not (tmp_path / "o").exists()
    r = run("live_test.py", ["--out", str(tmp_path / "o"), "--only", "openai"], {"OPENAI_API_KEY": DUMMY, "OPENAI_TEST_MODEL": ""})
    assert r.returncode == 2 and "no model for openai" in r.stderr and "Nothing was sent." in r.stderr and DUMMY not in r.stdout + r.stderr and not (tmp_path / "o").exists()


def test_live_test_against_a_stand_in_reports_each_step_and_fails_when_the_provider_does_not_reject_what_it_should(model_mock, tmp_path):
    """The stand-in answers 200 to everything, so the two error steps must be reported as failures (a provider that accepts a model that does not exist, or a wrong key, is not a pass),
    while the ordinary call, the tool schemas, the ledger and the search for the key pass. The run with real keys is the user's; this only proves the script judges honestly."""
    r = run("live_test.py", ["--out", str(tmp_path / "o"), "--anthropic-model", "m", "--openai-model", "m", "--anthropic-base-url", model_mock.base, "--openai-base-url", model_mock.base + "/v1"],
            {"ANTHROPIC_API_KEY": DUMMY, "OPENAI_API_KEY": DUMMY + "2"})
    out = r.stdout
    assert r.returncode == 1 and "RESULT: FAIL (10 check(s))" in out, out + r.stderr       # per provider: two error steps (two checks each) and no tool call from a stand-in that only talks
    for provider in ("anthropic", "openai"):
        assert f"PASS  {provider}: call succeeded" in out and f"PASS  {provider}: the provider accepts the MCP tool schemas as they are" in out
        assert f"FAIL  {provider}: a real provider error was returned as a GatewayError" in out and f"FAIL  {provider}: a wrong key is rejected by the provider" in out
    assert "PASS  ledger verifies with its anchor" in out and "PASS  no key, wrong key or key ending in the ledger file" in out
    assert DUMMY not in out + r.stderr and DUMMY not in (tmp_path / "o" / "report.json").read_text() and DUMMY.encode() not in (tmp_path / "o" / "ledger.db").read_bytes()
    report = json.loads((tmp_path / "o" / "report.json").read_text())
    assert report["providers"][0]["tools"]["nullable_arguments"] == {"advance_clock": ["to", "by"], "query": ["filters", "lookback", "limit"], "tool_call": ["arguments"]}
    sent = [b for path, _, b in model_mock.seen if b.get("tools")]
    assert len(sent) == 3 and "input_schema" in sent[0]["tools"][0] and sent[1]["tools"][0]["type"] == "function" and sent[2]["tools"][0]["function"]["strict"] is True


def test_the_smoke_scripts_contain_no_keys_and_no_hardcoded_contact_details():
    import re
    for f in SCRIPTS.glob("*.py"):
        text = f.read_text()
        assert not re.search(r"sk-[A-Za-z0-9]{20,}", text), f.name
        for m in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
            assert m.endswith(("@example.org", "@example.com")) or "your-domain" in m, (f.name, m)


# ------------------------------------------------------------------ failures exit non-zero (the user's own runs must never "pass" silently)
@pytest.mark.parametrize("script, key_env, base_suffix", [("smoke_anthropic.py", "ANTHROPIC_API_KEY", ""), ("smoke_openai.py", "OPENAI_API_KEY", "/v1")])
def test_a_passing_smoke_run_says_so(model_mock, tmp_path, script, key_env, base_suffix):
    r = run(script, ["--model", "m", "--base-url", model_mock.base + base_suffix, "--ledger", str(tmp_path / "l.db")], {key_env: DUMMY})
    assert r.returncode == 0 and "RESULT: PASS" in r.stdout and "python -m asof receipt " in r.stdout


@pytest.mark.parametrize("script, key_env, base_suffix", [("smoke_anthropic.py", "ANTHROPIC_API_KEY", ""), ("smoke_openai.py", "OPENAI_API_KEY", "/v1")])
def test_a_rejected_call_exits_non_zero_with_the_reason_and_never_prints_the_key(tmp_path, script, key_env, base_suffix):
    m = ModelMock("http401")
    try:
        r = run(script, ["--model", "m", "--base-url", m.base + base_suffix, "--ledger", str(tmp_path / "l.db")], {key_env: DUMMY})
    finally:
        m.srv.shutdown()
    assert r.returncode == 1 and "RESULT: FAIL" in r.stderr and "the model call failed" in r.stderr and "HTTP 401" in r.stderr
    assert DUMMY not in r.stdout + r.stderr and DUMMY.encode() not in (tmp_path / "l.db").read_bytes()               # even a provider that echoes the key does not leak it
    assert "verifies: True" in r.stdout                                                                           # the failed call is still a clean, verifiable ledger entry


@pytest.mark.parametrize("script, key_env, base_suffix", [("smoke_anthropic.py", "ANTHROPIC_API_KEY", ""), ("smoke_openai.py", "OPENAI_API_KEY", "/v1")])
def test_an_empty_reply_is_a_failure(tmp_path, script, key_env, base_suffix):
    m = ModelMock("empty")
    try:
        r = run(script, ["--model", "m", "--base-url", m.base + base_suffix, "--ledger", str(tmp_path / "l.db")], {key_env: DUMMY})
    finally:
        m.srv.shutdown()
    assert r.returncode == 1 and "the model returned an empty reply" in r.stderr


def test_an_unreachable_provider_is_a_failure_not_a_traceback(tmp_path):
    r = run("smoke_openai.py", ["--model", "m", "--base-url", "http://127.0.0.1:9/v1", "--ledger", str(tmp_path / "l.db")], {"OPENAI_API_KEY": DUMMY})
    assert r.returncode == 1 and "RESULT: FAIL" in r.stderr and "could not reach" in r.stderr and "Traceback" not in r.stderr and DUMMY not in r.stdout + r.stderr


def test_a_ledger_that_fails_verification_fails_the_smoke_run(model_mock, tmp_path, monkeypatch):
    # run the shared function in-process with a ledger whose chain is broken after the call: the exit code must be 1
    sys.path.insert(0, str(SCRIPTS))
    import _smoke_common as sc
    import argparse
    from asof import Ledger
    monkeypatch.setenv("OPENAI_API_KEY", DUMMY)
    real_verify = Ledger.verify

    class Broken:
        ok, n_events, anchor, reason, first_bad_seq = False, 3, "failed", "event 2 was altered", 2

    monkeypatch.setattr(Ledger, "verify", lambda self, *a, **k: Broken if self.path.name == "l.db" else real_verify(self, *a, **k))
    ap = argparse.ArgumentParser()
    sc.add_model_args(ap)
    a = ap.parse_args(["--model", "m", "--base-url", model_mock.base + "/v1", "--ledger", str(tmp_path / "l.db")])
    assert sc.run_model_smoke("openai", "OPENAI_API_KEY", a) == 1


# ------------------------------------------------------------------ smoke_edgar prints everything needed to check by hand
def test_smoke_edgar_prints_accession_raw_time_converted_utc_and_the_index_url_for_each_filing(tmp_path):
    m = EdgarMock("utc")
    try:
        r = run("smoke_edgar.py", m.args(tmp_path), {"ASOF_SEC_USER_AGENT": UA})
        base = f"http://127.0.0.1:{m.srv.server_port}/data"
    finally:
        m.srv.shutdown()
    assert r.returncode == 0, r.stderr
    block = r.stdout.split("CHECK BY HAND")[1]
    for acc, raw in zip(ACCESSIONS, RAW):
        assert f"accession {acc}" in block and f"submissions JSON): {raw}" in block
        assert f"{base}/9999991/{acc.replace('-', '')}/{acc}-index.htm" in block                    # the page to open
    assert block.count("AsOf available_at (UTC), default reading") == 3 and "US Eastern time" in block
    # the UTC the block prints is the library's own conversion of the raw string (digits read as New York time, errs late)
    from asof import sources
    default = sources.SECFilings(UA, [1], fetch=lambda u, a: b"{}", cache_dir=None)
    for raw in RAW:
        assert default.acceptance_to_utc(raw)[0].strftime("%Y-%m-%dT%H:%M:%SZ") in block


def test_smoke_edgar_no_index_makes_one_request_and_leaves_the_comparison_to_you(tmp_path):
    m = EdgarMock("utc")
    try:
        r = run("smoke_edgar.py", m.args(tmp_path, "--no-index"), {"ASOF_SEC_USER_AGENT": UA})
        n = len(m.requests)
    finally:
        m.srv.shutdown()
    assert r.returncode == 0 and n == 1 and r.stdout.count("compare by hand with the URL below") == 3 and "AGREES" not in r.stdout
    assert all(f"{a.replace('-', '')}/{a}-index.htm" in r.stdout for a in ACCESSIONS)


def _smoke_args(model_mock, tmp_path):
    sys.path.insert(0, str(SCRIPTS))
    import argparse

    import _smoke_common as sc
    ap = argparse.ArgumentParser()
    sc.add_model_args(ap)
    return sc, ap.parse_args(["--model", "m", "--base-url", model_mock.base + "/v1", "--ledger", str(tmp_path / "l.db")])


def test_a_key_found_in_the_ledger_file_fails_the_run(model_mock, tmp_path, monkeypatch, capsys):
    sc, a = _smoke_args(model_mock, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", DUMMY)
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: DUMMY.encode() if self.name == "l.db" else real(self))       # as if the key had been written to the file
    assert sc.run_model_smoke("openai", "OPENAI_API_KEY", a) == 1
    assert "the API key is in the ledger file" in capsys.readouterr().err


def test_a_broken_chain_alone_fails_the_run_even_if_the_anchor_reads_ok(model_mock, tmp_path, monkeypatch, capsys):
    sc, a = _smoke_args(model_mock, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", DUMMY)
    from asof import Ledger

    class V:
        ok, n_events, anchor, reason, first_bad_seq = False, 3, "ok", "event 2 was altered", 2

    real = Ledger.verify
    monkeypatch.setattr(Ledger, "verify", lambda self, *x, **k: V if self.path.name == "l.db" else real(self, *x, **k))
    assert sc.run_model_smoke("openai", "OPENAI_API_KEY", a) == 1
    assert "the ledger does not verify: event 2 was altered" in capsys.readouterr().err


# ------------------------------------------------------------------ tamper_demo.sh and verify_all.sh
def sh(script, *args, env=None):
    e = {**os.environ, "PYTHON": sys.executable, **(env or {})}
    return subprocess.run(["sh", str(SCRIPTS / script), *args], capture_output=True, text=True, timeout=300, env=e, cwd=ROOT)


def test_tamper_demo_alters_one_byte_of_a_copy_catches_it_and_restores_it():
    r = sh("tamper_demo.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert re.search(r"1\. the copy, untouched\n\s+OK: \d+ events", out) and re.search(r"2\. one byte .* byte \d+ -> \d+\n\s+FAIL: .*\(at event \d+\)", out)
    assert re.search(r"3\. put the byte back\n\s+OK: ", out) and "byte-identical to the original" in out and "the original ledger is unchanged" in out and "PASS:" in out


def test_tamper_demo_never_writes_to_your_ledger(tmp_path):
    from asof import Session, sources
    (tmp_path / "p.csv").write_text("symbol,date,close,available_at\n" + "".join(f"S,2023-01-{d:02d},{d}.0,2023-01-{d:02d}T21:00:00Z\n" for d in range(1, 29)))
    with Session("2023-01-21T14:00:00Z", ledger_path=tmp_path / "mine.db") as s:                          # enough events that SQLite splits pages (stale copies exist in dead space)
        s.add_source(sources.CSV(tmp_path / "p.csv", event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))
        for _ in range(3):
            s.query("p", filters={"symbol": "S"})
    import hashlib
    before = hashlib.sha256((tmp_path / "mine.db").read_bytes()).hexdigest()
    mtime = (tmp_path / "mine.db").stat().st_mtime_ns
    r = sh("tamper_demo.sh", "--ledger", str(tmp_path / "mine.db"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert hashlib.sha256((tmp_path / "mine.db").read_bytes()).hexdigest() == before and (tmp_path / "mine.db").stat().st_mtime_ns == mtime and before in r.stdout


def test_tamper_demo_fails_loudly_if_tampering_would_go_unnoticed(tmp_path):
    # a verifier that always says OK: the script must notice that the altered copy "still verifies" and exit 1
    fake = tmp_path / "fakepy"
    fake.write_text(f'#!/bin/sh\nif [ "$1 $2 $3" = "-m asof verify" ]; then echo "OK: 1 events, hash chain intact, tail anchor: ok"; exit 0; fi\nexec {sys.executable} "$@"\n')
    fake.chmod(0o755)
    r = sh("tamper_demo.sh", env={"PYTHON": str(fake)})
    assert r.returncode == 1 and "FAIL" in r.stdout and ("STILL VERIFIES" in r.stdout or "found no payload value" in r.stdout)


def test_verify_all_prints_the_exact_next_commands_and_never_a_key(tmp_path):
    canary = "sk-" + "ant-VERIFYALLCANARY0123456789abcdef"
    r = sh("verify_all.sh", "--print-only", env={"ANTHROPIC_API_KEY": canary, "OPENAI_API_KEY": "", "ASOF_SEC_USER_AGENT": ""})
    out = r.stdout
    assert r.returncode == 0 and canary not in out + r.stderr
    assert "ANTHROPIC_API_KEY: set" in out and "OPENAI_API_KEY: NOT set" in out and "ASOF_SEC_USER_AGENT: NOT set" in out
    for cmd in ("scripts/smoke_edgar.py --cik <CIK of a company you choose> --n 3", "scripts/smoke_anthropic.py --model", "scripts/smoke_openai.py --model", "docs/CLAUDE_DESKTOP.md", "demo/run_demo.py"):
        assert cmd in out, cmd
    assert "your-email@your-domain" in out and not re.search(r"[\w.+-]+@(?!your-domain)[\w-]+\.[a-z]{2,}", out)          # a placeholder, never a real address
    assert "export ANTHROPIC_API_KEY=...  " in out and "(--print-only: no checks were run)" in out


def test_verify_all_reports_a_failing_check_in_its_exit_code(tmp_path):
    # no uv on PATH and a python that fails pytest: the matrix cannot run, so the script must not claim success
    fake = tmp_path / "fakepy"
    fake.write_text(f'#!/bin/sh\nif [ "$1 $2" = "-m pytest" ]; then echo "1 failed"; exit 1; fi\nexec {sys.executable} "$@"\n')
    fake.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("sh", "cat", "mktemp", "dirname", "uname", "tail", "head", "cut", "sed", "tr", "od", "dd", "grep", "shasum", "cp", "rm", "printf", "sort", "mkdir", "basename"):
        found = shutil.which(tool)
        if found:
            (bindir / tool).symlink_to(found)
    r = sh("verify_all.sh", env={"PYTHON": str(fake), "PATH": str(bindir)})
    assert r.returncode == 1 and "SOME CHECKS FAILED" in r.stdout and "ALL KEYLESS CHECKS PASSED" not in r.stdout
