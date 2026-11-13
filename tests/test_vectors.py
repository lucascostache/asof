"""spec/vectors and the two independent verifiers (Node, and Python standard library): they must agree with the library and with each other, and fail on a tampered vector."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from asof import Ledger, Session, sources
from asof.sync import SyncConfig, Syncer

ROOT = Path(__file__).resolve().parent.parent
VEC = ROOT / "spec" / "vectors"
NODE = shutil.which("node")


def py_verify(*args):
    return subprocess.run([sys.executable, str(ROOT / "verifier" / "verify.py"), *args], capture_output=True, text=True)


def node_verify(*args):
    return subprocess.run([NODE, str(ROOT / "verifier" / "verify.mjs"), *args], capture_output=True, text=True)


needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def test_the_committed_vectors_are_what_the_library_generates():
    r = subprocess.run([sys.executable, str(VEC / "generate.py"), "--check"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_vectors_cover_what_matters():
    jc = json.loads((VEC / "jcs.json").read_text())["cases"]
    names = " ".join(c["name"] + " " + c["input"] for c in jc)
    for must in ("RFC 8785 section 3.2.2", "RFC 8785 section 3.2.3", "2592000.0", "1e21", "5e-324", "astral plane", "control characters", "negative zero"):
        assert must in names, must
    ev = json.loads((VEC / "events.json").read_text())
    assert {c["schema_version"] for c in ev["event_hash"]} == {2, 3} and {c["schema_version"] for c in ev["chains"]} == {2, 3}
    floats = json.dumps(ev["chains"][0]["events"][1]["payload"])
    assert "2592000.0" in floats and ev["chains"][0]["events"][1]["payload_canonical"].count("2592000") == 1 and "2592000.0" not in ev["chains"][0]["events"][1]["payload_canonical"]


def test_the_python_verifier_passes_without_importing_asof():
    r = py_verify("vectors", str(VEC))
    assert r.returncode == 0 and r.stdout.count("PASS") == 3 and "FAIL" not in r.stdout, r.stdout
    assert "import asof" not in (ROOT / "verifier" / "verify.py").read_text() and "from asof" not in (ROOT / "verifier" / "verify.py").read_text()


@needs_node
def test_the_node_verifier_passes_and_prints_exactly_what_the_python_one_prints():
    n, p = node_verify("vectors", str(VEC)), py_verify("vectors", str(VEC))
    assert n.returncode == 0 and n.stdout.count("PASS") == 3 and "FAIL" not in n.stdout, n.stdout + n.stderr
    assert n.stdout == p.stdout                                                                           # same cases, same counts, in two languages
    assert not re.search(r"\bimport .*asof|from ['\"].*asof", (ROOT / "verifier" / "verify.mjs").read_text())


@needs_node
@pytest.mark.parametrize("mutate", [lambda d: d["cases"][0].update(canonical=d["cases"][0]["canonical"].replace("1e+30", "1e30")),
                                    lambda d: d["cases"][5].update(sha256="0" * 64)])
def test_both_verifiers_fail_on_a_tampered_jcs_vector(tmp_path, mutate):
    shutil.copytree(VEC, tmp_path / "v")
    d = json.loads((tmp_path / "v" / "jcs.json").read_text())
    mutate(d)
    (tmp_path / "v" / "jcs.json").write_text(json.dumps(d))
    for run in (py_verify, node_verify):
        r = run("vectors", str(tmp_path / "v"))
        assert r.returncode == 1 and "FAIL" in r.stdout


@needs_node
@pytest.mark.parametrize("field, value", [("hash", "f" * 64), ("payload_sha256", "e" * 64), ("prev_hash", "d" * 64), ("seq", 9)])
def test_both_verifiers_fail_on_a_tampered_chain(tmp_path, field, value):
    shutil.copytree(VEC, tmp_path / "v")
    d = json.loads((tmp_path / "v" / "events.json").read_text())
    d["chains"][0]["events"][1][field] = value
    (tmp_path / "v" / "events.json").write_text(json.dumps(d))
    for run in (py_verify, node_verify):
        r = run("vectors", str(tmp_path / "v"))
        assert r.returncode == 1 and "FAIL  chain" in r.stdout, r.stdout


def make_body(tmp_path, privacy="hashes_only"):
    (tmp_path / "p.csv").write_text("symbol,date,close,available_at\nS,2023-01-19,1.5,2023-01-19T21:00:00Z\nS,2023-01-23,2.0,2023-01-23T21:00:00Z\n")
    led = Ledger(tmp_path / "l.db")
    with Session("2023-01-21T14:00:00Z", privacy=privacy, ledger=led) as s:
        s.add_source(sources.CSV(tmp_path / "p.csv", event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))
        s.query("p", filters={"symbol": "S"}, lookback="30d")
        s.decision("hold")
    sy = Syncer(led, SyncConfig(enabled=True, endpoint="https://x.example", api_key="k", send_payloads=True))
    from asof import __version__
    from asof.sync import CANONICALIZATION
    body = {"schema": "asof.sync/2", "client": {"name": "asof", "version": __version__}, "chain": {"id": led.chain_id},
            "ledger": {"schema_version": led.schema_version, "hash_domain": led.hash_domain, "canonicalization": CANONICALIZATION[led.schema_version]}, "events": [sy.wire_event(e) for e in led.events()]}
    return body


@needs_node
def test_a_real_sync_body_from_this_library_verifies_in_node_and_python_and_tampering_is_caught(tmp_path):
    body = make_body(tmp_path)
    f = tmp_path / "body.json"
    f.write_text(json.dumps(body))                                                                         # written by Python; read by a JavaScript JSON parser in Node
    for run in (py_verify, node_verify):
        r = run("body", str(f))
        assert r.returncode == 0 and "PASS" in r.stdout and body["chain"]["id"] in r.stdout, r.stdout
    q = next(e for e in body["events"] if e["type"] == "query")
    q["payload"]["served"] = 99
    f.write_text(json.dumps(body))
    for run in (py_verify, node_verify):
        r = run("body", str(f))
        assert r.returncode == 1 and f"event {q['seq']}" in r.stdout and "payload" in r.stdout
