"""README v1: short, and true. The quickstart is run exactly as it is written, the output block is compared with the real output, and the status and limits are pinned.

The long reference that used to be the README is docs/REFERENCE.md (checked in test_docs.py)."""
import re
import shlex
import subprocess
import sys
from pathlib import Path

from asof import __version__
from asof.cli import main

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()


def block(name):
    m = re.search(rf"<!-- {name}:start -->\n(?:```\w*\n)?(.*?)(?:```\n)?<!-- {name}:end -->", README, re.S)
    assert m, f"README has no {name} block"
    return m.group(1)


def asof_command(name):
    """The one `strathora ...` command in a bash block, as an argument list for the CLI."""
    lines = [ln for ln in block(name).splitlines() if ln.strip()]
    assert len(lines) == 1 and lines[0].startswith("strathora "), lines
    return shlex.split(lines[0])[1:]


def test_the_status_line_is_first_says_early_and_names_the_real_version():
    body = [ln for ln in README.splitlines() if ln.strip()]
    assert body[0] == "# AsOf"
    major, minor = __version__.split(".")[:2]
    assert major == "0", "at 1.0 the status line is no longer 'v0.x, early': rewrite it"
    assert body[1].startswith(f"**Status: v0.{minor} (0.x), early.**"), body[1]                # the first thing a reader sees after the title
    assert "nobody outside has run it yet" in body[1] and "can still change" in body[1]


def test_the_summary_is_three_lines_and_says_the_three_things():
    lines = block("summary").strip().splitlines()
    assert len(lines) == 3 and all(len(ln) < 190 for ln in lines), lines
    assert "knowable at a time `T`" in lines[0] and "cannot see the future" in lines[0]
    assert "tamper-evident local ledger" in lines[1] and "receipt" in lines[1]
    assert "no dependencies" in lines[2] and "MCP server" in lines[2] and "runs on your machine" in lines[2]
    assert README.index("<!-- summary:start -->") < README.index("## Install")               # before anything else


def test_install_is_one_pip_line_from_git_and_warns_about_the_name_on_pypi():
    section = README[README.index("## Install"):README.index("## Quickstart")]
    pips = [ln for ln in section.splitlines() if ln.startswith("pip install")]
    assert pips == ['pip install "strathora-asof[mcp]"']                                         # from PyPI, with the MCP extra
    assert "On PyPI its name is **`strathora-asof`**" in section and "do not `pip install asof`" in section and "Python 3.10 or newer" in section
    assert 'pip install "strathora-asof[mcp] @ git+https://github.com/strathora/asof"' in section          # the latest main, as the alternative
    assert "not on pypi" not in README.lower()
    assert "<REPOSITORY URL>" not in README
    # three names, said once and matching the package: what you install, what you import, what you run
    assert "The package is called `strathora-asof`; you import it as `asof` and run it as `strathora` (the older command name `asof` still works)." in section
    pyproject = (ROOT / "pyproject.toml").read_text()                                              # read as text: tomllib is not in Python 3.10
    assert re.search(r'^name = "strathora-asof"', pyproject, re.M)
    scripts = re.findall(r'^(\w+) = "([\w.:]+)"', pyproject.split("[project.scripts]")[1].split("\n[")[0], re.M)
    assert scripts == [("strathora", "asof.cli:main"), ("asof", "asof.cli:main")]                # `strathora` first, `asof` the alias, one entry point
    assert (ROOT / "asof" / "__init__.py").exists() and not (ROOT / "strathora_asof").exists()


