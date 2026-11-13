"""Campaign G, item 30: ledgers in awkward places. Paths with spaces and non-ASCII names, folders that do not exist yet, folders and files that cannot be written, and a disk that fills up
(simulated two ways: SQLite's own page limit, which gives the real "database or disk is full" error, and an anchor file that cannot be written)."""
from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from asof import Session, sources
from asof.cli import main
from asof.errors import LedgerError
from asof.ledger import EventSpec, Ledger, open_for_reading

T = "2023-01-21T14:00:00Z"
not_root = pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="permissions do not restrict this user")

AWKWARD = ["dir with spaces/led ger.db", "über/ñandú/ledger.db", "日本語/台帳.db", "emoji 🚀/ledger.db", "a'quote/it\"s.db", "trailing dot./ledger.db", "-starts-with-dash/ledger.db",
           "percent%20and#hash?query/ledger.db", "deep/" + "/".join(f"level{i}" for i in range(12)) + "/ledger.db"]


def _run(tmp_path: Path, ledger: Path) -> str:
    """The quickstart against a ledger at ``ledger``: returns the receipt id."""
    demo = tmp_path / "demo data"
    assert main(["init", "--ledger", str(ledger), "--demo", str(demo)]) == 0
    with Session(T, ledger_path=ledger) as s:
        s.add_source(sources.CSV(demo / "prices.csv", event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC"))
        assert [r.payload["close"] for r in s.query("prices", symbol="ACME-SYN")] == [147.0, 148.0, 150.0]
        return s.receipt().call_id


@pytest.mark.parametrize("rel", AWKWARD)
def test_a_ledger_at_an_awkward_path_is_created_with_its_missing_folders_and_everything_works(tmp_path, capsys, rel):
    ledger = tmp_path / rel
    assert not ledger.parent.exists()                                                     # the folders do not exist yet: they are made
    rid = _run(tmp_path, ledger)
    capsys.readouterr()
    assert main(["verify", "--ledger", str(ledger)]) == 0
    assert capsys.readouterr().out.startswith("OK: 9 events, hash chain intact, tail anchor: ok")
    assert main(["receipt", rid, "--ledger", str(ledger)]) == 0 and "blocked: future=3" in capsys.readouterr().out
    assert main(["head", "--ledger", str(ledger)]) == 0
    assert main(["report", "--ledger", str(ledger), "--html", str(ledger.parent / "report out.html"), "--synthetic"]) == 0
    anchors = [p.name for p in ledger.parent.iterdir() if p.name.endswith("head.json")]
    assert len(anchors) == 1, anchors                                                     # the anchor sits beside the ledger, once
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600                                   # and the ledger is private to its owner


def test_a_relative_path_and_a_home_path_mean_the_same_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    _run(tmp_path, Path("rel dir/ledger.db"))
    capsys.readouterr()
    assert main(["verify", "--ledger", "~/rel dir/ledger.db"]) == 0 and "OK: 9 events" in capsys.readouterr().out
    assert main(["verify", "--ledger", "./rel dir/../rel dir/ledger.db"]) == 0


@not_root
def test_a_new_ledger_in_a_folder_that_cannot_be_written_is_refused_in_one_line(tmp_path, capsys):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        code = main(["init", "--ledger", str(ro / "ledger.db")])
        err = capsys.readouterr().err
        assert code == 2 and err.startswith("strathora: cannot open ledger at") and "Traceback" not in err and len(err.strip().splitlines()) == 1
        with pytest.raises(LedgerError, match="cannot open ledger"):
            Session(T, ledger_path=ro / "ledger.db")
        with pytest.raises(LedgerError, match="cannot open ledger"):
            Session(T, ledger_path=ro / "missing" / "deeper" / "ledger.db")                  # ... and its missing parents cannot be made either
        assert list(ro.iterdir()) == []
    finally:
        ro.chmod(0o755)


@not_root
@pytest.mark.parametrize("lock", ["the file", "the folder", "both"])
def test_a_ledger_that_cannot_be_written_can_still_be_verified_and_read_and_is_not_changed(tmp_path, capsys, lock):
    """Found by this campaign: `verify` and `head` used to fail on a read-only ledger ("attempt to write a readonly database"), so evidence on read-only media could not be checked."""
    d = tmp_path / "archive"
    ledger = d / "ledger.db"
    rid = _run(tmp_path, ledger)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in d.iterdir()}
    if lock in ("the file", "both"):
        for p in d.iterdir():
            p.chmod(0o444)
    if lock in ("the folder", "both"):
        d.chmod(0o555)
    try:
        capsys.readouterr()
        assert main(["verify", "--ledger", str(ledger)]) == 0
        assert capsys.readouterr().out.startswith("OK: 9 events, hash chain intact, tail anchor: ok")
        assert main(["head", "--ledger", str(ledger)]) == 0 and '"seq": 9' in capsys.readouterr().out
        assert main(["receipt", rid, "--ledger", str(ledger)]) == 0 and "blocked: future=3" in capsys.readouterr().out
        assert main(["report", "--ledger", str(ledger), "--html", str(tmp_path / "r.html"), "--synthetic"]) == 0
        led = open_for_reading(ledger)
        assert led.read_only is True and len(led) == 9 and led.verify().ok
        with pytest.raises(LedgerError, match="read-only"):
            led.append("s", "note", event_time=None, available_at=None, payload={})
        with pytest.raises(LedgerError, match="read-only"):
            led.write_anchor()
        led.close()
        with pytest.raises(LedgerError, match="cannot open ledger|could not append to the ledger: attempt to write a readonly database"):      # writing to it is still refused, cleanly
            Session(T, ledger_path=ledger)
        assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in d.iterdir()} == before      # no byte changed, no file appeared
    finally:
        d.chmod(0o755)
        for p in d.iterdir():
            p.chmod(0o644)


