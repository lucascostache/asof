"""Universe-as-of: index membership by date, as it was knowable then, and queries restricted to it (the survivorship-bias guard)."""
import csv

import pytest

from asof import Record
from asof.errors import SourceError
from asof.privacy import digest
from asof.sources import Universe, members_as_of
from conftest import ListSource, T

ROWS = [
    {"symbol": "AAA", "action": "add", "effective": "2020-01-02", "announced": "2019-12-20"},
    {"symbol": "BBB", "action": "add", "effective": "2020-01-02", "announced": "2019-12-20"},
    {"symbol": "CCC", "action": "add", "effective": "2021-03-01", "announced": "2021-02-15"},
    {"symbol": "BBB", "action": "remove", "effective": "2022-06-30", "announced": "2022-06-10"},    # dropped from the index
    {"symbol": "DDD", "action": "add", "effective": "2022-09-01", "announced": "2022-08-15"},       # announced ahead of its effective date
    {"symbol": "AAA", "action": "remove", "effective": "2023-01-10", "announced": "2023-01-20"},    # reported ten days AFTER it took effect
]


def universe(rows=ROWS, name="sp500", **kw):
    return Universe(rows, name=name, **{"assume_tz": "UTC", **kw})


def members(make_session, as_of, rows=ROWS, **kw):
    s = make_session(as_of, **kw)
    s.add_source(universe(rows))
    return s, s.members("sp500")


# ---------------------------------------------------------------- members as of T
@pytest.mark.parametrize("as_of,expected", [
    ("2019-12-19T00:00:00Z", []),                                                  # nothing announced yet
    ("2019-12-25T00:00:00Z", []),                                                  # announced, not yet effective: pending, not members
    ("2020-01-03T00:00:00Z", ["AAA", "BBB"]),
    ("2021-04-01T00:00:00Z", ["AAA", "BBB", "CCC"]),
    ("2022-06-20T00:00:00Z", ["AAA", "BBB", "CCC"]),                               # BBB's removal is announced but not effective yet: still a member
    ("2022-07-15T00:00:00Z", ["AAA", "CCC"]),                                      # BBB is gone: it would be missing from today's table but it WAS in the index until now
    ("2022-09-05T00:00:00Z", ["AAA", "CCC", "DDD"]),
    ("2023-01-15T00:00:00Z", ["AAA", "CCC", "DDD"]),                               # AAA's removal took effect on the 10th but was only reported on the 20th: not knowable on the 15th
    ("2023-01-25T00:00:00Z", ["CCC", "DDD"]),
])
def test_members_are_what_had_been_announced_and_had_taken_effect_by_t(make_session, as_of, expected):
    assert members(make_session, as_of)[1] == expected


def test_today_s_membership_table_would_be_survivor_biased_and_the_as_of_one_is_not(make_session):
    today = members(make_session, "2024-01-01T00:00:00Z")[1]
    back_then = members(make_session, "2021-04-01T00:00:00Z")[1]
    assert today == ["CCC", "DDD"] and back_then == ["AAA", "BBB", "CCC"]
    assert "BBB" in back_then and "BBB" not in today and "DDD" not in back_then                # BBB (dropped) was a member then; DDD (a later winner) was not yet


def test_a_retroactive_announcement_never_reaches_an_earlier_session_but_does_reach_a_later_one(make_session):
    s1, early = members(make_session, "2023-01-15T00:00:00Z")
    s2, late = members(make_session, "2023-01-25T00:00:00Z")
    assert "AAA" in early and "AAA" not in late
    assert s1.receipt().blocked["future"] == 1 and s2.receipt().blocked["future"] == 0          # the early session was blocked from the report, and the receipt says so


def test_pending_changes_are_counted_in_the_receipt(make_session):
    s, m = members(make_session, "2022-08-20T00:00:00Z")                          # DDD's addition is announced (8/15) but takes effect on 9/1
    assert m == ["AAA", "CCC"]
    q = s.receipt().quality["membership"]
    assert q["pending_adds"] == 1 and q["pending_removes"] == 0 and q["n_members"] == 2 and q["members_sha256"] == digest(["AAA", "CCC"])
    s2, m2 = members(make_session, "2022-06-20T00:00:00Z")                         # BBB's removal is announced (6/10) but takes effect on 6/30
    q2 = s2.receipt().quality["membership"]
    assert m2 == ["AAA", "BBB", "CCC"] and q2["pending_removes"] == 1 and q2["pending_adds"] == 0


def test_the_membership_summary_flags_inconsistent_logs(make_session):
    rows = ROWS + [{"symbol": "AAA", "action": "add", "effective": "2020-06-01", "announced": "2020-05-01"},      # AAA is already a member
                   {"symbol": "ZZZ", "action": "remove", "effective": "2020-06-01", "announced": "2020-05-01"}]  # ZZZ was never added
    s, m = members(make_session, "2021-04-01T00:00:00Z", rows)
    q = s.receipt().quality
    assert q["membership"]["duplicate_adds"] == 1 and q["membership"]["orphan_removes"] == 1 and "membership_inconsistent" in q["flags"]
    assert m == ["AAA", "BBB", "CCC"]
    clean, _ = members(make_session, "2021-04-01T00:00:00Z")
    assert "membership_inconsistent" not in clean.receipt().quality["flags"]


