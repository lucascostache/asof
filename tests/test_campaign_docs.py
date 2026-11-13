"""Campaign I: the documents say what the program does.

33. every code block in every tracked Markdown file is either RUN here (or by a named test), or CHECKED against the real program (a command's arguments, a script's flags, a query's SQL),
    or listed below as not runnable with the reason. A block that is none of these fails the test, so a new block cannot arrive unchecked.
34. the licence, and the list of what each extra installs (docs/DEPENDENCIES.md).
35. nothing private in any tracked file: no service name, home folder, real address or unexpected host.
36. `strathora --help`, `--version` and every subcommand's help say what the program does.
"""
from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from asof import Session, __version__, sources
from asof.cli import DEFAULT_LEDGER, build_parser, main

ROOT = Path(__file__).resolve().parents[1]
TRACKED = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")
TRACKED = [f for f in TRACKED if f]
pytestmark = pytest.mark.skipif(not TRACKED, reason="not a git checkout: the list of tracked files is unknown")
MD = [f for f in TRACKED if f.endswith(".md")]
FENCE = re.compile(r"^[ ]{0,3}```(\w*)[^\n]*\n(.*?)^[ ]{0,3}```\s*$", re.M | re.S)


def blocks(lang: str | None = None) -> list[tuple[str, int, str, str]]:
    """(file, line, language, body) of every fenced block in every tracked Markdown file."""
    out = []
    for f in MD:
        text = (ROOT / f).read_text()
        for m in FENCE.finditer(text):
            if lang is None or (m.group(1) or "none") == lang:
                out.append((f, text[:m.start()].count("\n") + 1, m.group(1) or "none", m.group(2)))
    return out


def test_the_inventory_of_code_blocks_is_the_one_this_file_accounts_for():
    by_lang: dict[str, int] = {}
    for _, _, lang, _ in blocks():
        by_lang[lang] = by_lang.get(lang, 0) + 1
    assert by_lang == {"bash": 24, "python": 8, "json": 7, "text": 3, "none": 2, "html": 1}, by_lang       # a new block changes this line: decide how it is checked, then update it


# ------------------------------------------------------------------ 33a. JSON
def test_every_json_block_is_valid_json():
    js = blocks("json")
    assert len(js) == 7
    for f, line, _, body in js:
        try:
            json.loads(body)
        except ValueError as e:
            pytest.fail(f"{f}:{line} is not valid JSON: {e}")


# ------------------------------------------------------------------ 33b. Python
PYTHON_RUN_ELSEWHERE = {                                                                       # (file, first line of the block) -> the test that runs it exactly as written
    ("README.md", "from asof import Session, sources"): "tests/test_readme.py and scripts/clean_install_check.sh step 9",
    ("SPEC.md", "import hashlib, json, sqlite3"): "tests/test_spec.py and tests/test_schema_versions.py",
}


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ASOF_CLIENT_SECRET", raising=False)
    assert main(["init", "--ledger", "my_ledger.db", "--demo", "."]) == 0
    return tmp_path


def _python(file: str, first: str) -> str:
    hits = [b for f, _, _, b in blocks("python") if f == file and b.splitlines()[0].startswith(first)]
    assert len(hits) == 1, (file, first, len(hits))
    return hits[0]


def test_every_python_block_is_run_somewhere():
    here = {("docs/REFERENCE.md", "from asof import ModelResponse"), ("docs/REFERENCE.md", "print(s.receipt().summary())"), ("asof/widgets/README.md", 'w = session.show("receipt")'),
            ("asof/widgets/README.md", 'session.show("gap-breakdown"'), ("asof/widgets/README.md", 'session.show("quality-panel", secret'), ("asof/widgets/README.md", "from asof import widgets")}
    known = set(PYTHON_RUN_ELSEWHERE) | here
    for f, line, _, body in blocks("python"):
        first = body.splitlines()[0]
        assert any(f == kf and first.startswith(k) for kf, k in known), f"{f}:{line} is a Python block that no test runs: {first!r}"
    for (f, first), where in PYTHON_RUN_ELSEWHERE.items():
        _python(f, first)                                                                       # the block still exists
        for name in re.findall(r"tests/\w+\.py", where):
            assert (ROOT / name).exists(), name


