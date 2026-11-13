"""scripts/secret_scan.py --allowlist: CI fails on any finding or commit identity the allowlist does not name, and never prints or stores a value it masks."""
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCAN = ROOT / "scripts" / "secret_scan.py"
FAKE_KEY = "sk-" + "ant-" + "FAKE0TEST0VALUE0" * 2                       # built here so this file does not hold the shape it tests
HITS = ("anthropic key", "openai key")                                   # the two rules that value matches
NOREPLY = "1+someone" + "@users.noreply.github.com"                    # in two parts, so this file holds no address (tests/test_campaign_docs.py)


def repo(tmp_path, email=NOREPLY, text=f'KEY = "{FAKE_KEY}"\n'):
    d = tmp_path / "r"
    d.mkdir()
    (d / "conf.py").write_text(text)
    git = ["git", "-C", str(d), "-c", "user.name=Someone", "-c", f"user.email={email}", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "one"], check=True)
    return d


def scan(d, allow_text=None, tmp_path=None):
    cmd = [sys.executable, str(SCAN), str(d)]
    if allow_text is not None:
        a = tmp_path / "allow.txt"
        a.write_text(allow_text)
        cmd += ["--allowlist", str(a)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def entry(rule, file, value, why="made up for this test"):
    return f"{rule} | {file} | {hashlib.sha256(value.encode()).hexdigest()[:16]} | {why}\n"


def test_without_an_allowlist_it_only_reports(tmp_path):
    r = scan(repo(tmp_path))
    assert r.returncode == 0 and "[anthropic key]" in r.stdout and FAKE_KEY not in r.stdout


def test_a_finding_that_is_not_allowed_fails_and_prints_the_line_to_add_but_never_the_value(tmp_path):
    d = repo(tmp_path)
    r = scan(d, "identity | *@users.noreply.github.com\n", tmp_path)
    assert r.returncode == 1 and "NOT ALLOWED  [anthropic key]" in r.stdout and "FAIL:" in r.stdout
    assert entry("anthropic key", "conf.py", FAKE_KEY).rsplit("|", 1)[0] in r.stdout                  # the exact entry, fingerprint included
    assert FAKE_KEY not in r.stdout + r.stderr


def test_every_finding_allowed_and_a_noreply_identity_passes(tmp_path):
    d = repo(tmp_path)
    allow = "".join(entry(rule, "conf.py", FAKE_KEY) for rule in HITS)
    r = scan(d, allow + "identity | *@users.noreply.github.com\n", tmp_path)
    assert r.returncode == 0 and r.stdout.rstrip().endswith("OK: every finding and every commit identity is in the allowlist"), r.stdout
    assert FAKE_KEY not in (tmp_path / "allow.txt").read_text()                                        # the list holds fingerprints, not values


def test_an_entry_for_another_file_or_another_value_does_not_allow_it(tmp_path):
    d = repo(tmp_path)
    other_file = "".join(entry(rule, "other.py", FAKE_KEY) for rule in HITS)
    other_value = "".join(entry(rule, "conf.py", FAKE_KEY + "X") for rule in HITS)
    for allow in (other_file, other_value):
        r = scan(d, allow + "identity | *@users.noreply.github.com\n", tmp_path)
        assert r.returncode == 1 and "unused entry" in r.stdout


def test_a_commit_identity_that_is_not_allowed_fails(tmp_path):
    other = "someone" + "@company.example"                                # in two parts, so this file holds no address for the scan to find
    d = repo(tmp_path, email=other, text="nothing to see\n")
    r = scan(d, "identity | *@users.noreply.github.com\n", tmp_path)
    assert r.returncode == 1 and f"NOT ALLOWED  commit identity <{other}>" in r.stdout


def test_a_malformed_allowlist_is_refused(tmp_path):
    r = scan(repo(tmp_path, text="nothing to see\n"), "anthropic key | conf.py | not-hex | why\n", tmp_path)
    assert r.returncode == 1 and "not `rule | file | 16 hex digits | why`" in r.stderr


def test_the_repositorys_allowlist_holds_no_value_only_fingerprints_and_every_entry_says_why():
    lines = [ln for ln in (ROOT / "scripts" / "secret_scan_allowlist.txt").read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines
    for ln in lines:
        parts = [p.strip() for p in ln.split("|")]
        assert parts[0] == "identity" and parts[1].endswith("@users.noreply.github.com") or (len(parts) == 4 and len(parts[2]) == 16 and parts[3]), ln


@pytest.mark.skipif(not (ROOT / ".github" / "workflows" / "ci.yml").exists(), reason="no workflow")
def test_ci_runs_the_scan_with_the_allowlist_over_the_whole_history():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "python scripts/secret_scan.py . --allowlist scripts/secret_scan_allowlist.txt" in ci and "fetch-depth: 0" in ci
