"""B3: the asof command."""
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from asof import Session
from asof.cli import main

from conftest import ListSource, rec

ROOT = Path(__file__).resolve().parent.parent


def run(args, capsys):
    code = main(args)
    out = capsys.readouterr()
    return code, out.out, out.err


def populated(path):
    s = Session("2023-01-21T14:00:00Z", ledger_path=path)
    s.add_source(ListSource("d", [rec("a", "2023-01-01T00:00:00Z", {"v": 1}), rec("f", "2023-06-01T00:00:00Z", {"v": 2})]))
    s.query("d")
    s.decision("hold")
    s.close()
    return s


def test_version_and_help(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0 and capsys.readouterr().out.strip() == "strathora-asof 0.5.0"              # the distribution's name, whichever command name was typed
    with pytest.raises(SystemExit):
        main([])


def test_init_creates_the_ledger_and_anchor_and_a_working_demo(tmp_path, capsys):
    led = tmp_path / "home" / "ledger.db"
    code, out, _ = run(["init", "--ledger", str(led), "--demo", str(tmp_path / "demo")], capsys)
    assert code == 0 and led.exists() and (tmp_path / "home" / "head.json").exists()
    assert json.loads((tmp_path / "home" / "head.json").read_text())["seq"] == 0
    snippet = json.loads(out[out.index("{\n"):])
    srv = snippet["mcpServers"]["asof"]
    assert srv["command"] == sys.executable and srv["args"][:3] == ["-m", "asof", "serve-mcp"] and (tmp_path / "demo" / "prices.csv").exists()
    cfg = json.loads((tmp_path / "demo" / "asof.config.json").read_text())
    assert cfg["ledger_path"] == str(led) and cfg["as_of"] == "2023-01-21T14:00:00Z"
    assert main(["init", "--ledger", str(led)]) == 0                                                    # idempotent


def test_verify_exit_codes_and_messages(tmp_path, capsys):
    led = tmp_path / "ledger.db"
    populated(led)
    code, out, _ = run(["verify", "--ledger", str(led)], capsys)
    assert code == 0 and out.startswith("OK:") and "tail anchor: ok" in out
    code, out, _ = run(["verify", "--ledger", str(led), "--json"], capsys)
    d = json.loads(out)
    assert code == 0 and d["ok"] and d["anchor"] == "ok" and d["n_events"] > 5
    c = sqlite3.connect(led)
    c.execute("DROP TRIGGER events_no_delete")
    c.execute("DELETE FROM events WHERE seq > ?", (d["n_events"] - 2,))
    c.commit()
    code, out, err = run(["verify", "--ledger", str(led)], capsys)
    assert code == 1 and err.startswith("FAIL:") and "truncated" in err and f"records event {d['n_events']}" in err
    assert main(["verify", "--ledger", str(led), "--no-anchor"]) == 0                                    # chain alone cannot see it (documented)
    capsys.readouterr()


def test_verify_flags_tampering_with_the_event_number(tmp_path, capsys):
    led = tmp_path / "ledger.db"
    populated(led)
    c = sqlite3.connect(led)
    c.execute("DROP TRIGGER events_no_update")
    c.execute("UPDATE events SET recorded_at='2001-01-01T00:00:00Z' WHERE seq=4")
    c.commit()
    code, _, err = run(["verify", "--ledger", str(led)], capsys)
    assert code == 1 and "at event 4" in err and "altered" in err


def test_head_prints_the_anchor_and_a_stored_head_catches_a_rewrite(tmp_path, capsys):
    led = tmp_path / "ledger.db"
    populated(led)
    code, out, err = run(["head", "--ledger", str(led)], capsys)
    head = json.loads(out)
    assert code == 0 and set(head) >= {"seq", "hash", "recorded_at", "version", "source"} and "Store this somewhere you trust" in err
    on_disk = json.loads((tmp_path / "head.json").read_text())
    assert (head["seq"], head["hash"], head["recorded_at"]) == (on_disk["seq"], on_disk["hash"], on_disk["recorded_at"])
    saved = tmp_path / "elsewhere.json"
    saved.write_text(out)
    assert main(["verify", "--ledger", str(led), "--head", str(saved)]) == 0
    assert main(["verify", "--ledger", str(led), "--head", out]) == 0                                   # JSON text works too
    c = sqlite3.connect(led)
    c.execute("DROP TRIGGER events_no_delete")
    c.execute("DELETE FROM events WHERE seq > 6")
    c.commit()
    last = sqlite3.connect(led).execute("SELECT seq, hash, recorded_at FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    (tmp_path / "head.json").write_text(json.dumps({"version": 1, "seq": last[0], "hash": last[1], "recorded_at": last[2]}))      # the attacker fixes the local anchor too
    assert main(["verify", "--ledger", str(led)]) == 0                                                  # fooled...
    assert main(["verify", "--ledger", str(led), "--head", str(saved)]) == 1                           # ...but not by the copy stored elsewhere
    capsys.readouterr()


def test_receipt_from_the_cli_is_identical_to_the_in_process_receipt(tmp_path, capsys):
    led = tmp_path / "ledger.db"
    s = Session("2023-01-21T14:00:00Z", ledger_path=led)
    s.add_source(ListSource("d", [rec("a", "2023-01-01T00:00:00Z", {"v": 1}), rec("f", "2023-06-01T00:00:00Z", {"v": 2}), rec("u", None)]))
    s.add_tool("t", lambda: [{"timestamp": "2023-01-01T00:00:00Z"}, {"timestamp": "2024-01-01T00:00:00Z"}])
    s.register_provider("fake", lambda req: __import__("asof").ModelResponse("x"))
    s.query("d"); s.tool("t"); s.model("fake:m", messages=[{"role": "user", "content": "q"}]); s.decision("hold")
    s.close()
    for r in s.receipt(last=False):
        code, out, _ = run(["receipt", r.call_id, "--ledger", str(led), "--json"], capsys)
        assert code == 0 and json.loads(out) == json.loads(r.to_json()), r.kind                         # the very same evidence, the very same receipt
    q = s.receipt(last=False)[0]
    _, text, _ = run(["receipt", q.call_id, "--ledger", str(led)], capsys)
    assert "served 1" in text and "blocked: future=1, untimestamped=1" in text
    _, agent, _ = run(["receipt", q.call_id, "--ledger", str(led), "--json", "--agent"], capsys)
    assert "blocked" not in json.loads(agent)
    code, _, err = run(["receipt", "nonexistent", "--ledger", str(led)], capsys)
    assert code == 1 and "no ledger events" in err


def test_serve_mcp_passes_arguments_through_and_reports_config_errors(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"as_of": "2023-01-21T14:00:00Z", "nonsense": 1}))
    r = subprocess.run([sys.executable, "-m", "asof", "serve-mcp", "--config", str(bad)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "unknown key" in r.stderr and r.stdout == ""


def test_an_old_version_ledger_is_refused_with_the_fix_and_archive_legacy_works(tmp_path, capsys):
    led = tmp_path / "ledger.db"
    c = sqlite3.connect(led)
    c.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL); INSERT INTO meta VALUES ('schema_version','1');"
                    "CREATE TABLE events (seq INTEGER PRIMARY KEY, id TEXT, session_id TEXT, type TEXT, event_time TEXT, available_at TEXT, payload TEXT, "
                    "payload_sha256 TEXT, prev_hash TEXT, hash TEXT);")
    c.close()
    code, _, err = run(["verify", "--ledger", str(led)], capsys)
    assert code == 2 and "will not mix versions" in err and "strathora init --archive-legacy" in err
    code, out, _ = run(["init", "--ledger", str(led), "--archive-legacy"], capsys)
    assert code == 0 and "archived the old-version ledger" in out and list(tmp_path.glob("ledger.legacy-*.db"))
    assert main(["verify", "--ledger", str(led)]) == 0
    capsys.readouterr()