def test_the_quickstart_runs_exactly_as_written_and_prints_exactly_what_the_readme_shows(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)                                                                   # an empty folder, as a new user has
    assert main(asof_command("quickstart-init")) == 0                                             # step 1, the README's own command
    printed = capsys.readouterr().out
    assert (tmp_path / "asof-demo" / "prices.csv").exists() and (tmp_path / "asof-demo" / "asof.config.json").exists() and '"mcpServers"' in printed
    assert len((tmp_path / "asof-demo" / "prices.csv").read_text().strip().splitlines()) == 7 and "six-row `prices.csv`" in README      # a header and six rows

    (tmp_path / "try_asof.py").write_text(block("quickstart-code"))                              # step 2, the README's own code
    assert "`try_asof.py`" in README
    r = subprocess.run([sys.executable, "try_asof.py"], capture_output=True, text=True, timeout=60, cwd=tmp_path, env={"PYTHONPATH": str(ROOT), "PATH": ""})
    assert r.returncode == 0, r.stderr
    got = re.sub(r"Receipt [0-9a-f]{12} ", "Receipt <id> ", r.stdout)
    got = re.sub(r"hash [0-9a-f]{16}\.\.\.", "hash <hash>...", got)
    assert got == block("quickstart-output"), "the README's expected output is stale"
    assert "Three closes were served and three were withheld" in README and "releases one more" in README

    assert main(asof_command("quickstart-verify")) == 0                                           # step 3, the README's own command
    out = capsys.readouterr().out.strip()
    assert f"It prints `{out}`" in README, out                                                    # the exact line, with the real event count


def test_max_listed_blocked_is_documented_with_its_real_default_and_what_lowering_it_gives_up():
    from asof import Session
    from asof.query import MAX_LISTED
    import inspect
    assert MAX_LISTED == 1000 and inspect.signature(Session).parameters["max_listed_blocked"].default == 1000
    para = README[README.index("**Long replays: `max_listed_blocked`.**"):]
    para = para[:para.index("\n\n")]
    for needle in ("up to 1,000", "`Session(..., max_listed_blocked=N)`", '`"max_listed_blocked": N` in an MCP config', "`0` names none", "always exact",
                   "how many records were withheld, not which ones", "The default stays 1,000."):
        assert needle in para, needle
    assert README.index("**Long replays") < README.index("## What it doesn't do")


def test_what_it_doesnt_do_names_the_bypass_model_memory_and_the_rest():
    start = README.index("## What it doesn't do")
    section = README[start:README.index("\n## ", start + 1)]
    bullets = [ln for ln in section.splitlines() if ln.startswith("* **")]
    assert len(bullets) == 8
    low = section.lower()
    for needle in ("loads each source into memory, whole", "docs/baseline.md", "`max_listed_blocked`", "runs on linux and macos in ci", "windows is untested"):      # the size limit, with where its numbers come from
        assert needle in low, needle
    for needle in ("shares its import name with an unrelated package", "you install `strathora-asof` and import `asof`", "a different project on pypi is also called `asof`",
                   "separate virtual environment", "`strathora --version`"):
        assert needle in low, needle
    for needle in ("it only locks what goes through it", "web search", "another mcp server", "bypass the time lock", "run at the model provider",          # bypass through other tools
                   "it cannot make a model forget", "trained on data from after `t`", "never says a model's memory is clean", "`unknown`", "`observed`",       # model memory
                   "it trusts your timestamps", "does not read text",
                   "shows tampering; it does not prevent it", "strathora head", "not signed",
                   "not a hosted service", "no network transport, no login",
                   "run against one live service, with one model", "**openai: not yet tested** with a key", "local mock servers only", "one mcp client application has been run with a model", "its chat tab, and other client applications, have not been tried",
                   "streaming is not supported", "the gateway does not retry"):
        assert needle in low, needle
    assert bullets[0].startswith("* **It only locks what goes through it.**") and bullets[1].startswith("* **It cannot make a model forget.**")      # the two that matter most come first
    assert "[What it doesn't do](#what-it-doesnt-do)" in README.split("\n## ")[0]                 # and the status line points here


