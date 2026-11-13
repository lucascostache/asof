"""Bar rules: a bar is available at its close, partial bars are never served unless allowed (and then labelled), higher timeframes are locked, and intrabar ambiguity is flagged."""
import csv

import pytest

from asof import Session
from asof.errors import SourceError
from asof.sources import Bars, intrabar_ambiguous
from conftest import T


def five_min(rows=None, **kw):
    rows = rows or [{"open_time": f"2023-01-20T14:{m:02d}:00Z", "open": 10 + i, "high": 12 + i, "low": 9 + i, "close": 11 + i, "volume": 100} for i, m in enumerate((0, 5, 10, 15))]
    return Bars(rows, name="b", timeframe="5m", **kw)


def served_opens(s, **kw):
    return [r.payload["open_time"][11:16] for r in s.query("b", **kw)]


# ---------------------------------------------------------------- a bar is available at its close
def test_a_bar_is_available_at_its_close_and_not_before(make_session):
    src = five_min()
    recs = list(src.records())
    assert [r.available_at for r in recs][:2] == [T("2023-01-20T14:05:00Z"), T("2023-01-20T14:10:00Z")] and recs[0].event_time == T("2023-01-20T14:00:00Z")
    assert recs[0].payload["bar_close"] == "2023-01-20T14:05:00Z" and recs[0].payload["timeframe"] == "5m"
    for t, expect in (("2023-01-20T14:04:59Z", []), ("2023-01-20T14:05:00Z", ["14:00"]), ("2023-01-20T14:09:59.999999Z", ["14:00"]), ("2023-01-20T14:10:00Z", ["14:00", "14:05"])):
        s = make_session(t)
        s.add_source(five_min())
        assert served_opens(s) == expect, t


def test_the_bar_forming_at_t_is_withheld_and_counted_as_a_future_record(make_session):
    s = make_session("2023-01-20T14:07:30Z")                       # inside the 14:05 bar
    s.add_source(five_min())
    assert served_opens(s) == ["14:00"]
    assert s.receipt().blocked["future"] == 3                       # 14:05 (still forming), 14:10 and 14:15


def test_intraday_timeframes_use_their_own_length(make_session):
    rows = [{"open_time": "2023-01-20T14:00:00Z", "close": 1, "high": 1, "low": 1, "open": 1}]
    for tf, mins in (("1m", 1), ("15m", 15), ("1h", 60), ("4h", 240)):
        r = list(Bars(rows, name="b", timeframe=tf).records())[0]
        assert (r.available_at - r.event_time).total_seconds() == mins * 60, tf


# ---------------------------------------------------------------- a vendor cannot make a bar earlier
def test_a_vendor_availability_before_the_close_is_replaced_by_the_close_and_counted():
    rows = [{"open_time": "2023-01-20T14:00:00Z", "avail": "2023-01-20T14:00:00Z"},            # stamped at the open: a leak
            {"open_time": "2023-01-20T14:05:00Z", "avail": "2023-01-20T14:09:00Z"},            # stamped before the close
            {"open_time": "2023-01-20T14:10:00Z", "avail": "2023-01-20T14:20:00Z"}]            # vendor was slow: that is when it was knowable
    src = Bars(rows, name="b", timeframe="5m", available_at="avail")
    recs = list(src.records())
    assert [r.available_at.isoformat() for r in recs] == ["2023-01-20T14:05:00+00:00", "2023-01-20T14:10:00+00:00", "2023-01-20T14:20:00+00:00"]
    assert [("availability_moved_to_close" in r.flags) for r in recs] == [True, True, False]
    assert src.quality_notes() == {"partial_bars_withheld": 0, "availability_moved_to_close": 2}


def test_the_quality_summary_reports_what_the_bars_source_did(make_session):
    rows = [{"open_time": "2023-01-20T14:00:00Z", "avail": "2023-01-20T14:00:00Z", "open": 1, "high": 1, "low": 1, "close": 1}]
    s = make_session("2023-01-20T15:00:00Z")
    s.add_source(Bars(rows, name="b", timeframe="5m", available_at="avail"))
    q = s.quality("b")
    assert q["notes"] == {"availability_moved_to_close": 1, "partial_bars_withheld": 0} and "availability_moved_to_close" in q["flags"]
    s2 = make_session("2023-01-20T15:00:00Z", ledger_path=s.ledger.path.parent / "clean.db")          # a clean source: no flag
    s2.add_source(five_min())
    assert "notes" in s2.quality("b") and not (set(s2.quality("b")["flags"]) & {"availability_moved_to_close", "partial_bars_withheld"})