@not_root
def test_tampering_is_still_found_in_a_read_only_ledger(tmp_path, capsys):
    import sqlite3
    ledger = tmp_path / "a" / "ledger.db"
    _run(tmp_path, ledger)
    c = sqlite3.connect(ledger)
    c.execute("DROP TRIGGER IF EXISTS events_no_update")                                  # the trigger stops accidents, not someone with the file
    c.execute("UPDATE events SET type = 'forged' WHERE seq = 4")
    c.commit()
    c.close()
    ledger.chmod(0o444)
    capsys.readouterr()
    assert main(["verify", "--ledger", str(ledger)]) == 1
    assert "(at event 4)" in capsys.readouterr().err


def test_read_only_refuses_a_missing_file_and_a_file_that_is_not_a_ledger_without_creating_anything(tmp_path):
    with pytest.raises(LedgerError, match="cannot open ledger"):
        Ledger(tmp_path / "nowhere" / "ledger.db", read_only=True)
    assert not (tmp_path / "nowhere").exists()
    (tmp_path / "empty.db").write_bytes(b"")
    (tmp_path / "text.db").write_text("not a database at all, but long enough to be read as a header by SQLite " * 4)
    for name in ("empty.db", "text.db"):
        with pytest.raises(LedgerError, match="cannot open ledger"):
            Ledger(tmp_path / name, read_only=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["empty.db", "text.db"]


# ------------------------------------------------------------------ the disk fills up
def _full(led: Ledger, spare_pages: int = 2) -> None:
    n = led._db.execute("PRAGMA page_count").fetchone()[0]
    led._db.execute(f"PRAGMA max_page_count={n + spare_pages}")                               # SQLite now answers exactly as it does when the disk is full


def _space(led: Ledger) -> None:
    led._db.execute("PRAGMA max_page_count=1073741823")


def test_a_write_that_does_not_fit_is_refused_whole_and_the_ledger_stays_valid(tmp_path):
    led = Ledger(tmp_path / "ledger.db")
    for i in range(5):
        led.append("s", "note", event_time=None, available_at=None, payload={"i": i})
    _full(led)
    batch = [EventSpec("s", "note", None, None, {"blob": "x" * 3000, "i": i}, None) for i in range(50)]
    with pytest.raises(LedgerError, match="could not append to the ledger: database or disk is full"):
        led.append_many(batch)
    v = led.verify()
    assert v.ok and v.n_events == 5 and v.anchor == "ok"                                      # none of the 50: a batch is all or nothing
    _space(led)
    led.append_many(batch)
    v = led.verify()
    assert v.ok and v.n_events == 55 and v.anchor == "ok"
    led.close()


def test_a_session_whose_ledger_is_full_serves_nothing_it_cannot_record(tmp_path):
    (tmp_path / "p.csv").write_text("id,t,a,v\n" + "".join(f"r{i},2023-01-01T00:00:00Z,2023-01-01T00:00:00Z,{'y' * 500}\n" for i in range(400)))
    s = Session("2023-02-01T00:00:00Z", ledger_path=tmp_path / "s.db")
    s.add_source(sources.CSV(tmp_path / "p.csv", name="p", event_time="t", available_at="a", record_id="id"))
    _full(s.ledger, 3)
    with pytest.raises(LedgerError, match="database or disk is full"):                        # no rows come back: what cannot be written down is not served
        s.query("p")
    v = s.verify_ledger()
    assert v.ok and [e.type for e in s.events()] == ["session_start", "source_added", "source_loaded"]
    _space(s.ledger)
    assert len(s.query("p")) == 400 and s.verify_ledger().ok
    s.close()


def test_when_only_the_anchor_cannot_be_written_the_error_is_raised_and_the_next_write_repairs_it(tmp_path, monkeypatch):
    led = Ledger(tmp_path / "a.db")
    led.append("s", "note", event_time=None, available_at=None, payload={"i": 0})
    real = Path.write_text

    def no_space(self, *a, **k):
        if self.name.endswith(".tmp"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "write_text", no_space)
    with pytest.raises(LedgerError, match="could not write the head anchor .*No space left on device"):
        led.append("s", "note", event_time=None, available_at=None, payload={"i": 1})
    monkeypatch.setattr(Path, "write_text", real)
    v = led.verify()
    assert v.ok and v.n_events == 2 and v.anchor == "behind" and "1 event(s) behind" in v.warnings[0]      # the event is in the chain; the anchor says so honestly
    led.append("s", "note", event_time=None, available_at=None, payload={"i": 2})
    v = led.verify()
    assert v.ok and v.n_events == 3 and v.anchor == "ok"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
    led.close()