def test_the_claims_about_live_services_match_the_code():
    """The README says exactly what has been run live, and docs/LIVE_TEST.md is the record. Receipts do not claim more: `provider_verified` stays False for every built-in provider."""
    from asof.gateway import VERIFIED_PROVIDERS
    assert VERIFIED_PROVIDERS == {"anthropic": False, "openai": False, "local": False}
    record = (ROOT / "docs" / "LIVE_TEST.md").read_text()
    assert "On 2026-11-11 and 2026-11-12 the Anthropic adapter was run against the live API" in README and "(docs/LIVE_TEST.md)" in README
    assert "| Anthropic | **single calls: every check passed** | 2026-11-11 | `claude-haiku-5-5` |" in record
    assert "| Anthropic | **agent loop, provider errors, a provider-side tool: every check passed** | 2026-11-12 | `claude-haiku-5-5` |" in record
    assert "| OpenAI | **not yet tested** with a key |" in record
    for limit in ("Redaction of a provider's partial echo of a key", "OpenAI in any form with a key", "An MCP client application with a model in the loop", "Other models",
                  "Errors that cannot be produced on demand or cheaply", "Streaming", "Receipts still say `provider_verified: false`"):
        assert limit in record, limit                                                             # what the runs did NOT show is written down next to what they did
    assert "what protects the clock is that the server refuses, not that the model behaved" in record      # the injection result is not oversold
    assert not re.search(r"sk-[A-Za-z0-9_\-]{12,}|req_[A-Za-z0-9]{10,}", record)                  # no key, and no provider request ids either
    status = [ln for ln in README.splitlines() if ln.strip()][1]
    assert "one model provider has been called live" in status
    for script in ("scripts/live_test.py", "scripts/live_agent_loop.py", "scripts/secret_search.py"):
        assert script in record and (ROOT / script).exists()


def test_demo_and_example_data_use_the_fictional_ticker_and_no_real_one_appears_in_the_repository():
    """Invented prices must not sit under a real company's ticker. The demo, the examples, the docs and the tests all use the fictional ACME-SYN."""
    from asof.cli import DEMO_CSV
    rows = DEMO_CSV.strip().splitlines()[1:]
    assert len(rows) == 6 and all(r.startswith("ACME-SYN,") for r in rows)
    real = ["NV" + "DA", "AA" + "PL", "MS" + "FT", "TS" + "LA", "GO" + "OG", "AM" + "ZN", "ME" + "TA,"]      # written in two halves so this file does not contain them
    hits = []
    for f in sorted(p for p in ROOT.rglob("*") if p.is_file() and p.suffix in (".py", ".md", ".json", ".jsonl", ".csv", ".sh", ".toml", ".html", ".js")
                    and not any(part.startswith(".") or part in ("build", "__pycache__") or part.endswith(".egg-info") for part in p.relative_to(ROOT).parts)):
        text = f.read_text(errors="replace")
        hits += [f"{f.relative_to(ROOT)}: {t}" for t in real if re.search(rf"\b{re.escape(t)}", text)]
    assert hits == [], hits


def test_the_readme_is_short_and_every_link_in_it_exists():
    assert len(README.splitlines()) <= 110, "README v1 is the short page: detail belongs in docs/REFERENCE.md"
    links = re.findall(r"\]\(([^)#][^)]*)\)", README)
    assert {"docs/REFERENCE.md", "docs/MCP_CLIENTS.md", "docs/pilot/INSTALL.md", "docs/pilot/LIMITATIONS.md", "SPEC.md", "LICENSE", "NOTICE"} <= set(links)
    for link in links:
        assert not link.startswith("http"), link                                                  # nothing external to rot
        assert (ROOT / link).exists(), link
    for path in re.findall(r"`((?:scripts|demo)/[\w./]+)", README):
        assert (ROOT / path).exists(), path
    ref = (ROOT / "docs" / "REFERENCE.md").read_text()
    assert "(../README.md)" in ref and "(../SPEC.md)" in ref and "](SPEC.md)" not in ref          # the moved page's links were re-pointed