# ---------------------------------------------------------------- partial bars
PARTIAL = [{"open_time": "2023-01-20T14:00:00Z", "complete": True, "avail": "2023-01-20T14:05:00Z", "close": 1, "high": 1, "low": 1, "open": 1},
           {"open_time": "2023-01-20T14:05:00Z", "complete": False, "avail": "2023-01-20T14:07:00Z", "close": 2, "high": 2, "low": 2, "open": 2},       # a snapshot of the forming bar
           {"open_time": "2023-01-20T14:10:00Z", "complete": False, "avail": None, "close": 3, "high": 3, "low": 3, "open": 3}]


def test_partial_bars_are_never_served_by_default_and_are_counted(make_session):
    s = make_session("2023-01-20T14:08:00Z")
    src = Bars(PARTIAL, name="b", timeframe="5m", available_at="avail", complete="complete")
    s.add_source(src)
    assert served_opens(s) == ["14:00"] and src.quality_notes()["partial_bars_withheld"] == 2
    assert "partial_bars_withheld" in s.quality("b")["flags"]


def test_partial_bars_are_served_only_when_explicitly_allowed_and_are_labelled(make_session):
    s = make_session("2023-01-20T14:08:00Z")
    src = Bars(PARTIAL, name="b", timeframe="5m", available_at="avail", complete="complete", allow_partial=True)
    s.add_source(src)
    got = s.query("b")
    assert [r.payload["open_time"][11:16] for r in got] == ["14:00", "14:05"] and "partial_bar" in got[1].flags and "partial_bar" not in got[0].flags
    assert any("partial bars" in w for w in s.receipt().warnings)
    assert src.quality_notes()["partial_bars_withheld"] == 1                              # the one with no snapshot time is still withheld
    s2 = make_session("2023-01-20T14:06:00Z")                                              # before the snapshot was taken: not served either
    s2.add_source(Bars(PARTIAL, name="b", timeframe="5m", available_at="avail", complete="complete", allow_partial=True))
    assert served_opens(s2) == ["14:00"]


def test_complete_bars_alone_never_warn_about_partial_bars(make_session):
    s = make_session("2023-01-20T15:00:00Z")
    s.add_source(five_min())
    s.query("b")
    assert not any("partial" in w for w in s.receipt().warnings)


# ---------------------------------------------------------------- higher timeframes
MIXED = [{"open_time": "2023-01-23T00:00:00Z", "timeframe": "1w", "close": 5}, {"open_time": "2023-01-25T00:00:00Z", "timeframe": "1d", "close": 4},
         {"open_time": "2023-01-25T12:00:00Z", "timeframe": "1h", "close": 3}, {"open_time": "2023-01-25T13:00:00Z", "timeframe": "1h", "close": 2}]


@pytest.mark.parametrize("t,want", [
    ("2023-01-25T13:30:00Z", ["1h"]),                                         # only the 12:00 hour has closed: the day and the week are still forming
    ("2023-01-25T14:00:00Z", ["1h", "1h"]),
    ("2023-01-25T23:59:59Z", ["1h", "1h"]),
    ("2023-01-26T00:00:00Z", ["1h", "1h", "1d"]),                             # the day closes at midnight, inclusive
    ("2023-01-29T23:59:59Z", ["1h", "1h", "1d"]),
    ("2023-01-30T00:00:00Z", ["1h", "1h", "1d", "1w"]),                       # the week closes a week after its open
])
def test_daily_and_weekly_bars_exist_only_after_their_own_close(make_session, t, want):
    s = make_session(t)
    s.add_source(Bars(MIXED, name="b", timeframe=None, timeframe_field="timeframe"))
    assert sorted((r.payload["timeframe"] for r in s.query("b")), key=lambda x: {"h": 0, "d": 1, "w": 2}[x[-1]]) == want


def test_a_daily_bar_a_vendor_stamped_at_its_open_is_still_locked_until_the_close(make_session):
    rows = [{"open_time": "2023-01-25T00:00:00Z", "timeframe": "1d", "avail": "2023-01-25T00:00:00Z"}]
    s = make_session("2023-01-25T12:00:00Z")
    s.add_source(Bars(rows, name="b", timeframe=None, timeframe_field="timeframe", available_at="avail"))
    assert s.query("b") == [] and s.receipt().blocked["future"] == 1


def test_a_daily_bar_closes_at_the_session_close_in_its_own_zone_including_across_a_dst_change():
    rows = [{"open_time": "2023-01-20T00:00:00", "close": 1}, {"open_time": "2023-07-20T00:00:00", "close": 1}, {"open_time": "2023-03-10T00:00:00", "close": 1}]
    src = Bars(rows, name="b", timeframe="1d", session_close="16:00", session_tz="America/New_York", assume_tz="America/New_York")
    got = {r.payload["open_time"]: r.available_at.isoformat() for r in src.records()}
    assert got["2023-01-20T00:00:00"] == "2023-01-20T21:00:00+00:00"           # 16:00 EST
    assert got["2023-07-20T00:00:00"] == "2023-07-20T20:00:00+00:00"           # 16:00 EDT
    assert got["2023-03-10T00:00:00"] == "2023-03-10T21:00:00+00:00"           # the day before the clocks go forward


