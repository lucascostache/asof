"""F: the documentation is executable. The reference's quickstarts are run as written, and its claims are checked against the code.

The page checked here is docs/REFERENCE.md: it was the README until the short README v1 replaced it (that one is checked in test_readme.py). The name ``README`` below is kept
for the reference text so the checks read as they always did."""
import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import Client                                          # noqa: E402
from mcp.client.stdio import StdioServerParameters             # noqa: E402

from asof.cli import build_parser, main                         # noqa: E402
from asof.presets import backtest_safe_agent, earnings_analysis, time_locked_research   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "docs" / "REFERENCE.md").read_text()
PYPROJECT = (ROOT / "pyproject.toml").read_text()


def readme_block(name):
    m = re.search(rf"<!-- {name}:start -->\s*```\w*\n(.*?)```\s*<!-- {name}:end -->", README, re.S)
    assert m, name
    return m.group(1)


def test_the_mcp_quickstart_works_exactly_as_the_readme_describes(tmp_path, capsys):
    led, demo = tmp_path / "home" / "ledger.db", tmp_path / "asof-demo"
    assert main(["init", "--ledger", str(led), "--demo", str(demo)]) == 0                                # step 1: strathora init --demo
    printed = capsys.readouterr().out
    real = json.loads(printed[printed.index("{\n"):])
    doc = json.loads(readme_block("claude-desktop-config"))                                               # step 2: the README's config block
    # the README's block has the same shape as what `strathora init` prints
    assert doc["mcpServers"]["asof"]["args"][:3] == real["mcpServers"]["asof"]["args"][:3] == ["-m", "asof", "serve-mcp"]
    assert set(doc["mcpServers"]["asof"]) == set(real["mcpServers"]["asof"]) == {"command", "args"}
    srv = doc["mcpServers"]["asof"]
    command = srv["command"].replace("/ABSOLUTE/PATH/TO/python", sys.executable)
    args = [a.replace("/ABSOLUTE/PATH/TO/asof-demo/asof.config.json", str(demo / "asof.config.json")) for a in srv["args"]]
    assert "/ABSOLUTE" not in " ".join(args)

    async def session(c):
        names = sorted(t.name for t in (await c.list_tools()).tools)
        q = (await c.call_tool("query", {"source": "prices", "filters": {"symbol": "ACME-SYN"}})).structured_content
        denied = await c.call_tool("advance_clock", {"by": "1d"})
        return names, q, denied

    async def go():
        async with Client(StdioServerParameters(command=command, args=args)) as c:
            return await session(c)
    names, q, denied = asyncio.run(go())                                                                  # step 3: ask
    assert [r["payload"]["close"] for r in q["records"]] == [147.0, 148.0, 150.0] and q["as_of"] == "2023-01-21T14:00:00Z"
    assert "147.0, 148.0 and 150.0" in README                                                             # what the README promises
    assert denied.is_error and "owned by the operator" in denied.content[0].text                         # "asking the model to advance the clock is refused"
    for t in ("set_clock", "advance_clock", "query", "tool_call", "get_receipt", "verify_ledger", "ledger_head"):
        assert t in names and f"`{t}`" in README
    # step 4: check the evidence, with the commands the README gives
    assert main(["verify", "--ledger", str(led)]) == 0
    out = capsys.readouterr().out
    assert re.fullmatch(r"OK: \d+ events, hash chain intact, tail anchor: ok\n?", out)
    assert main(["head", "--ledger", str(led)]) == 0
    head = json.loads(capsys.readouterr().out)
    assert set(head) >= {"seq", "hash", "recorded_at"}
    assert main(["receipt", q["receipt_id"], "--ledger", str(led)]) == 0
    assert "served 3" in capsys.readouterr().out
    for cmd in ("strathora verify", "strathora head", "strathora receipt <receipt_id>"):
        assert cmd in README


def test_every_preset_rule_appears_in_the_readme_verbatim():
    for p in (time_locked_research(), backtest_safe_agent(), earnings_analysis("2023-11-02T21:00:00Z")):
        assert f"`{p.name}" in README
        for rule in p.rules:
            assert f"* {rule}" in README, (p.name, rule)


