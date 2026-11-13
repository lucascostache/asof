"""Execution events (SPEC 1.2, section 4.6): orders, fills and position marks, stamped honestly and shaped by the privacy mode."""
import json

import pytest

from asof.errors import AsOfError, ClockError
from conftest import T

START = "2023-01-20T15:00:00Z"
BAR_BOTH = {"high": 112.0, "low": 94.0}             # a 95 stop and a 110 target both lie inside
BAR_ONE = {"high": 108.0, "low": 94.0}              # only the stop does
BAR_NONE = {"high": 108.0, "low": 97.0}


def test_an_order_a_fill_and_a_mark_are_recorded_and_stamped_the_way_the_spec_says(make_session):
    s = make_session(START)
    d = s.decision("buy")
    s.order("o1", symbol="XYZ", side="buy", size=10, intended_price=100, expected_price=99.5, expected_source="backtest", decision=d)
    s.advance_to("2023-01-20T15:05:00Z")
    fid = s.fill("o1", price=100.2, size=10, fees=1.5, filled_at="2023-01-20T15:03:00Z", reference_price=100.1)
    s.mark("XYZ", size=10, price=101.0)
    [o] = s.events("order")
    [f] = s.events("fill")
    [m] = s.events("position_mark")
    assert (o.event_time, o.available_at) == (T(START).isoformat().replace("+00:00", "Z"),) * 2 and o.data()["decision_call_id"] == d and o.data()["expected_source"] == "backtest"
    assert (f.event_time, f.available_at) == ("2023-01-20T15:03:00Z", "2023-01-20T15:05:00Z") and f.data()["fill_id"] == fid == "o1-f1" and f.data()["terms"]["fees"] == 1.5
    assert (m.event_time, m.available_at) == ("2023-01-20T15:05:00Z",) * 2 and m.data()["terms"] == {"symbol": "XYZ", "size": 10.0, "price": 101.0, "marked_at": "2023-01-20T15:05:00Z"}
    assert s.verify_ledger(anchor="skip").ok


def test_a_fill_cannot_come_from_the_future_or_before_its_order(make_session):
    s = make_session(START)
    s.order("o1", symbol="X", side="buy", size=1)
    with pytest.raises(ClockError, match="has not happened yet"):
        s.fill("o1", price=1, size=1, filled_at="2023-01-20T15:00:01Z")
    s.advance_to("2023-01-20T16:00:00Z")
    with pytest.raises(AsOfError, match="before its order"):
        s.fill("o1", price=1, size=1, filled_at="2023-01-20T14:59:59Z")
    s.fill("o1", price=1, size=1, filled_at="2023-01-20T15:30:00Z")                          # now it has happened, and after the order
    with pytest.raises(ClockError):
        s.mark("X", size=1, price=1, marked_at="2023-01-20T16:00:01Z")
    assert len(s.events("fill")) == 1 and len(s.events("position_mark")) == 0                # the refused ones left nothing behind


def test_fills_cannot_exceed_the_order_and_ids_are_unique(make_session):
    s = make_session(START)
    s.order("o1", symbol="X", side="sell", size=10)
    s.fill("o1", price=5, size=6)
    s.fill("o1", price=5, size=4, fill_id="mine")
    with pytest.raises(AsOfError, match="exceed the order size"):
        s.fill("o1", price=5, size=0.001)
    with pytest.raises(AsOfError, match="already recorded"):
        s.order("o1", symbol="X", side="sell", size=1)
    with pytest.raises(AsOfError, match="not recorded"):
        s.fill("nope", price=5, size=1)
    assert [e.data()["fill_id"] for e in s.events("fill")] == ["o1-f1", "mine"]


@pytest.mark.parametrize("bad,kw", [("side", {"side": "long"}), ("size", {"size": 0}), ("size", {"size": float("nan")}), ("expected_price", {"expected_price": -1}), ("expected_source", {"expected_source": "guess"}),
                                    ("size", {"size": True})])
