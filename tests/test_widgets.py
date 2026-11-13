"""The widget kit from the Python side: rolling Sharpe and the leak overlay (hand-worked), the plugin API, Session.show, deterministic HTML, and `strathora report --html`."""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from pathlib import Path

import pytest

from asof import widgets as W
from asof.cli import main as cli_main
from asof.widgets import compute, report

from conftest import ListSource, rec

FIX = Path(__file__).parent / "fixtures" / "widgets"


def day(n, hour=0):
    return f"2023-01-{n:02d}T{hour:02d}:00:00Z"


def rows4(late_last=True):
    """Four daily returns 0.01, 0.02, 0.03, 0.04. The fourth is published two days after its event when late_last."""
    return [{"event_time": day(i), "available_at": day(i) if i < 4 or not late_last else day(6), "ret": 0.01 * i} for i in range(1, 5)]


# ---------------------------------------------------------------- rolling Sharpe as of
def test_rolling_sharpe_is_worked_by_hand_when_everything_is_published_on_time():
    d = compute.rolling_sharpe(rows4(late_last=False), window=3, periods_per_year=1, at=[day(2), day(3), day(4)])
    p = {x["t"]: x for x in d["points"]}
    assert p[day(2)]["v"] is None and p[day(2)]["n"] == 2                                       # fewer than 3 rows: unknown, never 0
    assert p[day(3)]["v"] == pytest.approx(2.0)                                                  # mean .02, sample sd .01
    assert p[day(4)]["v"] == pytest.approx(3.0)                                                  # last three: .02 .03 .04 -> mean .03, sd .01


def test_a_return_published_after_the_point_is_not_used_however_early_its_event_time():
    d = compute.rolling_sharpe(rows4(late_last=True), window=3, periods_per_year=1, at=[day(4), day(6)])
    p = {x["t"]: x["v"] for x in d["points"]}
    assert p[day(4)] == pytest.approx(2.0)                                                       # the 4th return is dated the 4th but only knowable on the 6th: the 4th's point still sees rows 1-3
    assert p[day(6)] == pytest.approx(3.0)                                                       # once it is published it counts


def test_annualisation_and_degenerate_windows():
    d = compute.rolling_sharpe(rows4(False), window=3, at=[day(3)])                              # periods_per_year defaults to 252
    assert d["points"][0]["v"] == pytest.approx(2.0 * math.sqrt(252))
    flat = [{"event_time": day(i), "available_at": day(i), "ret": 0.01} for i in range(1, 5)]
    assert compute.rolling_sharpe(flat, window=3, at=[day(4)])["points"][0]["v"] is None         # zero variance: undefined, not infinity
    with pytest.raises(ValueError, match="window"):
        compute.rolling_sharpe(rows4(), window=1)


@pytest.mark.parametrize("bad", [{"event_time": day(1), "available_at": day(1)}, {"event_time": day(1), "available_at": day(1), "ret": float("nan")}, {"event_time": day(1), "available_at": day(1), "ret": True},
                                 {"event_time": "2023-01-01T00:00:00", "available_at": day(1), "ret": 0.1}])
def test_malformed_rows_are_refused_not_skipped(bad):
    with pytest.raises(Exception):
        compute.rolling_sharpe([bad], window=2)


def test_default_points_are_the_moments_a_return_became_knowable_and_the_result_is_deterministic():
    a, b = compute.rolling_sharpe(rows4(), window=3), compute.rolling_sharpe(list(reversed(rows4())), window=3)
    assert a == b and [p["t"] for p in a["points"]] == [day(1), day(2), day(3), day(6)] and a["rule"].startswith("each point uses only rows with available_at <=")


