"""The pitch demo (demo/run_demo.py) is part of the product's promise, so it is tested: it must run clean offline, touch no network, print the counts it promises, catch its own
tampering at the right event, never echo a key, and be repeatable (a second run continues the same ledger chain)."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo" / "run_demo.py"

# runs the demo with every way of opening a network connection made to raise, so "no network" is proven, not assumed
GUARD = """
import runpy, socket, sys
def refuse(*a, **k): raise RuntimeError("the demo tried to use the network")
socket.socket.connect = refuse
socket.create_connection = refuse
socket.getaddrinfo = refuse
sys.argv = ["run_demo.py"] + sys.argv[1:]
runpy.run_path(%r, run_name="__main__")
""" % str(DEMO)


def run(tmp_path, *args, env=None, guard=True):
    import os
    e = {**os.environ, "PYTHONPATH": str(ROOT)}
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ASOF_SYNC_API_KEY", "ASOF_SYNC_ENDPOINT", "ASOF_DEMO_MODEL"):
        e.pop(k, None)
    e.update(env or {})
    return subprocess.run([sys.executable, "-c", GUARD if guard else GUARD.replace("socket.socket.connect = refuse\nsocket.create_connection = refuse\nsocket.getaddrinfo = refuse\n", ""), "--ledger", str(tmp_path / "ledger.db"), "--out", str(tmp_path / "out"), *args], capture_output=True, text=True, env=e, cwd=ROOT, timeout=120)


def test_the_offline_demo_runs_clean_and_says_what_it_promises(tmp_path):
    p = run(tmp_path, "--no-network")
    out = p.stdout
    assert p.returncode == 0, out + p.stderr
    assert "EVERYTHING BELOW IS SYNTHETIC" in out and out.count("[SYNTHETIC]") >= 4
    assert re.search(r"news tool call\n\s+served   3  \(3 with declared timing, 0 unknown\)\n\s+blocked  1  \(future=1\)", out), out        # step 3: served 3, blocked 1
    assert re.search(r"after the clock moved\n\s+served   4 .*\n\s+blocked  0\n", out), out                                                  # step 4: the article is now served
    assert "model memory label: unknown" in out and "declared cutoff: unknown" in out and "contamination score: none registered" in out
    assert re.search(r"ledger: OK: \d+ events, hash chain intact", out)
    target = int(re.search(r"event (\d+) is the block that recorded the hidden article", out).group(1))
    assert f"FAIL: payload does not match its recorded hash (the payload was altered) (at event {target})" in out                          # step 5: found at the edited event
    assert "(exit code 1)" in out and "skipped: --no-network" in out and "SELF-CHECK FAILED" not in out


def test_the_demo_touches_no_network_and_ignores_keys_in_the_environment(tmp_path):
    env = {"ANTHROPIC_API_KEY": "sk-" + "ant-demo-should-never-be-used-0000", "ASOF_SYNC_API_KEY": "asof_" + "x" * 43, "ASOF_DEMO_MODEL": "anthropic:some-model"}
    p = run(tmp_path, "--no-network", env=env)
    assert p.returncode == 0, p.stdout + p.stderr                                                                   # the guard raises on any connection attempt
    assert "sk-ant-demo" not in p.stdout + p.stderr and "x" * 43 not in p.stdout + p.stderr
    assert "standin:scripted" in p.stdout and "anthropic:some-model" not in p.stdout                                  # no real model even though one was requested


def test_without_a_key_the_sync_step_is_skipped_with_instructions_and_the_demo_still_passes(tmp_path):
    p = run(tmp_path)                                                                                                # default mode, no key in the environment
    assert p.returncode == 0, p.stdout + p.stderr
    assert "skipped: no ASOF_SYNC_API_KEY" in p.stdout and "Settings > Connections > AsOf clients" in p.stdout


def test_a_failing_sync_is_reported_and_does_not_break_the_demo(tmp_path):
    # nothing listens on port 9: a real connection refusal, as an unreachable Strathora would give (no guard here, since a refused connection is the point)
    p = run(tmp_path, "--sync", "http://localhost:9/api/asof/sync", env={"ASOF_SYNC_API_KEY": "asof_" + "y" * 43}, guard=False)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "NOT SYNCED" in p.stdout and "y" * 43 not in p.stdout + p.stderr


def test_running_it_again_continues_the_same_ledger_chain(tmp_path):
    a, b = run(tmp_path, "--no-network"), run(tmp_path, "--no-network")
    n1 = int(re.search(r"ledger: OK: (\d+) events", a.stdout).group(1))
    n2 = int(re.search(r"ledger: OK: (\d+) events", b.stdout).group(1))
    assert a.returncode == b.returncode == 0 and n2 == 2 * n1                                                        # one chain, two sessions
    t1 = int(re.search(r"event (\d+) is the block", a.stdout).group(1))
    t2 = int(re.search(r"event (\d+) is the block", b.stdout).group(1))
    assert t2 == t1 + n1                                                                                             # the second run's block event is where it should be


def test_all_demo_data_is_labelled_synthetic():
    news = json.loads((ROOT / "demo" / "data" / "synthetic_news.json").read_text())
    assert news and all(n["title"].startswith("[SYNTHETIC]") for n in news)
    assert (ROOT / "demo" / "data" / "synthetic_prices.csv").read_text().splitlines()[1].startswith("ACME-SYN,")
    assert "SYNTHETIC" in DEMO.read_text().splitlines()[1]


def test_the_untouched_ledger_verifies_and_the_tampered_copy_does_not(tmp_path):
    run(tmp_path, "--no-network")
    ok = subprocess.run([sys.executable, "-m", "asof", "verify", "--ledger", str(tmp_path / "ledger.db")], capture_output=True, text=True, cwd=ROOT)
    bad = subprocess.run([sys.executable, "-m", "asof", "verify", "--ledger", str(tmp_path / "out" / "tampered_ledger.db")], capture_output=True, text=True, cwd=ROOT)
    assert ok.returncode == 0 and ok.stdout.startswith("OK:") and bad.returncode == 1 and bad.stderr.startswith("FAIL:")


@pytest.mark.skipif(sys.version_info < (3, 10), reason="the demo uses 3.10+ syntax in annotations only at runtime via __future__; kept simple")
def test_the_demo_file_compiles_on_the_supported_pythons():
    compile(DEMO.read_text(), str(DEMO), "exec")


def _normalise(text):
    text = re.sub(r"session [0-9a-f]{16}", "session <session-id>", text)
    text = re.sub(r"hash [0-9a-f]{16}\.\.\.", "hash <hash>...", text)
    text = re.sub(r"\(/[^)]*ledger\.db\)", "(<ledger>)", text)
    return re.sub(r"Files: \S+", "Files: <out>", text)


def test_the_readme_shows_the_demos_real_output(tmp_path):
    readme = (ROOT / "docs" / "REFERENCE.md").read_text()                                                          # the reference page (it was the README until README v1)
    m = re.search(r"<!-- demo-output:start -->\s*```text\n(.*?)```\s*<!-- demo-output:end -->", readme, re.S)
    assert m, "docs/REFERENCE.md has no demo-output block"
    p = run(tmp_path, "--no-network")                                                                              # a fresh ledger, so event numbers are those of a first run
    assert p.returncode == 0, p.stdout + p.stderr
    assert _normalise(p.stdout).strip() == m.group(1).strip(), "the README's expected output is stale: regenerate it from `python demo/run_demo.py --no-network`"