def test_the_reference_quickstart_runs_as_written_with_a_stand_in_for_the_search_tool_and_the_model(demo, monkeypatch, capsys):
    """Two names in the block are the reader's own: `my_search_fn`, and a model id written as a placeholder. They are supplied here; every other line runs unchanged against the real library."""
    code = _python("docs/REFERENCE.md", "from asof import ModelResponse")
    assert code.count("my_search_fn") == 1 and code.count('"anthropic:<model>"') == 1
    calls = []

    def my_search_fn(query):
        return [{"title": "ACME-SYN raises guidance", "published_at": "2023-01-23T21:05:00Z"}, {"title": "from the future", "published_at": "2023-02-01T00:00:00Z"}]

    from asof import ModelResponse

    def stand_in(self, model, messages, **kw):
        calls.append((model, messages))
        return ModelResponse(text="ok")
    monkeypatch.setattr(Session, "model", stand_in)
    ns: dict = {"my_search_fn": my_search_fn}
    exec(compile(code, "docs/REFERENCE.md", "exec"), ns)                                       # ends with its own `assert s.verify_ledger().ok`
    assert [r.payload["close"] for r in ns["rows"]] == [147.0, 148.0, 150.0]
    assert [n["title"] for n in ns["news"]] == ["ACME-SYN raises guidance"]                    # the later item was dropped
    assert calls and calls[0][0] == "anthropic:<model>" and "blocked" in capsys.readouterr().out
    assert [e.type for e in ns["s"].events(type="decision")] == ["decision"]
    ns["s"].close()


def test_the_receipt_fragment_prints_the_two_data_quality_lines_it_shows(demo, capsys):
    code = _python("docs/REFERENCE.md", "print(s.receipt().summary())")
    with Session("2023-01-21T14:00:00Z", ledger_path="my_ledger.db") as s:
        s.add_source(sources.CSV("prices.csv", event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))
        s.query("prices")
        exec(compile(code, "docs/REFERENCE.md", "exec"), {"s": s})
    out = capsys.readouterr().out
    assert "  data quality: timestamps 100% event_time / 100% available_at; newest served record " in out and "  quality flags: none" in out


def test_the_four_widget_blocks_run_against_a_real_session(demo):
    fx = ROOT / "tests" / "fixtures" / "widgets"
    rows, att = json.loads((fx / "returns.json").read_text()), json.loads((fx / "attribution.json").read_text())
    with Session("2023-01-21T14:00:00Z", ledger_path="my_ledger.db") as session:
        session.add_source(sources.CSV("prices.csv", event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))
        session.query("prices")
        ns = {"session": session, "rows": rows, "att": att}
        for first in ('w = session.show("receipt")', 'session.show("gap-breakdown"', 'session.show("quality-panel", secret', "from asof import widgets"):
            code = _python("asof/widgets/README.md", first)
            if "secret=\"…\"" in code:
                code = code.replace('secret="…"', 'secret="a-client-secret-of-at-least-32-characters"')      # the block writes the secret as an ellipsis
            try:
                exec(compile(code, "asof/widgets/README.md", "exec"), ns)
            finally:
                if "widgets.register(" in code:
                    from asof import widgets
                    widgets.unregister("doubled")                                               # the README's plugin must not stay registered for the tests that run after this one
        assert "<asof-receipt" in ns["w"].html()


