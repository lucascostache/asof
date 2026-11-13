"""B2: presets. Each states its rules; each rule is enforced and tested."""
import json

import pytest

from asof import DecisionRequiredError, ClockError, sources
from asof.presets import PRESETS, PresetError, ToolDef, backtest_safe_agent, earnings_analysis, time_locked_research

from conftest import T
from test_sec import Fetcher, UA

START = "2023-11-04T00:00:00Z"


def sec(tmp_path):
    return sources.SECFilings(UA, [9999991], fetch=Fetcher(), cache_dir=tmp_path / "c", sleep=lambda s: None, include_older=False, acceptance_tz="UTC")


def macro(tmp_path):
    p = tmp_path / "macro.csv"
    p.write_text("series,period,value,release_time\nCPI,2023-09,100.0,2023-10-12T12:30:00Z\nCPI,2023-10,101.0,2023-11-14T13:30:00Z\n")
    return sources.MacroCalendar(p)


def prices(tmp_path):
    p = tmp_path / "prices.csv"
    p.write_text("symbol,date,close,available_at\nA,2023-01-02,1.0,2023-01-02T21:00:00Z\nA,2023-01-03,2.0,2023-01-03T21:00:00Z\nA,2023-01-04,3.0,2023-01-04T21:00:00Z\n")
    return sources.CSV(p, event_time="date", available_at="available_at", key=("symbol", "date"), assume_tz="UTC")


def news(query):
    return [{"title": "old", "published_at": "2023-10-01T00:00:00Z"}, {"title": "future", "published_at": "2023-12-01T00:00:00Z"}, {"title": "undated"}]


def test_every_preset_documents_its_rules_and_is_listed():
    assert set(PRESETS) == {"time_locked_research", "backtest_safe_agent", "earnings_analysis"}
    for name, factory in PRESETS.items():
        p = factory("2024-01-01T00:00:00Z") if name == "earnings_analysis" else factory()
        assert p.name == name and len(p.rules) >= 5 and name in p.describe() and all(r in p.describe() for r in p.rules)
        assert any("only moves forward" in r for r in p.rules) and any("allow_untimestamped is False" in r for r in p.rules)


# ------------------------------------------------------------------ time_locked_research
def test_time_locked_research_builds_with_sec_user_sources_and_a_proxied_news_tool(tmp_path):
    s = time_locked_research().build(START, sources=[sec(tmp_path), prices(tmp_path)], tools=[ToolDef("news", news, timestamp_field="published_at")], ledger_path=tmp_path / "l.db")
    assert s.allow_untimestamped is False and set(s.sources) == {"sec_filings", "prices"} and s.tools == ["news"]
    assert [i["title"] for i in s.tool("news", query="ACME-SYN")] == ["old"]                                          # future dropped, undated withheld
    assert [r.payload["form"] for r in s.query("sec_filings", form=["10-K"])] == ["10-K"]
    s.close()


def test_time_locked_research_refuses_missing_parts_and_attempts_to_loosen_it(tmp_path):
    p = time_locked_research()
    with pytest.raises(PresetError, match="SECFilings"):
        p.build(START, sources=[prices(tmp_path)], tools=[ToolDef("news", news, timestamp_field="published_at")], ledger_path=tmp_path / "l.db")
    with pytest.raises(PresetError, match="tool"):
        p.build(START, sources=[sec(tmp_path)], ledger_path=tmp_path / "l.db")
    for kw in ({"allow_untimestamped": True}, {"decision_gate": False}, {"max_as_of": "2099-01-01T00:00:00Z"}, {"whatever": 1}):
        with pytest.raises(PresetError, match="fixed by the preset"):
            p.build(START, sources=[sec(tmp_path)], tools=[ToolDef("n", news)], ledger_path=tmp_path / "l.db", **kw)
    s = p.build(START, sources=[sec(tmp_path)], tools=[ToolDef("news", news, timestamp_field="published_at")], ledger_path=tmp_path / "l2.db")
    with pytest.raises(ClockError):
        s.advance_to("2023-01-01T00:00:00Z")                                                          # forward-only
    s.close()


def test_a_preset_that_fails_to_build_leaves_no_open_session_behind(tmp_path):
    with pytest.raises(Exception):
        time_locked_research().build(START, sources=[sec(tmp_path), sources.CSV(tmp_path / "missing.csv", event_time="a", available_at="b")],
                                     tools=[ToolDef("news", news)], ledger_path=tmp_path / "l.db")