# ---------------------------------------------------------------- the leak overlay
def test_the_overlay_draws_locked_and_unlocked_and_the_gap_is_labelled_observed():
    d = compute.leak_overlay(rows4(), window=3, periods_per_year=1, at=[day(4), day(6)])
    assert d["label"] == "difference attributable to look-ahead (observed)"
    assert [x["v"] for x in d["locked"]] == [pytest.approx(2.0), pytest.approx(3.0)]
    assert [x["v"] for x in d["unlocked"]] == [pytest.approx(3.0), pytest.approx(3.0)]           # unlocked uses the 4th return at the 4th, as a backtest that ignores publication delay would
    assert d["summary"] == {"n_points": 2, "n_compared": 2, "max_abs_gap": pytest.approx(1.0), "mean_abs_gap": pytest.approx(0.5)}


def test_with_nothing_published_late_the_gap_is_zero_and_unknown_points_are_not_compared():
    d = compute.leak_overlay(rows4(late_last=False), window=3, periods_per_year=1, at=[day(2), day(3), day(4)])
    assert d["summary"]["max_abs_gap"] == 0 and d["summary"]["n_compared"] == 2 and d["summary"]["n_points"] == 3        # day 2 has too few rows in both runs
    none = compute.leak_overlay(rows4(), window=10, at=[day(4)])
    assert none["summary"]["max_abs_gap"] is None and none["summary"]["n_compared"] == 0


def test_the_synthetic_fixture_has_a_real_gap_because_every_seventh_return_is_late():
    rows = json.loads((FIX / "returns.json").read_text())
    s = compute.leak_overlay(rows, window=10, synthetic=True)
    assert s["synthetic"] is True and s["summary"]["max_abs_gap"] > 0 and s["summary"]["n_compared"] > 20


# ---------------------------------------------------------------- the plugin API
def test_rolling_sharpe_ships_as_the_example_plugin_and_the_builtins_are_listed():
    assert {"receipt", "clock-timeline", "quality-panel", "chain-status", "rolling-sharpe", "leak-overlay", "gap-breakdown"} <= set(W.names())


def test_register_a_plugin_use_it_through_show_and_unregister_it(make_session):
    calls = []

    def doubled(rows, **kw):
        calls.append(len(rows))
        return {"series": [{"name": "x2", "points": [{"t": r["event_time"], "v": r["ret"] * 2} for r in rows]}]}
    try:
        W.register("doubled", doubled, {"tag": "asof-series", "title": "Doubled returns"})
        w = make_session().show("doubled", rows=rows4(), synthetic=True)
        assert w.tag == "asof-series" and w.data["title"] == "Doubled returns" and w.synthetic and calls == [4]
        assert 'synthetic' in w.html(inline=False) and w.data["series"][0]["points"][1]["v"] == pytest.approx(0.04)
        with pytest.raises(ValueError, match="already registered"):
            W.register("doubled", doubled, {"tag": "asof-series"})
        W.register("doubled", doubled, {"tag": "asof-series", "title": "Again"}, replace=True)
    finally:
        W.unregister("doubled")
    assert "doubled" not in W.names()


@pytest.mark.parametrize("name,fn,spec,exc", [("bad name!", lambda: {}, {"tag": "asof-series"}, ValueError), ("x", "not callable", {"tag": "asof-series"}, TypeError), ("x", lambda: {}, {"tag": "my-own-element"}, ValueError),
                                              ("x", lambda: {}, {}, ValueError)])
def test_a_plugin_must_be_well_formed_and_can_only_use_the_kits_components(name, fn, spec, exc):
    with pytest.raises(exc):
        W.register(name, fn, spec)


def test_an_unknown_widget_name_lists_what_is_available(make_session):
    with pytest.raises(ValueError, match="rolling-sharpe"):
        make_session().show("nope")