# ------------------------------------------------------------------ 33c. shell
NOT_RUN = {                                                                                    # the first word of a command this test does not run, and why
    "pip": "installs from the network (the real lines are run by scripts/clean_install_check.sh)", "python3": "makes a virtual environment (run by scripts/clean_install_check.sh)",
    "cd": "the reader's own folder", "cp": "copies the example folder (its contents are tested in tests/test_claude_desktop.py)", "echo": "prints a path", "export": "the reader's own keys and addresses",
    "set": "loads the reader's own key file", "npx": "another program (MCP Inspector; run by tests/test_campaign_mcp.py when it is installed)", "opencode": "another program",
    "source": "activates a virtual environment", ".": "loads the reader's own key file",
}
SCRIPT_NEEDS = {"wrap_agent.py": "docs/pilot/examples/wrap_agent.py"}


def shell_lines() -> list[tuple[str, int, str]]:
    out = []
    for f, line, lang, body in blocks():
        if lang == "bash" or (lang == "none" and body.startswith("python ")):
            for i, raw in enumerate(body.splitlines()):
                words: list[str] = []
                for tok in shlex.split(re.sub(r"<[^>]+>", "PLACEHOLDER", raw), comments=True) + ["&&"]:      # quotes and `# comments` as the shell reads them; `<...>` is the reader's own value
                    end = tok.endswith(";") and " " not in tok                                         # `set -a; . file; set +a`: three commands (a `;` inside a quoted SQL statement is not one)
                    if tok != "&&":
                        words.append(tok.rstrip(";") if end else tok)
                    if (tok == "&&" or end) and words:
                        out.append((f, line + 1 + i, shlex.join(words)))
                        words = []
    return out


def _words(cmd: str) -> list[str]:
    return shlex.split(cmd)


def test_every_shell_line_in_every_document_is_checked_or_listed_as_not_run(tmp_path):
    lines = shell_lines()
    assert len(lines) >= 55
    parser, n_cli, n_script, n_sql, skipped = build_parser(), 0, 0, 0, {}
    ledger = tmp_path / "ledger.db"
    assert main(["init", "--ledger", str(ledger)]) == 0
    for f, line, cmd in lines:
        w = _words(cmd)
        where = f"{f}:{line}: {cmd}"
        if w[0].endswith("python") and w[1:3] == ["-m", "asof"]:
            w = ["strathora"] + w[3:]
        if w[0].endswith("strathora"):                                                          # a command of ours: the real parser must accept exactly these arguments
            n_cli += 1
            if w[1] == "serve-mcp":
                assert w[2] == "--config" and len(w) == 4, where
            elif w[1:] == ["--version"]:
                pass                                                                            # checked by test_version_prints_the_distribution_name_and_the_version_in_pyproject
            else:
                try:
                    a = parser.parse_args(w[1:])
                except SystemExit:
                    pytest.fail(f"the documented command is not accepted by the program: {where}")
                assert a.cmd == w[1], where
        elif w[0].endswith("python") or w[0].endswith(".sh"):                                   # a script of ours: it exists, and it knows every flag the document shows
            n_script += 1
            name = w[0] if w[0].endswith(".sh") else w[1]
            if name == "-c":
                assert "import asof, mcp" in w[2], where
                continue
            if name == "-m":                                                                    # `python -m venv`, `python -m pytest`: other people's programs, not run from here
                assert w[2] in ("venv", "pytest"), where
                n_script -= 1
                skipped[f"python -m {w[2]}"] = skipped.get(f"python -m {w[2]}", 0) + 1
                continue
            path = ROOT / SCRIPT_NEEDS.get(name, name)
            assert path.is_file(), f"{where}: no such file {path.relative_to(ROOT)}"
            flags = [x for x in w if x.startswith("--")]
            if flags and name.endswith(".py") and name != "-m":
                help_text = path.read_text()
                for flag in flags:
                    assert f'"{flag}"' in help_text, f"{where}: {name} has no option {flag}"
        elif w[0] == "sqlite3":                                                                 # a query on the ledger: the SQL runs on a real ledger
            n_sql += 1
            c = sqlite3.connect(ledger)
            c.execute(w[2].rstrip(";")).fetchall()
            c.close()
        elif w[0] in NOT_RUN or (w[0].endswith("/pip") and w[1] == "install"):
            skipped[w[0].rsplit("/", 1)[-1]] = skipped.get(w[0].rsplit("/", 1)[-1], 0) + 1
        else:
            pytest.fail(f"a shell line this test does not know how to check: {where}")
    assert n_cli >= 18 and n_script >= 14 and n_sql == 2, (n_cli, n_script, n_sql, skipped)


