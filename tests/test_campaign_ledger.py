"""Pre-launch campaign, area C: the ledger.

10. a tampering matrix: every way of changing a ledger file we could think of, and what `strathora verify` says to each
11. crash safety: the writing process is killed (SIGKILL) at random moments until 1,000 events have been written
12. concurrency: 50 threads and 5 processes writing to one ledger at once
(13, canonical JSON, is in test_campaign_canonical.py; 14, a million events, is measured by scripts/bench_baseline.py)

What a hash chain with a local anchor can and cannot show is stated by the tests themselves: three cases are NOT detectable without a head stored somewhere else, and the tests
assert that too, so the limit cannot quietly change.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from asof import Ledger, Session
from asof.cli import main
from asof.ledger import GENESIS, HASH_DOMAIN, EventSpec, _event_hash
from asof.records import sha256_hex

from conftest import ListSource, rec

WRITER = str(Path(__file__).with_name("campaign_writer.py"))
ROOT = Path(__file__).resolve().parent.parent
ENV = {**os.environ, "PYTHONPATH": str(ROOT)}
COLS = "seq,id,session_id,type,call_id,event_time,available_at,recorded_at,payload,payload_sha256,prev_hash,hash"


@pytest.fixture(scope="module")
def original(tmp_path_factory):
    """A real ledger of 27 events from a session (queries, blocks, a tool call, a decision, a clock move), closed, so it is one self-contained file with its anchor beside it."""
    d = tmp_path_factory.mktemp("orig")
    with Session("2023-01-21T14:00:00Z", ledger_path=d / "ledger.db") as s:
        s.add_source(ListSource("d", [rec(f"r{i}", f"2023-01-{10 + i:02d}T00:00:00Z", {"v": i, "note": f"row {i}"}) for i in range(1, 16)]))
        s.add_tool("t", lambda: [{"timestamp": "2023-01-01T00:00:00Z", "x": 1}, {"timestamp": "2024-01-01T00:00:00Z", "x": 2}])
        s.query("d")
        s.tool("t")
        s.decision("hold", reason="nothing to do")
        s.advance_to("2023-01-23T00:00:00Z")
        s.query("d", v=[1, 2, 3])
    n = len(Ledger(d / "ledger.db").events())
    assert n == 27 and main(["verify", "--ledger", str(d / "ledger.db")]) == 0
    return d


def copy(original, tmp_path, name="c"):
    d = tmp_path / name
    shutil.copytree(original, d)
    return d / "ledger.db"


def edit(path, *statements, drop=("events_no_update", "events_no_delete")):
    c = sqlite3.connect(path)
    for trig in drop:                                                                                      # the triggers stop accidents, not someone with the file
        c.execute(f"DROP TRIGGER IF EXISTS {trig}")
    for st in statements:
        c.execute(*st) if isinstance(st, tuple) else c.execute(st)
    c.commit()
    c.close()


def verify(path, capsys, *extra):
    """Run `strathora verify` as the command does. Returns (exit code, stdout, stderr). Any exception other than a clean exit is a crash, and a crash is a failed test."""
    capsys.readouterr()
    code = main(["verify", "--ledger", str(path), *extra])
    out = capsys.readouterr()
    return code, out.out, out.err


def rows(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return c.execute(f"SELECT {COLS} FROM events ORDER BY seq").fetchall()
    finally:
        c.close()


def rehash(path, from_seq=1):
    """What a careful forger does after editing: recompute every payload hash, link and event hash from `from_seq` to the end, so the chain is self-consistent again."""
    c = sqlite3.connect(path)
    for trig in ("events_no_update", "events_no_delete"):
        c.execute(f"DROP TRIGGER IF EXISTS {trig}")
    prev = GENESIS if from_seq == 1 else c.execute("SELECT hash FROM events WHERE seq=?", (from_seq - 1,)).fetchone()[0]
    for r in c.execute(f"SELECT {COLS} FROM events WHERE seq>=? ORDER BY seq", (from_seq,)).fetchall():
        seq, id_, sid, type_, call, ev, av, recd, payload, _, _, _ = r
        psha = sha256_hex(payload)
        h = _event_hash(seq, prev, id_, sid, type_, call, ev, av, recd, psha, HASH_DOMAIN)
        c.execute("UPDATE events SET payload_sha256=?, prev_hash=?, hash=? WHERE seq=?", (psha, prev, h, seq))
        prev = h
    c.commit()
    last = c.execute("SELECT seq, hash, recorded_at FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    c.close()
    return {"version": 1, "seq": last[0], "hash": last[1], "recorded_at": last[2]}


# ------------------------------------------------------------------ 10. the tampering matrix
K = 12          # the event most cases alter
FIELD_EDITS = {
    "one character of a payload":            ("UPDATE events SET payload = replace(payload, '\"count\":1', '\"count\":0') WHERE seq = 18", 18, "payload does not match its recorded hash"),
    "one byte of a payload (a space added)": (f"UPDATE events SET payload = payload || ' ' WHERE seq = {K}", K, "payload does not match its recorded hash"),
    "the payload and its hash together":     (f"UPDATE events SET payload = '{{}}', payload_sha256 = '{sha256_hex('{}')}' WHERE seq = {K}", K, "event hash does not match its contents"),
    "the event type":                        (f"UPDATE events SET type = 'forged' WHERE seq = {K}", K, "a field was altered"),
    "the session id":                        (f"UPDATE events SET session_id = 'someone-else' WHERE seq = {K}", K, "a field was altered"),
    "the call id":                           (f"UPDATE events SET call_id = 'ffffffffffff' WHERE seq = {K}", K, "a field was altered"),
    "the event id":                          (f"UPDATE events SET id = CASE substr(id,1,1) WHEN 'f' THEN 'e' ELSE 'f' END || substr(id, 2) WHERE seq = {K}", K, "a field was altered"),
    "event_time by one microsecond":         (f"UPDATE events SET event_time = '2023-01-21T14:00:00.000001Z' WHERE seq = {K}", K, "a field was altered"),
    "available_at":                          (f"UPDATE events SET available_at = '2000-01-01T00:00:00Z' WHERE seq = {K}", K, "a field was altered"),
    "available_at set to nothing":           (f"UPDATE events SET available_at = NULL WHERE seq = {K}", K, "a field was altered"),
    "recorded_at (the wall clock)":          (f"UPDATE events SET recorded_at = '2001-01-01T00:00:00Z' WHERE seq = {K}", K, "a field was altered"),
    "one character of an event hash":        (f"UPDATE events SET hash = CASE substr(hash,1,1) WHEN '0' THEN '1' ELSE '0' END || substr(hash, 2) WHERE seq = {K}", K, "event hash does not match its contents"),
    "one character of a link (prev_hash)":   (f"UPDATE events SET prev_hash = CASE substr(prev_hash,1,1) WHEN '0' THEN '1' ELSE '0' END || substr(prev_hash, 2) WHERE seq = {K}", K, "prev_hash does not match"),
    "the first event":                       ("UPDATE events SET type = 'forged' WHERE seq = 1", 1, "a field was altered"),
    "the last event":                        ("UPDATE events SET type = 'forged' WHERE seq = 27", 27, "a field was altered"),
}


@pytest.mark.parametrize("what", list(FIELD_EDITS))
def test_changing_any_part_of_any_event_fails_verification_at_that_event(original, tmp_path, capsys, what):
    sql, seq, reason = FIELD_EDITS[what]
    p = copy(original, tmp_path)
    before = rows(p)
    edit(p, sql)
    assert rows(p) != before, "the edit changed nothing: the case is not testing anything"
    code, out, err = verify(p, capsys)
    assert code == 1 and out == "" and err.startswith("FAIL:") and reason in err and f"(at event {seq})" in err, err
    code, out, _ = verify(p, capsys, "--json")
    d = json.loads(out)
    assert code == 1 and d["ok"] is False and d["first_bad_seq"] == seq                                    # the same answer for a program


STRUCTURE = {
    # name: (what is done, exit code, text that must be in the message, the event it must name or None)
    "an event deleted from the middle":             (lambda p: edit(p, f"DELETE FROM events WHERE seq = {K}"), 1, f"sequence gap: expected event {K}, found {K + 1}", K + 1),
    "the first event deleted":                      (lambda p: edit(p, "DELETE FROM events WHERE seq = 1"), 1, "sequence gap: expected event 1, found 2", 2),
    "the last event deleted":                       (lambda p: edit(p, "DELETE FROM events WHERE seq = 27"), 1, "the ledger was truncated", 27),
    "the last ten events deleted":                  (lambda p: edit(p, "DELETE FROM events WHERE seq > 17"), 1, "records event 27 but the ledger ends at event 17", 27),
    "every event deleted":                          (lambda p: edit(p, "DELETE FROM events"), 1, "records event 27 but the ledger ends at event 0", 27),
    "two neighbouring events swapped":              (lambda p: edit(p, f"UPDATE events SET seq = -1 WHERE seq = {K}", f"UPDATE events SET seq = {K} WHERE seq = {K + 1}", f"UPDATE events SET seq = {K + 1} WHERE seq = -1"), 1, "", K),
    "two distant events swapped":                   (lambda p: edit(p, "UPDATE events SET seq = -1 WHERE seq = 5", "UPDATE events SET seq = 5 WHERE seq = 25", "UPDATE events SET seq = 25 WHERE seq = -1"), 1, "", 5),
    "an event duplicated at the end":               (lambda p: edit(p, f"INSERT INTO events(seq,id,session_id,type,call_id,event_time,available_at,recorded_at,payload,payload_sha256,prev_hash,hash) "
                                                                       f"SELECT 28, id || 'x', session_id,type,call_id,event_time,available_at,recorded_at,payload,payload_sha256,prev_hash,hash FROM events WHERE seq = {K}"), 1, "", 28),
    "an event inserted in the middle (later ones renumbered)": (lambda p: edit(p, f"UPDATE events SET seq = seq + 100 WHERE seq > {K}", f"UPDATE events SET seq = seq - 99 WHERE seq > {K}",
                                                                                 f"INSERT INTO events(seq,id,session_id,type,call_id,event_time,available_at,recorded_at,payload,payload_sha256,prev_hash,hash) "
                                                                                 f"SELECT {K + 1}, 'inserted', session_id,'decision',NULL,event_time,available_at,recorded_at,'{{}}','{sha256_hex('{}')}',hash,'00' FROM events WHERE seq = {K}"), 1, "", K + 1),
    "an event inserted, then EVERY hash recomputed": (lambda p: (edit(p, f"UPDATE events SET payload = '{{\"forged\":true}}' WHERE seq = {K}"), rehash(p, K)), 1, "its hash differs from the anchor head.json: the chain was rewritten", 27),
    "a payload changed, then every hash from there to the end recomputed": (lambda p: (edit(p, "UPDATE events SET payload = replace(payload, '\"count\":1', '\"count\":0') WHERE seq = 18"), rehash(p, 18)), 1, "the chain was rewritten", 27),
    "head.json: the hash edited":                   (lambda p: p.with_name("head.json").write_text(json.dumps({**json.loads(p.with_name("head.json").read_text()), "hash": "0" * 64})), 1, "the chain was rewritten", 27),
    "head.json: the event number raised":           (lambda p: p.with_name("head.json").write_text(json.dumps({**json.loads(p.with_name("head.json").read_text()), "seq": 28})), 1, "the ledger was truncated", 28),
    "head.json: deleted":                           (lambda p: p.with_name("head.json").unlink(), 1, "is missing: truncation of the end of the ledger cannot be ruled out", None),
    "head.json: not JSON any more":                 (lambda p: p.with_name("head.json").write_text("{not json"), 1, "cannot be read", None),
    "head.json: emptied":                           (lambda p: p.with_name("head.json").write_text(""), 1, "cannot be read", None),
    "the whole ledger replaced by another valid chain, the anchor kept": (lambda p: _replace_with_another_chain(p, keep_anchor=True), 1, "", None),
    "the file cut in half":                         (lambda p: _truncate_file(p, 0.5), (1, 2), "", None),
    "the last page of the file cut off":            (lambda p: _truncate_file(p, None), (1, 2), "", None),
    "the file emptied":                             (lambda p: p.write_bytes(b""), 1, "records event 27 but the ledger ends at event 0", 27),
    "the file replaced by text":                    (lambda p: p.write_text("this is not a ledger\n" * 300), (1, 2), "", None),
}


def _replace_with_another_chain(p, keep_anchor):
    anchor = p.with_name("head.json").read_text()
    p.unlink()
    p.with_name("head.json").unlink()
    with Session("2023-01-21T14:00:00Z", ledger_path=p) as s:                                              # a different session's ledger, perfectly valid in itself, of the same length or longer
        s.add_source(ListSource("d", [rec(f"z{i}", "2023-01-01T00:00:00Z") for i in range(40)]))
        s.query("d")
    if keep_anchor:
        p.with_name("head.json").write_text(anchor)


def _truncate_file(p, fraction):
    data = p.read_bytes()
    p.write_bytes(data[:int(len(data) * fraction)] if fraction else data[:-4096])


@pytest.mark.parametrize("what", list(STRUCTURE))
def test_removing_adding_reordering_or_replacing_events_fails_verification(original, tmp_path, capsys, what):
    do, want_code, reason, seq = STRUCTURE[what]
    p = copy(original, tmp_path)
    do(p)
    code, out, err = verify(p, capsys)
    assert code in (want_code if isinstance(want_code, tuple) else (want_code,)), (code, out, err)
    assert out == "" and err and "Traceback" not in err and reason in err, err
    if seq is not None:
        assert f"event {seq}" in err, err                                                                  # the event (or the anchor's event) it stops at is named
    if code == 1:
        assert err.startswith("FAIL:")


def test_three_things_a_local_anchor_cannot_show_and_a_head_stored_elsewhere_can(original, tmp_path, capsys):
    """The ledger is not signed, and head.json sits beside it. Someone who can rewrite both can: (1) append events that are perfectly well formed, (2) cut the end off and move the
    anchor back, (3) rebuild the whole chain and write a matching anchor. `verify` alone says OK to all three. A head copied somewhere else beforehand (`strathora head`) catches 2 and 3;
    1 is what an unsigned log is: it shows what was written, not who wrote it. These are documented limits (README, LIMITATIONS); this test keeps them honest."""
    saved = (tmp_path / "saved-head.json")
    saved.write_text((original / "head.json").read_text())                                                 # what the operator stored elsewhere, before the attack

    p = copy(original, tmp_path, "append")                                                                 # 1. a forged event appended with correct hashes, anchor left alone
    last = rows(p)[-1]
    forged = '{"action":"buy","forged":true}'
    h = _event_hash(28, last[11], "f" * 32, last[2], "decision", None, last[5], last[6], last[7], sha256_hex(forged), HASH_DOMAIN)
    edit(p, ("INSERT INTO events(seq,id,session_id,type,call_id,event_time,available_at,recorded_at,payload,payload_sha256,prev_hash,hash) VALUES (28,?,?,?,NULL,?,?,?,?,?,?,?)",
             ("f" * 32, last[2], "decision", last[5], last[6], last[7], forged, sha256_hex(forged), last[11], h)))
    code, out, _ = verify(p, capsys)
    assert code == 0 and "tail anchor: behind" in out and "warning:" in out and "1 event(s) behind the ledger" in out      # not a failure, but it is said: the anchor does not cover the new event
    code, out, _ = verify(p, capsys, "--head", str(saved))
    assert code == 0 and "behind" in out                                                                   # the saved head agrees with the first 27 events; it cannot speak for a 28th

    p = copy(original, tmp_path, "cut")                                                                    # 2. the end cut off and the local anchor moved back to match
    edit(p, "DELETE FROM events WHERE seq > 20")
    r20 = rows(p)[-1]
    p.with_name("head.json").write_text(json.dumps({"version": 1, "seq": 20, "hash": r20[11], "recorded_at": r20[7]}))
    assert verify(p, capsys)[0] == 0                                                                       # fooled
    code, _, err = verify(p, capsys, "--head", str(saved))
    assert code == 1 and "the ledger was truncated: the head you supplied records event 27 but the ledger ends at event 20" in err      # not fooled

    p = copy(original, tmp_path, "rebuilt")                                                                # 3. one payload changed, every hash recomputed, and the local anchor rewritten to match
    edit(p, "UPDATE events SET payload = replace(payload, '\"count\":1', '\"count\":0') WHERE seq = 18")
    p.with_name("head.json").write_text(json.dumps(rehash(p, 18)))
    assert verify(p, capsys)[0] == 0                                                                       # fooled
    code, _, err = verify(p, capsys, "--head", str(saved))
    assert code == 1 and "event 27 exists but its hash differs from the head you supplied: the chain was rewritten" in err              # not fooled

    readme = (ROOT / "README.md").read_text() + (ROOT / "docs" / "pilot" / "LIMITATIONS.md").read_text()
    assert "Anyone who can rewrite the ledger file can also rewrite the anchor beside it" in readme and "not signed" in readme           # and the limits are written down


def test_flipping_any_single_byte_of_the_file_is_caught_or_changes_nothing(original, tmp_path, capsys):
    """400 single-byte flips at random offsets. `verify` must never crash. If it says OK, the events read back must be exactly the original ones (the byte was in unused space or in an
    index, which holds copies, not the events themselves)."""
    src = original / "ledger.db"
    data = src.read_bytes()
    good = rows(src)
    rnd = random.Random(20261111)
    d = tmp_path / "flip"
    d.mkdir()
    p = d / "ledger.db"
    caught = harmless = 0
    undetected = []
    offsets = sorted({rnd.randrange(len(data)) for _ in range(400)} | {0, 1, 15, 16, 18, 24, 28, 99, 100, len(data) - 1})
    for off in offsets:
        b = bytearray(data)
        b[off] ^= 1 << rnd.randrange(8)
        p.write_bytes(bytes(b))
        shutil.copy(original / "head.json", d / "head.json")
        for extra in d.glob("ledger.db-*"):
            extra.unlink()
        try:
            code, out, err = verify(p, capsys)
        except BaseException as e:  # noqa: BLE001 - the point of the test
            pytest.fail(f"verify crashed on a flipped byte at offset {off}: {type(e).__name__}: {e}")
        assert "Traceback" not in err
        if code == 0:
            try:
                same = rows(p) == good
            except sqlite3.Error:
                same = False
            if same:
                harmless += 1
            else:
                undetected.append(off)
        else:
            assert code in (1, 2) and err
            caught += 1
    assert not undetected, f"verify said OK although the events differ, for flips at offsets {undetected[:10]}"
    assert caught + harmless == len(offsets) and caught > 50, (caught, harmless)


# ------------------------------------------------------------------ 11. crash safety
@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL")
def test_killing_the_writer_at_random_moments_never_leaves_a_broken_or_half_written_ledger(tmp_path):
    path = tmp_path / "ledger.db"
    rnd = random.Random(42)
    kills = total = behind = 0
    while total < 1000 or kills < 10:                                                                   # both, whatever the machine's speed (a fast CI runner reached 1,145 events in 9 kills)
        p = subprocess.Popen([sys.executable, WRITER, str(path), f"w{kills}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ENV)
        assert p.stdout.readline().strip() == "ready", p.stderr.read()
        time.sleep(rnd.choice([0.0, 0.001, 0.003, 0.008, 0.02, 0.05]))                                    # sometimes straight away, sometimes mid-stream
        os.kill(p.pid, signal.SIGKILL)
        p.wait()
        p.stdout.close()
        p.stderr.close()
        kills += 1
        led = Ledger(path)                                                                                 # "after restart"
        v = led.verify()
        assert v.ok, (kills, v.reason)
        assert v.anchor in ("ok", "behind")                                                                # the anchor may be one write behind (a kill between a commit and its export); it is never ahead
        behind += v.anchor == "behind"
        events = led.events()
        led.close()
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        groups: dict[tuple, list[int]] = {}
        for e in events:
            d = e.data()
            groups.setdefault((d["writer"], d["write"]), []).append(d["part"])
            assert d["of"] in (1, 3)
        for (writer, write), parts in groups.items():
            want = [0, 1, 2] if len(parts) > 1 or any(e.data()["of"] == 3 for e in events if (e.data()["writer"], e.data()["write"]) == (writer, write)) else [0]
            assert parts == want, f"a half-written batch survived kill {kills}: {writer} write {write} has parts {parts}"
        per_writer: dict[str, list[int]] = {}
        for (writer, write) in groups:
            per_writer.setdefault(writer, []).append(write)
        for writer, writes in per_writer.items():
            assert writes == list(range(1, len(writes) + 1)), f"{writer} lost a write in the middle: {writes[:20]}"      # only the tail of a killed writer can be missing
        total = len(events)
        assert kills < 400, "the writer is being killed before it writes anything"
    assert total >= 1000 and kills >= 10, (total, kills)
    led = Ledger(path)
    led.append("after", "single", event_time=None, available_at=None, payload={"writer": "survivor", "write": 1, "part": 0, "of": 1})      # and the ledger is still writable
    assert led.verify().ok and led.verify().anchor == "ok"
    led.close()
    assert main(["verify", "--ledger", str(path)]) == 0
    leftovers = sorted(x.name for x in tmp_path.iterdir() if x.name not in ("ledger.db", "head.json"))
    assert leftovers == [], f"files left behind by killed writers: {leftovers[:5]} ({len(leftovers)} in all)"


# ------------------------------------------------------------------ 12. concurrency
def test_50_threads_and_5_processes_writing_at_once_keep_one_valid_chain(tmp_path):
    path = tmp_path / "ledger.db"
    Ledger(path).close()
    T = datetime(2023, 1, 21, 14, tzinfo=timezone.utc)
    procs = [subprocess.Popen([sys.executable, WRITER, str(path), f"p{i}", "150", "4"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ENV) for i in range(5)]      # 5 processes, 4 threads each
    for p in procs:
        assert p.stdout.readline().strip() == "ready", p.stderr.read()
    led = Ledger(path)
    errors: list[BaseException] = []
    gate = threading.Barrier(50)

    def run(i):
        try:
            gate.wait()
            for n in range(1, 21):
                if n % 2:
                    led.append_many([EventSpec("conc", "batch", T, T, {"writer": f"t{i}", "write": n, "part": k, "of": 3}) for k in range(3)])
                else:
                    led.append("conc", "single", event_time=T, available_at=T, payload={"writer": f"t{i}", "write": n, "part": 0, "of": 1})
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=run, args=(i,)) for i in range(50)]                                 # 50 threads sharing one Ledger object in this process
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    for p in procs:
        out, err = p.communicate(timeout=300)
        assert p.returncode == 0, err
    assert not errors, errors[:1]
    led.close()
    led = Ledger(path)
    events = led.events()
    expected = 50 * (10 * 3 + 10) + 5 * 4 * 150
    assert len(events) == expected == 5000
    assert [e.seq for e in events] == list(range(1, expected + 1))                                         # no gap, no duplicate sequence number
    assert len({e.id for e in events}) == expected and len({e.hash for e in events}) == expected
    v = led.verify()
    assert v.ok and v.anchor == "ok" and v.n_events == expected
    order: dict[str, list[tuple[int, int]]] = {}
    for e in events:
        d = e.data()
        order.setdefault(d["writer"], []).append((d["write"], d["part"]))
    assert len(order) == 70                                                                                # 50 threads here, 20 in the other processes
    for writer, seen in order.items():
        assert seen == sorted(seen), f"{writer}'s own writes are out of order in the chain"               # each writer's events are in the order it wrote them, batches unbroken
    batches = [e for e in events if e.type == "batch"]
    for i in range(0, len(batches) - 2):
        d = batches[i].data()
        if d["part"] == 0:
            nxt = [events[batches[i].seq].data(), events[batches[i].seq + 1].data()]                      # the two events right after it in the chain
            assert [(x["writer"], x["write"], x["part"]) for x in nxt] == [(d["writer"], d["write"], 1), (d["writer"], d["write"], 2)]      # a batch is never interleaved with another writer
    led.close()
    assert main(["verify", "--ledger", str(path)]) == 0