def test_members_as_of_is_a_pure_replay_that_orders_by_effective_date_not_arrival():
    recs = [Record("u", "2", T("2020-01-05T00:00:00Z"), T("2020-01-01T00:00:00Z"), {"symbol": "X", "action": "remove"}, order=0),
            Record("u", "1", T("2020-01-02T00:00:00Z"), T("2020-01-01T00:00:00Z"), {"symbol": "X", "action": "add"}, order=1)]
    assert members_as_of(recs, T("2020-01-03T00:00:00Z")).members == ("X",) and members_as_of(recs, T("2020-01-06T00:00:00Z")).members == ()


# ---------------------------------------------------------------- queries restricted to the universe
PRICES = [{"symbol": s, "day": d, "px": 100 + i} for i, s in enumerate(["AAA", "BBB", "CCC", "DDD"]) for d in ("2022-07-11", "2022-07-12", "2022-07-13")]


def price_source():
    return ListSource("prices", [Record("prices", f"{p['symbol']}|{p['day']}", T(p["day"] + "T00:00:00Z"), T(p["day"] + "T21:00:00Z"), p, order=n) for n, p in enumerate(PRICES)])


def test_a_universe_restricted_query_serves_only_members_as_of_t_and_says_how_many_it_removed(make_session):
    s = make_session("2022-07-15T00:00:00Z")
    s.add_source(universe())
    s.add_source(price_source())
    everyone = s.query("prices")
    members_only = s.query("prices", universe=("sp500", "symbol"))
    assert len(everyone) == 12 and sorted({r.payload["symbol"] for r in members_only}) == ["AAA", "CCC"] and len(members_only) == 6
    u = s.receipt().universe
    assert u["source"] == "sp500" and u["n_members"] == 2 and u["excluded"] == 6 and u["members_sha256"] == digest(["AAA", "CCC"])
    members_receipt = s.receipt(last=False)[-2]
    assert u["universe_call_id"] == members_receipt.call_id and members_receipt.target == "sp500"      # the membership lookup is its own logged query


def test_the_restriction_also_applies_the_users_other_filters_and_limit(make_session):
    s = make_session("2022-07-15T00:00:00Z")
    s.add_source(universe())
    s.add_source(price_source())
    got = s.query("prices", universe=("sp500", "symbol"), day="2022-07-13", limit=1)
    assert len(got) == 1 and got[0].payload["symbol"] in ("AAA", "CCC") and s.receipt().universe["excluded"] == 2


def test_an_empty_universe_serves_nothing_rather_than_everything(make_session):
    s = make_session("2019-12-19T00:00:00Z")
    s.add_source(universe())
    s.add_source(ListSource("prices", [Record("prices", "p", T("2019-01-01T00:00:00Z"), T("2019-01-01T21:00:00Z"), {"symbol": "AAA"}, order=0)]))
    assert s.query("prices", universe=("sp500", "symbol")) == [] and s.receipt().universe["n_members"] == 0 and s.receipt().universe["excluded"] == 1


def test_the_receipt_summary_describes_the_universe_effect(make_session):
    s = make_session("2022-07-15T00:00:00Z")
    s.add_source(universe())
    s.add_source(price_source())
    s.query("prices", universe=("sp500", "symbol"))
    assert "universe sp500: 2 members at T; 6 matching record(s) removed because they were not members" in s.receipt().summary()


@pytest.mark.parametrize("privacy,has_field", [("full", True), ("hashes_only", False)])
def test_the_field_name_is_kept_in_full_and_only_hashed_in_hashes_only(make_session, privacy, has_field):
    s = make_session("2022-07-15T00:00:00Z", privacy=privacy)
    s.add_source(universe())
    s.add_source(price_source())
    s.query("prices", universe=("sp500", "symbol"))
    u = s.events("query")[-1].data()["universe"]
    assert ("field" in u) is has_field and u["field_sha256"] == digest("symbol")
    assert s.verify_ledger(anchor="skip").ok


def test_a_query_without_a_universe_carries_no_universe_key(make_session):
    s = make_session("2022-07-15T00:00:00Z")
    s.add_source(price_source())
    s.query("prices")
    assert "universe" not in s.events("query")[-1].data() and s.receipt().universe is None
    assert "universe" not in s.receipt().agent_view()


def test_the_agent_view_of_a_universe_query_does_not_reveal_what_was_excluded(make_session):
    s = make_session("2022-07-15T00:00:00Z")
    s.add_source(universe())
    s.add_source(price_source())
    s.query("prices", universe=("sp500", "symbol"))
    v = s.receipt().agent_view()
    assert "universe" not in v and "blocked" not in v and "quality" not in v