def test_python_m_venv_and_pip_lines_name_the_package_the_way_the_project_does():
    for f, line, cmd in shell_lines():
        if "pip install" in cmd:
            assert re.search(r"git\+PLACEHOLDER|strathora_asof-|strathora-asof\[|-e '?\.|mcp>=2", cmd), f"{f}:{line}: {cmd}"       # never `pip install asof` (an unrelated project on PyPI)
            assert not re.search(r"pip install asof\b", cmd)


def test_the_version_in_documented_output_is_the_real_version():
    for f in MD:
        for v in re.findall(r"strathora-asof (\d+\.\d+\.\d+)", (ROOT / f).read_text()):
            assert v == __version__, f"{f} shows version {v}"
        for v in re.findall(r"strathora_asof-(\d+\.\d+\.\d+)-py3", (ROOT / f).read_text()):
            assert v == __version__, f"{f} names a wheel of version {v}"


def test_every_relative_link_in_every_document_points_at_a_tracked_file():
    tracked = set(TRACKED)
    for f in MD:
        for target in re.findall(r"\]\((?!https?://|#|mailto:)([^)#\s]+)", (ROOT / f).read_text()):
            p = (ROOT / f).parent.joinpath(target).resolve()
            rel = str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else None
            assert rel and (rel in tracked or any(t.startswith(rel.rstrip("/") + "/") for t in tracked)), f"{f} links to {target}, which is not in the repository"


# ------------------------------------------------------------------ 34. licences
def test_the_licence_is_apache_2_everywhere_it_is_stated():
    lic = (ROOT / "LICENSE").read_text()
    assert "Apache License" in lic and "Version 2.0, January 2004" in lic and "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in lic and "END OF TERMS AND CONDITIONS" in lic
    assert (ROOT / "NOTICE").read_text().startswith("AsOf\nCopyright ")
    py = (ROOT / "pyproject.toml").read_text()
    assert 'license = { text = "Apache-2.0" }' in py and "dependencies = []" in py
    assert "Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE)." in (ROOT / "README.md").read_text()
    assert {"LICENSE", "NOTICE", "docs/DEPENDENCIES.md"} <= set(TRACKED)


def test_every_declared_dependency_is_listed_with_a_licence_and_the_list_matches_what_is_installed_here():
    page = (ROOT / "docs" / "DEPENDENCIES.md").read_text()
    py = (ROOT / "pyproject.toml").read_text()
    extras = py.split("[project.optional-dependencies]")[1].split("\n[")[0]
    names = {re.split(r"[<>=!~\[]", d)[0] for d in re.findall(r'"([^"]+)"', extras)}
    assert names == {"pyarrow", "mcp", "certifi", "pytest", "hypothesis", "ruff", "mypy"}
    for n in names:
        assert f"`{n}`" in page, f"{n} is a declared dependency and is not in docs/DEPENDENCIES.md"
    assert "**The core has no dependencies.**" in page and "not a legal review" in page and "MPL-2.0" in page
    from importlib import metadata
    listed = dict(re.findall(r"^\| ([\w.-]+) \| [\w.]+ \| ([^|]+?) \|$", page, re.M))
    assert len(listed) == 27
    checked = 0
    for name, licence in listed.items():
        try:
            md = metadata.metadata(name)
        except metadata.PackageNotFoundError:
            continue                                                                            # not in this environment (the MCP extra is optional)
        declared = (md.get("License-Expression") or "").strip() or ((md.get("License") or "").strip().splitlines() or [""])[0]
        if declared:
            checked += 1
            assert declared == licence, f"{name}: the page says {licence}, the installed package says {declared}"
    if metadata.packages_distributions().get("mcp"):
        assert checked >= 20