def test_the_readme_documents_every_cli_command_extra_and_script():
    sub = next(a for a in build_parser()._actions if a.dest == "cmd")
    for cmd in set(sub.choices) | {"serve-mcp"}:
        assert f"strathora {cmd}" in README, cmd
    extras = re.search(r"\[project.optional-dependencies\](.*?)(?:\n\[|\Z)", PYPROJECT, re.S).group(1)
    for extra in re.findall(r"^(\w+) = \[", extras, re.M):
        if extra != "dev":
            assert f"strathora-asof[{extra}]" in README or f"'.[{extra}]'" in README or f"[{extra}]" in README, extra
    for f in sorted((ROOT / "scripts").glob("smoke_*.py")):
        assert f.name in README, f.name


def test_the_readme_states_what_asof_does_not_prove():
    section = README[README.index("## What AsOf does NOT prove"):README.index("## Python versions and CI")].lower()
    for needle in ("closed-model memory", "quality of user-supplied timestamps", "truncation, without a trusted head anchor", "only as trustworthy as where it is stored",
                   "observed"):
        assert needle in section, needle


def test_the_readme_is_honest_about_what_was_not_verified():
    low = README.lower()
    for needle in ("the author has not confirmed which is right against live edgar", "is **skipped**", "**windows has not been run.**", "the author has not run them against the live services",
                   "no deployed strathora endpoint exists yet"):
        assert needle in low, needle


def test_the_readme_python_matrix_matches_pyproject_and_ci():
    assert 'requires-python = ">=3.10"' in PYPROJECT
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    for v in ("3.10", "3.11", "3.12", "3.13", "3.14"):
        assert f'"{v}"' in ci, v


def test_the_ci_workflow_runs_every_step_on_linux_always_and_on_macos_on_main_by_hand_and_weekly():
    yaml = pytest.importorskip("yaml")
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    w = yaml.safe_load(text)
    on = w.get("on", w.get(True))                                                                   # YAML 1.1 reads a bare `on:` as True
    assert set(on) == {"push", "pull_request", "workflow_dispatch", "schedule"} and w["permissions"] == {"contents": "read"}
    assert on["schedule"] == [{"cron": "0 6 * * 1"}]                                                # weekly
    assert set(w["jobs"]) == {"secret-scan", "test-linux", "test-macos"}
    linux, mac = w["jobs"]["test-linux"], w["jobs"]["test-macos"]
    assert linux["runs-on"] == "ubuntu-latest" and mac["runs-on"] == "macos-latest"
    assert "if" not in linux and "if" not in w["jobs"]["secret-scan"]                              # every push and pull request
    assert mac["if"] == "github.event_name == 'workflow_dispatch' || github.event_name == 'schedule' || (github.event_name == 'push' && github.ref == 'refs/heads/main')"
    assert "delete this `if:` line so macOS runs on every push and pull request again" in text      # the note for when the repository is public
    for job in (linux, mac):
        assert job["strategy"]["matrix"] == {"python-version": ["3.10", "3.11", "3.12", "3.13", "3.14"]} and job["strategy"]["fail-fast"] is False
        runs = [s.get("run", "") for s in job["steps"]]
        assert "python -m pip install -e '.[dev]'" in "\n".join(runs)                                # every extra, hypothesis included: the property tests run
        for step in ("ruff check asof scripts demo --select E9,F,B", "mypy asof --ignore-missing-imports", "python -m pytest -q", "python -m pip wheel . --no-deps -w dist"):
            assert step in runs, step
        assert {"actions/checkout@v4", "actions/setup-python@v5", "actions/setup-node@v4"} <= {s.get("uses", "") for s in job["steps"]}
    assert linux["steps"] == mac["steps"]                                                           # the same steps on both
    scan = w["jobs"]["secret-scan"]
    assert scan["steps"][0] == {"uses": "actions/checkout@v4", "with": {"fetch-depth": 0}}
    assert scan["steps"][-1]["run"] == "python scripts/secret_scan.py . --allowlist scripts/secret_scan_allowlist.txt"
    assert not any(k in json.dumps(w) for k in ("secrets.", "GITHUB_TOKEN", "pypi", "upload"))       # no secret, no publishing: it only tests
