"""R6: every event is logged, and the log is tamper-evident (schema v2)."""
import json
import os
import sqlite3
import stat
import threading

import pytest

from asof import Ledger, LedgerError, Session
from asof.ledger import EventSpec

from conftest import ListSource, T, rec


def populated(path):
    s = Session("2023-01-21T14:00:00Z", ledger_path=path)
    s.add_source(ListSource("d", [rec("a", "2023-01-01T00:00:00Z", {"v": 1}), rec("b", "2023-03-01T00:00:00Z", {"v": 2})]))
    s.query("d")
    s.advance_to("2023-04-01T00:00:00Z")
    s.query("d")
    s.close()
    return s


def raw(path):
    c = sqlite3.connect(path)
    c.execute("DROP TRIGGER events_no_update")
    c.execute("DROP TRIGGER events_no_delete")
    return c


def test_r6_every_kind_of_event_is_recorded(make_session):
    s = make_session("2023-01-21T14:00:00Z")
    s.add_source(ListSource("d", [rec("a", "2023-01-01T00:00:00Z"), rec("f", "2024-01-01T00:00:00Z")]))
    s.add_tool("t", lambda: [{"timestamp": "2023-01-01T00:00:00Z"}])
    s.register_provider("fake", lambda req: __import__("asof").ModelResponse("hi"))
    s.query("d"); s.tool("t"); s.model("fake:m", messages=[{"role": "user", "content": "q"}]); s.decision("hold", why="test")
    s.advance_to("2023-02-01T00:00:00Z")
    with pytest.raises(Exception):
        s.advance_to("2023-01-01T00:00:00Z")
    types = {e.type for e in s.events()}
    for need in ("session_start", "source_added", "source_loaded", "tool_added", "query", "record_served", "block", "tool_call", "tool_item_served", "model_call",
                 "decision", "clock_advance", "clock_rewind_rejected"):
        assert need in types, need
    assert s.verify_ledger().ok


def test_the_chain_verifies_and_events_are_sequential_and_linked(ledger_path):
    populated(ledger_path)
    led = Ledger(ledger_path)
    evs = led.events()
    assert [e.seq for e in evs] == list(range(1, len(evs) + 1)) and evs[0].prev_hash == "0" * 64
    assert all(evs[i].prev_hash == evs[i - 1].hash for i in range(1, len(evs)))
    v = led.verify()
    assert v.ok and v.n_events == len(evs) and v.head_hash == evs[-1].hash and v.anchor == "ok" and bool(v)


