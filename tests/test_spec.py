"""SPEC.md must describe what the code does. This is the test that keeps them from drifting apart."""
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from asof import Session
from spec_probe import probe

from conftest import ListSource, rec

SPEC = (Path(__file__).resolve().parent.parent / "SPEC.md").read_text()


def block(name):
    m = re.search(rf"<!-- {name}:start -->\s*```\w*\n(.*?)```\s*<!-- {name}:end -->", SPEC, re.S)
    assert m, name
    return m.group(1)


def populated(path):
    s = Session("2023-01-21T14:00:00Z", ledger_path=path)
    s.add_source(ListSource("d", [rec("a", "2023-01-01T00:00:00Z", {"v": 1}, event_time="2022-12-31T00:00:00Z"), rec("f", "2023-06-01T00:00:00Z", {"v": 2})]))
    s.query("d")
    s.decision("hold")
    s.advance_to("2023-02-01T00:00:00Z")
    s.close()
    return s


@pytest.fixture
def verifier():
    ns = {}
    exec(block("asof-reference-verifier"), ns)
    return ns["verify"]


def test_the_reference_verifier_in_the_spec_accepts_a_real_ledger(ledger_path, verifier):
    populated(ledger_path)
    from asof import Ledger
    n = verifier(str(ledger_path), str(ledger_path.parent / "head.json"))
    assert n == len(Ledger(ledger_path)) and n > 8


@pytest.mark.parametrize("column, value, expect", [("payload", '{"x":1}', "payload altered"), ("recorded_at", "2001-01-01T00:00:00Z", "altered"), ("event_time", "2001-01-01T00:00:00Z", "altered"),
                                                   ("available_at", None, "altered"), ("call_id", "x", "altered"), ("type", "decision", "altered"), ("session_id", "x", "altered"),
                                                   ("prev_hash", "f" * 64, "broken link"), ("seq", 99, "gap")])
def test_the_reference_verifier_rejects_tampering(ledger_path, tmp_path, verifier, column, value, expect):
    populated(ledger_path)
    copy = tmp_path / "copy.db"
    shutil.copy(ledger_path, copy)
    c = sqlite3.connect(copy)
    c.execute("DROP TRIGGER events_no_update")
    c.execute(f"UPDATE events SET {column}=? WHERE seq=6", (value,))
    c.commit()
    with pytest.raises(AssertionError, match=expect):
        verifier(str(copy))


def test_the_reference_verifier_rejects_truncation_given_the_anchor_and_not_without_it(ledger_path, tmp_path, verifier):
    populated(ledger_path)
    head = ledger_path.parent / "head.json"
    c = sqlite3.connect(ledger_path)
    c.execute("DROP TRIGGER events_no_delete")
    c.execute("DELETE FROM events WHERE seq > 5")
    c.commit()
    assert verifier(str(ledger_path)) == 5                                                          # the chain alone cannot see it
    with pytest.raises(AssertionError, match="truncated"):
        verifier(str(ledger_path), str(head))


def test_the_event_schema_block_matches_what_the_library_emits():
    documented = json.loads(block("asof-event-schema"))
    assert documented == {"full": probe("full"), "hashes_only": probe("hashes_only")}


def test_every_event_type_the_library_emits_is_listed_in_the_spec_prose():
    types = set(probe("full"))
    for t in types:
        assert f"`{t}`" in SPEC, t


def test_hash_clock_and_privacy_rules_in_the_spec_hold_for_stored_events(ledger_path):
    import hashlib
    s = populated(ledger_path)
    for e in s.events():
        expect = hashlib.sha256(json.dumps(["asof-ledger/3", e.seq, e.prev_hash, e.id, e.session_id, e.type, e.call_id, e.event_time, e.available_at, e.recorded_at, e.payload_sha256],
                                           separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        assert e.hash == expect
        assert e.payload_sha256 == hashlib.sha256(e.payload.encode()).hexdigest()
        assert e.recorded_at.endswith("Z") and (e.event_time is None or e.event_time.endswith("Z"))
        if e.type == "record_served":
            assert (e.event_time, e.available_at) == ("2022-12-31T00:00:00Z", "2023-01-01T00:00:00Z")          # the record's own two clocks, not T
        elif e.type != "tool_item_served":
            assert e.event_time == e.available_at                                                    # agent actions: both are T


def test_the_spec_states_the_three_honest_limits():
    low = SPEC.lower()
    for needle in ("truth of any timestamp", "model forgot anything", "tail truncation without a trusted copy", "only as trustworthy as where it is stored"):
        assert needle in low, needle