# ---------------------------------------------------------------- Session.show and the HTML
def test_show_builds_the_builtin_widgets_from_a_real_session(make_session):
    s = make_session("2023-01-21T14:00:00Z")
    s.add_source(ListSource("d", [rec("a", "2023-01-01T00:00:00Z"), rec("later", "2023-06-01T00:00:00Z")]))
    s.query("d")
    r, t, q, c = s.show("receipt"), s.show("clock-timeline"), s.show("quality-panel"), s.show("chain-status")
    assert r.data["kind"] == "query" and r.data["blocked"]["future"] == 1 and r.tag == "asof-receipt"
    assert t.data["as_of"] == "2023-01-21T14:00:00Z" and len(t.data["events"]) >= 4 and c.data["status"] == "intact" and c.data["n_events"] >= 4
    assert q.tag == "asof-quality-panel" and set(q.data["by_severity"]) == {"error", "warning", "info"}
    att = json.loads((FIX / "attribution.json").read_text())
    assert s.show("gap-breakdown", attribution=att).data["gap"] == att["gap"] == -18.2373


def test_a_session_with_no_quality_summaries_says_not_recorded(make_session):
    assert make_session().show("quality-panel").data == {"recorded": False}


def test_the_html_is_deterministic_escaped_and_marks_synthetic_only_when_asked(make_session):
    d = json.loads((FIX / "chain.json").read_text())
    a, b = W.element("asof-chain-status", d), W.element("asof-chain-status", dict(reversed(list(d.items()))))
    assert a == b and "synthetic" not in a                                                      # key order does not change a byte
    assert W.element("asof-chain-status", d, synthetic=True).startswith('<asof-chain-status data="') and W.element("asof-chain-status", d, synthetic=True).count(" synthetic>") == 1
    evil = W.element("asof-receipt", {"call_id": "c", "target": '"><script>alert(1)</script>'})
    assert "<script>alert" not in evil and "&lt;script&gt;" in evil
    with pytest.raises(ValueError):
        W.element("my-own-element", {})


def test_a_widget_saves_a_self_contained_page(tmp_path, make_session):
    p = make_session().show("chain-status", synthetic=True).save(tmp_path / "w.html")
    h = p.read_text()
    assert h.startswith("<!DOCTYPE html>") and "<asof-chain-status" in h and "customElements.define" in h and "asof-w" in h
    assert not re.search(r'(src|href)="https?:', h) and "@import" not in h


# ---------------------------------------------------------------- gap breakdown: parts, unit, reconciliation
def _att(**kw):
    parts = [{"id": "costs", "name": "Costs", "value": -0.10, "label": "VERIFIED"}, {"id": "leakage", "name": "Look-ahead leakage", "value": -0.30, "label": "OBSERVED"},
             {"id": "data_issues", "name": "Data issues", "value": -0.50, "label": "OBSERVED"}, {"id": "unexplained", "name": "Unexplained", "value": -0.10, "label": "UNKNOWN"}]
    return {"gap": -1.0, "components": parts, "unit": "Sharpe", **kw}


def test_the_gap_breakdown_checks_that_the_parts_add_up_instead_of_trusting_the_input():
    assert compute.gap_breakdown(_att())["reconciles"] is True
    assert compute.gap_breakdown(_att(gap=-1.2, reconciles=True))["reconciles"] is False                            # an input that claims it reconciles is not believed
    broken = _att()
    broken["components"][0]["value"] = None
    assert compute.gap_breakdown(broken)["reconciles"] is None and compute.gap_breakdown({**_att(), "gap": None})["reconciles"] is None      # a missing part: unknown, not "yes"
    assert compute.gap_breakdown(_att())["unit"] == "Sharpe" and "unit" not in compute.gap_breakdown({k: v for k, v in _att().items() if k != "unit"})


def test_the_fixture_gap_has_leakage_and_data_issues_next_to_the_four_engine_parts():
    d = compute.gap_breakdown(json.loads((FIX / "attribution.json").read_text()))
    assert [c["id"] for c in d["components"]] == ["costs", "decay", "timing", "slippage", "leakage", "data_issues", "unexplained"] and d["reconciles"] is True