# ------------------------------------------------------------------ 35. nothing private in a tracked file
def _texts():
    for f in TRACKED:
        try:
            yield f, (ROOT / f).read_text()
        except (UnicodeDecodeError, FileNotFoundError):
            continue


def test_no_real_company_identifier_and_no_default_company_in_the_edgar_smoke_script():
    """A real company's SEC number was the smoke script's default and appeared in mock test data. It was replaced by a made-up one (9999991); the script now needs --cik."""
    real = "3201" + "93"                                                                          # in two parts, so this file does not hold it either
    for f, text in _texts():
        assert real not in text, f"{f} holds a real company's SEC number"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke_edgar.py")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "--cik" in r.stderr and "required" in r.stderr                  # no company is chosen for the user


ALLOWED_HOSTS = {"127.0.0.1", "localhost", "www.sec.gov", "data.sec.gov", "api.openai.com", "api.anthropic.com", "platform.openai.com", "api.meta.ai", "www.apache.org", "host", "x", "svc", "..."}


def test_no_tracked_file_names_the_service_a_home_folder_or_a_real_address():
    service = "strat" + "ton"                                                                   # written in two parts so this file does not match itself
    mail = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[a-z]{2,}")
    for f, text in _texts():
        assert service not in text.lower(), f"{f} names the service"
        assert not re.search(r"/Users/[A-Za-z]|/home/[a-z]+/|C:\\\\Users", text), f"{f} holds a home folder path"
        for addr in mail.findall(text):
            host = addr.rsplit("@", 1)[1]
            assert host.endswith((".example", ".example.org", ".example.com", ".invalid")) or host in ("example.org", "example.com"), f"{f} holds an address that is not a placeholder: {addr}"


def test_every_host_named_in_a_tracked_file_is_a_placeholder_a_local_address_or_a_documented_public_service():
    seen = set()
    for f, text in _texts():
        for host in re.findall(r"https?://([A-Za-z0-9.-]+|\.\.\.)", text):
            host = host.rstrip(".") or "..."
            seen.add(host)
            ok = host in ALLOWED_HOSTS or host.endswith((".example", ".invalid", ".example.org", ".example.com")) or host in ("example.org", "example.com")
            assert ok, f"{f} names the host {host}"
    assert {"api.anthropic.com", "api.openai.com", "data.sec.gov"} <= seen                     # the three services the library can call, and nothing internal


# ------------------------------------------------------------------ 36. the command's own help
COMMANDS = ["init", "verify", "report", "head", "receipt", "sync"]


