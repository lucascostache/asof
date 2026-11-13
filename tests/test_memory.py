"""Time-stamped agent memory: a memory learned after T is never recalled at T. The store is a normal AsOf source, so the boundary, restatement and receipt rules are the library's own."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from asof import Memory, Session
from asof.memory import MemoryWriteError, rank

T2023 = "2023-06-01T12:00:00Z"


def mem():
    m = Memory()
    m.remember("The 2024 earnings call revealed a guidance cut for Acme", learned_at="2024-02-01T21:00:00Z", tags=["acme"])
    m.remember("Acme opened a new regional office in Austin", learned_at="2023-01-10T09:00:00Z", tags=["acme", "ops"])
    m.remember("Beta signed a supply agreement with Gamma", learned_at="2023-03-05T10:00:00Z", tags=["beta"])
    return m


def texts(r):
    return [x.text for x in r.items]


# ---------------------------------------------------------------- the rule
def test_a_memory_written_in_2024_is_not_recalled_as_of_2023():
    r = mem().recall("Acme earnings guidance", as_of=T2023)
    assert all("2024" not in t for t in texts(r)) and r.withheld_future == 1                    # counted for the operator, not returned
    assert texts(mem().recall("Acme earnings guidance", as_of="2024-03-01T00:00:00Z"))[0].startswith("The 2024 earnings call")


def test_the_boundary_is_inclusive_a_memory_learned_exactly_at_T_is_recalled_and_a_microsecond_later_is_not():
    m = Memory()
    at = datetime(2023, 6, 1, 12, tzinfo=timezone.utc)
    m.remember("exactly at T", learned_at=at)
    m.remember("one microsecond later", learned_at=at + timedelta(microseconds=1))
    r = m.recall("T later exactly microsecond", as_of=at)
    assert texts(r) == ["exactly at T"] and r.withheld_future == 1
    assert texts(m.recall("T later exactly microsecond", as_of=at + timedelta(microseconds=1)))[0] in ("exactly at T", "one microsecond later") and len(m.recall("T later exactly microsecond", as_of=at + timedelta(microseconds=1)).items) == 2


def test_recall_never_returns_a_future_memory_for_any_t_property():
    m = Memory()
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    for d in range(0, 400, 7):
        m.remember(f"note number {d} about acme", learned_at=base + timedelta(days=d))
    for d in range(0, 420, 13):
        t = base + timedelta(days=d)
        for x in m.recall("acme note", as_of=t, limit=100).items:
            assert x.learned_at <= t


def test_a_naive_time_a_missing_time_and_empty_text_are_refused():
    m = Memory()
    for bad in ("2023-01-01T00:00:00", "", "soon", None):
        with pytest.raises(MemoryWriteError):
            m.remember("x", learned_at=bad)                                                      # type: ignore[arg-type]
    for t in ("", "   ", None):
        with pytest.raises(MemoryWriteError):
            m.remember(t, learned_at=T2023)                                                      # type: ignore[arg-type]
    assert len(m) == 0


# ---------------------------------------------------------------- restatement, ranking, tags
def test_a_rewritten_note_replaces_the_old_one_only_from_when_it_was_learned():
    m = Memory()
    m.remember("Acme target price is 100", learned_at="2023-01-01T00:00:00Z", key="acme-target")
    m.remember("Acme target price is 140", learned_at="2023-09-01T00:00:00Z", key="acme-target")
    assert texts(m.recall("Acme target price", as_of="2023-06-01T00:00:00Z")) == ["Acme target price is 100"]
    assert texts(m.recall("Acme target price", as_of="2023-10-01T00:00:00Z")) == ["Acme target price is 140"]
    assert len(m) == 2                                                                           # both versions are kept


def test_ranking_prefers_the_better_match_then_the_more_recent_and_drops_non_matches():
    r = mem().recall("Acme regional office Austin", as_of="2024-12-31T00:00:00Z", limit=5)
    assert texts(r)[0] == "Acme opened a new regional office in Austin" and all("Beta" not in t for t in texts(r))
    assert texts(mem().recall("zebra", as_of="2024-12-31T00:00:00Z")) == [] and texts(mem().recall("", as_of="2024-12-31T00:00:00Z")) == []
    tie = Memory()
    tie.remember("alpha note", learned_at="2023-01-01T00:00:00Z")
    tie.remember("alpha note", learned_at="2023-02-01T00:00:00Z")
    got = tie.recall("alpha", as_of="2023-03-01T00:00:00Z").items
    assert got[0].learned_at > got[1].learned_at                                                 # equal scores: the more recent first
    assert len(mem().recall("acme", as_of="2024-12-31T00:00:00Z", limit=1).items) == 1


def test_tags_narrow_the_recall():
    r = mem().recall("acme", as_of="2024-12-31T00:00:00Z", tags=["ops"])
    assert texts(r) == ["Acme opened a new regional office in Austin"] and rank([], "x") == []


# ---------------------------------------------------------------- append-only and persistence
def test_memories_are_append_only_and_persist_in_a_file(tmp_path):
    p = tmp_path / "notes" / "mem.db"
    m = Memory(path=p)
    mid = m.remember("kept", learned_at=T2023)
    m.close()
    raw = sqlite3.connect(p)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("UPDATE memories SET text='changed' WHERE id=?", (mid,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("DELETE FROM memories WHERE id=?", (mid,))
    raw.close()
    again = Memory(path=p)
    assert texts(again.recall("kept", as_of=T2023)) == ["kept"] and len(again) == 1


# ---------------------------------------------------------------- inside a session
def test_a_session_recall_is_time_locked_logged_and_the_receipt_counts_what_was_withheld(make_session):
    s = make_session(T2023)
    s.add_source(mem())
    got = s.recall("memory", "Acme earnings guidance")
    assert all("2024" not in x.text for x in got)
    r = s.receipt()
    assert r.kind == "query" and r.blocked["future"] == 1 and r.served == 2                      # two served (any), the 2024 note withheld
    assert len(s.ledger.events(session_id=s.id, type="query")) == 1 and s.verify_ledger().ok
    s.advance_to("2024-03-01T00:00:00Z")
    later = s.recall("memory", "Acme earnings guidance")
    assert later[0].text.startswith("The 2024 earnings call") and s.receipt().blocked["future"] == 0


def test_a_session_before_every_memory_recalls_nothing_and_a_missing_source_is_an_error(make_session):
    s = make_session("2022-01-01T00:00:00Z")
    s.add_source(mem())
    assert s.recall("memory", "acme") == [] and s.receipt().blocked["future"] == 3
    with pytest.raises(Exception):
        s.recall("nope", "acme")
