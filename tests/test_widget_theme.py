"""The default theme has SQUARE corners: cards, tiles, pills, bars, buttons, chart rectangles. The older rounded look is opt-in (the `rounded` attribute / class `asof-rounded`).
Checked on computed styles in a real browser (the only place a corner radius really exists), on the source, and on the HTML the widgets make."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from asof.widgets import compute, css, element, page, redact, show

FIX = Path(__file__).parent / "fixtures" / "widgets"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def widgets(**kw):
    a = compute.gap_breakdown(load("attribution"), synthetic=True)
    items = [("asof-receipt", {**load("receipt"), "synthetic": True}), ("asof-clock-timeline", {**load("timeline"), "synthetic": True}), ("asof-quality-panel", {**load("quality"), "synthetic": True}),
             ("asof-chain-status", {**load("chain"), "synthetic": True}), ("asof-rolling-sharpe", load("rolling-sharpe")), ("asof-leak-overlay", load("leak-overlay")), ("asof-gap-breakdown", a),
             ("asof-gap-breakdown", redact.widget("asof-gap-breakdown", a, "shared"))]
    return "".join(element(t, d, synthetic=True, downloadable=True, **kw) for t, d in items) + "".join(element(t, d, synthetic=True, compact=True, **kw) for t, d in items[:7])


# ---------------------------------------------------------------- the source and the HTML
def test_the_theme_rule_is_last_in_the_stylesheet_so_nothing_above_it_can_round_a_corner():
    c = css()
    theme = c.index(".asof-w:not(.asof-rounded), .asof-w:not(.asof-rounded) * { border-radius: 0; }")
    assert theme > c.rindex("border-radius: ", 0, theme) and c.count(".asof-w:not(.asof-rounded)") == 3          # after every declared radius; no more rules after it that set one
    assert "border-radius" not in c[theme + 100:].replace(".asof-w:not(.asof-rounded) * { border-radius: 0; }", "")


def test_no_default_widget_html_has_a_rounded_marker_or_a_corner_radius_on_a_chart_rectangle(tmp_path):
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    root = Path(__file__).parent.parent
    cases = [{"name": "g", "widget": "gap-breakdown", "data": compute.gap_breakdown(load("attribution"))}, {"name": "t", "widget": "clock-timeline", "data": load("timeline")},
             {"name": "gc", "widget": "gap-breakdown", "data": compute.gap_breakdown(load("attribution")), "opts": {"compact": True}}, {"name": "r", "widget": "gap-breakdown", "data": compute.gap_breakdown(load("attribution")), "opts": {"rounded": True}}]
    f = tmp_path / "cases.json"
    f.write_text(json.dumps(cases))
    out = json.loads(subprocess.run([node, str(root / "tests" / "widgets_render.js"), str(root / "asof" / "widgets" / "asof-widgets.js"), str(f)], capture_output=True, text=True, timeout=60, check=True).stdout)
    for k in ("g", "t", "gc"):
        assert "asof-rounded" not in out[k] and not re.search(r'<rect[^>]* rx=', out[k]), k                      # the script draws no rounded chart rectangle
    assert "asof-rounded" in out["r"]                                                                          # and the option adds the class


def test_the_rounded_option_is_an_attribute_and_default_elements_carry_nothing():
    a = compute.gap_breakdown(load("attribution"))
    assert " rounded" not in element("asof-gap-breakdown", a) and " rounded" in element("asof-gap-breakdown", a, rounded=True)
    assert show(None, "gap-breakdown", attribution=load("attribution")).rounded is False and show(None, "gap-breakdown", attribution=load("attribution"), rounded=True).rounded is True
    assert " rounded" in show(None, "gap-breakdown", attribution=load("attribution"), rounded=True).html(inline=False)


# ---------------------------------------------------------------- computed styles in a browser
@pytest.fixture(scope="module")
def pw():
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                                          # noqa: BLE001  (no browser installed)
            pytest.skip(f"no Chromium for Playwright: {type(e).__name__}")
        yield b
        b.close()


CORNERS = """() => { const bad = [], radii = [], n = [0];
  for (const el of document.querySelectorAll('.asof-w, .asof-w *')) { n[0]++; const cs = getComputedStyle(el);
    for (const p of ['borderTopLeftRadius', 'borderTopRightRadius', 'borderBottomLeftRadius', 'borderBottomRightRadius']) { if (cs[p] !== '0px') bad.push((el.className.baseVal ?? el.className) + ' ' + p + '=' + cs[p]); }
    if (el.tagName.toLowerCase() === 'rect' && cs.rx !== '0px' && cs.rx !== 'auto') bad.push('rect rx=' + cs.rx); }
  return { bad, n: n[0] }; }"""


def _open(pw, tmp_path, body, name):
    f = tmp_path / name
    f.write_text(page(body, title="theme"))
    pg = pw.new_page(viewport={"width": 820, "height": 900}, reduced_motion="reduce")
    pg.goto(f.as_uri())
    pg.wait_for_timeout(300)
    return pg


def test_no_element_of_any_widget_has_a_rounded_corner_by_default(pw, tmp_path):
    pg = _open(pw, tmp_path, widgets(), "d.html")
    r = pg.evaluate(CORNERS)
    assert r["n"] > 500 and r["bad"] == [], r["bad"][:10]                                                       # hundreds of elements checked: cards, tiles, pills, badges, bars, tracks, buttons, rectangles
    pg.close()


def test_the_light_theme_and_the_buttons_are_square_too(pw, tmp_path):
    pg = _open(pw, tmp_path, '<div class="asof-light" style="background:#fff">' + widgets() + "</div>", "l.html")
    r = pg.evaluate(CORNERS)
    assert pg.locator("button.asof-download").count() > 5 and r["bad"] == [], r["bad"][:10]
    pg.close()


def test_the_rounded_option_gives_the_radii_back_so_the_default_is_a_real_choice(pw, tmp_path):
    pg = _open(pw, tmp_path, widgets(rounded=True), "r.html")
    r = pg.evaluate(CORNERS)
    assert len(r["bad"]) > 50                                                                                  # cards, tiles, pills, bars are rounded again
    card = pg.evaluate("getComputedStyle(document.querySelector('.asof-w')).borderTopLeftRadius")
    assert card == "18px"
    pg.close()