# ---------------------------------------------------------------- a change log is not a price series
def test_announcing_a_change_ahead_of_its_effective_date_is_normal_and_not_flagged_as_suspicious(make_session):
    s, _ = members(make_session, "2022-09-05T00:00:00Z")
    q = s.quality("sp500")
    assert q["kind"] == "change_log" and q["timing"]["available_before_event"] == 0 and q["timing"]["announced_ahead"] == 5      # five of the six changes were announced before they took effect (AAA's removal was reported late): counted, not flagged
    assert "available_before_event" not in q["flags"] and "gaps" not in q["flags"] and q["gaps"]["n_gaps"] is None and "too_few_points_for_gaps" not in q["flags"]
    rq = s.receipt().quality
    assert rq["kind"] == "change_log" and rq["available_before_event"] == 0 and "available_before_event" not in rq["flags"]
    assert rq["freshness"]["stale"] is None and rq["gaps_between_served"] is None                  # irregular by nature: staleness and gaps are not measured
    assert not any("available_before_event" in w for w in s.receipt().warnings)                    # and the query's own warning about suspicious timing does not fire either


def test_a_price_series_keeps_the_availability_before_event_check_as_a_control(make_session):
    s = make_session("2022-09-05T00:00:00Z")
    s.add_source(ListSource("p", [Record("p", "x", T("2022-09-02T00:00:00Z"), T("2022-09-01T00:00:00Z"), {"v": 1}, flags=("available_before_event",), order=0)]))      # as a real source flags it
    s.query("p")
    q = s.quality("p")
    assert q["kind"] == "series" and q["timing"]["available_before_event"] == 1 and q["timing"]["announced_ahead"] is None and "available_before_event" in q["flags"]
    assert any("available_before_event" in w for w in s.receipt().warnings)


# ---------------------------------------------------------------- the source itself
def test_a_universe_can_be_read_from_a_csv_file(make_session, tmp_path):
    p = tmp_path / "sp500.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "action", "effective", "announced"])
        w.writeheader()
        w.writerows(ROWS)
    s = make_session("2022-07-15T00:00:00Z")
    s.add_source(Universe(path=p, name="sp500", assume_tz="UTC"))
    assert s.members("sp500") == ["AAA", "CCC"]


def test_custom_column_names_work(make_session):
    rows = [{"ticker": "AAA", "what": "ADD", "start": "2020-01-02", "known": "2019-12-20"}]
    s = make_session("2020-02-01T00:00:00Z")
    s.add_source(Universe(rows, name="u", member="ticker", action="what", effective="start", announced="known", assume_tz="UTC"))
    assert s.members("u") == ["AAA"]


def test_a_change_with_no_announcement_date_is_withheld_not_assumed_known(make_session):
    rows = ROWS[:1] + [{"symbol": "BBB", "action": "add", "effective": "2020-01-02", "announced": None}]
    s = make_session("2021-01-01T00:00:00Z")
    s.add_source(universe(rows))
    assert s.members("sp500") == ["AAA"] and s.receipt().blocked["untimestamped"] == 1


def test_the_universe_record_ids_make_restated_announcements_visible(make_session):
    rows = ROWS[:1] + [{"symbol": "AAA", "action": "add", "effective": "2020-01-02", "announced": "2020-01-15"}]       # the same change published twice
    s = make_session("2021-01-01T00:00:00Z")
    s.add_source(universe(rows))
    assert s.quality("sp500")["duplicates"]["n_duplicate_rows"] == 0 and s.quality("sp500")["identity"] == "record_id"       # two versions of one change, different availability: a restatement of the same id
    assert s.members("sp500") == ["AAA"]


@pytest.mark.parametrize("bad,msg", [
    ([{"symbol": "A", "action": "swap", "effective": "2020-01-01", "announced": "2020-01-01"}], "must be 'add' or 'remove'"),
    ([{"symbol": "", "action": "add", "effective": "2020-01-01", "announced": "2020-01-01"}], "has no member"),
    ([{"action": "add", "effective": "2020-01-01", "announced": "2020-01-01"}], "column 'symbol' is not in the data"),
])
def test_bad_rows_are_refused_with_a_reason(make_session, bad, msg):
    s = make_session("2021-01-01T00:00:00Z")
    with pytest.raises(SourceError, match=msg):
        s.add_source(universe(bad))


def test_construction_and_use_errors(make_session, tmp_path):
    with pytest.raises(SourceError, match="rows or a path"):
        Universe(name="u")
    with pytest.raises(SourceError, match="rows or a path"):
        Universe([], path=tmp_path / "x.csv", name="u")
    s = make_session("2021-01-01T00:00:00Z")
    s.add_source(price_source())
    with pytest.raises(SourceError, match="not a Universe"):
        s.members("prices")
    with pytest.raises(SourceError, match="not a Universe"):
        s.query("prices", universe=("prices", "symbol"))
    with pytest.raises(SourceError, match="not a Universe"):
        s.query("prices", universe=("nope", "symbol"))