@pytest.mark.parametrize("args", [["verify"], ["verify", "--json"], ["verify", "--no-anchor"], ["head"], ["receipt", "abc"]])
def test_reading_a_ledger_that_does_not_exist_is_an_error_and_creates_nothing(tmp_path, capsys, args):
    """A mistyped --ledger used to be created empty and reported as "OK: 0 events". Commands that read evidence must refuse a path with no ledger and leave the folder untouched."""
    missing = tmp_path / "typo" / "ledger.db"
    code, out, err = run([*args, "--ledger", str(missing)], capsys)
    assert code == 2 and out == "" and "no ledger at" in err and str(missing) in err and "strathora init" in err
    assert not missing.exists() and not missing.parent.exists() and list(tmp_path.iterdir()) == []      # nothing was written anywhere
    assert main(["init", "--ledger", str(missing)]) == 0                                                   # init is the command that creates one...
    capsys.readouterr()
    code, out, _ = run(["verify", "--ledger", str(missing)], capsys)
    assert code == 0 and out.startswith("OK: 0 events")                                                    # ...and a real, empty ledger still verifies


def test_init_demo_with_a_relative_ledger_path_writes_a_config_that_points_at_that_same_ledger(tmp_path, capsys, monkeypatch):
    """Seen on a clean install: `strathora init --ledger demo/ledger.db --demo demo` wrote "ledger_path": "demo/ledger.db" into demo/asof.config.json. A relative path in a config is read
    against the config's own folder, so the MCP server wrote to demo/demo/ledger.db and `strathora verify --ledger demo/ledger.db` checked a ledger the server never touched."""
    pytest.importorskip("mcp")
    from asof.mcp_server import load_config, session_from_config
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--ledger", "demo/ledger.db", "--demo", "demo"]) == 0
    capsys.readouterr()
    cfg, base = load_config("demo/asof.config.json")
    assert Path(cfg["ledger_path"]).is_absolute()
    s, _ = session_from_config(cfg, base)
    try:
        assert s.ledger.path.resolve() == (tmp_path / "demo" / "ledger.db").resolve()
        s.query("prices")
    finally:
        s.close()
    assert not (tmp_path / "demo" / "demo").exists()                                                      # no second ledger appeared
    code, out, _ = run(["verify", "--ledger", "demo/ledger.db"], capsys)
    assert code == 0 and not out.startswith("OK: 0 events")                                               # the query the server made is in the ledger the user checks


