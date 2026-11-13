"""Storage rules of the ledger: WAL while it is being written, secure_delete on, a closed ledger is one self-contained file, reading never changes a ledger, a safe online backup,
one chain per session, and the `strathora` command (`asof` is its alias) catching a tampered row at that row."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import re
import sys
from pathlib import Path

import pytest

from asof import Ledger, LedgerError, Session
from asof.cli import main as cli_main
from asof.ledger import default_head_path

from conftest import ListSource, T, rec

ROOT = Path(__file__).resolve().parents[1]


def put(led: Ledger, n: int = 3, session: str = "s1") -> None:
    for i in range(n):
        led.append(session, "x", event_time=T("2023-01-01T00:00:00Z"), available_at=T("2023-01-01T00:00:00Z"), payload={"i": i})


def mode(db: Path | str) -> str:
    c = sqlite3.connect(str(db))
    try:
        return str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        c.close()


# ---------------------------------------------------------------- WAL and secure_delete
def test_wal_is_switched_on_by_the_first_write_and_secure_delete_is_on(tmp_path):
    led = Ledger(tmp_path / "ledger.db")
    assert led._db.execute("PRAGMA secure_delete").fetchone()[0] == 1                       # a per-connection setting: freed pages are overwritten
    assert mode(tmp_path / "ledger.db") == "delete"                                         # just opening wrote nothing about journaling
    put(led)
    assert led._db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert mode(tmp_path / "ledger.db") == "wal"                                            # it is recorded in the file, so a second connection sees it
    led.close()


def test_closing_folds_the_wal_into_one_self_contained_file(tmp_path):
    p = tmp_path / "ledger.db"
    led = Ledger(p)
    put(led, 5)
    assert Path(str(p) + "-wal").exists()                                                   # while open, committed events may sit in the sidecar
    led.close()
    assert not Path(str(p) + "-wal").exists() and not Path(str(p) + "-shm").exists()
    assert mode(p) == "delete"
    copy = tmp_path / "copy" / "ledger.db"
    copy.parent.mkdir()
    shutil.copy(p, copy)                                                                    # a plain file copy of a CLOSED ledger loses nothing
    shutil.copy(default_head_path(p), default_head_path(copy))
    assert len(Ledger(copy)) == 5 and Ledger(copy).verify().ok


def test_reading_or_verifying_a_ledger_never_changes_the_file(tmp_path, capsys):
    p = tmp_path / "ledger.db"
    led = Ledger(p)
    put(led)
    led.close()
    before = p.read_bytes()
    for _ in range(2):
        r = Ledger(p)
        assert r.verify().ok and len(r) == 3
        r.close()
    assert cli_main(["verify", "--ledger", str(p)]) == 0
    capsys.readouterr()
    assert p.read_bytes() == before and not Path(str(p) + "-wal").exists()                   # verification is a read: it does not even flip the journal mode


def test_a_refused_old_schema_ledger_is_still_untouched(tmp_path):
    p = tmp_path / "ledger.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE events(seq INTEGER PRIMARY KEY, x TEXT)")
    c.commit()
    c.close()
    before = p.read_bytes()
    with pytest.raises(Exception):
        Ledger(p)
    assert p.read_bytes() == before


def test_an_online_backup_taken_while_the_ledger_is_open_and_busy_is_a_complete_single_file(tmp_path):
    led = Ledger(tmp_path / "ledger.db")
    put(led, 7)
    out = led.backup(tmp_path / "bk" / "copy.ledger.db")
    put(led, 3)                                                                              # more writes after the backup do not reach it
    assert not Path(str(out) + "-wal").exists() and mode(out) == "delete"
    b = Ledger(out, head_path=tmp_path / "bk" / "none.json")
    assert len(b) == 7 and b.verify(anchor="skip").ok
    assert len(led) == 10 and led.verify().ok
    led.close()


# ---------------------------------------------------------------- one chain per session
def test_each_session_can_have_its_own_chain_with_its_own_identity_and_anchor(tmp_path):
    a, b = Ledger.for_session(tmp_path, "sess-a"), Ledger.for_session(tmp_path, "sess-b")
    put(a, 4, "sess-a")
    put(b, 2, "sess-b")
    assert a.chain_id != b.chain_id and len(a) == 4 and len(b) == 2
    assert default_head_path(a.path).name == "sess-a.ledger.head.json" and default_head_path(b.path).name == "sess-b.ledger.head.json"
    assert json.loads(default_head_path(a.path).read_text())["seq"] == 4 and json.loads(default_head_path(b.path).read_text())["seq"] == 2
    a.close()
    b.close()
    c = sqlite3.connect(tmp_path / "sess-a.ledger.db")                                        # tamper with a's row 2: only a's chain fails
    c.execute("DROP TRIGGER events_no_update")
    c.execute("UPDATE events SET payload = replace(payload, '\"i\":1', '\"i\":9') WHERE seq=2")
    c.commit()
    c.close()
    va, vb = Ledger.for_session(tmp_path, "sess-a").verify(), Ledger.for_session(tmp_path, "sess-b").verify()
    assert not va.ok and va.first_bad_seq == 2 and "altered" in va.reason and vb.ok          # a's chain fails at the changed row; b's chain is not affected


def test_a_session_id_that_is_not_a_safe_file_name_is_refused(tmp_path):
    for bad in ("", "..", "a/b", "a\\b", "x\0y"):
        with pytest.raises(LedgerError):
            Ledger.for_session(tmp_path, bad)


def test_a_session_can_be_run_on_its_own_chain(tmp_path):
    led = Ledger.for_session(tmp_path, "run-1")
    s = Session("2023-01-21T14:00:00Z", ledger_path=led.path)
    s.add_source(ListSource("d", [rec("a", "2023-01-01T00:00:00Z")]))
    s.query("d")
    s.close()
    assert Ledger.for_session(tmp_path, "run-1").verify().ok and not (tmp_path / "ledger.db").exists()


# ---------------------------------------------------------------- the strathora command
def test_strathora_is_the_command_and_asof_is_an_alias_of_the_same_entry_point():
    text = (ROOT / "pyproject.toml").read_text()                                                          # read as text: tomllib is not in Python 3.10, and this file must be collected there
    scripts = dict(re.findall(r'^(\w+) = "([\w.:]+)"', text.split("[project.scripts]")[1].split("\n[")[0], re.M))
    assert list(scripts) == ["strathora", "asof"] and scripts["asof"] == scripts["strathora"] == "asof.cli:main"
    assert re.search(r'^name = "strathora-asof"', text, re.M)                                                           # the distribution; the import name stays `asof`


def test_both_command_names_run_the_same_program_and_help_shows_the_name_that_was_typed(tmp_path):
    for name in ("strathora", "asof"):
        exe = shutil.which(name)
        assert exe, f"run `pip install -e .` again: the {name} console script is created at install time"
        v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
        assert v.returncode == 0 and v.stdout.strip() == "strathora-asof 0.5.0"
        h = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=60)
        assert h.stdout.startswith(f"usage: {name} ")
    m = subprocess.run([sys.executable, "-m", "asof", "--help"], capture_output=True, text=True, timeout=60)
    assert m.stdout.startswith("usage: strathora ")                                                        # started any other way, it calls itself by its main name
    missing = subprocess.run([shutil.which("asof"), "verify", "--ledger", str(tmp_path / "none.db")], capture_output=True, text=True, timeout=60)
    assert missing.returncode == 2 and missing.stderr.startswith("strathora: no ledger at") and "`strathora init --ledger" in missing.stderr


def _tampered(tmp_path):
    p = tmp_path / "ledger.db"
    led = Ledger(p)
    put(led, 6)
    led.close()
    c = sqlite3.connect(p)
    c.execute("DROP TRIGGER events_no_update")
    c.execute("UPDATE events SET payload = replace(payload, '\"i\":3', '\"i\":30') WHERE seq=4")             # event 4 holds i=3
    c.commit()
    c.close()
    return p


def test_strathora_verify_reports_the_first_broken_link_at_the_row_that_was_changed(tmp_path):
    p = _tampered(tmp_path)
    exe = shutil.which("strathora")
    cmd = [exe] if exe else [sys.executable, "-m", "asof"]
    r = subprocess.run([*cmd, "verify", "--ledger", str(p)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and r.stderr.startswith("FAIL:") and "at event 4" in r.stderr and "altered" in r.stderr
    ok = subprocess.run([*cmd, "verify", "--ledger", str(_clean(tmp_path))], capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0 and ok.stdout.startswith("OK:")


def _clean(tmp_path):
    q = tmp_path / "clean" / "ledger.db"
    led = Ledger(q)
    put(led, 6)
    led.close()
    return q


def test_the_strathora_command_is_installed_in_this_environment():
    assert shutil.which("strathora"), "run `pip install -e .` again: the strathora console script is created at install time"


# ---------------------------------------------------------------- the first WAL switch under contention (a regression found by a flaky full-suite run)
def _first_write(args):
    path, i, barrier = args
    led = Ledger(path)
    barrier.wait()                                                                         # every process makes its first write at the same instant
    for n in range(5):
        led.append(f"s{i}", "x", event_time=T("2023-01-01T00:00:00Z"), available_at=T("2023-01-01T00:00:00Z"), payload={"i": i, "n": n})
    return True


def test_many_processes_making_their_first_write_at_the_same_instant_never_fail_with_database_is_locked(tmp_path):
    """Switching a database into WAL needs an exclusive lock that the busy timeout does not wait for. Before the retry, 8 aligned processes failed in about one trial in four. This test is PROBABILISTIC
    (ten trials miss the bug about one time in thirteen); the deterministic guard is the test below, which fails on the old code every time."""
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    for trial in range(10):
        p = tmp_path / f"t{trial}" / "ledger.db"
        Ledger(p).close()                                                                  # a closed ledger is back in its single-file journal mode, so the first write must switch again
        barrier = ctx.Manager().Barrier(8)
        with ctx.Pool(8) as pool:
            assert pool.map(_first_write, [(p, i, barrier) for i in range(8)]) == [True] * 8
        led = Ledger(p)
        assert len(led) == 40 and led.verify().ok


def test_a_switch_into_wal_that_cannot_be_made_never_fails_the_write_and_is_tried_again(tmp_path, monkeypatch):
    import asof.ledger as L
    led = Ledger(tmp_path / "ledger.db")
    monkeypatch.setattr(L, "_WAL_PATIENCE", 0.05)
    real = led._db
    calls = {"n": 0}

    class Locked:                                                                          # a connection whose journal-mode switch is always locked out, everything else real
        def execute(self, sql, *a):
            if sql.startswith("PRAGMA journal_mode=WAL"):
                calls["n"] += 1
                raise sqlite3.OperationalError("database is locked")
            return real.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(real, name)

    led._db = Locked()                                                                     # type: ignore[assignment]
    put(led, 2)
    assert len(led) == 2 and led.verify().ok and led._wal is False and calls["n"] >= 1      # the writes went through, in the current mode
    first = calls["n"]
    put(led, 1)
    assert calls["n"] > first                                                              # and the switch is tried again on the next write
    led._db = real
    put(led, 1)
    assert led._wal is True and len(led) == 4 and led.verify().ok                          # once it can be made, it is