# ---------------------------------------------------------------- strathora report --html
def _ledger(tmp_path, privacy="full"):
    from asof import Session
    p = tmp_path / "ledger.db"
    s = Session("2023-01-21T14:00:00Z", ledger_path=p, privacy=privacy)
    s.add_source(ListSource("d", [rec("a", "2023-01-01T00:00:00Z"), rec("f", "2023-06-01T00:00:00Z")]))
    s.query("d")
    s.decision("hold", why="x")
    s.close()
    return p


def test_the_report_is_one_self_contained_file_with_every_widget_and_no_network(tmp_path):
    out = tmp_path / "r.html"
    assert cli_main(["report", "--ledger", str(_ledger(tmp_path)), "--html", str(out), "--synthetic", "--title", "Demo"]) == 0
    h = out.read_text()
    for tag in ("asof-chain-status", "asof-quality-panel", "asof-clock-timeline", "asof-receipt"):
        assert f"<{tag} " in h, tag
    assert "<title>Demo</title>" in h and h.count(" synthetic>") >= 4 and "customElements.define" in h
    assert not re.search(r'(src|href)="(https?:)?//', h) and "fetch(" not in h.split("</style>", 2)[-1].split("<script>")[0]
    assert "Made from the ledger alone: no network was used" in h and str(tmp_path) not in h                       # no local path leaks into a file you might send


def test_the_same_ledger_gives_byte_identical_reports(tmp_path):
    led = _ledger(tmp_path)
    a, b = report.build(led), report.build(led)
    assert a == b


def test_the_report_covers_a_chosen_session_and_says_when_there_is_nothing(tmp_path):
    led = _ledger(tmp_path)
    from asof import Ledger
    sid = Ledger(led).events()[0].session_id
    assert "asof-receipt" in report.build(led, session_id=sid)
    from asof.errors import AsOfError
    with pytest.raises(AsOfError, match="no events to report"):
        report.build(led, session_id="nope")
    with pytest.raises(AsOfError, match="no ledger"):
        report.build(tmp_path / "missing.db")


def test_a_tampered_ledger_is_reported_as_broken_not_hidden(tmp_path):
    import sqlite3
    led = _ledger(tmp_path)
    c = sqlite3.connect(led)
    c.execute("DROP TRIGGER events_no_update")
    c.execute("UPDATE events SET payload = replace(payload, 'hold', 'hxld') WHERE type='decision'")
    c.commit()
    c.close()
    h = report.build(led)
    assert "broken" in h and "&quot;status&quot;:&quot;intact&quot;" not in h


def test_the_report_command_runs_as_strathora_report(tmp_path):
    out = tmp_path / "s.html"
    r = subprocess.run([sys.executable, "-m", "asof", "report", "--ledger", str(_ledger(tmp_path)), "--html", str(out)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "one self-contained file" in r.stdout and out.exists()


# ---------------------------------------------------------------- compact mode and pinned fixtures
def test_compact_is_an_attribute_off_by_default_and_the_report_stays_detailed(tmp_path):
    data = {"ok": True, "n_events": 3, "head_hash": "abc"}
    assert " compact" not in W.element("asof-chain-status", data) and "<asof-chain-status data=" in W.element("asof-chain-status", data, compact=True) and W.element("asof-chain-status", data, compact=True).split(">")[0].endswith(" compact")
    assert " compact>" not in report.build(_ledger(tmp_path)) and " compact" not in re.sub(r"<style>.*?</style>|<script>.*?</script>", "", report.build(_ledger(tmp_path)), flags=re.S).replace("asof-compact", "")    # Verumark report: detailed


def test_show_passes_compact_through_without_handing_it_to_the_compute_function():
    w = W.show(None, "gap-breakdown", attribution=json.loads((Path(__file__).parent / "fixtures" / "widgets" / "attribution.json").read_text()), compact=True)
    assert w.compact is True and re.search(r"<asof-gap-breakdown [^>]*compact>", w.html(inline=False))