# ------------------------------------------------------------------ backtest_safe_agent
def test_backtest_safe_agent_accepts_only_local_sources_and_no_tools(tmp_path):
    p = backtest_safe_agent()
    with pytest.raises(PresetError, match="SECFilings source is not allowed"):
        p.build(START, sources=[sec(tmp_path)], ledger_path=tmp_path / "l.db")
    with pytest.raises(PresetError, match="tools are not allowed"):
        p.build(START, sources=[prices(tmp_path)], tools=[ToolDef("news", news)], ledger_path=tmp_path / "l.db")
    s = p.build("2023-01-03T00:00:00Z", sources=[prices(tmp_path), macro(tmp_path)], ledger_path=tmp_path / "l2.db")
    assert s.decision_gate and s.allow_untimestamped is False
    s.close()
    with pytest.raises(PresetError, match="fixed by the preset"):
        p.build(START, sources=[prices(tmp_path)], ledger_path=tmp_path / "l3.db", decision_gate=False)


def test_backtest_safe_agent_logs_a_decision_before_every_step(tmp_path):
    s = backtest_safe_agent().build("2023-01-02T22:00:00Z", sources=[prices(tmp_path)], ledger_path=tmp_path / "l.db")
    assert [r.payload["close"] for r in s.query("prices")] == [1.0]
    with pytest.raises(DecisionRequiredError, match="decision"):
        s.advance_to("2023-01-03T22:00:00Z")                                                           # acted, but no decision recorded
    assert s.as_of == T("2023-01-02T22:00:00Z") and s.events(type="clock_gate_rejected")
    s.decision("hold", reason="no signal")
    s.advance_to("2023-01-03T22:00:00Z")
    assert [r.payload["close"] for r in s.query("prices")] == [1.0, 2.0]
    s.decision("buy", size=1)
    s.advance_to("2023-01-04T22:00:00Z")
    decisions = s.events(type="decision")
    assert [d.data()["action"] for d in decisions] == ["hold", "buy"]
    assert [d.event_time for d in decisions] == ["2023-01-02T22:00:00Z", "2023-01-03T22:00:00Z"] == [d.available_at for d in decisions]     # stamped with the clock time
    s.advance_to("2023-01-05T22:00:00Z")                                                              # no action since the last advance: no decision needed
    s.advance_to("2023-01-05T22:00:00Z")                                                              # a no-op never trips the gate
    assert s.verify_ledger().ok
    s.close()


# ------------------------------------------------------------------ earnings_analysis
EARN = "2023-11-02T21:00:00Z"


def test_earnings_analysis_needs_sec_and_macro_and_a_start_before_earnings(tmp_path):
    p = earnings_analysis(EARN)
    with pytest.raises(PresetError, match="MacroCalendar"):
        p.build("2023-10-20T00:00:00Z", sources=[sec(tmp_path)], ledger_path=tmp_path / "l.db")
    with pytest.raises(PresetError, match="SECFilings"):
        p.build("2023-10-20T00:00:00Z", sources=[macro(tmp_path)], ledger_path=tmp_path / "l.db")
    for bad in (EARN, "2023-11-03T00:00:00Z"):
        with pytest.raises(PresetError, match="must be before"):
            p.build(bad, sources=[sec(tmp_path), macro(tmp_path)], ledger_path=tmp_path / "l.db")


def test_earnings_analysis_clock_can_never_reach_the_earnings_date(tmp_path):
    s = earnings_analysis(EARN).build("2023-10-20T00:00:00Z", sources=[sec(tmp_path), macro(tmp_path)], ledger_path=tmp_path / "l.db")
    assert s.clock.ceiling == T("2023-11-02T20:59:59.999999Z")
    s.advance_to("2023-11-02T20:59:59.999999Z")                                                        # one microsecond before: allowed
    for bad in (EARN, "2023-11-05T00:00:00Z"):
        with pytest.raises(ClockError, match="ceiling"):
            s.advance_to(bad)
    assert len(s.events(type="clock_ceiling_rejected")) == 2
    s.close()


def test_earnings_analysis_serves_only_what_was_knowable_before_the_date(tmp_path):
    s = earnings_analysis(EARN).build("2023-11-02T20:59:59.999999Z", sources=[sec(tmp_path), macro(tmp_path)], ledger_path=tmp_path / "l.db")
    forms = [r.payload["form"] for r in s.query("sec_filings", form=["10-K", "10-K/A", "10-Q"])]
    assert forms == []                                                                                 # the 10-K was accepted 2023-11-03, the amendment in December
    assert [r.payload["value"] for r in s.query("macro")] == [100.0]                                    # the October CPI came out 14 Nov: after the earnings date
    assert s.receipt().blocked["future"] == 1
    s.close()


def test_a_preset_is_a_plain_frozen_config_object():
    p = time_locked_research()
    with pytest.raises(Exception):
        p.name = "other"                                                                               # type: ignore[misc]
    json.dumps({"name": p.name, "rules": list(p.rules)})
