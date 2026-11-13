"""The percentages of the gap breakdown are computed ONCE, from the unrounded values, by one function (compute.percent_shares; asof-widgets.js has the same rule for a payload that arrives without shares).
The internal view and the shared view read the same numbers, so they match, and the whole percents add up to 100."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from asof.widgets import compute, redact

ROOT = Path(__file__).parent.parent
FIX = Path(__file__).parent / "fixtures" / "widgets"
JS = ROOT / "asof" / "widgets" / "asof-widgets.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def attribution():
    return json.loads((FIX / "attribution.json").read_text())


# ---------------------------------------------------------------- the function
def test_the_shares_of_the_fixture_are_the_unrounded_shares_rounded_so_they_total_100():
    a = attribution()
    vals, gap = [c["value"] for c in a["components"]], a["gap"]
    s = compute.percent_shares(vals, gap)
    exact = [100 * Decimal(str(v)) / Decimal(str(gap)) for v in vals]
    assert sum(s) == 100 and all(abs(Decimal(x) - e) < 1 for x, e in zip(s, exact, strict=True))     # each is within one point of its exact share, and together they are 100


def test_naive_rounding_would_not_total_100_here_and_this_does():
    third = [1.0, 1.0, 1.0]
    assert [round(100 * v / 3) for v in third] == [33, 33, 33] and sum(round(100 * v / 3) for v in third) == 99       # what rounding each one separately gives
    assert compute.percent_shares(third) == [34, 33, 33] and sum(compute.percent_shares(third)) == 100               # the extra point goes to the first of equal remainders


def test_the_largest_remainder_gets_the_missing_points():
    s = compute.percent_shares([0.5, 0.3, 0.2, 0.0], 1.0)
    assert s == [50, 30, 20, 0]
    s = compute.percent_shares([1, 1, 1, 1, 1, 1, 1], 7.0)
    assert sum(s) == 100 and sorted(s) == [14, 14, 14, 14, 14, 15, 15]


def test_it_always_totals_100_over_many_inputs_including_mixed_signs():
    x = 12345
    for _ in range(300):
        vals = []
        for _ in range(7):
            x = (x * 1103515245 + 12345) % 2**31
            vals.append(round((x / 2**31 - 0.35) * 10, 4))
        total = round(sum(vals), 4)
        if total == 0:
            continue
        assert sum(compute.percent_shares(vals, total)) == 100, (vals, total)


def test_an_unknown_part_or_a_zero_total_gives_unknown_not_0_percent():
    assert compute.percent_shares([1.0, None, 2.0], 3.0) == [None, None, None]
    assert compute.percent_shares([1.0, -1.0], 0.0) == [None, None] and compute.percent_shares([]) == []
    assert compute.percent_shares([float("nan"), 1.0], 1.0) == [None, None]


# ---------------------------------------------------------------- the two views
def test_internal_and_shared_views_carry_the_same_shares_and_they_total_100():
    d = compute.gap_breakdown(attribution(), synthetic=True)
    s = redact.widget("asof-gap-breakdown", d, "shared", "a-secret-of-at-least-16-chars")
    internal, shared = [c["share"] for c in d["components"]], [c["share"] for c in s["components"]]
    assert internal == shared and sum(shared) == 100
    assert all("value" not in c for c in s["components"]) and s["gap"] is None                 # the shared view still withholds values and the total


def test_the_shared_view_does_not_calculate_its_own_percentages():
    d = compute.gap_breakdown(attribution(), synthetic=True)
    d["components"][0]["share"] = 77                                                           # whatever the payload says is what the shared view repeats: one source of truth
    assert redact.widget("asof-gap-breakdown", d, "shared")["components"][0]["share"] == 77


def test_a_shared_view_made_from_a_raw_attribution_gives_the_same_shares():
    raw = {"gap": -3.0, "components": [{"id": "a", "name": "A", "value": -1.0, "label": "OBSERVED"}, {"id": "b", "name": "B", "value": -1.0, "label": "OBSERVED"}, {"id": "c", "name": "C", "value": -1.0, "label": "UNKNOWN"}]}
    assert [c["share"] for c in redact.widget("asof-gap-breakdown", raw, "shared")["components"]] == [34, 33, 33] == [c["share"] for c in compute.gap_breakdown(raw)["components"]]


# ---------------------------------------------------------------- what is drawn
@pytest.fixture(scope="module")
def drawn(tmp_path_factory):
    a = attribution()
    d = compute.gap_breakdown(a, synthetic=True)
    mixed = {"gap": -2.0, "components": [{"id": "a", "name": "A", "value": -2.5, "label": "OBSERVED"}, {"id": "b", "name": "B", "value": 0.7, "label": "OBSERVED"}, {"id": "c", "name": "C", "value": -0.2, "label": "UNKNOWN"}]}
    thirds = {"gap": -3.0, "components": [{"id": "a", "name": "A", "value": -1.0, "label": "OBSERVED"}, {"id": "b", "name": "B", "value": -1.0, "label": "OBSERVED"}, {"id": "c", "name": "C", "value": -1.0, "label": "UNKNOWN"}]}
    cases = [{"name": "internal", "widget": "gap-breakdown", "data": d}, {"name": "shared", "widget": "gap-breakdown", "data": redact.widget("asof-gap-breakdown", d, "shared")},
             {"name": "raw", "widget": "gap-breakdown", "data": a},                                   # no `share` in it: the JavaScript fallback works them out
             {"name": "mixed-py", "widget": "gap-breakdown", "data": compute.gap_breakdown(mixed)}, {"name": "mixed-raw", "widget": "gap-breakdown", "data": mixed},
             {"name": "thirds-py", "widget": "gap-breakdown", "data": compute.gap_breakdown(thirds)}, {"name": "thirds-raw", "widget": "gap-breakdown", "data": thirds},
             {"name": "broken", "widget": "gap-breakdown", "data": compute.gap_breakdown({**thirds, "gap": -5.0})},                       # parts add up to -3, the gap says -5
             {"name": "compact", "widget": "gap-breakdown", "data": d, "opts": {"compact": True}}, {"name": "compact-shared", "widget": "gap-breakdown", "data": redact.widget("asof-gap-breakdown", d, "shared"), "opts": {"compact": True}}]
    f = tmp_path_factory.mktemp("p") / "c.json"
    f.write_text(json.dumps(cases))
    r = subprocess.run([NODE, str(ROOT / "tests" / "widgets_render.js"), str(JS), str(f)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def shown(html: str) -> list[int]:
    return [int(x) for x in re.findall(r'class="asof-gbs">(-?\d+)%<', html)]


def test_the_percentages_on_screen_match_between_internal_and_shared_and_total_100(drawn):
    i, s = shown(drawn["internal"]), shown(drawn["shared"])
    assert len(i) == 7 and i == s and sum(i) == 100


def test_the_tiles_agree_with_the_rows(drawn):
    for name in ("internal", "shared"):
        h = drawn[name]
        attributed = int(re.search(r"<b>(-?\d+)%</b><span>attributed", h).group(1))
        rows = shown(h)
        assert attributed == sum(rows[:-1]) == 100 - rows[-1]                                      # attributed = every part but Unexplained = 100 minus Unexplained's share
    assert re.search(r"<b>(\d+)%</b><span>unexplained \(of the gap\)", drawn["shared"]).group(1) == str(shown(drawn["shared"])[-1])


def test_a_payload_without_shares_gets_the_same_numbers_in_javascript_as_in_python(drawn):
    assert shown(drawn["raw"]) == shown(drawn["internal"])                                          # the fixture, raw
    assert shown(drawn["mixed-raw"]) == shown(drawn["mixed-py"]) == compute.percent_shares([-2.5, 0.7, -0.2], -2.0)
    assert shown(drawn["thirds-raw"]) == shown(drawn["thirds-py"]) == [34, 33, 33]
    assert sum(shown(drawn["mixed-raw"])) == 100 and sum(shown(drawn["thirds-raw"])) == 100


# ---------------------------------------------------------------- the "largest contributor" tile (replaces "parts add up")
def largest_tile(html: str):
    m = re.search(r'<div class="asof-tile asof-tile-name"><b>([^<]*)</b><span>largest contributor, (-?\d+)% of the gap</span>', html)
    return (m.group(1), int(m.group(2))) if m else None


@pytest.mark.parametrize("name", ["internal", "shared", "raw", "mixed-py", "thirds-py", "broken"])
def test_the_largest_contributor_tile_replaces_the_parts_add_up_tile(drawn, name):
    h = drawn[name]
    assert "<span>parts add up to the gap</span>" not in h and "<b>yes</b>" not in h and "largest contributor" in h             # the tile is gone (the sentence in the note below the chart stays)


def test_the_largest_contributor_is_the_part_with_the_biggest_share_and_matches_the_rows(drawn):
    for name in ("internal", "shared"):
        h = drawn[name]
        names = re.findall(r'class="asof-gbn">([^<]*)<', h)
        rows = shown(h)
        top = max(range(len(rows)), key=lambda i: (abs(rows[i]), -i))
        assert largest_tile(h) == (names[top], rows[top]) and names[top] == "Data issues" and rows[top] > 40      # what the tile says is what the longest bar says
    assert largest_tile(drawn["thirds-py"]) == ("A", 34)                                            # equal shares: the first part


def test_unexplained_can_be_the_largest_and_is_not_hidden():
    big = {"gap": -4.0, "components": [{"id": "a", "name": "A", "value": -1.0, "label": "OBSERVED"}, {"id": "unexplained", "name": "Unexplained", "value": -3.0, "label": "UNKNOWN"}]}
    assert [c["share"] for c in compute.gap_breakdown(big)["components"]] == [25, 75]


def test_a_gap_whose_parts_do_not_add_up_is_still_called_out_without_the_tile(drawn):
    h = drawn["broken"]
    assert "they do NOT here" in h and "<span>parts add up to the gap</span>" not in h


def test_compact_shows_four_numbers_including_the_largest_contributor(drawn):
    for name in ("compact", "compact-shared"):
        h = drawn[name]
        assert len(re.findall(r'<div class="asof-tile[" ]', h)) == 4 and "largest contributor" in h and "parts add up" not in h