def test_bad_orders_are_refused_before_anything_is_written(make_session, bad, kw):
    s = make_session(START)
    args = {"symbol": "X", "side": "buy", "size": 1, **kw}
    with pytest.raises(AsOfError):
        s.order("o", **args)
    assert s.events("order") == []


def test_an_order_cannot_cite_a_decision_this_session_never_made_and_a_stop_must_be_on_the_right_side(make_session):
    s = make_session(START)
    with pytest.raises(AsOfError, match="was not recorded in this session"):
        s.order("o", symbol="X", side="buy", size=1, decision="abcdef012345")
    with pytest.raises(ValueError, match="stop must be below"):
        s.order("o", symbol="X", side="buy", size=1, stop_loss=110, take_profit=100)
    with pytest.raises(AsOfError, match="fees cannot be negative"):
        s.order("ok", symbol="X", side="buy", size=1)
        s.fill("ok", price=1, size=1, fees=-1)


def test_full_mode_keeps_the_terms_and_hashes_only_keeps_only_their_hash(make_session):
    s_full = make_session(START)
    s_full.order("o1", symbol="SECRETCO", side="buy", size=7, expected_price=123.456)
    s_full.fill("o1", price=124.5, size=7, fees=0.77)
    s_full.mark("SECRETCO", size=7, price=125.5)
    blob = json.dumps([e.data() for e in s_full.events() if e.type in ("order", "fill", "position_mark")])
    assert "SECRETCO" in blob and "123.456" in blob and "124.5" in blob and "125.5" in blob


def test_hashes_only_hides_every_number_and_the_symbol_but_keeps_what_links(tmp_path):
    from asof import Session
    s = Session(START, privacy="hashes_only", ledger_path=tmp_path / "h.db")
    d = s.decision("buy")
    s.order("o1", symbol="SECRETCO", side="buy", size=7, expected_price=123.456, decision=d)
    s.fill("o1", price=124.5, size=7, fees=0.77)
    s.mark("SECRETCO", size=7, price=125.5)
    evs = [e for e in s.events() if e.type in ("order", "fill", "position_mark")]
    blob = json.dumps([e.data() for e in evs])
    for needle in ("SECRETCO", "123.456", "124.5", "0.77", "125.5"):
        assert needle not in blob, needle
    o, f, m = evs
    assert o.data()["order_id"] == "o1" and o.data()["decision_call_id"] == d and "terms_sha256" in o.data() and "terms" not in o.data()
    assert f.data()["order_id"] == "o1" and f.data()["fill_id"] == "o1-f1" and "terms_sha256" in f.data() and set(m.data()) == {"terms_sha256"}
    assert s.verify_ledger(anchor="skip").ok
    s.close()


@pytest.mark.parametrize("bar,want", [(BAR_BOTH, True), (BAR_ONE, False), (BAR_NONE, False), (None, None)])
def test_a_fill_records_whether_stop_and_target_both_lay_inside_its_bar(make_session, bar, want):
    s = make_session(START)
    s.order("o1", symbol="X", side="buy", size=1, stop_loss=95, take_profit=110)
    s.fill("o1", price=100, size=1, bar=bar)
    assert s.events("fill")[0].data()["intrabar_ambiguous"] is want


def test_without_both_levels_ambiguity_is_not_assessed_even_with_a_bar(make_session):
    s = make_session(START)
    s.order("o1", symbol="X", side="buy", size=1, stop_loss=95)
    s.fill("o1", price=100, size=1, bar=BAR_BOTH)
    assert s.events("fill")[0].data()["intrabar_ambiguous"] is None


def test_a_record_can_be_passed_as_the_bar(make_session):
    from asof import Record
    s = make_session(START)
    s.order("o1", symbol="X", side="sell", size=1, stop_loss=110, take_profit=95)             # a short: stop above, target below
    s.fill("o1", price=100, size=1, bar=Record("b", "1", T("2023-01-20T14:00:00Z"), T("2023-01-20T15:00:00Z"), {"high": 112, "low": 94, "open": 100, "close": 101}))
    assert s.events("fill")[0].data()["intrabar_ambiguous"] is True
