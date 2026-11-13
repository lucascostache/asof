"""The widget components (asof/widgets/asof-widgets.js), run under Node with the real file: snapshots of every widget from fixed synthetic fixtures, accessibility, determinism, escaping and
"no network, no clock, no randomness" in the source. Snapshots live in tests/snapshots/widgets; regenerate on purpose with UPDATE_SNAPSHOTS=1 and read the diff."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from asof.widgets import compute, css, js, redact

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "widgets"
SNAP = ROOT / "tests" / "snapshots" / "widgets"
JS = ROOT / "asof" / "widgets" / "asof-widgets.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed: the widget snapshot tests need it")


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def _custom_quality():
    q = load("quality")
    q["sources"][0]["thresholds"] = {**q["sources"][0]["thresholds"], "gap_factor": 99}               # a client's own tuning: the showcase view must show the defaults instead
    return q


CUSTOM_QUALITY = _custom_quality()
SECRET = "fixture-client-secret-not-a-real-key"
SHARED = ("receipt-shared", "rolling-sharpe-shared", "leak-overlay-shared", "gap-breakdown-shared")
PLAIN_ID = "ab" * 32
NO_SECRET = {**load("quality"), "issues": [{**load("quality")["issues"][0], "example_row_ids_hmac": None, "example_record_ids_sha256": [PLAIN_ID]}]}


def cases():
    rows, syn = load("returns"), {"synthetic": True}
    base = [
        {"name": "receipt", "widget": "receipt", "data": {**load("receipt"), **syn}},
        {"name": "clock-timeline", "widget": "clock-timeline", "data": {**load("timeline"), **syn}},
        {"name": "quality-panel", "widget": "quality-panel", "data": {**load("quality"), **syn}},
        {"name": "quality-panel-shared", "widget": "quality-panel", "data": {**redact.apply(load("quality"), "shared"), **syn}},
        {"name": "quality-panel-showcase", "widget": "quality-panel", "data": {**redact.apply(CUSTOM_QUALITY, "showcase"), **syn}},
        {"name": "quality-panel-no-secret", "widget": "quality-panel", "data": {**redact.apply(NO_SECRET, "internal", None), **syn}},
        {"name": "receipt-shared", "widget": "receipt", "data": {**redact.widget("asof-receipt", load("receipt"), "shared", SECRET), **syn}},
        {"name": "rolling-sharpe-shared", "widget": "rolling-sharpe", "data": redact.widget("asof-rolling-sharpe", load("rolling-sharpe"), "shared")},
        {"name": "leak-overlay-shared", "widget": "leak-overlay", "data": redact.widget("asof-leak-overlay", load("leak-overlay"), "shared")},
        {"name": "gap-breakdown-shared", "widget": "gap-breakdown", "data": redact.widget("asof-gap-breakdown", compute.gap_breakdown(load("attribution"), synthetic=True), "shared")},
        {"name": "quality-panel-clean", "widget": "quality-panel", "data": {"v": 1, "kind": "series", "n_records": 12, "by_severity": {"error": 0, "warning": 0, "info": 0}, "issues": [], "flags": [], **syn}},
        {"name": "quality-panel-not-recorded", "widget": "quality-panel", "data": {"recorded": False, **syn}},
        {"name": "chain-status", "widget": "chain-status", "data": {**load("chain"), **syn}},
        {"name": "chain-status-broken", "widget": "chain-status", "data": {"ok": False, "status": "broken", "n_events": 25, "first_bad_seq": 12, "reason": "payload does not match its recorded hash", "head_hash": "ab" * 32, **syn}},
        {"name": "rolling-sharpe", "widget": "rolling-sharpe", "data": load("rolling-sharpe")},
        {"name": "leak-overlay", "widget": "leak-overlay", "data": load("leak-overlay")},
        {"name": "gap-breakdown", "widget": "gap-breakdown", "data": compute.gap_breakdown(load("attribution"), synthetic=True)},
        {"name": "series", "widget": "series", "data": {"title": "Two series", "series": [{"name": "a", "points": [{"t": "2023-01-01T00:00:00Z", "v": 1}, {"t": "2023-01-02T00:00:00Z", "v": 3}]}], **syn}},
        {"name": "not-synthetic", "widget": "chain-status", "data": {"ok": True, "n_events": 3, "head_hash": "abc"}},
    ]
    compact = [{**c, "name": c["name"] + "-compact", "opts": {"compact": True}} for c in base if c["name"] not in ("not-synthetic", "quality-panel-not-recorded", "quality-panel-clean", "quality-panel-shared", "quality-panel-showcase", "quality-panel-no-secret", *SHARED)]
    return base + compact


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    f = tmp_path_factory.mktemp("w") / "cases.json"
    f.write_text(json.dumps(cases()))
    r = subprocess.run([NODE, str(ROOT / "tests" / "widgets_render.js"), str(JS), str(f)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


NAMES = [c["name"] for c in cases()]


@pytest.mark.parametrize("name", NAMES)
def test_each_widget_matches_its_snapshot(out, name):
    p = SNAP / f"{name}.html"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAP.mkdir(parents=True, exist_ok=True)
        p.write_text(out[name] + "\n")
    assert p.exists(), f"no snapshot for {name}: run with UPDATE_SNAPSHOTS=1, then read the file before committing it"
    assert out[name] + "\n" == p.read_text(), f"{name} changed: if that is intended, rerun with UPDATE_SNAPSHOTS=1 and review the diff"


def test_the_same_inputs_give_byte_identical_html_twice(out, tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps(cases()))
    again = json.loads(subprocess.run([NODE, str(ROOT / "tests" / "widgets_render.js"), str(JS), str(f)], capture_output=True, text=True, timeout=60).stdout)
    assert again == out


# ---------------------------------------------------------------- privacy views of the data-quality panel
def test_the_internal_view_shows_keyed_row_ids_and_lag_and_the_real_names(out):
    h = out["quality-panel"]
    assert "(keyed row ids)" in h and "median lag" in h and "returns" in h and "Thresholds used" in h and "hashed row ids" not in h


def test_the_shared_view_draws_no_row_ids_no_lag_and_no_real_source_name(out):
    h = out["quality-panel-shared"]
    assert "Source A" in h and "returns" not in h and "median lag" not in h and "<code>" not in h and "keyed row ids" not in h
    assert "Shared view: source names are aliased" in h and "Thresholds (generic defaults)" in h


def test_the_shared_view_draws_nothing_that_was_withheld_even_if_the_data_still_carries_it(out, tmp_path):
    leaky = {**load("quality"), "view": "shared"}                                                       # a summary marked shared that still holds ids and lag: the component must not draw them
    f = tmp_path / "c.json"
    f.write_text(json.dumps([{"name": "leaky", "widget": "quality-panel", "data": leaky}]))
    h = json.loads(subprocess.run([NODE, str(ROOT / "tests" / "widgets_render.js"), str(JS), str(f)], capture_output=True, text=True, timeout=60).stdout)["leaky"]
    assert "median lag" not in h and "<code>" not in h


def test_the_showcase_view_shows_the_generic_defaults_not_the_client_thresholds_and_keeps_them_collapsed(out):
    h = out["quality-panel-showcase"]
    assert "Thresholds (generic defaults)" in h and "gap factor: 5" in h and "gap factor: 99" not in h
    assert "returns" in h and "median lag" in h                                                          # the showcase view only changes the thresholds
    for name in ("quality-panel", "quality-panel-shared", "quality-panel-showcase"):
        assert not re.search(r"<details[^>]*\bopen\b", out[name]), name                                   # the thresholds are collapsed by default in every view


def test_with_no_client_secret_the_panel_says_ids_are_withheld_and_never_draws_the_unkeyed_hash(out):
    h = out["quality-panel-no-secret"]
    assert "no client secret is set" in h and PLAIN_ID[:10] not in h and "<code>" not in h


# ---------------------------------------------------------------- shared views of the performance widgets and the receipt
def _text(h):
    h = re.sub(r"<(style|script).*?</\1>", "", h, flags=re.S)
    return re.sub(r"<[^>]+>", " ", h)


def test_shared_rolling_sharpe_draws_no_values_no_axis_numbers_and_no_table(out):
    h, i = out["rolling-sharpe-shared"], out["rolling-sharpe"]
    assert "<table" not in h and "asof-endv" not in h and "Shared view" in h and "indexed 0–100" in h
    assert "asof-endv" in i and "<table" in i and "highest" in i and "highest" not in h                         # internal is unchanged: end value, table and the value tiles
    assert not re.search(r'class="asof-tick"[^>]*>-?\d+(?:\.\d+)?</text>', h) and re.search(r'class="asof-tick"[^>]*>\d{4}-\d\d-\d\d</text>', h)      # no y-axis numbers; the dates stay
    assert "<b>63</b>" in h                                                                                    # the window is a setting, not a result


def test_shared_leak_overlay_draws_no_values_and_no_table_but_keeps_the_gap(out):
    h, i = out["leak-overlay-shared"], out["leak-overlay"]
    assert "<table" not in h and "asof-endv" not in h and "of range" in h and "asof-band" in h and "asof-gbar-on" in h      # the shaded gap is still drawn
    assert "<table" in i and "asof-endv" in i


def test_shared_gap_breakdown_shows_percentages_only(out):
    h, i = _text(out["gap-breakdown-shared"]), _text(out["gap-breakdown"])
    assert "%" in h and "share of gap" in h and "withheld" in h and "return gap (actual" not in h
    a = load("attribution")
    raws = [f"{abs(c['value']):.2f}" for c in a["components"]] + [f"{abs(a['gap']):.2f}", f"{abs(a['headline']['sharpe_gap']):.2f}"]
    for raw in raws + [" pp", "Sharpe gap", "this run:"]:
        assert raw not in h, raw                                                                               # no value, no total, no Sharpe gap, no row counts in the shared view
    assert all(f"{c['value']:.2f} pp" in i for c in a["components"]) and f"{a['headline']['sharpe_gap']:.2f}" in i and "Sharpe gap (headline only)" in i      # internal still shows the values


def test_the_gap_breakdown_defines_leakage_and_data_issues_under_the_chart_in_every_full_view(out):
    for name in ("gap-breakdown", "gap-breakdown-shared"):
        h = out[name]
        assert "Look-ahead leakage</b> = rows used before their available_at" in h and "Data issues</b> = wrong or revised values (restatements, survivorship, bad rows)" in h and "never both" in h
        assert h.index("never both") > h.index("</ol>")                                                        # under the chart, not above it
    assert "(this run: 29 leakage, 40 data-issue, 0 in both)" in out["gap-breakdown"] and "this run:" not in out["gap-breakdown-shared"]
    assert "never both" not in out["gap-breakdown-compact"]                                                    # compact shows the headline numbers only


def test_shared_receipt_hides_T_and_the_raw_ids_and_shows_keyed_ids(out):
    h, i = out["receipt-shared"], out["receipt"]
    assert "withheld in the shared view" in h and "2022-10-24" not in h and "keyed row ids" in h and "r0" not in re.findall(r"<span class=\"asof-chip[^>]*>([^<]*)<", h)
    assert "2022-10-24" in i and "Session clock T" in i
    assert not re.search(r"\bchip\b[^>]*>r\d+<", h)


def test_the_availability_lag_is_stated_on_the_internal_widgets_and_withheld_in_the_shared_ones(out):
    assert "Availability lag" in out["receipt"] and "median 21.1 h, longest 120.9 d" in _text(out["receipt"])
    for name in ("rolling-sharpe", "leak-overlay"):
        assert "Availability lags the event: median 21.1 h, longest 120.9 d" in _text(out[name]), name
    for name in ("receipt-shared", "rolling-sharpe-shared", "leak-overlay-shared"):
        assert "lag" not in _text(out[name]).lower() and "120.9" not in out[name], name                        # lag statistics are not shown in a shared view
    assert "lag_seconds" not in json.dumps(redact.widget("asof-receipt", load("receipt"), "shared", SECRET))    # nor carried in its data
    assert "lag_seconds" not in json.dumps(redact.widget("asof-rolling-sharpe", load("rolling-sharpe"), "shared"))


@pytest.mark.parametrize("name", ["rolling-sharpe", "rolling-sharpe-shared", "leak-overlay", "leak-overlay-shared", "gap-breakdown", "gap-breakdown-shared"])
def test_every_widget_that_annualises_says_how_many_periods_and_which_calendar(out, name):
    h = _text(out[name])
    assert "252 periods a year" in h and "one return per business day, Mon–Fri" in h, name                  # annualising with 252 is only right for business days: the widget says which it is


# ---------------------------------------------------------------- gap breakdown wording
def test_the_gap_breakdown_says_attributed_not_explained_by_recorded_prices_and_names_its_unit_and_all_seven_parts(out):
    h = out["gap-breakdown"]
    assert "attributed" in h and "explained by recorded prices" not in h and "recorded prices cannot explain" not in h
    assert "return gap (actual − expected)" in h and "<span>≈ Sharpe gap (headline only)</span>" in h and " pp" in h       # the parts are in return; the Sharpe gap is one number, marked approximate
    assert "Sharpe gap = return gap ÷ annualised volatility" in h and "not split into parts" in h                                # and the method line stays
    t = _text(h)                                                                                                                 # the check anyone can do by hand, from what is printed on the widget
    tile = float(re.search(r"(-?\d+\.\d\d)\s+≈ Sharpe gap", t).group(1))
    gap = float(re.search(r"(-?\d+\.\d\d) pp\s+return gap", t).group(1))
    vol = re.search(r"volatility of the window used = (-?\d+\.\d\d) ÷ (\d+\.\d\d)% = (-?\d+\.\d\d)", t)
    assert vol and float(vol.group(1)) == gap and float(vol.group(3)) == tile and round(gap / float(vol.group(2)), 2) == tile     # volatility to 2 decimals; gap ÷ volatility is the tile
    assert "≈ Sharpe gap (headline only)" in out["gap-breakdown-compact"] and "Sharpe gap" not in out["gap-breakdown-shared"]
    for name in ("Costs", "Signal decay", "Timing", "Slippage", "Look-ahead leakage", "Data issues", "Unexplained"):
        assert name in h, name


# ---------------------------------------------------------------- accessibility
@pytest.mark.parametrize("name", [n for n in NAMES if n not in ("quality-panel-clean", "quality-panel-not-recorded")])
def test_every_widget_is_a_labelled_group_and_every_chart_has_a_text_alternative(out, name):
    h = out[name]
    assert re.match(r'<div class="asof-w [^"]*" role="group" aria-label="[^"]+">', h)
    for svg in re.findall(r"<svg\b[^>]*>", h):
        assert 'role="img"' in svg and re.search(r'aria-label="[^"]{12,}"', svg) and 'focusable="false"' in svg      # a summary a screen reader reads in place of the picture
    if "<svg" in h and not name.endswith("-compact") and name not in SHARED:
        assert '<details class="asof-alt">' in h and "<table>" in h and 'class="asof-sr"' in h                  # ...and the same numbers as a table, and a hidden text summary
    if name in SHARED and "<svg" in h:
        assert 'class="asof-sr"' in h and (name == "gap-breakdown-shared" or "<table" not in h)                     # the shared views withhold the value table on purpose (the gap one keeps a table of percentages); the text summary stays
    if "<svg" in h and name.endswith("-compact"):
        assert 'class="asof-sr"' in h                                                                              # compact drops the table but keeps the hidden text summary


@pytest.mark.parametrize("name", NAMES)
def test_no_widget_shows_markup_as_text_and_the_chain_head_is_the_hash(out, name):
    assert "&lt;p class" not in out[name] and "&lt;div" not in out[name] and "&lt;svg" not in out[name]            # escaped markup on screen would mean a variable held HTML where data was expected
    if name == "chain-status":
        assert load("chain")["head_hash"][:20] in out[name]


COMPACT = [c["name"] for c in cases() if c["name"].endswith("-compact")]


@pytest.mark.parametrize("name", COMPACT)
def test_compact_mode_is_headline_numbers_only(out, name):
    h, full = out[name], out[name[: -len("-compact")]]
    assert 'asof-compact' in h and "<table" not in h and "<details" not in h and "<dl" not in h and 'class="asof-ul"' not in h and 'class="asof-warn"' not in h and "<h3" not in h       # no tables, lists, notes or section headings
    n = len(re.findall(r'<div class="asof-tile[" ]', h))
    assert (n == 0 and name.startswith("series")) or 3 <= n <= 4, f"{name}: {n} headline numbers"
    assert len(h) < len(full)
    assert 'class="asof-synth"' in h                                                                               # still labelled synthetic


def test_a_chart_without_data_says_so_in_text_instead_of_drawing_nothing(out):
    r = subprocess.run([NODE, "-e", f"globalThis.HTMLElement=class{{}};globalThis.customElements={{get(){{}},define(){{}}}};eval(require('fs').readFileSync({json.dumps(str(JS))},'utf8'));"
                        "process.stdout.write(AsofWidgets.render('rolling-sharpe',{points:[{t:'2023-01-01T00:00:00Z',v:null,n:2}],window:5}))"], capture_output=True, text=True)
    assert "No point has enough data yet" in r.stdout and "<svg" not in r.stdout


def test_motion_is_never_required_and_is_switched_off_for_reduced_motion():
    c = css()
    assert "prefers-reduced-motion: reduce" in c and re.search(r"prefers-reduced-motion: reduce\) \{ \.asof-w \{ animation: none; \}", c)
    assert "setInterval" not in js() and "requestAnimationFrame" not in js()                                       # nothing animates from script


def test_the_screen_reader_summary_is_clipped_not_display_none():
    assert re.search(r"\.asof-sr \{[^}]*clip: rect\(0 0 0 0\)", css()) and not re.search(r"\.asof-sr \{[^}]*display: none", css())


# ---------------------------------------------------------------- the synthetic tag
def test_a_widget_from_synthetic_data_shows_the_tag_and_one_from_other_data_does_not(out):
    for n in NAMES:
        has = 'class="asof-synth"' in out[n]
        assert has is (n != "not-synthetic"), n
    assert 'class="asof-synth"' in out["__synthetic_attr"]                                                       # the element's own `synthetic` attribute works too


# ---------------------------------------------------------------- the elements, bad data, escaping
def test_every_component_is_defined_and_bad_data_is_an_error_box_not_an_exception(out):
    assert out["__tags"] == ["asof-chain-status", "asof-clock-timeline", "asof-gap-breakdown", "asof-leak-overlay", "asof-quality-panel", "asof-receipt", "asof-rolling-sharpe", "asof-series"]
    assert 'role="alert"' in out["__bad_json"] and "not valid JSON" in out["__bad_json"]
    assert 'role="alert"' in out["__empty"]
    assert sorted(out["__names"]) == ["chain-status", "clock-timeline", "gap-breakdown", "leak-overlay", "quality-panel", "receipt", "rolling-sharpe", "series"]


def test_text_in_the_data_is_escaped_never_run(tmp_path):
    evil = '<img src=x onerror=alert(1)>"\'&'
    f = tmp_path / "c.json"
    f.write_text(json.dumps([{"name": "x", "widget": "receipt", "data": {"call_id": "c", "kind": "query", "target": evil, "as_of": "2023-01-01T00:00:00Z", "served": 1, "warnings": [evil], "ledger_hash": evil}}]))
    h = json.loads(subprocess.run([NODE, str(ROOT / "tests" / "widgets_render.js"), str(JS), str(f)], capture_output=True, text=True, timeout=60).stdout)["x"]
    assert "<img" not in h and "&lt;img src=x onerror=alert(1)&gt;" in h and "&quot;" in h


# ---------------------------------------------------------------- the source itself
def test_the_script_makes_no_network_calls_reads_no_clock_and_uses_no_randomness():
    src = js()
    for banned in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "EventSource", "import(", "http://", "https://", "Math.random", "Date.now", "performance.now", "localStorage", "document.cookie"):
        assert banned not in src, banned
    assert not re.search(r"new Date\(\)", src)                                                                  # a Date is only ever made from a value in the data


def test_the_stylesheet_loads_nothing():
    c = css()
    assert not re.search(r"url\((?!#)", c) and "@import" not in c and "@font-face" not in c            # only in-document references (a gradient: url(#id)) are allowed; nothing is fetched
