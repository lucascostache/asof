"""docs/ALPHA_INVITE.md: what it tells an alpha tester to run is what the README quickstart and the Claude Desktop page (both tested) say, and its example runs as written."""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INVITE = (ROOT / "docs" / "ALPHA_INVITE.md").read_text()
README = (ROOT / "README.md").read_text()
DESKTOP = (ROOT / "docs" / "CLAUDE_DESKTOP.md").read_text()


def fenced(text, lang):
    return [re.sub(r"(?m)^ {3}", "", b) for b in re.findall(rf"```{lang}\n(.*?)```", text, re.S)]


def test_the_example_is_the_readme_quickstart_verbatim_and_prints_what_the_page_says(tmp_path):
    code = fenced(INVITE, "python")
    assert len(code) == 1 and code[0] in README
    (tmp_path / "try_asof.py").write_text(code[0])
    exe = Path(sys.executable).parent / "strathora"
    init = [str(exe)] if exe.exists() else [sys.executable, "-m", "asof"]
    subprocess.run([*init, "init", "--ledger", "asof-demo/ledger.db", "--demo", "asof-demo"], cwd=tmp_path, check=True, capture_output=True)
    out = subprocess.run([sys.executable, "try_asof.py"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout
    for line in ("knowable on 21 Jan: [147.0, 148.0, 150.0]", "served 3", "blocked: future=3", "knowable on 24 Jan: [147.0, 148.0, 150.0, 155.0]"):
        assert line in out and line in INVITE, line
    v = subprocess.run([*init, "verify", "--ledger", "asof-demo/ledger.db"], cwd=tmp_path, capture_output=True, text=True)
    assert v.returncode == 0 and v.stdout.startswith("OK: ") and "hash chain intact, tail anchor: ok" in v.stdout and "hash chain intact, tail anchor: ok" in INVITE


def test_the_claude_desktop_config_and_test_are_the_tested_pages():
    blocks = fenced(INVITE, "json")
    assert len(blocks) == 1
    tested = re.search(r"<!-- claude-desktop-test-config:start -->\s*```json\n(.*?)```", DESKTOP, re.S).group(1)
    assert json.loads(blocks[0]) == json.loads(tested)
    assert "cp -R docs/examples/claude_desktop ~/asof-claude-test" in INVITE and (ROOT / "docs" / "examples" / "claude_desktop" / "asof.config.json").is_file()
    assert "As of 2023-01-20, summarize news on ACME-SYN." in INVITE and "As of 2023-01-20, summarize news on ACME-SYN." in DESKTOP
    for word in ("office opening", "steady quarter", "beating estimates", "raised outlook"):
        assert word in INVITE and word in DESKTOP, word
    assert "Not yet confirmed in Claude Desktop" in INVITE and "UNVERIFIED in Claude Desktop" in DESKTOP


def test_the_install_and_issue_instructions_name_this_repository_and_warn_about_keys_and_the_other_asof():
    assert "gh repo clone lucascostache/asof" in INVITE and "gh issue create --repo lucascostache/asof" in INVITE
    assert "pip install -e '.[mcp]'" in INVITE and "do not run `pip install asof`" in INVITE
    low = INVITE.lower()
    assert "api key" in low and "a ledger stores what was served" in low
    import asof
    assert f"prints: strathora-asof {asof.__version__}" in INVITE