def test_a_daily_bar_that_opens_after_the_session_close_closes_at_the_next_one():
    rows = [{"open_time": "2023-01-20T17:00:00", "close": 1}]                       # a futures-style session that opens at 17:00 the evening before
    r = list(Bars(rows, name="b", timeframe="1d", session_close="16:00", session_tz="America/New_York", assume_tz="America/New_York").records())[0]
    assert r.available_at.isoformat() == "2023-01-21T21:00:00+00:00"


def test_an_explicit_close_time_column_wins():
    rows = [{"open_time": "2023-01-23T00:00:00Z", "timeframe": "1w", "close_time": "2023-01-27T21:00:00Z"}]
    r = list(Bars(rows, name="b", timeframe=None, timeframe_field="timeframe", close_time="close_time").records())[0]
    assert r.available_at == T("2023-01-27T21:00:00Z")                         # Friday's close, not a full week after the open


# ---------------------------------------------------------------- small things
def test_inconsistent_ohlc_is_flagged_and_kept():
    rows = [{"open_time": "2023-01-20T14:00:00Z", "open": 10, "high": 9, "low": 8, "close": 10}, {"open_time": "2023-01-20T14:05:00Z", "open": 10, "high": 12, "low": 9, "close": 11}]
    recs = list(Bars(rows, name="b", timeframe="5m").records())
    assert "inconsistent_ohlc" in recs[0].flags and "inconsistent_ohlc" not in recs[1].flags


def test_zone_less_open_times_are_not_assumed_to_be_utc():
    rows = [{"open_time": "2023-01-20T14:00:00", "close": 1}]
    r = list(Bars(rows, name="b", timeframe="5m").records())[0]
    assert r.available_at is None and r.event_time is None and "naive_event_time" in r.flags


def test_a_csv_file_is_a_bars_source(make_session, tmp_path):
    p = tmp_path / "bars.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["open_time", "open", "high", "low", "close"])
        w.writeheader()
        w.writerow({"open_time": "2023-01-20T14:00:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5})
    s = make_session("2023-01-20T14:05:00Z")
    s.add_source(Bars(path=p, name="b", timeframe="5m"))
    assert len(s.query("b")) == 1


@pytest.mark.parametrize("kw,msg", [({"timeframe": "5x"}, "not like"), ({"timeframe": "0m"}, "not like"), ({"timeframe": None}, "timeframe or a timeframe_field"),
                                    ({"session_close": "16:00"}, "needs session_tz"), ({"session_close": "4pm", "session_tz": "UTC"}, "HH:MM")])
def test_bad_construction_is_refused(kw, msg):
    with pytest.raises(SourceError, match=msg):
        Bars([{"open_time": "2023-01-20T14:00:00Z"}], name="b", **{"timeframe": "5m", **kw})
    with pytest.raises(SourceError, match="rows or a path"):
        Bars(name="b")


# ---------------------------------------------------------------- intrabar ambiguity
@pytest.mark.parametrize("side,high,low,stop,target,want", [
    ("buy", 112, 94, 95, 110, True), ("long", 112, 94, 95, 110, True),                         # both levels inside the bar
    ("buy", 108, 94, 95, 110, False), ("buy", 112, 97, 95, 110, False), ("buy", 108, 97, 95, 110, False),       # one level, the other, neither
    ("buy", 110, 95, 95, 110, True),                                                           # touching counts: the bar reached both
    ("sell", 112, 94, 110, 95, True), ("short", 112, 94, 110, 95, True),
    ("sell", 112, 97, 110, 95, False), ("sell", 108, 94, 110, 95, False), ("sell", 108, 97, 110, 95, False),
])
def test_intrabar_ambiguity_needs_both_levels_inside_the_bar(side, high, low, stop, target, want):
    assert intrabar_ambiguous(high, low, side, stop, target) is want


@pytest.mark.parametrize("args,msg", [((10, 5, "buy", 110, 95), "below"), ((10, 5, "sell", 95, 110), "above"), ((5, 10, "buy", 1, 2), "low is above"),
                                      ((10, 5, "flat", 1, 2), "side must be"), ((float("nan"), 5, "buy", 1, 2), "finite")])
def test_intrabar_ambiguity_refuses_nonsense(args, msg):
    with pytest.raises(ValueError, match=msg):
        intrabar_ambiguous(*args)


def test_bars_work_with_a_session_end_to_end_and_the_ledger_verifies(make_session):
    s = make_session("2023-01-20T14:12:00Z")
    s.add_source(five_min())
    s.query("b")
    s.decision("buy")
    assert s.verify_ledger(anchor="skip").ok and isinstance(s, Session)