def test_serve_mcp_without_the_mcp_package_says_how_to_add_it_in_one_line(tmp_path):
    """Seen on a clean install: a two-part traceback that ended in a hint to `pip install 'asof[mcp]'`, which on PyPI is somebody else's package."""
    prog = "import sys; sys.modules['mcp'] = None; from asof.cli import main; sys.exit(main(['serve-mcp', '--config', 'x.json']))"      # None in sys.modules makes `import mcp` fail
    r = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, timeout=60, cwd=tmp_path, env={"PYTHONPATH": str(ROOT), "PATH": ""})
    assert r.returncode == 2 and r.stdout == ""
    assert "Traceback" not in r.stderr and len(r.stderr.strip().splitlines()) == 1, r.stderr
    assert "pip install 'mcp>=2,<3'" in r.stderr and "asof[mcp]" not in r.stderr


def test_nothing_tells_a_user_to_install_asof_by_name_from_the_public_index():
    """The name `asof` on PyPI is an unrelated project, so `pip install asof` (or 'asof[mcp]') would install someone else's code. Every install line must name a source:
    a checkout (`.`), a wheel file, or a git address (`strathora-asof[mcp] @ git+...`). The distribution is called `strathora-asof` for the same reason."""
    named = re.compile(r"pip3? install (?:-\S+ )*['\"]?asof(?![-\w./])(?:\[[\w,]+\])?(.*)")

    class bad:                                                                                             # `pip install asof...` with no ` @ <source>` after the name
        @staticmethod
        def search(line):
            m = named.search(line)
            return m if m and not m.group(1).lstrip().startswith("@") else None

    hits = []
    for f in sorted([ROOT / "README.md", ROOT / "pyproject.toml", *ROOT.glob("asof/**/*.py"), *ROOT.glob("asof/**/*.md"), *ROOT.glob("docs/**/*.md"), *ROOT.glob("scripts/*"), *ROOT.glob("examples/**/*.py"),
                     *ROOT.glob("demo/*.py"), *ROOT.glob("showcase/**/*.md")]):
        if not f.is_file():
            continue
        for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            if bad.search(line) and "do not" not in line.lower():
                hits.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()[:120]}")
    assert hits == [], "\n".join(hits)
    assert bad.search("pip install 'asof[mcp]'") and bad.search("pip install asof") and bad.search("python -m pip install -U asof[parquet]")      # the pattern catches the real mistake...
    for ok in ('pip install "strathora-asof[mcp] @ git+https://example.invalid/x.git"', 'pip install "asof[mcp] @ git+https://example.invalid/x.git"', "pip install strathora_asof-0.5.0-py3-none-any.whl",
               "pip install -e '.[mcp]'", "pip install 'mcp>=2,<3'"):
        assert not bad.search(ok), ok                                                                      # ...and not the right ways


def test_every_data_file_in_the_package_is_declared_so_a_git_install_ships_it():
    """`pip install git+...` builds a wheel from pyproject.toml alone: a .js/.css/.md under asof/ that no package-data pattern covers would be missing after install."""
    import fnmatch
    tomllib = pytest.importorskip("tomllib")                                                               # standard library from Python 3.11
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]
    for f in sorted(p for p in (ROOT / "asof").rglob("*") if p.is_file() and p.suffix not in (".py", ".pyc")):
        pkg = ".".join(f.parent.relative_to(ROOT).parts)
        assert any(fnmatch.fnmatch(f.name, pat) for pat in data.get(pkg, [])), f"{f.relative_to(ROOT)} is not covered by [tool.setuptools.package-data] for {pkg!r}"


def test_sync_command_is_refused_while_sync_is_off(tmp_path, capsys, monkeypatch):
    for k in ("ASOF_SYNC_ENABLED", "ASOF_SYNC_ENDPOINT", "ASOF_SYNC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    code, _, err = run(["sync", "--ledger", str(tmp_path / "ledger.db")], capsys)
    assert code == 1 and "off by default" in err