def test_the_triggers_refuse_update_and_delete(ledger_path):
    populated(ledger_path)
    c = sqlite3.connect(ledger_path)
    for sql in ("UPDATE events SET payload='{}' WHERE seq=2", "DELETE FROM events WHERE seq=2"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute(sql)


# ---------------------------------------------------------------- tampering: every single field, on a copy of the database
FIELDS = {"id": "forged-id", "session_id": "someone-else", "type": "decision", "call_id": "forged-call", "event_time": "2020-01-01T00:00:00Z",
          "available_at": "2020-01-01T00:00:00Z", "recorded_at": "2020-01-01T00:00:00Z", "payload": '{"forged":true}', "payload_sha256": "0" * 64,
          "prev_hash": "f" * 64, "hash": "e" * 64}


@pytest.mark.parametrize("column", sorted(FIELDS))
def test_tampering_with_any_field_is_detected_at_that_event(ledger_path, tmp_path, column):
    populated(ledger_path)
    copy = tmp_path / "copy" / "ledger.db"
    copy.parent.mkdir()
    copy.write_bytes(ledger_path.read_bytes())                                                # a copy of the database, as an auditor would receive it
    target = 6
    c = raw(copy)
    c.execute(f"UPDATE events SET {column}=? WHERE seq=?", (FIELDS[column], target))
    c.commit()
    v = Ledger(copy).verify(anchor="skip")
    assert not v.ok and v.first_bad_seq == target, (column, v)


def test_the_three_clock_columns_are_all_covered_by_the_hash_even_when_set_to_null(ledger_path):
    populated(ledger_path)
    for column in ("event_time", "available_at", "call_id"):
        p = ledger_path.parent / f"null_{column}.db"
        p.write_bytes(ledger_path.read_bytes())
        c = raw(p)
        c.execute(f"UPDATE events SET {column}=NULL WHERE seq=6")                        # seq 6 is the first query: all three are set on it
        c.commit()
        assert Ledger(p).verify(anchor="skip").first_bad_seq == 6, column


def test_changing_the_sequence_number_is_detected(ledger_path):
    populated(ledger_path)
    c = raw(ledger_path)
    c.execute("UPDATE events SET seq=999 WHERE seq=5")
    c.commit()
    v = Ledger(ledger_path).verify(anchor="skip")
    assert not v.ok and "gap" in v.reason


def test_editing_a_payload_and_its_hash_is_still_detected_by_the_chain(ledger_path):
    from asof.records import sha256_hex
    populated(ledger_path)
    c = raw(ledger_path)
    body = '{"forged":true}'
    c.execute("UPDATE events SET payload=?, payload_sha256=? WHERE seq=5", (body, sha256_hex(body)))
    c.commit()
    v = Ledger(ledger_path).verify(anchor="skip")
    assert not v.ok and v.first_bad_seq == 5 and "hash" in v.reason


def test_deleting_an_event_from_the_middle_is_detected(ledger_path):
    populated(ledger_path)
    c = raw(ledger_path)
    c.execute("DELETE FROM events WHERE seq=4")
    c.commit()
    v = Ledger(ledger_path).verify(anchor="skip")
    assert not v.ok and v.first_bad_seq == 5 and "gap" in v.reason


# ---------------------------------------------------------------- A2: the tail anchor
def test_head_json_is_exported_after_every_write_and_matches_the_database(ledger_path):
    led = Ledger(ledger_path)
    head = led.head_path
    assert head == ledger_path.parent / "head.json" and json.loads(head.read_text())["seq"] == 0          # an empty ledger is anchored too
    seen = []
    for i in range(5):
        ev = led.append("s", "x", event_time=T("2023-01-01T00:00:00Z"), available_at=T("2023-01-01T00:00:00Z"), payload={"i": i})
        d = json.loads(head.read_text())
        assert d == {"version": 1, "seq": ev.seq, "hash": ev.hash, "recorded_at": ev.recorded_at}
        seen.append(d["seq"])
    assert seen == [1, 2, 3, 4, 5] and stat.S_IMODE(os.stat(head).st_mode) == 0o600
    assert not list(ledger_path.parent.glob("*.tmp"))                                           # atomic replace leaves no temp files behind


def test_a_ledger_not_named_ledger_db_gets_its_own_head_file(tmp_path):
    led = Ledger(tmp_path / "research.db")
    assert led.head_path == tmp_path / "research.head.json" and led.head_path.exists()


@pytest.mark.parametrize("n_cut", [1, 3, 10])
def test_deleting_the_last_n_rows_is_detected_via_head_json_with_a_clear_message(ledger_path, n_cut):
    populated(ledger_path)
    before = Ledger(ledger_path).head()
    c = raw(ledger_path)
    c.execute("DELETE FROM events WHERE seq > ?", (before.seq - n_cut,))
    c.commit()
    after = Ledger(ledger_path)
    assert after.verify(anchor="skip").ok                                                      # the chain alone cannot see it: documented limitation
    v = after.verify()
    assert not v.ok and v.anchor == "failed" and "truncated" in v.reason and f"records event {before.seq}" in v.reason and f"ends at event {before.seq - n_cut}" in v.reason


def test_deleting_the_whole_ledger_but_not_its_anchor_is_detected(ledger_path):
    populated(ledger_path)
    c = raw(ledger_path)
    c.execute("DELETE FROM events")
    c.commit()
    v = Ledger(ledger_path).verify()
    assert not v.ok and "truncated" in v.reason


def test_a_missing_head_file_on_a_non_empty_ledger_fails_with_instructions(ledger_path):
    populated(ledger_path)
    (ledger_path.parent / "head.json").unlink()
    led = Ledger.__new__(Ledger)                                                               # open without re-creating the anchor, as a verifier of a bare copy would
    import threading as _t
    led.path, led.head_path, led._lock = ledger_path, ledger_path.parent / "head.json", _t.RLock()
    led._db = sqlite3.connect(ledger_path, check_same_thread=False, isolation_level=None)
    v = led.verify()
    assert not v.ok and "missing" in v.reason and "expected_head" in v.reason and "--no-anchor" in v.reason
    assert led.verify(anchor="skip").ok and led.verify(anchor="skip").anchor == "skipped" and led.verify(anchor="skip").warnings


def test_a_corrupt_head_file_fails(ledger_path):
    populated(ledger_path)
    (ledger_path.parent / "head.json").write_text("not json")
    assert "cannot be read" in Ledger(ledger_path).verify().reason


def test_an_anchor_that_does_not_match_the_chain_means_it_was_rewritten(ledger_path):
    populated(ledger_path)
    d = json.loads((ledger_path.parent / "head.json").read_text())
    d["hash"] = "a" * 64
    (ledger_path.parent / "head.json").write_text(json.dumps(d))
    v = Ledger(ledger_path).verify()
    assert not v.ok and "rewritten" in v.reason


def test_a_stale_anchor_is_ok_with_a_warning_and_never_hides_a_longer_chain(ledger_path):
    populated(ledger_path)
    led = Ledger(ledger_path)
    older = led.events()[-3]
    (ledger_path.parent / "head.json").write_text(json.dumps({"version": 1, "seq": older.seq, "hash": older.hash, "recorded_at": older.recorded_at}))
    v = led.verify()
    assert v.ok and v.anchor == "behind" and "2 event(s) behind" in v.warnings[0]


def test_the_anchor_never_moves_backwards(ledger_path):
    led = Ledger(ledger_path)
    for i in range(3):
        led.append("s", "x", event_time=None, available_at=None, payload={"i": i})
    newest = led.head()
    led._write_head(type(newest)(1, "b" * 64, None))                                           # a stale writer
    assert json.loads(led.head_path.read_text())["seq"] == newest.seq


def test_THE_LIMIT_an_attacker_who_rewrites_both_files_wins_unless_you_stored_the_head_elsewhere(ledger_path):
    """The anchor is only as trustworthy as where it is stored (documented). Cut the ledger AND rewrite head.json: file-only verification passes;
    a head you saved elsewhere still catches it."""
    populated(ledger_path)
    saved_elsewhere = Ledger(ledger_path).head().to_dict()                                     # e.g. committed to git, emailed
    c = raw(ledger_path)
    c.execute("DELETE FROM events WHERE seq > 8")
    c.commit()
    led = Ledger(ledger_path)
    last = led.events()[-1]
    (ledger_path.parent / "head.json").write_text(json.dumps({"version": 1, "seq": last.seq, "hash": last.hash, "recorded_at": last.recorded_at}))
    assert led.verify().ok                                                                     # fooled: both local files agree
    v = led.verify(expected_head=saved_elsewhere)
    assert not v.ok and "truncated" in v.reason


def test_an_external_head_is_accepted_in_several_forms(ledger_path, tmp_path):
    populated(ledger_path)
    led = Ledger(ledger_path)
    h = led.head()
    saved = tmp_path / "saved.json"
    saved.write_text(json.dumps(h.to_dict()))
    for form in (h, (h.seq, h.hash), h.to_dict(), json.dumps(h.to_dict()), saved, str(saved)):
        assert led.verify(expected_head=form).ok
    assert not led.verify(expected_head=(h.seq, "0" * 64)).ok
    assert led.verify(expected_head=(0, "0" * 64)).ok
    with pytest.raises(ValueError):
        led.verify(expected_head=42)


# ---------------------------------------------------------------- versions
V1_DDL = """CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL, type TEXT NOT NULL, event_time TEXT NOT NULL,
 available_at TEXT NOT NULL, payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version','1');
INSERT INTO events VALUES (1,'x','s','session_start','2023-01-01T00:00:00Z','2026-01-01T00:00:00Z','{}','h','0','h1');"""


def test_a_schema_v1_ledger_is_refused_never_extended_and_can_be_archived(ledger_path):
    from asof import LedgerVersionError
    from asof.ledger import archive_legacy
    c = sqlite3.connect(ledger_path)
    c.executescript(V1_DDL)
    c.close()
    before = ledger_path.read_bytes()
    with pytest.raises(LedgerVersionError, match="will not mix versions"):
        Ledger(ledger_path)
    with pytest.raises(LedgerVersionError):
        Session("2023-01-01T00:00:00Z", ledger_path=ledger_path)
    assert ledger_path.read_bytes() == before                                                  # untouched by the refusal
    moved = archive_legacy(ledger_path)
    assert moved.exists() and moved.read_bytes() == before and not ledger_path.exists()
    s = Session("2023-01-01T00:00:00Z", ledger_path=ledger_path)                                # a fresh v2 chain starts cleanly
    s.close()
    assert s.verify_ledger().ok and sqlite3.connect(ledger_path).execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"


def test_an_unlabelled_legacy_database_is_also_refused(ledger_path):
    from asof import LedgerVersionError
    c = sqlite3.connect(ledger_path)
    c.execute("CREATE TABLE events (seq INTEGER, payload TEXT)")
    c.close()
    with pytest.raises(LedgerVersionError):
        Ledger(ledger_path)


# ---------------------------------------------------------------- concurrency (E: 50 concurrent writes)
def test_50_concurrent_writes_from_threads_keep_the_chain_valid(ledger_path):
    led = Ledger(ledger_path)

    def work(i):
        other = Ledger(ledger_path)                                                           # separate connections
        for n in range(10):
            other.append(f"s{i}", "x", event_time=T("2023-01-01T00:00:00Z"), available_at=T("2023-01-01T00:00:00Z"), payload={"i": i, "n": n})

    threads = [threading.Thread(target=work, args=(i,)) for i in range(5)]                    # 5 x 10 = 50
    [t.start() for t in threads]
    [t.join() for t in threads]
    v = led.verify()
    assert len(led) == 50 and v.ok and v.anchor == "ok"
    assert json.loads(led.head_path.read_text())["seq"] == 50


def _proc_work(args):
    path, i = args
    led = Ledger(path)
    for n in range(10):
        led.append(f"p{i}", "x", event_time=None, available_at=None, payload={"i": i, "n": n})


def test_50_concurrent_writes_from_separate_processes_keep_the_chain_valid(ledger_path):
    import multiprocessing as mp
    Ledger(ledger_path)
    with mp.get_context("spawn").Pool(5) as pool:
        pool.map(_proc_work, [(str(ledger_path), i) for i in range(5)])
    led = Ledger(ledger_path)
    v = led.verify()
    assert len(led) == 50 and v.ok and v.anchor in ("ok", "behind")                           # a racing process may leave the anchor a little behind: never ahead
    assert json.loads(led.head_path.read_text())["seq"] <= 50


def test_batches_are_atomic(ledger_path):
    led = Ledger(ledger_path)
    ok = EventSpec("s", "x", None, None, {"a": 1})
    bad = EventSpec("s", "x", None, None, {"a": object()})                                      # cannot be serialised
    with pytest.raises(TypeError):
        led.append_many([ok, bad])
    assert len(led) == 0
    evs = led.append_many([ok, EventSpec("s", "y", None, None, {"a": 2})])
    assert [e.seq for e in evs] == [1, 2] and len({e.recorded_at for e in evs}) == 1 and led.verify().ok


def test_the_ledger_file_is_private_and_unwritable_locations_are_errors(tmp_path):
    p = tmp_path / "sub" / "l.db"
    Ledger(p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600 and stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
    with pytest.raises(LedgerError):
        Ledger(tmp_path / "sub")                                                              # a directory is not a database


# ---------------------------------------------------------------- structural attacks that each event survives on its own
def test_deleting_an_event_and_renumbering_the_rest_is_detected(ledger_path):
    """seq is bound into every event's hash, so closing the gap by renumbering breaks the renumbered events."""
    populated(ledger_path)
    c = raw(ledger_path)
    c.execute("DELETE FROM events WHERE seq=4")
    c.execute("UPDATE events SET seq = seq - 1 WHERE seq > 4")
    c.commit()
    v = Ledger(ledger_path).verify(anchor="skip")
    assert not v.ok and v.first_bad_seq == 4


def test_splicing_in_an_event_from_another_chain_is_detected_by_the_link(ledger_path, tmp_path):
    """A row copied from another ledger at the same position is internally consistent: only the prev_hash link exposes it."""
    populated(ledger_path)
    other = tmp_path / "b" / "ledger.db"
    other.parent.mkdir()
    populated(other)                                                                           # a different chain, same shape
    row = sqlite3.connect(other).execute("SELECT * FROM events WHERE seq=6").fetchone()
    c = raw(ledger_path)
    c.execute("DELETE FROM events WHERE seq=6")
    c.execute("INSERT INTO events VALUES (" + ",".join("?" * len(row)) + ")", row)
    c.commit()
    v = Ledger(ledger_path).verify(anchor="skip")
    assert not v.ok and v.first_bad_seq == 6 and "prev_hash" in v.reason


def test_swapping_two_events_is_detected(ledger_path):
    populated(ledger_path)
    c = raw(ledger_path)
    c.execute("UPDATE events SET seq = -1 WHERE seq = 5")
    c.execute("UPDATE events SET seq = 5 WHERE seq = 6")
    c.execute("UPDATE events SET seq = 6 WHERE seq = -1")
    c.commit()
    assert not Ledger(ledger_path).verify(anchor="skip").ok


def test_head_export_is_safe_when_many_threads_with_separate_ledger_objects_write_at_once(ledger_path):
    """Regression: the temp file for head.json was named by process id only, so two threads of one process collided (FileNotFoundError after a committed write)."""
    from asof.ledger import Head
    Ledger(ledger_path)
    errors = []

    def work(i):
        led = Ledger(ledger_path)
        for n in range(150):
            try:
                led._write_head(Head(i * 1000 + n + 1, "a" * 64, None))
            except Exception as e:                                                             # noqa: BLE001
                errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    d = json.loads((ledger_path.parent / "head.json").read_text())                             # never a torn or missing file
    assert d["seq"] >= 1 and d["version"] == 1 and not list(ledger_path.parent.glob("*.tmp"))