def _help(*args: str) -> str:
    r = subprocess.run([sys.executable, "-m", "asof", *args, "--help"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stderr == "", r.stderr
    return r.stdout


def test_version_prints_the_distribution_name_and_the_version_in_pyproject():
    r = subprocess.run([sys.executable, "-m", "asof", "--version"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout == f"strathora-asof {__version__}\n"
    assert re.search(rf'^version = "{re.escape(__version__)}"', (ROOT / "pyproject.toml").read_text(), re.M)


def test_the_top_level_help_lists_every_command_and_the_exit_codes():
    h = _help()
    sub = next(a for a in build_parser()._actions if a.dest == "cmd")
    assert sorted(sub.choices) == sorted(COMMANDS + ["serve-mcp"])
    for c in sub.choices:
        assert re.search(rf"^\s+{re.escape(c)}\s+\S", h, re.M), f"{c} has no line in --help"
    assert "Exit codes: 0 done; 1 the check failed" in h and "2 the command could not run" in h


@pytest.mark.parametrize("cmd", COMMANDS)
def test_every_subcommand_says_what_it_does_and_every_option_has_help_text(cmd):
    sub = next(a for a in build_parser()._actions if a.dest == "cmd").choices[cmd]
    assert sub.description and sub.description[0].isupper() and sub.description.endswith(".")
    for a in sub._actions:
        assert a.help and len(a.help) > 8, f"`strathora {cmd}`: {a.option_strings or a.dest} has no help text"
    h = _help(cmd)
    assert sub.description.split(" (")[0][:40] in h.replace("\n", " ").replace("  ", " ")
    if any(a.dest == "ledger" for a in sub._actions):
        assert f"(default {DEFAULT_LEDGER})" in h and DEFAULT_LEDGER == "~/.asof/ledger.db"


def test_serve_mcp_help_lists_the_five_options_the_server_takes():
    pytest.importorskip("mcp")
    h = _help("serve-mcp")
    for opt in ("--config", "--as-of", "--max-as-of", "--ledger", "--allow-model-clock"):
        assert opt in h
    assert "Serve an AsOf session over MCP (stdio)." in h


def test_the_exit_codes_are_the_ones_the_help_states(tmp_path, capsys):
    led = tmp_path / "ledger.db"
    assert main(["init", "--ledger", str(led), "--demo", str(tmp_path)]) == 0
    with Session("2023-01-21T14:00:00Z", ledger_path=led) as s:
        s.add_source(sources.CSV(tmp_path / "prices.csv", event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))
        s.query("prices")
        rid = s.receipt().call_id
    assert main(["verify", "--ledger", str(led)]) == 0                                          # 0: done
    assert main(["receipt", "nosuchreceipt", "--ledger", str(led)]) == 1                        # 1: no such receipt
    assert main(["sync", "--ledger", str(led)]) == 1                                            # 1: sync is not enabled, so it is refused
    assert main(["verify", "--ledger", str(tmp_path / "missing.db")]) == 2                      # 2: the ledger is missing
    with pytest.raises(SystemExit) as e:
        main(["verify", "--no-such-option"])
    assert e.value.code == 2                                                                    # 2: a wrong argument
    c = sqlite3.connect(led)
    c.execute("DROP TRIGGER IF EXISTS events_no_update")
    c.execute("UPDATE events SET type = 'forged' WHERE seq = 3")
    c.commit()
    c.close()
    capsys.readouterr()
    assert main(["verify", "--ledger", str(led)]) == 1 and "(at event 3)" in capsys.readouterr().err      # 1: verify found a problem
    assert main(["verify", "--ledger", str(led), "--json"]) == 1                                # "--json ... the exit code is the same"
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"ok", "n_events", "anchor", "first_bad_seq", "reason", "warnings", "head_hash"}     # the seven fields the help names
    assert rid


def test_receipt_agent_never_prints_the_operators_view(tmp_path, capsys):
    """Found by this campaign: `strathora receipt ID --agent` (without --json) printed the operator's summary, blocked counts included, although the help said "the agent view"."""
    led = tmp_path / "ledger.db"
    assert main(["init", "--ledger", str(led), "--demo", str(tmp_path)]) == 0
    with Session("2023-01-21T14:00:00Z", ledger_path=led) as s:
        s.add_source(sources.CSV(tmp_path / "prices.csv", event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))
        s.query("prices")
        rid = s.receipt().call_id
    capsys.readouterr()
    for args in (["--agent"], ["--agent", "--json"], ["--json", "--agent"]):
        assert main(["receipt", rid, "--ledger", str(led), *args]) == 0
        out = capsys.readouterr().out
        view = json.loads(out)
        assert "blocked" not in out and "ledger_seq" not in view and view["served"] == 3, args
    assert main(["receipt", rid, "--ledger", str(led)]) == 0 and "blocked: future=3" in capsys.readouterr().out      # the operator's view, when asked for
    assert main(["receipt", rid, "--ledger", str(led), "--json"]) == 0 and json.loads(capsys.readouterr().out)["blocked"]["future"] == 3
